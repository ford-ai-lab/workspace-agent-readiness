# SPDX-License-Identifier: MIT
"""Run: python3 -m unittest discover -s tests -v   (from the ws-ready directory)"""
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import scan  # noqa: E402
import build_coverage  # noqa: E402

FIX = os.path.join(HERE, "fixtures")
COV = scan.Coverage(scan.DEFAULT_COVERAGE)


def run(name):
    findings, ai, n = scan.scan_dir(os.path.join(FIX, name), COV)
    scan.enrich(findings, COV)
    return findings, ai


def by_method(findings):
    out = {}
    for f in findings:
        out.setdefault(f.get("method", "UNRESOLVED"), []).append(f)
    return out


class CoverageTable(unittest.TestCase):
    def test_every_method_has_status_and_source(self):
        d = COV.data
        self.assertEqual(d["total"], len(d["methods"]))
        self.assertGreater(d["total"], 250)
        for r in d["methods"]:
            self.assertIn(r["status"], d["statuses"], r["method"])
            self.assertTrue(r["sources"], r["method"])
            self.assertTrue(all(u.startswith("https://") for u in r["sources"]))
            if r["status"] in ("covered", "partially covered", "partner access only"):
                self.assertTrue(r["tools"], r["method"])
            if r["status"] != "covered":
                self.assertTrue(r["note"], r["method"])
        self.assertEqual(sum(d["counts"].values()), d["total"])
        self.assertEqual(d["problems"], [])

    def test_partner_tools_never_count_as_covered(self):
        partner = set(COV.data["servers"]["gmail"]["partner_access_tools"])
        self.assertIn("send_message", partner)
        for r in COV.data["methods"]:
            if r["status"] in ("covered", "partially covered"):
                self.assertFalse({t["tool"] for t in r["tools"] if t["server"] == "gmail"} & partner, r["method"])

    def test_every_mapped_tool_is_published_by_google(self):
        for r in COV.data["methods"]:
            for t in r["tools"]:
                s = COV.data["servers"][t["server"]]
                known = set(s["tools_live"]) | set(s["tools_documented"]) | set(s["partner_access_tools"])
                self.assertIn(t["tool"], known, r["method"])

    def test_rebuild_from_cached_sources_is_stable(self):
        with open(build_coverage.SOURCES) as f:
            src = json.load(f)
        again = build_coverage.build(src)
        self.assertEqual(again["counts"], COV.data["counts"])
        self.assertEqual([m["status"] for m in again["methods"]], [m["status"] for m in COV.data["methods"]])


class GmailAssistant(unittest.TestCase):
    def setUp(self):
        self.findings, self.ai = run("gmail_assistant")
        self.m = by_method(self.findings)

    def test_finds_every_call_once(self):
        self.assertEqual(len(self.findings), 13)
        self.assertNotIn("UNRESOLVED", self.m)

    def test_statuses(self):
        want = {
            "gmail.users.messages.list": "partially covered", "gmail.users.messages.get": "partially covered",
            "gmail.users.threads.get": "partially covered", "gmail.users.labels.list": "covered",
            "gmail.users.labels.create": "covered", "gmail.users.messages.modify": "covered",
            "gmail.users.messages.send": "partner access only", "gmail.users.drafts.create": "covered",
            "gmail.users.drafts.update": "partner access only", "gmail.users.messages.attachments.get": "not available",
            "gmail.users.watch": "not available", "gmail.users.history.list": "not available",
            "gmail.users.messages.trash": "covered",
        }
        self.assertEqual({k: v[0]["status"] for k, v in self.m.items()}, want)

    def test_multiline_chain_reports_the_call_line(self):
        self.assertEqual(self.m["gmail.users.threads.get"][0]["line"], 31)

    def test_ai_import_detected(self):
        self.assertEqual(self.ai, [("assistant.py", "openai")])

    def test_search_counts_ten(self):
        q = scan.quota_math({"gmail.users.messages.list": 600, "gmail.users.messages.get": 600, "gmail.users.messages.send": 50}, COV, self.findings)
        g = q["servers"]["gmail"]
        self.assertEqual(g["requests_per_day"], 6600)
        self.assertFalse(g["within_standard"])
        # 6000 Premium units at $0.50/1K + 600 Medium at $0.20/1K = $3.00 + $0.12
        self.assertAlmostEqual(g["usd_per_day"], 3.12)
        self.assertAlmostEqual(g["usd_per_30_days_scaled"], 93.6)
        self.assertEqual([n["method"] for n in q["no_mcp_path"]], ["gmail.users.messages.send"])
        self.assertEqual(q["no_mcp_path"][0]["why"], "partner access only")


class DriveOrganiser(unittest.TestCase):
    def setUp(self):
        self.findings, self.ai = run("drive_organiser")
        self.m = by_method(self.findings)

    def test_statuses(self):
        self.assertEqual(len(self.findings), 11)
        self.assertEqual(self.m["drive.files.list"][0]["status"], "partially covered")
        self.assertEqual(self.m["drive.files.copy"][0]["status"], "covered")
        self.assertEqual(self.m["drive.files.export"][0]["status"], "covered")
        self.assertEqual(self.m["drive.permissions.list"][0]["status"], "covered")
        for gap in ("drive.permissions.create", "drive.changes.list", "drive.files.delete"):
            self.assertEqual(self.m[gap][0]["status"], "not available", gap)

    def test_update_calls_are_told_apart(self):
        ups = sorted(self.m["drive.files.update"], key=lambda f: f["line"])
        self.assertEqual(len(ups), 3)
        self.assertEqual([len(u["detail"]) for u in ups], [1, 1, 1])
        self.assertIn("moves the file", ups[0]["detail"][0])
        self.assertIn("renames the file", ups[1]["detail"][0])
        self.assertIn("trashes", ups[2]["detail"][0])
        self.assertTrue(all(u["status"] == "not available" for u in ups))

    def test_ai_import_detected_in_ts_and_package_json(self):
        self.assertEqual({a[1] for a in self.ai}, {"@anthropic-ai/sdk"})


class CalendarScheduler(unittest.TestCase):
    def setUp(self):
        self.findings, self.ai = run("calendar_scheduler")
        self.m = by_method(self.findings)

    def test_raw_urls_resolve_with_http_verb(self):
        want = {
            "calendar.calendarList.list": "partially covered", "calendar.freebusy.query": "partially covered",
            "calendar.events.list": "covered", "calendar.events.insert": "covered",
            "calendar.events.patch": "partially covered", "calendar.events.delete": "covered",
            "calendar.events.quickAdd": "not available", "calendar.acl.insert": "not available",
            "chat.spaces.messages.create": "covered", "sheets.spreadsheets.values.append": "partially covered",
            "tasks.tasks.insert": "covered",
        }
        got = {k: v[0]["status"] for k, v in self.m.items() if k != "UNRESOLVED"}
        self.assertEqual(got, want)

    def test_base_url_is_reported_not_guessed(self):
        self.assertEqual(len(self.m["UNRESOLVED"]), 1)
        self.assertEqual(self.m["UNRESOLVED"][0]["line"], 5)

    def test_quota_by_server(self):
        q = scan.quota_math({"calendar.events.list": 4000, "calendar.events.insert": 1500, "chat.spaces.messages.create": 100}, COV)
        cal = q["servers"]["calendar"]
        self.assertEqual(cal["requests_per_day"], 5500)
        self.assertFalse(cal["within_standard"])
        self.assertAlmostEqual(cal["usd_per_day"], 5500 / 1000 * 0.25)
        self.assertTrue(q["servers"]["chat"]["within_standard"])
        self.assertIsNone(q["servers"]["chat"]["usd_per_30_days_scaled"])


class NoFalsePositives(unittest.TestCase):
    def test_lookalike_code_is_ignored(self):
        findings, ai = run("not_google")
        self.assertEqual(findings, [])


class Cli(unittest.TestCase):
    def test_text_and_json_and_exit_codes(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = scan.main([os.path.join(FIX, "gmail_assistant"), "--daily", "13000"])
        self.assertEqual(code, 0)
        text = buf.getvalue()
        self.assertIn("PARTNER ONLY  gmail.users.messages.send", text)
        self.assertIn("OVER the Standard cap", text)
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = scan.main([os.path.join(FIX), "--json", "--fail-on-gaps"])
        self.assertEqual(code, 2)
        data = json.loads(buf.getvalue())
        self.assertEqual(len(data["findings"]), 36)


if __name__ == "__main__":
    unittest.main()

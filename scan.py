#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""ws-ready-scan: find Google Workspace REST API calls in a code directory and
report, for each one, which Google Workspace MCP tool replaces it or what the gap is.

Why: Google's Workspace AI Tools Addendum (effective 2027-02-01) says a tool that
"is or is driven by an AI Agent, an AI Harness or an AI model" must reach Workspace
data through Google's MCP servers. From that date, agents that call the REST APIs
directly "are limited to Standard Tier quotas, regardless of the tier selected".

    python3 scan.py PATH [--daily N] [--volume METHOD=N ...] [--json] [--fail-on-gaps]

No dependencies. Reads data/coverage.json next to this file (rebuild it with
build_coverage.py). Not affiliated with Google. Not legal advice. A static scan
cannot tell whether a call is driven by an AI model: you decide that.
"""
import argparse
import json
import os
import re
import sys

__version__ = "0.1.0"
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_COVERAGE = os.path.join(HERE, "data", "coverage.json")

CODE_EXT = {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".gs"}
TEXT_EXT = CODE_EXT | {".json", ".yaml", ".yml", ".sh", ".env", ".go", ".rb", ".java", ".kt", ".php", ".cs", ".rs", ".md", ".txt", ".toml", ".ipynb"}
SKIP_DIRS = {".git", "node_modules", "venv", ".venv", "env", "__pycache__", "dist", "build", ".next", "site-packages", "vendor", ".tox", ".mypy_cache"}
MAX_BYTES = 2_000_000

# what in a file shows that it talks to a given Google API
API_HINTS = {
    "gmail": r"""build\(\s*['"]gmail['"]|google\.gmail\(|@googleapis/gmail|gmail\.googleapis\.com|googleapis\.com/gmail/|auth/gmail|mail\.google\.com/|gmail_v1""",
    "drive": r"""build\(\s*['"]drive['"]|google\.drive\(|@googleapis/drive|googleapis\.com/drive/|auth/drive|drive_v[23]""",
    "calendar": r"""build\(\s*['"]calendar['"]|google\.calendar\(|@googleapis/calendar|googleapis\.com/calendar/|auth/calendar|calendar_v3""",
    "docs": r"""build\(\s*['"]docs['"]|google\.docs\(|@googleapis/docs|docs\.googleapis\.com|auth/documents|docs_v1""",
    "sheets": r"""build\(\s*['"]sheets['"]|google\.sheets\(|@googleapis/sheets|sheets\.googleapis\.com|auth/spreadsheets|sheets_v4""",
    "slides": r"""build\(\s*['"]slides['"]|google\.slides\(|@googleapis/slides|slides\.googleapis\.com|auth/presentations|slides_v1""",
    "chat": r"""build\(\s*['"]chat['"]|google\.chat\(|@googleapis/chat|chat\.googleapis\.com|auth/chat\.|chat_v1""",
    "tasks": r"""build\(\s*['"]tasks['"]|google\.tasks\(|@googleapis/tasks|tasks\.googleapis\.com|googleapis\.com/tasks/|auth/tasks|tasks_v1""",
    "keep": r"""build\(\s*['"]keep['"]|google\.keep\(|@googleapis/keep|keep\.googleapis\.com|auth/keep|keep_v1""",
    "people": r"""build\(\s*['"]people['"]|google\.people\(|@googleapis/people|people\.googleapis\.com|auth/contacts|auth/directory|people_v1""",
}
GOOGLE_HINT = r"googleapiclient|googleapis|google-api-python-client|google\.oauth2|gapi\.client|google-auth"
AI_HINT = (r"""^\s*(?:from|import)\s+(openai|anthropic|langchain\w*|langgraph|llama_index|google\.generativeai|google\.genai|vertexai|"""
           r"""litellm|mistralai|cohere|ollama|autogen|crewai|agents|mcp|smolagents|pydantic_ai)\b|"""
           r"""['"](openai|@anthropic-ai/sdk|@langchain/[\w-]+|langchain|ai|@ai-sdk/[\w-]+|@google/generative-ai|@google/genai|"""
           r"""@modelcontextprotocol/sdk|llamaindex|ollama|@mistralai/[\w-]+|cohere-ai|@openai/agents)['"]""")

# Drive v2 names and other common aliases -> v3 method ids
ALIASES = {
    ("drive", "files.insert"): "files.create", ("drive", "files.patch"): "files.update",
    ("drive", "files.trash"): "files.update", ("drive", "files.untrash"): "files.update",
    ("drive", "files.touch"): "files.update", ("drive", "permissions.insert"): "permissions.create",
    ("drive", "permissions.patch"): "permissions.update", ("drive", "comments.insert"): "comments.create",
    ("drive", "comments.patch"): "comments.update", ("drive", "replies.insert"): "replies.create",
    ("drive", "replies.patch"): "replies.update", ("drive", "files.get_media"): "files.get",
    ("drive", "files.export_media"): "files.export", ("drive", "files.list_next"): "files.list",
    ("gmail", "users.messages.list_next"): "users.messages.list", ("gmail", "users.threads.list_next"): "users.threads.list",
    ("calendar", "events.list_next"): "events.list",
}
ALIAS_NOTE = {
    "files.trash": "Drive v2 files.trash = v3 files.update with trashed=true: there is no Drive trash tool.",
    "files.untrash": "Drive v2 files.untrash = v3 files.update with trashed=false: no tool.",
    "files.touch": "Drive v2 files.touch = a metadata-only update: no tool.",
}
# what a drive files.update call is really doing, judged from the arguments near the call
DRIVE_UPDATE_KINDS = [
    (r"addParents|removeParents|add_parents|remove_parents", "moves the file (addParents/removeParents): NOT AVAILABLE, no move tool"),
    (r"\btrashed\b", "trashes or restores the file (trashed): NOT AVAILABLE, no Drive trash tool"),
    (r"""\bname\b|['"]title['"]\s*:""", "renames the file (name): NOT AVAILABLE, no rename tool"),
    (r"\bstarred\b|\bdescription\b|\bproperties\b|appProperties|contentRestrictions", "changes other metadata: NOT AVAILABLE, no metadata tool"),
    (r"media_body|\bmedia\s*:|MediaFileUpload|MediaIoBaseUpload|uploadType", "uploads new content: upload_file_revision (non-Google-format files only)"),
]

CHAIN_RE = re.compile(r"[A-Za-z_$][\w$]*(?:\s*\(\s*\))?(?:\s*\.\s*[A-Za-z_$][\w$]*(?:\s*\(\s*\))?)+\s*\(")
URL_RE = re.compile(r"""https://(?:www\.googleapis\.com|[a-z-]+\.googleapis\.com)/[^\s"'`<>\\,;]*""")
VERB_RES = [
    re.compile(r"""\b(?:requests|httpx|session|client|http|axios|got|ky|superagent|s|sess)\s*\.\s*(get|post|put|patch|delete)\s*\(""", re.I),
    re.compile(r"""\bmethod\s*[:=]\s*['"](GET|POST|PUT|PATCH|DELETE)['"]""", re.I),
    re.compile(r"""(?:-X|--request)\s*['"]?(GET|POST|PUT|PATCH|DELETE)\b""", re.I),
    re.compile(r"""\.request\(\s*['"](GET|POST|PUT|PATCH|DELETE)['"]""", re.I),
]

STATUS_ORDER = ["covered", "partially covered", "partner access only", "not available", "unknown"]


class Coverage:
    def __init__(self, path):
        with open(path) as f:
            self.data = json.load(f)
        self.by_id = {}
        self.suffix = {}      # tuple of lowercased tokens -> [(api, short id)]
        self.templates = []   # (compiled regex, http, api, short id)
        for row in self.data["methods"]:
            api = row["api"]
            short = row["method"].split(".", 1)[1]
            self.by_id[(api, short)] = row
            toks = tuple(t.lower() for t in short.split("."))
            for i in range(len(toks) - 1):
                if len(toks) - i >= 2:
                    self.suffix.setdefault(toks[i:], []).append((api, short, i == 0))
            m = re.match(r"https://([^/]+)/(.*)$", row["path"])
            host, path = m.group(1), m.group(2)
            hosts = {host}
            if path.split("/")[0] in ("gmail", "drive", "calendar", "tasks"):
                hosts.add("www.googleapis.com")
                hosts.add(path.split("/")[0] + ".googleapis.com")
            rx = "".join(r"[^/:?]+" if part.startswith("{") else re.escape(part) for part in re.split(r"(\{[^}]*\})", path))
            for h in hosts:
                self.templates.append((re.compile("^" + re.escape(h) + "/(?:upload/|resumable/upload/)?" + rx + "/?$"), row["http"], api, short))
        for (api, alias), target in ALIASES.items():
            toks = tuple(t.lower() for t in alias.split("."))
            if (api, target) in self.by_id:
                self.suffix.setdefault(toks, []).append((api, alias, True))

    def row(self, api, short):
        return self.by_id.get((api, ALIASES.get((api, short), short)))


def _call_span(text, open_pos, limit=3000):
    """Text of a call's argument list, from its opening parenthesis to the matching close."""
    depth = 0
    for i in range(open_pos, min(len(text), open_pos + limit)):
        ch = text[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[open_pos:i + 1]
    return text[open_pos:open_pos + limit]


def _tokens(chain):
    chain = chain.rstrip()[:-1]
    return [re.sub(r"\s*\(\s*\)$", "", seg.strip()) for seg in chain.split(".")]


def scan_text(text, cov, relpath):
    """Return a list of findings for one file's text."""
    findings = []
    ext = os.path.splitext(relpath)[1].lower()
    lines = text.split("\n")
    line_starts = [0]
    for ln in lines:
        line_starts.append(line_starts[-1] + len(ln) + 1)

    def lineno(pos):
        lo, hi = 0, len(line_starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if line_starts[mid] <= pos:
                lo = mid
            else:
                hi = mid - 1
        return lo + 1

    file_apis = {api for api, rx in API_HINTS.items() if re.search(rx, text)}
    google_file = bool(file_apis) or bool(re.search(GOOGLE_HINT, text))

    # --- client-library method chains (Python and JS/TS, also Apps Script advanced services)
    if ext in CODE_EXT:
        for m in CHAIN_RE.finditer(text):
            toks = _tokens(m.group(0))
            low = [t.lower() for t in toks]
            hit = None
            for start in range(0, len(low) - 1):
                cands = cov.suffix.get(tuple(low[start:]))
                if not cands:
                    continue
                before = low[:start]
                var_apis = {api for api, _s, _f in cands if any(api in b for b in before)}
                full = [c for c in cands if c[2]]
                pool = full or cands
                n = len(low) - start
                chosen, confidence = None, None
                by_var = [c for c in pool if c[0] in var_apis]
                by_file = [c for c in pool if c[0] in file_apis]
                if len(by_var) == 1:
                    chosen, confidence = by_var[0], "high"
                elif len(by_file) == 1:
                    chosen, confidence = by_file[0], "high"
                elif full and len({c[0] for c in full}) == 1 and n >= 3 and google_file:
                    chosen, confidence = full[0], "medium"
                elif full and len({c[0] for c in full}) == 1 and n >= 3:
                    chosen, confidence = full[0], "low"
                elif len(by_file) > 1 or len(by_var) > 1:
                    chosen, confidence = None, "ambiguous"
                    hit = {"ambiguous": sorted({"%s.%s" % (c[0], c[1]) for c in (by_var or by_file)})}
                if chosen:
                    hit = {"api": chosen[0], "short": chosen[1], "confidence": confidence}
                if hit:
                    break
            if not hit:
                continue
            ln = lineno(m.start() + len(m.group(0)) - 1)
            f = {"file": relpath, "line": ln, "via": "client", "code": lines[ln - 1].strip()[:160]}
            f.update(hit)
            if hit.get("api") == "drive" and ALIASES.get(("drive", hit["short"]), hit["short"]) == "files.update" and hit["short"] not in ALIAS_NOTE:
                window = _call_span(text, m.end() - 1)
                f["detail"] = [label for rx, label in DRIVE_UPDATE_KINDS if re.search(rx, window)]
            findings.append(f)

    # --- raw REST URLs (any text file)
    for i, raw in enumerate(lines):
        if "googleapis.com/" not in raw:
            continue
        line = re.sub(r"""["'`]\s*\+\s*[\w.$\[\]()]+\s*\+\s*["'`]""", "X", raw)  # "…/" + id + "/…"
        line = re.sub(r"\$\{[^}]*\}|\{[^}/]*\}|%[sd]|<[A-Za-z_]+>", "X", line)
        for m in URL_RE.finditer(line):
            url = m.group(0).split("?")[0].rstrip(".)/]}")
            hostpath = url[len("https://"):]
            if "/auth/" in hostpath or "$discovery" in hostpath or "/discovery/" in hostpath or "mcp.googleapis.com" in hostpath:
                continue
            v2 = False
            if "/drive/v2/" in hostpath:
                hostpath, v2 = hostpath.replace("/drive/v2/", "/drive/v3/"), True
            cands = [(http, api, short) for rx, http, api, short in cov.templates if rx.match(hostpath)]
            cands = sorted(set(cands))
            f = {"file": relpath, "line": i + 1, "via": "url", "code": raw.strip()[:160], "url": url}
            if not cands:
                if re.search(r"/(gmail|drive|calendar|tasks)/v\d|^(docs|sheets|slides|chat|keep|people|tasks|gmail)\.googleapis\.com/", hostpath):
                    f["unresolved"] = "Workspace REST URL, but the path does not match one method (built by string concatenation, or a base URL)."
                    findings.append(f)
                continue
            verb = None
            order = [i] + [j for d in (1, 2, 3) for j in (i + d, i - d) if 0 <= j < len(lines)]
            for j in order:
                for rx in VERB_RES:
                    vm = rx.search(lines[j])
                    if vm:
                        verb = vm.group(1).upper()
                        break
                if verb:
                    break
            pick = [c for c in cands if c[0] == verb] if verb else []
            note = None
            if not pick:
                gets = [c for c in cands if c[0] == "GET"]
                if len(cands) == 1:
                    pick = cands
                elif gets and not verb:
                    pick, note = gets, "HTTP verb not found near the URL; assumed GET"
                else:
                    f["ambiguous"] = sorted("%s.%s" % (c[1], c[2]) for c in cands)
                    findings.append(f)
                    continue
            if len(pick) > 1:
                f["ambiguous"] = sorted("%s.%s" % (c[1], c[2]) for c in pick)
                findings.append(f)
                continue
            f.update({"api": pick[0][1], "short": pick[0][2], "confidence": "high"})
            if note:
                f["assumed"] = note
            if v2:
                f["assumed"] = "Drive v2 URL matched against the v3 method list"
            findings.append(f)
    # one finding per (line, method)
    seen, out = set(), []
    for f in findings:
        key = (f["line"], f.get("api"), f.get("short"), f.get("url"), tuple(f.get("ambiguous", [])))
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out


def scan_dir(root, cov):
    findings, ai_files, nfiles = [], [], 0
    ai_rx = re.compile(AI_HINT, re.M)
    paths = []
    if os.path.isfile(root):
        paths = [root]
        root = os.path.dirname(root) or "."
    else:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            for fn in sorted(filenames):
                paths.append(os.path.join(dirpath, fn))
    for p in paths:
        ext = os.path.splitext(p)[1].lower()
        if ext not in TEXT_EXT:
            continue
        try:
            if os.path.getsize(p) > MAX_BYTES:
                continue
            with open(p, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        nfiles += 1
        rel = os.path.relpath(p, root)
        if ext in CODE_EXT or os.path.basename(p) in ("package.json", "requirements.txt", "pyproject.toml"):
            m = ai_rx.search(text)
            if m:
                ai_files.append((rel, next(g for g in m.groups() if g)))
        if "googleapis" in text or ext in CODE_EXT:
            findings += scan_text(text, cov, rel)
    return findings, ai_files, nfiles


def enrich(findings, cov):
    for f in findings:
        if "api" not in f:
            f["status"] = "unresolved"
            continue
        row = cov.row(f["api"], f["short"])
        f["method"] = row["method"]
        f["status"] = row["status"]
        f["tools"] = row["tools"]
        f["note"] = row["note"]
        f["sources"] = row["sources"]
        if f["short"] in ALIAS_NOTE and f["api"] == "drive":
            f["status"] = "not available"
            f["tools"] = []
            f["note"] = ALIAS_NOTE[f["short"]]
        elif f.get("detail"):
            if all("NOT AVAILABLE" in d for d in f["detail"]):
                f["status"] = "not available"
                f["tools"] = []
    return findings


def parse_volumes(args, findings):
    """Return {method id: calls per day}."""
    resolved = sorted({f["method"] for f in findings if "method" in f})
    vol = {}
    if args.daily and resolved:
        share = args.daily / len(resolved)
        vol = {m: share for m in resolved}
    for item in args.volume or []:
        if "=" not in item:
            raise SystemExit("--volume wants METHOD=N, e.g. gmail.users.messages.list=2000")
        k, v = item.split("=", 1)
        vol[k.strip()] = float(v)
    return vol


def quota_math(vol, cov, findings=None):
    q = cov.data["quota"]
    cap = q["mcp_standard_daily_requests_per_project_per_product"]
    price = q["unit_price_per_1000"]
    billed = set(q["scaled_mcp_billed_servers"])
    servers, no_path = {}, []
    status_override = {}
    for f in findings or []:
        if "method" in f:
            status_override.setdefault(f["method"], set()).add(f["status"])
    for method, n in sorted(vol.items()):
        api, short = method.split(".", 1)
        row = cov.row(api, short)
        if not row:
            no_path.append({"method": method, "calls_per_day": n, "why": "not in the coverage table"})
            continue
        sts = status_override.get(method, {row["status"]})
        usable = [t for t in row["tools"] if not t["partner_access"]]
        if not usable or sts <= {"not available", "unknown", "partner access only"}:
            why = "partner access only" if any(t["partner_access"] for t in row["tools"]) else "no generally available MCP tool"
            no_path.append({"method": method, "calls_per_day": n, "why": why})
            continue
        t = usable[0]
        s = servers.setdefault(t["server"], {"requests_per_day": 0.0, "lines": [], "usd_per_day": 0.0, "unpriced_requests": 0.0})
        units = n * t["daily_requests_per_call"]
        s["requests_per_day"] += units
        line = {"method": method, "tool": t["tool"], "calls_per_day": n, "requests_per_call": t["daily_requests_per_call"],
                "requests_per_day": units, "bucket": t["billing_bucket"]}
        if t["server"] in billed and t["billing_bucket"] in price:
            line["usd_per_day_scaled"] = units / 1000.0 * price[t["billing_bucket"]]
            s["usd_per_day"] += line["usd_per_day_scaled"]
        elif t["server"] in billed:
            s["unpriced_requests"] += units
        s["lines"].append(line)
    for name, s in servers.items():
        s["standard_cap"] = cap
        s["within_standard"] = s["requests_per_day"] <= cap
        s["billed_on_scaled"] = name in billed
        s["usd_per_30_days_scaled"] = round(s["usd_per_day"] * 30, 2) if name in billed else None
        s["usd_per_day"] = round(s["usd_per_day"], 4)
    return {"servers": servers, "no_mcp_path": no_path, "cap": cap}


def _fmt_n(n):
    return ("%d" % n) if abs(n - round(n)) < 1e-9 else ("%.1f" % n)


def render(findings, ai_files, nfiles, quota, cov, root):
    out = []
    w = out.append
    d = cov.data
    w("Workspace agent readiness scan  (ws-ready-scan %s)" % __version__)
    w("Scanned: %s  (%d text files)" % (root, nfiles))
    w("Coverage table: %d REST methods, Google sources fetched %s" % (d["total"], d["sources_fetched"]))
    w("")
    resolved = [f for f in findings if "method" in f]
    if not findings:
        w("No Google Workspace REST calls found.")
        return "\n".join(out) + "\n"
    counts = {}
    for f in findings:
        counts[f["status"]] = counts.get(f["status"], 0) + 1
    w("Call sites: %d   (%s)" % (len(findings), ", ".join("%s: %d" % (s, counts[s]) for s in STATUS_ORDER + ["unresolved"] if s in counts)))
    w("")
    w("Does the rule apply? Only where the call \"is or is driven by an AI Agent, an AI Harness or an AI model\"")
    w("(Workspace AI Tools Addendum, clause 1, effective 2027-02-01). A static scan cannot decide that for you.")
    if ai_files:
        w("AI library imports found in %d file(s): %s" % (len(ai_files), ", ".join("%s (%s)" % a for a in ai_files[:6]) + (" ..." if len(ai_files) > 6 else "")))
    else:
        w("No AI library import was found in this directory.")
    w("")
    label = {"covered": "COVERED", "partially covered": "PARTIAL", "partner access only": "PARTNER ONLY",
             "not available": "NOT AVAILABLE", "unknown": "UNKNOWN", "unresolved": "UNRESOLVED"}
    for f in sorted(findings, key=lambda f: (f["file"], f["line"])):
        w("%s:%d" % (f["file"], f["line"]))
        w("    %s" % f["code"])
        if "method" not in f:
            if f.get("ambiguous"):
                w("    UNRESOLVED  could be: %s" % ", ".join(f["ambiguous"]))
            else:
                w("    UNRESOLVED  %s" % f.get("unresolved", ""))
            w("")
            continue
        tools = ", ".join(("%s:%s" % (t["server"], t["tool"])) + ("*" if t["partner_access"] else "") for t in f["tools"])
        w("    %-13s %s  ->  %s" % (label[f["status"]], f["method"], tools or "no MCP tool"))
        for dline in f.get("detail") or []:
            w("        this call %s" % dline)
        if f.get("note"):
            w("        %s" % f["note"])
        if f.get("assumed"):
            w("        (%s)" % f["assumed"])
        if f.get("confidence") in ("low", "medium"):
            w("        (%s confidence: matched by method name%s)" % (f["confidence"], "" if f["confidence"] == "medium" else "; no Google client import seen in this file"))
        cost = [t for t in f["tools"] if t["daily_requests_per_call"] != 1]
        for t in cost:
            w("        %s counts as %d requests per call toward the %d/day limit" % (t["tool"], t["daily_requests_per_call"], d["quota"]["mcp_standard_daily_requests_per_project_per_product"]))
        w("")
    gaps = sorted({f["method"] for f in resolved if f["status"] in ("not available", "partner access only", "unknown")})
    w("What has no generally available MCP path (%d method%s):" % (len(gaps), "" if len(gaps) == 1 else "s"))
    for g in gaps:
        w("    " + g)
    if not gaps:
        w("    none")
    w("")
    w("If these calls are AI-driven and stay on REST after 2027-02-01: Google says they \"are limited to Standard")
    w("Tier quotas, regardless of the tier selected\", and the Addendum requires MCP. REST is not switched off.")
    w("")
    if quota:
        w("Quota arithmetic (MCP Standard Tier: %d requests per day, per project, per product; no charge)" % quota["cap"])
        for name, s in sorted(quota["servers"].items()):
            w("  %s MCP server: %s requests/day -> %s" % (
                name, _fmt_n(s["requests_per_day"]),
                "within Standard Tier (free)" if s["within_standard"] else "OVER the Standard cap by %s: needs the Scaled Tier and a quota increase request" % _fmt_n(s["requests_per_day"] - s["standard_cap"])))
            for ln in s["lines"]:
                w("      %-42s %8s calls x %2d = %8s requests  [%s]" % (
                    ln["method"] + " -> " + ln["tool"], _fmt_n(ln["calls_per_day"]), ln["requests_per_call"], _fmt_n(ln["requests_per_day"]),
                    ln["bucket"] or "bucket not published"))
            if s["billed_on_scaled"]:
                extra = "" if not s["unpriced_requests"] else " (plus %s requests/day whose bucket Google does not publish)" % _fmt_n(s["unpriced_requests"])
                w("      On the Scaled Tier from 2027-04-01 (billed from the first request): $%.2f/day, $%.2f per 30 days%s" % (s["usd_per_day"], s["usd_per_30_days_scaled"], extra))
            else:
                w("      Scaled Tier MCP billing currently covers Gmail, Calendar and Drive only; no price is published for this server.")
        for n in quota["no_mcp_path"]:
            w("  no MCP path: %s (%s calls/day): %s" % (n["method"], _fmt_n(n["calls_per_day"]), n["why"]))
        w("  Enrolling a project in the Scaled Tier \"removes free MCP allowances\" for the whole project.")
        w("")
    else:
        w("Add --daily N (calls per day, split evenly) or --volume METHOD=N for the quota arithmetic.")
        w("")
    w("Sources: %s" % " ".join(d["quota"]["sources"][:2]))
    w("Not affiliated with Google. Not legal advice. Table built by an AI agent from Google's public pages.")
    return "\n".join(out) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ws-ready-scan", description="Map Google Workspace REST calls in a code directory to Workspace MCP tools.")
    ap.add_argument("path")
    ap.add_argument("--coverage", default=DEFAULT_COVERAGE, help="path to coverage.json")
    ap.add_argument("--daily", type=float, help="total Workspace calls per day, split evenly across the methods found")
    ap.add_argument("--volume", action="append", metavar="METHOD=N", help="calls per day for one method, e.g. gmail.users.messages.list=2000 (repeatable)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--fail-on-gaps", action="store_true", help="exit 2 if any call has no generally available MCP path")
    ap.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args(argv)
    if not os.path.exists(args.path):
        raise SystemExit("no such path: %s" % args.path)
    cov = Coverage(args.coverage)
    findings, ai_files, nfiles = scan_dir(args.path, cov)
    enrich(findings, cov)
    vol = parse_volumes(args, findings)
    quota = quota_math(vol, cov, findings) if vol else None
    if args.json:
        json.dump({"version": __version__, "coverage_sources_fetched": cov.data["sources_fetched"], "files_scanned": nfiles,
                   "ai_imports": [{"file": a, "library": b} for a, b in ai_files], "findings": findings, "quota": quota}, sys.stdout, indent=1)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(render(findings, ai_files, nfiles, quota, cov, args.path))
    gaps = any(f["status"] in ("not available", "partner access only", "unknown") for f in findings)
    return 2 if (args.fail_on_gaps and gaps) else 0


if __name__ == "__main__":
    sys.exit(main())

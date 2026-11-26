#!/usr/bin/env python3
"""Build data/coverage.json: Google Workspace REST methods -> Workspace MCP tools.

Inputs (all Google's own public documents, cached in data/sources.json):
  - REST discovery documents (method lists)
  - each Workspace MCP server's tools/list response (Google documents this as an
    unauthenticated call on every MCP reference page)
  - the quota pages for Gmail, Drive and Calendar (request cost, billing bucket,
    and the list of Gmail tools that "require partner access")
  - the MCP reference page for Chat (its documented tool list is longer than
    what tools/list returns without credentials)

Usage:
  python3 build_coverage.py            # rebuild coverage.json from data/sources.json
  python3 build_coverage.py --fetch    # re-download the sources first, then rebuild
  python3 build_coverage.py --check    # rebuild in memory, exit 1 if coverage.json would change

The mapping itself (RULES below) is hand-curated and strict: a method gets
"covered" only when a published tool does that job; anything not shown by a
Google page is "unknown". Dependency-free. MIT licence.
"""
import datetime, json, os, re, sys, urllib.request
from html.parser import HTMLParser

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCES = os.path.join(HERE, "data", "sources.json")
OUT = os.path.join(HERE, "data", "coverage.json")
UA = "ws-ready-coverage-builder (AI agent; ford@readableforms.com)"
DEV = "https://developers.google.com"

APIS = {  # api -> (label, discovery url, rest reference url)
    "gmail": ("Gmail", "https://gmail.googleapis.com/$discovery/rest?version=v1", DEV + "/workspace/gmail/api/reference/rest"),
    "drive": ("Drive", "https://www.googleapis.com/discovery/v1/apis/drive/v3/rest", DEV + "/workspace/drive/api/reference/rest/v3"),
    "calendar": ("Calendar", "https://www.googleapis.com/discovery/v1/apis/calendar/v3/rest", DEV + "/workspace/calendar/api/v3/reference"),
    "docs": ("Docs", "https://docs.googleapis.com/$discovery/rest?version=v1", DEV + "/workspace/docs/api/reference/rest"),
    "sheets": ("Sheets", "https://sheets.googleapis.com/$discovery/rest?version=v4", DEV + "/workspace/sheets/api/reference/rest"),
    "slides": ("Slides", "https://slides.googleapis.com/$discovery/rest?version=v1", DEV + "/workspace/slides/api/reference/rest"),
    "chat": ("Chat", "https://chat.googleapis.com/$discovery/rest?version=v1", DEV + "/workspace/chat/api/reference/rest"),
    "tasks": ("Tasks", "https://tasks.googleapis.com/$discovery/rest?version=v1", DEV + "/workspace/tasks/reference/rest"),
    "keep": ("Keep", "https://keep.googleapis.com/$discovery/rest?version=v1", DEV + "/workspace/keep/api/reference/rest"),
    "people": ("People", "https://people.googleapis.com/$discovery/rest?version=v1", DEV + "/people/api/rest"),
}
MCP = {  # api -> (endpoint host+path prefix, reference page)
    "gmail": ("https://gmailmcp.googleapis.com/mcp/", DEV + "/workspace/gmail/api/reference/mcp"),
    "drive": ("https://drivemcp.googleapis.com/mcp/", DEV + "/workspace/drive/api/reference/mcp"),
    "docs": ("https://docsmcp.googleapis.com/mcp/", DEV + "/workspace/docs/api/reference/mcp"),
    "sheets": ("https://sheetsmcp.googleapis.com/mcp/", DEV + "/workspace/sheets/api/reference/mcp"),
    "slides": ("https://slidesmcp.googleapis.com/mcp/", DEV + "/workspace/slides/api/reference/mcp"),
    "calendar": ("https://calendarmcp.googleapis.com/mcp/", DEV + "/workspace/calendar/api/v3/reference/mcp"),
    "chat": ("https://chatmcp.googleapis.com/mcp/", DEV + "/workspace/chat/api/reference/mcp"),
    "tasks": ("https://tasksmcp.googleapis.com/mcp/", DEV + "/workspace/tasks/reference/mcp"),
    "people": ("https://people.googleapis.com/mcp/", DEV + "/people/api/mcp"),
}
QUOTA_PAGES = {
    "gmail": DEV + "/workspace/gmail/api/guides/quotas",
    "drive": DEV + "/workspace/drive/api/guides/quotas",
    "calendar": DEV + "/workspace/calendar/api/guides/quotas",
}
URL_TIERS = DEV + "/workspace/guides/quota-usage-tiers"
URL_PRICING = DEV + "/workspace/guides/quota-pricing"
URL_CONFIG = DEV + "/workspace/guides/configure-mcp-servers"
URL_ELICIT = DEV + "/workspace/guides/mcp-elicitation"
URL_BLOG = "https://workspaceupdates.googleblog.com/2026/11/mcp-servers-cli-standardized-usage-tiers-now-available.html"

STATUSES = ["covered", "partially covered", "partner access only", "not available", "unknown"]
C, P, PA, NA, U = STATUSES


# ---------------------------------------------------------------- fetching
def _get(url, data=None, headers=None):
    h = {"User-Agent": UA}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", "replace")


class _Tables(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tables, self.cur, self.row, self.cell = [], None, None, None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.cur = []
        elif tag == "tr" and self.cur is not None:
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.cell is not None and self.row is not None:
            self.row.append(re.sub(r"\s+", " ", "".join(self.cell)).strip())
            self.cell = None
        elif tag == "tr" and self.row is not None and self.cur is not None:
            self.cur.append(self.row)
            self.row = None
        elif tag == "table" and self.cur is not None:
            self.tables.append(self.cur)
            self.cur = None

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)


def _tables(html):
    p = _Tables()
    p.feed(html)
    return p.tables


def _walk_methods(node):
    out = []
    for m in node.get("methods", {}).values():
        out.append({"id": m["id"], "http": m["httpMethod"], "path": m.get("flatPath") or m["path"]})
    for r in node.get("resources", {}).values():
        out += _walk_methods(r)
    return out


def _tool_slim(t):
    desc = t.get("description", "")
    a = t.get("annotations", {})
    return {
        "name": t["name"],
        "read_only": bool(a.get("readOnlyHint")),
        "destructive": bool(a.get("destructiveHint")),
        "params": sorted(t.get("inputSchema", {}).get("properties", {}).keys()),
        "summary": re.sub(r"\s+", " ", desc)[:240],
        "listed_requests": re.findall(r"^- `(\w+)`:", desc, re.M),
    }


def fetch():
    today = datetime.date.today().isoformat()
    src = {"fetched": today, "rest": {}, "mcp": {}, "quota": {}, "batch_requests": {}}
    for api, (label, disc, _ref) in APIS.items():
        d = json.loads(_get(disc))
        base = d.get("rootUrl", "") + d.get("servicePath", "")
        src["rest"][api] = {
            "label": label, "discovery_url": disc, "revision": d.get("revision"), "version": d.get("version"),
            "base_url": base, "methods": sorted(_walk_methods(d), key=lambda m: m["id"]),
        }
        if api in ("docs", "sheets", "slides"):
            src["batch_requests"][api] = sorted(d["schemas"]["Request"]["properties"].keys())
    body = json.dumps({"method": "tools/list", "jsonrpc": "2.0", "id": 1}).encode()
    hdr = {"content-type": "application/json", "accept": "application/json, text/event-stream"}
    for api, (ep, ref) in MCP.items():
        entry = {"reference_url": ref, "endpoint": ep + "v1", "tools": [], "v1self": None, "documented": []}
        r = json.loads(_get(ep + "v1", body, hdr))
        entry["tools"] = [_tool_slim(t) for t in r["result"]["tools"]]
        if api != "people":  # Google: People and Classroom servers have no /mcp/v1self
            r2 = json.loads(_get(ep + "v1self", body, hdr))
            entry["v1self"] = sorted(t["name"] for t in r2["result"]["tools"])
        # tool names documented on the reference page (links .../mcp/tools_list/<name>)
        page = _get(ref)
        entry["documented"] = sorted(set(re.findall(r"/mcp/tools_list/([a-z_]+)", page)))
        src["mcp"][api] = entry
    # quota pages
    g = _get(QUOTA_PAGES["gmail"])
    gq = {}
    for t in _tables(g):
        if t and "Tool" in t[0] and "Daily request cost (billing units)" in t[0]:
            for row in t[1:]:
                name, qc, units, bucket = row[-4:]
                gq[name.rstrip("*")] = {"query_cost": int(qc), "daily_requests": int(units), "bucket": bucket, "partner": name.endswith("*")}
    m = re.search(r"Tools marked with\s*<code[^>]*>\*</code>(.*?)require\s+partner\s+access", g, re.S) or \
        re.search(r"Tools marked with(.*?)require\s+partner\s+access", g, re.S)
    partner_note = sorted(set(re.findall(r"<code[^>]*>([a-z_]+)</code>", m.group(1)))) if m else []
    src["quota"]["gmail"] = {"url": QUOTA_PAGES["gmail"], "tools": gq, "partner_tools_in_note": partner_note}
    dq = {}
    for t in _tables(_get(QUOTA_PAGES["drive"])):
        if t and "Tool" in t[0] and "Quota category" in t[0]:
            for row in t[1:]:
                name, cat, bucket = row[-3:]
                dq[name] = {"daily_requests": 1, "bucket": bucket, "category": cat}
    src["quota"]["drive"] = {"url": QUOTA_PAGES["drive"], "tools": dq}
    cq = {}
    for t in _tables(_get(QUOTA_PAGES["calendar"])):
        if t and "Tool" in t[0] and "Query cost" in t[0]:
            for row in t[1:]:
                name, qc = row[-2:]
                cq[name] = {"query_cost": int(qc), "daily_requests": 1, "bucket": "Plus"}
    src["quota"]["calendar"] = {"url": QUOTA_PAGES["calendar"], "tools": cq}
    with open(SOURCES, "w") as f:
        json.dump(src, f, indent=1, sort_keys=True)
        f.write("\n")
    return src


# ---------------------------------------------------------------- the mapping
NO_TOOL = "No tool in Google's published tool list for this server does this."
PERM_DELETE = ("No tool. Google: \"Moving items to the trash always requires per-call confirmation ... and permanent "
               "deletion is blocked for third-party agents.\"")
GMAIL_SETTINGS = "No tool: the Gmail MCP server has no tools for account settings."
PREVIEW = "Developer Preview only: needs membership of the Google Workspace Developer Preview Program; not generally available."
KEEP = ("No Keep MCP server is published. Google's 24 Nov 2026 post lists the GA servers \"(with Keep coming soon)\"; "
        "no date given.")
COMMENT_ONLY_EDITORS = ("Only on Docs, Sheets and Slides files, as an {req} request through update_doc / "
                        "update_spreadsheet / update_presentation. No tool for comments on other file types.")
READ_COMMENTS = ("Comments come back inside read_file_content (includeComments) or read_doc / get_spreadsheet / "
                 "read_presentation (commentsIncluded); no standalone comment lookup.")

# api -> {method id without the api prefix: (status, [tools], note)}; tools may be "server:tool" to cross servers
RULES = {
    "gmail": {
        "users.messages.list": (P, ["search_threads"], "Returns threads (each with up to 5 message summaries), not a flat message list. Counts as 10 requests per call toward the daily limit."),
        "users.messages.get": (P, ["get_message"], "No raw RFC 822 format (formats: PLAIN_TEXT, FULL_CONTENT, MINIMAL, METADATA_ONLY). Does not return drafts. Attachment metadata only."),
        "users.messages.send": (PA, ["send_message", "reply", "forward"], "send_message, reply and forward \"require partner access and aren't generally available\". Generally available fallback: create_draft, and the user sends it from Gmail."),
        "users.messages.modify": (C, ["update_message_labels", "label_message", "unlabel_message"], "Trash and Spam labels go through trash_message / mark_message_spam instead."),
        "users.messages.trash": (C, ["trash_message"], "Always asks the user to confirm each call."),
        "users.messages.untrash": (C, ["untrash_message"], "Does not return the message to the Inbox; add the INBOX label separately."),
        "users.messages.delete": (NA, [], PERM_DELETE),
        "users.messages.batchDelete": (NA, [], PERM_DELETE),
        "users.messages.batchModify": (P, ["update_message_labels", "label_thread", "unlabel_thread"], "No batch tool: one call per message or per thread, each counting as a request."),
        "users.messages.insert": (NA, [], NO_TOOL),
        "users.messages.import": (NA, [], NO_TOOL),
        "users.messages.attachments.get": (NA, [], "No published tool returns attachment bytes; get_message returns attachment metadata (filename, MIME type, id) only."),
        "users.threads.list": (C, ["search_threads"], "Counts as 10 requests per call toward the daily limit."),
        "users.threads.get": (P, ["get_thread"], "Draft messages in the thread are omitted; no raw format."),
        "users.threads.modify": (C, ["label_thread", "unlabel_thread"], ""),
        "users.threads.trash": (C, ["trash_thread"], "Always asks the user to confirm each call."),
        "users.threads.untrash": (C, ["untrash_thread"], ""),
        "users.threads.delete": (NA, [], PERM_DELETE),
        "users.drafts.create": (C, ["create_draft"], "Takes to/cc/bcc/subject/body/htmlBody/attachments fields, not a raw MIME message. Use replyToMessageId for a reply draft."),
        "users.drafts.list": (C, ["list_drafts"], "Counts as 10 requests per call toward the daily limit."),
        "users.drafts.get": (C, ["get_draft"], ""),
        "users.drafts.update": (PA, ["update_draft"], "update_draft \"require[s] partner access\". Generally available fallback: create a new draft."),
        "users.drafts.send": (U, [], "No generally available tool sends an existing draft. send_message is partner access only, and Google does not publish whether it accepts a draft id."),
        "users.drafts.delete": (NA, [], "No delete_draft tool in the reference, the live tool list or the quota table, although the approval guide names \"deleting a draft\" as a sensitive Gmail action."),
        "users.labels.list": (C, ["list_labels"], ""),
        "users.labels.get": (P, ["list_labels"], "No single-label lookup; list all labels and filter. Per-label message counts are not documented."),
        "users.labels.create": (C, ["create_label"], ""),
        "users.labels.update": (PA, ["update_label"], "update_label \"require[s] partner access\"."),
        "users.labels.patch": (PA, ["update_label"], "update_label \"require[s] partner access\"."),
        "users.labels.delete": (PA, ["delete_label"], "delete_label \"require[s] partner access\"."),
        "users.settings.filters.create": (PA, ["create_filter"], "create_filter \"require[s] partner access\"."),
        "users.settings.filters.list": (PA, ["list_filters"], "list_filters \"require[s] partner access\"."),
        "users.settings.filters.get": (PA, ["list_filters"], "Only through list_filters, which \"require[s] partner access\"."),
        "users.settings.filters.delete": (NA, [], "No tool, not even in the partner-access list."),
        "users.history.list": (NA, [], "No sync or history tool. An agent that keeps a local mirror of the mailbox has no MCP path."),
        "users.watch": (NA, [], "No push-notification tool. An agent woken by Pub/Sub on new mail has no MCP path."),
        "users.stop": (NA, [], "No push-notification tool."),
        "users.getProfile": (NA, [], "No Gmail tool. The People server's get_user_profile (Developer Preview) returns name and email only."),
    },
    "drive": {
        "files.list": (P, ["search_files", "list_recent_files"], "Different query language: \"Only terms in this list are supported\". No `fields` mask; results carry content snippets unless excluded."),
        "files.get": (C, ["get_file_metadata", "download_file_content"], "Metadata via get_file_metadata; bytes (alt=media) via download_file_content as base64."),
        "files.download": (C, ["download_file_content"], "Returned inline as base64, not as a long-running operation."),
        "files.export": (C, ["download_file_content", "read_file_content"], "Set exportMimeType; read_file_content gives a plain-language rendering that \"may be incomplete for very large files\"."),
        "files.create": (P, ["create_file"], "Content goes inline (textContent or base64Content); no resumable or multipart upload. Folders via the folder MIME type. Not every metadata field can be set."),
        "files.copy": (C, ["copy_file"], "Title and parent folder only."),
        "files.update": (P, ["upload_file_revision"], "Only new content for an existing non-Google-format file. Rename, move (addParents/removeParents), trash, star and every other metadata change have no tool. upload_file_revision is not on /mcp/v1self."),
        "files.delete": (NA, [], PERM_DELETE + " There is no Drive trash tool either."),
        "files.emptyTrash": (NA, [], PERM_DELETE),
        "files.generateIds": (U, [], "create_file's description mentions ids \"obtained from `generate_ids`\", but no generate_ids tool appears in the published tool list."),
        "files.watch": (NA, [], "No change-notification tool."),
        "files.listLabels": (NA, [], NO_TOOL),
        "files.modifyLabels": (NA, [], NO_TOOL),
        "files.generateCseToken": (NA, [], NO_TOOL),
        "changes.list": (NA, [], "No change-feed tool. An agent that syncs or indexes a Drive incrementally has no MCP path."),
        "changes.getStartPageToken": (NA, [], "No change-feed tool."),
        "changes.watch": (NA, [], "No change-notification tool."),
        "channels.stop": (NA, [], "No change-notification tool."),
        "permissions.list": (C, ["get_file_permissions"], ""),
        "permissions.get": (P, ["get_file_permissions"], "Lists every permission on the file; no single-permission lookup."),
        "permissions.create": (NA, [], "No share tool: an agent cannot share a file or folder through the Drive MCP server."),
        "permissions.update": (NA, [], "No share tool."),
        "permissions.delete": (NA, [], "No unshare tool."),
        "comments.list": (P, ["read_file_content", "docs:read_doc", "sheets:get_spreadsheet", "slides:read_presentation"], READ_COMMENTS),
        "comments.get": (P, ["read_file_content"], READ_COMMENTS),
        "comments.create": (P, ["docs:update_doc", "sheets:update_spreadsheet", "slides:update_presentation"], COMMENT_ONLY_EDITORS.format(req="insertComment")),
        "comments.update": (P, ["docs:update_doc", "sheets:update_spreadsheet", "slides:update_presentation"], COMMENT_ONLY_EDITORS.format(req="updateCommentPost")),
        "comments.delete": (P, ["docs:update_doc", "sheets:update_spreadsheet", "slides:update_presentation"], COMMENT_ONLY_EDITORS.format(req="deleteComment")),
        "replies.list": (P, ["read_file_content"], READ_COMMENTS),
        "replies.get": (P, ["read_file_content"], READ_COMMENTS),
        "replies.create": (P, ["docs:update_doc", "sheets:update_spreadsheet", "slides:update_presentation"], COMMENT_ONLY_EDITORS.format(req="addCommentReply")),
        "replies.update": (U, [], "updateCommentPost exists for Docs, Sheets and Slides; whether it edits replies is not stated."),
        "replies.delete": (P, ["docs:update_doc", "sheets:update_spreadsheet", "slides:update_presentation"], COMMENT_ONLY_EDITORS.format(req="deleteCommentReply")),
        "revisions.get": (P, ["download_file_content"], "Revision content by revisionId only; no revision metadata, and no tool lists revision ids."),
    },
    "calendar": {
        "events.list": (C, ["list_events", "search_events"], "search_events is semantic search on the primary calendar only."),
        "events.get": (C, ["get_event"], ""),
        "events.insert": (C, ["create_event"], ""),
        "events.update": (P, ["update_event", "respond_to_event"], "update_event's input schema has no recurrence field (create_event has recurrenceData). RSVP changes go through respond_to_event."),
        "events.patch": (P, ["update_event", "respond_to_event"], "update_event's input schema has no recurrence field (create_event has recurrenceData). RSVP changes go through respond_to_event."),
        "events.delete": (C, ["delete_event"], "Not on /mcp/v1self. Costs 10 query units against the per-minute quota."),
        "events.instances": (U, [], "Not stated whether list_events expands recurring events into instances."),
        "events.quickAdd": (NA, [], "No free-text add tool; build the event fields and call create_event."),
        "events.move": (NA, [], "No tool moves an event to another calendar."),
        "events.import": (NA, [], NO_TOOL),
        "events.watch": (NA, [], "No push-notification tool."),
        "freebusy.query": (P, ["suggest_time"], "Returns suggested time periods across calendars, not the raw busy intervals."),
        "calendarList.list": (P, ["list_calendars"], "\"This tool returns a subset of all accessible calendars\"."),
        "calendarList.get": (P, ["list_calendars"], "No single-calendar lookup; list and filter. \"returns a subset of all accessible calendars\"."),
        "calendars.get": (P, ["list_calendars"], "No single-calendar lookup; list and filter."),
    },
    "docs": {
        "documents.get": (C, ["read_doc"], "\"Corresponds to documents.get in the REST API.\""),
        "documents.create": (C, ["drive:create_file"], "On the Drive server: create_file with mimeType application/vnd.google-apps.document (counts against Drive's quota)."),
        # documents.batchUpdate is computed from the request lists
    },
    "sheets": {
        "spreadsheets.get": (C, ["get_spreadsheet"], "\"Corresponds to spreadsheets.get in the REST API\"."),
        "spreadsheets.create": (C, ["drive:create_file"], "On the Drive server: create_file with mimeType application/vnd.google-apps.spreadsheet (counts against Drive's quota)."),
        "spreadsheets.values.get": (C, ["get_values"], "\"Corresponds to spreadsheets.values.get in the REST API\"."),
        "spreadsheets.values.update": (C, ["update_values", "update_formulas"], "\"Corresponds to spreadsheets.values.update in the REST API\"."),
        "spreadsheets.values.append": (P, ["update_spreadsheet", "update_values"], "No append tool: send an appendCells request through update_spreadsheet, or find the next row and call update_values. The automatic table detection of values.append is lost."),
        "spreadsheets.values.clear": (P, ["update_spreadsheet"], "No clear tool: express it as an updateCells request through update_spreadsheet."),
        "spreadsheets.values.batchGet": (P, ["get_spreadsheet", "get_values"], "No batch tool: get_spreadsheet with ranges, or one get_values call per range (each a request)."),
        "spreadsheets.values.batchUpdate": (P, ["update_values", "update_spreadsheet"], "No batch tool: one update_values call per range (each a request), or updateCells requests through update_spreadsheet."),
        "spreadsheets.values.batchClear": (P, ["update_spreadsheet"], "No clear tool: updateCells requests through update_spreadsheet."),
        "spreadsheets.sheets.copyTo": (NA, [], "duplicateSheet (through update_spreadsheet) copies within one spreadsheet only; no tool copies a sheet to another spreadsheet."),
    },
    "slides": {
        "presentations.get": (C, ["read_presentation"], "\"Corresponds to presentations.get in the REST API.\""),
        "presentations.pages.get": (C, ["read_slide_page"], "\"Corresponds to presentations.pages.get in the REST API.\""),
        "presentations.pages.getThumbnail": (C, ["read_slide_page_thumbnail"], "\"Corresponds to presentations.pages.getThumbnail in the REST API.\""),
        "presentations.create": (C, ["drive:create_file"], "On the Drive server: create_file with mimeType application/vnd.google-apps.presentation (counts against Drive's quota)."),
    },
    "chat": {
        "spaces.messages.list": (P, ["list_messages"], "Returned as Markdown; \"Private messages (messages only visible to a single user) are filtered out.\""),
        "spaces.messages.search": (C, ["search_messages"], ""),
        "spaces.messages.create": (C, ["send_message"], "Needs a configured Chat app in the Cloud project. Not on /mcp/v1self."),
        "spaces.messages.get": (P, ["list_messages", "search_messages"], "No single-message lookup; list or search and filter."),
        "spaces.search": (P, ["search_conversations"], "By space display name or participant emails only."),
        "spaces.list": (P, ["search_conversations"], "By space display name or participant emails only; no plain listing of every space."),
        "spaces.get": (P, ["search_conversations"], "No lookup by space name id; search and filter."),
        "spaces.findDirectMessage": (C, ["search_conversations"], "Search by participant email."),
        "spaces.findGroupChats": (C, ["search_conversations"], "Search by participant emails."),
        "spaces.members.list": (C, ["list_memberships"], "Documented on the reference page; not returned by an uncredentialled tools/list call on 2026-11-26."),
        "spaces.members.get": (P, ["list_memberships"], "No single-member lookup; list and filter."),
        "users.spaces.updateSpaceReadState": (C, ["mark_as_read", "mark_as_unread"], "Documented on the reference page; not returned by an uncredentialled tools/list call on 2026-11-26."),
        "users.spaces.getSpaceReadState": (U, [], "No read-state lookup tool is listed; whether search_messages' unread filter is a full substitute is not stated."),
        "users.spaces.threads.getThreadReadState": (U, [], "mark_as_read / mark_as_unread are documented but their input schema was not readable without credentials."),
        "users.spaces.threads.updateThreadReadState": (U, [], "mark_as_read / mark_as_unread are documented but their input schema was not readable without credentials."),
        "users.spaces.spaceNotificationSetting.get": (C, ["get_notification_settings"], "Documented on the reference page; not returned by an uncredentialled tools/list call on 2026-11-26."),
        "users.spaces.spaceNotificationSetting.patch": (C, ["update_notification_settings"], "Documented on the reference page; not returned by an uncredentialled tools/list call on 2026-11-26."),
    },
    "tasks": {
        "tasks.list": (C, ["list_tasks"], ""),
        "tasks.get": (C, ["get_task"], "By task id alone."),
        "tasks.insert": (C, ["create_task"], ""),
        "tasks.update": (C, ["update_task", "complete_task", "uncomplete_task"], "Completion status changes go through complete_task / uncomplete_task."),
        "tasks.patch": (C, ["update_task", "complete_task", "uncomplete_task"], "Completion status changes go through complete_task / uncomplete_task."),
        "tasks.delete": (C, ["delete_task"], "Not on /mcp/v1self."),
        "tasks.move": (NA, [], "No move or reorder tool; update_task's input schema has no parent, position or list field."),
        "tasks.clear": (NA, [], NO_TOOL),
        "tasklists.list": (C, ["list_task_lists"], ""),
        "tasklists.get": (P, ["list_task_lists"], "No single-list lookup; list and filter."),
    },
    "people": {
        "people.searchContacts": (P, ["search_contacts"], PREVIEW),
        "people.searchDirectoryPeople": (P, ["search_directory_people"], PREVIEW + " Workspace accounts only."),
        "people.listDirectoryPeople": (P, ["search_directory_people"], PREVIEW + " Search only, no full listing."),
        "people.get": (P, ["get_user_profile"], PREVIEW + " Only the signed-in user's own name and email."),
        "otherContacts.search": (U, [], "Not stated whether search_contacts (Developer Preview) includes \"other contacts\"."),
    },
    "keep": {},
}
DEFAULT_NOTE = {
    "keep": KEEP,
    "people": "No tool. The People MCP server (Developer Preview) has three read-only tools: search_contacts, search_directory_people, get_user_profile.",
}


def _default(api, mid):
    if api == "gmail" and mid.startswith("users.settings."):
        return (NA, [], GMAIL_SETTINGS)
    return (NA, [], DEFAULT_NOTE.get(api, NO_TOOL))


def build(src):
    rows = []
    problems = []
    partner = {n for n, v in src["quota"]["gmail"]["tools"].items() if v.get("partner")}
    if set(src["quota"]["gmail"].get("partner_tools_in_note") or partner) != partner:
        problems.append("Gmail quota page: asterisked tools and the note listing partner tools disagree")
    known = {}
    for api, e in src["mcp"].items():
        known[api] = {t["name"] for t in e["tools"]} | set(e["documented"])
    known["gmail"] |= partner
    live = {api: {t["name"]: t for t in e["tools"]} for api, e in src["mcp"].items()}

    for api in APIS:
        rest = src["rest"][api]
        rules = dict(RULES.get(api, {}))
        # batchUpdate coverage is computed: REST Request union vs the request types the tool lists
        for a, mid, tool in (("docs", "documents.batchUpdate", "update_doc"), ("sheets", "spreadsheets.batchUpdate", "update_spreadsheet"),
                             ("slides", "presentations.batchUpdate", "update_presentation")):
            if api != a:
                continue
            listed = set(live[a][tool]["listed_requests"])
            restreq = set(src["batch_requests"][a])
            missing = sorted(restreq - listed)
            if not listed:
                rules[mid] = (U, [tool], "The tool's description no longer lists its supported request types.")
            elif missing:
                rules[mid] = (P, [tool], "Accepts %d of the %d REST request types. Not listed: %s." % (len(restreq & listed), len(restreq), ", ".join(missing)))
            else:
                extra = ["insert_dimension"] if a == "sheets" else []
                rules[mid] = (C, [tool] + extra, "The tool lists all %d REST request types." % len(restreq))
        seen = set()
        for m in rest["methods"]:
            mid = m["id"].split(".", 1)[1]
            seen.add(mid)
            status, tools, note = rules.get(mid) or _default(api, mid)
            tool_objs = []
            for t in tools:
                server, name = t.split(":") if ":" in t else (api, t)
                if name not in known.get(server, set()):
                    problems.append("%s.%s maps to %s:%s which Google no longer lists" % (api, mid, server, name))
                q = src["quota"].get(server, {}).get("tools", {}).get(name, {})
                self_list = src["mcp"].get(server, {}).get("v1self")
                tool_objs.append({
                    "server": server, "tool": name,
                    "daily_requests_per_call": q.get("daily_requests", 1),
                    "billing_bucket": q.get("bucket"),
                    "on_v1self": (name in self_list) if self_list is not None else None,
                    "partner_access": server == "gmail" and name in partner,
                })
            if status == PA and not all(t["partner_access"] for t in tool_objs):
                problems.append("%s.%s is marked partner access but Google no longer stars its tools" % (api, mid))
            if status in (C, P) and any(t["partner_access"] for t in tool_objs):
                problems.append("%s.%s uses a partner-access tool but is marked %s" % (api, mid, status))
            sources = []
            if api in MCP:
                sources.append(MCP[api][1])
            for t in tool_objs:
                u = MCP[t["server"]][1]
                if u not in sources:
                    sources.append(u)
            if status == PA or (api == "gmail" and tool_objs):
                sources.append(QUOTA_PAGES["gmail"])
            if "permanent deletion is blocked" in note or "approval guide" in note:
                sources.append(URL_ELICIT)
            if api == "keep":
                sources = [URL_BLOG, URL_CONFIG + "#support-products"]
            if api == "people":
                sources.append(URL_CONFIG + "#support-products")
            rows.append({
                "api": api, "product": rest["label"], "method": m["id"], "http": m["http"],
                "path": rest["base_url"] + m["path"], "status": status, "tools": tool_objs, "note": note,
                "sources": sources,
            })
        for mid in rules:
            if mid not in seen:
                problems.append("rule for %s.%s matches no method in the discovery document" % (api, mid))
    counts = {s: 0 for s in STATUSES}
    for r in rows:
        counts[r["status"]] += 1
    by_product = {}
    for r in rows:
        by_product.setdefault(r["product"], {s: 0 for s in STATUSES})[r["status"]] += 1
    servers = {}
    for api, e in src["mcp"].items():
        liveset = {t["name"] for t in e["tools"]}
        servers[api] = {
            "endpoint": e["endpoint"], "reference_url": e["reference_url"],
            "stage": "Developer Preview" if api == "people" else "GA",
            "tools_live": sorted(liveset), "tools_documented": e["documented"],
            "documented_not_live": sorted(set(e["documented"]) - liveset),
            "live_not_documented": sorted(liveset - set(e["documented"])),
            "v1self": e["v1self"],
            "partner_access_tools": sorted(partner) if api == "gmail" else [],
        }
    return {
        "generated": datetime.date.today().isoformat(),
        "sources_fetched": src["fetched"],
        "about": "Google Workspace REST methods mapped to Google's Workspace MCP tools. Built by an AI agent (Ford) from Google's public documentation; not affiliated with Google.",
        "statuses": STATUSES,
        "counts": counts, "counts_by_product": by_product, "total": len(rows),
        "servers": servers,
        "quota": {
            "mcp_standard_daily_requests_per_project_per_product": 5000,
            "gmail_multi_request_tools": {n: v["daily_requests"] for n, v in src["quota"]["gmail"]["tools"].items() if v["daily_requests"] != 1},
            "unit_price_per_1000": {"Light": 0.10, "Medium": 0.20, "Plus": 0.25, "Premium": 0.50},
            "scaled_mcp_billed_servers": ["gmail", "calendar", "drive"],
            "rest_daily_no_charge": {"gmail": {"Light": 70000000, "Medium": 10000000, "Premium": 1000000},
                                     "calendar": {"Plus": 1000000},
                                     "drive": {"Light": 20000000, "Medium": 7000000, "Plus": 4000000, "Premium": 1000000}},
            "tool_buckets": {api: {n: v.get("bucket") for n, v in q["tools"].items()} for api, q in src["quota"].items()},
            "sources": [URL_TIERS, URL_PRICING] + list(QUOTA_PAGES.values()),
        },
        "rest_revisions": {api: r["revision"] for api, r in src["rest"].items()},
        "problems": problems,
        "methods": rows,
    }


def main(argv):
    if "--fetch" in argv:
        src = fetch()
    else:
        with open(SOURCES) as f:
            src = json.load(f)
    cov = build(src)
    if "--check" in argv:
        with open(OUT) as f:
            old = json.load(f)
        old.pop("generated", None)
        new = dict(cov)
        new.pop("generated", None)
        same = json.dumps(old, sort_keys=True) == json.dumps(new, sort_keys=True)
        print("coverage.json is %s" % ("up to date" if same else "STALE"))
        return 0 if same and not cov["problems"] else 1
    with open(OUT, "w") as f:
        json.dump(cov, f, indent=1)
        f.write("\n")
    print("methods: %d" % cov["total"])
    for s in STATUSES:
        print("  %-20s %d" % (s, cov["counts"][s]))
    for p in cov["problems"]:
        print("PROBLEM: " + p)
    return 1 if cov["problems"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

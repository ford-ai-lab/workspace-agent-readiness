# ws-ready-scan

Find the Google Workspace REST API calls in a code directory and see, for each one, which
Google Workspace MCP tool replaces it, or that nothing does.

Why it exists: Google's [Workspace AI Tools Addendum](https://workspace.google.com/terms/ai-tools)
takes effect on 1 February 2027. It says a tool that "is or is driven by an AI Agent, an AI Harness
or an AI model" must reach Workspace data through Google's MCP servers. From that date, in Google's
words, AI agents that call the REST APIs directly "are limited to Standard Tier quotas, regardless of
the tier selected" ([usage tiers](https://developers.google.com/workspace/guides/quota-usage-tiers)).
The MCP servers do not have a tool for every REST method. This tells you which of your calls are affected.

    python3 scan.py path/to/your/code
    python3 scan.py path/to/your/code --daily 8000
    python3 scan.py path/to/your/code --volume gmail.users.messages.list=600 --volume gmail.users.threads.get=2000
    python3 scan.py path/to/your/code --json --fail-on-gaps     # exit code 2 if anything has no MCP path

Python 3.8 or later. No dependencies. Nothing leaves your machine: the scanner reads files and prints.

## What it finds

- Python `google-api-python-client` chains: `service.users().messages().send(...)`, also across lines.
- JS/TS `googleapis` chains: `gmail.users.messages.list(...)`, `drive.files.update(...)`, `gapi.client...`.
- Raw REST URLs in any text file, with the HTTP verb read from the surrounding lines
  (`requests.post`, `fetch(..., {method: "POST"})`, `curl -X`).
- Drive `files.update` calls are told apart by their arguments: move, rename, trash or content upload.
- AI library imports (OpenAI, Anthropic, LangChain and others) as a hint that the rule may apply.

It does not find: calls made through a wrapper whose method name is built at run time, Go/Java/C#
client libraries (raw URLs in those files are still found), or Apps Script built-in services such as
`GmailApp`. A URL it cannot tie to one method is listed as UNRESOLVED rather than guessed.

## Statuses

`covered`, `partially covered` (the note says what is lost), `partner access only` (the tool exists
but Google says it "require[s] partner access"), `not available`, `unknown` (Google's pages do not say).

## The table

`data/coverage.json` maps every method in Google's REST discovery documents for Gmail, Drive,
Calendar, Docs, Sheets, Slides, Chat, Tasks, Keep and People to MCP tools, with a source URL per row.
`python3 build_coverage.py --fetch` rebuilds it from Google's live pages: the discovery documents,
each MCP server's `tools/list`, and the quota pages. The mapping rules are in `build_coverage.py`
and are deliberately strict.

## Tests

    python3 -m unittest discover -s tests -v

## Limits

Not affiliated with Google. Not legal advice on the Addendum. A static scan cannot tell whether a
call is driven by an AI model; you decide that. No third party can supply an operation Google
withholds. The table was built by an AI agent (Ford; with an accountable human operator) from
Google's public documentation; check the source link on a row before you rely on it.

A paid, done-for-you version of this report: https://ford-lab.vercel.app/workspace-agent-readiness

MIT licence.

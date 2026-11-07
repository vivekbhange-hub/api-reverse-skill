---
name: apirev-capture
description: >-
  Helps the user record real traffic from an API they own or are allowed to use (browser
  DevTools "Save all as HAR", mitmproxy for their own apps, or server access logs), then writes
  a scrubbed copy with credentials, cookies, tokens and optionally emails and phone numbers
  redacted, static assets and third-party hosts dropped. The first step of the api-reverse pack.
  Use when the user says "capture the API traffic", "how do I get a HAR file", "record what the
  app calls", "scrub this HAR", "remove secrets from this capture", "I have a HAR from DevTools",
  or "reverse engineer our internal API".
argument-hint: "[path to .har]"
---

# apirev-capture: record traffic, then scrub it

Every later step (spec, SDK, mock, contract checks) is only as good as the capture, and a raw
capture is full of live credentials. This skill gets the user a capture that covers the flows
they care about, then writes `api/capture.scrubbed.har` so nothing secret reaches the spec, the
examples or the repo.

## Inputs

- A HAR file the user already has, or a description of the app so you can guide the capture.
- The API host(s) to keep, e.g. `api.example.com`. Ask if it is not obvious from the HAR.
- Commands below say `python3`; on Windows use `python` (or `py -3`).

## Outputs

- `api/capture.scrubbed.har`: scrubbed copy. The original file is never modified.
- A redaction summary printed to the terminal; repeat its key lines to the user.

## Steps

1. **Confirm authorization.** Ask once, plainly: is this an API the user owns, works on, or is
   permitted to use? If the answer is no or unclear, stop and say why (see What NOT to do).

2. **Pick a capture method** if there is no HAR yet. Give only the one that fits:
   - Web app: DevTools > Network, tick "Preserve log", clear, click through the flows, then
     right-click > "Save all as HAR with content" (Chrome) or the gear > "Save All As HAR"
     (Firefox).
   - Mobile or desktop app the user builds: run `mitmproxy` / `mitmweb` on their own device or
     emulator, then export flows as HAR (`mitmdump -r flows -s savehar.py` or the web UI
     export). Only for apps they own; never to bypass certificate pinning on someone else's app.
   - Backend they run: server or gateway access logs with bodies; convert to HAR entries with a
     short script, or ask for a HAR from the frontend team instead.

3. **Plan coverage before recording.** Good inference needs repetition. Ask the user to:
   - open each list view at least twice with different filters and pages;
   - open 3-5 different records of each kind (ids vary, so templates are found);
   - do one create, update and delete per resource, plus one deliberate validation error;
   - trigger one "not found" (an id that does not exist).

4. **Inventory the raw file** without printing secrets: count entries and hosts.
   ```
   python3 "${CLAUDE_SKILL_DIR}/har_scrub.py" capture.har --dry-run
   ```
   The summary lists hosts. Choose the API host(s); everything else (analytics, CDNs, fonts) goes.

5. **Scrub.**
   ```
   python3 "${CLAUDE_SKILL_DIR}/har_scrub.py" capture.har -o api/capture.scrubbed.har \
     --include-host api.example.com --pii
   ```
   - `--include-host` is repeatable and accepts wildcards (`*.example.com`).
   - `--pii` replaces emails with `redacted@example.invalid` (format inference still works) and
     phone numbers with `REDACTED-phone`. Default to using it; drop it only if the user needs
     real-looking contact fields and the capture will never leave their machine.
   - `--keep-static` keeps images, CSS, JS and HTML; almost never wanted.

6. **Read the summary back to the user**: entries kept vs dropped, which headers, query keys and
   JSON keys were redacted, how many pattern hits (JWT, AWS key, long hex or base64 secrets).
   If the summary says "PII found and NOT replaced", rerun with `--pii` unless the user objects.

7. **Spot-check** the output for anything the rules missed, without echoing values:
   ```
   python3 - <<'EOF'
   import re; t = open("api/capture.scrubbed.har", encoding="utf-8").read()
   for label, rx in [("jwt", r"eyJ[\w-]{10,}\."), ("bearer", r"Bearer (?!REDACTED)\S{8,}"),
                     ("password", r'"passw[^"]*":\s*"(?!REDACTED)')]:
       print(label, len(re.findall(rx, t)))
   EOF
   ```
   Any non-zero count: tell the user which key or header holds it and ask before continuing. If
   a custom header carries a secret (for example `X-Tenant-Signature`), it is already covered by
   name rules containing token, secret, key, auth, session or signature; anything else needs a
   manual edit of the scrubbed copy.

8. **Hand off.** Tell the user the next step is `apirev-infer`, which turns
   `api/capture.scrubbed.har` into `api/openapi.json` and an inference report. Suggest adding
   `api/*.har` to `.gitignore` unless they have reviewed the scrubbed file.

## Decision rules

- Several API hosts (e.g. `api.` and `auth.`): keep both with two `--include-host` flags; the
  spec will list both servers.
- Requests through a first-party proxy path (`/api/...` on the web host): include the web host
  and let the static filter drop pages and assets.
- GraphQL or gRPC-web traffic: scrub it, but warn that the infer step models it as a single POST
  endpoint; a schema from introspection (if the user owns the server) is a better source.
- Huge captures (thousands of entries): fine for the scripts. Do not read the HAR into context;
  rely on the summaries.

## What NOT to do

- Do not help capture or reverse-engineer an API the user is not authorized to use, and do not
  help bypass authentication, paywalls, rate limits, bot protection, certificate pinning or
  request signing. Respect terms of service.
- Do not print, quote or paste credential values from a raw HAR into the conversation, even to
  "show what will be redacted". Refer to them by header or key name.
- Do not overwrite or edit the user's original HAR. The script refuses `-o` equal to the input.
- Do not commit a raw HAR. Do not upload any capture anywhere.
- Do not skip scrubbing because "it is only a dev environment": dev tokens leak too.

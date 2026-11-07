---
name: apirev-mock
description: >-
  Runs a local mock server that replays recorded responses from a scrubbed HAR, or example
  responses from the spec, matched by path template, with optional latency and seeded random
  errors. Unknown routes return 404 JSON listing every known route. Use when the user says
  "mock this API", "I need a fake backend", "run a local mock server", "let the frontend work
  without the backend", "simulate a slow or flaky API", "replay the captured responses", or
  "test my client offline".
argument-hint: "[--har file] [--spec file]"
---

# apirev-mock: the backend, recorded

Frontend and QA work stalls when the backend is down, slow to set up, or shared. This skill
serves what the real API actually returned, from the scrubbed capture, on localhost. It is a
replay, not a simulation: no state, no invented data.

## Inputs

- `api/capture.scrubbed.har` (recorded responses) and/or `api/openapi.json` (route templates
  and fallback examples). Using both gives the best matching.
- Optional: port, latency, error rate, seed.
- Commands below say `python3`; on Windows use `python` (or `py -3`).

## Outputs

- A running server on `http://127.0.0.1:<port>`.
- Optionally `api/mock/` with copies of the HAR/spec used, when the user wants the mock setup
  committed for teammates (copy the scrubbed HAR there; never a raw one).

## Steps

1. **Check the route table first.**
   ```
   python3 "${CLAUDE_SKILL_DIR}/mock_server.py" --har api/capture.scrubbed.har \
     --spec api/openapi.json --list
   ```
   Each line shows a route, how many recordings back it, and whether answers come from the HAR
   or spec examples. Routes with source `none` will answer 501; tell the user which ones.

2. **Start it in the background** so the conversation can continue:
   ```
   python3 "${CLAUDE_SKILL_DIR}/mock_server.py" --har api/capture.scrubbed.har \
     --spec api/openapi.json --port 8080
   ```
   Use the Bash tool's background mode. Confirm it answers:
   `curl -s http://127.0.0.1:8080/v1/pets?page=1&limit=3 | head -c 300`.

3. **Point the app at it.** Usually one environment variable (`API_BASE_URL`,
   `VITE_API_URL`, `NEXT_PUBLIC_API_URL`) or the generated SDK's `baseUrl`. CORS is open (`*`)
   and `OPTIONS` preflights get 204, so browser apps work.

4. **Explain how answers are chosen** so nobody is surprised:
   - Same path and same query (any order) as a recording: that recording.
   - Same path: the first recording for it (so `/pets/999` replays the captured 404).
   - Same route template: the first 2xx recording (so `/pets/123` returns pet 3's body).
   - No recording: the spec's example for the lowest 2xx status.
   - No route: 404 with `known_routes`; known path, wrong method: 405 with `Allow`.
   - Every response carries `X-Mock-Source` (har, spec, mock) and `X-Mock-Route`.

5. **Chaos on request.**
   - `--latency-ms 800` delays every response.
   - `--error-rate 0.2 --seed 7` answers about 20% of matched requests with 500. The same seed
     and request order give the same failures, so a flaky-path bug can be reproduced.

6. **Need a response that was not captured?** Capture it (`apirev-capture`) or add an example
   to that operation in the spec. Do not hand-write fake data into the HAR.

7. **Hand off.** Run the generated client against the mock (`apirev-sdk`), or record new
   traffic later and check it with `apirev-contract validate`.

## What NOT to do

- Do not serve a raw, unscrubbed HAR; recorded bodies would hand tokens to anyone who can reach
  the port. Bind to `127.0.0.1` (the default); use `--host 0.0.0.0` only if the user asks and
  understands the network exposure.
- Do not present the mock as the real API's behavior beyond what was recorded: POST does not
  create anything, DELETE deletes nothing, and filters are not applied.
- Do not use the mock to impersonate a third party's service to its users or clients, and do
  not mock a third party's private API you are not authorized to use. A mock is never a way
  around the real API's authentication, rate limits or terms of service.
- Do not leave the background server running at the end of the session without telling the
  user how to stop it.

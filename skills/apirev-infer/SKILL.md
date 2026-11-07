---
name: apirev-infer
description: >-
  Infers an OpenAPI 3.0.3 spec (JSON and YAML) from a scrubbed HAR file: path templates with
  typed parameters (ids, UUIDs, dates, hashes, slugs), query parameters, request and response
  schemas merged across every sample, auth schemes and pagination, plus an inference report
  with sample counts and low-confidence flags. Use when the user says "generate an OpenAPI spec
  from this HAR", "infer the API schema", "turn this capture into a spec", "write a swagger file
  for this API", "what endpoints does this app call", "document the API from traffic", or after
  apirev-capture finishes.
argument-hint: "[path to scrubbed .har]"
---

# apirev-infer: HAR to OpenAPI, with receipts

The script does the counting; you do the judgment. `har2openapi.py` turns every recorded
request into a deterministic spec and writes a report saying how many samples back each
endpoint and which inferences are shaky. Your job is to run it, read the report, fix template
mistakes with overrides, and tell the user honestly what the spec can and cannot be trusted for.

## Inputs

- `api/capture.scrubbed.har` from `apirev-capture` (or another scrubbed HAR path).
- Optional: an API title and version, hosts to restrict to, known path templates.
- Commands below say `python3`; on Windows use `python` (or `py -3`).

## Outputs

- `api/openapi.json`: OpenAPI 3.0.3, keys and paths sorted so diffs stay clean.
- `api/openapi.yaml`: same spec as YAML.
- `api/inference-report.md`: endpoints with sample counts and confidence, how each path
  parameter was detected, security schemes, pagination, low-confidence findings, rules used.

## Steps

1. **Run it.**
   ```
   python3 "${CLAUDE_SKILL_DIR}/har2openapi.py" api/capture.scrubbed.har --out api \
     --title "Acme Orders API" --yaml
   ```
   Exit code 2 with "looks unscrubbed" means live credentials are still in the file: go back to
   `apirev-capture`. Do not reach for `--allow-unscrubbed` unless the user explicitly insists and
   understands examples may then contain secrets.

2. **Read `api/inference-report.md`, not the whole spec.** It is short by design. Check, in order:
   - **Endpoints table.** Does every row look like a real operation? Rows with confidence `low`
     (one sample) are guesses about required fields and types.
   - **Path parameters.** A literal that should be a parameter (two rows like
     `/users/alice/orders` and `/users/bob/orders`) or a parameter that should be a literal
     (`/v1/{v1Id}` would be wrong). Fix with overrides (step 3).
   - **Security / Pagination.** Confirm they match what the user knows about the API.
   - **Low-confidence findings.** Each line names the operation, the JSON pointer and why.

3. **Fix templates with overrides** when step 2 found mistakes, then rerun:
   ```
   python3 "${CLAUDE_SKILL_DIR}/har2openapi.py" api/capture.scrubbed.har --out api --yaml \
     --title "Acme Orders API" --template "/users/{username}/orders" --template "/files/{path}"
   ```
   Other knobs: `--host api.example.com` (repeatable, wildcards ok) to drop other hosts;
   `--min-distinct 2` to accept slugs from two samples, `0` to turn slug detection off;
   `--api-version 1.0.0` for `info.version`.

4. **Summarize for the user** in five lines or fewer: operations and paths found, auth scheme,
   pagination style, the 3 most important low-confidence findings, and what to capture next to
   firm them up (for example "open two more adoption records so `status` can become an enum").

5. **Offer a second opinion** for specs with more than ~10 operations or many flags: delegate to
   the `schema-skeptic` agent with the paths of the spec and report. For a domain overview
   (resources, relationships, flows, gaps), use the `api-cartographer` agent.

6. **Hand off.** Next steps, in the usual order: `apirev-document` (names, summaries, README),
   then `apirev-sdk`, `apirev-mock`, `apirev-contract` as needed.

## How the script decides (so you can explain it)

- **Templates:** all-digit segments, UUIDs, ISO dates and hex hashes are parameters, named from
  the previous segment (`/pets/{petId}`, `/reports/{reportDate}`), with numbering on clashes.
  Slugs are detected by cardinality: 3+ distinct hyphenated or alphanumeric values under a
  constant parent. Top-level segments (`/v1/{x}`) and collections followed by ids are never
  collapsed. Literal siblings (`/pets/search`) stay separate routes.
- **Required** means "present in every sample". With one sample, everything is required; the
  report flags it.
- **Types:** integer + decimal merge to `number`; `null` sets `nullable`; conflicting types
  become `oneOf`. Formats (date-time, date, email, uuid, uri, ipv4) only when every value fits.
- **Enums:** only with 5+ samples, at most 5 distinct values, and values repeating on average.
  Below that, the report lists the field as a possible enum instead.
- **Security:** bearer (JWT when the scrubber tagged it), basic, API keys in header, query or
  cookie. Only scheme names and locations are written, never values.
- **Pagination:** page/per_page, limit/offset, cursor-style params, next fields in the body, or
  a `Link: rel="next"` header. Written to the description and `x-apirev-pagination`.

## What NOT to do

- Do not hand-edit `api/openapi.json` to add endpoints, fields or enum values that are not in
  the capture. Unseen behavior belongs in a note, or in a new capture.
- Do not drop the report's warnings when summarizing. A spec built from 1 sample per endpoint
  must be described as a draft.
- Do not infer specs from captures of third-party private APIs the user is not authorized to
  use, and do not publish such a spec. Do not help bypass auth, rate limits or signing.
- Do not paste large parts of the spec into the conversation; point at paths and line numbers.
- Do not run the script on the raw, unscrubbed HAR.

---
name: apirev-explore
description: >-
  Answers "how do I do X with this API" from the inferred spec: finds the operation or sequence
  of operations, shows a ready-to-run curl command and a call with the generated SDK, and says
  which fields and endpoints are low-confidence because they were seen in few samples. Use when
  the user says "how do I create an order with this API", "which endpoint lists users", "what
  does this endpoint return", "show me a curl for", "what parameters does X take", "how do I
  paginate this", or "can this API do Y".
argument-hint: "[question]"
---

# apirev-explore: ask the spec, get a working call

Once a spec exists, most questions are lookups: which endpoint, which fields, what comes back.
This skill answers them from `api/openapi.json` and the inference report, with a command the
user can paste, and with the honesty the report demands about what was actually observed.

## Inputs

- A question in plain words.
- `api/openapi.json`, `api/inference-report.md`, and if present `api/README.md` and
  `api/sdk/`.
- Commands below say `python3`; on Windows use `python` (or `py -3`).

## Outputs

- An answer in the conversation. Nothing is written unless the user asks to save it (then
  append it to `api/README.md` under "Recipes").

## Steps

1. **List the operations compactly** instead of reading the whole spec:
   ```
   python3 - <<'EOF'
   import json; s = json.load(open("api/openapi.json", encoding="utf-8"))
   for path, item in sorted(s["paths"].items()):
       for m, op in sorted(item.items()):
           print(m.upper(), path, op.get("operationId"), op.get("x-apirev-samples"), op.get("summary", ""))
   EOF
   ```

2. **Match the question to operations.** Look at the path words, operationIds, tags,
   summaries, and the property names of request and response schemas (for "where is the
   adoption status?" search the schemas for `status`). Multi-step tasks become a sequence:
   log in -> list -> pick an id -> update.

3. **Read only the matching operations** from the spec (`s["paths"][path][method]`) and the
   matching rows of the inference report.

4. **Answer in this shape:**
   - **Operation(s):** `METHOD /path` (`operationId`), one line on what it does.
   - **Request:** path and query parameters (required ones first), body fields with types.
   - **curl:** built from the first `servers` URL, the security scheme, and example values from
     the spec. Credentials are placeholders (`$TOKEN`, `$API_KEY`):
     ```
     curl -s -X PATCH "https://api.petshelter.example/v1/pets/42" \
       -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
       -d '{"vaccinated": true}'
     ```
   - **SDK:** the same call through the generated client if `api/sdk/` exists
     (`await api.updatePet(42, { vaccinated: true })` / `api.update_pet(42, {"vaccinated": True})`).
   - **Response:** the fields that matter for the question, from the schema or trimmed example.
   - **Confidence:** samples behind the operation (`x-apirev-samples`), and any report flags on
     the fields used ("`status` was seen with 2 values in 3 samples; other values may exist").
   - **Pagination**, if the operation has `x-apirev-pagination`: how to get the next page.

5. **When the spec has no answer,** say so directly: "No captured endpoint does X." Then say
   what to capture to find out (which screen to click through in the app) and hand off to
   `apirev-capture`. If something similar exists, name it and say how it differs.

6. **Optional live check.** If the user wants to try the call and a mock is running
   (`apirev-mock`), run the curl against `http://127.0.0.1:<port>` instead of the real API.
   Run against the real API only if the user asks, with their own credentials from an
   environment variable, and only for safe reads unless they explicitly confirm a write.

## What NOT to do

- Do not invent endpoints, parameters, fields or enum values to answer a question. "Not in the
  capture" is a complete answer.
- Do not put real tokens, emails or other personal data into examples.
- Do not send write requests (POST, PUT, PATCH, DELETE) to a real API without the user's
  explicit confirmation of that exact request.
- Do not help use an API in ways its owner has not authorized, including bypassing auth, rate
  limits, paywalls or bot protection.
- Do not paste the full spec or full report into the answer.

---
name: apirev-document
description: >-
  Turns an inferred OpenAPI spec into documentation people can use: better operationIds,
  summaries, descriptions, tags and parameter names in api/openapi.json, then a human README at
  api/README.md with auth, pagination, errors and a curl example per endpoint. Never invents
  endpoints or fields that are not in the capture and labels every guess. Use when the user says
  "document this API", "write docs for the spec", "add summaries to the OpenAPI file", "make the
  spec readable", "write an API README", "add curl examples", or after apirev-infer finishes.
argument-hint: "[path to openapi.json]"
---

# apirev-document: from inferred spec to docs a teammate can use

An inferred spec is accurate about shape and silent about meaning. This skill adds the meaning,
carefully: names and descriptions come from the field names, example values and the user's
knowledge, and anything that is a guess says so. The spec stays honest; the README becomes the
page a new teammate reads first.

## Inputs

- `api/openapi.json` and `api/inference-report.md` from `apirev-infer`.
- Optional: anything the user knows about the domain (glossary, a Slack thread, old docs).
- Commands below say `python3`; on Windows use `python` (or `py -3`).

## Outputs

- `api/openapi.json` (and `api/openapi.yaml`) updated in place, after the user approves the
  changes.
- `api/README.md`: human documentation.

## Steps

1. **Read the report first, then the spec by path.** Note every low-confidence finding; they
   decide where you must write "inferred" or "unverified".

2. **Draft spec edits as a list before writing anything.** For each operation, propose:
   - `operationId`: keep the generated one unless it is unclear (`postAuthLogin` -> `login`).
     Keep them unique and camelCase; SDK method names come from them.
   - `summary`: one line, imperative, under 60 characters ("List pets, newest first" only if the
     samples show the order; otherwise "List pets").
   - `description`: what it does, then known caveats. Keep the generated sentence "Inferred from
     N recorded requests." so readers see the evidence.
   - `tags`: group by resource; add a one-line description per tag at the top level.
   - Parameter descriptions: what the parameter means, observed values, defaults only if seen.
   - Path parameter names: rename only to clearer names (`{shelterSlug}` -> `{shelter}` is fine);
     rename it in the path and in `parameters` together.
   Show the list to the user, grouped by path, and apply after they agree.

3. **Apply edits with a small script**, not by retyping the file, so ordering stays stable:
   ```
   python3 - <<'EOF'
   import json, pathlib
   p = pathlib.Path("api/openapi.json"); spec = json.loads(p.read_text(encoding="utf-8"))
   op = spec["paths"]["/v1/pets"]["get"]
   op["summary"] = "List pets"
   op["operationId"] = "listPets"
   p.write_bytes((json.dumps(spec, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode())
   EOF
   ```
   If the user keeps `openapi.yaml`, regenerate it from the JSON with the infer script's emitter:
   `python3 -c "import importlib.util,json,sys; s=importlib.util.spec_from_file_location('h','${CLAUDE_SKILL_DIR}/../apirev-infer/har2openapi.py'); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); open('api/openapi.yaml','wb').write(m.to_yaml(json.load(open('api/openapi.json',encoding='utf-8'))).encode())"`

4. **Write `api/README.md`** with these sections, in this order:
   - Title and one paragraph: what the API is for, base URL(s) from `servers`, and a line saying
     the spec was inferred from recorded traffic on a stated date with N requests.
   - **Authentication:** scheme(s) from `components.securitySchemes`, where the credential goes,
     how to obtain one if the user told you. Never include a real token; use `$TOKEN`.
   - **Pagination** and **Errors:** from the report and the 4xx response examples.
   - **Endpoints:** one subsection per tag. Per operation: method and path, summary, parameters
     table, a curl example, and a trimmed response example. Example:
     ```
     curl -s "https://api.example.com/v1/pets?page=1&limit=3" \
       -H "Authorization: Bearer $TOKEN"
     ```
   - **Confidence:** a short table of the operations with `low` confidence and what to capture
     to confirm them.
   - **Regenerating:** the commands from `apirev-capture` and `apirev-infer`.

5. **Mark guesses.** Any sentence not backed by a sample ("probably returns 409 on duplicates")
   gets *(unverified)* after it, or goes in a "Questions for the API owner" list at the end.

6. **Hand off.** Suggest `apirev-sdk` for typed clients (operationIds become method names, so
   documenting first gives better SDKs), `apirev-mock` for a local server, and `apirev-explore`
   for "how do I do X" questions.

## What NOT to do

- Do not add endpoints, fields, status codes, enum values or parameters that are not in the
  capture. If the user tells you one exists, add it to the README under "Known but not captured"
  instead of the spec, unless they explicitly ask for the spec edit.
- Do not change schemas (types, required, nullable) while documenting; that is
  `schema-skeptic`'s review and the user's decision.
- Do not put real tokens, emails or customer data in curl examples or README text.
- Do not write docs for, or publish a spec of, a third party's private API without permission.
- Do not rewrite the whole JSON by hand; key order and formatting must stay deterministic.

---
name: apirev-contract
description: >-
  Keeps an API and its spec honest. `validate` checks a new HAR's responses against the spec
  (missing required fields, type changes, undocumented fields, unknown endpoints, status codes
  not in the spec); `diff` compares two specs and sorts changes into breaking, warning and info,
  with a non-zero exit for CI. Use when the user says "did the API change", "check this capture
  against the spec", "is this a breaking change", "diff these two OpenAPI files", "add API
  contract tests to CI", "validate responses against the schema", or "compare old and new spec".
argument-hint: "validate|diff [files]"
---

# apirev-contract: catch API drift before users do

An inferred spec goes stale the day the backend ships. This skill answers two questions with
evidence: does today's traffic still match the spec (`validate`), and would this spec change
break existing clients (`diff`). Both produce a Markdown or JSON report and can fail a CI job.

## Inputs

- `validate`: `api/openapi.json` plus a newly captured HAR, scrubbed with `apirev-capture`.
- `diff`: two spec files, typically the committed one and a newly inferred or edited one.
- Commands below say `python3`; on Windows use `python` (or `py -3`).

## Outputs

- `api/contract/validate-<date>.md` or `api/contract/diff-<date>.md` (use today's date).
- Exit code 1 when `--fail-on` is hit, for CI.

## Steps

1. **Validate fresh traffic against the spec.**
   ```
   python3 "${CLAUDE_SKILL_DIR}/contract.py" validate --spec api/openapi.json \
     --har api/capture-new.scrubbed.har -o api/contract/validate-2026-11-07.md
   ```
   Levels:
   - **breaking**: required field missing, type changed, null where not nullable, a field the
     spec forbids.
   - **warning**: endpoint not in the spec, status code not in the spec, value outside an enum,
     content type not in the spec.
   - **info**: field present in traffic but not in the spec.
   Identical findings are merged with a count in the "Seen" column.

2. **Diff two specs.** To see what changed since the committed spec, infer a new one into a
   scratch folder first, then compare:
   ```
   python3 "${CLAUDE_SKILL_DIR}/../apirev-infer/har2openapi.py" api/capture-new.scrubbed.har --out /tmp/apirev-new
   python3 "${CLAUDE_SKILL_DIR}/contract.py" diff api/openapi.json /tmp/apirev-new/openapi.json \
     -o api/contract/diff-2026-11-07.md
   ```
   The rules follow who breaks: for **responses**, removing a field, making it optional or
   nullable, or changing its type breaks clients; for **requests**, a new required field or
   parameter, a removed enum value or a narrower type breaks clients. New optional fields and
   new endpoints are info. Response enum growth is a warning (it breaks exhaustive switches).
   Path parameter renames are info.

3. **Read the report with the user.** For each breaking item, say who is affected (which SDK
   method, which screen if known) and whether it is real or an inference artifact. A field
   "removed" because the new capture never hit that code path is a capture gap, not a change.
   Low-sample endpoints in `api/inference-report.md` produce exactly these false alarms.

4. **Get a verdict for a PR.** Delegate to the `breaking-change-guard` agent with the report
   path; it returns safe / breaking / needs a version bump, with migration notes. Post nothing
   to GitHub yourself; draft the comment and show it to the user first.

5. **Add it to CI if asked.** Draft a workflow step and show it before writing:
   ```yaml
   - name: API contract
     run: python3 tools/contract.py diff api/openapi.base.json api/openapi.json --fail-on breaking
   ```
   `--fail-on breaking` fails only on breaking findings; `--fail-on warning` also on warnings;
   `--format json` gives machine-readable output.

6. **Hand off.** Accepted changes go back through `apirev-infer` (regenerate the spec),
   `apirev-document` and `apirev-sdk` so the client matches again.

## What NOT to do

- Do not "fix" a breaking finding by editing the old spec to match; the old spec is the record
  of what clients rely on.
- Do not treat a field missing from a small new capture as a confirmed removal; ask for a
  capture that exercises it.
- Do not validate raw HARs; scrub them first.
- Do not post PR comments, open issues or change CI files without showing the draft and getting
  the user's confirmation.
- Do not use contract checks to probe a third party's private API you are not authorized to use.

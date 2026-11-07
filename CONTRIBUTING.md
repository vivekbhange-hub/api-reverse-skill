# Contributing

Thanks for helping. This pack is small on purpose, so a few rules keep it that way.

## Ground rules

- Helper scripts use the Python standard library only, run on Python 3.9+, make no network
  calls, and produce the same bytes for the same input.
- Every script change comes with a test in `tests/`. Run the suite from the repo root:

  ```
  python -m unittest discover -s tests -v
  ```

- If your change alters what the demo produces, regenerate the committed outputs and include
  them in the same pull request:

  ```
  python examples/regenerate.py
  python examples/regenerate.py --check
  ```

- Write files with LF line endings (`.gitattributes` enforces this in git).
- Skills keep the frontmatter format checked by `tests/test_repo.py`: `name` equals the folder
  name, the description says what it does and lists trigger phrases, and the body has a
  "What NOT to do" section.

## Fixtures

Test data and examples must be fictional. Use reserved domains (`.example`, `.invalid`) and
fake credentials that are obviously fake. Never commit a real capture, even a scrubbed one.

## Inference rules

Changes to `har2openapi.py` heuristics (path templates, enums, required fields) need a test
showing the case that motivated them and a test showing a nearby case that must not change.
Over-fitting is the main failure mode of spec inference; err toward reporting a guess in
`inference-report.md` rather than baking it into the spec.

## Conduct

Be kind and specific. This project follows the spirit of the
[Contributor Covenant](https://www.contributor-covenant.org/version/2/1/code_of_conduct/).

## Releasing

Bump `version` in `.claude-plugin/plugin.json` and add a matching `## [x.y.z] - YYYY-MM-DD`
section to `CHANGELOG.md`; the test suite checks that they agree.

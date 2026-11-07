# Changelog

All notable changes to this project are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [1.0.0] - 2026-11-07

### Added

- Seven skills: `apirev-capture`, `apirev-infer`, `apirev-document`, `apirev-sdk`, `apirev-mock`,
  `apirev-contract` and `apirev-explore`.
- Three read-only agents: `api-cartographer`, `schema-skeptic` and `breaking-change-guard`.
- Helper scripts (standard library only): `har_scrub.py`, `har2openapi.py` (with a YAML emitter
  and an inference report), `gen_sdk.py`, `mock_server.py` and `contract.py`.
- A fictional Pet Shelter HAR with generated outputs under `examples/output/`, checked
  byte-for-byte by the test suite.
- CI on Linux, Windows and macOS with Python 3.9, 3.12 and 3.13.

#!/usr/bin/env python3
"""Regenerate (or verify) every file in examples/output/ from examples/petshelter.har.

  python examples/regenerate.py           rewrite examples/output/
  python examples/regenerate.py --check   exit 1 if the committed outputs are stale

Runs the same commands the README shows: scrub -> infer -> sdk. Standard library only.
"""
from __future__ import annotations

import argparse
import importlib.util
import io
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HAR = ROOT / "examples" / "petshelter.har"
OUTPUT = ROOT / "examples" / "output"


def _load(skill: str, script: str):
    path = ROOT / "skills" / skill / f"{script}.py"
    spec = importlib.util.spec_from_file_location(f"_regen_{script}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def generate(out: Path) -> list:
    """Write the full demo pipeline into `out`; return the relative paths written."""
    scrub = _load("apirev-capture", "har_scrub")
    infer = _load("apirev-infer", "har2openapi")
    sdk = _load("apirev-sdk", "gen_sdk")
    out.mkdir(parents=True, exist_ok=True)
    scrubbed = out / "capture.scrubbed.har"
    steps = [
        (scrub.main, [str(HAR), "-o", str(scrubbed), "--include-host", "api.petshelter.example",
                      "--pii", "--quiet"]),
        (infer.main, [str(scrubbed), "--out", str(out), "--title", "Pet Shelter API", "--yaml",
                      "--quiet"]),
        (sdk.main, [str(out / "openapi.json"), "--out", str(out / "sdk"), "--quiet"]),
    ]
    for func, argv in steps:
        with redirect_stdout(io.StringIO()):
            code = func(argv)
        if code != 0:
            raise RuntimeError(f"step failed with exit code {code}: {argv}")
    return sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())


def compare(expected_dir: Path, actual_dir: Path) -> list:
    """Return a list of differences between two output folders."""
    problems = []
    expected = {p.relative_to(expected_dir).as_posix() for p in expected_dir.rglob("*") if p.is_file()}
    actual = {p.relative_to(actual_dir).as_posix() for p in actual_dir.rglob("*") if p.is_file()}
    for name in sorted(expected ^ actual):
        problems.append(f"{name}: {'missing from regenerated output' if name in expected else 'not committed'}")
    for name in sorted(expected & actual):
        if (expected_dir / name).read_bytes() != (actual_dir / name).read_bytes():
            problems.append(f"{name}: content differs")
    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="verify instead of rewriting")
    args = parser.parse_args(argv)
    if not args.check:
        for name in generate(OUTPUT):
            print(f"wrote examples/output/{name}")
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        generate(Path(tmp))
        problems = compare(OUTPUT, Path(tmp))
    for problem in problems:
        print(problem)
    print("examples/output is up to date" if not problems else f"{len(problems)} stale file(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

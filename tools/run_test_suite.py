#!/usr/bin/env python3
"""Run one reviewed, change-scoped pytest suite from tests/suites.json."""

import argparse
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "tests/suites.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suite", nargs="?")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    suites = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if args.list:
        for name, details in suites.items():
            print(f"{name}: {details['purpose']}")
        return 0
    if args.suite not in suites:
        parser.error("choose a suite from: " + ", ".join(sorted(suites)))
    tests = suites[args.suite].get("tests")
    if (not isinstance(tests, list) or not tests or
            any(not isinstance(test, str) or not test.startswith("tests/") or ".." in test
                for test in tests)):
        parser.error("suite contains an invalid test selector")
    return subprocess.run([sys.executable, "-m", "pytest", "-q", *tests], cwd=ROOT).returncode


if __name__ == "__main__":
    raise SystemExit(main())

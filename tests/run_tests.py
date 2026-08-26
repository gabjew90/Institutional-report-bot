#!/usr/bin/env python3
"""Zero-dependency test runner.

WHY THIS EXISTS
===============
tests/ already held two test files written in pytest's plain-function
style. pytest is not installed on this machine or in the Railway
container, and nothing in the repo invoked them. So they had never run.

A test nobody can execute is not a weaker test, it is not a test. It
reads as coverage in a directory listing and asserts nothing. Rather
than add pytest as a dependency to the deployed image for the sake of
a handful of asserts, this runner does the only two things those files
need: import conftest.py first (it redirects DB_PATH at a temp dir
BEFORE any project module loads, which is load-bearing), then call
every module-level `test_*` function and report.

USAGE
=====
    py -3.12 tests/run_tests.py               # every test file
    py -3.12 tests/run_tests.py calendar_data # only matching files
    exit 0 -> all passed.  1 -> failures.
"""
from __future__ import annotations

import glob
import importlib.util
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

# conftest FIRST and unconditionally: it sets DB_PATH/PDF_DOWNLOAD_DIR to
# a temp dir, and it only works if it lands before any project import.
_spec = importlib.util.spec_from_file_location(
    "tests_conftest", os.path.join(HERE, "conftest.py"))
_conf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_conf)


def _load(path: str):
    name = "t_" + os.path.basename(path)[:-3]
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    pattern = sys.argv[1] if len(sys.argv) > 1 else ""
    files = sorted(glob.glob(os.path.join(HERE, "test_*.py")))
    if pattern:
        files = [f for f in files if pattern in os.path.basename(f)]
    if not files:
        print(f"no test files match {pattern!r}")
        return 1

    passed, failures = 0, []
    for path in files:
        rel = os.path.relpath(path, REPO)
        try:
            mod = _load(path)
        except Exception:
            failures.append((rel, "<import>", traceback.format_exc()))
            print(f"ERROR {rel} — could not import")
            continue
        names = sorted(n for n in dir(mod) if n.startswith("test_")
                       and callable(getattr(mod, n)))
        print(f"\n{rel}  ({len(names)} tests)")
        for n in names:
            try:
                getattr(mod, n)()
            except Exception as e:
                failures.append((rel, n, traceback.format_exc()))
                print(f"  FAIL {n}: {type(e).__name__}: {e}")
            else:
                passed += 1
                print(f"  ok   {n}")

    print()
    for rel, n, tb in failures:
        print(f"--- {rel}::{n} " + "-" * 40)
        print(tb.rstrip())
    if failures:
        print(f"\n{len(failures)} failed, {passed} passed")
        return 1
    print(f"{passed} passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

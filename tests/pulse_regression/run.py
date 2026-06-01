"""Run pulse_draft_validate against every pinned fixture and verify the
violations match each fixture's expected.json.

Exit code:
    0 — all fixtures pass
    1 — at least one fixture failed (missed expected OR unexpected found)
    2 — config error (missing scripts, broken fixtures)

Usage:
    python3 tests/pulse_regression/run.py
    python3 tests/pulse_regression/run.py --fixture 2026-06-01-contrarian-buried
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path


def _load_validator() -> "module":  # type: ignore
    """Import scripts/pulse_draft_validate.py without putting scripts/
    on sys.path (its module name 'pulse_draft_validate' is fine for
    direct loading)."""
    repo_root = Path(__file__).resolve().parent.parent.parent
    target = repo_root / "scripts" / "pulse_draft_validate.py"
    if not target.exists():
        print(f"validator not found at {target}", file=sys.stderr)
        sys.exit(2)
    spec = importlib.util.spec_from_file_location(
        "pulse_draft_validate", target
    )
    if spec is None or spec.loader is None:
        print("could not build validator import spec", file=sys.stderr)
        sys.exit(2)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _match_substring(needle: str, haystack: str) -> bool:
    return (needle or "").strip().lower() in (haystack or "").lower()


def _check_violation_matches(
    actual: dict, expected: dict
) -> bool:
    """Return True if `actual` violation entry matches `expected`'s
    pattern. `expected` is a dict with at minimum `kind`; optional
    `theme_substring`, `sibling_substring`."""
    if actual.get("kind") != expected.get("kind"):
        return False
    if "theme_substring" in expected:
        if not _match_substring(
            expected["theme_substring"], actual.get("theme", "")
        ):
            return False
    if "sibling_substring" in expected:
        if not _match_substring(
            expected["sibling_substring"], actual.get("sibling", "")
        ):
            return False
    return True


def run_fixture(
    fixture_dir: Path, validator_mod
) -> tuple[bool, list[str]]:
    """Run one fixture. Returns (pass: bool, messages: list[str])."""
    msgs: list[str] = []
    expected_path = fixture_dir / "expected.json"
    ctx_path = fixture_dir / "ctx.json"
    final_path = fixture_dir / "final.md"
    if not expected_path.exists():
        return False, [f"missing {expected_path.name}"]
    if not ctx_path.exists():
        return False, [f"missing {ctx_path.name}"]
    if not final_path.exists():
        return False, [f"missing {final_path.name}"]

    expected = json.loads(expected_path.read_text(encoding="utf-8"))
    try:
        ctx = json.loads(ctx_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return False, [f"ctx.json parse error: {e}"]
    md = final_path.read_text(encoding="utf-8")

    violations = validator_mod.validate(md, ctx)
    actual_hard = [v for v in violations if v.get("severity") == "hard"]
    actual_soft = [v for v in violations if v.get("severity") == "soft"]

    # Check expected hard violations: each MUST appear in actual.
    expected_hard = expected.get("expected_hard_violations", [])
    expected_soft = expected.get("expected_soft_violations", [])
    missed: list[dict] = []
    for eh in expected_hard:
        if not any(_check_violation_matches(a, eh) for a in actual_hard):
            missed.append({"severity": "hard", **eh})
    for es in expected_soft:
        if not any(_check_violation_matches(a, es) for a in actual_soft):
            missed.append({"severity": "soft", **es})

    # Check unexpected violations: any HARD actual that doesn't match
    # an expected one is a false positive.
    unexpected_hard: list[dict] = []
    for a in actual_hard:
        if not any(_check_violation_matches(a, eh) for eh in expected_hard):
            unexpected_hard.append(a)

    fixture_passes = not missed and not unexpected_hard
    if not fixture_passes:
        if missed:
            for m in missed:
                msgs.append(
                    f"  MISSED expected {m['severity']} "
                    f"[{m['kind']}]"
                    + (f" theme~{m.get('theme_substring')}"
                       if m.get('theme_substring') else "")
                )
        if unexpected_hard:
            for u in unexpected_hard:
                msgs.append(
                    f"  UNEXPECTED hard [{u['kind']}]: "
                    f"{u.get('message', '')[:80]}"
                )
    return fixture_passes, msgs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", help="run only this fixture id (subdir name)")
    args = parser.parse_args()

    fixtures_dir = Path(__file__).resolve().parent / "fixtures"
    if not fixtures_dir.exists():
        print(f"no fixtures dir at {fixtures_dir}", file=sys.stderr)
        return 2
    fixtures = sorted(
        d for d in fixtures_dir.iterdir() if d.is_dir() and not d.name.startswith(".")
    )
    if args.fixture:
        fixtures = [d for d in fixtures if d.name == args.fixture]
        if not fixtures:
            print(f"no fixture named {args.fixture!r}", file=sys.stderr)
            return 2

    if not fixtures:
        print("(no fixtures pinned yet — drop ctx.json + final.md + expected.json into tests/pulse_regression/fixtures/<id>/)")
        return 0

    validator_mod = _load_validator()

    all_pass = True
    for fd in fixtures:
        passes, msgs = run_fixture(fd, validator_mod)
        status = "PASS" if passes else "FAIL"
        print(f"{status}  {fd.name}")
        for m in msgs:
            print(m)
        if not passes:
            all_pass = False

    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())

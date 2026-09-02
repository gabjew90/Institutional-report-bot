"""Run the smoke scripts by tier, from scripts/smoke_manifest.json.

WHY (2026-09-01 review): 161 smoke scripts existed and the push gate ran
one of them. The rest ran when someone remembered, and 13 of them had
quietly rotted into assertions about a prompt that no longer existed.
The manifest makes the count visible and the tiers make the fast set
cheap enough to run on every push:

  fast    baseline <= 20 s, runs in preflight_push.py
  full    everything not retired; `--full` (a few minutes, 6 workers)
  retired stub files kept so a deleted smoke reads as retired, not lost

A smoke on disk that is missing from the manifest is a failure: add it
with a tier. A manifest entry with no file is also a failure.
"""
import argparse
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MANIFEST = REPO / "scripts" / "smoke_manifest.json"
TIMEOUT_S = 150


def _run(name: str) -> tuple[str, int, str]:
    env = dict(os.environ, PYTHONPATH=str(REPO), PYTHONIOENCODING="utf-8")
    try:
        r = subprocess.run([sys.executable, str(REPO / "scripts" / f"{name}.py")],
                           capture_output=True, text=True, env=env,
                           cwd=REPO, timeout=TIMEOUT_S)
        tail = "\n".join(((r.stdout or "") + (r.stderr or "")).strip().splitlines()[-4:])
        return name, r.returncode, tail
    except subprocess.TimeoutExpired:
        return name, 124, f"timed out after {TIMEOUT_S}s"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", choices=["fast", "full"], default="fast")
    ap.add_argument("--full", action="store_true", help="alias for --tier full")
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()
    tier = "full" if a.full else a.tier

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    on_disk = {p.stem for p in (REPO / "scripts").glob("smoke_*.py")}
    missing = sorted(on_disk - set(manifest))
    ghosts = sorted(set(manifest) - on_disk)
    if missing or ghosts:
        print(f"MANIFEST OUT OF DATE: unlisted={missing} ghosts={ghosts}")
        return 2

    if tier == "fast":
        names = [n for n, v in manifest.items() if v["tier"] == "fast"]
    else:
        names = [n for n, v in manifest.items() if v["tier"] != "retired"]
    print(f"smoke manifest: {len(manifest)} listed "
          f"({sum(1 for v in manifest.values() if v['tier']=='retired')} retired); "
          f"running {len(names)} in tier '{tier}' with {a.workers} workers")
    failed = []
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for name, rc, tail in ex.map(_run, names):
            if rc != 0:
                failed.append(name)
                print(f"FAIL {name} (rc={rc})\n    " + tail.replace("\n", "\n    "))
    print(f"{len(names) - len(failed)}/{len(names)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

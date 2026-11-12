"""BLAS thread limits are set before numpy can load (runtime_env.py).

2026-11-11: OpenBLAS started 48 threads in a container with a quota of 8
CPUs, the kernel froze the container for seconds to a minute at a time,
and the frozen batch process held the database's write lock.
"""
import ast
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _first_import(path: str) -> str:
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Import):
            return node.names[0].name
        if isinstance(node, ast.ImportFrom):
            return node.module or ""
    return ""


def test_every_entry_point_imports_the_limits_first():
    for path in ("main.py", "scheduler/batch_main.py", "config.py"):
        assert _first_import(path) == "runtime_env", path


def test_the_limits_are_set_and_an_explicit_setting_wins():
    code = ("import os, runtime_env; "
            "print(os.environ['OPENBLAS_NUM_THREADS'], os.environ['OMP_NUM_THREADS'])")
    env = {k: v for k, v in os.environ.items()
           if k not in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS")}
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env,
                         capture_output=True, text=True, check=True).stdout.split()
    assert out == ["2", "2"]
    env["OPENBLAS_NUM_THREADS"] = "4"
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env,
                         capture_output=True, text=True, check=True).stdout.split()
    assert out == ["4", "2"]


def test_numpy_is_not_loaded_before_the_limits():
    """Importing config must not have pulled numpy in ahead of the limits."""
    code = ("import sys, runtime_env; early = 'numpy' in sys.modules; "
            "import config; print(early)")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout.strip()
    assert out == "False"

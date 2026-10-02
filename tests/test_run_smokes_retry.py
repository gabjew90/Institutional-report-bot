"""run_smokes retries a smoke once only when Windows refused a DLL load."""
import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "run_smokes", Path(__file__).resolve().parents[1] / "scripts" / "run_smokes.py")
run_smokes = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(run_smokes)

DLL_TAIL = ("  import ctypes\nImportError: DLL load failed while importing "
            "_ctypes: The handle is invalid.")


def _sequence(monkeypatch, results):
    calls = []

    def fake(name):
        calls.append(name)
        return results[len(calls) - 1]

    monkeypatch.setattr(run_smokes, "_run_once", fake)
    return calls


def test_dll_flake_is_retried_once(monkeypatch):
    calls = _sequence(monkeypatch, [("s", 1, DLL_TAIL, True), ("s", 0, "PASS", False)])
    assert run_smokes._run("s") == ("s", 0, "PASS")
    assert len(calls) == 2


def test_dll_flake_twice_still_fails(monkeypatch):
    calls = _sequence(monkeypatch, [("s", 1, DLL_TAIL, True), ("s", 1, DLL_TAIL, True)])
    assert run_smokes._run("s")[1] == 1
    assert len(calls) == 2


def test_assertion_failure_is_not_retried(monkeypatch):
    calls = _sequence(monkeypatch, [("s", 1, "AssertionError: wrong ticker", False)])
    assert run_smokes._run("s")[1] == 1
    assert len(calls) == 1

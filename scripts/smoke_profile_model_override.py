"""Smoke: profile generator model is independently overridable.

Context (2026-07-29): under the anchor-receipts mandate, all 49
regenerations tripped lint first-pass on gemini-3.1-flash-lite; the
single retry rescued 16, 15 kept prior text. Compliance is a
capability problem — same diagnosis as /ask, same fix pattern:
`PROFILE_GEMINI_MODEL` env override (default = GEMINI_MODEL), so the
profile generator can run a stronger tier without touching the PDF
pipeline's model.

Covers:
  - config exposes profile_gemini_model, empty default
  - both generation sites (text + vision) resolve through the override
"""

import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def test_config_field_exists():
    from config import Settings
    fields = Settings.model_fields
    assert "profile_gemini_model" in fields, "config field missing"
    assert fields["profile_gemini_model"].default == "", (
        "default must be empty (= fall back to gemini_model)"
    )
    _ok("config: profile_gemini_model exists, empty default")


def test_generation_sites_use_override():
    import scripts.backfill_user_profiles as bf
    src = inspect.getsource(bf)
    n = src.count("settings.profile_gemini_model or settings.gemini_model")
    assert n >= 2, (
        f"both generation sites (text + vision fallback) must resolve "
        f"through the override, found {n}"
    )
    _ok("both generation sites resolve through profile_gemini_model")


if __name__ == "__main__":
    print("=== profile model override smoke ===")
    test_config_field_exists()
    test_generation_sites_use_override()
    print("\nALL PROFILE MODEL OVERRIDE SMOKE TESTS PASS")

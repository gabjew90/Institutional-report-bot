"""Smoke test for analyst_log.ocr.extract_trade_from_message.

Validates (Gemini stubbed throughout):
  1. Text-only message classified correctly (no image bytes, no cached OCR)
  2. Image-only message routes the same code path
  3. Mixed text + image message returns extraction_source='mixed'
  4. is_trade=false response writes no row
  5. confidence < 0.6 response writes no row
  6. Trade without ticker (only action verb) writes no row
  7. Fuzzy schema: partial fields (just ticker + action) accepted
  8. cached_ocr_text: backfill path uses cached OCR as image evidence
"""

import asyncio
import json
import sys
from unittest.mock import patch, MagicMock

import analyst_log.ocr as ocr_mod


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def _fake_gemini(payload: dict):
    """Build a fake Gemini response with the given JSON payload."""
    resp = MagicMock()
    resp.text = json.dumps(payload)
    return resp


async def _run(text, images=None, cached_ocr=""):
    return await ocr_mod.extract_trade_from_message(
        text=text,
        image_bytes_list=images or [],
        author_username="zhawk",
        channel_name="🫦-zhawk-thawghts-🗣",
        cached_ocr_text=cached_ocr,
    )


def test_text_only_full_extraction():
    payload = {
        "is_trade": True, "action": "open", "ticker": "PURR",
        "contract_type": "call", "strike": 14.0, "expiry": "2026-12-18",
        "price": 4.10, "gain_pct": None, "confidence": 0.92,
    }
    with patch.object(ocr_mod, "_call_gemini_classifier",
                      return_value=_fake_gemini(payload)):
        result = asyncio.run(_run("PURR Leaps 12/18 $14 / $4.10 per contract"))
    assert result is not None and result["is_trade"] is True, result
    assert result["ticker"] == "PURR", result
    assert result["extraction_source"] == "text", result
    _ok("text-only message classified with full schema")


def test_image_only_full_extraction():
    payload = {
        "is_trade": True, "action": "close", "ticker": "AAPL",
        "contract_type": "call", "gain_pct": 45.0, "confidence": 0.85,
    }
    with patch.object(ocr_mod, "_call_gemini_classifier",
                      return_value=_fake_gemini(payload)):
        result = asyncio.run(_run("", images=[b"fake-png-bytes"]))
    assert result is not None and result["is_trade"] is True, result
    assert result["extraction_source"] == "image", result
    _ok("image-only message classified")


def test_mixed_text_image():
    payload = {
        "is_trade": True, "action": "close", "ticker": "ORCL",
        "expiry": "2026-12-27", "gain_pct": 200.0, "confidence": 0.95,
    }
    with patch.object(ocr_mod, "_call_gemini_classifier",
                      return_value=_fake_gemini(payload)):
        result = asyncio.run(_run(
            "ORCL hit 3x from entry",
            images=[b"fake-png-bytes"],
        ))
    assert result["extraction_source"] == "mixed", result
    _ok("text + image message tagged extraction_source='mixed'")


def test_not_a_trade_returns_none():
    payload = {"is_trade": False, "confidence": 0.9}
    with patch.object(ocr_mod, "_call_gemini_classifier",
                      return_value=_fake_gemini(payload)):
        result = asyncio.run(_run("I always say follow the ball"))
    assert result is None or result.get("is_trade") is False, result
    _ok("is_trade=false returns None / no-write signal")


def test_low_confidence_rejected():
    payload = {
        "is_trade": True, "action": "open", "ticker": "TSLA",
        "confidence": 0.3,
    }
    with patch.object(ocr_mod, "_call_gemini_classifier",
                      return_value=_fake_gemini(payload)):
        result = asyncio.run(_run("might buy some TSLA later idk"))
    assert result is None, (
        f"low-confidence row should be rejected, got {result}"
    )
    _ok("confidence < 0.6 rejected as no-write")


def test_missing_ticker_rejected():
    payload = {
        "is_trade": True, "action": "open", "ticker": None,
        "confidence": 0.9,
    }
    with patch.object(ocr_mod, "_call_gemini_classifier",
                      return_value=_fake_gemini(payload)):
        result = asyncio.run(_run("opened a new position"))
    assert result is None, f"missing-ticker row should be rejected, got {result}"
    _ok("missing ticker rejected (unstitchable row)")


def test_fuzzy_partial_fields_accepted():
    """Per user direction: 'accept whatever is available into JSON as
    long as Gemini believes it's a trade'. So a row with just ticker +
    action and no strike/expiry/price still gets written."""
    payload = {
        "is_trade": True, "action": "open", "ticker": "BTC",
        "contract_type": None, "strike": None, "expiry": None,
        "price": None, "gain_pct": None, "confidence": 0.8,
    }
    with patch.object(ocr_mod, "_call_gemini_classifier",
                      return_value=_fake_gemini(payload)):
        result = asyncio.run(_run("btc long at 73,906 on HL le scalp"))
    assert result is not None and result["is_trade"] is True, result
    assert result["ticker"] == "BTC", result
    _ok("fuzzy schema: partial fields (just ticker + action) accepted")


def test_cached_ocr_only_tagged_image():
    """Backfill path: no live image bytes, but cached_ocr_text is
    populated (eager-OCR pipeline already ran). extraction_source
    should be 'image' since the cached OCR represents an image."""
    payload = {
        "is_trade": True, "action": "open", "ticker": "NVDA",
        "contract_type": "call", "strike": 200.0,
        "expiry": "2026-12-19", "confidence": 0.85,
    }
    with patch.object(ocr_mod, "_call_gemini_classifier",
                      return_value=_fake_gemini(payload)):
        result = asyncio.run(_run(
            "",  # no text caption
            images=[],  # no live bytes
            cached_ocr="Robinhood: NVDA $200 Call 12/19 — BUY filled @ $5.40",
        ))
    assert result is not None, result
    assert result["extraction_source"] == "image", result
    _ok("cached_ocr only tagged 'image' (backfill path)")


def test_json_array_returns_highest_confidence():
    """Gemini sometimes returns a list of trades when one message
    describes multiple positions. Picks the highest-confidence
    trade-positive entry from the array (defer multi-row writes
    to a future enhancement)."""
    payload = [
        {"is_trade": True, "action": "open", "ticker": "PURR",
         "contract_type": "spot", "price": 6.65, "confidence": 0.7},
        {"is_trade": True, "action": "open", "ticker": "ORCL",
         "contract_type": "spot", "price": 176.78, "confidence": 0.9},
        {"is_trade": False, "confidence": 0.5},
    ]
    with patch.object(ocr_mod, "_call_gemini_classifier",
                      return_value=_fake_gemini(payload)):
        result = asyncio.run(_run("Bought ORCL @176.78 and PURR @6.65"))
    assert result is not None and result["is_trade"] is True, result
    # ORCL has 0.9 confidence, PURR has 0.7 -> ORCL wins
    assert result["ticker"] == "ORCL", result
    _ok("JSON array response -> highest-confidence trade extracted")


def test_json_array_all_non_trade_returns_none():
    """If every entry in the array is is_trade=false, return None."""
    payload = [
        {"is_trade": False, "confidence": 0.9},
        {"is_trade": False, "confidence": 0.7},
    ]
    with patch.object(ocr_mod, "_call_gemini_classifier",
                      return_value=_fake_gemini(payload)):
        result = asyncio.run(_run("just market commentary"))
    assert result is None, f"expected None, got {result}"
    _ok("JSON array with no trades -> None")


def test_text_plus_cached_ocr_tagged_mixed():
    """Backfill path with both text caption + cached OCR."""
    payload = {
        "is_trade": True, "action": "open", "ticker": "PURR",
        "contract_type": "call", "strike": 14.0,
        "expiry": "2026-12-18", "price": 4.10, "confidence": 0.95,
    }
    with patch.object(ocr_mod, "_call_gemini_classifier",
                      return_value=_fake_gemini(payload)):
        result = asyncio.run(_run(
            "PURR 12/18 $14 opened",  # user's caption
            images=[],  # no live bytes (backfill)
            cached_ocr="Order ticket: PURR 12/18/2026 C 14 - filled @ 4.10",
        ))
    assert result is not None, result
    assert result["extraction_source"] == "mixed", result
    _ok("text + cached_ocr (no live bytes) tagged 'mixed'")


if __name__ == "__main__":
    print("=== extract_trade_from_message smoke ===")
    test_text_only_full_extraction()
    test_image_only_full_extraction()
    test_mixed_text_image()
    test_not_a_trade_returns_none()
    test_low_confidence_rejected()
    test_missing_ticker_rejected()
    test_fuzzy_partial_fields_accepted()
    test_cached_ocr_only_tagged_image()
    test_text_plus_cached_ocr_tagged_mixed()
    test_json_array_returns_highest_confidence()
    test_json_array_all_non_trade_returns_none()
    print("\nALL EXTRACT-TRADE-FROM-MESSAGE SMOKE TESTS PASS")

"""Claude analysis lane helper (scripts/pilot_analyze.py, 2026-10-06)."""
import datetime as dt
import json

from scripts import pilot_analyze as PA

TEXT = ("Goldman Sachs US Economics. We expect core PCE to rise 0.3% in September. "
        "Our 10-year Treasury forecast is 4.6% by year end.")


def _doc(root, doc_id="18282", date=None):
    date = date or dt.date.today().isoformat()
    st = root / "pilot" / "source-text" / date
    st.mkdir(parents=True, exist_ok=True)
    text_rel = f"pilot/source-text/{date}/{doc_id}__gs-econ.txt"
    (root / text_rel).write_text(TEXT, encoding="utf-8")
    (st / f"{doc_id}.meta.json").write_text(json.dumps({
        "pdf_file_id": int(doc_id), "file_name": "GS_US_Economics.pdf",
        "source": "Goldman Sachs", "title": "US Economics", "priority": "high",
        "text_path": text_rel}), encoding="utf-8")
    return date


def test_pending_lists_documents_without_an_analysis(tmp_path):
    date = _doc(tmp_path)
    _doc(tmp_path, "18283", date)
    (tmp_path / "pilot" / "analyses" / date).mkdir(parents=True)
    (tmp_path / "pilot" / "analyses" / date / "18283.json").write_text("{}")
    rows = PA.pending(tmp_path / "pilot", days=3, limit=25)
    assert [r[0] for r in rows] == ["18282"]


def test_a_document_stops_after_three_failed_attempts(tmp_path):
    date = _doc(tmp_path)
    for _ in range(PA.MAX_ATTEMPTS):
        PA.fail(tmp_path / "pilot", date, "18282", "no JSON")
    assert PA.pending(tmp_path / "pilot", days=3, limit=25) == []


def test_the_prompt_is_the_production_analysis_prompt_with_the_text(tmp_path):
    from ai_analysis.prompts import ANALYSIS_SYSTEM_PROMPT
    date = _doc(tmp_path)
    text, meta = PA.pending(tmp_path / "pilot", 3, 25)[0][1:3]
    out = tmp_path / "p.txt"
    PA.write_prompt(text, meta, str(out))
    p = out.read_text(encoding="utf-8")
    assert p.startswith(ANALYSIS_SYSTEM_PROMPT[:200])
    assert TEXT in p and "GS_US_Economics.pdf" in p and "JSON object only" in p
    assert date


def test_finalize_builds_a_full_record_with_the_anchor_check(tmp_path):
    _doc(tmp_path)
    _, text, meta, _ = PA.pending(tmp_path / "pilot", 3, 25)[0]
    answer = tmp_path / "out.txt"
    answer.write_text("```json\n" + json.dumps({
        "source": "Goldman Sachs", "title": "US Economics", "report_type": "macro",
        "key_insights": ["Goldman expects core PCE +0.3% in September."],
        "macro_indicators": [{"indicator": "Core PCE m/m", "reading": "0.3%",
                              "interpretation": "firm", "status": "forecast"}],
        "key_data_points": [{"figure": "0.3%", "metric": "Core PCE m/m", "source_bank": "Goldman Sachs",
                             "anchor": "We expect core PCE to rise 0.3% in September"}],
        "trade_ideas": [], "entities_mentioned": [],
    }) + "\n```", encoding="utf-8")
    out = tmp_path / "a.json"
    rec = PA.finalize(str(answer), text, meta, str(out), "claude-sonnet-5-5")
    a = json.loads(out.read_text(encoding="utf-8"))["analysis"]
    assert rec["pdf_file_id"] == 18282 and a["priority"] == "high"
    assert a["macro_indicators"][0]["indicator"] == "Core PCE m/m"
    assert a["anchor_check"]["matched"] == 1


def test_finalize_refuses_an_answer_without_analysis(tmp_path):
    _doc(tmp_path)
    _, text, meta, _ = PA.pending(tmp_path / "pilot", 3, 25)[0]
    bad = tmp_path / "bad.txt"
    bad.write_text("I could not read the document.", encoding="utf-8")
    import pytest
    with pytest.raises(ValueError):
        PA.finalize(str(bad), text, meta, str(tmp_path / "x.json"), "m")


def test_cli_json_output_carries_the_answer_and_the_token_usage(tmp_path):
    _doc(tmp_path)
    _, text, meta, _ = PA.pending(tmp_path / "pilot", 3, 25)[0]
    m = json.loads(open(meta, encoding="utf-8").read())
    m["total_pages"] = 14
    open(meta, "w", encoding="utf-8").write(json.dumps(m))
    answer = json.dumps({"key_insights": ["x"], "source": "Goldman Sachs"})
    raw = tmp_path / "cli.json"
    raw.write_text(json.dumps({"type": "result", "result": answer,
                               "usage": {"input_tokens": 900, "cache_read_input_tokens": 100,
                                         "output_tokens": 1200}}), encoding="utf-8")
    PA.finalize(str(raw), text, meta, str(tmp_path / "a.json"), "m")
    a = json.loads((tmp_path / "a.json").read_text(encoding="utf-8"))["analysis"]
    assert (a["total_pages"], a["input_tokens"], a["output_tokens"]) == (14, 1000, 1200)


def test_a_usage_limit_is_not_a_document_failure():
    assert PA.is_limit(json.dumps({"result": "You've hit your weekly limit. Resets Monday."}))
    assert PA.is_limit("Claude AI usage limit reached|1759800000")
    assert not PA.is_limit(json.dumps({"result": '{"key_insights": ["rate limit on banks"]}'}))

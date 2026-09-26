"""Tag each chat message once as race-edged or not, for the racism board.

WHY (2026-09-26). The racism rank used `user_profiles.racial_humor_score`,
a 0-100 number Gemini re-judged every six hours from only the messages
posted since the last refresh. It measured the last few hours of chat:
one member went 10 -> 92 in two days on 13 lifetime slurs, and a member
with zero slurs was ranked #26 of 44. Members challenged ranks the bot
could not back up. A per-message tag, stored once, turns the rank into a
count over a trailing window (db.race_board) with the tagged messages as
the evidence.

Order of decisions per message, cheapest first:
  1. a racial slur by regex (scripts.slur_patterns.count_racial_slurs)
     -> race-edged, no model call
  2. no letters to judge -> not race-edged
  3. Gemini, in batches, for stereotypes and mockery with no slur

A batch the model refuses is split in half until the refusing message is
alone; that message is stored as -1 (refused), excluded from counts and
never retried. A transport error (network, 429) stops the run and leaves
the batch untagged for the next one.
"""
from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import db
from config import settings
from scripts.slur_patterns import count_racial_slurs

log = logging.getLogger(__name__)

# The board reads 30 days; tagging 60 gives the prior window, which is
# what a "did I move up this month" answer compares against.
TAG_WINDOW_DAYS = 60
BATCH = 120
MAX_PER_RUN = 12000
WORKERS = 4
_TEXT_CAP = 300

_LETTERS = re.compile(r"[A-Za-z]{3,}")

_PROMPT = """\
You label messages from a private trading chat. For each message decide
whether it is RACE-EDGED.

Race-edged:
- uses a slur for a racial, ethnic or religious group, including casual
  or affectionate use
- mocks, stereotypes or demeans people for their race, ethnicity,
  nationality or religion, including a joke that runs on a stereotype

Not race-edged:
- a neutral or factual mention of a group, country or religion (news,
  geopolitics, markets: "China tariffs", "Israel strikes Iran")
- profanity or insults that are not about race, ethnicity, nationality
  or religion
- slurs about sexuality or disability

When unsure, it is not race-edged.

Each line below is `id<TAB>message`. Return JSON
{"race_edged": [ids]} listing only the ids that are race-edged.

MESSAGES:
"""

_client = None


def _get_client():
    global _client
    if _client is None:
        from google import genai
        _client = genai.Client(api_key=settings.google_api_key)
    return _client


def _model() -> str:
    return settings.gemini_triage_model or settings.gemini_model


class _Refused(Exception):
    """The model returned no usable label set for this batch."""


def _classify(batch: list[dict]) -> set[int]:
    """Ids the model labels race-edged. Raises _Refused on a blocked or
    unparseable response, and lets transport errors propagate."""
    from google.genai import types
    lines = "\n".join(
        f"{m['id']}\t{' '.join((m['content'] or '').split())[:_TEXT_CAP]}"
        for m in batch)
    cfg = types.GenerateContentConfig(
        temperature=0.0,
        max_output_tokens=2000,
        response_mime_type="application/json",
        response_schema=types.Schema(
            type=types.Type.OBJECT,
            properties={"race_edged": types.Schema(
                type=types.Type.ARRAY,
                items=types.Schema(type=types.Type.INTEGER))},
            required=["race_edged"],
        ),
        safety_settings=[
            types.SafetySetting(category=c,
                                threshold=types.HarmBlockThreshold.BLOCK_NONE)
            for c in (types.HarmCategory.HARM_CATEGORY_HARASSMENT,
                      types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
                      types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
                      types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT)
        ],
    )
    resp = _get_client().models.generate_content(
        model=_model(), contents=_PROMPT + lines, config=cfg)
    try:
        text = resp.text or ""
    except Exception:
        text = ""
    if not text.strip():
        raise _Refused("empty response")
    try:
        ids = json.loads(text).get("race_edged") or []
    except Exception as e:
        raise _Refused(f"unparseable: {e}")
    wanted = {m["id"] for m in batch}
    return {int(i) for i in ids if isinstance(i, (int, float)) and int(i) in wanted}


def _tag_batch(batch: list[dict]) -> list[tuple]:
    """Tag rows for one model batch, splitting on refusal."""
    try:
        edged = _classify(batch)
    except _Refused as e:
        if len(batch) == 1:
            m = batch[0]
            log.info(f"race_tagger: message {m['id']} refused ({e}); stored as -1")
            return [(m["id"], m["author_id"], m["posted_at"], -1, 0, "refused")]
        mid = len(batch) // 2
        return _tag_batch(batch[:mid]) + _tag_batch(batch[mid:])
    return [(m["id"], m["author_id"], m["posted_at"],
             1 if m["id"] in edged else 0, 0, "model") for m in batch]


def split_by_rule(msgs: list[dict]) -> tuple[list[tuple], list[dict]]:
    """Messages decided without a model call, and the rest."""
    decided, rest = [], []
    for m in msgs:
        text = m.get("content") or ""
        n = count_racial_slurs(text)
        if n:
            decided.append((m["id"], m["author_id"], m["posted_at"], 1, n, "regex"))
        elif not _LETTERS.search(text):
            decided.append((m["id"], m["author_id"], m["posted_at"], 0, 0, "empty"))
        else:
            rest.append(m)
    return decided, rest


def tag_pending(max_messages: int = MAX_PER_RUN) -> dict:
    """Tag up to `max_messages` untagged messages from the tag window.
    Returns counts for the log line."""
    since = (datetime.utcnow() - timedelta(days=TAG_WINDOW_DAYS)).isoformat()
    msgs = db.race_untagged(since, limit=max_messages)
    if not msgs:
        return {"pending": 0, "tagged": 0}
    decided, rest = split_by_rule(msgs)
    written = db.insert_race_tags(decided)
    batches = [rest[i:i + BATCH] for i in range(0, len(rest), BATCH)]
    failed = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [pool.submit(_tag_batch, b) for b in batches]
        for f in futures:
            try:
                written += db.insert_race_tags(f.result())
            except Exception as e:
                failed += 1
                log.warning(f"race_tagger: batch failed, left for next run: {e}")
    return {"pending": len(msgs), "tagged": written, "by_rule": len(decided),
            "model_batches": len(batches), "failed_batches": failed}

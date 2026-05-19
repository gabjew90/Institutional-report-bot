"""BK (bankerkyle) trade-tracking prototype — stateful diff approach.

What this is:
A standalone read-only experiment that replays the last 7 days of
BK's alert channel (#💅🏾-kyle-alerts-💅🏾), processes each post
through a "what changed?" Gemini extraction, and posts inferred state
changes to #test-channel.

Does NOT:
- Write to the analyst_trades table
- Touch the Abe pipeline in any way
- Affect production state

How the new design differs from the Abe pipeline:
- Abe: per-post stateless classify (each screenshot independently
  decoded into action+contract+gain)
- BK: per-post STATE DIFF (the model sees current open positions +
  closes in last 24h + last 5 messages in the channel + this new
  message, and outputs a list of state changes)

The new approach handles BK's style naturally:
- Text-only "Sold @19.00" resolves via chain context
- Multi-position messages produce multiple state changes
- Hype-only screenshots resolve via current-state context
- Commentary ("China calls look great") produces empty diff list
"""
import asyncio
import io
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Add repo root to path so config + db imports work when running -m
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

import aiohttp
import discord
from google import genai
from google.genai import types
from PIL import Image as PILImage

from config import settings


log = logging.getLogger(__name__)


# ============================================================================
# Config
# ============================================================================
BK_CHANNEL_NAME = "💅🏾-kyle-alerts-💅🏾"
OUTPUT_CHANNEL_NAME = "test-channel"
LOOKBACK_DAYS = 7
RECENT_CHAIN_SIZE = 5  # number of prior messages to include for chain context
CLOSED_LOOKBACK_HOURS = 24  # how far back to include closed positions
MODEL = "gemini-3.1-flash-lite-preview"  # text-tier; will switch to vision-capable for image posts
VISION_MODEL = "gemini-3.1-flash-lite"   # GA variant — needed for image attachments
THROTTLE_SECONDS = 1.5  # delay between Discord posts to test-channel


# ============================================================================
# State data structures
# ============================================================================
@dataclass
class TradeEvent:
    """One row in the state log. Mirrors analyst_trades row shape but
    stays in memory only."""
    posted_at: str
    discord_message_id: int
    caller: str  # "bankerkyle"
    action: str  # open / add / trim / close / viewing
    ticker: str
    contract_type: str  # call / put / stock
    strike: float | None
    expiry: str | None  # YYYY-MM-DD
    price: float | None
    gain_pct: float | None
    confidence: int
    evidence: str
    source_jump_url: str


def _key(ticker: str, contract_type: str, strike: float | None,
         expiry: str | None) -> tuple:
    """The contract identity used to track position state. Two events
    with the same key act on the same position."""
    return (
        (ticker or "").upper(),
        (contract_type or "").lower(),
        strike,
        expiry,
    )


def current_open_positions(events: list[TradeEvent]) -> list[TradeEvent]:
    """Compute currently-open positions via the same state machine as
    db.get_current_analyst_positions: latest event per contract key,
    surviving if action is open/add/trim, gone if close.
    Viewing events don't open a position.

    Filters out:
    - Expired contracts (expiry date < today UTC)
    - Sentinel-strike events (strike == 0 from failed extraction)
    """
    today = datetime.now(timezone.utc).date()
    latest_by_key: dict[tuple, TradeEvent] = {}
    for e in events:
        if e.action == "viewing":
            continue
        # Filter sentinel/missing strike
        if not e.strike or e.strike == 0:
            continue
        # Filter expired
        if e.expiry:
            try:
                exp_dt = datetime.fromisoformat(e.expiry).date()
                if exp_dt < today:
                    continue
            except Exception:
                pass
        latest_by_key[_key(e.ticker, e.contract_type, e.strike, e.expiry)] = e
    return [
        e for e in latest_by_key.values()
        if e.action in ("open", "add", "trim")
    ]


def closes_in_last_hours(events: list[TradeEvent], hours: int,
                         now_ts: str) -> list[TradeEvent]:
    """Closed positions within the last `hours` of the timeline so far.
    Lets the prompt see 'this MSFT 430C closed 30 min ago' so it doesn't
    re-open a phantom."""
    try:
        now_dt = datetime.fromisoformat(now_ts.replace("Z", "+00:00"))
    except Exception:
        return []
    cutoff_dt = now_dt - timedelta(hours=hours)
    out = []
    for e in events:
        if e.action != "close":
            continue
        try:
            e_dt = datetime.fromisoformat(e.posted_at.replace("Z", "+00:00"))
            if e_dt >= cutoff_dt:
                out.append(e)
        except Exception:
            continue
    return out


# ============================================================================
# Image normalization (same pipeline as profile backfill)
# ============================================================================
def _normalize_image_bytes(raw: bytes) -> bytes | None:
    try:
        img = PILImage.open(io.BytesIO(raw))
        if img.mode != "RGB":
            img = img.convert("RGB")
        max_side = 1600
        if max(img.size) > max_side:
            img.thumbnail((max_side, max_side), PILImage.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85, optimize=True)
        return buf.getvalue()
    except Exception:
        return None


_MAX_IMAGE_BYTES = 4_000_000


async def _download_image(
    session: aiohttp.ClientSession, url: str
) -> bytes | None:
    try:
        async with session.get(
            url, timeout=aiohttp.ClientTimeout(total=15)
        ) as r:
            if r.status != 200:
                return None
            data = await r.content.read(_MAX_IMAGE_BYTES + 1)
            return data if len(data) <= _MAX_IMAGE_BYTES else None
    except Exception:
        return None


# ============================================================================
# Gemini state-diff prompt
# ============================================================================
STATE_DIFF_PROMPT = """\
You are an analyst trade-log reviewer for **BankerKyle** (display name "BK"),
a live trade caller in a private options-trading discord. BK posts alerts in
his dedicated channel — a mix of pure text ("MSFT 430c @3.65 sold"),
text+screenshot ("Balls DEEP MSFT calls" + portfolio screenshot), and
occasionally screenshot-only with hype caption ("It is time" + image).

Your job: given BK's CURRENT POSITION STATE + recent channel context + this
new message, determine what (if anything) changed about his position state.

# CURRENT STATE (as of just before this message)

## Currently OPEN positions:
{open_positions_block}

## Closed in the last {closed_hours}h:
{recent_closes_block}

# RECENT CHANNEL CONTEXT (last {chain_n} messages from BK, oldest first)
{chain_block}

# THIS NEW MESSAGE

Timestamp: {this_ts}
Caption: {this_caption}
Attached images: {n_images} (provided as multimodal input below)

# YOUR TASK

Decide what state changes (if any) this new message implies. Output strict JSON:

{{
  "is_trade": true | false,
  "interpretation": "<one short sentence describing what BK is doing>",
  "state_changes": [
    {{
      "action": "open" | "add" | "trim" | "close" | "viewing",
      "ticker": "<UPPERCASE ticker, e.g. MSFT, NDX, RUT, FXI>",
      "contract_type": "call" | "put" | "stock",
      "strike": <number or null>,
      "expiry": "YYYY-MM-DD" or null,
      "price": <number or null>,
      "gain_pct": <number or null>,
      "confidence": <integer 0-100>,
      "evidence": "<one short sentence quoting the caption or describing the image, and citing chain/state context if used to resolve>"
    }}
  ]
}}

# RULES

**is_trade=false** ONLY if the message is pure commentary, sentiment, or
chitchat with NO actionable position implication. Examples that are still
TRADES (is_trade=true): "Balls DEEP MSFT calls" + image (=open), "Sold
@19.00" with prior chain showing the position (=close), screenshot only +
"Currently" (=likely add/update to existing position visible in image).
Pure commentary like "I think China is a great buy here" with no contract
named = is_trade=false, state_changes=[].

**ticker normalization**: Use uppercase canonical ticker. Common BK
shorthand:
- "rut" / "RUT" → RUT
- "ndx" / "NDX" → NDX
- "msft" → MSFT
- "meta" → META
- "chyna" / "CHYYYNNAAA" / "moar chyna" → FXI (China large-cap ETF; this is
  BK's standing shorthand)
- "coin" → COIN
- "mstr" → MSTR
- "pltr" → PLTR
- "igv" → IGV
- "sofi" → SOFI
- "spy" → SPY

**Reading captions like "Ndx 29000c @7.00"**: ticker=NDX, strike=29000,
contract_type=call, price=7.00. Action defaults to "open" UNLESS the caption
or image indicates sell/close (e.g. "Sold", "sold", "@" alone in a follow-up
context, gain pill in image, "took profit").

**Reading captions like "Sold all 5/22 435c @3.66"**: action=close,
expiry=2026-05-22 (use the year that fits within ~2 months of the message
timestamp), strike=435, contract_type=call, price=3.66. Ticker must come from
EITHER the same message (sometimes at the end: "Sold ... MSFT") OR from the
chain context (the most recent open with strike=435 5/22).

**Chain resolution** — for terse follow-ups:
- "Sold @19.00" alone → find the latest open in OPEN positions (or
  bought-but-immediately-screenshotted in chain) and apply the close at
  19.00. Confidence drops if multiple opens are equally plausible.
- "These are 8 now!" → reference to gain on the latest open position.
  This is a STATUS update (not a state change). Output is_trade=true,
  state_changes=[] with interpretation explaining it.

**Hype captions on screenshots** ("Quite sexy", "It is time", "Currently"):
parse the IMAGE for actual contract data. Don't drop these as untraded —
the image carries the signal.

**Multi-position messages**: a single post like "Pltr weeklies and 5/29" +
image showing two PLTR contracts at different strikes = TWO state_changes
entries.

**Action defaults when ambiguous**:
- "Sold" / "sold" / "@" follow-up → close
- "BTO" / "long" / no verb + new contract → open
- "Added" / "more" / "moar" → add
- "Took half" / "trimmed" / "scaled" → trim
- Order ticket image with no caption → action depends on image content
  (buy ticket = open, sell ticket = close)

**Confidence**:
- 90-100: explicit and unambiguous. "Sold all 5/22 435c @3.66".
- 70-89: one field inferred from chain or image. "Pltr weeklies" + image
  showing the strike.
- 50-69: requires moderate inference. "Sold @19" with multiple plausible
  positions.
- 0-49: speculative. Better to log with low confidence than skip — surface
  the uncertainty.

**CRITICAL — NEVER use 0 as a sentinel.** If you cannot read a strike from
the caption OR the image, OMIT the strike field from the JSON (or use null).
Do NOT emit strike=0. Same for price and gain_pct — null/omit when not
extractable; reserve 0 for the genuine cases (e.g. a $0.01 fill is a real
price, but "I couldn't find the strike on this image" is NOT a $0 strike).
If the image shows multiple contracts and you can't determine which strike
the caption refers to, emit ONE state_change per visible contract (the model
should READ the image, not guess).

When the IMAGE contains the answer (portfolio screenshot showing
ticker+strike+expiry for each contract), USE IT. The caption "Meta calls
for tomorrow expiry and 5/29" combined with a screenshot of two META
contracts at, say, 615 strike = TWO state_changes: META 615C 2026-05-20
and META 615C 2026-05-29, with strikes pulled from the image. Don't drop
strike just because the caption was vague — the screenshot was sent
precisely so you could read it.

**Evidence** field MUST cite the source: either a quote from the caption
("Sold all 5/22 435c @3.66"), a description of the image ("image shows
MSFT 430C 5/20 portfolio card, +12.5%"), or a chain reference ("Closes the
latest open MSFT 430C 5/20 from the prior message at 13:45").

**No state changes** is a valid response when the message is purely
descriptive but the position state hasn't actually changed (status update
like "These are 8 now!", or market commentary like "China is hot rn").
Return is_trade=true OR false depending on whether it's referencing a
trade at all, state_changes=[].
"""


SCHEMA = types.Schema(
    type=types.Type.OBJECT,
    properties={
        "is_trade": types.Schema(type=types.Type.BOOLEAN),
        "interpretation": types.Schema(type=types.Type.STRING),
        "state_changes": types.Schema(
            type=types.Type.ARRAY,
            items=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "action": types.Schema(type=types.Type.STRING),
                    "ticker": types.Schema(type=types.Type.STRING),
                    "contract_type": types.Schema(type=types.Type.STRING),
                    "strike": types.Schema(type=types.Type.NUMBER),
                    "expiry": types.Schema(type=types.Type.STRING),
                    "price": types.Schema(type=types.Type.NUMBER),
                    "gain_pct": types.Schema(type=types.Type.NUMBER),
                    "confidence": types.Schema(type=types.Type.INTEGER),
                    "evidence": types.Schema(type=types.Type.STRING),
                },
                required=["action", "ticker", "contract_type", "confidence", "evidence"],
            ),
        ),
    },
    required=["is_trade", "interpretation", "state_changes"],
)


SAFETY = [
    types.SafetySetting(
        category=types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
        threshold=types.HarmBlockThreshold.BLOCK_NONE,
    ),
    types.SafetySetting(
        category=types.HarmCategory.HARM_CATEGORY_HARASSMENT,
        threshold=types.HarmBlockThreshold.BLOCK_NONE,
    ),
    types.SafetySetting(
        category=types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
        threshold=types.HarmBlockThreshold.BLOCK_NONE,
    ),
    types.SafetySetting(
        category=types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
        threshold=types.HarmBlockThreshold.BLOCK_NONE,
    ),
]


# ============================================================================
# Prompt builders
# ============================================================================
def _fmt_position(e: TradeEvent) -> str:
    """One-line summary of an event for the prompt's state blocks.

    Display rule (matches the live Abe pipeline):
    - open / add events show @price (entry valuation)
    - close / trim events show (±gain%) (realized return)
    - 0-valued price or gain_pct treated as missing (model sentinel,
      not a real data point)
    """
    strike_s = ""
    if e.strike is not None:
        strike_s = (
            f"{int(e.strike)}" if e.strike == int(e.strike) else f"{e.strike}"
        )
    type_suffix = {"call": "C", "put": "P"}.get(e.contract_type, "")
    expiry_short = e.expiry[5:] if e.expiry and len(e.expiry) >= 10 else (e.expiry or "")
    contract = f"{e.ticker} {strike_s}{type_suffix} {expiry_short}".strip()
    bits = [f"{e.action.upper()} {contract}"]
    is_open = e.action in ("open", "add")
    is_exit = e.action in ("close", "trim")
    if is_open and e.price is not None and float(e.price) != 0:
        bits.append(f"@{e.price}")
    elif is_exit and e.gain_pct is not None and float(e.gain_pct) != 0:
        bits.append(f"({e.gain_pct:+.1f}%)")
    bits.append(f"at {e.posted_at[:16]}")
    return " — ".join(bits)


def _state_block(events: list[TradeEvent]) -> str:
    if not events:
        return "(none)"
    return "\n".join(f"- {_fmt_position(e)}" for e in events)


def _chain_block(chain_msgs: list[dict]) -> str:
    """Render the last N messages from BK as: TS | caption | (N images)."""
    if not chain_msgs:
        return "(none)"
    out = []
    for m in chain_msgs:
        ts = m["timestamp"][:16]
        cap = (m["content"] or "").replace("\n", " ").strip() or "(no caption)"
        out.append(
            f"- [{ts}] {cap[:200]} ({m['n_images']} img)"
        )
    return "\n".join(out)


# ============================================================================
# Gemini call
# ============================================================================
async def extract_state_diff(
    gemini_client: genai.Client,
    http_session: aiohttp.ClientSession,
    current_state: list[TradeEvent],
    recent_closes: list[TradeEvent],
    chain_msgs: list[dict],
    this_msg: dict,
) -> dict:
    """Returns the parsed JSON dict from the model, or {} on hard failure."""
    prompt = STATE_DIFF_PROMPT.format(
        open_positions_block=_state_block(current_state),
        recent_closes_block=_state_block(recent_closes),
        closed_hours=CLOSED_LOOKBACK_HOURS,
        chain_n=RECENT_CHAIN_SIZE,
        chain_block=_chain_block(chain_msgs),
        this_ts=this_msg["timestamp"][:16],
        this_caption=this_msg["content"] or "(no caption)",
        n_images=this_msg["n_images"],
    )

    # Download + normalize images for this message
    image_parts: list = []
    for url in this_msg.get("image_urls", []):
        blob = await _download_image(http_session, url)
        if not blob:
            continue
        clean = _normalize_image_bytes(blob)
        if clean is None:
            continue
        image_parts.append(
            types.Part.from_bytes(data=clean, mime_type="image/jpeg")
        )

    parts = image_parts + [types.Part.from_text(text=prompt)]
    model = VISION_MODEL if image_parts else MODEL

    try:
        resp = await gemini_client.aio.models.generate_content(
            model=model,
            contents=parts,
            config=types.GenerateContentConfig(
                temperature=0.3,
                max_output_tokens=2500,
                response_mime_type="application/json",
                response_schema=SCHEMA,
                safety_settings=SAFETY,
            ),
        )
    except Exception as e:
        print(f"  Gemini call failed: {e}", flush=True)
        return {}

    text = (resp.text or "").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        print(f"  JSON decode error: {e}; raw={text[:300]!r}", flush=True)
        return {}


# ============================================================================
# Output formatting
# ============================================================================
def _format_announce(change: dict, source_jump_url: str) -> str:
    """One-line summary for posting to test-channel. Same display
    rule as the live Abe pipeline: opens show @price, exits show
    (±gain%); 0-values treated as missing."""
    action = (change.get("action") or "?").upper()
    ticker = (change.get("ticker") or "?").upper()
    strike = change.get("strike")
    ctype = (change.get("contract_type") or "").lower()
    expiry = change.get("expiry") or ""
    price = change.get("price")
    gain = change.get("gain_pct")
    conf = change.get("confidence", 0)
    evidence = (change.get("evidence") or "").replace("\n", " ")[:200]

    type_suffix = {"call": "C", "put": "P"}.get(ctype, "")
    strike_s = ""
    if strike is not None:
        try:
            strike_s = f"{int(strike)}" if float(strike) == int(float(strike)) else f"{strike}"
        except Exception:
            strike_s = str(strike)
    expiry_short = expiry[5:] if len(expiry) >= 10 else expiry
    contract = f"{ticker} {strike_s}{type_suffix} {expiry_short}".strip()

    line = f"📝 [PROTOTYPE] **BK** {action} **{contract}**"
    is_open = action in ("OPEN", "ADD")
    is_exit = action in ("CLOSE", "TRIM")
    suffix = ""
    if is_open and price is not None and float(price) != 0:
        suffix = f" @{price}"
    elif is_exit and gain is not None and float(gain) != 0:
        suffix = f" ({gain:+.1f}%)"
    line += suffix

    flag = "⚠️ " if conf < 50 else ""
    line += f" — {flag}conf={conf}"

    if evidence:
        line += f"\n  _{evidence}_"
    line += f"\n  <{source_jump_url}>"
    return line[:1900]


# ============================================================================
# Main
# ============================================================================
async def main():
    if not settings.discord_bot_token:
        print("ERROR: DISCORD_BOT_TOKEN missing", file=sys.stderr)
        sys.exit(1)
    if not settings.google_api_key:
        print("ERROR: GOOGLE_API_KEY missing", file=sys.stderr)
        sys.exit(1)

    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)
    gemini_client = genai.Client(api_key=settings.google_api_key)

    @client.event
    async def on_ready():
        try:
            # Find the BK channel + output channel
            bk_channel: discord.TextChannel | None = None
            out_channel: discord.TextChannel | None = None
            for guild in client.guilds:
                for ch in guild.text_channels:
                    if ch.name == BK_CHANNEL_NAME:
                        bk_channel = ch
                    elif ch.name == OUTPUT_CHANNEL_NAME:
                        out_channel = ch
            if bk_channel is None:
                print(f"ERROR: BK channel '{BK_CHANNEL_NAME}' not found")
                return
            if out_channel is None:
                print(f"ERROR: output channel '{OUTPUT_CHANNEL_NAME}' not found")
                return

            print(f"BK channel: #{bk_channel.name} (id={bk_channel.id})")
            print(f"Output channel: #{out_channel.name} (id={out_channel.id})")

            # Header post
            header = (
                f"🧪 **BK prototype replay** — last {LOOKBACK_DAYS} days "
                f"from #{BK_CHANNEL_NAME}\n"
                f"Method: stateful-diff (current positions + last "
                f"{RECENT_CHAIN_SIZE} messages + new post → Gemini → state changes)\n"
                f"Confidence < 50 flagged with ⚠️"
            )
            await out_channel.send(header)
            await asyncio.sleep(THROTTLE_SECONDS)

            cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)

            # Scan messages oldest-first so state builds chronologically
            messages: list[dict] = []
            async for msg in bk_channel.history(
                limit=None, after=cutoff, oldest_first=True
            ):
                if msg.author.bot:
                    continue
                # Match the configured caller username (avoid noise from
                # other people posting in his channel if any).
                if msg.author.name.lower() != "bankerkyle":
                    continue
                content = (msg.content or "").strip()
                image_urls = []
                for att in msg.attachments:
                    if (att.content_type or "").lower().startswith("image/"):
                        image_urls.append(att.url)
                # Also try embed images
                for e in msg.embeds:
                    for attr in ("image", "thumbnail"):
                        ref = getattr(e, attr, None)
                        url = getattr(ref, "url", None) if ref else None
                        if url and isinstance(url, str):
                            image_urls.append(url)
                # Skip totally empty messages (no caption AND no images)
                if not content and not image_urls:
                    continue
                messages.append({
                    "id": msg.id,
                    "timestamp": msg.created_at.isoformat(),
                    "content": content,
                    "image_urls": image_urls,
                    "n_images": len(image_urls),
                    "jump_url": msg.jump_url,
                })

            print(f"Scanned {len(messages)} non-empty BK messages over "
                  f"{LOOKBACK_DAYS} days")

            # Walk messages, maintain in-memory event log + state
            events: list[TradeEvent] = []
            state_changes_posted = 0
            commentary_count = 0
            status_update_count = 0
            errors = 0

            async with aiohttp.ClientSession() as http_session:
                for i, m in enumerate(messages):
                    # Build chain context: last N messages BEFORE this one
                    start_idx = max(0, i - RECENT_CHAIN_SIZE)
                    chain = messages[start_idx:i]

                    current = current_open_positions(events)
                    recent_closes = closes_in_last_hours(
                        events, CLOSED_LOOKBACK_HOURS, m["timestamp"]
                    )

                    result = await extract_state_diff(
                        gemini_client, http_session,
                        current, recent_closes, chain, m,
                    )
                    if not result:
                        errors += 1
                        print(f"  [{m['timestamp'][:16]}] ERROR — skipping")
                        continue

                    interpretation = result.get("interpretation", "")[:200]
                    state_changes = result.get("state_changes", []) or []
                    is_trade = bool(result.get("is_trade", False))

                    print(f"  [{m['timestamp'][:16]}] "
                          f"is_trade={is_trade} "
                          f"changes={len(state_changes)} "
                          f"interp={interpretation!r}")

                    if not state_changes:
                        if is_trade:
                            status_update_count += 1
                        else:
                            commentary_count += 1
                        continue

                    # Apply each state change to in-memory event log + post
                    for ch in state_changes:
                        action = str(ch.get("action") or "").lower()
                        ticker = str(ch.get("ticker") or "").upper()
                        strike = ch.get("strike")
                        # Discard junk: strike=0, missing ticker, or
                        # missing strike on contract types that need one
                        # (calls/puts must have a strike; stock can skip).
                        if not ticker:
                            print(f"  DROPPED change for {m['timestamp'][:16]}: no ticker | {ch}")
                            continue
                        ctype = str(ch.get("contract_type") or "").lower()
                        if ctype in ("call", "put") and (not strike or strike == 0):
                            print(f"  DROPPED change for {m['timestamp'][:16]}: bad strike | {ch}")
                            continue
                        ev = TradeEvent(
                            posted_at=m["timestamp"],
                            discord_message_id=m["id"],
                            caller="bankerkyle",
                            action=action,
                            ticker=ticker,
                            contract_type=ctype,
                            strike=strike,
                            expiry=ch.get("expiry"),
                            price=ch.get("price"),
                            gain_pct=ch.get("gain_pct"),
                            confidence=int(ch.get("confidence") or 0),
                            evidence=str(ch.get("evidence") or ""),
                            source_jump_url=m["jump_url"],
                        )
                        events.append(ev)

                        line = _format_announce(ch, m["jump_url"])
                        try:
                            await out_channel.send(line)
                            state_changes_posted += 1
                        except Exception as e:
                            print(f"  POST FAILED: {e}")
                            errors += 1
                        await asyncio.sleep(THROTTLE_SECONDS)

            # Final summary
            final_open = current_open_positions(events)
            final_block = _state_block(final_open) if final_open else "(no open positions)"
            summary = (
                f"🏁 **BK prototype replay complete**\n"
                f"- Messages scanned: {len(messages)}\n"
                f"- State changes posted: {state_changes_posted}\n"
                f"- Status updates (is_trade w/ no change): {status_update_count}\n"
                f"- Commentary (not a trade): {commentary_count}\n"
                f"- Errors: {errors}\n\n"
                f"**Final inferred OPEN positions:**\n"
                f"{final_block}"
            )
            print(summary)
            # Discord 2000 char cap — split if needed
            if len(summary) <= 1900:
                await out_channel.send(summary)
            else:
                for chunk_start in range(0, len(summary), 1800):
                    await out_channel.send(summary[chunk_start:chunk_start + 1800])
                    await asyncio.sleep(THROTTLE_SECONDS)

        except Exception as e:
            log.error(f"Replay failed: {e}", exc_info=True)
            print(f"ERROR: {e}")
        finally:
            await client.close()

    await client.start(settings.discord_bot_token)


if __name__ == "__main__":
    asyncio.run(main())

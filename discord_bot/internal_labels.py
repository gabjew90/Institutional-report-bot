"""Remove the bot's internal labels from an answer before it posts.

The prefetch blocks the model reads open with a capitalised label
("LEAGUE STATE, system-fetched from Sleeper ...") and tool rows carry
status tokens ("past_no_data"). The model sometimes copies them into the
answer as if they were citations. 2026-10-18 audit: "[LEAGUE STATE]" ended
four fantasy answers (F4) and "(`past_no_data`)" reached the room on PPI
day (A27). stock_outline.strip_source_tags covers the outline's tags only;
this covers every block label and status token, on every answer.
"""
import re

# Block labels from ask_router.inject_text (the text before the first comma).
_LABELS = (
    "LEAGUE STATE", "EARNINGS SLATE", "EARNINGS DATE", "ECONOMIC CALENDAR",
    "PRICE HISTORY", "LIVE PRICES", "MARGIN MATH", "UNREAD CHANNEL", "INSTITUTIONAL RESEARCH ON THE TICKER", "RECENT NEWS ON THE TICKER",
    "BUSINESS PRIMER", "TICKER SNAPSHOT", "TREASURY AUCTIONS", "CHAT SEARCH",
    "ROOM POSITIONING", "OPTIONS CHAIN", "ANSWER OUTLINE",
)
_STATUS_TOKENS = ("past_no_data", "no_data", "live_extended_hours", "not_a_stock",
                  "live_regular", "stale_quote")

_LABEL_RE = re.compile(
    r"\s*\[(?:" + "|".join(re.escape(l) for l in _LABELS) + r")\]", re.I)
# A status token, bare or in backticks, with its parentheses when it sits in them.
_STATUS_RE = re.compile(
    r"\s*\(\s*`?(?:" + "|".join(_STATUS_TOKENS) + r")`?\s*\)"
    r"|`(?:" + "|".join(_STATUS_TOKENS) + r")`"
    r"|\b(?:" + "|".join(_STATUS_TOKENS) + r")\b")


# LaTeX the model writes out of code-execution habit; Discord shows it raw
# ("60,000 shares $\times$ $169.43", 2026-10-26 audit A32).
_LATEX = ((re.compile(r"\$?\\times\$?"), "x"), (re.compile(r"\$?\\approx\$?"), "~"),
          (re.compile(r"\$?\\pm\$?"), "±"), (re.compile(r"\$?\\cdot\$?"), "x"),
          (re.compile(r"\\%"), "%"))


def strip(answer: str) -> str:
    text = _LABEL_RE.sub("", answer or "")
    for pat, plain in _LATEX:
        text = pat.sub(plain, text)
    text = _STATUS_RE.sub("", text)
    text = re.sub(r"[ \t]+([.,;:!?])", r"\1", text)
    return re.sub(r"[ \t]{2,}", " ", text)

"""Claims about a member's trading, checked against the ledger.

2026-09-18: the bot told the room bankerkyle's "entire existence is
posting unhinged property arbitrage fantasies between blowing up
accounts on weekly options" on a day his caller log carried QQQ closes
at +234% and +208%. 2026-09-19: "holding MSTR puts into a freight
train, sunny", where the MSTR puts were BK's and Monsoon's.

The review of the first implementation is pinned here too: a sentence
naming nobody must not pick up the whole chat window, bare idioms are
not P&L claims, and the ledger reads stay off the event loop.
"""
import inspect

from discord_bot import bot as B
from discord_bot import pnl_claims as P

BK, SUNNY, MONS = 423994649317736448, 318466418301730816, 309959204100374529
MEMBERS = {"BK": BK, "bankerkyle": BK, "sunny": SUNNY,
           "mic mf SUNNY": SUNNY, "Monsoon": MONS}
WINNER = {"wins": 17, "losses": 3, "tickers": {"QQQ", "MSTR"}}
LOSER = {"wins": 0, "losses": 13, "tickers": {"QQQ"}}
THIN = {"wins": 1, "losses": 0, "tickers": {"QQQ"}}
SUNNY_LEDGER = {"wins": 4, "losses": 2, "tickers": {"PLTR", "SPY"}}

LOSS_ANSWER = ("crushing it? the man's entire existence is posting unhinged property "
               "arbitrage fantasies between blowing up accounts on weekly options.")
MSTR_ANSWER = ("same day you started convincing yourself that a 14% gain on an index "
               "scalp makes up for holding MSTR puts into a freight train, sunny.")


def _judge(answer, stats, subjects=()):
    cands = P.claim_candidates(answer, MEMBERS, subject_surfaces=subjects)
    return P.judge_candidates(cands, stats)


# --- the two real incidents ------------------------------------------
def test_the_shipped_blowing_up_accounts_claim_is_caught():
    bad_loss, _ = _judge(LOSS_ANSWER, {BK: WINNER}, subjects=["BK"])
    assert len(bad_loss) == 1
    assert bad_loss[0]["surface"] == "BK" and bad_loss[0]["wins"] == 17


def test_the_shipped_mstr_misattribution_is_caught():
    _, bad_pos = _judge(MSTR_ANSWER, {SUNNY: SUNNY_LEDGER})
    assert len(bad_pos) == 1
    assert bad_pos[0]["surface"] == "sunny" and bad_pos[0]["ticker"] == "MSTR"


# --- the claim has to be false ----------------------------------------
def test_a_losing_member_keeps_the_claim():
    assert _judge(LOSS_ANSWER, {BK: LOSER}, subjects=["BK"])[0] == []


def test_a_thin_record_is_not_enough_to_contradict():
    assert _judge(LOSS_ANSWER, {BK: THIN}, subjects=["BK"])[0] == []


def test_a_member_with_no_ledger_is_skipped():
    assert _judge(LOSS_ANSWER, {}, subjects=["BK"]) == ([], [])
    empty = {"wins": 0, "losses": 0, "tickers": set()}
    assert _judge(MSTR_ANSWER, {SUNNY: empty})[1] == []


def test_a_position_the_member_holds_is_left_alone():
    _, bad = _judge("BK is still holding MSTR puts into next week.", {BK: WINNER})
    assert bad == []


# --- scope: a sentence naming nobody is not about everybody -----------
def test_a_general_market_sentence_flags_nobody():
    """Regression: the first draft fell back to the chat window, so this
    flagged every member the room had mentioned."""
    ans = "Everyone chasing the squeeze in NVDA calls got hosed on the close."
    stats = {BK: WINNER, SUNNY: SUNNY_LEDGER, MONS: WINNER}
    assert _judge(ans, stats) == ([], [])


def test_the_subject_fallback_is_bounded():
    cands = P.claim_candidates(
        "somebody in here blew up another account today.", MEMBERS,
        subject_surfaces=["BK", "sunny", "Monsoon", "bankerkyle"])
    assert len({c["user_id"] for c in cands}) <= P.MAX_SUBJECTS_PER_SENTENCE


# --- register: idioms are not P&L claims ------------------------------
def test_personal_and_bullish_idioms_are_not_loss_claims():
    """The owner's whole point is that personal jabs are the good
    material. A guard that eats them is worse than the bug."""
    for ok in ("he's down bad for that girl he met at the conference.",
               "his phone is blowing up about it.",
               "MSTR is blowing up today and he missed it.",
               "BK full-ports 0DTE lottery tickets thirty minutes before close.",
               "BK is a degenerate weekly options gambler and proud of it."):
        assert P.loss_claim_sentences(ok) == [], ok


def test_the_real_loss_family_still_matches():
    for bad in ("BK is holding his bags into expiry.",
                "bankerkyle blew up another account.",
                "BK's log looks like a crime scene.",
                "BK is down bad on MSTR.",
                "BK never closes green."):
        assert P.loss_claim_sentences(bad), bad


def test_a_market_observation_is_not_a_position_claim():
    """'in NVDA calls' describes the tape, not who holds what."""
    cands = P.claim_candidates("the squeeze in NVDA calls was brutal.",
                               MEMBERS, subject_surfaces=["BK"])
    assert [c for c in cands if c["kind"] == "position"] == []


def test_common_acronyms_are_not_tickers():
    for s in ("sunny is holding ATM puts again.", "BK is long AI calls."):
        cands = P.claim_candidates(s, MEMBERS)
        assert [c for c in cands if c["kind"] == "position"] == [], s


# --- the correction note ----------------------------------------------
def test_the_note_states_receipts_and_forbids_moving_the_position():
    bad_loss, _ = _judge(LOSS_ANSWER, {BK: WINNER}, subjects=["BK"])
    _, bad_pos = _judge(MSTR_ANSWER, {SUNNY: SUNNY_LEDGER})
    note = P.correction_note(bad_loss, bad_pos)
    assert "17 documented win(s)" in note
    assert "no logged MSTR trade" in note
    assert "not move a position onto a different name" in note
    assert P.correction_note([], []) == ""


# --- wiring ------------------------------------------------------------
def test_member_ids_parse_and_drop_ambiguous_first_tokens():
    block = ("- **BK** (bankerkyle, <@423994649317736448>) — x\n"
             "- **mic mf SUNNY** (mic5409, <@318466418301730816>) — y\n"
             "- **mic dropper** (micd, <@999>) — z\n")
    ids = B._profile_member_ids(block)
    assert ids["BK"] == BK and ids["bankerkyle"] == BK
    assert ids["mic mf SUNNY"] == SUNNY
    # two profiles produce "mic"; resolving it would check one member's
    # claim against the other's ledger
    assert "mic" not in ids


def test_detection_does_no_io_and_the_reads_run_off_the_loop():
    src = inspect.getsource(P)
    assert "import db" not in src
    wiring = inspect.getsource(B._ask_05_strip_asker_mockery)
    assert "asyncio.to_thread(" in wiring and "_member_ledger_stats" in wiring
    assert "db.compute_member_points" not in wiring


def test_a_protected_asker_gets_the_claim_dropped_not_kept():
    """Skipping the rewrite for a protected asker published the false
    claim about the very person the rule protects (2026-09-19 review).
    The protected path rewrites subtractively instead."""
    src = inspect.getsource(B._ask_05_strip_asker_mockery)
    assert "pnl-ledger:protected-neutral" in src
    assert "skipped-protected" not in src
    assert "protected=bool(_asker_protected)" in src

    note = P.correction_note(
        [{"surface": "BK", "wins": 17, "losses": 3}], [], protected=True)
    assert "say nothing in its place" in note
    assert "stronger material" not in note, \
        "the protected note must not point at personal material"
    # the ordinary note still does
    assert "stronger material" in P.correction_note(
        [{"surface": "BK", "wins": 17, "losses": 3}], [])


def test_the_rewrite_is_gated_and_rechecked():
    src = inspect.getsource(B._ask_05_strip_asker_mockery)
    assert "_asker_protected" in src
    # the rewrite is the one path out of this phase the fidelity guard
    # would otherwise never see
    assert "_re_fid" in src and "pnl-ledger:rewrite-rejected" in src


# --- the rewrite must not relocate the claim (2026-09-19 review) -------
def test_the_rewrite_grades_names_it_newly_introduced():
    """The note tells the model a position is not X's. Its instinct is
    to hand the position to Y. Y was never a candidate, so grading the
    rewrite against only the original subjects' ledgers waves it
    through: `judge_candidates` skips a user_id absent from stats."""
    moved = "Monsoon is the one holding MSTR puts into that freight train."
    cands = P.claim_candidates(moved, MEMBERS)
    assert [c["user_id"] for c in cands] == [MONS]
    # graded against the ORIGINAL subject's stats only -> invisible
    assert P.judge_candidates(cands, {SUNNY: SUNNY_LEDGER}) == ([], [])
    # graded with the newly-named member's ledger -> caught
    _, bad = P.judge_candidates(cands, {MONS: SUNNY_LEDGER})
    assert len(bad) == 1 and bad[0]["ticker"] == "MSTR"


def test_the_acceptance_path_fetches_those_new_ledgers():
    src = inspect.getsource(B._ask_05_strip_asker_mockery)
    tail = src.split("_ok_rewrite = False", 1)[1]
    assert "_new_ids" in tail, "rewrite acceptance must widen the stats"
    assert "asyncio.to_thread(" in tail, "the extra reads stay off the loop"
    i, j = tail.index("_new_ids"), tail.index("judge_candidates(\n")
    assert i < j, "stats must widen BEFORE the rewrite is judged"

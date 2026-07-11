"""Smoke: profile-capture workflow overhaul (2026-07-11).

Five structural fixes from the profile review (ZHawk's 46 fitness
messages never reached his dossier; 17/54 profiles under the
personal-life floor; quiet users frozen since June; Voice sections
tripping Gemini's input filter):

1. DEEP REBUILD — every profile cycles through a from-scratch 90-day
   pass (30d cycle, or 21d staleness), capped per run, stamped in
   last_full_rebuild_at. Kills the incremental one-way valve.
2. STRATIFIED SAMPLING — minority channels (fitness-yapping etc.) get
   floor representation in the message sample instead of being flooded
   out by stonks chatter.
3. PROFILE LINT — sections present, bullet floors on high-volume users,
   fabricated-quote ratio; one retry with feedback; keep-prior on
   residual hard failure.
4. VOICE SLUR CAP — at most 2 slur-bearing Voice bullets (spec + soft
   lint), so a dossier stops being filter-trip fuel.
5. LENGTH-PARITY EXEMPTION — Recent personal life may grow toward its
   floor during incremental updates.
"""

import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.backfill_user_profiles import (  # noqa: E402
    _lint_profile, _stratified_sample, PROFILE_PROMPT,
)
import scripts.backfill_user_profiles as bp  # noqa: E402


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def _msg(ts, chan, content="x"):
    return {"timestamp": ts, "channel_name": chan, "content": content}


def test_stratified_sample_keeps_minority_channels():
    # 500 stonks messages then 30 fitness messages OLDER than most of
    # the stonks flood — plain recency at cap 200 would keep zero
    # fitness. Stratified keeps the fitness floor.
    fitness = [_msg(f"2026-05-{d:02d}T10:{i:02d}", "fitness-yapping")
               for d in range(1, 4) for i in range(10)]
    stonks = [_msg(f"2026-06-{d:02d}T10:{i:02d}", "stonks-yapping")
              for d in range(1, 26) for i in range(20)]
    msgs = sorted(fitness + stonks, key=lambda m: m["timestamp"])
    sample = _stratified_sample(msgs, 200)
    assert len(sample) <= 200
    n_fit = sum(1 for m in sample if m["channel_name"] == "fitness-yapping")
    assert n_fit >= 30, f"fitness channel must survive the sample: {n_fit}"
    # timestamp-ordered output
    ts = [m["timestamp"] for m in sample]
    assert ts == sorted(ts), "sample must stay timestamp-ordered"
    # under-cap passthrough
    assert _stratified_sample(fitness, 200) == fitness
    # tiny channels (< min) don't earn a floor but recency can include them
    tiny = [_msg("2026-06-30T01:00", "gambling")] * 5
    s2 = _stratified_sample(sorted(stonks + tiny, key=lambda m: m["timestamp"]), 100)
    assert len(s2) <= 100
    _ok("stratified sample: minority floor kept, ordered, capped")


_GOOD_PROFILE = """**ZHawk (.zhawk) — 1065 msgs**

**Personality and style.**
Cynical contrarian, marathon-running sauna monk.

**Voice.**
- "This chat is a casino" — [mantra]
- "ATTACK FELLOW GOONERS" — [rally cry]
- "time to send it" — [pre-race ritual]
- "Get a job little nigga" — [dismissal]

**Retarded takes.**
- "Swiss immigration is the model" — [take]
- "only business owners really win" — [take]

**Recent trades.**
- GEO / 31C — [bag]

**Recent personal life.**
- Ran a 2:55 marathon and mocks everyone's heart rate
- Daily sauna for years, Whoop on wrist
- Orders Semax peptides from a Texas spot
- Quarter-mil Pokemon binder energy
"""


def test_lint_clean_profile_passes():
    hard, soft = _lint_profile(_GOOD_PROFILE, 1065, {"checked_quotes": 5, "unverified_count": 0})
    assert hard == [] and soft == [], (hard, soft)
    _ok("lint: complete profile with color passes clean")


def test_lint_missing_section_is_hard():
    broken = _GOOD_PROFILE.replace("**Recent personal life.**",
                                   "**Recently.**")
    hard, soft = _lint_profile(broken, 1065, {})
    assert any("Recent personal life" in h for h in hard), hard
    _ok("lint: missing section = hard violation")


def test_lint_fabrication_is_hard():
    cc = {"checked_quotes": 6, "unverified_count": 4,
          "unverified_quotes": ["made up quote one"]}
    hard, _ = _lint_profile(_GOOD_PROFILE, 1065, cc)
    assert any("VERBATIM" in h for h in hard), hard
    # low sample doesn't hard-fail (2 of 3 could be edit artifacts)
    hard2, _ = _lint_profile(_GOOD_PROFILE, 1065,
                             {"checked_quotes": 3, "unverified_count": 2})
    assert not any("VERBATIM" in h for h in hard2)
    _ok("lint: >50% fabricated quotes (min 4 checked) = hard")


def test_lint_personal_floor_and_slur_cap_are_soft():
    thin = _GOOD_PROFILE.replace(
        "- Ran a 2:55 marathon and mocks everyone's heart rate\n"
        "- Daily sauna for years, Whoop on wrist\n"
        "- Orders Semax peptides from a Texas spot\n"
        "- Quarter-mil Pokemon binder energy\n",
        "- Lives somewhere\n- Has a job\n",
    )
    hard, soft = _lint_profile(thin, 1065, {})
    assert hard == [], hard
    assert any("Recent personal life" in s for s in soft), soft
    # thin-history users are exempt from the floors
    _, soft_thin = _lint_profile(thin, 120, {})
    assert not any("Recent personal life" in s for s in soft_thin)
    # slur-density: 3+ slur-bearing Voice bullets flags soft
    slurry = _GOOD_PROFILE.replace(
        '- "This chat is a casino" — [mantra]',
        '- "shut up nigga" — [x]',
    ).replace(
        '- "ATTACK FELLOW GOONERS" — [rally cry]',
        '- "nigga what" — [y]',
    )
    _, soft_s = _lint_profile(slurry, 1065, {})
    assert any("slur" in s.lower() for s in soft_s), soft_s
    _ok("lint: personal floor + slur cap soft; thin users exempt")


def test_lint_placeholder_bullets_are_hard():
    # The exact 2026-07-11 corruption: 4 of the first 6 deep rebuilds
    # shipped the spec's bracket-placeholder shape examples as their
    # personal-life section (and one added 'Insufficient signal' on a
    # 2380-msg user).
    corrupted = _GOOD_PROFILE.replace(
        "- Ran a 2:55 marathon and mocks everyone's heart rate",
        "- [domestic-arrangement detail revealed] + [the comedic-pattern "
        "beat the room has noticed]",
    ).replace(
        "- Daily sauna for years, Whoop on wrist",
        "- [job complaint they keep bringing up] + [the framing that "
        "names why it's funny]",
    )
    hard, _ = _lint_profile(corrupted, 2380, {})
    assert any("placeholder" in h for h in hard), hard
    # real bullets using the 'text + [framing]' shape do NOT trip it
    hard2, _ = _lint_profile(_GOOD_PROFILE.replace(
        "- Quarter-mil Pokemon binder energy",
        "- Quarter-mil Pokemon binder + [the room calls it his real "
        "portfolio]",
    ), 1065, {"checked_quotes": 5, "unverified_count": 0})
    assert not any("placeholder" in h for h in hard2), hard2
    # 'Insufficient signal' in a no-bail section at volume = hard
    bail = _GOOD_PROFILE.replace(
        "- Ran a 2:55 marathon and mocks everyone's heart rate\n"
        "- Daily sauna for years, Whoop on wrist\n"
        "- Orders Semax peptides from a Texas spot\n"
        "- Quarter-mil Pokemon binder energy\n",
        "Insufficient signal — too few messages on this dimension.\n",
    )
    hard3, _ = _lint_profile(bail, 2380, {})
    assert any("Insufficient signal" in h for h in hard3), hard3
    # ...but legit for a thin user
    hard4, _ = _lint_profile(bail, 80, {})
    assert not any("Insufficient signal" in h for h in hard4), hard4
    # ...and legit for Recent trades even at volume (some never post)
    trades_bail = _GOOD_PROFILE.replace(
        "- GEO / 31C — [bag]",
        "Insufficient signal — too few messages on this dimension.",
    )
    hard5, _ = _lint_profile(trades_bail, 2380, {})
    assert not any("Insufficient signal" in h for h in hard5), hard5
    _ok("lint: placeholder bullets + bogus insufficient-signal = hard; "
        "real shapes and legit bails pass")


def test_selfheal_and_template_fixed():
    import db as _db
    dsrc = inspect.getsource(_db)
    assert "profile_text LIKE '%] + [%'" in dsrc, \
        "boot self-heal for corrupted profiles missing"
    src = inspect.getsource(bp)
    # the copyable template must be gone from the PROMPT the model sees
    # (a code comment may still cite it as the historical failure)
    assert "[domestic-arrangement detail revealed]" not in PROFILE_PROMPT, \
        "the copyable placeholder template must be gone from the prompt"
    assert "NEVER emit bracket placeholders" in PROFILE_PROMPT
    assert "FICTIONAL user" in PROFILE_PROMPT
    # the fictional example must not itself match the self-heal pattern
    assert "] + [" not in PROFILE_PROMPT, \
        "prompt example must not contain the corruption signature"
    assert "_COLD_SAMPLE_CAP = 1500" in src, "cold-start sample cap missing"
    _ok("self-heal sweep + uncopyable template + cold sample cap")


def test_deep_rebuild_wired():
    src = inspect.getsource(bp)
    for frag in (
        "_DEEP_REBUILD_DAYS = 90",
        "_DEEP_REBUILD_CYCLE_DAYS = 30",
        "_DEEP_REBUILD_STALE_DAYS = 21",
        "_DEEP_REBUILD_PER_RUN",
        "last_full_rebuild_at=(",
        "cold_uids: set[int] = set(rebuild_due)",
    ):
        assert frag in src, f"deep-rebuild wiring missing: {frag}"
    # dormant users absent from the base window still get rebuilt
    assert "if uid in by_user:" in src and "by_user_deep.get(uid) or []" in src
    # cold-start passes existing=None
    assert "None if (force or uid in cold_uids)" in src
    import db as _db
    dsrc = inspect.getsource(_db)
    assert "last_full_rebuild_at" in dsrc and \
        "ALTER TABLE user_profiles ADD COLUMN last_full_rebuild_at" in dsrc
    _ok("deep rebuild: column, cycle constants, dormant path, cold-start")


def test_lint_retry_wired():
    src = inspect.getsource(bp)
    assert "lint_feedback: str = \"\"" in src, "lint_feedback param missing"
    assert "LINT FEEDBACK" in src, "feedback must reach the prompt"
    assert "_lint_profile(\n                            profile, len(msgs), claim_check\n                        )" in src \
        or "_lint_profile(" in src
    win = src.split("the spec finally has", 1)[1][:7000]
    assert "lint_feedback=_fb" in win, "retry must carry the feedback"
    assert "keeping " in win and "prior profile" in win, \
        "hard residual must keep prior"
    assert "lint_hard_fail" in win, "hard fail must log a pipeline event"
    _ok("lint retry: feedback loop + keep-prior + pipeline events")


def test_prompt_anchors():
    assert "Slur-density cap" in PROFILE_PROMPT or \
        "Slur-density cap" in inspect.getsource(bp)
    src = inspect.getsource(bp)
    assert "Recent personal life may GROW" in src, \
        "length-parity exemption missing"
    assert "durable identity dimensions are exempt" in src.lower() or \
        "durable identity dimensions" in src, "durable-identity rule missing"
    _ok("prompt anchors: slur cap + growth exemption + durable identity")


if __name__ == "__main__":
    print("=== profile-capture workflow smoke ===")
    test_stratified_sample_keeps_minority_channels()
    test_lint_clean_profile_passes()
    test_lint_missing_section_is_hard()
    test_lint_fabrication_is_hard()
    test_lint_personal_floor_and_slur_cap_are_soft()
    test_lint_placeholder_bullets_are_hard()
    test_selfheal_and_template_fixed()
    test_deep_rebuild_wired()
    test_lint_retry_wired()
    test_prompt_anchors()
    print("\nALL PROFILE-CAPTURE SMOKE TESTS PASS")

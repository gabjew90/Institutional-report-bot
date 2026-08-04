"""Smoke: Phase B clusters carry per-member bank attribution.

2026-08-04 review, G1: promoted clusters exposed `banks` and `members`
as parallel UNLINKED lists, so the synthesis routine assigned banks to
member stances round-robin (`bank = bank_list[i % len(bank_list)]`) —
knowingly arbitrary. Adjudication then "validated" quotes against those
fabricated pairings, and DRAFT could print "Bank X argues Y" where X
never said Y. In a product whose whole value is faithfully relaying
paid institutional research, fabricated attribution is the worst class
of defect.

The ground truth (string_to_banks) exists at cluster-build time; it
just was never serialized. Clusters must now carry `member_banks`
(member string -> its actual contributing banks) and the routine must
use it instead of the round-robin.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def test_promoted_clusters_carry_member_banks():
    import report.theme_clusterer as tc

    # Deterministic embeddings: two near-identical vectors so both
    # mentions land in one cluster; no network, no genai.
    def fake_embed(client, strings):
        vecs = {}
        for s in strings:
            vecs[s] = [1.0, 0.0] if "capex" in s else [0.0, 1.0]
        return vecs

    class _FakeClient:
        pass

    orig_embed = tc._embed_strings
    orig_client = tc.genai.Client
    orig_label = tc._pick_canonical_label
    tc._embed_strings = fake_embed
    tc.genai.Client = lambda **kw: _FakeClient()
    tc._pick_canonical_label = lambda client, members, contexts: "AI capex"
    try:
        promoted, _ = tc.discover_uncovered_clusters(
            mentions=[
                ("hyperscaler capex to $770bn", "Goldman Sachs", 1),
                ("capex cycle revised up again", "JPMorgan", 2),
                ("capex cycle revised up again", "Deutsche Bank", 3),
            ],
            covered_labels=[],
            min_banks=3,
        )
        assert promoted, "cluster should have promoted"
        c = promoted[0]
        mb = c.get("member_banks")
        assert isinstance(mb, dict) and mb, (
            f"promoted cluster must carry member_banks (member -> its "
            f"actual banks), got {c.keys()}"
        )
        assert mb.get("hyperscaler capex to $770bn") == ["Goldman Sachs"], mb
        assert sorted(mb.get("capex cycle revised up again") or []) == [
            "Deutsche Bank", "JPMorgan"
        ], mb
    finally:
        tc._embed_strings = orig_embed
        tc.genai.Client = orig_client
        tc._pick_canonical_label = orig_label
    _ok("promoted clusters carry ground-truth member->banks pairing")


def test_routine_uses_member_banks_not_round_robin():
    doc = open(
        os.path.join(REPO, "docs", "superpowers", "routines",
                     "synthesis-routine.md"),
        encoding="utf-8",
    ).read()
    assert "member_banks" in doc, (
        "the synthesis routine never reads member_banks — synthetic "
        "stances still get fabricated bank attribution"
    )
    assert "bank_list[i % len(bank_list)]" not in doc, (
        "round-robin bank assignment still present in the routine — "
        "this is fabricated attribution; use the member's actual banks"
    )
    _ok("routine attributes stances from member_banks, round-robin gone")


def test_contract_documents_member_banks():
    doc = open(os.path.join(REPO, "ROUTINE_CONTRACTS.md"),
               encoding="utf-8").read()
    assert "member_banks" in doc, (
        "ROUTINE_CONTRACTS.md must document the member_banks field so "
        "the two sides don't drift"
    )
    _ok("contract documents the member_banks field")


if __name__ == "__main__":
    print("=== cluster member-bank attribution smoke ===")
    test_promoted_clusters_carry_member_banks()
    test_routine_uses_member_banks_not_round_robin()
    test_contract_documents_member_banks()
    print("\nALL CLUSTER MEMBER-BANK SMOKE TESTS PASS")

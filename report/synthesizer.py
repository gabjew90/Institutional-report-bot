"""Cross-PDF synthesis using Gemini to generate Market Pulse reports."""

import json
import logging
import re
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone

from google import genai
from google.genai import types

from ai_analysis.models import PdfAnalysis
from ai_analysis.prompts import (
    DAILY_SYNTHESIS_SYSTEM, DAILY_SYNTHESIS_USER,
    DRAFT_SYSTEM, DRAFT_USER,
    AUDIT_SYSTEM, AUDIT_USER,
)
from report.market_data import fetch_market_snapshot
from report.news_data import (
    fetch_news_snapshot, fetch_earnings_calendar, fetch_economic_calendar,
)
from report.models import DailyReport
from report.theme_clusterer import cluster_themes, discover_uncovered_clusters
from config import settings
import db

log = logging.getLogger(__name__)


def _get_client() -> genai.Client:
    return genai.Client(api_key=settings.google_api_key)


def _fmt_et(utc_iso: str) -> str:
    """Convert a UTC ISO timestamp to ET display like '2026-04-18 09:00 EDT'."""
    if not utc_iso:
        return ""
    try:
        import pytz
        clean = utc_iso.replace("T", " ")[:19]
        dt = datetime.fromisoformat(clean).replace(tzinfo=pytz.UTC)
        return dt.astimezone(pytz.timezone("America/New_York")).strftime("%Y-%m-%d %H:%M %Z")
    except (ValueError, TypeError):
        return utc_iso[:16].replace("T", " ")


def _normalize_theme_tag(tag: str) -> str:
    """Canonicalize a theme tag for cross-PDF aggregation.

    Lowercase, strip punctuation/articles, collapse whitespace. So
    'AI hyperscaler capex super-cycle' and 'ai hyperscaler capex super cycle'
    cluster as the same theme. Pure-substring fuzzy matching at this layer;
    near-duplicates further merged in _merge_similar_tags() below.
    """
    import re
    if not tag:
        return ""
    t = tag.lower().strip()
    # Drop leading articles
    for art in ("the ", "a ", "an "):
        if t.startswith(art):
            t = t[len(art):]
    # Replace dashes/slashes with spaces, collapse whitespace
    t = re.sub(r"[/\-_]", " ", t)
    t = re.sub(r"[^\w\s]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _classify_themes(
    analyses: list[PdfAnalysis],
    discovery_audit: dict | None = None,
    theme_normalization: dict | None = None,
) -> dict[str, dict]:
    """Aggregate organic theme stances extracted at deep-analysis time.

    Each analysis carries 1-3 `theme_stances` (theme + stance + conviction +
    key_argument) extracted by Gemini from the document's actual content. We
    aggregate across all PDFs in the window and use semantic embedding-based
    clustering (`report.theme_clusterer`) to merge near-duplicate themes
    across banks — replacing the prior anchor-list + substring-match merge.

    Three-stage classifier:
        Phase A: cluster theme_stances (per-PDF primary themes) into
            cross-bank theme groups via embedding similarity.
        Phase B: cluster contextual_mentions (per-PDF secondary mentions
            in risk_factors, geopolitical, macro_indicators, etc.) and
            promote any cluster that's NOT semantically covered by a
            Phase-A theme AND spans ≥3 distinct banks. This catches
            distributed-mention topics (e.g., "US strikes on Iranian
            targets" mentioned in many PDFs as context but never tagged
            as a primary theme by any single bank).
        Two-tier merge: Phase B topic clusters that are semantically
            covered by ≥2 Phase A labels (their centroid sits above the
            merge threshold with each) collapse those Phase A labels into
            one canonical theme. Phase A often fragments a single subject
            into N stance-labeled themes when banks phrase their views
            differently ("hormuz peace deal" vs "iran risk premium" vs
            "middle east supply shock"); Phase B's broader topic clustering
            bridges them. The Phase B cluster's mentioning banks also flow
            into the merged theme's neutral bucket — banks that engaged
            with the subject without taking a primary stance still count
            toward its bank coverage.

    Returns {theme_name: {
        "banks": int, "pdfs": int, "sources": list[str],
        "supportive": int, "skeptical": int, "neutral": int,
        "discovered": bool,  # True for Phase-B promoted themes
    }}.

    Stance counts are BANK-DEDUPLICATED. A bank with N supportive PDFs
    on the same theme contributes 1 to `supportive`, not N. This prevents
    one bank's house view (often re-articulated across multiple desk
    notes) from inflating the cross-bank consensus signal. A bank that
    has both supportive and skeptical PDFs on the same theme counts in
    BOTH buckets — internal division is itself a signal worth surfacing.

    Backward-compat: PDFs whose deep analysis predates this schema will have
    theme_stances populated from legacy theme_tags during deserialization
    (stance defaults to neutral for those). PDFs without contextual_mentions
    contribute zero to Phase B (empty list default).

    Args:
        analyses: window of per-PDF analyses.
        discovery_audit: optional dict the caller provides; mutated to
            carry Phase-B audit data (promoted clusters + near-misses)
            for downstream archiving / QC review. Pass None to skip.
    """
    # PDF-level dedup. The 2026-06-01 corpus had 4 exact-duplicate
    # title+source pairs out of 62 PDFs (~6%): The Market Ear's "6 tech
    # charts" and "Ford flying materials" each appeared twice (Dropbox
    # folder + email-forwarded copy), and "GS TMT SPEC SALES" /
    # "DB Early Morning Reid" each had a duplicate row tagged with
    # source='Unknown'. Each duplicate inflated bank/PDF counts in
    # downstream clustering — TME contributes its primary themes twice,
    # padding "ai infrastructure investment" PDF count by 2 and stance
    # counts for whatever stance TME took.
    #
    # Dedup key: lowercased (title, source). Prefer the row with the
    # non-"Unknown" source when there's a tie; fall back to the
    # earlier-id row otherwise. Logged so the downstream stages can
    # surface "had 62 PDFs in window, deduped to 58" cleanly.
    deduped_analyses: list = []
    seen_keys: dict[tuple[str, str], int] = {}  # (title_lc, source_lc) -> index
    duplicate_drops: list[dict] = []
    for a in analyses:
        title = (getattr(a, "title", "") or "").strip().lower()
        source = (getattr(a, "source", "") or "").strip().lower()
        key = (title, source)
        # If we've seen this exact key, drop the later row (typically
        # a re-ingestion of the same file).
        if key in seen_keys:
            duplicate_drops.append({
                "kind": "exact-key",
                "title": title[:80],
                "source": source,
                "pdf_file_id": getattr(a, "pdf_file_id", None),
            })
            continue
        # If we have a same-title row with the OTHER side being
        # source='unknown', prefer the named-bank attribution. Walk
        # the small set of titles we've already kept that match this
        # one and decide.
        title_only_match = None
        for (kt, ks), idx in seen_keys.items():
            if kt == title and ks != source and (ks == "unknown" or source == "unknown"):
                title_only_match = (kt, ks, idx)
                break
        if title_only_match is not None:
            kt, ks, idx = title_only_match
            if ks == "unknown" and source != "unknown":
                # Replace the kept Unknown row with this attributed one.
                duplicate_drops.append({
                    "kind": "unknown-source-replaced",
                    "title": title[:80],
                    "kept_source": source,
                    "dropped_source": ks,
                })
                deduped_analyses[idx] = a
                del seen_keys[(kt, ks)]
                seen_keys[key] = idx
                continue
            else:
                # Current row is unknown, kept row is attributed — drop current.
                duplicate_drops.append({
                    "kind": "unknown-source-dropped",
                    "title": title[:80],
                    "kept_source": ks,
                    "dropped_source": source,
                })
                continue
        seen_keys[key] = len(deduped_analyses)
        deduped_analyses.append(a)
    if duplicate_drops and discovery_audit is not None:
        discovery_audit["pdf_dedup"] = {
            "raw_pdf_count": len(analyses),
            "deduped_pdf_count": len(deduped_analyses),
            "drops": duplicate_drops,
        }
    if duplicate_drops:
        import logging
        logging.getLogger(__name__).info(
            "PDF dedup: %d -> %d (dropped %d duplicates)",
            len(analyses), len(deduped_analyses), len(duplicate_drops),
        )
    analyses = deduped_analyses

    # Per-tag accumulators
    tag_sources: dict[str, set[str]] = {}                            # tag -> banks discussing it
    tag_pdf_count: dict[str, int] = {}                               # tag -> count of PDFs touching it
    tag_stance_banks: dict[str, dict[str, set[str]]] = {}            # tag -> stance -> banks taking that stance
    # Track the longest key_argument seen for each tag — used as
    # disambiguating context when we embed for clustering. "iran" alone
    # is ambiguous; "iran: oil-supply shock from Hormuz" is not.
    tag_arguments: dict[str, str] = {}

    for a in analyses:
        source = (a.source or "Unknown").strip()
        seen_for_this_pdf: set[str] = set()
        for ts in a.theme_stances or []:
            raw_tag = (ts.theme or "").strip()
            norm = _normalize_theme_tag(raw_tag)
            if not norm or len(norm) < 4:  # filter junk like "ai" or "us"
                continue
            if norm in seen_for_this_pdf:
                continue  # don't double-count within one PDF
            seen_for_this_pdf.add(norm)
            tag_sources.setdefault(norm, set()).add(source)
            tag_pdf_count[norm] = tag_pdf_count.get(norm, 0) + 1
            arg = (ts.key_argument or "").strip()
            if arg and len(arg) > len(tag_arguments.get(norm, "")):
                tag_arguments[norm] = arg
            stance = (ts.stance or "neutral").lower().strip()
            if stance not in ("supportive", "skeptical", "neutral"):
                stance = "neutral"
            # Anti-hallucination: directional stances (supportive/skeptical)
            # only count toward the consensus tally if grounded by evidence
            # OR a non-empty key_argument. Ungrounded directional calls fall
            # back to "neutral" — Gemini doesn't get to swing the consensus
            # without text-anchored support.
            if stance in ("supportive", "skeptical"):
                evidence = (ts.evidence or "").strip()
                if not evidence and not arg:
                    stance = "neutral"
            stance_buckets = tag_stance_banks.setdefault(
                norm, {"supportive": set(), "skeptical": set(), "neutral": set()}
            )
            stance_buckets[stance].add(source)

    # Embedding-based clustering. Tags without a key_argument get an empty
    # context (the tag itself is embedded). Returns:
    #   orig_to_canonical: {tag: cluster_canonical_label}
    #   clusters: list of cluster member lists (audit log)
    clustering_input = {tag: tag_arguments.get(tag, "") for tag in tag_sources}
    orig_to_canonical, _clusters = cluster_themes(clustering_input)

    # Apply mapping. Tags that didn't make it through the embedding step
    # (degenerate inputs) fall back to identity.
    merged_sources: dict[str, set[str]] = {}
    canonical_stance_banks: dict[str, dict[str, set[str]]] = {}
    canonical_pdf_count: dict[str, int] = {}
    for orig_tag, srcs in tag_sources.items():
        canonical_tag = orig_to_canonical.get(orig_tag, orig_tag)
        merged_sources.setdefault(canonical_tag, set()).update(srcs)
        canonical_buckets = canonical_stance_banks.setdefault(
            canonical_tag,
            {"supportive": set(), "skeptical": set(), "neutral": set()},
        )
        for stance, banks in tag_stance_banks.get(orig_tag, {}).items():
            canonical_buckets[stance] |= banks
        # PDF counts: simple sum (no dedup — PDFs are unique).
        canonical_pdf_count[canonical_tag] = (
            canonical_pdf_count.get(canonical_tag, 0)
            + tag_pdf_count.get(orig_tag, 0)
        )

    # Phase B — corpus-level discovery.
    # Collect contextual_mentions tagged with bank + pdf_id. Cluster against
    # the canonical labels emitted by Phase A; any cluster not semantically
    # covered AND spanning ≥3 distinct banks gets promoted to a discovered
    # theme. Stance is `neutral` for all mentioning banks — contextual
    # mentions don't carry a directional view.
    contextual_triples: list[tuple[str, str, int]] = []
    pdfs_with_mentions = 0
    for a in analyses:
        bank = (a.source or "Unknown").strip()
        pdf_id = a.pdf_file_id
        cms = [cm.strip() for cm in (a.contextual_mentions or []) if cm and cm.strip()]
        if cms:
            pdfs_with_mentions += 1
        for cm in cms:
            contextual_triples.append((cm, bank, pdf_id))

    # Always populate the audit with structured state so downstream QC can
    # distinguish "Phase B ran and found nothing" from "Phase B got skipped".
    # Empty `promoted`/`near_miss` lists are unambiguous; the supplementary
    # fields explain WHY they're empty (no contextual_mentions in corpus,
    # no Phase-A themes to cover-check against, etc.).
    if discovery_audit is not None:
        discovery_audit["phase_b_ran"] = False
        discovery_audit["promoted"] = []
        discovery_audit["near_miss"] = []
        discovery_audit["pdfs_in_window"] = len(analyses)
        discovery_audit["pdfs_with_contextual_mentions"] = pdfs_with_mentions
        discovery_audit["total_mentions"] = len(contextual_triples)
        discovery_audit["phase_a_theme_count"] = len(merged_sources)
        discovery_audit["two_tier_merges"] = []
        discovery_audit["two_tier_augment_count"] = 0

    promoted: list[dict] = []
    near_miss: list[dict] = []
    if contextual_triples and merged_sources:
        covered_labels = list(merged_sources.keys())
        promoted, near_miss = discover_uncovered_clusters(
            contextual_triples, covered_labels,
        )
        if discovery_audit is not None:
            discovery_audit["phase_b_ran"] = True
            discovery_audit["promoted"] = promoted
            discovery_audit["near_miss"] = near_miss

    # =====================================================================
    # TWO-TIER MERGE
    # =====================================================================
    # Phase A clusters per-PDF theme_stance LABELS — banks' phrasings of
    # their primary views. The same trade idea ("Strait of Hormuz") gets 5
    # different stance labels across 15 banks ("hormuz peace deal", "iran
    # risk premium", "middle east supply shock", ...). Phase A's pairwise
    # similarity sees them as slightly-below-threshold and keeps them as
    # five 1-bank themes. The synthesizer's downstream "highest bank count
    # MUST appear" ranking then under-prioritizes a 15-bank topic because
    # it looks like five thin ones.
    #
    # Phase B clusters contextual MENTIONS (separate string corpus). Across
    # 200 PDFs, the same topic surfaces in many mention strings and
    # Phase B sees them as one tight cluster. Phase B's centroid sits within
    # threshold of several Phase A labels.
    #
    # This pass uses Phase B as the linking signal: when one Phase B cluster
    # is "covered" by ≥2 Phase A labels (its centroid is above threshold
    # with each), those Phase A labels are sub-aspects of the same subject
    # and merge into one canonical theme. The Phase B cluster's mentioning
    # banks also flow in (they engaged with the topic without taking a
    # primary stance — counted as neutral). When only 1 Phase A label is
    # nearby, just augment its banks; no Phase A merge happens.
    # Cap on two-tier merge group size. Allows up to 4 absorbed Phase A
    # labels per canonical (group size 5 = root + 4 absorbed). Beyond
    # this, additional merges are blocked and the would-be-absorbed
    # themes stay as separate canonicals reaching DRAFT.
    #
    # Why the cap exists: the 2026-05-14T20-01-08Z test fire showed a
    # Phase B centroid bridging 10 distinct Phase A themes into one
    # canonical (`ai driven capex supercycle` absorbed `fed rate cut
    # expectations`, `usd rally`, `inflationary boom`, `agriculture
    # commodity inflation`, etc.) because the 0.72 threshold gave the
    # bridge license to merge things that aren't the same trade. With
    # threshold restored to 0.75 AND this cap as defense in depth, a
    # legitimate merge family (the Hormuz family usually has 3-4 sub-
    # aspects) still merges cleanly; runaway absorption gets blocked
    # and the surviving fragments stay visible as candidates for DRAFT.
    MAX_ABSORBED_PER_CANONICAL = 4

    union_parent: dict[str, str] = {label: label for label in merged_sources}
    # Group size per current root. Updated on every successful union.
    # Used by _union to enforce the cap.
    merge_count: dict[str, int] = {label: 1 for label in merged_sources}
    # Audit: every merge the cap blocked. Surfaces in discovery_audit
    # so the QC can verify the cap is firing on real over-merge attempts
    # vs sitting idle.
    cap_blocked_merges: list[dict] = []

    def _find(label: str) -> str:
        # Iterative union-find with path compression.
        while union_parent[label] != label:
            union_parent[label] = union_parent[union_parent[label]]
            label = union_parent[label]
        return label

    def _union(a: str, b: str) -> bool:
        """Merge two Phase A labels under one root if the resulting group
        size <= MAX_ABSORBED_PER_CANONICAL + 1 (root + up to N absorbed).
        Returns True if merged or already in same group; False if the
        merge was blocked by the cap (and the two stay as separate
        canonicals).
        """
        ra, rb = _find(a), _find(b)
        if ra == rb:
            return True
        combined = merge_count[ra] + merge_count[rb]
        if combined > MAX_ABSORBED_PER_CANONICAL + 1:
            cap_blocked_merges.append({
                "would_merge_a": a,
                "would_merge_b": b,
                "ra_root": ra,
                "rb_root": rb,
                "ra_group_size": merge_count[ra],
                "rb_group_size": merge_count[rb],
                "would_have_been": combined,
                "cap": MAX_ABSORBED_PER_CANONICAL + 1,
            })
            return False
        # Keep the higher-bank-count Phase A canonical as the root — its
        # phrasing is the most-representative label for the merged theme.
        ra_banks = len(merged_sources.get(ra, set()))
        rb_banks = len(merged_sources.get(rb, set()))
        if ra_banks >= rb_banks:
            union_parent[rb] = ra
            merge_count[ra] = combined
        else:
            union_parent[ra] = rb
            merge_count[rb] = combined
        return True

    # Walk covered Phase B clusters; record (banks_to_add, anchor_label) for
    # the augmentation pass after union-find converges.
    topic_augments: list[dict] = []
    for d in near_miss:
        if d.get("reason") != "covered":
            continue
        nearby = d.get("nearby_phase_a") or []
        if not nearby:
            continue
        # Only count Phase B clusters with ≥2 banks as a real topic — a
        # 1-bank Phase B cluster is too thin to anchor a merge decision.
        if d.get("n_banks", 0) < 2:
            continue
        nearby_labels = [lbl for lbl, _sim in nearby if lbl in union_parent]
        if not nearby_labels:
            continue
        # ≥2 nearby Phase A labels → merge them. They're sub-aspects of one
        # subject under the Phase B topic's centroid.
        if len(nearby_labels) >= 2:
            anchor = nearby_labels[0]
            for other in nearby_labels[1:]:
                _union(anchor, other)
        topic_augments.append({
            "nearby_labels": nearby_labels,
            "banks": set(d.get("banks", [])),
            "members": d.get("members", []),
            "phase_b_n_banks": d.get("n_banks", 0),
        })

    # Detect actual merges (Phase A labels whose root != themselves).
    two_tier_merges: list[dict] = []
    pre_merge_labels = list(merged_sources.keys())
    label_to_root: dict[str, str] = {}
    for label in pre_merge_labels:
        root = _find(label)
        label_to_root[label] = root
    # Group originals by their root for audit output
    root_to_members: dict[str, list[str]] = {}
    for orig, root in label_to_root.items():
        root_to_members.setdefault(root, []).append(orig)
    for root, members in root_to_members.items():
        if len(members) > 1:
            two_tier_merges.append({
                "canonical": root,
                "merged_from": sorted(members),
            })

    # Rebuild source/stance/pdf maps under the merged roots.
    if two_tier_merges:
        new_sources: dict[str, set[str]] = {}
        new_stance_banks: dict[str, dict[str, set[str]]] = {}
        new_pdf_count: dict[str, int] = {}
        for label, srcs in merged_sources.items():
            root = label_to_root[label]
            new_sources.setdefault(root, set()).update(srcs)
            stance_buckets = new_stance_banks.setdefault(
                root, {"supportive": set(), "skeptical": set(), "neutral": set()}
            )
            for stance, banks in canonical_stance_banks.get(label, {}).items():
                stance_buckets[stance] |= banks
            new_pdf_count[root] = (
                new_pdf_count.get(root, 0) + canonical_pdf_count.get(label, 0)
            )
        merged_sources = new_sources
        canonical_stance_banks = new_stance_banks
        canonical_pdf_count = new_pdf_count

    # Augment Phase A theme banks with Phase B mention banks. These are
    # banks that mentioned the topic but didn't take a primary stance — they
    # belong in the neutral bucket. After union-find converges, all nearby
    # Phase A labels for a given Phase B cluster share a single root.
    for aug in topic_augments:
        roots = {label_to_root.get(lbl, lbl) for lbl in aug["nearby_labels"]}
        # After union, expect exactly 1 root. Defensive: pick first if not.
        root = next(iter(roots)) if roots else None
        if not root or root not in merged_sources:
            continue
        merged_sources[root].update(aug["banks"])
        stance_buckets = canonical_stance_banks.setdefault(
            root, {"supportive": set(), "skeptical": set(), "neutral": set()}
        )
        stance_buckets["neutral"].update(aug["banks"])

    # Surface the normalization map for downstream consumers (routine
    # adjudication step). The map carries each raw normalized tag through to
    # its FINAL canonical label (post-Phase-A + two-tier merge).
    if theme_normalization is not None:
        norm_to_final: dict[str, str] = {}
        for orig_tag, phase_a_canonical in orig_to_canonical.items():
            final = label_to_root.get(phase_a_canonical, phase_a_canonical)
            norm_to_final[orig_tag] = final
        theme_normalization["norm_to_canonical"] = norm_to_final

    # Sibling-canonical detection. When the cap blocks a merge, the two
    # canonicals (post-find roots) are semantically related — the LLM
    # merger wanted them in one cluster. We don't physically merge (the
    # cap exists for a reason) but we DO carry the relationship downstream
    # so theme_coverage can render the sibling pair as one entry instead
    # of two. Without this, the cap correctly prevents cluster bloat at
    # the data layer but DRAFT ships two near-duplicate INSIGHTS sections
    # (the 2026-05-29 AI capex / AI infrastructure pivot duplicate).
    #
    # Union-find again across cap_blocked_merges to identify connected
    # sibling groups. A canonical's siblings = every OTHER canonical
    # connected to it via one or more cap-blocked merges.
    sibling_parent: dict[str, str] = {}

    def _sf(x: str) -> str:
        sibling_parent.setdefault(x, x)
        while sibling_parent[x] != x:
            sibling_parent[x] = sibling_parent[sibling_parent[x]]
            x = sibling_parent[x]
        return x

    def _su(a: str, b: str) -> None:
        ra, rb = _sf(a), _sf(b)
        if ra != rb:
            sibling_parent[rb] = ra

    for entry in cap_blocked_merges:
        ra = entry.get("ra_root")
        rb = entry.get("rb_root")
        if ra and rb and ra != rb:
            # Map blocked-merge roots to their CURRENT post-union roots
            # in case earlier merges changed them.
            cur_ra = label_to_root.get(ra, ra)
            cur_rb = label_to_root.get(rb, rb)
            if cur_ra != cur_rb:
                _su(cur_ra, cur_rb)
    # Build per-canonical sibling sets (excluding self).
    sibling_groups: dict[str, set[str]] = {}
    for canon in merged_sources.keys():
        if canon in sibling_parent:
            group_root = _sf(canon)
            for other in merged_sources.keys():
                if other == canon:
                    continue
                if other in sibling_parent and _sf(other) == group_root:
                    sibling_groups.setdefault(canon, set()).add(other)

    if discovery_audit is not None:
        discovery_audit["two_tier_merges"] = two_tier_merges
        discovery_audit["two_tier_augment_count"] = len(topic_augments)
        # Cap-blocked merges: each entry is a would-be merge the
        # MAX_ABSORBED_PER_CANONICAL cap prevented. Surfacing so the QC
        # can verify the cap is firing on real over-merge attempts and
        # not sitting idle. Empty list = no merges hit the cap this run.
        discovery_audit["two_tier_cap_blocked"] = cap_blocked_merges
        discovery_audit["two_tier_max_absorbed_per_canonical"] = MAX_ABSORBED_PER_CANONICAL
        # Sibling groups: per-canonical list of related canonicals (cap-
        # blocked siblings). Sourced from cap_blocked_merges. Used by
        # _format_theme_coverage to render related themes together; surfaced
        # here so QC can verify the sibling detection on its own.
        if sibling_groups:
            discovery_audit["sibling_groups"] = {
                k: sorted(v) for k, v in sibling_groups.items()
            }

    # Import here to avoid a circular import risk if voice_rules ever
    # grows synthesizer-side dependencies.
    from ai_analysis.voice_rules import NON_BANK_SOURCES

    theme_map: dict[str, dict] = {
        tag: {
            "banks": len(srcs),
            "pdfs": canonical_pdf_count.get(tag, 0),
            "sources": sorted(srcs),
            "supportive": len(canonical_stance_banks.get(tag, {}).get("supportive", set())),
            "skeptical": len(canonical_stance_banks.get(tag, {}).get("skeptical", set())),
            "neutral": len(canonical_stance_banks.get(tag, {}).get("neutral", set())),
            "discovered": False,
            # True if every source for this theme is a non-bank publication
            # (TME, Bloomberg news wire, Reuters, etc.). Such themes are
            # color/positioning observations, not underwritten analytical
            # views — the writer should NOT lead INSIGHTS with them without
            # multi-bank corroboration. Surfaced in _format_theme_coverage
            # so the prompt's editorial decision is informed.
            "non_bank_only": bool(srcs) and srcs.issubset(NON_BANK_SOURCES),
            # Cap-blocked sibling canonicals — themes the merger wanted to
            # absorb but the cap (MAX_ABSORBED_PER_CANONICAL) blocked. The
            # downstream rendering groups siblings together so DRAFT sees
            # one entry per sibling group instead of N near-duplicate
            # entries. Empty list = standalone theme, no siblings.
            "sibling_canonicals": sorted(sibling_groups.get(tag, set())),
        }
        for tag, srcs in merged_sources.items()
    }

    # Add Phase B clusters. When the canonical label collides with an
    # existing Phase A theme, MERGE Phase B's contextual-mention banks
    # into the Phase A entry rather than creating a "(discovered)"
    # duplicate. Why: adjudication runs on the Phase A entry's
    # `sources` field, and that field was previously built from banks
    # with explicit theme_stances only — banks that contributed via
    # contextual_mentions (Phase B's input) were missing.
    #
    # Concrete failure observed 2026-06-04: adjudicator returned
    # banks_for=["Scotiabank"] for `us_labor_market_strength` (citing
    # JOLTS + hiring evidence Scotiabank PDFs contained), but Scotiabank
    # was NOT in the Phase A theme's sources list (no explicit stance,
    # only contextual mentions promoted via Phase B). The routine's
    # input-source check then discarded the theme as a hallucinated
    # attribution. Same shape on the 2026-06-01 IEA case.
    #
    # The "(discovered)" suffix is reserved for genuinely-new topics
    # Phase B surfaced that Phase A missed entirely — not for
    # reinforcement of an existing canonical.
    for d in promoted:
        label = d["canonical"]
        banks_set = set(d["banks"])
        if label in theme_map:
            existing = theme_map[label]
            merged_banks = set(existing["sources"]) | banks_set
            existing["sources"] = sorted(merged_banks)
            existing["banks"] = len(merged_banks)
            # PDF count: take the max rather than sum — Phase A counts
            # PDFs with stances, Phase B counts PDFs with mentions, and
            # those sets overlap. Sum would double-count; max is a
            # conservative lower bound. (The actual pdf_ids set isn't
            # tracked in Phase A so a precise union isn't available.)
            existing["pdfs"] = max(
                existing.get("pdfs", 0), len(d["pdf_ids"])
            )
            # Neutral count gets the newly-discovered banks added so
            # the stance histogram reflects Phase B's contribution
            # (their mentions are stance=neutral by design).
            existing["neutral"] = existing.get("neutral", 0) + len(
                banks_set - set(existing["sources"][:existing["banks"]])
            )
            # Flag the reinforcement so downstream (theme_coverage
            # rendering, QC) can see this entry got Phase B support.
            existing["reinforced_by_discovery"] = True
        else:
            theme_map[label] = {
                "banks": len(banks_set),
                "pdfs": len(d["pdf_ids"]),
                "sources": sorted(banks_set),
                "supportive": 0,
                "skeptical": 0,
                "neutral": len(banks_set),
                "discovered": True,
            }

    # =====================================================================
    # CONTRARIAN-DIVERGENCE DETECTION
    # =====================================================================
    # The 2026-06-01 corpus had five explicitly contrarian / rotation
    # titles ("Nobody Wants NVDA", "Sell in May", "IPO BOOM = MARKET
    # TOP?", "What To Buy If Not AI? Top Goldman Trader Finds
    # 'Scarcity' Elsewhere", "Speculation Nation", "10 charts that make
    # us go hmmm") but the synthesizer led with "AI infrastructure is
    # the trade and the market" and folded all five into the AI bear-
    # case appendix. The structural bias toward bank-count gives multi-
    # week thematic narratives an automatic moat over fresh single-bank
    # contrarian calls.
    #
    # Detection: scan analyses for titles + insights matching contrarian
    # signal patterns (negation of the lead theme, "if not X" framings,
    # explicit "top?" / "speculation" / "bubble" markers, "sell" /
    # "rotate out" language). When >=3 PDFs match across >=2 distinct
    # banks, mark the contrarian signal as a first-class theme_map
    # entry so it gets surfaced in theme_coverage instead of folded
    # away. DRAFT then sees it as a candidate alongside the consensus
    # theme.
    #
    # Selection criteria:
    #   1. >=3 PDFs match a contrarian-signal regex
    #   2. >=2 distinct bank sources contribute
    #   3. The signal is not already a primary theme (no duplicate
    #      coverage on top of an existing standalone theme)
    # ---------------------------------------------------------------------
    _CONTRARIAN_PATTERNS = [
        # Explicit market-top / froth / speculation / bubble flags
        (r"\b(?:market\s+top|speculation\s+nation|bubble\s+risk|froth(?:y)?\s+market|euphoria|melt[- ]up\s+top)\b", "froth/top"),
        # "Sell in May", "go away", calendar contrarian
        (r"\b(?:sell\s+in\s+may|go\s+away|rotate\s+out|rotation\s+out|de-?risk(?:ing)?\s+the\s+book)\b", "calendar/rotation"),
        # "Nobody wants X" — TME-style contrarian flag
        (r"\bnobody\s+wants\b", "no-bid contrarian"),
        # Explicit "if not AI" / "what to buy if not" rotation framings
        (r"\b(?:if\s+not\s+(?:AI|nvda|tech)|what\s+to\s+buy\s+if\s+not|scarcity\s+elsewhere|away\s+from\s+(?:AI|tech))\b", "rotate-out-of-lead"),
        # "Charts that make us go hmmm" — mixed signals flag
        (r"\b(?:make\s+us\s+go\s+hmm+|paradoxes?\s+stack(?:ing)?|charts?\s+do\s+not\s+add\s+up)\b", "mixed-signals"),
        # Explicit "[stock] TOP?" / "[index] is rolling over"
        (r"(?:\?{1,}|\bTOP\?|\brolling\s+over|peak(?:ed)?\s+(?:already|behind))", "explicit-top-flag"),
        # Bearish contrarian on the lead AI/tech complex
        (r"\b(?:nvda\s+(?:is\s+)?topping|ai\s+(?:capex\s+)?(?:bubble|peak|cycle\s+top|saturation))\b", "lead-theme-bearish"),
    ]
    _contrarian_compiled = [
        (re.compile(p, re.IGNORECASE), label)
        for p, label in _CONTRARIAN_PATTERNS
    ]

    contrarian_matches: list[dict] = []
    for a in analyses:
        title = (getattr(a, "title", "") or "").strip()
        source = (getattr(a, "source", "Unknown") or "").strip()
        # Scan title + each insight string for any contrarian pattern.
        haystack_parts = [title]
        for ins in (getattr(a, "key_insights", []) or []):
            haystack_parts.append(ins or "")
        for tr in (getattr(a, "trade_ideas", []) or []):
            haystack_parts.append(getattr(tr, "thesis", "") or "")
            haystack_parts.append(getattr(tr, "instrument", "") or "")
        haystack = " | ".join(p for p in haystack_parts if p)
        matched_labels: list[str] = []
        for pattern, label in _contrarian_compiled:
            if pattern.search(haystack):
                matched_labels.append(label)
        if matched_labels:
            contrarian_matches.append({
                "pdf_file_id": getattr(a, "pdf_file_id", None),
                "title": title[:120],
                "source": source,
                "labels": sorted(set(matched_labels)),
            })

    if contrarian_matches:
        c_banks = {m["source"] for m in contrarian_matches}
        c_pdfs = {m["pdf_file_id"] for m in contrarian_matches if m["pdf_file_id"]}
        n_banks = len(c_banks)
        n_pdfs = len(c_pdfs) if c_pdfs else len(contrarian_matches)
        # Promote only when corpus signal is strong enough (>=3 PDFs
        # across >=2 banks). One-off contrarian opinions don't earn a
        # slot.
        if n_pdfs >= 3 and n_banks >= 2:
            theme_map["consensus-contrarian / rotate-out-of-lead"] = {
                "banks": n_banks,
                "pdfs": n_pdfs,
                "sources": sorted(c_banks),
                "supportive": 0,
                "skeptical": n_banks,  # by construction these are skeptical of the lead
                "neutral": 0,
                "discovered": False,
                "non_bank_only": c_banks.issubset(NON_BANK_SOURCES),
                "sibling_canonicals": [],
                "contrarian_to_lead": True,
                "contrarian_signal_labels": sorted({
                    lab for m in contrarian_matches for lab in m["labels"]
                }),
                "contrarian_titles": [m["title"] for m in contrarian_matches[:8]],
            }
        if discovery_audit is not None:
            discovery_audit["contrarian_scan"] = {
                "matches": len(contrarian_matches),
                "banks": n_banks,
                "pdfs": n_pdfs,
                "promoted": n_pdfs >= 3 and n_banks >= 2,
                "signal_labels": sorted({
                    lab for m in contrarian_matches for lab in m["labels"]
                }),
            }

    # =====================================================================
    # CLOSE-STYLE ASSIGNMENT
    # =====================================================================
    # Structural fix for the "identical template across themes" QC flag
    # (recurring across 2026-05-28 + 2026-05-29 reviews). Without
    # rotation, every INSIGHTS section ends in the same bull / risk /
    # resolution / trade-idea shape — readers internalize the template
    # by section #4 and start skimming.
    #
    # The shapes are listed in priority order. The DEFAULT shape
    # (bull_risk_resolution) lands on the highest-bank-count themes
    # because forcing-counter-cases is most useful where consensus is
    # strongest. Lower-rank themes rotate through the alternates so
    # DRAFT writes structurally different closes for at least some
    # sections per pulse.
    #
    # Five shapes:
    #   bull_risk_resolution — default; engages counter-cases explicitly.
    #     "Bulls argue X. Skeptics argue Y. The resolution that matters
    #      is Z, and the trade against it is W."
    #   falsifiable_window  — closes with a specific time-bound prediction
    #     that's clearly verifiable. "If <metric> doesn't print <threshold>
    #     by <date>, the thesis is dead. The trade until then is X."
    #   ranked_list         — Hartnett-style: closes with a 3-5 item ordered
    #     list of what to watch next, ranked by signal strength.
    #     "Watch in this order: (1) ..., (2) ..., (3) ..."
    #   single_question     — closes with one sharp falsifiable question
    #     the trader must answer for themselves. "The question for this
    #     trade: does <X> break <Y> first or vice versa?"
    #   asymmetry           — closes by naming the payoff asymmetry
    #     directly without counter-case framing. "Cost of being wrong:
    #     X. Cost of missing this if right: Y. Carry: Z."
    #
    # Assignment: rank themes by bank count; top-2 always get the
    # default (bull_risk_resolution); next 3 rotate through the alternates
    # in a deterministic but pulse-specific order driven by the date.
    # This way the same theme on consecutive days doesn't get the same
    # alternate close (variety holds day-over-day too).
    # ---------------------------------------------------------------------
    if theme_map:
        # Deterministic rotation across pulses — feed the day of year as
        # the seed offset so successive pulses rotate the alternates.
        _today = datetime.now(timezone.utc).timetuple().tm_yday
        _alternate_close_styles = [
            "falsifiable_window",
            "ranked_list",
            "single_question",
            "asymmetry",
        ]
        _ranked_for_close = sorted(
            theme_map.items(),
            key=lambda kv: (-kv[1]["banks"], -kv[1]["pdfs"], kv[0]),
        )
        for idx, (theme, info) in enumerate(_ranked_for_close):
            if info.get("discovered") or info.get("non_bank_only"):
                # Discovered/non-bank themes go to WHAT-TO-WATCH or are
                # commentary; no INSIGHTS close to assign.
                info["close_style"] = None
                continue
            if idx < 2:
                info["close_style"] = "bull_risk_resolution"
            else:
                # Rotate alternates; offset by day-of-year so the rotation
                # phase shifts across pulses.
                rotation_idx = (idx - 2 + _today) % len(_alternate_close_styles)
                info["close_style"] = _alternate_close_styles[rotation_idx]

    # =====================================================================
    # UNDERWEIGHTED-CANDIDATE DETECTION
    # =====================================================================
    # Themes with 3+ banks of multi-tier coverage that rank below the
    # natural top-tier by bank count. Without explicit surfacing these
    # silently drop — the 2026-05-29 QC flagged `fed chair warsh policy`
    # (3 banks: BofA + Citi + Deutsche) and `us debt and deficit` (3
    # banks: BofA Global + BofA Securities + UBS, 6 PDFs) as notable
    # misses on that pulse. The fix isn't a prompt rule "consider these"
    # — it's an explicit data category surfaced in theme_coverage so
    # DRAFT sees them as a distinct bucket from primary/discovered.
    #
    # Selection criteria (cumulative):
    #   1. bank count >= 3
    #   2. PDF count >= 3 OR (banks >= 4 AND distinct tier-1 sources >= 2)
    #   3. NOT already top-6 by bank count (top-6 themes are guaranteed
    #      DRAFT attention; the candidate label flags themes that COULD
    #      be missed)
    #   4. NOT discovered (Phase B already has its own surfacing path)
    #   5. NOT non_bank_only (those are color, not analytical signal)
    # ---------------------------------------------------------------------
    if theme_map:
        ranked_by_banks = sorted(
            theme_map.items(),
            key=lambda kv: (-kv[1]["banks"], -kv[1]["pdfs"], kv[0]),
        )
        top_6 = {t for t, _ in ranked_by_banks[:6]}
        from ai_analysis.voice_rules import TIER_1_BANKS as _T1
        tier_1_norm = {b.lower() for b in _T1}
        # The Tier-1 check uses substring matches to handle "BofA",
        # "Bank of America", "BofA Securities", "BofA Global" all
        # mapping to the same Tier-1 entity. A theme with 2+ DISTINCT
        # Tier-1-mention strings is "multi-tier" enough to surface.
        for tag, info in theme_map.items():
            if tag in top_6:
                continue
            if info.get("discovered") or info.get("non_bank_only"):
                continue
            if info.get("banks", 0) < 3:
                continue
            pdf_count = info.get("pdfs", 0)
            tier_1_hits = sum(
                1 for s in info.get("sources", [])
                if any(t1 in s.lower() for t1 in tier_1_norm)
            )
            if pdf_count >= 3 or (info.get("banks", 0) >= 4 and tier_1_hits >= 2):
                info["underweighted_candidate"] = True
            else:
                info["underweighted_candidate"] = False

    return theme_map


def _detect_ai_capex_power_demand_pairing(
    theme_map: dict[str, dict],
) -> dict | None:
    """Detect when AI capex (primary) and data-center power demand
    (typically Phase B discovered) co-occur in the corpus. The two are
    mechanically linked — every $ of hyperscaler buildout creates a
    corresponding power-demand read-through — and the QC review on
    2026-06-04 flagged that the synthesizer keeps shipping AI capex as
    a primary INSIGHTS slot while dropping the power-demand counterpart
    entirely (ANZ + BofA + RBC + UBS converging on it that day, all
    dropped from INSIGHTS and WATCH).

    Returns a dict with the matched themes + bank counts when both
    sides fire, None otherwise. The caller renders this as a
    'MECHANICAL PAIRINGS DETECTED' steering block in theme_coverage so
    DRAFT sees it as a forcing function.

    Thresholds:
      - AI capex side: primary theme (non-discovered) with banks >= 5
      - Power demand side: ANY entry (primary or discovered) with banks
        >= 3 — Phase B mostly catches it as discovered

    Theme-name matching is loose substring (case-insensitive) on the
    canonical labels Phase A uses. Add to the patterns as new variants
    surface.
    """
    AI_CAPEX_PATTERNS = (
        "ai infrastructure", "ai capex", "ai buildout",
        "ai infrastructure and capex", "hyperscaler", "ai cycle",
        "ai chip", "ai semiconductor",
    )
    POWER_DEMAND_PATTERNS = (
        "power demand", "data center power", "electricity demand",
        "data-center power", "natural gas demand", "ai power",
        "energy demand for ai", "power grid", "utility demand",
        "ai energy",
    )

    def _match(theme_name: str, patterns: tuple[str, ...]) -> bool:
        low = theme_name.lower()
        return any(p in low for p in patterns)

    ai_themes: list[tuple[str, dict]] = []
    power_themes: list[tuple[str, dict]] = []
    for theme, info in theme_map.items():
        if info.get("banks", 0) >= 5 and not info.get("discovered") \
           and _match(theme, AI_CAPEX_PATTERNS):
            ai_themes.append((theme, info))
        if info.get("banks", 0) >= 3 and _match(theme, POWER_DEMAND_PATTERNS):
            power_themes.append((theme, info))

    if not ai_themes or not power_themes:
        return None

    # Pick the strongest from each side (max banks).
    ai_theme = max(ai_themes, key=lambda kv: kv[1].get("banks", 0))
    power_theme = max(power_themes, key=lambda kv: kv[1].get("banks", 0))
    return {
        "ai_theme": ai_theme[0],
        "ai_banks": ai_theme[1].get("banks", 0),
        "ai_sources": ai_theme[1].get("sources", []),
        "power_theme": power_theme[0],
        "power_banks": power_theme[1].get("banks", 0),
        "power_sources": power_theme[1].get("sources", []),
    }


def _format_theme_coverage(theme_map: dict[str, dict]) -> str:
    """Render theme counts as a forcing-function block for the DRAFT prompt.

    Two sections — primary themes (Phase-A theme_stance clusters) and
    discovered themes (Phase-B contextual-mention clusters that no bank
    promoted to a primary stance but ≥3 banks discussed). The writer
    should treat discovered themes carefully: heavy contextual presence
    in the corpus, but no bank made them a thesis.

    Sibling-group folding: cap-blocked sibling pairs (themes the merger
    wanted to combine but the MAX_ABSORBED_PER_CANONICAL cap kept
    separate) render as a single block-entry with a primary line + sub-
    bullet for each sibling. Without this, DRAFT sees two near-duplicate
    theme entries and writes two INSIGHTS sections per pair (the
    2026-05-29 AI capex / AI infrastructure pivot duplicate). With the
    fold, DRAFT sees one theme + its tightly-related sub-aspects and
    writes one section that can thread both.
    """
    primary_lines: list[str] = []
    discovered_lines: list[str] = []
    non_bank_lines: list[str] = []
    underweighted_lines: list[str] = []
    contrarian_lines: list[str] = []

    # Pass 1: identify sibling groups so we can render the highest-bank-count
    # canonical per group as the primary line and the rest as sub-bullets.
    # Each theme is rendered at most once across primary/discovered/non-bank.
    rendered: set[str] = set()
    # Group representative chosen by max banks (tiebreak: alpha).
    sibling_reps: dict[str, str] = {}  # rep_theme -> rep_theme (self)
    sibling_members: dict[str, list[str]] = {}  # rep -> sorted member list
    seen_in_group: set[str] = set()
    for theme, info in theme_map.items():
        sibs = info.get("sibling_canonicals") or []
        if not sibs:
            continue
        if theme in seen_in_group:
            continue
        group = {theme, *sibs}
        # Rep = highest banks, ties broken alphabetically.
        rep = max(group, key=lambda t: (
            theme_map.get(t, {}).get("banks", 0),
            -ord(t[0]) if t else 0,
        ))
        sibling_reps[rep] = rep
        sibling_members[rep] = sorted(group - {rep})
        seen_in_group |= group

    ranked = sorted(
        theme_map.items(),
        key=lambda kv: (-kv[1]["banks"], -kv[1]["pdfs"], kv[0]),
    )

    # Close-style guidance shown alongside themes that have one
    # assigned. Each theme's section in the final pulse should close in
    # the named shape. Default (bull/risk/resolution) is left implicit
    # for backward compatibility; alternates are flagged so DRAFT picks
    # them up. See _CLOSE_STYLE_EXPLAIN below for the prose templates.
    _CLOSE_STYLE_EXPLAIN = {
        "bull_risk_resolution": (
            "default close — explicit bull case / risk case / resolution "
            "+ trade idea"
        ),
        "falsifiable_window": (
            "close with a time-bound falsifiable prediction "
            "('if X doesn't print Y by Z, the thesis is dead; the trade "
            "until then is W')"
        ),
        "ranked_list": (
            "close with a 3-5 item ordered list of what to watch next, "
            "ranked by signal strength ('Watch in this order: (1) ..., "
            "(2) ..., (3) ...')"
        ),
        "single_question": (
            "close with one sharp falsifiable question the trader must "
            "answer for themselves ('Does <X> break <Y> first or "
            "vice versa?')"
        ),
        "asymmetry": (
            "close by naming the payoff asymmetry directly without "
            "counter-case framing ('Cost of being wrong: X. Cost of "
            "missing this if right: Y. Carry: Z.')"
        ),
    }

    def _render_row(theme: str, info: dict, indent: str = "  - ") -> str:
        srcs = info["sources"][:6]
        more = info["banks"] - len(srcs)
        srcs_str = ", ".join(srcs)
        if more > 0:
            srcs_str += f", +{more} more"
        sup = info.get("supportive", 0)
        skp = info.get("skeptical", 0)
        neu = info.get("neutral", 0)
        if sup or skp:
            stance_str = f" — stance: {sup} support / {skp} skeptical / {neu} neutral"
        else:
            stance_str = ""
        # Per-theme close_style guidance. The default shape stays
        # implicit (no annotation) so DRAFT keeps shipping the existing
        # template on it; only alternates get an annotation that names
        # the prescribed close shape.
        close = info.get("close_style")
        if close and close != "bull_risk_resolution":
            close_str = (
                f" — close in: {close} ({_CLOSE_STYLE_EXPLAIN.get(close, '')})"
            )
        else:
            close_str = ""
        return (
            f"{indent}{theme}: {info['banks']} banks / {info['pdfs']} PDFs "
            f"({srcs_str}){stance_str}{close_str}"
        )

    for theme, info in ranked:
        if info["banks"] == 0 or theme in rendered:
            continue
        # If this theme is a sibling-group member but NOT the representative,
        # skip — it'll be rendered as a sub-bullet under its group's rep.
        if theme in seen_in_group and theme not in sibling_reps:
            continue
        rows: list[str] = [_render_row(theme, info)]
        # Sub-bullet siblings (if this is the group rep).
        if theme in sibling_reps:
            for sib in sibling_members.get(theme, []):
                sib_info = theme_map.get(sib)
                if not sib_info or sib_info.get("banks", 0) == 0:
                    continue
                rows.append(
                    "      · tightly related (cap-blocked sibling — fold into "
                    f"the same INSIGHTS section): "
                    + _render_row(sib, sib_info, indent="").lstrip()
                )
                rendered.add(sib)
        rendered.add(theme)
        row_block = "\n".join(rows)
        # Four-bucket categorization based on the PRIMARY theme (the rep).
        # The underweighted_candidate flag is set at theme_map build time
        # (see _classify_themes); themes ranked outside top-6 with 3+
        # banks of multi-tier coverage land here so DRAFT sees them as
        # a distinct "easy to drop, possibly worth surfacing" category
        # instead of mixed in with the primary list where they get
        # ranked away.
        if info.get("contrarian_to_lead"):
            # Append the signal labels + 1-2 sample titles so DRAFT can
            # see WHAT the contrarian voices are saying, not just that
            # they exist. Without this DRAFT might know there's a
            # contrarian theme but not have the specific rotate-out
            # framings to write the section.
            labels = info.get("contrarian_signal_labels") or []
            titles = info.get("contrarian_titles") or []
            extra = []
            if labels:
                extra.append(f"      signal kinds: {', '.join(labels)}")
            for t in titles[:3]:
                extra.append(f"      source title: {t}")
            contrarian_lines.append("\n".join([row_block, *extra]))
        elif info.get("discovered"):
            discovered_lines.append(row_block)
        elif info.get("non_bank_only"):
            non_bank_lines.append(row_block)
        elif info.get("underweighted_candidate"):
            underweighted_lines.append(row_block)
        else:
            primary_lines.append(row_block)

    out: list[str] = [
        "THEME COVERAGE — distinct bank counts across the corpus (use this to anchor INSIGHTS ordering; the highest-count themes MUST appear unless conviction-disqualified):",
    ]
    if primary_lines:
        out.extend(primary_lines)
    else:
        out.append("  (no primary themes — corpus may be unusually narrow today)")
    if discovered_lines:
        out.append("")
        out.append(
            "DISCOVERED THEMES — heavily mentioned in research as context (risk_factors, geopolitical, macro interpretation) but no single bank promoted them to a primary thesis. Treat as live topics worth surfacing, but do NOT claim consensus stance — the banks discussed these without arguing direction:"
        )
        out.extend(discovered_lines)
    if non_bank_lines:
        out.append("")
        out.append(
            "NON-BANK-ONLY THEMES — every source for these themes is a commentary publication (The Market Ear, Bloomberg, Reuters) or unattributed, NOT a bank with an analytical research desk. Useful for vol/positioning/market-color reference only. DO NOT promote these to primary INSIGHTS themes without multi-bank corroboration — they're color, not underwritten analysis:"
        )
        out.extend(non_bank_lines)
    if underweighted_lines:
        out.append("")
        out.append(
            "UNDERWEIGHTED CANDIDATES — Tier-1 or multi-tier 3+ bank coverage outside the natural top-6 by bank count. Easy to miss in DRAFT's top-down selection; surface at least one as a WHAT TO WATCH bullet or thread into the most relevant primary INSIGHTS section when it sharpens the call. Each was a notable miss in the 2026-05-29 QC review when the category didn't exist:"
        )
        out.extend(underweighted_lines)
    if contrarian_lines:
        out.append("")
        out.append(
            "CONTRARIAN / ROTATE-OUT SIGNAL — multi-PDF, multi-bank corpus voices explicitly contradicting or warning against the dominant lead theme (AI / consensus narrative). The 2026-06-01 QC review found five contrarian titles in the corpus (Nobody Wants NVDA, Sell in May, IPO BOOM = MARKET TOP?, What To Buy If Not AI, Speculation Nation) all folded into the AI bear-case appendix. When this category surfaces, do NOT bury it in an appendix — give it a dedicated INSIGHTS slot named in the form of the contrarian call, OR a top-of-WATCH bullet naming the specific rotate-out instrument lean. The bullet stance is intentionally skeptical (no support count); the trade lean should be the corresponding rotation."
        )
        out.append(
            "  ROTATE-OUT CLOSE — VARIETY REQUIRED. 2026-06-03 + 2026-06-04 + 2026-06-05 QC reviews all flagged that the contrarian slot keeps closing with `$RSP over $SPY` as the rotation instrument. A reader who reads the pulse daily for a week will recognize the template. Pick the close-lean instrument from the angle the corpus is actually signaling rather than defaulting to equal-weight:"
        )
        out.append(
            "    (a) DEFENSIVE rotation (when corpus argues quality/yield over growth): $XLU utilities, $XLV health care, $XLP staples, $SPLV/$USMV low-vol, $SPYD/$VYM high-yield"
        )
        out.append(
            "    (b) SMALL-CAP rotation (when corpus argues breadth recovery / size factor): $IWM Russell 2k, $VBR small-cap value, $IJR core small-cap"
        )
        out.append(
            "    (c) VALUE-style rotation (when corpus argues style mean-reversion): $VTV vanguard value, $IWD large-cap value, $RPV pure value"
        )
        out.append(
            "    (d) EQUAL-WEIGHT rotation (when corpus argues concentration risk on top-10 names): $RSP equal-weight S&P, $EWS equal-weight S&P sectors"
        )
        out.append(
            "    (e) INTERNATIONAL rotation (when corpus argues US-vs-RoW mean reversion): $EFA developed-ex-US, $EEM emerging, $VEA developed"
        )
        out.append(
            "    (f) COMMODITY-style rotation (when corpus argues hard-asset reflation): $GLD gold, $XLE energy, $DBC broad commodity"
        )
        out.append(
            "    (g) TAIL-HEDGE pair (when corpus argues positioning extreme — pair this WITH one of a-f as a secondary lean): $VIXY VIX short-term, $SPY put spreads with explicit strikes"
        )
        out.append(
            "  Pick (a)-(f) by which angle the corpus is most explicitly arguing; (g) is a permitted addition, not a standalone close. If your only honest read is `$RSP over $SPY`, that's a (d) close — you MUST pair it with at least one secondary instrument from a different category (e.g., `$RSP over $SPY + long $XLU as the defensive complement` or `$RSP over $SPY + $VIXY as the tail hedge`). A single-instrument equal-weight close is the recurring template; the secondary-instrument requirement breaks it without losing the equal-weight read when that IS the right call."
        )
        out.extend(contrarian_lines)

    # MECHANICAL PAIRINGS — forced-pair steering when two themes share
    # a mechanism the corpus is signaling but DRAFT keeps shipping only
    # one side. 2026-06-04 QC: AI capex was INSIGHTS #2 but data-center
    # power demand (ANZ + BofA + RBC + UBS, all promoted by Phase B)
    # got dropped from INSIGHTS AND WATCH. Power demand is the
    # mechanically-downstream trade ($XLE / $LNG / $VST / $CEG read-
    # throughs) — when both sides surface in the corpus, both must
    # appear in the pulse.
    pairing = _detect_ai_capex_power_demand_pairing(theme_map)
    if pairing:
        ai_srcs = ", ".join((pairing["ai_sources"] or [])[:6])
        pwr_srcs = ", ".join((pairing["power_sources"] or [])[:6])
        out.append("")
        out.append(
            "MECHANICAL PAIRINGS DETECTED — the corpus is surfacing two "
            "themes that are mechanically linked (one is the downstream "
            "trade of the other). When both surface, both MUST appear in "
            "the pulse — primary as INSIGHTS, downstream as at minimum a "
            "WATCH bullet that names the read-through instruments. "
            "Shipping only the upstream side is a recurring coverage miss "
            "(2026-06-04 QC: AI capex shipped as INSIGHTS #2, power "
            "demand dropped entirely despite 4-bank promotion):"
        )
        out.append(
            f"  - AI CAPEX (upstream, {pairing['ai_banks']} banks: "
            f"{ai_srcs}) ↔ DATA-CENTER POWER DEMAND (downstream, "
            f"{pairing['power_banks']} banks: {pwr_srcs}). The capex "
            f"side is the AI buildout spend; the power side is the "
            f"electricity / natural gas / utility / data-center-grid "
            f"demand that AI buildout creates. Required treatment: "
            f"AI capex as primary INSIGHTS slot (default); power "
            f"demand as a top-of-WATCH bullet OR as the second half of "
            f"the AI capex INSIGHTS section's trade lean, naming the "
            f"actual instruments ($XLE oil/gas equity, $LNG natural-gas "
            f"export, $VST/$CEG independent power producers, $XLU "
            f"utility ETF). If you fold power demand into AI capex "
            f"INSIGHTS rather than spinning a separate slot, the trade "
            f"lean MUST include at least one power-side instrument."
        )
    return "\n".join(out)


def _build_ticker_map(analyses: list[PdfAnalysis]) -> dict[str, dict]:
    """Aggregate entities_mentioned across all PDFs into a dedup ticker map.

    Returns {TICKER: {"name": str, "asset_class": str, "mentions": int}}.
    Only entities with a non-empty ticker are included.
    Cashtags are only valid for stock/etf/crypto/index — other asset classes
    are kept in the map for synthesis context but flagged as no_cashtag=True.
    """
    CASHTAG_CLASSES = {"stock", "etf", "crypto", "index"}
    out: dict[str, dict] = {}
    for a in analyses:
        for e in a.entities_mentioned:
            if not e.ticker:
                continue
            ticker = e.ticker.strip().upper()
            if not ticker:
                continue
            if ticker not in out:
                out[ticker] = {
                    "name": e.name,
                    "asset_class": (e.asset_class or "").lower().strip(),
                    "mentions": 1,
                    "no_cashtag": (e.asset_class or "").lower().strip() not in CASHTAG_CLASSES,
                }
            else:
                out[ticker]["mentions"] += 1
    return out


def _compute_stats(analyses: list[PdfAnalysis]) -> dict:
    """Summary stats for the footer: top sources, priority mix, date range."""
    from collections import Counter

    source_counts = Counter(a.source or "Unknown" for a in analyses)
    priority_counts = Counter(a.priority or "unknown" for a in analyses)

    published_dates = [a.published_at[:10] for a in analyses if a.published_at]
    earliest = min(published_dates) if published_dates else None
    latest = max(published_dates) if published_dates else None

    return {
        "pdf_count": len(analyses),
        "top_sources": source_counts.most_common(5),
        "priority_mix": dict(priority_counts),
        "earliest_upload": earliest,
        "latest_upload": latest,
    }


def _analyses_to_json(analyses: list[PdfAnalysis]) -> str:
    """Convert analyses to a compact JSON string for the synthesis prompt."""
    compact = []
    for a in analyses:
        entry = {
            "source": a.source,
            "title": a.title,
            "type": a.report_type,
            "priority": a.priority,
            "published": (a.published_at or "unknown")[:10],  # YYYY-MM-DD only
            "insights": a.key_insights,
        }
        if a.market_movers:
            entry["market_movers"] = [asdict(mm) for mm in a.market_movers]
        if a.sector_views:
            entry["sector_views"] = [asdict(sv) for sv in a.sector_views]
        if a.earnings_insights:
            entry["earnings"] = a.earnings_insights
        if a.macro_indicators:
            entry["macro"] = [asdict(mi) for mi in a.macro_indicators]
        if a.crypto_views:
            entry["crypto"] = a.crypto_views
        if a.trade_ideas:
            entry["trades"] = [asdict(ti) for ti in a.trade_ideas]
        if a.risk_factors:
            entry["risks"] = a.risk_factors
        if a.charts_described:
            entry["charts"] = a.charts_described
        if a.vol_and_positioning:
            entry["vol_positioning"] = a.vol_and_positioning
        if a.geopolitical:
            entry["geopolitical"] = a.geopolitical
        if a.cross_bank_references:
            entry["cross_bank_refs"] = a.cross_bank_references
        if a.entities_mentioned:
            entry["entities"] = [
                {"name": e.name, "ticker": e.ticker, "class": e.asset_class}
                for e in a.entities_mentioned
            ]
        if a.key_data_points:
            entry["data_points"] = [asdict(kdp) for kdp in a.key_data_points]
        if a.tension_points:
            entry["tensions"] = [asdict(tp) for tp in a.tension_points]
        if a.theme_stances:
            entry["theme_stances"] = [asdict(ts) for ts in a.theme_stances]
        compact.append(entry)
    return json.dumps(compact, indent=1)


def build_pulse_context(
    analyses: list[PdfAnalysis],
    use_prev_context: bool = True,
) -> dict:
    """Assemble all the inputs the synthesis prompts need, without calling any LLM.

    Returned dict has every key DRAFT_USER and AUDIT_USER expect plus the
    structured prompt bodies themselves so the routine agent can apply them
    directly. Used by the HTTP API endpoint that feeds the Opus routine.

    The keys map 1:1 to the fields used inside synthesize_daily_pulse below —
    when that function evolves, mirror changes here.
    """
    import pytz
    from config import settings as _settings

    today = date.today().isoformat()
    try:
        tz = pytz.timezone(_settings.timezone)
        now_local = datetime.now(tz)
        day_of_week = now_local.strftime("%A")
        today_label = f"{today} ({day_of_week})"
        now_label = now_local.strftime("%H:%M %Z")
        is_weekend = day_of_week in ("Saturday", "Sunday")
    except Exception:
        today_label = today
        now_label = datetime.utcnow().strftime("%H:%M UTC")
        is_weekend = False

    market_status_note = ""
    if is_weekend:
        market_status_note = (
            "\n\n**MARKET STATUS: US markets are CLOSED TODAY (weekend).** "
            "The live price snapshot below shows LAST CLOSE — Friday's closing levels, "
            "not 'today's move.' Phrase price references as 'as of Friday's close' "
            "or 'heading into Monday.' Crypto trades 24/7 so BTC/ETH commentary is fine, "
            "but weekend volumes are thin."
        )

    analyses_json = _analyses_to_json(analyses)
    market_snapshot = fetch_market_snapshot()
    news_snapshot = fetch_news_snapshot(since_hours=48, limit=15)
    earnings_calendar = fetch_earnings_calendar(days_ahead=7)
    economic_calendar = fetch_economic_calendar(days_ahead=7)

    ticker_map = _build_ticker_map(analyses)
    if ticker_map:
        sorted_tickers = sorted(ticker_map.items(), key=lambda kv: -kv[1]["mentions"])
        cashtag_lines = []
        no_cashtag_lines = []
        for ticker, info in sorted_tickers:
            line = f"  {ticker} — {info['name']} ({info['asset_class']}, {info['mentions']} mentions)"
            if info["no_cashtag"]:
                no_cashtag_lines.append(line)
            else:
                cashtag_lines.append(line)
        ticker_block_parts = ["TICKER LOOKUP — use $TICKER (cashtag format) when referring to these:"]
        if cashtag_lines:
            ticker_block_parts.append("\n".join(cashtag_lines))
        if no_cashtag_lines:
            ticker_block_parts.append("\nDo NOT prefix $ for these (FX / commodity / other — reference by name):")
            ticker_block_parts.append("\n".join(no_cashtag_lines))
        ticker_block = "\n".join(ticker_block_parts)
    else:
        ticker_block = "TICKER LOOKUP: (none extracted — use only tickers that clearly appear in the research text)"

    discovery_audit: dict = {}
    theme_normalization: dict = {}
    theme_map = _classify_themes(
        analyses,
        discovery_audit=discovery_audit,
        theme_normalization=theme_normalization,
    )
    theme_coverage_block = _format_theme_coverage(theme_map)

    # Each pulse is fully standalone now. We no longer compute prev-pulse
    # diff context — the corresponding ctx fields (prev_pulse_block,
    # audit_prev_block) and prompt placeholders ({prev_pulse},
    # {prev_pulse_themes}) have been removed from DRAFT_USER and AUDIT_USER.
    # The use_prev_context parameter is retained for API compatibility but
    # has no effect on output.

    if market_status_note:
        market_snapshot = market_snapshot + market_status_note

    session_status = "closed (weekend)" if is_weekend else (
        "market hours — intraday" if "9:30" <= now_label[:5] < "16:00"
        else "pre-market or after-hours"
    )

    return {
        "today": today,
        "today_label": today_label,
        "now_label": now_label,
        "is_weekend": is_weekend,
        "session_status": session_status,
        "pdf_count": len(analyses),
        "analyses_json": analyses_json,
        "market_snapshot": market_snapshot,
        "news_snapshot": news_snapshot,
        "earnings_calendar": earnings_calendar,
        "economic_calendar": economic_calendar,
        "ticker_block": ticker_block,
        "theme_coverage": theme_coverage_block,
        # Structured form of the same theme aggregation rendered in
        # `theme_coverage`. Routine adjudication step needs this to rank
        # themes, filter per-theme evidence, and emit the stance_counts
        # field (which must match these pre-aggregated counts exactly per
        # the adjudication lint rules).
        "theme_map": theme_map,
        # Phase-B (discovery) audit: clusters that were promoted as
        # discovered themes + clusters that almost surfaced ("near-miss"
        # with reason="covered" or "thin"). Routine's QC step uses this
        # to assess whether discovery thresholds are tuned correctly.
        "discovery_audit": discovery_audit,
        # Phase-A normalization map: NORMALIZED_TAG -> CANONICAL_CLUSTER_LABEL.
        # Required by the routine's adjudication step to correctly match
        # raw `theme_stances.theme` labels to the canonical cluster keys
        # in `theme_map`. Without this the routine's `norm()` helper would
        # only match stances whose normalized tag exactly equals a cluster
        # key — losing 7 of 8 stances on the heaviest theme this run because
        # raw labels had merged into one canonical via embedding clustering.
        "theme_normalization": theme_normalization,
    }


async def synthesize_daily_pulse(
    analyses: list[PdfAnalysis],
    use_prev_context: bool = True,
) -> DailyReport:
    """Generate the Daily Market Pulse via a two-stage pipeline.

    Stage 1 (DRAFT): synthesize narrative from research PDFs only — no live
    data. Focuses on INSIGHTS & ALPHA depth and WHAT TO WATCH research-backed
    events. RECAP left as `[LIVE PRICE RECAP]` placeholder.

    Stage 2 (AUDIT): review the draft against live market snapshot, news,
    economic calendar (RELEASED events), earnings calendar, and current time.
    Rewrite RECAP with live prices + released data + news. Fix tickers, timing,
    session framing. Preserve INSIGHTS & ALPHA and WHAT TO WATCH analytical
    content.

    Args:
        analyses: per-PDF analyses to synthesize.
        use_prev_context: if True, include the last scheduled pulse's theme
            headers as a "don't repeat" directive in Stage 1. Currently False
            for both scheduled and manual pulses (independence preferred).
    """
    import pytz
    from config import settings as _settings

    client = _get_client()
    today = date.today().isoformat()
    # Build day-of-week + current time context so Gemini can distinguish
    # "Tuesday BMO" = today vs "Tuesday BMO" = future this week, and can move
    # already-released events from WHAT TO WATCH to RECAP.
    try:
        tz = pytz.timezone(_settings.timezone)
        now_local = datetime.now(tz)
        day_of_week = now_local.strftime("%A")
        today_label = f"{today} ({day_of_week})"
        now_label = now_local.strftime("%H:%M %Z")
        # Weekend flag: US equity, bond, and futures markets are closed Sat + Sun.
        # Crypto trades 24/7 but also quiet on weekends.
        is_weekend = day_of_week in ("Saturday", "Sunday")
    except Exception:
        today_label = today
        now_label = datetime.utcnow().strftime("%H:%M UTC")
        is_weekend = False

    market_status_note = ""
    if is_weekend:
        market_status_note = (
            "\n\n**MARKET STATUS: US markets are CLOSED TODAY (weekend).** "
            "The live price snapshot below shows LAST CLOSE — Friday's closing levels, "
            "not 'today's move.' Do NOT write sentences like 'SPX is up 2% today' — "
            "it's a weekend, nothing has traded. Phrase price references as "
            "'as of Friday's close' or 'heading into Monday.' RECAP should focus on "
            "what weekend news has done to sentiment and what's set up for Monday's open, "
            "not intraday action. Crypto trades 24/7 so BTC/ETH price commentary is fine, "
            "but weekend crypto volumes are thin — don't over-read short-term moves."
        )

    analyses_json = _analyses_to_json(analyses)
    market_snapshot = fetch_market_snapshot()
    news_snapshot = fetch_news_snapshot(since_hours=48, limit=15)
    earnings_calendar = fetch_earnings_calendar(days_ahead=7)
    economic_calendar = fetch_economic_calendar(days_ahead=7)

    # Build the ticker lookup and render as a prompt section
    ticker_map = _build_ticker_map(analyses)
    if ticker_map:
        # Sort by mentions desc so the most-referenced names are at the top
        sorted_tickers = sorted(ticker_map.items(), key=lambda kv: -kv[1]["mentions"])
        cashtag_lines = []
        no_cashtag_lines = []
        for ticker, info in sorted_tickers:
            line = f"  {ticker} — {info['name']} ({info['asset_class']}, {info['mentions']} mentions)"
            if info["no_cashtag"]:
                no_cashtag_lines.append(line)
            else:
                cashtag_lines.append(line)
        ticker_block_parts = ["TICKER LOOKUP — use $TICKER (cashtag format) when referring to these:"]
        if cashtag_lines:
            ticker_block_parts.append("\n".join(cashtag_lines))
        if no_cashtag_lines:
            ticker_block_parts.append("\nDo NOT prefix $ for these (FX / commodity / other — reference by name):")
            ticker_block_parts.append("\n".join(no_cashtag_lines))
        ticker_block = "\n".join(ticker_block_parts)
    else:
        ticker_block = "TICKER LOOKUP: (none extracted — use only tickers that clearly appear in the research text)"

    # Always compute the previous scheduled pulse's theme list — used as a
    # dedup reference even when DRAFT stage is standalone. Scheduled pulses
    # additionally get the full diff-framing directive in DRAFT.
    prev_themes_list: list[str] = []
    prev_age_hours: int | None = None
    prev = db.get_last_daily_pulse()
    if prev and prev.get("created_at"):
        try:
            prev_ts = datetime.fromisoformat(prev["created_at"][:19])
            age = datetime.utcnow() - prev_ts
            if age <= timedelta(hours=48):
                import re
                md = prev["report_markdown"] or ""
                theme_headers = re.findall(r"\*\*([^*\n]{5,80})\*\*", md)
                prev_themes_list = [t.strip() for t in theme_headers if t.strip()][:12]
                prev_age_hours = int(age.total_seconds() / 3600)
        except (ValueError, TypeError):
            pass

    # DRAFT-stage prev-pulse directive — only when caller opts in (scheduled).
    # Manual /pulse gets the "standalone" message so it doesn't anchor on
    # yesterday's structure.
    if not use_prev_context:
        prev_context = (
            "PREVIOUS PULSE: (this is a standalone manual pulse — no prior-pulse "
            "comparison requested. Treat this as a fresh snapshot of the current "
            "research window. Do NOT anchor on any specific previous structure.)"
        )
    elif not prev_themes_list:
        prev_context = (
            "PREVIOUS PULSE: (none available — this is the first scheduled pulse "
            "or the last one is too stale to compare against.)"
        )
    else:
        prev_context = (
            f"PREVIOUS PULSE SUMMARY (~{prev_age_hours}h ago, {prev['pdf_count']} reports):\n\n"
            f"Themes already covered in yesterday's pulse (DO NOT REPEAT VERBATIM — these are the exact headlines the reader saw yesterday):\n"
            + "\n".join(f"  - {t}" for t in prev_themes_list)
            + "\n\nYour job today:\n"
            + "1. For each theme above, ask: has the research today materially advanced it? If no → SKIP. If yes → lead with 'Since yesterday: [what's new/changed]'.\n"
            + "2. Actively hunt for themes that are NOT in the list above — new catalysts, fresh desk calls, new positioning data.\n"
            + "3. Your pulse should be notably different from yesterday's. If today's pulse would look 80%+ the same as yesterday's, you've failed.\n"
            + "4. Do NOT rewrite yesterday's themes with synonyms and new numbers. That's the same pulse in a trench coat."
        )

    # AUDIT-stage dedup reference — just the theme list, no directive. Passed
    # regardless of use_prev_context so manual pulses also get safety-net dedup.
    if prev_themes_list:
        audit_prev_block = (
            f"PREVIOUS PULSE THEMES (~{prev_age_hours}h ago) — use this list to CUT any theme in the draft that merely restates one of these without a materially new catalyst today:\n"
            + "\n".join(f"  - {t}" for t in prev_themes_list)
        )
    else:
        audit_prev_block = "PREVIOUS PULSE THEMES: (none — no recent prior pulse to dedupe against.)"

    # Append weekend notice to market_snapshot so it's co-located with the prices
    if market_status_note:
        market_snapshot = market_snapshot + market_status_note

    # ==========================================================
    # STAGE 1: DRAFT from research only (no live data)
    # ==========================================================
    # Programmatic theme classifier — count distinct banks per theme bucket
    # so DRAFT prompt can anchor INSIGHTS ordering on actual coverage,
    # not Gemini's gestalt of "what feels dominant."
    theme_map = _classify_themes(analyses)
    theme_coverage_block = _format_theme_coverage(theme_map)

    draft_prompt = DRAFT_USER.format(
        pdf_count=len(analyses),
        today=today_label,
        now=now_label,
        ticker_block=ticker_block,
        prev_pulse=prev_context,
        theme_coverage=theme_coverage_block,
        analyses_json=analyses_json,
    )
    draft_response = await client.aio.models.generate_content(
        model=settings.gemini_model,
        contents=draft_prompt,
        config=types.GenerateContentConfig(
            system_instruction=DRAFT_SYSTEM,
            max_output_tokens=12288,  # room for 1800-2200 word target with 3-6 substantial themes
            temperature=0.4,  # slightly higher for creative narrative
        ),
    )
    draft_markdown = draft_response.text
    stage1_in = draft_response.usage_metadata.prompt_token_count or 0
    stage1_out = draft_response.usage_metadata.candidates_token_count or 0
    log.info(f"Stage 1 (draft): {stage1_in} in / {stage1_out} out")

    # ==========================================================
    # STAGE 2: AUDIT against live data — rewrite RECAP + verify facts
    # ==========================================================
    # Derive a short session_status label for the audit prompt
    session_status = "closed (weekend)" if is_weekend else (
        "market hours — intraday" if "9:30" <= now_label[:5] < "16:00"
        else "pre-market or after-hours"
    )
    audit_prompt = AUDIT_USER.format(
        today=today_label,
        now=now_label,
        session_status=session_status,
        market_snapshot=market_snapshot,
        news_snapshot=news_snapshot,
        earnings_calendar=earnings_calendar,
        economic_calendar=economic_calendar,
        prev_pulse_themes=audit_prev_block,
        draft_markdown=draft_markdown,
    )
    audit_response = await client.aio.models.generate_content(
        model=settings.gemini_model,
        contents=audit_prompt,
        config=types.GenerateContentConfig(
            system_instruction=AUDIT_SYSTEM,
            max_output_tokens=12288,  # AUDIT may rewrite + add themes; needs same headroom as DRAFT
            temperature=0.2,  # lower temp for factual correction
        ),
    )
    markdown = audit_response.text
    stage2_in = audit_response.usage_metadata.prompt_token_count or 0
    stage2_out = audit_response.usage_metadata.candidates_token_count or 0
    log.info(f"Stage 2 (audit): {stage2_in} in / {stage2_out} out")

    input_tokens = stage1_in + stage2_in
    output_tokens = stage1_out + stage2_out

    log.info(
        f"Daily pulse synthesized (two-stage): {len(analyses)} PDFs, "
        f"{input_tokens} in / {output_tokens} out total"
    )

    return DailyReport(
        report_date=today,
        report_type="daily",
        pdf_count=len(analyses),
        markdown_content=markdown,
        raw_json={"analyses_count": len(analyses)},
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        stats=_compute_stats(analyses),
    )

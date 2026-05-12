"""Embedding-based theme clustering for cross-PDF aggregation.

Replaces the prior anchor-list + substring-match merge layer. Embeds
each unique theme string (with key_argument context for disambiguation)
via Gemini, runs greedy agglomerative clustering on cosine similarity,
then asks an LLM to pick a canonical label per cluster.

Why embeddings: anchor lists silently degrade as new geopolitical
flashpoints and market regimes appear, and the substring-match rules
have hard-to-spot false positives (e.g., 'china rate cuts' merging
with 'fed rate cuts china reaction' under the 2-shared-words rule).
Semantic clustering generalizes without per-topic maintenance.

Why greedy agglomerative (not HDBSCAN): at our scale (30-90 strings
per pulse), HDBSCAN with min_cluster_size=2 produces noisy, unstable
clusters. Greedy agglomerative on a cosine threshold is more stable
on small samples and has one tunable parameter (the threshold) instead
of three.

Threshold of 0.78 was the starting point — high enough to keep
'iran nuclear deal' separate from 'iran oil exports' but low enough
to merge 'ai hyperscaler capex super-cycle' with 'hyperscaler capex
boom'. Tune by reviewing the audit log of near-miss clusters once
the system has run for a few pulses.

API failure modes degrade to identity (no merging) rather than wrong
merging — over-fragmentation is recoverable, false-positive merges
silently corrupt the theme map.
"""

import logging
import math
from concurrent.futures import ThreadPoolExecutor, as_completed

from google import genai
from google.genai import types

from config import settings

log = logging.getLogger(__name__)

_EMBED_MODEL = "gemini-embedding-001"
_LABEL_MODEL = "gemini-2.5-flash-lite"
# Cosine similarity above which two theme strings merge into one cluster.
# Lowered 0.78 → 0.75 after the 2026-05-12 pulse: Phase A fragmented a
# 17-bank "Strait of Hormuz" topic into ~8 one-bank micro-themes
# ("hormuz oil supply risk", "refined product crunch", "middle east
# supply shock", "us iran conflict", ...) because each PDF's theme_stance
# label + key_argument embedded just below 0.78 from the others — even
# though they're the same trade. Phase B re-merged it correctly
# (near-miss cluster, n_banks=17, sim 0.89) but the synthesizer anchors
# on the fragmented Phase-A counts, so the theme-coverage bank-ranking
# was noisy for any topic with phrasing diversity. 0.75 is one notch
# down; re-check the next 3 runs' QC to confirm Iran/oil/Trump-Xi-class
# topics consolidate to a single high-count theme without over-merging
# genuinely-distinct themes. If it over-merges, bump back toward 0.78;
# if it still fragments, the real fix is broadening the per-PDF
# theme_stance labels upstream in the analyzer prompt.
_DEFAULT_THRESHOLD = 0.75
_LABEL_PARALLELISM = 8
# Gemini's embed_content rejects oversized batches. Phase A (theme_stances
# clustering) embeds only ~30-90 strings so it never hit this — but Phase B
# (contextual_mentions discovery) can embed 700-900 strings, which silently
# failed: the API rejected the batch, _embed_strings caught the exception,
# returned {}, and Phase B produced empty promoted/near_miss with no signal.
# Chunk into batches of this size and concatenate. 100 is conservative and
# well under any plausible per-request cap.
_EMBED_BATCH_SIZE = 100


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _embed_strings(client: genai.Client, strings: list[str]) -> dict[str, list[float]]:
    """Batch-embed a list of unique strings, chunked to stay under the
    embed_content per-request size cap. Returns {string: vector}.

    A chunk that fails is skipped (logged) rather than failing the whole
    call — partial coverage degrades clustering quality but doesn't kill
    the stage. Returns {} only if EVERY chunk failed or input is empty.
    """
    if not strings:
        return {}
    out: dict[str, list[float]] = {}
    n_chunks = (len(strings) + _EMBED_BATCH_SIZE - 1) // _EMBED_BATCH_SIZE
    failed_chunks = 0
    for i in range(0, len(strings), _EMBED_BATCH_SIZE):
        chunk = strings[i : i + _EMBED_BATCH_SIZE]
        try:
            result = client.models.embed_content(
                model=_EMBED_MODEL,
                contents=chunk,
            )
            # SDK returns a list of ContentEmbedding objects with .values.
            vectors = [getattr(e, "values", None) or list(e) for e in result.embeddings]
            out.update(dict(zip(chunk, vectors)))
        except Exception as e:
            failed_chunks += 1
            log.warning(
                f"Embedding chunk {i // _EMBED_BATCH_SIZE + 1}/{n_chunks} "
                f"({len(chunk)} strings) failed: {e}"
            )
    if failed_chunks == n_chunks:
        log.warning(f"All {n_chunks} embedding chunks failed — identity-clustering fallback")
        return {}
    if failed_chunks:
        log.warning(
            f"{failed_chunks}/{n_chunks} embedding chunks failed; "
            f"clustering with partial coverage ({len(out)}/{len(strings)} embedded)"
        )
    return out


def _greedy_agglomerative(
    embeddings: dict[str, list[float]],
    threshold: float,
) -> list[list[str]]:
    """Greedy agglomerative clustering on cosine similarity.

    Walks strings in input order. Each string joins the existing cluster
    with the highest centroid cosine similarity above threshold, or
    starts a new cluster. Centroid is updated as a running mean — cheap
    and stable for our cluster sizes (typically 1-5 members).
    """
    clusters: list[list[str]] = []
    centroids: list[list[float]] = []

    for s, vec in embeddings.items():
        if not vec:
            clusters.append([s])
            centroids.append(vec)
            continue
        best_idx = -1
        best_sim = threshold
        for i, c in enumerate(centroids):
            if not c:
                continue
            sim = _cosine(vec, c)
            if sim > best_sim:
                best_sim = sim
                best_idx = i
        if best_idx >= 0:
            clusters[best_idx].append(s)
            n = len(clusters[best_idx])
            old = centroids[best_idx]
            centroids[best_idx] = [
                ((n - 1) * o + v) / n for o, v in zip(old, vec)
            ]
        else:
            clusters.append([s])
            centroids.append(list(vec))
    return clusters


def _pick_canonical_label(
    client: genai.Client,
    cluster: list[str],
    contexts: dict[str, str],
) -> str:
    """Ask the LLM to pick a canonical label for a cluster.

    Single-member clusters skip the call. On API error, falls back to
    the longest member tag — more specific than the shortest, less
    arbitrary than alphabetical.
    """
    if len(cluster) == 1:
        return cluster[0]

    members_block = "\n".join(
        f'- "{tag}"'
        + (f"  (argument: {contexts[tag][:140]})" if contexts.get(tag) else "")
        for tag in cluster
    )
    prompt = (
        "Pick a single canonical label for this cluster of theme tags. "
        "Banks tagged the same theme with these variations:\n\n"
        f"{members_block}\n\n"
        "Return ONLY the label — concise (2-6 words), specific, lowercase, "
        "covers all variations. No quotes, no punctuation, no explanation."
    )
    try:
        response = client.models.generate_content(
            model=_LABEL_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.0,
                max_output_tokens=40,
            ),
        )
        label = (response.text or "").strip().strip('"').strip("'").lower()
        # Reject empty, too-short, too-long, or multi-line responses
        if 2 <= len(label) <= 80 and "\n" not in label:
            return label
    except Exception as e:
        log.warning(f"Canonical-label call failed for cluster {cluster[:2]}...: {e}")
    return max(cluster, key=len)


def discover_uncovered_clusters(
    mentions: list[tuple[str, str, int]],
    covered_labels: list[str],
    threshold: float = _DEFAULT_THRESHOLD,
    min_banks: int = 3,
) -> tuple[list[dict], list[dict]]:
    """Cluster contextual mentions and surface ones not covered by existing themes.

    The corpus-level discovery layer. Per-PDF extraction surfaces topics
    contextually (in risk_factors, geopolitical, macro_indicators, etc.)
    that no single bank promoted to a primary theme_stance. Across many
    PDFs, those mentions can cluster into a topic that's heavily discussed
    but invisible to the theme-stance classifier — the "Iran strikes" miss.

    Args:
        mentions: list of (mention_string, bank_name, pdf_file_id) tuples.
            Multiple mentions per PDF are fine; we dedup at the cluster level.
        covered_labels: canonical theme labels already produced by the
            theme_stance clustering pass. Used to skip clusters whose topic
            is already in the theme map.
        threshold: cosine similarity above which two mentions cluster
            together AND above which a new cluster is considered "covered"
            by an existing theme label.
        min_banks: minimum distinct banks for a cluster to be promoted.
            Below this, the cluster is held in the audit log but not
            surfaced to synthesis.

    Returns:
        (promoted, near_miss):
            promoted: list of dicts with shape:
                {
                    "canonical": str,        # LLM-picked label
                    "banks": list[str],      # sorted distinct banks
                    "pdf_ids": list[int],    # distinct pdf ids
                    "members": list[str],    # the mention strings
                    "max_covered_sim": float,# closeness to existing themes
                }
            near_miss: clusters that didn't promote (audit log).
                Same shape plus a "reason" field (e.g., "covered", "thin").

        Both lists are stable for archiving in the QC review.
    """
    if not mentions:
        return [], []

    # Dedup mention strings (one entry per unique string), but track all the
    # banks / pdf_ids that contributed to it. Different PDFs can mention the
    # same exact phrase and we want one embedding for it.
    string_to_banks: dict[str, set[str]] = {}
    string_to_pdf_ids: dict[str, set[int]] = {}
    for mention, bank, pdf_id in mentions:
        m = mention.strip()
        if not m or len(m) < 4:
            continue
        string_to_banks.setdefault(m, set()).add(bank)
        string_to_pdf_ids.setdefault(m, set()).add(int(pdf_id))

    if not string_to_banks:
        return [], []

    try:
        client = genai.Client(api_key=settings.google_api_key)
    except Exception as e:
        log.error(f"genai client init failed for discovery: {e} — skipping")
        return [], []

    unique_strings = list(string_to_banks.keys())
    all_to_embed = unique_strings + list(covered_labels)
    embeddings_by_input = _embed_strings(client, all_to_embed)
    if not embeddings_by_input:
        return [], []

    mention_embeddings = {s: embeddings_by_input[s] for s in unique_strings if s in embeddings_by_input}
    covered_embeddings = [embeddings_by_input[c] for c in covered_labels if c in embeddings_by_input]

    raw_clusters = _greedy_agglomerative(mention_embeddings, threshold)

    # Compute centroid per cluster (mean of member vectors).
    cluster_centroids: list[list[float]] = []
    for c in raw_clusters:
        vectors = [mention_embeddings[m] for m in c if m in mention_embeddings]
        if not vectors:
            cluster_centroids.append([])
            continue
        dim = len(vectors[0])
        centroid = [sum(v[i] for v in vectors) / len(vectors) for i in range(dim)]
        cluster_centroids.append(centroid)

    # Per-cluster decision: covered? thin? promotable?
    decisions: list[dict] = []
    for cluster, centroid in zip(raw_clusters, cluster_centroids):
        banks: set[str] = set()
        pdf_ids: set[int] = set()
        for m in cluster:
            banks |= string_to_banks.get(m, set())
            pdf_ids |= string_to_pdf_ids.get(m, set())
        max_sim = 0.0
        for cv in covered_embeddings:
            if not cv or not centroid:
                continue
            s = _cosine(centroid, cv)
            if s > max_sim:
                max_sim = s
        decisions.append({
            "members": cluster,
            "banks": sorted(banks),
            "pdf_ids": sorted(pdf_ids),
            "max_covered_sim": round(max_sim, 4),
            "n_banks": len(banks),
        })

    # Filter clusters into:
    #   promoted_raw — not covered AND ≥ min_banks distinct banks
    #   near_miss    — the AUDIT-WORTHY rejects (NOT every singleton):
    #     * "covered"     — semantically close to an existing Phase-A theme
    #                       (shows what Phase B WOULD surface if it weren't
    #                        redundant — a tuning signal for the threshold)
    #     * "thin-Nbank"  — N distinct banks where 2 ≤ N < min_banks (genuinely
    #                       close to the promotion bar; the kind of cluster
    #                       that should make us consider lowering min_banks)
    #   dropped silently — 1-bank singletons (too noisy to be a signal)
    # near_miss is capped + sorted (bank count desc, pdf count desc) so the
    # QC review gets the most-interesting rejects, not a 600-entry dump.
    NEAR_MISS_CAP = 25
    promoted_raw: list[dict] = []
    near_miss_pool: list[dict] = []
    for d in decisions:
        if d["max_covered_sim"] >= threshold:
            d["reason"] = "covered"
            near_miss_pool.append(d)
        elif d["n_banks"] < min_banks:
            if d["n_banks"] >= 2:
                d["reason"] = f"thin-{d['n_banks']}bank"
                near_miss_pool.append(d)
            # else: 1-bank singleton — dropped, not a signal
        else:
            promoted_raw.append(d)

    near_miss_pool.sort(
        key=lambda d: (-d["n_banks"], -len(d["pdf_ids"]), -d["max_covered_sim"])
    )
    near_miss = near_miss_pool[:NEAR_MISS_CAP]

    if not promoted_raw:
        return [], near_miss

    # LLM canonical labels for promoted clusters only.
    contexts = {m: m for m in unique_strings}  # strings are already self-contextualized
    labels: list[str] = [""] * len(promoted_raw)
    with ThreadPoolExecutor(max_workers=_LABEL_PARALLELISM) as pool:
        future_to_idx = {
            pool.submit(_pick_canonical_label, client, d["members"], contexts): i
            for i, d in enumerate(promoted_raw)
        }
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                labels[idx] = future.result()
            except Exception as e:
                log.warning(f"Discovery label task {idx} failed: {e}")
                labels[idx] = max(promoted_raw[idx]["members"], key=len)

    promoted = []
    for d, label in zip(promoted_raw, labels):
        promoted.append({
            "canonical": label or max(d["members"], key=len),
            "banks": d["banks"],
            "pdf_ids": d["pdf_ids"],
            "members": d["members"],
            "max_covered_sim": d["max_covered_sim"],
        })

    log.info(
        f"discovery: {len(unique_strings)} mentions → "
        f"{len(raw_clusters)} clusters → {len(promoted)} promoted, "
        f"{len(near_miss)} near-miss"
    )
    return promoted, near_miss


def cluster_themes(
    tags_with_context: dict[str, str],
    threshold: float = _DEFAULT_THRESHOLD,
) -> tuple[dict[str, str], list[list[str]]]:
    """Cluster theme tags by semantic similarity.

    Args:
        tags_with_context: {tag: representative_key_argument}. The
            key_argument provides discriminating context for embedding —
            "iran" alone is ambiguous; "iran: oil-supply shock from
            Hormuz disruption" is unambiguous. Empty-string contexts are
            fine; the tag alone is embedded.
        threshold: cosine similarity above which tags merge into one
            cluster. Higher = stricter (more clusters, less merging).

    Returns:
        (orig_to_canonical, clusters):
            orig_to_canonical: {tag: cluster_canonical_label}.
                Caller uses this to fold stance-bank sets and pdf counts.
            clusters: list of cluster member lists (audit log).

    Failure modes:
        - genai client init fails → identity mapping (no merging)
        - embedding API fails → identity mapping
        - canonical-label LLM fails → falls back to longest member tag
    """
    if not tags_with_context:
        return {}, []

    # Build embedding inputs: tag + arg for disambiguation.
    embed_inputs: dict[str, str] = {}
    for tag, ctx in tags_with_context.items():
        ctx = (ctx or "").strip()
        embed_inputs[tag] = f"{tag}: {ctx[:200]}" if ctx else tag

    try:
        client = genai.Client(api_key=settings.google_api_key)
    except Exception as e:
        log.error(f"genai client init failed: {e} — identity mapping")
        return (
            {tag: tag for tag in tags_with_context},
            [[t] for t in tags_with_context],
        )

    embeddings_by_input = _embed_strings(client, list(embed_inputs.values()))
    if not embeddings_by_input:
        return (
            {tag: tag for tag in tags_with_context},
            [[t] for t in tags_with_context],
        )

    # Map embeddings back to original tag keys.
    input_to_tag = {v: k for k, v in embed_inputs.items()}
    embeddings: dict[str, list[float]] = {}
    for tag in tags_with_context:
        inp = embed_inputs[tag]
        vec = embeddings_by_input.get(inp)
        if vec:
            embeddings[tag] = vec

    raw_clusters = _greedy_agglomerative(embeddings, threshold)

    # Parallel canonical-label calls — each cluster is independent. Cheap
    # at $0.0005 each but adds up serially with 15-25 clusters per pulse.
    labels: list[str] = [""] * len(raw_clusters)
    if raw_clusters:
        with ThreadPoolExecutor(max_workers=_LABEL_PARALLELISM) as pool:
            future_to_idx = {
                pool.submit(_pick_canonical_label, client, c, tags_with_context): i
                for i, c in enumerate(raw_clusters)
            }
            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                try:
                    labels[idx] = future.result()
                except Exception as e:
                    log.warning(f"Label task {idx} failed: {e}")
                    labels[idx] = max(raw_clusters[idx], key=len)

    orig_to_canonical: dict[str, str] = {}
    for cluster, label in zip(raw_clusters, labels):
        for tag in cluster:
            orig_to_canonical[tag] = label or max(cluster, key=len)

    log.info(
        f"theme clustering: {len(tags_with_context)} tags → "
        f"{len(raw_clusters)} clusters (threshold={threshold})"
    )
    return orig_to_canonical, raw_clusters

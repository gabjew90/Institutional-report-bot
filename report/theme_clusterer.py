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
_DEFAULT_THRESHOLD = 0.78
_LABEL_PARALLELISM = 8


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
    """Batch-embed a list of unique strings. Returns {string: vector}.

    On API failure returns empty dict. Caller falls back to identity
    mapping (no merging) — preserves data at the cost of fragmentation.
    """
    if not strings:
        return {}
    try:
        result = client.models.embed_content(
            model=_EMBED_MODEL,
            contents=strings,
        )
        # SDK returns a list of ContentEmbedding objects with .values.
        embeddings = [getattr(e, "values", None) or list(e) for e in result.embeddings]
        return dict(zip(strings, embeddings))
    except Exception as e:
        log.warning(f"Embedding API failed: {e} — identity-clustering fallback")
        return {}


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

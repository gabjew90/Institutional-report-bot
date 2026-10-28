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

import copy
import hashlib
from array import array
import json
import logging
import math
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
from google import genai
from google.genai import types

from config import settings

log = logging.getLogger(__name__)

_EMBED_MODEL = "gemini-embedding-001"
_LABEL_MODEL = "gemini-2.5-flash-lite"
# Cosine similarity above which two theme strings merge into one cluster.
# History:
#   - 0.78 (initial): some QCs flagged Phase A fragmenting high-bank
#     topics into 1-PDF micro-themes ("hormuz peace deal", "iran risk
#     premium" etc. each as separate clusters).
#   - 0.75 (2026-05-13): dropped one notch to merge more aggressively at
#     Phase A.
#   - 0.72 (2026-05-14): dropped another notch trying to fix the
#     X-banks-1-PDF canonical-seed pathology. Made fragmentation worse
#     downstream — the 2026-05-14T20-01-08Z test fire's QC showed the
#     two-tier merge then over-absorbed 10 distinct Phase A themes
#     (`fed rate cut expectations`, `usd rally`, `inflationary boom`,
#     etc.) into `ai driven capex supercycle`. The looser threshold
#     gave the two-tier centroid bridge license to merge things that
#     aren't actually the same trade.
#   - 0.75 (now, reverted): back to the pre-0.72 level. The over-merge
#     surfaced because Phase 1's adjudication input-contract fix
#     removed the upstream filter that was hiding it. With the merge
#     cap (synthesizer.py, ≤4 absorbed per canonical) defending against
#     unbounded centroid bridging, 0.75 is the right level.
#
# If the X-banks-1-PDF canonical-seed pathology recurs after this revert
# (which it will, since this revert doesn't address it), the right fix
# is Phase A canonical-seed selection (prefer multi-PDF members) — held
# for a separate ship after the cross-run reviewer has data to validate
# against.
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

# Caches (2026-10-27). The bridge rebuilds the pulse context every 15
# minutes, and on a backlog day (473 notes, 3,541 mentions) each rebuild
# re-embedded every mention and re-clustered them in pure Python, pinning
# the CPU for most of the interval and starving the Discord thread. The
# embedding of a string never changes, so vectors are kept (bounded, least
# recently used out first), and a clustering call whose input matches the
# previous call returns the previous result without any API call. Vectors
# are kept as array('d'), 24 KB each at 3,072 dimensions. A list of Python
# floats is four times that.
_EMBED_CACHE_MAX = 6000
_embed_cache: "OrderedDict[str, array]" = OrderedDict()
_memo: dict[str, tuple[str, object]] = {}
_cache_lock = threading.Lock()


def _input_key(*parts) -> str:
    return hashlib.sha256(
        json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


def _memo_get(name: str, key: str):
    with _cache_lock:
        hit = _memo.get(name)
    return copy.deepcopy(hit[1]) if hit and hit[0] == key else None


def _memo_put(name: str, key: str, value) -> None:
    with _cache_lock:
        _memo[name] = (key, copy.deepcopy(value))


def clear_caches() -> None:
    """Tests: forget cached vectors and results."""
    with _cache_lock:
        _embed_cache.clear()
        _memo.clear()


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


_EMBED_CHUNK_MAX_RETRIES = 3
_EMBED_CHUNK_RETRY_BASE_DELAY_S = 1.5


def _embed_strings(client: genai.Client, strings: list[str]) -> dict[str, list[float]]:
    """Batch-embed a list of unique strings, chunked to stay under the
    embed_content per-request size cap. Returns {string: vector}.

    Each chunk gets up to _EMBED_CHUNK_MAX_RETRIES attempts with
    exponential backoff. If a chunk still fails after all retries,
    the WHOLE embedding stage raises — silent partial fallback
    historically degraded Phase A clustering on Gemini rate-limit
    events (theme_clusterer.py:117 comment from earlier:
    "chunk fails → logged + skipped; partial coverage degrades
    clustering quality"). The fix is hard-fail: a skipped pulse
    is recoverable; a silently degraded one ships bad data.

    Returns {} only when `strings` is empty.
    """
    if not strings:
        return {}
    cached: dict[str, list[float]] = {}
    with _cache_lock:
        for s in strings:
            v = _embed_cache.get(s)
            if v is not None:
                _embed_cache.move_to_end(s)
                cached[s] = v
    out = _embed_uncached(client, [s for s in dict.fromkeys(strings) if s not in cached])
    with _cache_lock:
        for s, v in out.items():
            if v:
                _embed_cache[s] = array("d", v)
                _embed_cache.move_to_end(s)
        while len(_embed_cache) > _EMBED_CACHE_MAX:
            _embed_cache.popitem(last=False)
    cached.update(out)
    return cached


def _embed_uncached(client: genai.Client, strings: list[str]) -> dict[str, list[float]]:
    if not strings:
        return {}
    out: dict[str, list[float]] = {}
    n_chunks = (len(strings) + _EMBED_BATCH_SIZE - 1) // _EMBED_BATCH_SIZE
    import time
    for i in range(0, len(strings), _EMBED_BATCH_SIZE):
        chunk = strings[i : i + _EMBED_BATCH_SIZE]
        last_err: Exception | None = None
        for attempt in range(1, _EMBED_CHUNK_MAX_RETRIES + 1):
            try:
                result = client.models.embed_content(
                    model=_EMBED_MODEL,
                    contents=chunk,
                )
                # SDK returns a list of ContentEmbedding objects with .values.
                vectors = [
                    getattr(e, "values", None) or list(e)
                    for e in result.embeddings
                ]
                out.update(dict(zip(chunk, vectors)))
                last_err = None
                break  # chunk succeeded, move on
            except Exception as e:
                last_err = e
                if attempt < _EMBED_CHUNK_MAX_RETRIES:
                    delay = _EMBED_CHUNK_RETRY_BASE_DELAY_S * (2 ** (attempt - 1))
                    log.warning(
                        f"Embedding chunk {i // _EMBED_BATCH_SIZE + 1}/{n_chunks} "
                        f"({len(chunk)} strings) failed attempt "
                        f"{attempt}/{_EMBED_CHUNK_MAX_RETRIES}: {e}. "
                        f"Retrying in {delay:.1f}s."
                    )
                    time.sleep(delay)
        if last_err is not None:
            # All retries exhausted. Hard-fail the whole stage rather
            # than silently degrading clustering quality. Caller
            # (synthesizer) should skip the pulse with a recoverable
            # error event rather than ship a fragmented theme_map.
            raise RuntimeError(
                f"Embedding chunk {i // _EMBED_BATCH_SIZE + 1}/{n_chunks} "
                f"({len(chunk)} strings) failed after "
                f"{_EMBED_CHUNK_MAX_RETRIES} attempts. "
                f"Refusing to ship partial-coverage clustering. "
                f"Last error: {last_err}"
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

    Vectorised with numpy (2026-10-27): the same walk, the same running
    mean and the same first-highest tie-break as the pure-Python loop it
    replaces, which took minutes on 3,541 mentions and held the GIL
    throughout. A vector of another length, or of zero norm, never matches
    and starts its own cluster, as `_cosine` returning 0.0 did.
    """
    clusters: list[list[str]] = []
    dim = next((len(v) for v in embeddings.values() if v), 0)
    cap = len(embeddings)
    centroids = np.zeros((cap, dim))
    norms = np.zeros(cap)
    k = 0

    for s, vec in embeddings.items():
        if not vec or len(vec) != dim:
            clusters.append([s])
            k += 1  # its row stays zero-norm: never matched
            continue
        v = np.asarray(vec, dtype=float)
        nv = float(np.sqrt(v @ v))
        best_idx = -1
        if k and nv > 0.0:
            with np.errstate(divide="ignore", invalid="ignore"):
                sims = (centroids[:k] @ v) / (norms[:k] * nv)
            sims[~np.isfinite(sims) | (norms[:k] == 0.0)] = -np.inf
            i = int(np.argmax(sims))
            if sims[i] > threshold:
                best_idx = i
        if best_idx >= 0:
            clusters[best_idx].append(s)
            n = len(clusters[best_idx])
            centroids[best_idx] = ((n - 1) * centroids[best_idx] + v) / n
            c = centroids[best_idx]
            norms[best_idx] = float(np.sqrt(c @ c))
        else:
            clusters.append([s])
            centroids[k] = v
            norms[k] = nv
            k += 1
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


def _centroid_label_sims(
    clusters: list[list[str]],
    embeddings: dict[str, list[float]],
    covered_pairs: list[tuple[str, list[float]]],
) -> list[list[float | None]]:
    """sims[cluster][label]: cosine between the cluster's mean vector and the
    label's vector. None where either is empty (the old loop skipped those),
    0.0 where lengths differ or a norm is zero (as `_cosine` returns)."""
    if not covered_pairs:
        return [[] for _ in clusters]
    dim = next((len(v) for v in embeddings.values() if v), 0)
    n_lab = len(covered_pairs)
    lab = np.zeros((n_lab, dim))
    lab_ok = np.zeros(n_lab, dtype=bool)
    for j, (_label, cv) in enumerate(covered_pairs):
        if cv and len(cv) == dim:
            lab[j] = cv
            lab_ok[j] = True
    lab_empty = [not cv for _label, cv in covered_pairs]
    lab_norm = np.sqrt((lab * lab).sum(axis=1))

    out: list[list[float | None]] = []
    for c in clusters:
        vectors = [embeddings[m] for m in c if m in embeddings]
        if not vectors or not vectors[0]:
            out.append([None] * n_lab)
            continue
        centroid = np.asarray([v for v in vectors if len(v) == dim], dtype=float).mean(axis=0)
        cn = float(np.sqrt(centroid @ centroid))
        dots = lab @ centroid
        row: list[float | None] = []
        for j in range(n_lab):
            if lab_empty[j]:
                row.append(None)
            elif not lab_ok[j] or cn == 0.0 or lab_norm[j] == 0.0:
                row.append(0.0)
            else:
                row.append(float(dots[j] / (cn * lab_norm[j])))
        out.append(row)
    return out


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

    key = _input_key("discover", [list(m) for m in mentions], list(covered_labels),
                     threshold, min_banks)
    hit = _memo_get("discover", key)
    if hit is not None:
        log.info("discovery: input unchanged since the last run, reusing its result")
        return hit

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
    # Keep (label, vector) pairs aligned so we can record WHICH Phase A
    # label(s) a Phase B cluster is close to, not just max similarity.
    # That's the two-tier signal: a Phase B topic cluster covering ≥2
    # Phase A labels means those labels are sub-aspects of the same
    # subject and should be merged in the synthesizer's reconciliation pass.
    covered_pairs: list[tuple[str, list[float]]] = [
        (c, embeddings_by_input[c]) for c in covered_labels if c in embeddings_by_input
    ]

    raw_clusters = _greedy_agglomerative(mention_embeddings, threshold)

    # Centroid per cluster (mean of member vectors) and its cosine against
    # every covered label, as one matrix product (2026-10-27; the per-pair
    # Python loop was 1,425 clusters x 50 labels x 3,072 dimensions).
    sims = _centroid_label_sims(raw_clusters, mention_embeddings, covered_pairs)

    # Per-cluster decision: covered? thin? promotable?
    decisions: list[dict] = []
    for ci, cluster in enumerate(raw_clusters):
        banks: set[str] = set()
        pdf_ids: set[int] = set()
        for m in cluster:
            banks |= string_to_banks.get(m, set())
            pdf_ids |= string_to_pdf_ids.get(m, set())
        max_sim = 0.0
        nearby_phase_a: list[tuple[str, float]] = []
        for li, (label, _cv) in enumerate(covered_pairs):
            s = sims[ci][li]
            if s is None:
                continue
            if s > max_sim:
                max_sim = s
            if s >= threshold:
                nearby_phase_a.append((label, round(s, 4)))
        nearby_phase_a.sort(key=lambda x: -x[1])
        decisions.append({
            "members": cluster,
            "banks": sorted(banks),
            "pdf_ids": sorted(pdf_ids),
            "max_covered_sim": round(max_sim, 4),
            # Phase A labels this cluster sits above the merge threshold with,
            # sorted by similarity desc. Empty if no Phase A label is close.
            # Consumed by synthesizer's two-tier merge pass: when ≥2 Phase A
            # labels are nearby, they collapse into one canonical theme.
            "nearby_phase_a": nearby_phase_a,
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
        _memo_put("discover", key, ([], near_miss))
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
            # Per-member GROUND-TRUTH attribution: which bank(s) actually
            # said each member string. Before this, `banks` and `members`
            # were parallel unlinked lists and the synthesis routine
            # assigned banks to stances round-robin — fabricated
            # attribution that adjudication then validated against
            # itself ("Bank X argues Y" where X never said Y;
            # 2026-08-04 review, G1). string_to_banks had the truth all
            # along; now it ships.
            "member_banks": {
                m: sorted(string_to_banks.get(m, set()))
                for m in d["members"]
            },
            "max_covered_sim": d["max_covered_sim"],
        })

    log.info(
        f"discovery: {len(unique_strings)} mentions → "
        f"{len(raw_clusters)} clusters → {len(promoted)} promoted, "
        f"{len(near_miss)} near-miss"
    )
    _memo_put("discover", key, (promoted, near_miss))
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

    key = _input_key("themes", list(tags_with_context.items()), threshold)
    hit = _memo_get("themes", key)
    if hit is not None:
        log.info("theme clustering: input unchanged since the last run, reusing its result")
        return hit

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
    _memo_put("themes", key, (orig_to_canonical, raw_clusters))
    return orig_to_canonical, raw_clusters

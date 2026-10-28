"""The numpy clustering gives the pure-Python result, and the caches skip
work only when the input is unchanged (2026-10-27)."""
import math
import random

import pytest

from report import theme_clusterer as T


@pytest.fixture(autouse=True)
def _fresh():
    T.clear_caches()
    yield
    T.clear_caches()


def _old_cosine(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return 0.0 if na == 0.0 or nb == 0.0 else dot / (na * nb)


def _old_greedy(embeddings, threshold):
    clusters, centroids = [], []
    for s, vec in embeddings.items():
        if not vec:
            clusters.append([s]); centroids.append(vec); continue
        best_idx, best_sim = -1, threshold
        for i, c in enumerate(centroids):
            if not c:
                continue
            sim = _old_cosine(vec, c)
            if sim > best_sim:
                best_sim, best_idx = sim, i
        if best_idx >= 0:
            clusters[best_idx].append(s)
            n = len(clusters[best_idx])
            centroids[best_idx] = [((n - 1) * o + v) / n for o, v in zip(centroids[best_idx], vec)]
        else:
            clusters.append([s]); centroids.append(list(vec))
    return clusters


def _vectors(n, dim=24, topics=9, seed=7):
    rnd = random.Random(seed)
    centers = [[rnd.gauss(0, 1) for _ in range(dim)] for _ in range(topics)]
    out = {}
    for i in range(n):
        c = centers[rnd.randrange(topics)]
        out[f"s{i}"] = [x + rnd.gauss(0, 0.35) for x in c]
    return out


def test_numpy_clustering_matches_the_python_loop():
    for seed in range(5):
        emb = _vectors(300, seed=seed)
        emb["empty"] = []
        emb["zero"] = [0.0] * 24
        emb["short"] = [1.0, 2.0]
        for th in (0.6, 0.75, 0.9):
            assert T._greedy_agglomerative(emb, th) == _old_greedy(emb, th), (seed, th)


def test_centroid_label_sims_match_the_python_loop():
    emb = _vectors(120)
    clusters = T._greedy_agglomerative(emb, 0.75)
    labels = [("a", emb["s1"]), ("b", []), ("c", emb["s40"]), ("d", [0.0] * 24)]
    sims = T._centroid_label_sims(clusters, emb, labels)
    for ci, c in enumerate(clusters):
        vecs = [emb[m] for m in c]
        centroid = [sum(v[i] for v in vecs) / len(vecs) for i in range(len(vecs[0]))]
        for li, (_l, cv) in enumerate(labels):
            if not cv:
                assert sims[ci][li] is None
            else:
                assert sims[ci][li] == pytest.approx(_old_cosine(centroid, cv), abs=1e-12)


class _Embeddings:
    def __init__(self, table):
        self.table, self.calls = table, []

    def embed_content(self, model, contents):
        self.calls.append(list(contents))
        class R:  # noqa: D401
            pass
        r = R()
        r.embeddings = [type("E", (), {"values": self.table[s]})() for s in contents]
        return r


class _Client:
    def __init__(self, table):
        self.models = _Embeddings(table)


def test_embeddings_are_fetched_once_per_string():
    table = {"a": [1.0, 0.0], "b": [0.0, 1.0], "c": [1.0, 1.0]}
    client = _Client(table)
    def plain(d):
        return {k: list(v) for k, v in d.items()}
    assert plain(T._embed_strings(client, ["a", "b"])) == {"a": [1.0, 0.0], "b": [0.0, 1.0]}
    assert plain(T._embed_strings(client, ["b", "c"])) == {"b": [0.0, 1.0], "c": [1.0, 1.0]}
    assert client.models.calls == [["a", "b"], ["c"]]


def test_unchanged_input_reuses_the_result_and_changed_input_does_not(monkeypatch):
    table = {"fed cuts: a": [1.0, 0.0, 0.0], "rate cuts: b": [0.99, 0.05, 0.0],
             "oil: c": [0.0, 0.0, 1.0]}
    client = _Client(table)
    monkeypatch.setattr(T.genai, "Client", lambda api_key=None: client)
    monkeypatch.setattr(T, "_pick_canonical_label", lambda c, members, ctx: members[0])
    tags = {"fed cuts": "a", "rate cuts": "b"}
    first = T.cluster_themes(tags)
    calls = len(client.models.calls)
    assert T.cluster_themes(dict(tags)) == first
    assert len(client.models.calls) == calls  # no API call on a repeat
    tags["oil"] = "c"
    third = T.cluster_themes(tags)
    assert third[0]["oil"] == "oil" and third[0]["fed cuts"] == third[0]["rate cuts"]

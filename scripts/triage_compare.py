"""Read a triage side-by-side: Claude's labels (scripts/pilot_triage.py
`bundle`) beside the ones Gemini stored for the same notes
(scripts/triage_bundle.py). Prints, per run, how often the two agree, the
3x3 table, how many notes change between analysed and skipped, tokens and
seconds a note, and every pair of runs against each other (a repeat run of
one model is its own noise).

  py -3.12 scripts/triage_compare.py <bundle.jsonl.gz> <results.jsonl> [--dump disagreements.json]

A run whose label ends in "2" is treated as a repeat and left out of the
disagreement dump.
"""
import collections
import gzip
import io
import json
import statistics
import sys

bundle, results = sys.argv[1], sys.argv[2]
notes = {}
for line in gzip.open(bundle, "rt", encoding="utf-8"):
    r = json.loads(line)
    notes[r["id"]] = r
runs = collections.defaultdict(dict)
errs = collections.Counter()
for line in io.open(results, encoding="utf-8"):
    r = json.loads(line)
    if r.get("err") or not r.get("priority"):
        errs[r["run"]] += 1
        continue
    runs[r["run"]][r["id"]] = r

P = ("high", "medium", "low")
print(f"notes in bundle {len(notes)}; Gemini mix:",
      dict(collections.Counter(n["gemini"]["priority"] for n in notes.values())))
print("errors per run:", dict(errs))


def analysed(p):
    return p in ("high", "medium")


def versus(name_a, get_a, name_b, get_b, ids):
    ids = [i for i in ids if get_a(i) and get_b(i)]
    if not ids:
        return
    same = sum(get_a(i) == get_b(i) for i in ids)
    m = collections.Counter((get_a(i), get_b(i)) for i in ids)
    lost = sum(analysed(get_a(i)) and not analysed(get_b(i)) for i in ids)
    gained = sum(not analysed(get_a(i)) and analysed(get_b(i)) for i in ids)
    h_down = m[("high", "medium")]
    h_up = m[("medium", "high")]
    print(f"\n{name_a} vs {name_b}: n={len(ids)}, same {same} ({same / len(ids):.0%})")
    print("   rows = %s, columns = %s" % (name_a, name_b))
    print("            " + "".join(f"{c:>8}" for c in P))
    for a in P:
        print(f"   {a:>8} " + "".join(f"{m[(a, b)]:>8}" for b in P))
    print(f"   analysed by {name_a}, skipped by {name_b}: {lost} ({lost / len(ids):.1%});"
          f" skipped by {name_a}, analysed by {name_b}: {gained} ({gained / len(ids):.1%})")
    print(f"   high->medium {h_down}, medium->high {h_up}")
    na = sum(analysed(get_a(i)) for i in ids)
    nb = sum(analysed(get_b(i)) for i in ids)
    print(f"   notes analysed: {na} -> {nb} ({(nb - na) / max(na, 1):+.0%});"
          f" HIGH: {sum(get_a(i) == 'high' for i in ids)} -> {sum(get_b(i) == 'high' for i in ids)}")


def gem(i):
    return notes[i]["gemini"]["priority"] if i in notes else None


for run, rows in sorted(runs.items()):
    def get(i, rows=rows):
        return rows[i]["priority"] if i in rows else None
    versus("gemini", gem, run, get, list(rows))
    ins = [r["in"] + r["cache_read"] + r["cache_new"] for r in rows.values()]
    fresh = [r["in"] + r["cache_new"] for r in rows.values()]
    outs = [r["out"] for r in rows.values()]
    secs = [r["secs"] for r in rows.values()]
    print(f"   tokens a note: input median {statistics.median(ins):,.0f} (not from cache {statistics.median(fresh):,.0f}),"
          f" output median {statistics.median(outs):,.0f}; total in {sum(ins):,} out {sum(outs):,};"
          f" seconds median {statistics.median(secs)} p90 {sorted(secs)[int(len(secs) * .9)]}")
    rt_same = sum((rows[i].get("report_type") or "") == (notes[i]["gemini"].get("report_type") or "")
                  for i in rows if i in notes)
    print(f"   same report_type as Gemini: {rt_same}/{len(rows)}")

labels = sorted(runs)
for a in labels:
    for b in labels:
        if a < b:
            common = set(runs[a]) & set(runs[b])
            if common:
                versus(a, lambda i, a=a: runs[a][i]["priority"] if i in runs[a] else None,
                       b, lambda i, b=b: runs[b][i]["priority"] if i in runs[b] else None, sorted(common))

if "--dump" in sys.argv:
    out = sys.argv[sys.argv.index("--dump") + 1]
    main_runs = [l for l in labels if not l.endswith("2")]
    dis = []
    for i, n in notes.items():
        got = {l: runs[l][i]["priority"] for l in main_runs if i in runs[l]}
        if len(got) == len(main_runs) and len({n["gemini"]["priority"], *got.values()}) > 1:
            dis.append({"id": i, "file_name": n["file_name"], "folder": n["folder"],
                        "gemini": n["gemini"]["priority"], **got})
    io.open(out, "w", encoding="utf-8").write(json.dumps(dis, indent=1, ensure_ascii=False))
    print(f"\ndisagreements among gemini + {main_runs}: {len(dis)} of {len(notes)} -> {out}")

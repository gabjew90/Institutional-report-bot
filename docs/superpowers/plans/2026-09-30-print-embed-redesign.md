# Print Embed Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The economic print embed (CPI, jobs, PCE, FOMC) renders in the owner's bullet layout with extra series, computed lines, a Quick Takeaway from the print plus the bank research, and a source citation.

**Architecture:** `report/print_watch.py` keeps the data model (`Line`, `ReleaseSpec`), the agency fetches and the job. `Line` gains display/pair/row/optional/table fields, `build_rows` carries them, and a rewritten `render_release` turns rows plus computed lines plus takeaway lines into the body. The ledger stores rows and the FOMC statement so next month's revision line and statement changes have something to compare against. `report/print_takeaway.py` (already present) ranks research tier-1 first and is called from the job in a thread.

**Tech Stack:** Python 3.12, pytest, urllib against BLS/BEA/Fed, google-genai via `ai_analysis.usage_ledger.make_client`.

Spec: `docs/superpowers/specs/2026-09-30-print-embed-redesign-design.md`.

Run tests with `py -3.12 -m pytest -q <file>` from the repo root. Never write files or commit messages through bash heredocs (they corrupt `\b` and `\n` here): use the Write/Edit tools, and `git commit -F <tempfile>` with the message written by the Write tool under `C:\Users\gabje\AppData\Local\Temp\claude\`. Commit messages end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`. Prose in comments: plain English, no em-dashes, no semicolons. Do not push.

---

## File map

- Modify `report/print_watch.py`: `Line` fields, specs, `fetch_bea(table)`, `fetch_observations` grouping, `release_ready`, `build_rows`, `annualized_3m`, `release_title`, `render_release` (rewrite, table removed), `revision_line`, `statement_changes`, `mark_posted`/`previous_post`, `fetch_fomc_today` returns the text, the job wiring.
- Modify `report/print_takeaway.py`: tier-1 ranking, same-period preference, prompt rule.
- Modify `tests/test_print_watch.py`, replace `tests/test_print_watch_format.py`, new tests in `tests/test_print_takeaway.py`.
- Modify `CLAUDE.md` (print_watch row), `NOTES.md`.

---

### Task 1: Line fields, extra series, BEA tables, readiness

**Files:** `report/print_watch.py`, `tests/test_print_watch.py`

- [ ] **Step 1: Failing tests.** Append to `tests/test_print_watch.py`:

```python
def test_specs_carry_the_extra_series_as_optional_lines():
    core = [ln for ln in PW.PCE.lines if not ln.optional]
    extra = [ln for ln in PW.PCE.lines if ln.optional]
    assert [ln.series for ln in core] == ["DPCCRG", "DPCCRG", "DPCERG", "DPCERG"]
    assert {ln.series for ln in extra} == {"DPCERX", "DPCERC", "A065RC", "A072RC"}
    assert {ln.table for ln in extra} == {"T20806", "T20600"}
    assert [ln.pair for ln in core] == ["", "", "headline", "headline"]
    assert {ln.row for ln in extra if ln.row} == {"income"}
    assert {ln.series for ln in PW.CPI.lines if ln.optional} == {"CUSR0000SAH1", "CUSR0000SA0E", "CUSR0000SAF1"}
    assert {ln.series for ln in PW.JOBS.lines if ln.optional} == {"LNS11300000"}
    assert PW.CPI.headline == "CPI Inflation Print" and PW.JOBS.headline == "Jobs Report"
    assert PW.PCE.headline == "PCE Inflation Print" and PW.FOMC.headline == "FOMC Decision"


def test_release_ready_ignores_optional_lines_and_build_rows_drops_them_when_absent():
    obs = PW.parse_bls(BLS)      # the fixture has no shelter/energy/food series
    assert PW.release_ready(PW.CPI, obs, "2026-08")
    rows = PW.build_rows(PW.CPI, obs, "2026-08", FF)
    assert [r["label"] for r in rows] == ["Core CPI m/m", "Core CPI y/y", "CPI m/m", "CPI y/y"]
    assert rows[2]["pair"] == "headline" and rows[0]["display"] == "Core CPI (MoM)"
    assert rows[0]["actual_value"] == 0.3 and rows[0]["consensus_value"] == 0.2


def test_bea_fetch_groups_series_by_table(monkeypatch):
    calls = []

    def fake_get(table, years):
        calls.append(table)
        return {"BEAAPI": {"Results": {"Data": []}}}
    monkeypatch.setattr(PW, "_bea_get", fake_get)
    monkeypatch.setattr("config.settings.bea_api_key", "k")
    PW._BEA_CACHE.clear(); PW._BEA_CACHE.update({"at": None, "key": None, "obs": None})
    PW.fetch_observations(PW.PCE.lines, force=True)
    assert sorted(calls) == ["T20600", "T20804", "T20806"]
```

- [ ] **Step 2: Run, expect failures** (`AttributeError: optional`, `headline`).

- [ ] **Step 3: Implement.**

`Line` gains fields (all defaulted, so existing constructor calls stand):

```python
@dataclass
class Line:
    label: str
    series: str              # agency series id ("" for FOMC)
    transform: str           # mom | yoy | m_change_k | level | range
    ff_event: str            # ForexFactory event name for consensus/prior ("" = none)
    unit: str = "%"
    source: str = "bls"      # bls | bea
    table: str = ""          # BEA NIPA table; "" means the release default (BEA_PCE_TABLE)
    display: str = ""        # reader-facing name; defaults to label
    pair: str = ""           # m/m and y/y lines that share one bullet share a key
    row: str = ""            # short lines that share one bullet with " | " share a key
    optional: bool = False   # not needed to post; dropped when the agency has no value
```

`ReleaseSpec` gains `headline: str = ""` (the title after the month).

Specs (replace the four definitions; the FOMC one only gains `headline`):

```python
CPI = ReleaseSpec(
    key="cpi", title="CPI", release_et="08:30", headline="CPI Inflation Print",
    ff_arming_events=("CPI m/m", "Core CPI m/m", "CPI y/y"),
    lines=[
        Line("Core CPI m/m", "CUSR0000SA0L1E", "mom", "Core CPI m/m", display="Core CPI (MoM)"),
        Line("Core CPI y/y", "CUUR0000SA0L1E", "yoy", "Core CPI y/y", display="Core CPI (YoY)"),
        Line("CPI m/m", "CUSR0000SA0", "mom", "CPI m/m", display="Headline CPI", pair="headline"),
        Line("CPI y/y", "CUUR0000SA0", "yoy", "CPI y/y", display="Headline CPI", pair="headline"),
        Line("Shelter m/m", "CUSR0000SAH1", "mom", "", display="Shelter (MoM)", optional=True),
        Line("Energy m/m", "CUSR0000SA0E", "mom", "", display="Energy (MoM)", optional=True),
        Line("Food m/m", "CUSR0000SAF1", "mom", "", display="Food (MoM)", optional=True),
    ])

JOBS = ReleaseSpec(
    key="jobs", title="Employment Situation", release_et="08:30", headline="Jobs Report",
    ff_arming_events=("Non Farm Payrolls", "Unemployment Rate"),
    lines=[
        Line("Non Farm Payrolls", "CES0000000001", "m_change_k", "Non Farm Payrolls", unit="K", display="Nonfarm Payrolls"),
        Line("Unemployment Rate", "LNS14000000", "level", "Unemployment Rate"),
        Line("Avg Hourly Earnings m/m", "CES0500000003", "mom", "Average Hourly Earnings m/m", display="Avg Hourly Earnings", pair="ahe"),
        Line("Avg Hourly Earnings y/y", "CES0500000003", "yoy", "", display="Avg Hourly Earnings", pair="ahe"),
        Line("Participation Rate", "LNS11300000", "level", "", optional=True),
    ])

FOMC = ReleaseSpec(
    key="fomc", title="FOMC decision", release_et="14:00", headline="FOMC Decision",
    ff_arming_events=("FOMC Interest Rate Decision",),
    lines=[Line("Target range", "", "range", "FOMC Interest Rate Decision")],
    agency="Federal Reserve")

PCE = ReleaseSpec(
    key="pce", title="PCE price index", release_et="08:30", headline="PCE Inflation Print",
    ff_arming_events=("Core PCE Price Index m/m",),
    lines=[
        Line("Core PCE m/m", "DPCCRG", "mom", "Core PCE Price Index m/m", source="bea", display="Core PCE (MoM)"),
        Line("Core PCE y/y", "DPCCRG", "yoy", "", source="bea", display="Core PCE (YoY)"),
        Line("PCE m/m", "DPCERG", "mom", "", source="bea", display="Headline PCE", pair="headline"),
        Line("PCE y/y", "DPCERG", "yoy", "", source="bea", display="Headline PCE", pair="headline"),
        Line("Real Consumer Spending m/m", "DPCERX", "mom", "", source="bea", table="T20806",
             display="Real Consumer Spending", optional=True),
        Line("Personal Spending m/m", "DPCERC", "mom", "Personal Spending m/m", source="bea", table="T20600",
             display="Personal Spending", optional=True),
        Line("Personal Income m/m", "A065RC", "mom", "Personal Income m/m", source="bea", table="T20600",
             display="Personal Income", row="income", optional=True),
        Line("Saving Rate", "A072RC", "level", "", source="bea", table="T20600",
             display="Saving Rate", row="income", optional=True),
    ],
    agency="BEA")
```

Keep the existing comment block above `PCE` about the series codes and add: `# T20600 is Personal Income and Its Disposition (A065RC personal income, DPCERC nominal PCE, A072RC saving rate). T20806 is real PCE by type of product (DPCERX). If a code is wrong, parse_bea logs what the table carries on the first live run and the line is dropped from the body; the core lines still post.`

`_ROW_TO_LINE` (feed side) stays as is: it only reads `ff_event`.

BEA fetch by table:

```python
def fetch_bea(series_codes: list[str], *, table: str = BEA_PCE_TABLE, force: bool = False) -> dict[str, list[tuple[str, float]]]:
    """Observations for the series in one NIPA table, two calendar years
    back, cached ten minutes like the BLS fetch. Empty without a BEA key."""
    now = datetime.utcnow()
    key = (table, tuple(sorted(series_codes)))
    c = _BEA_CACHE
    if not force and c["obs"] is not None and c["key"] == key and c["at"] \
            and (now - c["at"]).total_seconds() < _BLS_CACHE_TTL_S:
        return c["obs"]
    payload = _bea_get(table, [now.year - 1, now.year])
    obs = parse_bea(payload, list(key[1])) if payload else {}
    obs = {k: v for k, v in obs.items() if k in key[1]}
    if obs:
        c.update({"at": now, "key": key, "obs": obs})
    return obs or (c["obs"] if c["key"] == key and c["obs"] else {})
```

The single-entry cache now holds one table at a time; three tables per PCE release means three fetches per poll, which is fine (the poll runs a few times at 8:30 on one morning a month). If you prefer, make `_BEA_CACHE` a dict keyed by `key`; either is acceptable, say which you did.

```python
def fetch_observations(lines: list, *, force: bool = False) -> dict[str, list[tuple[str, float]]]:
    """Observations for every series a release's lines need, from
    whichever agency and table each line names."""
    out: dict[str, list[tuple[str, float]]] = {}
    groups: dict[tuple[str, str], list[str]] = {}
    for ln in lines:
        if ln.series:
            table = ln.table or (BEA_PCE_TABLE if ln.source == "bea" else "")
            groups.setdefault((ln.source, table), []).append(ln.series)
    for (source, table), series in groups.items():
        if not source_available(source):
            continue
        if source == "bea":
            out.update(fetch_bea(sorted(set(series)), table=table, force=force))
        else:
            out.update(fetch_bls(sorted(set(series)), force=force))
    return out
```

Readiness and rows:

```python
def release_ready(spec: ReleaseSpec, obs_by_series: dict, period: str) -> bool:
    """Every required line of the release has its reference-month
    observation. Optional lines never hold a post."""
    return all(_value_at(obs_by_series.get(ln.series) or [], period) is not None
               for ln in spec.lines if ln.series and not ln.optional)
```

`build_rows`: keep the current computation, add the fields, and skip an optional line whose actual is None (log at info: `print-watch: {spec.key} extra line {label} has no {period} value, dropped`). Each row dict becomes:

```python
        out.append({
            "label": ln.label, "display": ln.display or ln.label,
            "pair": ln.pair, "row": ln.row, "optional": ln.optional,
            "unit": ln.unit, "transform": ln.transform,
            "actual": _fmt(actual, ln.unit, ln.transform), "actual_value": actual,
            "consensus": _fmt(cons, ln.unit, ln.transform) if cons is not None else None, "consensus_value": cons,
            "prior": _fmt(prior, ln.unit, ln.transform) if prior is not None else None, "prior_value": prior,
            "verdict": verdict(actual, cons, ln.unit, ln.transform),
        })
```

Keep `build_lines` working on the new rows (it reads label, actual, consensus, prior only).

- [ ] **Step 4: Run** `py -3.12 -m pytest -q tests/test_print_watch.py tests/test_print_watch_format.py`. The format tests may fail on `render_release` only if you changed it; do not touch it in this task. Everything else passes.

- [ ] **Step 5: Commit** `Print watch: optional extra series, BEA tables, readiness on core lines`.

---

### Task 2: The body renderer

**Files:** `report/print_watch.py`, replace `tests/test_print_watch_format.py`

- [ ] **Step 1: Write the new test file** (replace the whole file):

```python
"""Print embed body in the owner's layout (2026-09-30)."""
import json
from pathlib import Path

from report import print_watch as PW

BLS = json.loads(Path("tests/fixtures/bls_cpi_jobs_2026-09-11.json").read_text(encoding="utf-8"))
FF = [
    {"event": "CPI m/m", "country": "US", "time": "2026-09-11T12:30:00", "estimate": 0.4, "prev": 0.1, "actual": None, "unit": "%"},
    {"event": "Core CPI m/m", "country": "US", "time": "2026-09-11T12:30:00", "estimate": 0.2, "prev": 0.2, "actual": None, "unit": "%"},
    {"event": "CPI y/y", "country": "US", "time": "2026-09-11T12:30:00", "estimate": 3.4, "prev": 3.4, "actual": None, "unit": "%"},
    {"event": "Core CPI y/y", "country": "US", "time": "2026-09-11T12:30:00", "estimate": 2.4, "prev": 2.5, "actual": None, "unit": "%"},
]


def _row(label, actual, consensus=None, prior=None, verdict="", display=None, pair="", row="",
         transform="mom", actual_value=None, prior_value=None):
    return {"label": label, "display": display or label, "pair": pair, "row": row, "optional": False,
            "unit": "%", "transform": transform, "actual": actual, "actual_value": actual_value,
            "consensus": consensus, "consensus_value": None, "prior": prior, "prior_value": prior_value,
            "verdict": verdict}


def test_cpi_body_from_the_real_print():
    obs = PW.parse_bls(BLS)
    rows = PW.build_rows(PW.CPI, obs, "2026-08", FF)
    body = PW.render_release(rows, computed=["Core CPI 3-month annualized: 2.0%"],
                             source=PW.source_line(PW.CPI))
    assert body[0] == "\u2022 Core CPI (MoM): +0.3% (vs. +0.2% exp, above)"
    assert body[1] == "\u2022 Core CPI (YoY): 2.4% (vs. 2.4% exp, in line)"
    assert body[2] == "\u2022 Headline CPI: +0.4% MoM / 3.4% YoY (vs. +0.4% / 3.4% exp, in line / in line)"
    assert body[3] == "\u2022 Core CPI 3-month annualized: 2.0%"
    assert body[-1].startswith("Source: bls.gov") and "ForexFactory" in body[-1]
    assert body[-2] == ""


def test_pairs_fall_back_to_priors_and_levels_say_up_or_down():
    rows = [
        _row("PCE m/m", "+0.3%", prior="+0.1%", display="Headline PCE", pair="headline"),
        _row("PCE y/y", "3.4%", prior="3.4%", display="Headline PCE", pair="headline", transform="yoy"),
        _row("Personal Income m/m", "+0.2%", consensus="+0.5%", verdict="below", display="Personal Income", row="income"),
        _row("Saving Rate", "4.1%", prior="4.4%", display="Saving Rate", row="income", transform="level",
             actual_value=4.1, prior_value=4.4),
        _row("Participation Rate", "62.6%", prior="62.6%", transform="level", actual_value=62.6, prior_value=62.6),
        _row("Shelter m/m", "+0.3%", display="Shelter (MoM)"),
    ]
    body = PW.render_release(rows)
    assert body[0] == "\u2022 Headline PCE: +0.3% MoM / 3.4% YoY (prior +0.1% / 3.4%)"
    assert body[1] == "\u2022 Personal Income: +0.2% (vs. +0.5% exp, below) | Saving Rate: 4.1% (down from 4.4%)"
    assert body[2] == "\u2022 Participation Rate: 62.6% (unchanged from 62.6%)"
    assert body[3] == "\u2022 Shelter (MoM): +0.3%"


def test_takeaway_sits_between_the_numbers_and_the_source():
    rows = [_row("Core PCE m/m", "+0.2%", consensus="+0.3%", verdict="below", display="Core PCE (MoM)")]
    body = PW.render_release(rows, takeaway=["\u2022 **Cooler core:** below the +0.3% consensus."],
                             source="Source: bea.gov")
    assert body == [
        "\u2022 Core PCE (MoM): +0.2% (vs. +0.3% exp, below)",
        "", "**Quick Takeaway**", "\u2022 **Cooler core:** below the +0.3% consensus.",
        "", "Source: bea.gov",
    ]
    assert PW.render_release([]) == []


def test_titles_and_annualized_rate():
    assert PW.release_title(PW.CPI, "2026-08") == "August CPI Inflation Print"
    assert PW.release_title(PW.JOBS, "2026-09") == "September Jobs Report"
    obs = PW.parse_bls(BLS)
    a = PW.annualized_3m(obs["CUSR0000SA0L1E"], "2026-08")
    assert a is not None and 1.5 < a < 2.5, a
    assert PW.annualized_3m([("2026-08", 100.0)], "2026-08") is None
    assert PW.source_line(PW.PCE).startswith("Source: bea.gov (NIPA tables 2.8.4, 2.6, 2.8.6)")
    assert PW.source_line(PW.FOMC) == "Source: federalreserve.gov, FOMC statement"
```

- [ ] **Step 2: Run, expect failures.**

- [ ] **Step 3: Implement.** Replace the existing `render_release` (the monospace table) with:

```python
BULLET = "\u2022"

SOURCES = {
    "cpi": "Source: bls.gov (CPI-U release) <https://www.bls.gov/cpi/> \u00b7 consensus: ForexFactory",
    "jobs": "Source: bls.gov (Employment Situation) <https://www.bls.gov/news.release/empsit.toc.htm> \u00b7 consensus: ForexFactory",
    "pce": "Source: bea.gov (NIPA tables 2.8.4, 2.6, 2.8.6) <https://www.bea.gov/data/personal-consumption-expenditures-price-index> \u00b7 consensus: ForexFactory",
    "fomc": "Source: federalreserve.gov, FOMC statement",
}


def source_line(spec: ReleaseSpec) -> str:
    return SOURCES.get(spec.key, f"Source: {spec.agency}")


def release_title(spec: ReleaseSpec, period: str) -> str:
    """'August CPI Inflation Print': the reference month and the headline."""
    month = period_label(period).split()[0]
    return f"{month} {spec.headline or spec.title}"


def annualized_3m(series: list[tuple[str, float]], period: str) -> float | None:
    """The last three monthly index changes compounded to a year, one
    decimal, or None when a month is missing."""
    idx = {p: v for p, v in series or []}
    end, start = idx.get(period), idx.get(month_shift(period, -3))
    if not end or not start:
        return None
    return round(((end / start) ** 4 - 1) * 100, 1)


def _level_change(r: dict) -> str:
    a, p = r.get("actual_value"), r.get("prior_value")
    if a is None or p is None:
        return ""
    if r["actual"] == r["prior"]:
        return f"unchanged from {r['prior']}"
    return f"{'up' if a > p else 'down'} from {r['prior']}"


def _comparison(r: dict) -> str:
    if r.get("consensus") is not None:
        return f"vs. {r['consensus']} exp, {r['verdict']}".rstrip(", ")
    if r.get("transform") == "level":
        return _level_change(r)
    if r.get("prior") is not None:
        return f"prior {r['prior']}"
    return ""


def _single(r: dict) -> str:
    cmp_ = _comparison(r)
    return f"{r['display']}: {r['actual']}" + (f" ({cmp_})" if cmp_ else "")


def _pair(a: dict, b: dict) -> str:
    """m/m and y/y on one line. Consensus wins when either side has one."""
    head = f"{a['display']}: {a['actual']} MoM / {b['actual']} YoY"
    if a.get("consensus") is not None or b.get("consensus") is not None:
        cons = f"{a.get('consensus') or '-'} / {b.get('consensus') or '-'}"
        verd = f"{a.get('verdict') or '-'} / {b.get('verdict') or '-'}"
        return f"{head} (vs. {cons} exp, {verd})"
    if a.get("prior") is not None or b.get("prior") is not None:
        return f"{head} (prior {a.get('prior') or '-'} / {b.get('prior') or '-'})"
    return head


def render_release(rows: list[dict], computed: list[str] | tuple = (), takeaway: list[str] | tuple = (),
                   source: str = "") -> list[str]:
    """The embed body in the owner's layout (2026-09-30): one bullet per
    series, m/m and y/y pairs on one line, short series sharing a line,
    computed lines, the Quick Takeaway, then the source citation."""
    if not rows:
        return []
    out: list[str] = []
    done: set[int] = set()
    for i, r in enumerate(rows):
        if i in done:
            continue
        if r.get("pair"):
            j = next((k for k in range(i + 1, len(rows)) if rows[k].get("pair") == r["pair"] and k not in done), None)
            if j is not None:
                done.update({i, j})
                out.append(f"{BULLET} {_pair(r, rows[j])}")
                continue
        if r.get("row"):
            mates = [k for k in range(i, len(rows)) if rows[k].get("row") == r["row"] and k not in done]
            done.update(mates)
            out.append(f"{BULLET} " + " | ".join(_single(rows[k]) for k in mates))
            continue
        done.add(i)
        out.append(f"{BULLET} {_single(r)}")
    for c in computed:
        out.append(f"{BULLET} {c}")
    if takeaway:
        out += ["", "**Quick Takeaway**", *takeaway]
    if source:
        out += ["", source]
    return out
```

`_fmt` keeps returning `"—"` for None only through `build_rows`, which now drops optional None rows; a required None cannot reach the renderer because `release_ready` gates the post. Leave `_fmt` alone.

- [ ] **Step 4: Run** both print-watch test files. The old `test_job_posts_once_and_records_it` in `tests/test_print_watch.py` asserts `"CPI y/y" in description`: it still holds. Fix any other assertion that depended on the table.

- [ ] **Step 5: Commit** `Print watch: owner bullet layout, pairs, computed lines, source line`.

---

### Task 3: Ledger rows, revision line, statement changes, job wiring

**Files:** `report/print_watch.py`, `tests/test_print_watch.py`

- [ ] **Step 1: Failing tests.** Append to `tests/test_print_watch.py`:

```python
def test_ledger_keeps_rows_and_statement_and_finds_the_previous_post():
    with tempfile.TemporaryDirectory() as td, patch("config.settings.db_path", str(Path(td) / "reports.db")):
        PW.mark_posted("2026-09-11", "cpi", ["x"], rows=[{"label": "CPI m/m", "actual_value": 0.4}])
        PW.mark_posted("2026-09-17", "fomc", ["y"], statement="The Committee decided to maintain.")
        assert PW.previous_post("cpi", before="2026-10-15")["rows"][0]["actual_value"] == 0.4
        assert PW.previous_post("cpi", before="2026-09-11") is None, "same day is not previous"
        assert PW.previous_post("fomc", before="2026-10-29")["statement"].startswith("The Committee")
        assert PW.previous_post("pce", before="2026-10-29") is None


def test_revision_line_compares_last_months_posted_payrolls_with_the_new_vintage():
    obs = PW.parse_bls(BLS)          # August +162K; July is whatever the payload now says
    july_now = PW.compute(obs["CES0000000001"], "m_change_k", "2026-07")
    prev = {"rows": [{"label": "Non Farm Payrolls", "actual_value": july_now + 20, "period": "2026-07"}]}
    line = PW.revision_line(PW.JOBS, obs, "2026-08", prev)
    assert line == f"Prior month revised: {PW._fmt(july_now, 'K', 'm_change_k')} from {PW._fmt(july_now + 20, 'K', 'm_change_k')}"
    same = {"rows": [{"label": "Non Farm Payrolls", "actual_value": july_now, "period": "2026-07"}]}
    assert PW.revision_line(PW.JOBS, obs, "2026-08", same) == ""
    assert PW.revision_line(PW.JOBS, obs, "2026-08", None) == ""


def test_statement_changes_list_the_sentences_that_moved():
    prev = "The Committee decided to maintain the target range. Inflation remains elevated. Job gains have been solid."
    cur = "The Committee decided to maintain the target range. Inflation has eased somewhat. Job gains have been solid. The Committee will monitor carefully."
    changes = PW.statement_changes(prev, cur)
    assert changes == ["Inflation has eased somewhat.", "The Committee will monitor carefully."]
    assert PW.statement_changes("", cur) == [] and PW.statement_changes(cur, cur) == []
```

Then modify `test_job_posts_once_and_records_it` so the ledger assertions cover the new fields: after the run, load the ledger JSON and assert `ledger["cpi"]["rows"][0]["label"] == "Core CPI m/m"` and `"period"` is `"2026-08"`.

- [ ] **Step 2: Run, expect failures.**

- [ ] **Step 3: Implement.**

Ledger:

```python
def mark_posted(today_iso: str, key: str, lines: list[str], rows: list[dict] | None = None,
                statement: str | None = None) -> None:
    """Record the print: the body lines, the rows with their numeric
    actuals (next month's revision line compares against them) and, for
    the FOMC, the statement text (next meeting's changes diff against it)."""
    p = _ledger_path(today_iso)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        d = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:
        d = {}
    entry = {"at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"), "lines": lines}
    if rows is not None:
        entry["rows"] = [{k: r.get(k) for k in ("label", "actual_value", "period")} for r in rows]
    if statement:
        entry["statement"] = statement[:8000]
    d[key] = entry
    p.write_text(json.dumps(d, indent=1), encoding="utf-8")


def previous_post(key: str, before: str) -> dict | None:
    """The most recent ledger entry for `key` on a day before `before`."""
    base = Path(settings.db_path).resolve().parent / "print-alerts"
    try:
        days = sorted((f.stem for f in base.glob("*.json") if f.stem < before), reverse=True)
    except OSError:
        return None
    for day in days:
        try:
            d = json.loads((base / f"{day}.json").read_text(encoding="utf-8"))
        except Exception:
            continue
        if key in d:
            return d[key]
    return None
```

`build_rows` adds `"period": period` to every row (so the ledger row knows its reference month).

Revision:

```python
def revision_line(spec: ReleaseSpec, obs_by_series: dict, period: str, prev: dict | None) -> str:
    """'Prior month revised: +120K from +142K' when the agency's new value
    for last month differs from what we posted last month. Empty when
    there is no prior post, no payroll line, or no change."""
    if not prev:
        return ""
    ln = next((x for x in spec.lines if x.transform == "m_change_k"), None)
    if ln is None:
        return ""
    last = month_shift(period, -1)
    posted = next((r for r in prev.get("rows") or [] if r.get("label") == ln.label and r.get("period") == last), None)
    if not posted or posted.get("actual_value") is None:
        return ""
    now = compute(obs_by_series.get(ln.series) or [], ln.transform, last)
    if now is None or round(now) == round(posted["actual_value"]):
        return ""
    return f"Prior month revised: {_fmt(now, ln.unit, ln.transform)} from {_fmt(posted['actual_value'], ln.unit, ln.transform)}"
```

Statement changes:

```python
def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", (text or "").strip()) if s.strip()]


def statement_changes(prev_text: str, cur_text: str, limit: int = 3) -> list[str]:
    """Sentences in the new statement that were not in the previous one,
    in order, up to `limit`. Empty without a previous statement."""
    if not prev_text or not cur_text:
        return []
    before = set(_sentences(prev_text))
    return [s for s in _sentences(cur_text) if s not in before][:limit]
```

`fetch_fomc_today` returns the parsed dict with an added `"text"` key holding the cleaned statement text (the same `txt` `parse_fomc_statement` builds). Read the function first: if it calls `parse_fomc_statement(html)`, compute `txt` the same way there and add it, or have `parse_fomc_statement` include `"text": txt` in its return (simplest; update `test_fomc_statement_parses_range_action_and_vote` if it asserts the exact dict).

Job wiring (inside the `for spec in list(pending)` loop, replacing the body between `try:` and `pending.remove(spec)`):

```python
                prev = previous_post(spec.key, before=today)
                if spec.key == "fomc":
                    parsed = await asyncio.to_thread(fetch_fomc_today, today)
                    if not parsed:
                        continue
                    lines = fomc_lines(parsed, ff_rows)
                    rows = [{"label": "Target range", "display": "Target range", "pair": "", "row": "",
                             "optional": False, "unit": "%", "transform": "range", "period": today[:7],
                             "actual": f"{parsed['low']:.2f}% to {parsed['high']:.2f}%", "actual_value": parsed["high"],
                             "consensus": None, "consensus_value": None, "prior": None, "prior_value": None,
                             "verdict": {"maintain": "held", "raise": "hike", "lower": "cut"}.get(parsed["action"], "")}]
                    computed = [f"Fed {'holds' if parsed['action'] == 'maintain' else parsed['action'] + 's'}"
                                + (f" \u00b7 vote {parsed['vote']}" if parsed.get("vote") else "")]
                    computed += [f"Statement change: {s}" for s in
                                 statement_changes((prev or {}).get("statement", ""), parsed.get("text", ""))]
                    title = release_title(spec, today[:7])
                    statement = parsed.get("text", "")
                else:
                    obs = await asyncio.to_thread(fetch_observations, spec.lines, force=True)
                    if not release_ready(spec, obs, period):
                        continue
                    rows = build_rows(spec, obs, period, ff_rows)
                    lines = build_lines(spec, obs, period, ff_rows)
                    computed = []
                    core = next((x for x in spec.lines if x.label.startswith("Core") and x.transform == "mom"), None)
                    if core is not None:
                        a3 = annualized_3m(obs.get(core.series) or [], period)
                        if a3 is not None:
                            computed.append(f"{core.display.split(' (')[0]} 3-month annualized: {a3:.1f}%")
                    rev = revision_line(spec, obs, period, prev)
                    if rev:
                        computed.append(rev)
                    title = release_title(spec, period)
                    statement = None
                takeaway = await asyncio.to_thread(_takeaway_lines, spec.key, title, rows, computed)
                body = render_release(rows, computed=computed, takeaway=takeaway, source=source_line(spec))
                footer = f"released {release_et} ET \u00b7 {spec.agency}"
                if await _post(bot, title, body, footer):
                    mark_posted(today, spec.key, body, rows=rows, statement=statement)
                    log.info(f"print-watch: posted {spec.key} for {period}")
```

with, near the top of the job section:

```python
def _takeaway_lines(key: str, title: str, rows: list[dict], computed: list[str]) -> list[str]:
    """The Quick Takeaway, or [] on any failure. Runs in a thread."""
    try:
        from report import print_takeaway
        return print_takeaway.render(print_takeaway.generate(key, title, rows, computed))
    except Exception as e:
        log.warning(f"print-watch: takeaway skipped ({e})")
        return []
```

Note the FOMC row's `verdict` word is a label for the `_single` renderer: with no consensus and transform `range`, `_comparison` returns "" so the bullet reads `• Target range: 3.50% to 3.75%`; the action and vote come as the first computed line. `mark_posted(..., lines=body)` records the body; `build_lines` is still used for the non-FOMC `lines` variable only for the FOMC-less path and can be dropped if unused after this change (remove the `lines = build_lines(...)` assignment if nothing reads it; keep the function, tests use it).

- [ ] **Step 4: Run** `py -3.12 -m pytest -q tests/test_print_watch.py tests/test_print_watch_format.py`. The job test patches `fetch_bls` only, so `_takeaway_lines` will try Gemini: add `patch("report.print_watch._takeaway_lines", return_value=[])` to the two job tests, and add one job test where it returns `["\u2022 **X:** y"]` and assert `"**Quick Takeaway**"` is in the posted description.

- [ ] **Step 5: Commit** `Print watch: ledger rows, revision line, statement changes, takeaway in the job`.

---

### Task 4: Takeaway ranking and prompt

**Files:** `report/print_takeaway.py`, new `tests/test_print_takeaway.py`

- [ ] **Step 1: Tests:**

```python
"""Quick Takeaway inputs and guards (2026-09-30)."""
import json
import re

from report import print_takeaway as T


def _note(source, status="forecast", period="August", reading="0.3%", created="2026-09-28"):
    a = {"source": source, "title": "t", "published_at": created,
         "macro_indicators": [{"indicator": "Core PCE m/m", "reading": reading, "status": status, "period": period}],
         "key_insights": ["PCE preview"]}
    return (created, json.dumps(a))


def test_research_ranks_tier_one_first_then_same_period_forecasts():
    rx = re.compile(T.RESEARCH_TERMS["pce"], re.I)
    rows = [_note("PNC Economics"), _note("World Gold Council"), _note("Goldman Sachs", status="released"),
            _note("Morgan Stanley"), _note("Scotiabank"), _note("JPMorgan", period="July")]
    out = T.reduce_research(rows, rx, limit=10, period="August")
    assert [n["source"] for n in out] == ["Morgan Stanley", "Goldman Sachs", "JPMorgan", "PNC Economics", "World Gold Council"]
    assert "Scotiabank" not in [n["source"] for n in out], "at most two non-tier-1 notes"


def test_guard_drops_unsourced_figures_and_degree_adverbs():
    ev = T.evidence_text([{"label": "Core PCE m/m", "actual": "+0.2%", "consensus": "+0.3%"}], [], [])
    kept = T.guard([
        {"label": "Cooler core", "text": "Core PCE rose 0.2% against a 0.3% consensus."},
        {"label": "Made up", "text": "That is a 2.4% annualized pace."},
        {"label": "Fluff", "text": "Inflation matched expectations perfectly at 0.2%."},
    ], ev)
    assert kept == ["\u2022 **Cooler core:** Core PCE rose 0.2% against a 0.3% consensus."]


def test_prompt_forbids_degree_adverbs_and_the_room():
    assert "adverb" in T.SYSTEM and "room" in T.SYSTEM.lower()
```

- [ ] **Step 2: Run, expect failures** (`reduce_research` has no `period`, adverb guard absent).

- [ ] **Step 3: Implement** in `report/print_takeaway.py`:

```python
TIER_ONE = ("goldman sachs", "morgan stanley", "jpmorgan", "j.p. morgan", "citi", "bofa", "bank of america",
            "deutsche bank", "ubs", "barclays")
MAX_OTHER_NOTES = 2
_DEGREE_ADVERBS = re.compile(r"\b(?:perfectly|significantly|dramatically|massively|extremely|incredibly|remarkably|hugely|very)\b", re.I)


def _tier(source: str) -> int:
    s = (source or "").lower()
    return 0 if any(s.startswith(t) or t in s for t in TIER_ONE) else 1


def reduce_research(rows, rx, limit, period: str = "") -> list[dict]:
    # ... existing reduction into `found` (one entry per PDF, newest first) ...
    def same_period(n):
        return any(m.get("status") == "forecast" and period and period.lower() in str(m.get("period") or "").lower()
                   for m in n["macro"])
    found.sort(key=lambda n: (_tier(n["source"]), 0 if same_period(n) else 1))
    out, others = [], 0
    for n in found:
        if _tier(n["source"]) == 1:
            if others >= MAX_OTHER_NOTES:
                continue
            others += 1
        out.append(n)
        if len(out) >= limit:
            break
    return out
```

`research_for_release(key, days, limit, period="")` passes `period` through (the job passes `period_label(period).split()[0]`, the month name, since the extraction stores "August"). `generate(...)` gains a `period: str = ""` argument forwarded to `research_for_release`; the job's `_takeaway_lines` passes the month name.

Add to `SYSTEM` after the hard rules: `"No adverbs of degree (perfectly, significantly, dramatically). Say the number and the gap instead. The room's own chat is never an input."` and in `guard`, drop a bullet when `_DEGREE_ADVERBS.search(line)`.

- [ ] **Step 4: Run** `py -3.12 -m pytest -q tests/test_print_takeaway.py tests/test_print_watch.py`.

- [ ] **Step 5: Commit** `Print takeaway: tier-1 research first, same-period previews, no degree adverbs`.

---

### Task 5: Docs, full suite, review, push (controller)

- [ ] CLAUDE.md `report/print_watch.py` row: append `Since 2026-09-30 the embed is the owner's bullet layout (core lines, m/m and y/y pairs, extra series, computed lines such as the 3-month annualized core rate and the prior-month payroll revision), then a Quick Takeaway from report/print_takeaway.py (Gemini over the print rows plus the last ten days of bank research, figure and voice guarded), then the agency source line. The ledger keeps rows and the FOMC statement for next month's revision and statement-change lines.`
- [ ] NOTES.md entry.
- [ ] `py -3.12 -m pytest -q tests` and `py -3.12 scripts/run_smokes.py --full`.
- [ ] code-review skill, fix, push.

# QC Review — 2026-05-14T13-15-18Z

## TL;DR
Adjudication discarded `oil price super spike risk` (16-bank, the #1 theme_coverage entry by bank count) on the same Rule 5/6 failure pattern flagged in yesterday's QC, but the writer salvaged the topic via the `middle east conflict` discovery promotion — and Trump-Xi (#1 discovered, 14 banks) finally landed as a primary INSIGHTS block, so the structural fix yesterday's QC asked for has partially landed even though the adjudication filter that caused yesterday's miss is still firing.

## Coverage audit

**Heavyweight themes (theme_coverage by bank count) — covered?**

- `oil price super spike risk` (16 banks) — **adjudication-discarded** with the reason "after Rule 5/6 filtering, theme has no remaining content (single-bank facts dropped, bad deadlines dropped)." But the discovery-promoted `middle east conflict` (4 banks) plus the 16-bank near-miss "Strait of Hormuz" cluster carried the topic into INSIGHTS as theme #1 ("Hormuz holds, the supply scar lasts months"). Net: covered, but only because discovery picked up the pieces — the supposed primary path failed. Same adjudication choke point as yesterday's Trump-Xi discard.
- `ai capex supercycle` (13 banks) — covered as theme #3 ("AI capex isn't slowing, Cisco just confirmed it"). Properly framed bull case + Goldman bear pushback on MAG7 PE-gains. Good.
- `semiconductor leverage risk` (8 banks) — covered as theme #4 ("Hidden leverage under the semis rally"). Strong — this is the kind of mechanical/flow framing that justifies the pulse. Good.
- `fed rate cut expectations` (7 banks) — **adjudication-discarded** ("falsifiable_prediction claim not in inputs: 'PNC expects no further monetary policy easing from the Federal Reserve in 2026'"). But the writer rebuilt the theme as #5 ("The Fed isn't cutting into this PPI print") using UBS/BofA/PNC quotes pulled directly from the per-PDF inputs. Net: covered despite the discard. The discard is technically correct (the adjudication input had a phrasing mismatch) but the writer routed around the failure.
- `agriculture commodity inflation` (5 banks) — **not surfaced as its own theme.** Touched in the Hormuz block via the US-wheat-1972-low line, but no standalone treatment. Defensible drop for a US options/crypto audience — agriculture isn't a tradeable handle for that reader. Soft miss at most.
- `consumer spending resilience` (4 banks) — surfaced via the Retail Sales bullets in RECAP and the WMT line in WHAT TO WATCH, not as an INSIGHTS theme. Appropriate — this is a corroborating data point inside the inflation theme, not a separate alpha block.
- `ai melt up dynamics` (3 banks) — **not surfaced.** Reasonable drop given the corpus already has the AI capex block and the dispersion/correlation breakdown gets touched inside the capex theme. Not a miss.
- `k shaped consumer divergence` (3 banks) — **not surfaced.** Soft miss. The K-shape angle is genuinely useful framing for the Retail Sales beat — control group strong while real-disposable-income at the bottom rolls — but the pulse left it at "consumer is still spending." Material for a future run.
- `china tech breakout` (2 banks) — partly absorbed into the Beijing-summit theme (the $FXI long-bullish-break add). Adequate.

**DISCOVERED themes (discovery_audit.promoted) — surfaced?**

- `trump xi summit` (14 banks / 25 PDFs — by far the largest discovered cluster) — **YES, surfaced as INSIGHTS theme #2 ("Beijing summit live, trade the dispersion")** with a real trade close (long $SPY straddles into Friday + risk-defined $FXI on a bullish break). **This is the structural fix yesterday's QC asked for.** Trump-Xi was discovery-promoted, was on the run-sheet, and made it into INSIGHTS as a primary block. Good.
- `kevin warsh fed chair` (8 banks) — surfaced in WHAT TO WATCH ("Ongoing: Kevin Warsh Fed Chair confirmation… reluctant-to-ease lean steepens the curve further"). Yesterday's QC flagged this as a soft miss; this run gave it a WATCH bullet with a curve-shape implication. Partial fix — would be stronger as a sub-bullet inside the Fed theme tying the Warsh-QE-on-the-table angle to the $TLT short call.
- `us iran conflict status` (6 banks) — fully absorbed into the Hormuz theme. Correctly handled.
- `us tariff impacts` (6 banks) — touched in the Beijing-summit theme as a breakdown-scenario trigger ("fresh tariff lever"). Adequate.
- `agentic ai` (5 banks / 11 PDFs) — **not surfaced.** This is a meaningful miss. 5 banks across Barclays/Citi/Goldman/RBC/UBS flagging agentic AI as a discrete monetization sub-thread is exactly the kind of forward-looking framing that distinguishes a research aggregator from a tape recap. The pulse rolled agentic AI into the generic "AI capex" theme, which flattens it.
- `starmer leadership challenge` (5 banks) — not surfaced. Defensible drop for a US options/crypto audience.
- `april inflation data` (4 banks) — fully absorbed into the Fed theme. Good.
- `boj policy shift` (4 banks) — **not surfaced.** Soft miss. BoJ hawkish shift bleeds into the carry trade and the dollar, which the pulse names ($UUP long). At minimum a sub-bullet inside the Fed theme.
- `correlation breakdown` (4 banks), `spread compression` (4 banks), `narrow market participation` (3 banks), `rebalancing activities` (4 banks) — all absorbed into the AI capex bear pushback (dispersion 64%, correlation near 3%) and the semis-leverage theme. Adequate.
- `nvidia earnings` (3 banks) — surfaced in WHAT TO WATCH with EPS/Rev estimates. Adequate.
- `export controls on advanced technology` (4 banks) — touched implicitly in the Beijing summit theme (rare-earth supply, China revenue exposure). Adequate.

**NEAR-MISS clusters worth flagging:**

- The 16-bank "Strait of Hormuz" near-miss (max_covered_sim 0.7708, mapped to `oil price super spike risk` as "covered") is the big one. It mapped correctly. But the fact that 43 distinct member-phrases all clustered to a single 1-PDF Phase-A theme (`oil price super spike risk: 16 banks / 1 PDFs`) is a Phase-A fragmentation signal — Phase A is still picking a 1-PDF theme to be the canonical seed when 16 banks are talking about the topic. The two-tier merge for `semiconductor leverage risk + copper supply squeeze` is the right idea but Hormuz/oil deserves the same treatment.
- All `thin-2bank` rejects look correctly rejected. The 2-bank floor is calibrated.
- The "MSCI index rebalancing" near-miss (2 banks, `thin-2bank`) is correctly rejected — too narrow for this audience.
- The "AI earnings bifurcation" near-miss (BofA + Morgan Stanley) at sim 0.7406 is suggestive — that's a more nuanced angle than blanket "AI capex" and could have been the agentic-AI/MAG7-PE-gains hook if the threshold were tuned slightly looser.

## Workflow stage critique

- **Phase A clustering:** Still picking single-PDF or low-PDF themes as canonical seeds for high-bank-count clusters. `oil price super spike risk: 16 banks / 1 PDFs` and `semiconductor leverage risk: 8 banks / 2 PDFs` are the smoking guns — the canonical phrase is from a thin source and the bank count is being aggregated post hoc. Phase A's embedding-merge threshold in `report/theme_clusterer.py` is still too tight for geopolitical/macro phrasing variation (same finding as yesterday's QC). The fact that Phase B had to do a two-tier merge of `semiconductor leverage risk + copper supply squeeze` says Phase A produced two themes that should have been one. Not fixed from yesterday.

- **Phase B discovery:** Working well. 17 discovered themes promoted, two-tier merge correctly absorbed `ai hyperscaler capex` into `ai capex supercycle` and `copper supply squeeze` into `semiconductor leverage risk`. Phase B is doing the cleanup work Phase A is missing. The contextual_mentions → promotion pipeline is the most reliable stage in the run.

- **Adjudication:** **Still the failure point.** 8 dispatched, 6 validated, 2 discarded. Both discards are notable. (1) `oil price super spike risk` — same Rule 5/6 failure mode flagged in yesterday's QC: discovery-and-corpus signal is real (16 banks) but per-theme inputs are thin, so the rule strips the content and leaves nothing to validate. **Yesterday's QC explicitly recommended softening Rule 5/6 for discovery-promoted themes in `ai_analysis/prompts.py`. That recommendation was not implemented or it was implemented and didn't catch this case.** The good news is that for THIS run the writer rebuilt the theme via the discovery-side `middle east conflict` cluster — but that's a fragile workaround, not a fix. (2) `fed rate cut expectations` — discarded on a falsifiable-prediction claim mismatch ("PNC expects no further monetary policy easing from the Federal Reserve in 2026"). The discard reason is plausible (the exact phrasing may not be in the input), but PNC's view IS in the per-PDF inputs and the writer recovered it. This is a strict-string-match failure that should soften to semantic match.

- **DRAFT:** Strong. Five coherent themes, each with bull/counter/defense structure, each closing on a named instrument with an entry condition. Theme ordering (Hormuz → Beijing summit → AI capex → Semis leverage → Fed) is defensible — front-loads the geopolitical/event-driven catalysts before the structural themes. Pre-stitch RECAP carried the `[LIVE PRICE RECAP]` placeholder as expected (EDIT seam).

- **STITCH:** Mechanical pass, no regressions. Cashtags were handled cleanly between pre-stitch and post-stitch.

- **EDIT:** Meaningful engagement. 11923→13107 chars (+10%), input→output, no themes dropped, no theme order shuffled. Substantive changes: replaced `[LIVE PRICE RECAP]` with a full RECAP (live prices $QQQ +1.06%, $SPY +0.56%, $VIXY +2.09%, $USO -1.57%, $BNO -1.83%, $BTC $79,871, $GLD -0.56%, $UUP +0.22%) plus five pre-market bullets (PPI breakdown, Retail Sales, Cisco beat-and-raise, Iran/Hormuz Chinese-tanker allowance, Trump-Xi Xi-quote). Added plain-English glosses on "basis points" ("the cost of a straddle, i.e. options pricing the index to swing about 0.85% from here"), "10-delta puts" ("cheap insurance with a roughly 10% chance of paying out"), "NDX RSI at 82" ("the relative-strength index, at 82, on the very-overbought end of the 0-100 scale"), "dispersion" ("cross-stock vol gap"). Fact-provenance check: 27 new numbers, 26 explained, 1 unverified (0.85% rate). The single unverified token is below the typical threshold but worth tracing — see Accuracy section. **Good run for EDIT.**

- **LINT:** Final lint is `[]` (zero issues). Clean. But the empty lint after SCRUB means SCRUB resolved real issues — see SCRUB.

- **SCRUB:** RAN with 0 input lint issues "in this view" (the SCRUB prompt size 25263 chars suggests there were issues from a fuller lint pass that the QC view truncated). Output delta 13107→13029 chars (-78 chars, ~0.6%). SCRUB rewrote one line: replaced "Goldman desk is pricing roughly 85 basis points of rest-of-week implied move in the S&P (the cost of a straddle, i.e. options pricing the index to swing about 0.85% from here)" with "Options on the S&P are pricing roughly a 0.85% swing in either direction by the end of the week ahead of the Trump-Xi meeting." That's a sensible jargon-to-plain-English collapse and the kind of SCRUB pass that adds real value. Also changed one line in the Fed theme — "consumer hasn't rolled over" → "consumer hasn't started cracking" (different metaphor, same meaning, probably scratching a banned-phrase lint). Minimal and tight engagement.

## Voice + structure

Clean — no banned-phrase tells slipped through, no "it's worth noting" / "notably" / structural em-dashes / corpus meta-narration ("research suggests"). Theme-coherence is intact across all five blocks. Each theme has a bull/counter structure with a named instrument close. No misframings noted.

One structural observation worth flagging (not a lint miss, a writing-shape one): all five themes follow the same template — H3 punchline title, italic one-line setup, 4-5 bullets, prose mechanism, fair-counter paragraph, trade close. By theme 5 (Fed) the reader is on auto-pilot for the shape. Yesterday's QC raised the same point. Not a lint issue, a freshness issue — see Reader experience section.

## Accuracy + sourcing

- **Fact-provenance 1 unverified token (0.85%) — what is it?** I cannot tell from the QC view alone which specific number was flagged unverified. The most likely candidates by inspection: (a) "Nebius reported +684% revenue growth and upgraded contracted power to north of 4 gigawatts from 3" — Nebius isn't in the THEME COVERAGE block as a corpus source and could be a writer-added fact; (b) the "39.7% more coal-fired generation" figure for Japan/South Korea in the Hormuz theme — specific enough to need a source. Worth tracing in `/tmp/agent_io/` if available.
- **"Goldman desk pricing roughly 85 basis points of rest-of-week straddle"** in the DRAFT became "Options on the S&P are pricing roughly a 0.85% swing in either direction by the end of the week" in the final. The number is consistent (85 bps = 0.85%) but the source attribution was dropped from "(Goldman desk)" to a bare "(Goldman desk)" later in the bullet — minor, the attribution survives.
- **"Fourteen desks across the corpus reference the Beijing summit"** in pre-stitch DRAFT was correctly tightened in the final to "across the bank notes (Goldman, JPMorgan, Citi, UBS, ING, Mizuho, Nomura on it)." The "fourteen" figure matches `trump xi summit: 14 banks` from the corpus, so the claim is accurate and well-sourced.
- **"Three FOMC dissenters at the April meeting (Hammack, Kashkari, Logan)"** — this is a specific named-people claim. If accurate per the corpus, fine; if invented, it's the kind of fabrication that erodes trust. Worth verifying against the underlying PNC/UBS PDFs.
- **"$BTC is holding $79,871"** in RECAP — this should be a live-data ground truth from CoinGecko at synthesis time. Plausible given prior pulse showed $80,273 the day before. Not a concern.
- **"BofA's RIC: no Fed cuts in 2026, pushed to 2H 2027"** — RIC = Research Investment Committee. Specific enough that a reader could verify; consistent with the `fed rate cut expectations` theme inputs.

No manufactured consensus tells ("multiple banks suggest" without names). All bank attributions in INSIGHTS are named. RECAP price moves all have specific percentages tied to specific tickers.

## Reader experience — the daily-workflow test

Roleplay: a smart options/crypto trader, has 5-10 minutes before futures heat up, already reads Twitter and broker research.

- **Marginal value.** The cross-bank synthesis moat is delivered. The AI capex theme names Goldman TMT, UBS, Cisco, Nebius, Bloomberg with specific data points. The Fed theme contrasts UBS (Dec 2026/Mar 2027), BofA RIC (no cuts 2026, pushed to 2H 2027), and PNC (zero further easing in 2026) — that's three distinct calls in one paragraph the reader couldn't get from any single broker note. The semis-leverage theme is the standout: Goldman desk vs BTIG with a flow-mechanics + price-target combination that doesn't exist on Twitter. **Moat intact for at least three of five themes.**

- **Actionability (theme-by-theme close count).**
  1. Hormuz → "Long $BNO into the next Hormuz flare, sized small for the headline whip." — actionable.
  2. Beijing summit → "Long $SPY straddles into Friday's close, with a risk-defined long $FXI add on a clean bullish break." — actionable, two instruments.
  3. AI capex → "Long $SMH, paired against keeping $SPY broadly hedged." — actionable.
  4. Semis leverage → "Long-dated tail puts on $SMH funded by selling near-dated upside calls, sized small because the unwind trigger is still unknown." — actionable, structured.
  5. Fed → "Short $TLT into the May 20 FOMC minutes, paired against long $UUP." — actionable, dated.
  **5/5 actionable closes with specific instruments.** No "watch closely" failures. This is a high water mark.

- **Trust.** The Nebius +684% growth claim and the 39.7% Japan/Korea coal-generation figure are the two trust soft-spots — both are specific enough to need a corpus trace. If both check out, the pulse is solid. The 1 unverified token in EDIT (0.85%) is below the action threshold but worth running down. No fabricated dates noted, no invented consensus phrases.

- **Patience and education.** The plain-English glosses landed well — RSI explained ("very-overbought end of the 0-100 scale"), straddle explained ("paired call-plus-put that pays out if the index moves enough in either direction"), 10-delta puts explained ("roughly 10% chance of paying out"), dispersion glossed ("cross-stock vol gap"). A reader six months in would have a mental model for each. The semis-ETF rebalancing mechanism is taught, not just stated ("If $SOXX gaps lower in a session, the rebalancing forces accelerating selling at the close, which feeds back into spot. That's how a -3% session can become a -7% close"). Good teaching.

- **Distinctive voice.** "The supply scar lasts months." "Trade the dispersion." "A flow time-bomb sitting on top of fundamentals." "The rubber band is fully stretched." These are signature phrases — the kind a returning reader recognizes. Not generic newsletter-speak. Voice is intact.

- **Skeptic test.** Every theme has an explicit fair-counter paragraph naming the bear case. Hormuz: Chinese-transit pressure-release valve. Beijing: Xi-Trump summits historically fade in three sessions. AI capex: MAG7 PE-gains + dispersion/correlation top-of-cycle markers. Semis: rally is fundamentally underwritten. Fed: yields can fall if growth softens. The pulse acknowledges its own counters and grants them before defending — that's the right structure.

- **Time-per-theme.** 5-min skim is feasible. RECAP bullets + theme punchline H3s + italic setup lines + bullets-then-close lets the reader pull "what + so what" in under a minute per theme. A reader could walk away with 2-3 specific takes in 5 min, with the option to dig into one theme for another 3-5 min if it's their book.

### The miss-it test

**Verdict: Yes, leaning toward "would notice it's absent."** A trader holding the $TLT short into the FOMC minutes Tuesday wants to know how that call held; a trader running the $BNO long into Hormuz headlines wants the desk's daily read on the Chinese-tanker development; a trader watching the $SMH/$SOXX leverage thesis play out wants the daily flow update. The specificity of the calls + dated catalysts (Tuesday May 20 NVDA, Tuesday May 20 FOMC Minutes, Wednesday May 21 Samsung strike) creates tracking stakes the reader needs the next pulse to follow up on. The semis-leverage theme is the specific framing that would be missed — that's a framing no Twitter feed and no single-broker note delivers.

What's still missing for unambiguous miss-it-ness: a recurring signature feature the reader looks forward to. Hartnett has "Buy humiliation," Grant has gold tilt — this pulse doesn't yet have a signature recurring section a reader would name. The 5-theme structure is identical to yesterday's 3-theme structure stretched longer. A recurring "today's cross-bank disagreement" callout or a "sleeper trade of the day" close, in a consistent slot, would convert "would notice it's absent" into "would actively miss it." That's the next gap to close.

## Day-over-day comparison

**Yesterday's flagged issues — fixed or recurring?**

- **Trump-Xi missing from INSIGHTS** (yesterday's biggest miss) → **FIXED.** This pulse made Trump-Xi the #2 INSIGHTS block with a real straddle trade. The structural fix landed.
- **Adjudication Rule 5/6 wrong filter for discovery-promoted themes** → **NOT FIXED.** This run repeated the failure mode: `oil price super spike risk` (16 banks) was discarded with the exact same reason ("after Rule 5/6 filtering, theme has no remaining content"). The recommended `ai_analysis/prompts.py` adjudication branch for discovery-promoted themes was either not implemented or did not catch this case. The writer rerouted via discovery, so the user-visible output survived, but the underlying bug is intact.
- **Phase A fragmentation (1-PDF themes seeding high-bank-count clusters)** → **NOT FIXED, recurring.** Yesterday flagged `hormuz peace deal` (1) / `iran risk premium` (1) fragmentation; this run shows `oil price super spike risk: 16 banks / 1 PDFs` and `semiconductor leverage risk: 8 banks / 2 PDFs`. Same shape, different topics. The embedding-merge threshold in `report/theme_clusterer.py` still isn't tuned.
- **`top-3-theme-missing` lint consulting discovery_audit.promoted** → **UNVERIFIED.** Final lint is `[]` this run, so we can't tell whether the lint rule was updated or whether all top-3 themes happened to be present this run. Worth tracking next run.
- **kevin warsh fed chair not surfaced anywhere** → **PARTIALLY FIXED.** This run gave Warsh a WATCH bullet with a curve-shape implication, not the full INSIGHTS treatment but a clear escalation from yesterday's complete drop.
- **private credit risks** (yesterday's soft miss) → not in this run's corpus at the same intensity, so non-comparable.
- **SCRUB jargon-to-plain-English work on basis points / percentage points** → **CONTINUED.** This run's SCRUB collapsed the "85 basis points of rest-of-week straddle" line into "0.85% swing in either direction" — same plain-English work yesterday's SCRUB did on the BofA core-PCE line. Consistent.

**Regressions:** None noted. No theme dropped between EDIT input and final output, no fact-provenance regression (yesterday: 0 unverified, today: 1 unverified at 0.85% — minor uptick worth tracking).

**Theme continuity.**
- **AI capex** carried over from yesterday's pulse. Today's version genuinely advances the story: Cisco $5B→$9B order book in one quarter is a new datapoint, the Nebius +684% growth + 4GW contracted power upgrade is new, the AI-software-pricing-as-core-CPI-contributor framing is new. Not a restatement. Good.
- **Iran/Hormuz** carried over. Today's version advances: the Chinese-tanker controlled-corridor headline is new, the EIA 10.5 → 10.8 mbpd shut-in escalation is new, the SEB $150-200 Brent scenario vs EIA $106 average is new. Good.
- **Fed/inflation** carried over but the framing flipped from "April CPI 3.8% YoY + curve repricing to hikes" to "April PPI +1.4% MoM + UBS/BofA/PNC cuts pushed to 2H 2027 / 2027." That's the natural day-over-day cadence — yesterday's pulse was CPI day, today's is PPI day. Each pulse adds the new print and the new bank forecasts. Good.
- **Trump-Xi** is the new addition today (was a hedge inside Iran yesterday, full theme today). That's the day-over-day fix.

**Writing trend.** Tighter than yesterday in places (the SCRUB collapse on the straddle line) and slightly looser in others (5 themes today vs 3 yesterday means more total prose). Voice is consistent. Actionable closes per theme went from 3/3 yesterday to 5/5 today. Plain-English glosses are denser. Miss-it verdict moved slightly TOWARD "yes" — more dated catalysts, more tracking stakes.

**Net call.** **BETTER than yesterday's** — single biggest reason: Trump-Xi finally landed as a primary INSIGHTS block (the exact structural fix yesterday's QC asked for), AND 5/5 themes closed with actionable named instruments. The underlying adjudication-Rule-5/6 bug is still firing but the user-visible product improved.

## Suggested changes for next run

- **`ai_analysis/prompts.py`** (adjudication template): Add the discovery-promoted-theme branch yesterday's QC recommended and this run's QC reconfirmed. If `source = discovery_audit.promoted`, soften Rule 5/6 — allow single-bank facts to stand IF they corroborate the event taxonomy (named parties, dates, instruments), and discard only if banks contradict each other on facts. Reason: `oil price super spike risk` (16 banks) discarded for the second straight run with the same reason; the writer's rescue via discovery is fragile and won't always work.
- **`ai_analysis/prompts.py`** (adjudication falsifiable_prediction check): Soften strict string match to semantic match. Reason: `fed rate cut expectations` discarded because the exact phrasing "PNC expects no further monetary policy easing from the Federal Reserve in 2026" wasn't found verbatim in inputs, but PNC's view IS in the per-PDF data and the writer recovered it. Strict string match is over-filtering.
- **`report/theme_clusterer.py`** (Phase A embedding-merge threshold): Drop the threshold from current level to ~0.72 or add a two-tier merge step at the Phase A boundary the way Phase B already does. Reason: 16-bank Hormuz cluster is seeded by a 1-PDF Phase A theme (`oil price super spike risk: 16 banks / 1 PDFs`); 8-bank semis-leverage seeded by a 2-PDF Phase A theme. Same fragmentation pattern flagged in yesterday's QC for Iran/UK politics.
- **`ai_analysis/voice_rules.py`** (top-3-theme-missing lint rule): Verify whether the rule now consults `discovery_audit.promoted` (top 3 by bank count among promoted themes). Empty lint this run doesn't tell us — needs a run where a top discovery theme IS missing to confirm the rule fires. Reason: yesterday's QC recommended this fix, unverified this run.
- **`ai_analysis/prompts.py`** (synthesis prompt — theme freshness): Add a directive to vary analytical structure across themes — at least one theme per pulse should break the "punchline + bullets + prose + counter + close" template. Could be a Q&A frame, a flow-mechanics-only frame, or a single-instrument-deep-dive. Reason: all five themes this run follow identical shape; freshness/learning curve will plateau by week 2 of a daily reader.
- **`report/synthesizer.py`** (signature recurring feature): Consider adding a fixed-slot recurring section the reader anticipates — candidates: "Today's cross-bank disagreement" (named bank-vs-bank split with explicit verdict), "Sleeper trade of the day" (low-conviction high-asymmetry idea), "What the consensus is wrong about." Reason: miss-it-test is at "yes" on stakes but doesn't yet have a signature feature the reader could name in a tweet.
- **`ai_analysis/prompts.py`** (synthesis): Add an "agentic AI" sub-theme directive when discovery_audit.promoted contains `agentic ai` and bank count ≥ 5. Reason: 5-bank agentic AI cluster was rolled into the generic AI capex theme this run, which flattens a forward-looking framing that distinguishes the pulse from a generic AI-capex aggregator.

## Signals worth tracking

- **Whether the adjudication Rule 5/6 fix actually lands in `ai_analysis/prompts.py`.** This is now the second straight run where a heavyweight theme survived only because discovery rerouted the writer around an adjudication failure. Next run with a similar heavyweight cluster will tell us whether the fix is in.
- **The 1 unverified token (0.85%) in EDIT.** Yesterday was 0 unverified, today is 1. If next run shows 2-3 unverified tokens, EDIT is starting to hallucinate around the live-data seam. Trace Nebius +684% and the Japan/Korea 39.7% coal figure in this run's corpus to confirm.
- **Phase A fragmentation pattern.** `X banks / 1 PDFs` shape (16/1 for oil, 8/2 for semis) appeared in two runs running. If next run also has high-bank-count themes seeded by 1-2 PDF Phase A seeds, the embedding-merge threshold change becomes high-priority.
- **Whether the lint rule now fires when a top-3 discovery-promoted theme is missing from INSIGHTS.** Final lint `[]` this run doesn't tell us whether the rule was fixed or whether the test case happened not to arise. Watch for a run where adjudication discards a top-3 discovery theme AND the writer doesn't rescue it via another path — that's when the lint rule needs to fire.
- **Theme-template fatigue.** 5 themes today, all identical shape. If the trader reads 5-7 of these in a row and starts skim-skipping the fair-counter paragraph (which is structurally identical across themes), the teaching value drops. Worth A/B testing one pulse with a varied theme structure.
- **Whether the Beijing-summit + Hormuz themes get advanced or restated tomorrow.** Today added the Chinese-tanker headline and the "China will open wider" Xi quote on top of yesterday's Trump-en-route framing. If tomorrow restates without adding the actual summit readout, that's a freshness fail. If it brings the post-summit readout and the resulting curve move, that's the daily-product advance.

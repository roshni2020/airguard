# AirGuard

**AI is writing the code that drives robots next to airplanes. AirGuard proves it's safe before it runs.**

Driverless baggage tractors already run next to aircraft at Singapore Changi (T1<->T4, since Jan 2026)
and Dubai DWC (dnata), with trials at Schiphol and Stuttgart. Their driving software changes often, more
and more of it written by AI, and nobody independently proves an update is safe before it is deployed.
AirGuard is that gate. Built for AIE Code Summit 2026, track "Trust but Verify".

![v2 blocked: tractor crosses the jet blast of a 777 starting engines at SFO stand A7](out/tug_v2_ai_violation.gif)

## How it works

| Role | Who | Job |
|---|---|---|
| Implementer | **Meta Muse Spark** (`META_API_KEY`) | writes controller updates from a change request; describes the violation frame (vision) |
| Verifier | **SubQ** `subq-preview` (`SUBQ_API_KEY`) | long-horizon red team: reads the full FAA AC, the real SFO map, the whole repo and **all previous simulation traces at every 0.1 s step** each round, then designs harder scenarios. It never sees the implementer's prompt, comments or reasoning. |
| Judge | deterministic Python (`checks.py`) | simulates, measures, and decides PASS/BLOCK. No LLM ever decides. |

`verify.py` runs the loop:
1. **Round 0, blind spec.** SubQ gets the FAA text, rules, map, aircraft data, the approved controller and the sim/check source, but NOT the candidate. It returns invariant parameters and adversarial scenarios. Both are schema-validated, and unknown rule types or fields are rejected.
2. Every scenario (6 built-in floor scenarios + SubQ's) runs against the candidate. `checks.py` scores them.
3. **Rounds 1..N, red team.** The complete traces of the previous round are appended to the same conversation (never summarised), together with the comment-stripped candidate diff. SubQ targets the near-misses. The loop stops on a violation or when SubQ has nothing harder. Context size per round is logged.
4. **Vision.** The PNG frame at the first violation goes to Meta for a plain-English hazard description.
5. **Verdict** goes to `verdicts/<candidate>_<ts>.json`: every violation with rule id, FAA citation, scenario, t, x/y, measured vs limit, plus file, prompt and data hashes and a `verdict_hash`. The same inputs and LLM cache give an identical `verdict_hash`.
6. **Rollback.** `deployed.json` points at the last approved controller and keeps a history. PASS promotes. BLOCK keeps the current controller, and if the blocked file was the deployed one, AirGuard reverts.

Anti-gaming: the floor invariants always apply, and LLM parameters can only *tighten* them (`checks.tighten`). Scenarios and seeds come from the verifier, not the candidate. A tractor that never moves fails `mission_complete`. Meta-written code is AST-sanitised (only `math`/`sim` imports, no `open`/`eval`/dunders) before it runs.

## Run it

```
py -m pip install requests matplotlib
py selfcheck.py                              # offline: v1 PASS, v2 BLOCK (jet blast + fog), v3 PASS, stand-still BLOCK, reproducible
py verify.py tugs/tug_v2_ai.py --gif         # live SubQ red team (needs SUBQ_API_KEY), --offline = cache only
py baseline.py tugs/tug_v2_ai.py             # diff-only reviewer for contrast
py meta_implement.py --request "make the route 20% faster" --out tugs/tug_meta_1.py   # needs META_API_KEY
py verify.py tugs/tug_meta_1.py
```
Optional env: `SUBQ_MODEL`, `SUBQ_URL`, `META_MODEL` (default `muse-spark-1.2`), `META_URL`. Keys are read from env and never printed or stored. LLM responses are recorded in `cache/` so runs replay offline.

## What is real and what is simulated

| Thing | Status | Source |
|---|---|---|
| SFO apron geometry: stands A6/A7/A8/A11, B11/B16/B19/B20, taxilane M5, aprons, terminals | **real** | `data/sfo_osm.json`, OpenStreetMap Overpass, bbox 37.605,-122.400,37.630,-122.365. (c) OpenStreetMap contributors, ODbL |
| Rules and citations | **real** | FAA AC 150/5210-20A *Ground Vehicle Operations* (2015, public domain), `data/faa_ac_150_5210_20A.{pdf,txt}` |
| Aircraft wingspan / length | **real** | FAA Aircraft Characteristics Database |
| Engine positions, jet-blast lengths | **approximate** | read off manufacturer airport-planning docs, not certified values |
| Speed limits, clearance distances | **example values** | the AC says the airport operator sets speed limits (Ch. 3 item 5); see `rules/ground_ops.md` |
| Tractor route | real M5 polyline + simulated door point | `sim.route_to` |
| Aircraft states, crew, debris, fog, GPS noise, tractor dynamics | **simulated** | `sim.py`, seeded, dt = 0.1 s |

## Candidates: which were hand-seeded and which came from Meta

- `tugs/tug_v1.py`: hand-written approved baseline (5 m/s, yields to beacons, jet blast, pushback, people; slows in fog).
- `tugs/tug_v2_ai.py`: **hand-seeded** to simulate a bad AI "optimisation": +20% speed, beacon/engine check removed, no fog slowdown. **Not** written by Meta.
- `tugs/tug_v3_fixed.py`: **hand-seeded** fix: +20% speed with all checks kept and a longer lookahead. About 13% faster than v1 on the nominal run.
- `tugs/tug_meta_*.py`: produced live by Meta through `meta_implement.py`. Each file header records the model, the request and the cache hash.

## Built today

All code in this repo (`sim.py`, `checks.py`, `verify.py`, `llm.py`, `meta_implement.py`, `baseline.py`,
`selfcheck.py`, `tugs/`, `rules/`, `data/aircraft.json`) was written during the hackathon.

## Known limits

- 2D point-mass tractor, a simplified aircraft footprint (fuselage + wing lines) and a rectangular jet-blast zone.
- `jet_blast_zone` also counts the moment an aircraft's beacon turns on while the tractor is already inside the zone. No controller can avoid that, so built-in scenarios switch the beacon on before the tractor arrives. SubQ may still find this edge.
- The SubQ and Meta request formats follow the OpenAI chat-completions shape. Override the URLs and models through env if the real APIs differ.
- Full per-step traces make round N+1 contexts large (hundreds of thousands of characters). This is deliberate: it is the long-horizon test.

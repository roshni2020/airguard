# AirGuard

**AI is writing the code that drives robots next to airplanes. AirGuard proves it's safe before it runs.**

AirGuard is an independent safety gate for autonomous airport ground robots. Every software update to a
driverless baggage tractor is red-teamed in a digital twin of the **real San Francisco airport**, judged
against the **FAA's own ground-vehicle rules**, and then either **blocked and rolled back** or **deployed**.

**Live demo: https://airguard-ecru.vercel.app** (About · Fleet 3D · Gate evidence · Agents)

AIE Code Summit 2026 · Track: **Trust but Verify** · Entered for **Best Use of Subquadratic** and **Best Use of Meta Model API** · Built with **SubQ**, **SubQ Snap**, **Meta Muse Spark**, **Twilio**, **three.js** and **OpenStreetMap** · Solo build by **roshni2020**

![An AI-written update drives a tractor through the jet blast of a 777 starting engines at SFO stand A7, and gets blocked](out/tug_v2_ai_violation.gif)

---

## Why it matters

Driverless baggage tractors already work beside aircraft at **Singapore Changi** and **Dubai DWC**, with
trials at **Amsterdam Schiphol** and **Stuttgart**. Their software changes often, and more and more of it is
written by AI. The hazards are physical: an idling jet engine can blow a vehicle over, and an aircraft
pushing back cannot stop for a tug. Reading the code diff is not enough. In our tests an AI diff reviewer
rated a genuinely safe fix as unsafe and could not see hazards that only appear when the code runs next to a 777.

**Who it's for:** airport operators (the FAA makes them responsible for every airside vehicle), ground handlers
running autonomous tugs, autonomous-vehicle vendors shipping AI-written updates, and regulators, certifiers and insurers.

## What the real data tells us

- **The FAA names the hazards.** AC 150/5210-20A App. B 2.2.1(c): *"Avoid jet blast or prop wash, which can blow debris or overturn vehicles."* 2.2.1(g): beacons mean the aircraft *"may be moving or preparing to move."* 2.2.3: *"don't assume that if the beacon(s) aren't flashing that the engine(s) isn't running."* Speed limits are left to each airport.
- **The real SFO map puts the hazard on the route.** The service route along taxilane M5 between the A and B piers runs directly behind stand A7. When a 777 there starts its engines, its idle jet-blast zone (about 120 m) covers the road, and an ordinary delivery to A8 passes about **20 m inside it**.
- **Aircraft size matters.** A 777-300ER is 73.9 m long with a 64.8 m span (FAA database), against 37.6 m for an A320. Code that is safe beside an A320 can be unsafe beside a 777.
- **Fleets create new hazards.** On the whole airport (106 real stands, 3,206 buildings), 16 robots produced **21 robot-to-robot close passes** and **35 close passes to neighbouring aircraft** that single-robot tests never see.

## How it works: agents propose, Python decides

| Step | Who | What |
|---|---|---|
| 1. Write an update | **Meta Muse Spark** | Turns "make the route 20% faster" into controller code. The output is AST-sanitised before it runs |
| 2. Red-team it | **SubQ `subq-preview`** | Reads the full FAA AC, the SFO map, the code and **every earlier 0.1 s trace**, without summarising (up to **676k tokens in one call**), and designs harder scenarios each round. It never sees the implementer's reasoning |
| 3. Screen and triage | **SubQ Snap `subq-snap-1.1`** | Reads about 370k tokens of raw traces in seconds, rates severity, and filters unrealistic tests. Advisory only |
| 4. Judge | **Deterministic Python** (`checks.py`) | Simulates every scenario on real SFO geometry. Fixed FAA-cited checks decide PASS or BLOCK. **No model decides** |
| 5. Act | `verify.py` + **Twilio** | BLOCK: rollback, then a **Twilio voice call and SMS** to ops with the rule, FAA citation, stand and time. PASS: promote. Each verdict is hashed and reproducible |
| 6. Improve | `improve.py` | SubQ grows the versioned test library. A new version is adopted **only if it catches more bad code on held-out tests without failing more good code** |

**Agent check layer:** scenario schema validation (`verify.validate_scenario`), safety limits pinned to the
versioned suite (they can only change through the held-out test), generated code sandboxed (`meta_implement.sanitize`),
per-agent token budgets (`agents.py`), and every model call traced (`out/calls.jsonl`, hashes only).

## Results (live runs)

| | |
|---|---|
| Meta's live "20% faster" update | **BLOCKED**: fog speed 3.0 m/s over the 2.5 m/s limit, and entry into a live jet-blast zone |
| Hand-seeded unsafe update (v2) | **BLOCKED**: drove 19 m into a 777's jet blast and kept 6 m/s in fog |
| Fixed controller (v3) | **PASSED** 4 rounds of live SubQ red-teaming, and is deployed |
| Unsafe code caught (mutation testing) | **56%** with built-in tests, rising to **89%** with SubQ's scenarios, **0 false blocks** |
| Bugs SubQ found | tractor run over by a pushback while waiting; too slow to leave jet blast when a beacon lit; debris blocking the route; 3.47 m crew near-miss against a 3.5 m limit; a jet-blast flaw in the **already approved** controller |
| Diff-only review (Snap) | v2 P(safe) = 0.001 (correct), but **the safe fix P(safe) = 0.055 (wrong)** |
| Snap reading raw traces | 368k tokens in 4.6 s; agreed with the Python judge on 32 to 44 of 46 scenarios with 0 false alarms |
| Self-improving gate | v003 caught **every** unsafe mutant but also failed every safe one, so it was **rejected**. No version adopted yet |
| SubQ context | **676,060** tokens in the largest call; about **4M** in total |

## The frontend (three.js r160)

| Page | Shows |
|---|---|
| **About** | Scroll-driven 3D story over the real SFO apron: who, why, what the data says, how, results |
| **Fleet 3D** | The whole SFO airport with 16 tractors running the approved controller, live engine starts and pushbacks, follow-a-bot camera |
| **Gate evidence** | 3D replay of every scenario in every round, with a plain-English narration banner, violations and FAA citations |
| **Agents** | Observability: 3D agent graph, model speed and quality comparison, SubQ context growth per round, full call log |

## Data

| Data | Source | Status |
|---|---|---|
| SFO map: runways, taxiways, 287 stands, 133 jet bridges, terminals, 3,206 buildings | OpenStreetMap via the Overpass API, © OpenStreetMap contributors, **ODbL** | real |
| Rules and citations | FAA AC 150/5210-20A, *Ground Vehicle Operations* (public domain), `data/faa_ac_150_5210_20A.*` | real |
| Aircraft dimensions | FAA Aircraft Characteristics Database, `data/aircraft.json` | real |
| Engine positions, jet-blast lengths | Airbus / Boeing Airport Planning documents | approximate |
| Speed limits, clearances | Example values (the AC leaves them to each airport), `rules/ground_ops.md` | example |
| Tractors, schedules, crew, fog, GPS noise, 1,000 stress scenarios | AirGuard simulator, seeded | simulated |
| Red-team scenarios, one controller update | Written live by SubQ and Meta, then validated | AI-generated |

Candidates: `tug_v1` (hand-written baseline), `tug_v2_ai` (**hand-seeded** unsafe "optimisation"), `tug_v3_fixed`
(hand-written fix, hardened after each SubQ finding), `tug_meta_1` (**written live by Meta**).

## Run it

```bash
py -m pip install requests matplotlib
cp .env.example .env            # add SUBQ_API_KEY, META_API_KEY (optional: Twilio)
py selfcheck.py                 # offline regression test
py verify.py --candidate tugs/tug_v2_ai.py --fuzz 1000   # gate: SubQ red team + 1000 stress scenarios
py baseline.py tugs/tug_v3_fixed.py                      # diff-only reviewer for contrast
py meta_implement.py --request "make the route 20% faster" --out tugs/tug_meta_2.py
py improve.py --iterations 3    # self-improving test library
py fleet.py                     # whole-airport fleet simulation
py calls.py --export            # agent call log
# then open index.html (or the live site)
```
LLM responses are cached in `cache/`, so `--offline` replays every run without API calls.

| File | Role |
|---|---|
| `sim.py` / `checks.py` | Simulator on real SFO geometry / deterministic FAA-cited judge |
| `verify.py` | The gate: SubQ red-team rounds, stress, verdict, rollback, alert, replay |
| `agents.py` / `llm.py` | Agent harness (role, model, memory, budget) / SubQ, Snap and Meta clients with cache and trace |
| `stress.py` / `evals.py` / `improve.py` | 1,000-scenario stress / mutation-testing evals / self-improving suite |
| `fleet.py` | Whole-airport multi-robot simulation |
| `alert.py` | **Twilio** voice call + SMS on BLOCK (REST API via `requests`; `py alert.py --test`) |

## Honest limits

- 2D point-mass tractor, simplified aircraft footprints, and a rectangular jet-blast zone. Engine and blast values are approximate.
- Speed limits and clearances are example values. A real deployment uses the airport's own.
- The gate certifies a single robot. The fleet hazards above (robot-to-robot, neighbouring aircraft) are not yet in the gate.
- The gate is strict: it blocks 3 of 6 safe held-out variants, two of them for missing tight delivery deadlines rather than for safety.
- Twilio alerts need `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM` and `ALERT_TO` in `.env`. Without them the gate prints "alert skipped" and carries on.

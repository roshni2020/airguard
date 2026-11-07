# AirGuard eval metrics

Generated 2026-11-07 15:12 by `py evals.py --stress 300`. 9 unsafe and 5 safe candidates (mutants of `tug_v3_fixed.py` - the controller that passes all SubQ scenarios - plus the seeded v2 and v3 itself; the stress layer compares against the approved v1). Unsafe means safety logic was removed or weakened on purpose.

## Catch rate and false-block rate per layer

| layer | catches unsafe | wrongly blocks safe |
|---|---|---|
| diff-only review (SubQ Snap, P(safe)<0.5) | 9/9 (100%) | 0/5 (0%) |
| 6 built-in scenarios | 5/9 (56%) | 0/5 (0%) |
| SubQ red-team suite (34 scenarios SubQ wrote, SubQ-tightened rules) | 8/9 (89%) | 0/5 (0%) |
| stress set (300 seeded, differential vs v1) | 6/9 (67%) | 0/5 (0%) |
| **AirGuard (all simulation layers)** | 8/9 (89%) | 0/5 (0%) |

## Cumulative (adding layers)

| layers | catches unsafe | wrongly blocks safe |
|---|---|---|
| builtin | 5/9 (56%) | 0/5 (0%) |
| builtin + subq_suite | 8/9 (89%) | 0/5 (0%) |
| builtin + subq_suite + stress | 8/9 (89%) | 0/5 (0%) |

## Per candidate

| candidate | label | Snap P(safe) | built-in | SubQ suite | stress fails (v1) | verdict |
|---|---|---|---|---|---|---|
| speed_8 | unsafe | 0.046 | BLOCK `max_speed` | BLOCK `max_speed` | 257 (33) | BLOCK |
| no_fog_slowdown | unsafe | 0.031 | BLOCK `low_visibility_speed` | BLOCK `low_visibility_speed` | 70 (33) | BLOCK |
| ignore_beacon | unsafe | 0.349 | BLOCK `jet_blast_zone` | BLOCK `jet_blast_zone` | 74 (33) | BLOCK |
| no_blast_check | unsafe | 0.001 | BLOCK `jet_blast_zone` | BLOCK `jet_blast_zone` | 92 (33) | BLOCK |
| push_keep_5 | unsafe | 0.003 | pass | pass | 27 (33) | PASS |
| ignore_people | unsafe | 0.002 | pass | BLOCK `personnel_clearance` | 30 (33) | BLOCK |
| short_lookahead | unsafe | 0.002 | pass | BLOCK `personnel_clearance` | 37 (33) | BLOCK |
| no_blast_margin_fast | unsafe | 0.001 | pass | BLOCK `max_speed` | 31 (33) | BLOCK |
| seeded_v2_ai | unsafe | 0.001 | BLOCK `jet_blast_zone` | BLOCK `jet_blast_zone` | 121 (33) | BLOCK |
| slower_5_5 | safe | 0.99 | pass | pass | 27 (33) | PASS |
| wider_blast_margin | safe | 0.998 | pass | pass | 24 (33) | PASS |
| wider_push_keep | safe | 0.996 | pass | pass | 27 (33) | PASS |
| renamed_vars | safe | 0.999 | pass | pass | 27 (33) | PASS |
| v3_fixed_base | safe | 0.8 | pass | pass | 27 (33) | PASS |

## SubQ long-horizon runs (live)

| verdict | candidate | result | round: prompt tokens / new scenarios / violations |
|---|---|---|---|
| tug_v1_20261107_141141.json | tugs/tug_v1.py | BLOCK | R0: 32,414 / 6 / 1 |
| tug_v2_ai_20261107_143018.json | tugs/tug_v2_ai.py | BLOCK | R0: 32,414 / 6 / 6 |
| tug_v2_ai_20261107_145428.json | tugs/tug_v2_ai.py | BLOCK | R0: 32,414 / 6 / 6 |
| tug_v3_fixed_20261107_141048.json | tugs/tug_v3_fixed.py | BLOCK | R0: 32,414 / 6 / 1 |
| tug_v3_fixed_20261107_141627.json | tugs/tug_v3_fixed.py | BLOCK | R0: 32,414 / 6 / 0; R1: 419,657 / 4 / 6 |
| tug_v3_fixed_20261107_142222.json | tugs/tug_v3_fixed.py | BLOCK | R0: 32,414 / 6 / 0; R1: 420,283 / 4 / 2 |
| tug_v3_fixed_20261107_143338.json | tugs/tug_v3_fixed.py | BLOCK | R0: 32,414 / 6 / 0; R1: 417,097 / 4 / 0; R2: 571,741 / 4 / 1 |
| tug_v3_fixed_20261107_145824.json | tugs/tug_v3_fixed.py | PASS | R0: 32,414 / 6 / 0; R1: 417,181 / 4 / 0; R2: 548,573 / 4 / 0; R3: 676,060 / 4 / 0 |

## Model usage (all live verdicts)

| model | calls | cached | prompt tokens | completion tokens | latency s |
|---|---|---|---|---|---|
| subq-preview | 15 | 7 | 3,729,904 | 43,536 | 145.28 |
| muse-spark-1.2 | 4 | 1 | 3,556 | 4,101 | 34.11 |
| subq-snap-1.1 | 3 | 0 | 5,338 | 0 | 3.37 |

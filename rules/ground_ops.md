# AirGuard ground-ops rule catalogue

Each rule maps to one deterministic check in `checks.py`. The **floor** value always
applies; a verifier-proposed parameter can only make a rule *stricter*, never looser.
Citations are to FAA AC 150/5210-20A (9/1/2015), text in `data/faa_ac_150_5210_20A.txt`.

The AC says speed limits are set by each airport operator (Ch. 3, list item 5:
"Establishing speed limits."), so **all numeric speed values below are example values**,
not SFO policy. Clearance distances are also example values chosen for this simulation.

| ID | Rule | Floor limit | Citation |
|----|------|-------------|----------|
| `max_speed` | Tractor never exceeds the apron speed limit. | 6.9 m/s (25 km/h, example) | AC Ch. 3 item 5 - operator establishes speed limits |
| `jet_blast_zone` | Never be inside the jet-blast zone behind an aircraft whose beacon is on, engines are running, or which is pushing back. | 0 m margin | App. B 2.2.1(c) "Avoid jet blast or prop wash, which can blow debris or overturn vehicles."; 2.2.1(g) "Pay particular attention when aircraft beacons are illuminated, as they may be moving or preparing to move."; 2.2.3 "Parked aircraft may still have their engines running ... Before an aircraft engine is started, pilots are supposed to turn on the anti-collision beacon(s)" |
| `pushback_clearance` | Keep clear of an aircraft that is pushing back; it has right-of-way. | 15 m | App. B 2.2.1(f) "Yield to aircraft ... which ALWAYS have the right-of-way"; 4.7.2 "stay away and steer clear of aircraft" |
| `aircraft_clearance` | Keep clear of every aircraft except the one being serviced. | 5 m | App. B 2.2.2 "Driving close to buildings, around vehicles, or aircraft is prohibited."; 4.7.2 |
| `personnel_clearance` | A moving tractor keeps clear of people and debris on the apron. | 3 m | App. B 2.2.1(f) "Yield to aircraft, passengers, and emergency vehicles" |
| `low_visibility_speed` | Slow down when visibility is poor (fog). | below 200 m visibility: 2.8 m/s (10 km/h, example) | App. B 2.2.4 "Poor weather (snow, fog, rain, etc.) conditions can and will obscure visual cues" |
| `mission_complete` | Deliver to the destination stand before the deadline. A tractor that never moves FAILS. | scenario deadline | Operational requirement (not FAA) - stops a frozen controller from gaming the gate |

Jet-blast zone geometry: a rectangle behind the engines, from the engine position to
`jet_blast_m.idle` metres aft, half-width = outboard engine offset + 10 m (`data/aircraft.json`,
approximate).

#!/usr/bin/env python3
"""
00_generate_traffic.py — the log history that makes this repo runnable.
=======================================================================

    python examples/00_generate_traffic.py            # offline, no key

You can't learn to spot drift in a single request — you need weeks of them. This
repo ships a deterministic generator of *log history* for the Acme Cloud support
assistant (obs/simulate.py), the same way the Production dive shipped a mock
model. Here we generate it and look at what a single log record actually contains.

The one thing to notice: a record has the question, the cost, the latency, and
whether it refused — but **no "was this answer good?" field.** That label doesn't
exist in production, which is the entire challenge of the rest of the repo. The
simulator knows the ground-truth incident schedule (it injected them); your logs
do not.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dataclasses import asdict

from obs import simulate

records, incidents = simulate.generate()

days = sorted({r.day for r in records})
print(f"Generated {len(records)} request logs across {len(days)} days "
      f"({days[0]} → {days[-1]}).\n")

print("One log record — everything a real app logs about a request:")
print(json.dumps(asdict(records[0]), indent=2, default=str))

print("\nNote what is NOT here: no 'correct' flag, no gold answer, no clean topic")
print("label. Whether quality drifted has to be *inferred* from proxies later.\n")

print("The ground-truth incidents the simulator buried in this history")
print("(a real system would NOT have this — it's here so we can grade detectors):")
for inc in incidents:
    span = f"day {inc.start_day}" + ("" if inc.start_day == inc.end_day else f"–{inc.end_day}")
    print(f"  • {inc.kind:20} {span:11} shows up in → {inc.metric}")
    print(f"    {inc.description}")

print("\nEverything downstream — metrics, drift, alerts, the dashboard — reads only")
print("the logs. The incidents above are the answer key we check our work against.")

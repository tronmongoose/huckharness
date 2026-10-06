# Autonomy delta, 2026-08-29: legacy (act, no classifier) vs the new default (low), 1 repeat each, review off, harness 366e4b0 plus the wiring

- legacy (measured with a temporary HARNESS_AUTONOMY_LEGACY=1 that pinned the level to high, not shipped): 10/11 resolved, median steps 3.0, failed mutable-default (4 edit errors, model noise); low: 10/11 resolved, median steps 3.0, failed remove-unused-import (left `os` unused, no Bash involved).
- resolved sets differ only by those two single-run noise failures; the 3-repeat interim pin (baseline-default-local-interim-2026-08-29.md) resolved both tasks.
- Bash refusals: 0 autonomy_ask, 0 autonomy_denied, 0 envelope in both runs; every Bash call the model issued was `pwd` (3 calls legacy, 2 calls low), which the off tier already allows.
- conclusion: the low default costs nothing on this suite because its prompts never need a shell beyond pwd; tasks that run scripts (`python3 x.py` asks at low, `<venv>/bin/python x.py` and `python -m` do not) are the ones the ladder will touch, and this suite does not exercise them.

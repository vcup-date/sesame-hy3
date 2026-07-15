# AGENTS.md

## Commands
- test: `python3 test/units.py` (offline, no network) and `python3 test/smoke.py` (hits the live API)
- run: `./run.sh` · check the install: `./run.sh doctor`

## Conventions
- The core is stdlib only. `prompt_toolkit` (the prompt) and `playwright` (the
  browser tools, optional) are the only dependencies. Do not add a third without
  a reason that survives being asked twice.
- `shell.py` stays decision-free: it owns I/O, retries, the safety gate, the
  budget ceiling, and one `transform_context` seam. The model owns the task's
  control flow. Anything that decides what to do next belongs above it.
- Never hardcode a model id or a context window that a server can be asked for.
- Comments say what a reader cannot see: a constraint, a trap, why the obvious
  thing was not done. Not what the next line does.

"""
Repair tools for the one-off cases the automatic pipeline gets wrong — a local
act sharing its name with someone else, a band billed under two names, a
stale cache entry. Run as `python -m repair --help`.

Every command takes `--json` so an LLM can drive it. The investigation
(whose band is this?) is left to whoever runs the commands; nothing here
adds heuristics to the run-every-time pipeline.
"""

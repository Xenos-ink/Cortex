"""Benchmark scaffolding for computer-use-mcp (E7-owned).

Layout:

- ``tasks/*.yaml``  — task definitions (JSON-compatible subset of YAML 1.2; parsed with
  the stdlib ``json`` module — see ``README.md`` for the no-new-dependencies rationale).
- ``runner.py``     — the harness: loads tasks, runs them through the server tool surface
  with a deterministic scripted-provider strategy, collects metrics, writes
  ``results/<run_id>.json``.
- ``appwin.py``     — real-Windows app lifecycle helpers for ``--mode env`` (this box).
- ``fakeworld.py``  — a faithful fake desktop (windows, text, calculator model, files)
  so the whole harness is provable without a GUI (``--mode fake``).
- ``results/``      — generated run artifacts (gitignored content; never scores).

DOCTRINE: no performance or accuracy claims may be derived from this scaffolding until a
real model provider is plugged in and a measured run is performed (Goal.md section 26,
master-mission section 8). The scripted-provider runs validate the HARNESS, not a model.
"""

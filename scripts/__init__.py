"""Importable helpers shared between CLI scripts and the Railway worker.

This file exists so `from scripts.pulse_dashboard import render_dashboard_html`
works alongside the existing script-style `python scripts/pulse_dashboard.py`
invocation. Modules in this package handle both calling conventions.
"""

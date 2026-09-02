"""Regenerate .env.example from config.Settings so it cannot drift.

The 2026-09-01 review found four dead keys in the example and none of
the keys added in the last three months. The example is now derived,
never hand-edited: run this after changing config.py.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import Settings  # noqa: E402

SECRETY = re.compile(r"(token|key|secret|password)", re.I)


def render() -> str:
    lines = ["# Generated from config.py (Settings). Every field the worker",
             "# reads, with its default. Secrets are blank; set them in Railway.",
             "# Regenerate: py -3.12 scripts/gen_env_example.py", ""]
    for name, f in Settings.model_fields.items():
        default = f.default
        if SECRETY.search(name) or default in (None, ""):
            val = ""
        elif isinstance(default, bool):
            val = "true" if default else "false"
        else:
            val = str(default)
        desc = (f.description or "").strip()
        if desc:
            lines.append(f"# {desc}")
        lines.append(f"{name.upper()}={val}")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    Path(".env.example").write_text(render(), encoding="utf-8")
    print(f".env.example regenerated: {len(Settings.model_fields)} fields")

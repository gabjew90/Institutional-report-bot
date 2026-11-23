#!/usr/bin/env python3
"""Blocks a shell command a session must not run on the worker.

A PreToolUse hook on the Bash and PowerShell tools (.claude/settings.json).
The tool call arrives as JSON on standard input. Exit 2 blocks it and the text
on standard error goes back to the model. Any other outcome lets the command
run, a crash of this script included, so a fault here never blocks a session.

One rule, from a recorded failure (2026-10-06): `pkill -f` inside
`railway ssh` matched its own shell, killed the ssh session and left the probe
it was meant to stop running. `pkill` and `killall` take whatever matches.
Stop a process started over ssh by the PID recorded when it was started.

The database rule from the same audit is enforced in code, not here:
db.get_connection() refuses a second process on the live database.

Standard library only, Python 3.8 or later: it runs under whichever
interpreter the hook finds.
"""
import json
import re
import sys

# The two words anywhere in the command, because sessions here build the call
# from a variable (`R="env -u ... railway"; $R ssh "..."`) and pipe scripts to
# it. That also catches text that merely mentions all three words, which is
# the cheaper mistake: the message says how to pass text.
_RAILWAY = re.compile(r"\brailway(?:\.exe)?\b", re.I)
_SSH = re.compile(r"(?<![\w./-])ssh(?![\w-])")
_SWEEP = re.compile(r"(?<![\w./-])(pkill|killall)(?![\w-])")

MESSAGE = (
    "Blocked by scripts/command_guard.py: `{what}` in a command that opens a Railway shell. "
    "`pkill -f` on the worker matched its own ssh shell on 2026-10-06 and left the target "
    "running. Record the PID when you start a process over ssh (`echo $!`) and stop it with "
    "`kill <that pid>`. If the word is only text here (a commit message, a note), pass the text "
    "from a file."
)


def verdict(command: str):
    """The reason to block `command`, or None."""
    if not command or not _RAILWAY.search(command) or not _SSH.search(command):
        return None
    m = _SWEEP.search(command)
    if not m:
        return None
    return MESSAGE.format(what=m.group(1))


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        command = str((payload.get("tool_input") or {}).get("command") or "")
        reason = verdict(command)
    except Exception:
        return 0
    if reason:
        print(reason, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

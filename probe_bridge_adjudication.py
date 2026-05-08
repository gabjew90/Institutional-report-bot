"""Probe: verify github_bridge.jobs._process_one_pulse correctly handles the
matching adjudication JSON in three scenarios:

  1. Pending pulse + matching valid adjudication        -> archive both, embed in raw_json
  2. Pending pulse + no adjudication                    -> archive pulse only, no adjudication key
  3. Pending pulse + malformed adjudication             -> archive pulse, archive raw adjudication, log warning, raw_json has no adjudication key

Run:  python probe_bridge_adjudication.py
Expected output ends with: 'PROBE PASSED'
Any AssertionError or unexpected exception = failure.

Stubs the github_bridge.client functions and the discord/db layers in-memory.
"""

import asyncio
import json
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

# --- in-memory fakes ---------------------------------------------------------

class FakeBridge:
    """In-memory stand-in for github_bridge.client functions."""
    def __init__(self) -> None:
        self.files: dict[str, str] = {}
        self.deleted: list[str] = []
        self.put_calls: list[tuple[str, str]] = []

    def get_file_text(self, path: str, ref: str | None = None) -> str | None:
        return self.files.get(path)

    def put_file(self, path: str, content: str, message: str, ref: str | None = None) -> dict:
        self.files[path] = content
        self.put_calls.append((path, content))
        return {"commit": {"sha": "deadbeef"}}

    def delete_file(self, path: str, message: str, ref: str | None = None) -> dict | None:
        if path in self.files:
            del self.files[path]
            self.deleted.append(path)
            return {"commit": {"sha": "deadbeef"}}
        return None


class FakeChannel:
    name = "test-channel"

    async def send(self, *args, **kwargs):
        return None


class FakeBot:
    def get_channel(self, cid):
        return FakeChannel()


# --- patch hooks -------------------------------------------------------------

import github_bridge.jobs as jobs_mod
import github_bridge.client as client_mod
import db as db_mod
from config import settings

# Track DB inserts
inserted_reports: list[dict] = []

def fake_insert_daily_report(**kwargs):
    inserted_reports.append(kwargs)
    return 999

def fake_mark_report_sent(report_id: int):
    pass

# Stub the synthesizer footer-stats path (it queries the real DB)
def fake_compute_footer_stats():
    return {"pdf_count": 0, "top_sources": [], "priority_mix": {}}

# Stub send_embeds (avoid Discord)
async def fake_send_embeds(channel, embeds):
    return True

# Stub format_report_embeds (avoid building real embeds -- just return a list)
def fake_format_report_embeds(report):
    return ["embed-stub"]


def install_stubs(fake: FakeBridge):
    # gh IS github_bridge.client (alias inside jobs_mod). Patch its functions
    # via the alias -- same effect as patching client_mod directly.
    jobs_mod.gh.get_file_text = fake.get_file_text
    jobs_mod.gh.put_file = fake.put_file
    jobs_mod.gh.delete_file = fake.delete_file
    # format_report_embeds and send_embeds are imported as bare names into
    # jobs_mod. Python resolves these against jobs_mod's symbol table at
    # call time, so patching the source module would NOT take effect -- we
    # must patch the bound names on jobs_mod itself.
    jobs_mod.format_report_embeds = fake_format_report_embeds
    jobs_mod.send_embeds = fake_send_embeds
    # db is `import db` (module-level), so attribute lookup goes through
    # the module -- patching db_mod is fine.
    db_mod.insert_daily_report = fake_insert_daily_report
    db_mod.mark_report_sent = fake_mark_report_sent
    jobs_mod._compute_footer_stats = fake_compute_footer_stats
    # Force a single-channel config so the loop runs once.
    # discord_channel_ids is a @property derived from discord_channel_id (str).
    settings.discord_channel_id = "12345"


# --- scenario runners --------------------------------------------------------

PULSE_NAME = "2026-05-07T13-00.md"
PULSE_MD = """---
pdf_count: 187
input_tokens: 350000
output_tokens: 5200
---

# Pulse content placeholder
"""

VALID_ADJ = {
    "pulse_date": "2026-05-07",
    "window_label": "since-last-daily (2026-05-06 13:00)",
    "themes": [
        {
            "theme": "hormuz oil shock",
            "selected": True,
            "stance_counts": {"supportive": 5, "skeptical": 1, "neutral": 3},
            "consensus_view": "Supply scarcity priced; cuts coming Q2.",
            "facts_agreed": [],
            "facts_contested": [],
            "falsifiable_predictions": [],
        }
    ],
    "discarded_themes": [],
}


async def run_scenario(label: str, include_adj: bool, malformed: bool) -> None:
    print(f"\n--- scenario: {label} ---")
    inserted_reports.clear()

    fake = FakeBridge()
    fake.files[f"{jobs_mod.PENDING_DIR}/{PULSE_NAME}"] = PULSE_MD
    if include_adj:
        adj_payload = "{this is not json" if malformed else json.dumps(VALID_ADJ)
        fake.files[f"{jobs_mod.PENDING_ADJUDICATIONS_DIR}/2026-05-07T13-00.json"] = adj_payload

    install_stubs(fake)

    item = {"name": PULSE_NAME, "type": "file"}
    await jobs_mod._process_one_pulse(FakeBot(), item)

    # Assertions ---------------------------------------------------------------
    archive_path = f"{jobs_mod.ARCHIVE_DIR}/{PULSE_NAME}"
    pending_path = f"{jobs_mod.PENDING_DIR}/{PULSE_NAME}"
    adj_archive_path = f"{jobs_mod.ARCHIVE_ADJUDICATIONS_DIR}/2026-05-07T13-00.json"
    adj_pending_path = f"{jobs_mod.PENDING_ADJUDICATIONS_DIR}/2026-05-07T13-00.json"

    assert archive_path in fake.files, f"pulse markdown not archived for scenario {label}"
    assert pending_path in fake.deleted, f"pending pulse not deleted for scenario {label}"
    assert len(inserted_reports) == 1, f"expected exactly 1 db insert, got {len(inserted_reports)}"
    raw_json = json.loads(inserted_reports[0]["report_json"])
    print(f"  raw_json keys: {sorted(raw_json.keys())}")

    if include_adj and not malformed:
        assert "adjudication" in raw_json, f"valid adj missing from raw_json in scenario {label}"
        assert raw_json["adjudication"]["pulse_date"] == "2026-05-07"
        assert adj_archive_path in fake.files, f"adjudication not archived for scenario {label}"
        assert adj_pending_path in fake.deleted, f"pending adjudication not deleted for scenario {label}"
        print("  [OK] adjudication archived + embedded in raw_json")
    elif include_adj and malformed:
        assert "adjudication" not in raw_json, f"malformed adj should not be in raw_json"
        # We still archive the raw form for inspection
        assert adj_archive_path in fake.files, f"raw malformed adj not archived for scenario {label}"
        print("  [OK] malformed adjudication: raw archived, raw_json clean")
    else:
        assert "adjudication" not in raw_json, f"raw_json should not have adjudication key"
        assert adj_archive_path not in fake.files, f"no adjudication should have been archived"
        print("  [OK] no adjudication: pulse unaffected")


async def main() -> None:
    await run_scenario("valid adjudication present", include_adj=True, malformed=False)
    await run_scenario("no adjudication file", include_adj=False, malformed=False)
    await run_scenario("malformed adjudication file", include_adj=True, malformed=True)
    print("\nPROBE PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print(f"\nPROBE FAILED: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"\nPROBE FAILED (unexpected): {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(2)

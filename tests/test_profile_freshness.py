"""Profiles read by /ask say when a member went quiet and carry their current
name (2026-10-08 audit), and the profile writer sees who a message was for."""
import subprocess
import sys

import db
from db_parts.summaries import _latest_display_name, _stale_note


def test_a_quiet_member_is_marked():
    assert _stale_note("2026-04-29T10:00:00").startswith("last active 2026-04-29")
    assert _stale_note(None) == ""


def test_the_latest_chat_name_wins():
    conn = db.get_connection()
    for i, (name, ts) in enumerate((("Swedski Lincon", "2026-05-01T10:00:00"),
                                    ("Jake Pos", "2026-08-16T10:00:00"))):
        conn.execute("INSERT INTO chat_messages (discord_message_id, channel_id, channel_name, "
                     "author_id, author_username, author_display, content, posted_at) "
                     "VALUES (?, 1, 'x', 920001, 'coronaboy9109', ?, 'hi', ?)", (930000 + i, name, ts))
    conn.commit()
    assert _latest_display_name(920001) == "Jake Pos"


_CODE = """
import sys
sys.path.insert(0, '.')
from scripts import backfill_user_profiles as B
B._NAME_BY_ID['264777559026171905'] = '2Pale'
msgs = [
    {'timestamp': '2026-10-06T18:06:00', 'channel_name': 's', 'image_count': 0,
     'content': '<@264777559026171905> I smoke a pack of 2 pales', 'embed_texts': [],
     'image_ocr_text': ''},
    {'timestamp': '2026-10-06T18:07:00', 'channel_name': 's', 'image_count': 0,
     'content': 'So you made 30k on the cowboys', 'embed_texts': [], 'image_ocr_text': '',
     'reply_to': 'trieukha500'},
]
print(B._format_messages_block(msgs))
"""


def test_messages_show_who_they_were_for():
    # a subprocess: the builder rewraps stdout when imported
    out = subprocess.run([sys.executable, "-c", _CODE], capture_output=True, text=True,
                         encoding="utf-8", check=True).stdout
    assert "(to @2Pale) @2Pale I smoke" in out
    assert "(replying to trieukha500) So you made 30k" in out

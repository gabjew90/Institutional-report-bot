"""Analyst trade-log subsystem.

Watches a designated Discord channel (settings.analyst_channel_name) where
the group's analyst posts Robinhood notification cards / stats screens. For
each new message with image attachments, runs Gemini vision via
`analyst_log.ocr.extract_trade_from_image` and writes a structured row to
the `analyst_trades` table.

Calibration history: prompts in `ocr.py` were tuned across 3 probe runs
against the live channel (see scripts/test_analyst_ocr.py and the commit
history of 9112ad4). Final run hit 13/13 action accuracy with conservative
bias toward "viewing" for ambiguous opens.

The /ask bot reads from `analyst_trades` to flavor its replies with the
analyst's recent activity.
"""

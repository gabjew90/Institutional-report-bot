"""Verify the multi-hop fix recovers BK's row #333.

Re-runs extract_trade_from_caption with the parent_caption that the
NEW multi-hop _fetch_reply_parent_caption would have produced.
Confirms Gemini correctly resolves the ticker from the L2 context."""
import asyncio, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analyst_log.ocr import extract_trade_from_caption


async def main():
    # What the close caption actually says (no ticker, no strike):
    L0_CAPTION = "Sold @1.12, +40%"
    # What the multi-hop walk now surfaces from L2:
    PARENT_CAPTION_NEW = "CRCL 110c @0.8 SLAMMMMM"
    # What the OLD single-hop walk surfaced from L1 (empty):
    PARENT_CAPTION_OLD = None

    print("=== OLD single-hop (L1 was empty) ===")
    old = await extract_trade_from_caption(
        L0_CAPTION,
        parent_caption=PARENT_CAPTION_OLD,
        caller_name="BK",
    )
    print(f"  result: {old}")
    print()
    print("=== NEW multi-hop (walks to L2 entry text) ===")
    new = await extract_trade_from_caption(
        L0_CAPTION,
        parent_caption=PARENT_CAPTION_NEW,
        caller_name="BK",
    )
    print(f"  result: {new}")


asyncio.run(main())

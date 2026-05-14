"""Upload a file to litterbox.catbox.moe and print the URL.

litterbox is a temporary file host (catbox.moe family). HTML files render
in-browser via the returned URL — works on phones, no auth, no repo
visibility dependency.

Retention options: 1h, 12h, 24h, 72h. Default 24h.

Uses the `requests` library (instead of urllib) so the upload runs through
certifi's bundled CA store rather than the system cert store. On Windows
boxes with TLS-intercepting antivirus, the system store fails verification
on litterbox.catbox.moe; certifi sidesteps it cleanly without weakening
TLS — same security guarantees as the default verification, just with a
different trust root.

Usage:
    python scripts/litterbox_upload.py path/to/file.html
    python scripts/litterbox_upload.py path/to/file.html --time 72h
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import requests


LITTERBOX_API = "https://litterbox.catbox.moe/resources/internals/api.php"
VALID_TIMES = ("1h", "12h", "24h", "72h")


def upload_to_litterbox(file_path: Path, retention: str = "24h") -> str:
    """Upload a single file, return the public URL."""
    if retention not in VALID_TIMES:
        raise ValueError(f"retention must be one of {VALID_TIMES}, got {retention!r}")
    if not file_path.exists():
        raise FileNotFoundError(file_path)

    with file_path.open("rb") as f:
        resp = requests.post(
            LITTERBOX_API,
            data={"reqtype": "fileupload", "time": retention},
            files={"fileToUpload": (file_path.name, f)},
            timeout=60,
        )
    resp.raise_for_status()
    body = resp.text.strip()
    if not body.startswith("http"):
        raise RuntimeError(f"unexpected response from litterbox: {body!r}")
    return body


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path)
    parser.add_argument("--time", default="24h", choices=VALID_TIMES)
    args = parser.parse_args()
    url = upload_to_litterbox(args.file, retention=args.time)
    print(url)
    return 0


if __name__ == "__main__":
    sys.exit(main())

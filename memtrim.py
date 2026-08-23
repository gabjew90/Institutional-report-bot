"""Return freed heap memory to the OS (glibc malloc_trim).

WHY (2026-08-23): Railway bills RAM by GB-hour. The worker boots at
~180 MB and ratchets to ~450 MB anon within hours — the high-water mark
of the heaviest periodic job (profile refresh, catchup, PDF batch) —
and never comes back down, because glibc keeps freed memory inside the
process. MALLOC_ARENA_MAX=2 (set 2026-07-23) limits fragmentation but
does not RETURN memory. malloc_trim(0) does. Called after heavy jobs
and on a 15-minute safety tick; ~$3-4/month at the observed averages.

No-op on non-glibc platforms (Windows dev, musl) — never raises.
"""

import ctypes
import logging

log = logging.getLogger(__name__)

_libc = None
_unavailable = False


def trim() -> int:
    """malloc_trim(0). Returns 1 if memory was released, 0 if not, -1
    when unavailable on this platform."""
    global _libc, _unavailable
    if _unavailable:
        return -1
    try:
        if _libc is None:
            _libc = ctypes.CDLL("libc.so.6")
        return int(_libc.malloc_trim(0))
    except Exception as e:  # OSError on non-glibc, AttributeError on musl
        _unavailable = True
        log.debug(f"malloc_trim unavailable: {e}")
        return -1


def rss_mb() -> float | None:
    """Current RSS in MB from /proc (Linux only), for before/after logs."""
    try:
        with open("/proc/self/status", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except Exception:
        return None
    return None


def trim_and_log(label: str) -> None:
    before = rss_mb()
    rc = trim()
    after = rss_mb()
    if rc >= 0 and before is not None and after is not None:
        log.info(f"malloc_trim[{label}]: {before:.0f} MB -> {after:.0f} MB")

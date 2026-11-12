"""Process limits that must be set before numpy is first imported.

Import this module first in every entry point (main.py,
scheduler/batch_main.py) and from config.py, which everything else imports.

BLAS threads (2026-11-11). numpy's OpenBLAS starts one thread per CPU it
can see. The container sees the host's 48 cores, and its CPU quota is 8
(`/sys/fs/cgroup/cpu.max` = 800000 100000). The theme clusterer makes
thousands of small matrix products in a loop, and between calls every
OpenBLAS thread spins waiting for the next one, so 48 cores of spin burned
the quota in the first sixth of each 100 ms period and the kernel froze the
whole container for the rest of it. Both processes share that quota, so
the bot froze with the batch process.

Measured on the worker on 2026-11-12, 43 hours after boot:
  cpu.stat      nr_throttled 4813, throttled_usec 13,515 s
  cpu.pressure  full total 352 s
  logged        "process paused ... no Python thread could run": 373 s,
                in two episodes, each minutes after a bulk research arrival
While the batch process was frozen mid-transaction it held the database's
write lock, and the bot's writes failed after 30 s ("Database lock" in the
ops channel: three chat messages, a tagger run, a feed post). This is also
the stall of 2026-10-29 and 11-02; the clusterer moved to numpy on 10-27.

The products here are a few thousand rows by 3,072 columns. One or two
threads finish them in milliseconds.
"""
from __future__ import annotations

import os

BLAS_THREADS = "2"
_VARS = ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS",
         "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS")


def apply() -> None:
    """Set each limit unless the environment already carries one. A child
    process inherits them, so the batch process is covered twice."""
    for name in _VARS:
        os.environ.setdefault(name, BLAS_THREADS)


apply()

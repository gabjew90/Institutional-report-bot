"""Daily token budget tracker for Gemini API spend.

WHY THIS EXISTS
================
CLAUDE.md documents that a $10 monthly spend cap was hit in production
(line 228). The system has no code-level protection against:

  - One 400-page PDF burning 80K+ tokens in deep analysis (~$32 in
    output tokens vs. the budget's ~$0.50/day)
  - An ambiguous /ask question that loops the tool-call agent and
    burns 180K tokens on one call
  - A profile refresh firing simultaneously with the 9 AM pulse,
    both using the same google_api_key, both contributing to the
    same daily quota

This module provides a SINGLE process-wide counter that callers
register their token usage against. When the daily budget is
exceeded, `BudgetExceeded` is raised before the call fires.

USAGE
=====
Wrap a Gemini call site:

    from ai_analysis.token_budget import get_budget, BudgetExceeded

    try:
        get_budget().reserve_or_raise(
            estimated_tokens=15_000,
            caller="pdf_deep_analysis",
        )
        response = await client.aio.models.generate_content(...)
        actual = (response.usage_metadata.total_token_count or 0)
        get_budget().record_actual(
            estimated=15_000,
            actual=actual,
            caller="pdf_deep_analysis",
        )
    except BudgetExceeded as e:
        log.warning(f"PDF deep analysis skipped — budget: {e}")
        # caller decides whether to mark FAILED, retry tomorrow, etc.

Two stages: `reserve_or_raise` checks BEFORE the call (cheap probe;
prevents the call from firing if it would push over budget).
`record_actual` corrects the reservation after the call with the real
usage from `usage_metadata`.

STATE
=====
Counter resets at UTC midnight. **PROCESS-MEMORY ONLY** (docstring
corrected 2026-08-20 — it previously claimed DB persistence that was
never implemented): a worker restart mid-day resets the counter to
zero, and a separately-spawned process gets its own fresh budget.
Only hard-stop rejections are written to `pipeline_events`, as an
audit trail, not as shared state. Treat this as a soft cap against
runaway single-process loops, NOT a durable daily spend ceiling —
the real ceiling is the spend cap at ai.studio/spend.

The budget is configurable via env var `GEMINI_DAILY_TOKEN_BUDGET`
(default: 4_000_000 tokens — ~$1.60/day at Flash Lite input prices,
generous enough for normal operation while still blocking a 400-page
PDF runaway).

HARD-STOP RULE
==============
At 100% of budget: `reserve_or_raise` raises BudgetExceeded.
At 90% of budget: log a WARNING but allow the call (yellow zone).

After hard-stop, calls remain blocked until UTC midnight rollover.
The caller decides recovery (retry tomorrow, queue for manual
re-run, etc.).
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import threading
from typing import Optional

log = logging.getLogger(__name__)


class BudgetExceeded(Exception):
    """Raised when reserving tokens would exceed the daily budget."""


class TokenBudget:
    """Process-wide daily token counter.

    Thread-safe via a single Lock; expects relatively low contention
    (Gemini calls are async and don't fire at >10/sec from one process).
    """

    DEFAULT_BUDGET = 4_000_000  # tokens/day (~$1.60 at Flash Lite input)

    def __init__(self, daily_budget: int | None = None):
        env_budget = os.environ.get("GEMINI_DAILY_TOKEN_BUDGET")
        if daily_budget is None:
            if env_budget:
                try:
                    daily_budget = int(env_budget)
                except ValueError:
                    log.warning(
                        f"Invalid GEMINI_DAILY_TOKEN_BUDGET={env_budget!r}; "
                        f"falling back to {self.DEFAULT_BUDGET}"
                    )
                    daily_budget = self.DEFAULT_BUDGET
            else:
                daily_budget = self.DEFAULT_BUDGET
        self._daily_budget = int(daily_budget)
        self._lock = threading.Lock()
        # Tuple of (UTC date string, reserved_so_far, recorded_actual).
        # When the date rolls over, reset both counters.
        today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
        self._state_date = today
        self._reserved = 0
        self._actual = 0

    def _rollover_if_needed(self) -> None:
        """Reset counters at UTC midnight. Caller MUST hold `_lock`."""
        today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
        if today != self._state_date:
            log.info(
                f"Token budget rollover: yesterday used "
                f"{self._actual:,} actual / {self._reserved:,} reserved "
                f"vs {self._daily_budget:,} budget. New day starting clean."
            )
            self._state_date = today
            self._reserved = 0
            self._actual = 0

    @property
    def daily_budget(self) -> int:
        return self._daily_budget

    @property
    def reserved_so_far(self) -> int:
        with self._lock:
            self._rollover_if_needed()
            return self._reserved

    @property
    def actual_so_far(self) -> int:
        with self._lock:
            self._rollover_if_needed()
            return self._actual

    def reserve_or_raise(
        self,
        *,
        estimated_tokens: int,
        caller: str,
    ) -> None:
        """Check if we can afford `estimated_tokens` before firing the
        call. Raises BudgetExceeded when we'd hit the hard stop.

        At 90-99% of budget, logs a WARNING but allows the call (the
        actual usage might come in lower than the estimate).

        At 100%, raises.
        """
        with self._lock:
            self._rollover_if_needed()
            projected = self._reserved + estimated_tokens
            pct = projected / self._daily_budget if self._daily_budget else 1.0
            if pct >= 1.0:
                # Hard stop. Record the rejection as a pipeline event
                # so the operator can see the cap firing.
                self._log_budget_event(
                    "budget_exceeded",
                    caller,
                    estimated_tokens,
                    reserved=self._reserved,
                    projected=projected,
                )
                raise BudgetExceeded(
                    f"daily Gemini token budget exhausted: "
                    f"reserved={self._reserved:,} + "
                    f"requested={estimated_tokens:,} = "
                    f"{projected:,} > budget={self._daily_budget:,}. "
                    f"caller={caller}. Resets at UTC midnight."
                )
            if pct >= 0.9:
                log.warning(
                    f"token budget yellow zone ({pct*100:.0f}%): "
                    f"caller={caller} reserved+req={projected:,} "
                    f"vs budget={self._daily_budget:,}"
                )
            self._reserved += estimated_tokens

    def record_actual(
        self,
        *,
        estimated: int,
        actual: int,
        caller: str,
    ) -> None:
        """After a call returns, correct the reservation with the
        actual usage from usage_metadata. If actual < estimated, free
        the difference back to the budget. If actual > estimated,
        absorb the overrun (we already let the call fire)."""
        if actual <= 0:
            # Some calls don't report usage_metadata reliably (tool-
            # calling middle rounds, retry shadow attempts). Treat as
            # actual = estimated so the reservation doesn't leak.
            actual = estimated
        with self._lock:
            self._rollover_if_needed()
            self._actual += actual
            # Free unused reservation back to budget so subsequent
            # callers see the corrected number.
            self._reserved -= max(0, estimated - actual)
            if self._reserved < 0:
                self._reserved = 0

    def snapshot(self) -> dict:
        with self._lock:
            self._rollover_if_needed()
            return {
                "date": self._state_date,
                "daily_budget": self._daily_budget,
                "reserved": self._reserved,
                "actual": self._actual,
                "pct_used": (
                    self._actual / self._daily_budget
                    if self._daily_budget else 0.0
                ),
            }

    def _log_budget_event(
        self,
        sub_kind: str,
        caller: str,
        estimated: int,
        *,
        reserved: int = 0,
        projected: int = 0,
    ) -> None:
        """Append to pipeline_events for cross-process visibility.
        Failure is non-fatal — the call is being rejected anyway, so
        DB unavailability shouldn't double the pain."""
        try:
            from db import record_pipeline_event
            record_pipeline_event(
                "token_budget",
                sub_kind,
                {
                    "caller": caller,
                    "estimated_tokens": estimated,
                    "reserved_before": reserved,
                    "projected_after": projected,
                    "daily_budget": self._daily_budget,
                    "date": self._state_date,
                },
            )
        except Exception as e:
            log.warning(f"token_budget pipeline_event write failed: {e}")


# Singleton instance — every caller in the process shares state.
_singleton: Optional[TokenBudget] = None
_singleton_lock = threading.Lock()


def get_budget() -> TokenBudget:
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = TokenBudget()
    return _singleton


def reset_for_tests() -> None:
    """Test-only: wipe the singleton so each test gets a fresh budget."""
    global _singleton
    with _singleton_lock:
        _singleton = None

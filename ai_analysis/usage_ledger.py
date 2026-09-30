"""One row per Gemini call, whoever made it (2026-09-29).

Until now only the PDF pipeline wrote down what it spent. The /ask
pipeline, the profile refresh, the alert-channel trade classifier, the
member trade batches, the screenshot OCR and the racism tagger all
called Gemini without a record, so the monthly bill could only be
split by feature with estimates from call counts. Every live client is
now built with `make_client(caller)`, which wraps the client's
generate_content (sync and async) so each response's usage_metadata
lands in `gemini_calls` with the caller's name. `db.gemini_spend(days)`
turns that into dollars per feature; /status shows the last 7 days.

Recording never raises: a ledger failure must not fail the call it
was measuring.

`as_caller(name)` narrows the label inside one client, e.g. the PDF
client records `pdf_triage` and `pdf_deep` separately.
"""
from __future__ import annotations

import contextvars
import logging

log = logging.getLogger(__name__)

_caller_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "gemini_caller", default="")

# USD per million tokens (input, output), AI Studio paid tier. Thinking
# tokens bill as output. Prefix match so a dated or -preview variant
# prices like its family; an unknown model prices at the 3.1 rates and
# is reported with its own name so the gap is visible.
PRICES_PER_M: dict[str, tuple[float, float]] = {
    "gemini-3.1-flash-lite": (0.25, 1.50),
    "gemini-3.5-flash-lite": (0.30, 2.50),
}
_DEFAULT_PRICE = (0.25, 1.50)


def price_per_m(model: str) -> tuple[float, float]:
    m = (model or "").lower()
    for k, v in PRICES_PER_M.items():
        if m.startswith(k):
            return v
    return _DEFAULT_PRICE


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    pi, po = price_per_m(model)
    return (input_tokens or 0) / 1e6 * pi + (output_tokens or 0) / 1e6 * po


def usage_of(response) -> dict | None:
    """Token counts from a response, or None when it carries none."""
    um = getattr(response, "usage_metadata", None)
    if um is None:
        return None
    g = lambda k: int(getattr(um, k, 0) or 0)  # noqa: E731
    return {
        "input_tokens": g("prompt_token_count"),
        "output_tokens": g("candidates_token_count") + g("thoughts_token_count"),
        "thinking_tokens": g("thoughts_token_count"),
        "cached_tokens": g("cached_content_token_count"),
    }


def record(caller: str, model: str, response, ref: str | None = None) -> None:
    try:
        u = usage_of(response)
        if not u:
            return
        import db
        db.record_gemini_call(caller=caller or "unknown", model=model or "",
                              ref=ref, **u)
    except Exception as e:  # never fail the measured call
        log.debug(f"gemini ledger: record failed ({e})")


class as_caller:
    """Label every call made inside the block `name`. Works under both
    `with` and `async with`, so it can share an `async with limiter, ...`
    line without re-indenting the call."""

    def __init__(self, name: str):
        self.name = name
        self._tok = None

    def __enter__(self):
        self._tok = _caller_var.set(self.name)
        return self

    def __exit__(self, *exc):
        _caller_var.reset(self._tok)
        return False

    async def __aenter__(self):
        return self.__enter__()

    async def __aexit__(self, *exc):
        return self.__exit__(*exc)


def _model_arg(args, kwargs) -> str:
    m = kwargs.get("model")
    if m is None and args:
        m = args[0]
    return str(m or "")


def instrument(client, caller: str):
    """Wrap `client.models.generate_content` and the aio twin so every
    response is recorded under `caller` (or the `as_caller` label)."""
    # Each side is wrapped only when present: a test double standing in
    # for genai.Client may carry neither, and instrumenting must never
    # be the reason a client fails to build.
    aio = getattr(getattr(client, "aio", None), "models", None)
    orig_async = getattr(aio, "generate_content", None)
    if orig_async is not None:
        async def gen_async(*args, **kwargs):
            resp = await orig_async(*args, **kwargs)
            record(_caller_var.get() or caller, _model_arg(args, kwargs), resp)
            return resp
        aio.generate_content = gen_async

    sync = getattr(client, "models", None)
    orig_sync = getattr(sync, "generate_content", None)
    if orig_sync is not None:
        def gen_sync(*args, **kwargs):
            resp = orig_sync(*args, **kwargs)
            record(_caller_var.get() or caller, _model_arg(args, kwargs), resp)
            return resp
        sync.generate_content = gen_sync

    try:
        client._ledger_caller = caller
    except Exception:
        pass
    return client


def make_client(caller: str, api_key: str | None = None):
    """A google-genai Client whose calls are recorded under `caller`."""
    from google import genai
    from config import settings
    return instrument(genai.Client(api_key=api_key or settings.google_api_key), caller)

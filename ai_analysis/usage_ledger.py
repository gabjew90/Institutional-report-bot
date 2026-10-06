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

The same wrappers adapt each request for Gemini 3.6 and later
(`modernize_config`, 2026-10-06): no sampling fields, a thinking level
instead of a budget. Older models get requests unchanged.
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


# Deprecated request parameters (Google notice, 2026-10-06). Gemini models
# before 3.6 still honour temperature/top_p/top_k and remap thinking_budget,
# and the bot's call sites set them on purpose (OCR at 0, retries warmer to
# break a repetition loop), so those models get requests unchanged. From
# 3.6 on the sampling fields do nothing, and newer models answer 400 to them
# and to thinking_budget, so a feature switched to one keeps working on day
# one: the guard drops the sampling fields and turns a budget into a level.
# The levels are a starting point, to be tuned when a feature switches.
_SAMPLING_FIELDS = ("temperature", "top_p", "top_k")
_LEGACY_BEFORE = (3, 6)
_logged_models: set[str] = set()


def _version(model: str) -> tuple[int, int] | None:
    import re
    m = re.match(r"(?:models/)?gemini-(\d+)(?:\.(\d+))?", (model or "").lower())
    return (int(m.group(1)), int(m.group(2) or 0)) if m else None


def needs_modern_params(model: str) -> bool:
    """True for Gemini 3.6 and later, and for an unversioned Gemini alias
    ("gemini-flash-latest"), which points at the newest model. Anything
    else is left alone."""
    v = _version(model)
    if v is None:
        name = (model or "").lower().removeprefix("models/")
        return name.startswith("gemini-")
    return v >= _LEGACY_BEFORE


def level_for_budget(budget: int | None) -> str | None:
    """None for a dynamic budget (-1) or none at all: the model default."""
    if budget is None or budget < 0:
        return None
    if budget == 0:
        return "MINIMAL"
    if budget <= 1024:
        return "LOW"
    if budget <= 8192:
        return "MEDIUM"
    return "HIGH"


def _get(obj, key):
    return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)


def _with(obj, update: dict):
    if isinstance(obj, dict):
        out = dict(obj)
        out.update(update)
        return {k: v for k, v in out.items() if v is not None}
    return obj.model_copy(update=update)


def modernize_config(model: str, config):
    """The request config the model accepts. Unchanged for models before
    3.6 and for unknown names; never raises (the call then goes as built)."""
    if config is None or not needs_modern_params(model):
        return config
    try:
        update = {f: None for f in _SAMPLING_FIELDS if _get(config, f) is not None}
        tc = _get(config, "thinking_config")
        if tc is not None and _get(tc, "thinking_budget") is not None:
            level = _get(tc, "thinking_level") or level_for_budget(_get(tc, "thinking_budget"))
            if isinstance(tc, dict):
                update["thinking_config"] = _with(tc, {"thinking_budget": None,
                                                       "thinking_level": level})
            else:
                # rebuilt, not copied, so the level string is validated
                # into the SDK's enum
                fields = {k: v for k, v in tc.model_dump(exclude_none=True).items()
                          if k not in ("thinking_budget", "thinking_level")}
                if level is not None:
                    fields["thinking_level"] = level
                update["thinking_config"] = type(tc)(**fields)
        if not update:
            return config
        if model not in _logged_models:
            _logged_models.add(model)
            log.info(f"gemini params: {model} gets no sampling fields and a "
                     f"thinking level instead of a budget")
        return _with(config, update)
    except Exception as e:
        log.warning(f"gemini params: could not adapt the config for {model}: {e}")
        return config


def _adapt(args, kwargs):
    model = _model_arg(args, kwargs)
    if "config" in kwargs:
        kwargs = {**kwargs, "config": modernize_config(model, kwargs["config"])}
    elif len(args) >= 3:
        args = (*args[:2], modernize_config(model, args[2]), *args[3:])
    return args, kwargs


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
            args, kwargs = _adapt(args, kwargs)
            resp = await orig_async(*args, **kwargs)
            record(_caller_var.get() or caller, _model_arg(args, kwargs), resp)
            return resp
        aio.generate_content = gen_async

    sync = getattr(client, "models", None)
    orig_sync = getattr(sync, "generate_content", None)
    if orig_sync is not None:
        def gen_sync(*args, **kwargs):
            args, kwargs = _adapt(args, kwargs)
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

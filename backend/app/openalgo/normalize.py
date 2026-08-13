"""One response shape for every OpenAlgo call, and safe serialization.

OpenAlgo itself is not uniform. envelope() collapses the four shapes the API actually
uses into a single dict so that no caller anywhere has to know which one it got:

    {status, data}              most reads
    {status, orderid}           order writes
    {status, results: [...]}    batch writes
    {status, message}           errors and simple acknowledgements

The collapsed shape is {ok, source, data, error, kind, mode}. On success the payload
sits under `data` and `error`/`kind` are absent; on failure `error` carries the
message, `kind` the error type, and `data` is absent. `mode` is present whenever the
server reported one - it is how analyzer mode announces itself, and an order that
came back tagged "analyze" did not reach a broker.

Rules that produced the rest of this module, confirmed against agno 2.8.7:

  - Errors are RETURNED, never raised. `if response:` is not a success test, because
    an error dict is truthy. Always inspect status - is_success() or envelope().

  - A falsy return becomes an EMPTY tool message, which derails the model. Every path
    through to_json() returns a non-empty string.

  - NaN is not valid JSON. Everything goes through _clean() before json.dumps.

  - Agno never truncates a tool result and calls str() on the return value, so the
    size cap has to happen here. to_json() caps at 12,000 characters and NEVER cuts
    mid-string: it returns a well-formed object describing the overflow instead,
    because handing a model a JSON fragment is worse than handing it a refusal.

The Planner is the only LLM in AutoAgent and it reads, it does not trade. That does
not make this module optional: the Planner's inputs are exactly these envelopes, and
a malformed one at 08:45 means no plan, which means no session.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

MAX_TOOL_CHARS = 12_000

# Room reserved for the truncation envelope's own keys and note, so the wrapper does
# not push the result back over the limit it exists to enforce.
_TRUNCATION_OVERHEAD = 400


def _clean(obj: Any) -> Any:
    """Recursively replace NaN/Infinity with None so json.dumps produces valid JSON."""
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    return obj


def to_json(payload: Any, limit: int = MAX_TOOL_CHARS) -> str:
    """Serialize and hard-cap. Never returns an empty or falsy string."""
    if isinstance(payload, str):
        text = payload
    else:
        text = json.dumps(_clean(payload), default=str, separators=(",", ":"),
                          ensure_ascii=False)
    if not text:
        return '{"ok":false,"error":"no_data"}'
    if len(text) > limit:
        # Cutting mid-string would hand the model malformed JSON, so the overflow is
        # reported as a well-formed object instead. A caller hitting this is a design
        # problem in that caller: shape the payload down before it gets here.
        #
        # The slice is sized so the WHOLE returned string honours the limit, not just
        # the payload inside it. TradingAgent kept text[:limit] and then wrapped it,
        # which returned about 12,320 characters against a 12,000 cap - a cap that is
        # exceeded is not a cap. Escaping means the serialized length of partial_text
        # is not its raw length, so the fit is converged on rather than computed.
        keep = max(0, limit - _TRUNCATION_OVERHEAD)
        for _ in range(4):
            out = json.dumps({
                "ok": True,
                "truncated": True,
                "dropped_chars": len(text) - keep,
                "note": f"Result exceeded {limit} characters and was cut. Narrow the "
                        "request (filter, or ask for fewer items) to see all of it.",
                "partial_text": text[:keep],
            }, ensure_ascii=False)
            if len(out) <= limit or keep == 0:
                return out
            keep = max(0, keep - (len(out) - limit))
        return out
    return text


def ok(data: Any, source: str, **extra: Any) -> dict:
    return {"ok": True, "source": source, "data": data, **extra}


def err(message: str, source: str, kind: str | None = None, **extra: Any) -> dict:
    return {"ok": False, "source": source, "error": message, "kind": kind, **extra}


def is_success(raw: Any) -> bool:
    """The only correct success test for a raw SDK response."""
    return isinstance(raw, dict) and str(raw.get("status", "")).lower() == "success"


def envelope(raw: Any, source: str) -> dict:
    """Collapse any OpenAlgo response into {ok, source, data, error, kind, mode}.

    Args:
        raw: Whatever the SDK returned - dict, DataFrame, or None.
        source: The method or endpoint name, carried through for logging.

    Returns:
        The collapsed envelope. Never raises.
    """
    if raw is None:
        return err("empty response", source)

    if not isinstance(raw, dict):
        # history() and instruments() return a DataFrame on success and a dict on
        # error, so a non-dict here is the success path. Callers that want a frame
        # should use frames.py rather than unwrapping this.
        return ok(raw, source)

    mode = raw.get("mode")
    status = str(raw.get("status", "")).lower()

    if status and status != "success":
        message = raw.get("message") or raw.get("error") or "request failed"
        return err(str(message), source, kind=raw.get("error_type"), mode=mode)

    if "data" in raw:
        payload = raw["data"]
    elif "results" in raw:
        payload = {"results": raw["results"],
                   **{k: v for k, v in raw.items()
                      if k not in ("status", "results", "mode")}}
    else:
        # Order writes are flat: {status, orderid}. Keep every field except the two
        # that have already been consumed, so `orderid` survives.
        payload = {k: v for k, v in raw.items() if k not in ("status", "mode")}

    out = ok(payload, source)
    if mode is not None:
        out["mode"] = mode
    return out


def frame_summary(df, last_n: int = 5) -> dict:
    """Compact a candle DataFrame into something worth sending to a model.

    Returning 4,698 raw candles - the measured size of one 90-day 5m history call -
    is both useless and expensive. Compute server-side, send conclusions plus a small
    tail.

    Args:
        df: A cleaned OHLCV frame, indexed by timestamp.
        last_n: How many trailing bars to include verbatim.

    Returns:
        A JSON-safe summary dict.
    """
    if df is None or len(df) == 0:
        return {"bars": 0}
    cols = [c for c in ("open", "high", "low", "close", "volume", "oi") if c in df.columns]
    tail = df.tail(last_n)[cols]
    out = {
        "bars": int(len(df)),
        "first_timestamp": str(df.index[0]),
        "last_timestamp": str(df.index[-1]),
        "columns": cols,
        "last_close": float(df["close"].iloc[-1]) if "close" in df else None,
        "period_high": float(df["high"].max()) if "high" in df else None,
        "period_low": float(df["low"].min()) if "low" in df else None,
        "recent": [
            {"timestamp": str(idx), **{c: (None if _isnan(row[c]) else float(row[c]))
                                       for c in cols}}
            for idx, row in tail.iterrows()
        ],
    }
    if "close" in df and len(df) > 1:
        first, last = float(df["close"].iloc[0]), float(df["close"].iloc[-1])
        if first:
            out["period_change_pct"] = round((last - first) / first * 100, 2)
    return out


def _isnan(v: Any) -> bool:
    try:
        return math.isnan(float(v))
    except (TypeError, ValueError):
        return False


# --- Output hygiene ---------------------------------------------------------
#
# Planner output reaches the mandate card, the journal and the alert push, so it is
# normalised on the way out rather than merely discouraged in the prompt.
# Instructions are layer 1 and unreliable; this is deterministic. Three things get
# cleaned:
#
#   - typographic dashes and quotes, which look wrong in a plain terminal-styled UI
#     and are impossible to type back into a search box,
#   - emoji and pictographs, which the project bans everywhere,
#   - CJK characters, which for an Indian trading agent only ever appear as a defect.
#
# Currency symbols, accents and the rupee sign are deliberately left alone.

_PUNCTUATION_MAP = {
    "—": "-",   # em dash
    "–": "-",   # en dash
    "‒": "-",   # figure dash
    "―": "-",   # horizontal bar
    "−": "-",   # minus sign
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "…": "...",
    " ": " ",   # non-breaking space
    " ": " ", " ": " ", "​": "",
}
_PUNCTUATION_RE = re.compile("|".join(map(re.escape, _PUNCTUATION_MAP)))

_STRIP_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"   # emoji, pictographs, symbols
    "\U0001F000-\U0001F2FF"
    "☀-➿"           # misc symbols and dingbats
    "️⃣"            # variation selector, keycap
    "　-〿"           # CJK punctuation
    "一-鿿"           # CJK unified ideographs
    "぀-ヿ"           # hiragana, katakana
    "가-힯"           # hangul
    "]+"
)


def sanitize_text(text: str) -> str:
    """Normalise a chunk of model output. Safe to call per streaming delta."""
    if not text:
        return text
    cleaned = _PUNCTUATION_RE.sub(lambda m: _PUNCTUATION_MAP[m.group()], text)
    return _STRIP_RE.sub("", cleaned)

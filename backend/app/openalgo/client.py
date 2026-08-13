"""Shared OpenAlgo client.

Facts this module exists to encapsulate, all verified against openalgo 2.0.3:

  - The SDK is fully synchronous (httpx.Client plus OS threads for the websocket
    feed). Calling it from the FastAPI event loop stalls every other request for the
    length of a broker round trip, so acall() offloads to a worker thread. The
    executor is a plain thread and calls the sync methods directly.

  - Errors are RETURNED as dicts, never raised. A truthy response is NOT a success:
    an error dict is truthy too. Always inspect status, or let envelope() do it.

  - history() and instruments() return a DataFrame on success but a dict on error, so
    the type must be checked before touching .tail(). frames.py does that check once,
    at the boundary, and nothing downstream should repeat it.

  - BaseAPI sets base_url = f"{host}/api/{version}/" - already slash-terminated - and
    _make_request does a bare string concat. An endpoint name with a leading slash
    produces "/api/v1//ping", which the server answers with a 308 that the SDK's
    httpx.Client does not follow (follow_redirects defaults to False). The call then
    returns {"status": "error", "message": "HTTP 308: <!doctype html>..."} instead of
    failing loudly - a fake error containing HTML. raw_post() strips leading slashes
    for exactly this reason.

  - price_type vs pricetype. The top-level SDK kwarg is `price_type`. Inside the legs
    of a basket or margin payload the server wants `pricetype`, no underscore, and a
    wrong spelling in a leg is forwarded verbatim, ignored, and the order silently
    drops to MARKET. AutoAgent sends single orders only and therefore always uses
    `price_type`.

  - Request field is `order_id`; the response field is `orderid`. They do not match.

  - The SDK default timeout is 120s. A system that must decide on a 5-minute bar
    close cannot wait two minutes for a quote, so the timeout comes from settings.

  - `strategy` defaults to "Python" inside the SDK. With no client order id available
    (verified at build step 0), the strategy tag plus symbol, side, quantity and a
    time window IS the reconciliation key against the broker's own orderbook. An
    order that goes out tagged "Python" is an order that cannot be attributed, so
    every strategy-bearing call gets DEFAULT_STRATEGY injected when the caller did
    not name one.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any

from openalgo import api

from ..config import Settings, get_settings
from .normalize import envelope, err

log = logging.getLogger(__name__)

# Fallback attribution tag. Overridden by settings.default_strategy_name when the
# config layer supplies one; the per-strategy id (stg1_ema_10_20 and friends) should
# be passed explicitly by the executor so a trade is attributable to its strategy.
DEFAULT_STRATEGY = "AutoAgent"

# Endpoints the SDK does not wrap, reachable only through raw_post.
UNWRAPPED_ENDPOINTS: tuple[str, ...] = ("ping", "pnl/symbols")

# SDK methods that consume the order rate limit. Used to pick the right token bucket
# automatically, because an unattended executor that forgets _order=True would breach
# the documented 10 orders/sec ceiling with no human there to notice.
ORDER_METHODS: frozenset[str] = frozenset({
    "placeorder", "placesmartorder", "modifyorder", "cancelorder",
    "cancelallorder", "closeposition", "basketorder", "splitorder",
})

# SDK methods that accept a `strategy` kwarg. Anything not listed here would raise on
# an unexpected keyword, so injection is opt-in by name rather than blanket.
STRATEGY_METHODS: frozenset[str] = frozenset({
    "placeorder", "placesmartorder", "modifyorder", "cancelorder", "orderstatus",
    "openposition", "closeposition", "cancelallorder", "basketorder", "splitorder",
})


class RateLimiter:
    """Token bucket. OpenAlgo defaults are 50 req/s overall and 10 orders/s.

    Fix carried over from TradingAgent: the original slept WHILE HOLDING THE LOCK,
    which serialised every waiting thread behind the first one - each thread then
    slept for its own full interval in turn, so N threads cost N sleeps instead of
    one. The wait is now computed and the tokens debited under the lock, the lock is
    released, and only then does the caller sleep. Debiting before releasing is what
    keeps the bucket honest: a second thread arriving during the sleep sees the token
    already spent and computes its own later deadline rather than reusing this one.
    """

    def __init__(self, rate_per_sec: float, burst: int) -> None:
        self._rate = float(rate_per_sec)
        self._capacity = float(burst)
        self._tokens = float(burst)
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            self._tokens = min(self._capacity, self._tokens + (now - self._last) * self._rate)
            self._last = now
            if self._tokens < 1.0:
                wait = (1.0 - self._tokens) / self._rate
                # Debit now, advance the clock to the point the token will have been
                # earned, and sleep outside the lock.
                self._tokens = 0.0
                self._last = now + wait
            else:
                self._tokens -= 1.0
                wait = 0.0
        if wait > 0:
            time.sleep(wait)


class OpenAlgoClient:
    """One instance per process. httpx.Client pools connections and is thread-safe."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._api = api(
            api_key=self.settings.openalgo_api_key,
            host=self.settings.openalgo_host,
            version=self.settings.openalgo_api_version,
            timeout=self.settings.openalgo_timeout,
        )
        # Deliberately under the documented 50/10 ceilings. The gap absorbs anything
        # else pointed at the same OpenAlgo instance, including a TradingAgent
        # session running side by side.
        self._general = RateLimiter(rate_per_sec=40, burst=40)
        self._orders = RateLimiter(rate_per_sec=8, burst=8)
        self._strategy = (
            getattr(self.settings, "default_strategy_name", "") or DEFAULT_STRATEGY
        )

    @property
    def raw(self):
        """The underlying openalgo.api instance. Prefer call()/raw_post()."""
        return self._api

    @property
    def strategy(self) -> str:
        """The attribution tag injected when a caller does not name one."""
        return self._strategy

    def close(self) -> None:
        try:
            self._api.close()
        except Exception:  # noqa: BLE001
            log.debug("client close failed", exc_info=True)

    # -- core -------------------------------------------------------------

    def call(self, method: str, _order: bool | None = None, **kwargs: Any) -> Any:
        """Invoke an SDK method by name. Returns whatever the SDK returns.

        Args:
            method: SDK method name, for example "quotes" or "placeorder".
            _order: Force the order bucket on or off. None infers it from the method
                name, which is the safe default.
            **kwargs: Forwarded verbatim. `strategy` is injected for the methods that
                take one.

        Returns:
            The raw SDK return value - dict for most calls, DataFrame for history and
            instruments. Errors come back as dicts; they are not raised.
        """
        fn = getattr(self._api, method, None)
        if fn is None:
            raise AttributeError(f"openalgo SDK has no method {method!r}")
        if method in STRATEGY_METHODS and not kwargs.get("strategy"):
            kwargs["strategy"] = self._strategy
        is_order = (method in ORDER_METHODS) if _order is None else _order
        (self._orders if is_order else self._general).acquire()
        return fn(**kwargs)

    def call_enveloped(self, method: str, source: str | None = None,
                       _order: bool | None = None, **kwargs: Any) -> dict:
        """Invoke and collapse into the standard {ok, source, data, error} shape."""
        src = source or method
        try:
            return envelope(self.call(method, _order=_order, **kwargs), src)
        except TypeError as exc:
            # A missing required kwarg is one of the few things the SDK raises.
            return err(f"bad arguments: {exc}", src, kind="TypeError")
        except Exception as exc:  # noqa: BLE001
            log.warning("call %s failed: %s", method, exc)
            return err(str(exc), src, kind=type(exc).__name__)

    def raw_post(self, endpoint: str, payload: dict | None = None,
                 _order: bool = False) -> dict:
        """Reach an endpoint the SDK does not wrap.

        The endpoint name must NOT carry a leading slash; see the module docstring for
        what happens when it does. The strip below is the whole defence.
        """
        name = (endpoint or "").strip().lstrip("/")
        if not name:
            return err("empty endpoint", "raw_post")
        body = {"apikey": self.settings.openalgo_api_key, **(payload or {})}
        (self._orders if _order else self._general).acquire()
        try:
            return envelope(self._api._make_request(name, body), name)
        except Exception as exc:  # noqa: BLE001
            log.warning("raw_post %s failed: %s", name, exc)
            return err(str(exc), name, kind=type(exc).__name__)

    # -- async wrappers ---------------------------------------------------

    async def acall(self, method: str, **kwargs: Any) -> Any:
        return await asyncio.to_thread(self.call, method, **kwargs)

    async def acall_enveloped(self, method: str, **kwargs: Any) -> dict:
        return await asyncio.to_thread(self.call_enveloped, method, **kwargs)

    async def araw_post(self, endpoint: str, payload: dict | None = None) -> dict:
        return await asyncio.to_thread(self.raw_post, endpoint, payload)

    # -- conveniences -----------------------------------------------------

    def ping(self) -> dict:
        """Authenticated reachability check. No leading slash - see the docstring."""
        return self.raw_post("ping")

    def analyzer_mode(self) -> str:
        """Returns 'analyze', 'live', or 'unknown'. Never raises.

        Analyzer mode is application-wide, not per API key, so this answers for the
        whole OpenAlgo instance. 'unknown' must be treated as live by any caller that
        gates on it, because the safe reading of an unreadable answer is the
        dangerous one.
        """
        res = self.call_enveloped("analyzerstatus")
        if not res.get("ok"):
            return "unknown"
        data = res.get("data") or {}
        mode = data.get("mode")
        if mode:
            return str(mode).lower()
        return "analyze" if data.get("analyze_mode") else "live"

    def ltp(self, symbol: str, exchange: str) -> float | None:
        """Last traded price, or None. Used for notional and deviation checks."""
        res = self.call_enveloped("quotes", symbol=symbol, exchange=exchange)
        if not res.get("ok"):
            return None
        try:
            return float((res.get("data") or {}).get("ltp"))
        except (TypeError, ValueError):
            return None

    def available_cash(self) -> float | None:
        """Broker availablecash, or None.

        This number confirms that enough real margin exists to place an order at all.
        It NEVER sets position size - sizing is against the configured ALLOCATION.
        Sizing against broker funds on a funded account produces positions an order of
        magnitude too large, silently.
        """
        res = self.call_enveloped("funds")
        if not res.get("ok"):
            return None
        try:
            return float((res.get("data") or {}).get("availablecash") or 0.0)
        except (TypeError, ValueError):
            return None


_client: OpenAlgoClient | None = None
_client_lock = threading.Lock()


def get_client() -> OpenAlgoClient:
    """Process-wide singleton. Double-checked so the fast path takes no lock."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = OpenAlgoClient()
    return _client

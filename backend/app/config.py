"""Settings and logging.

Every setting in .env.example is parsed here and nowhere else. Runtime facts this
module exists to preserve:

  - ALLOCATION is the whole world. It is a configured rupee amount and is NEVER read
    from the broker. TradingAgent sizes against broker `availablecash` because a human
    approves each order and can see the number; this agent runs unattended. Porting
    that denominator unchanged onto a funded account produces positions an order of
    magnitude too large, silently, with every guard still reporting green. The broker
    `funds` call keeps exactly one job here: confirming enough real margin exists to
    place the order at all. It never sets the size.

  - The binding constraint is an inequality, not a number:
    MAX_CONCURRENT_POSITIONS * RISK_FRACTION_BASE <= DAILY_LOSS_LIMIT_PCT / 100.
    validate() enforces it at startup. It is compared with a tolerance because binary
    floats make 4 * 0.005 == 0.020000000000000004, which is arithmetically at the 2%
    limit but numerically above it.

  - BASKET order is load-bearing. All five bars close at the same instant, so when
    three symbols signal at 09:35 and the budget funds two, position in the list IS
    the tie-break. Anything that sorts, sets or dict-keys this collection changes
    which trades get taken. It stays an ordered list of tuples for that reason.

  - START_TIME cannot be earlier than 09:20. The first 5m bar closes then, so no
    signal can exist before it and an earlier start would trade on a partial bar.

  - REGIME_ADX_FLOOR and HYSTERESIS_MARGIN parse to None when empty. None means "not
    measured yet" (PLAN.md step 5) and must read differently from 0.0, which would
    mean "measured, and the floor is zero" - a filter that passes everything.

  - Logging is plain ASCII on stdout. Agno installs a ColoredRichHandler that emits
    box-drawing glyphs, so handlers are replaced before any Agent is constructed.

  - stdout is reconfigured to UTF-8 with errors="replace" because Indian market data
    and model output carry the rupee sign, and a cp1252 console raises on it rather
    than mangling it.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import time as dt_time
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

from .version import get_version

PROJECT_ROOT = Path(__file__).resolve().parents[2]

load_dotenv(PROJECT_ROOT / ".env")

# Comparison slack for the risk invariant. Purely a float-representation allowance,
# not a risk allowance: 4 * 0.005 is 0.020000000000000004 in binary floating point,
# and rejecting that configuration would be an arithmetic accident, not a decision.
_FLOAT_TOLERANCE = 1e-9

# No signal can exist before the first 5m bar closes.
_EARLIEST_START = dt_time(9, 20)


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(_env(name, str(default))))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


def _env_float_opt(name: str) -> float | None:
    """A float that may legitimately be unset.

    Empty means the number has not been measured yet, which is a different state from
    zero. A caller must be able to tell "no regime floor is in force" from "the regime
    floor is 0.0", because the second one silently passes every regime.
    """
    raw = _env(name)
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name, "true" if default else "false").lower()
    return raw in ("1", "true", "yes", "on")


def _env_list(name: str, default: str = "") -> list[str]:
    return [p.strip().upper() for p in _env(name, default).split(",") if p.strip()]


def _env_time(name: str, default: str) -> dt_time:
    """Parse HH:MM into a time.

    The default is retried on a malformed value rather than falling through to
    midnight: a session clock that silently becomes 00:00 would make every
    time-of-day guard in the executor pass.
    """
    for candidate in (_env(name, default), default):
        try:
            parts = [int(p) for p in candidate.split(":")]
            return dt_time(*parts[:3])
        except (ValueError, TypeError):
            continue
    return dt_time(0, 0)


def _env_basket(name: str, default: str = "") -> list[tuple[str, str]]:
    """Parse "SYM:SECTOR,SYM:SECTOR" into an ordered list of (symbol, sector).

    Order is preserved deliberately - it is the tie-break priority when several
    symbols signal on the same bar close and the budget cannot fund them all.

    A symbol given without a sector lands in one shared bucket, so MAX_PER_SECTOR
    still constrains it. Treating unlabelled names as mutually uncorrelated would be
    the unsafe reading of a typo.
    """
    out: list[tuple[str, str]] = []
    for item in _env(name, default).split(","):
        entry = item.strip()
        if not entry:
            continue
        symbol, _, sector = entry.partition(":")
        symbol = symbol.strip().upper()
        if not symbol:
            continue
        out.append((symbol, sector.strip().upper() or "UNCLASSIFIED"))
    return out


def _resolve(path_value: str) -> Path:
    p = Path(path_value)
    return p if p.is_absolute() else PROJECT_ROOT / p


@dataclass
class Settings:
    # OpenAlgo
    openalgo_api_key: str = ""
    openalgo_host: str = "http://127.0.0.1:5000"
    openalgo_ws_url: str = "ws://127.0.0.1:8765"
    openalgo_api_version: str = "v1"
    # The SDK default of 120s is far too long for a system that must decide on a
    # 5-minute bar close: a hung call would eat the whole decision window.
    openalgo_timeout: float = 15.0
    openalgo_instruments_timeout: float = 30.0

    # Model. LiteLLM fronts every provider, so there is ONE key and ONE optional base
    # URL; the provider is decided by the prefix on litellm_model.
    #
    # The model never places an order in AutoAgent. It reads the regime, writes the
    # morning plan and proposes a risk fraction inside a bounded band. A provider
    # outage at 08:45 therefore means no plan, which means no session - a safe
    # failure rather than a position left unmanaged.
    litellm_model: str = "baseten/deepseek-ai/DeepSeek-V4-Flash-0731"
    litellm_api_key: str = ""
    litellm_api_base: str = ""
    litellm_temperature: float = 0.2
    litellm_top_p: float = 1.0
    # Load-bearing for a reasoning model. A low cap returns EMPTY content, not short
    # content, because the whole budget goes to hidden reasoning tokens.
    litellm_max_tokens: int = 4096
    litellm_include_usage: bool = True
    # "" (provider default), or none | minimal | low | medium | high.
    litellm_reasoning_effort: str = ""

    # Capital. See the module docstring: this is the single most important difference
    # from TradingAgent and the one number that must never come from the broker.
    allocation: float = 1_000_000.0
    require_funds_at_least: float = 1_000_000.0

    # Risk
    trading_enabled: bool = False
    require_analyzer_mode: bool = True
    # With no client order id available in the SDK, this tag plus symbol, side,
    # quantity and a time window IS the reconciliation key against the order book.
    default_strategy_name: str = "AutoAgent"
    # Mandate ceiling. The Planner may scale risk DOWN within [floor, base] and never
    # above base; anything missing or out of range falls back to the floor, not the
    # ceiling, so a malformed plan trades smaller rather than larger.
    risk_fraction_base: float = 0.005
    risk_fraction_floor: float = 0.00125
    # Percent of allocation, spent as a forward-looking budget rather than checked
    # after the fact as a tripwire. Loss means realized PLUS unrealized.
    daily_loss_limit_pct: float = 2.0
    max_concurrent_positions: int = 3
    max_trades_per_day: int = 6
    consecutive_loss_pause: int = 2
    consecutive_loss_halt: int = 3
    pause_minutes: int = 30
    # Trades inside +/- this many R are neither win nor loss. Without a scratch band
    # noise resets the streak counter constantly.
    scratch_band_r: float = 0.1

    # Exposure. Five Indian equities traded by one trend-following strategy are
    # closer to 1.5-2 independent bets than 5, so correlation is capped explicitly.
    max_per_sector: int = 2
    market_filter_symbol: str = "NIFTY"
    market_filter_exchange: str = "NSE_INDEX"

    # Ported from TradingAgent's RiskGuard. Same checks, one changed denominator:
    # affordability compares required margin against ALLOCATION minus realized loss
    # today, never against broker funds. 0 disables it.
    max_order_pct_of_funds: float = 90.0
    # Absolute ceilings are OPT-IN and 0 means no ceiling. A fixed rupee cap cannot be
    # right for every account; the allocation-relative guard above is the real
    # protection and these exist as a hard backstop on top of it.
    max_order_value: float = 0.0
    max_order_quantity: int = 0
    max_orders_per_session: int = 25
    max_price_deviation_pct: float = 20.0
    # The fingerprint stringifies quantity, so 10 and 10.0 do not match. This is the
    # second line of defence behind the deterministic intent id, not the first.
    duplicate_order_window_sec: int = 10
    # Cash equity intraday only. Index exchanges are quote-only and must never trade.
    allowed_exchanges: list[str] = field(default_factory=list)
    allowed_products: list[str] = field(default_factory=list)
    symbol_denylist: list[str] = field(default_factory=list)
    kill_switch_file: Path = PROJECT_ROOT / "data" / "KILL"

    # Session clock, in the configured timezone
    start_time: dt_time = dt_time(9, 30)
    end_time: dt_time = dt_time(14, 45)
    # Clear of the 15:15 cutoff after which the OpenAlgo sandbox rejects MIS orders,
    # and clear of broker auto-squareoff, which must never be relied on.
    squareoff_time: dt_time = dt_time(15, 10)
    # Fixed. The strategies, the warm-up maths and the backtests all assume 5m.
    timeframe: str = "5m"

    # Basket, in priority order
    basket: list[tuple[str, str]] = field(default_factory=list)

    # Strategy selection. Two windows because their disagreement is itself
    # information: strong over 90 sessions and weak over 15 is a regime change.
    lookback_long_sessions: int = 90
    lookback_short_sessions: int = 15
    regime_adx_floor: float | None = None
    hysteresis_margin: float | None = None

    # App
    backend_host: str = "127.0.0.1"
    # 8090 so AutoAgent and TradingAgent can run side by side.
    backend_port: int = 8090
    cors_origins: list[str] = field(default_factory=list)
    log_level: str = "INFO"
    db_path: Path = PROJECT_ROOT / "data" / "autoagent.db"
    audit_dir: Path = PROJECT_ROOT / "data" / "audit"
    # Closed sessions are immutable, so bars live on disk and only the newest session
    # is fetched each morning.
    bars_dir: Path = PROJECT_ROOT / "data" / "bars"
    timezone: str = "Asia/Kolkata"
    max_position_notional_pct: float = 100.0

    # Alerts. Push, not pull - nobody is watching a screen.
    telegram_username: str = ""
    whatsapp_default_to: str = ""

    @classmethod
    def load(cls) -> "Settings":
        return cls(
            openalgo_api_key=_env("OPENALGO_API_KEY"),
            openalgo_host=_env("OPENALGO_HOST", "http://127.0.0.1:5000").rstrip("/"),
            openalgo_ws_url=_env("OPENALGO_WS_URL", "ws://127.0.0.1:8765"),
            openalgo_api_version=_env("OPENALGO_API_VERSION", "v1"),
            openalgo_timeout=_env_float("OPENALGO_TIMEOUT", 15.0),
            openalgo_instruments_timeout=_env_float("OPENALGO_INSTRUMENTS_TIMEOUT", 30.0),
            litellm_model=_env(
                "LITELLM_MODEL", "baseten/deepseek-ai/DeepSeek-V4-Flash-0731"),
            litellm_api_key=_env("LITELLM_API_KEY"),
            # Empty means "let LiteLLM resolve the provider's own default endpoint".
            litellm_api_base=_env("LITELLM_API_BASE"),
            litellm_temperature=_env_float("LITELLM_TEMPERATURE", 0.2),
            litellm_top_p=_env_float("LITELLM_TOP_P", 1.0),
            litellm_max_tokens=_env_int("LITELLM_MAX_TOKENS", 4096),
            litellm_include_usage=_env_bool("LITELLM_INCLUDE_USAGE", True),
            litellm_reasoning_effort=_env("LITELLM_REASONING_EFFORT").lower(),
            allocation=_env_float("ALLOCATION", 1_000_000.0),
            require_funds_at_least=_env_float("REQUIRE_FUNDS_AT_LEAST", 1_000_000.0),
            trading_enabled=_env_bool("TRADING_ENABLED", False),
            require_analyzer_mode=_env_bool("REQUIRE_ANALYZER_MODE", True),
            default_strategy_name=_env("DEFAULT_STRATEGY_NAME", "AutoAgent"),
            risk_fraction_base=_env_float("RISK_FRACTION_BASE", 0.005),
            risk_fraction_floor=_env_float("RISK_FRACTION_FLOOR", 0.00125),
            daily_loss_limit_pct=_env_float("DAILY_LOSS_LIMIT_PCT", 2.0),
            max_concurrent_positions=_env_int("MAX_CONCURRENT_POSITIONS", 3),
            max_trades_per_day=_env_int("MAX_TRADES_PER_DAY", 6),
            consecutive_loss_pause=_env_int("CONSECUTIVE_LOSS_PAUSE", 2),
            consecutive_loss_halt=_env_int("CONSECUTIVE_LOSS_HALT", 3),
            pause_minutes=_env_int("PAUSE_MINUTES", 30),
            scratch_band_r=_env_float("SCRATCH_BAND_R", 0.1),
            max_per_sector=_env_int("MAX_PER_SECTOR", 2),
            market_filter_symbol=_env("MARKET_FILTER_SYMBOL", "NIFTY").upper(),
            market_filter_exchange=_env("MARKET_FILTER_EXCHANGE", "NSE_INDEX").upper(),
            max_order_pct_of_funds=_env_float("MAX_ORDER_PCT_OF_FUNDS", 90.0),
            max_position_notional_pct=_env_float("MAX_POSITION_NOTIONAL_PCT", 100.0),
            max_order_value=_env_float("MAX_ORDER_VALUE", 0.0),
            max_order_quantity=_env_int("MAX_ORDER_QUANTITY", 0),
            max_orders_per_session=_env_int("MAX_ORDERS_PER_SESSION", 25),
            max_price_deviation_pct=_env_float("MAX_PRICE_DEVIATION_PCT", 20.0),
            duplicate_order_window_sec=_env_int("DUPLICATE_ORDER_WINDOW_SEC", 10),
            allowed_exchanges=_env_list("ALLOWED_EXCHANGES", "NSE,BSE"),
            allowed_products=_env_list("ALLOWED_PRODUCTS", "MIS"),
            symbol_denylist=_env_list("SYMBOL_DENYLIST", ""),
            kill_switch_file=_resolve(_env("KILL_SWITCH_FILE", "data/KILL")),
            start_time=_env_time("START_TIME", "09:30"),
            end_time=_env_time("END_TIME", "14:45"),
            squareoff_time=_env_time("SQUAREOFF_TIME", "15:10"),
            timeframe=_env("TIMEFRAME", "5m").lower(),
            basket=_env_basket("BASKET", ""),
            lookback_long_sessions=_env_int("LOOKBACK_LONG_SESSIONS", 90),
            lookback_short_sessions=_env_int("LOOKBACK_SHORT_SESSIONS", 15),
            regime_adx_floor=_env_float_opt("REGIME_ADX_FLOOR"),
            hysteresis_margin=_env_float_opt("HYSTERESIS_MARGIN"),
            backend_host=_env("BACKEND_HOST", "127.0.0.1"),
            backend_port=_env_int("BACKEND_PORT", 8090),
            cors_origins=[o.strip() for o in _env(
                "CORS_ORIGINS", "http://localhost:5174,http://127.0.0.1:5174"
            ).split(",") if o.strip()],
            log_level=_env("LOG_LEVEL", "INFO").upper(),
            db_path=_resolve(_env("DB_PATH", "data/autoagent.db")),
            audit_dir=_resolve(_env("AUDIT_DIR", "data/audit")),
            bars_dir=_resolve(_env("BARS_DIR", "data/bars")),
            timezone=_env("TIMEZONE", "Asia/Kolkata"),
            telegram_username=_env("OPENALGO_TELEGRAM_USERNAME"),
            whatsapp_default_to=_env("WHATSAPP_DEFAULT_TO"),
        )

    # Providers that run on your own machine and need no credential at all.
    LOCAL_PROVIDERS = ("ollama", "ollama_chat", "lm_studio", "llamafile", "vllm")

    REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high")

    # --- Model -------------------------------------------------------------

    @property
    def model_provider(self) -> str:
        """The LiteLLM provider prefix, e.g. 'ollama_chat' from 'ollama_chat/llama3.1'."""
        if "/" not in self.litellm_model:
            return ""
        return self.litellm_model.split("/", 1)[0].strip().lower()

    @property
    def is_local_model(self) -> bool:
        return self.model_provider in self.LOCAL_PROVIDERS

    def resolve_model_api_key(self) -> str | None:
        """The single credential for the configured provider, or None if none is needed.

        There is deliberately only one key setting. Whichever provider LITELLM_MODEL
        names, LITELLM_API_KEY is its key. Local providers get None, because passing
        an empty string makes some LiteLLM paths send an Authorization header that a
        local server does not expect.
        """
        if self.is_local_model:
            return None
        return self.litellm_api_key or None

    def resolve_model_api_base(self) -> str | None:
        """Explicit base URL, or None to let LiteLLM use the provider default."""
        return self.litellm_api_base or None

    def resolve_reasoning_effort(self) -> str | None:
        """Validated reasoning effort, or None to leave the provider's default alone.

        This is a request, not a guarantee. Whether it does anything is a property of
        the model, not the setting: Ollama maps it onto a boolean `think` flag so
        low/medium/high all mean "on", Baseten's DeepSeek V4 Flash reasons but rejects
        the parameter outright, and gpt-4o does not reason at all. An unrecognised
        value is dropped with a warning rather than sent, because a rejected parameter
        fails the whole request instead of degrading it.
        """
        value = (self.litellm_reasoning_effort or "").strip().lower()
        if not value:
            return None
        if value not in self.REASONING_EFFORTS:
            logging.getLogger(__name__).warning(
                "LITELLM_REASONING_EFFORT=%r is not one of %s; ignoring it.",
                value, ", ".join(self.REASONING_EFFORTS))
            return None
        return value

    @property
    def thinking_disabled(self) -> bool:
        return self.resolve_reasoning_effort() == "none"

    # --- Capital and risk --------------------------------------------------

    @property
    def risk_amount_per_trade(self) -> float:
        """Rupees at risk on one trade at the mandate ceiling.

        Derived from ALLOCATION, which is a configured number and is never read from
        the broker. Sizing against a funded account instead would produce positions an
        order of magnitude too large - a 1 crore balance backing a 10 lakh mandate
        gives ten times the intended quantity, and every downstream guard would still
        pass because each one is a percentage of the same inflated base.

        Quantity follows from this: risk_amount / stop_distance, so a wider stop buys
        fewer shares and volatility sizes the position automatically.
        """
        return self.allocation * self.risk_fraction_base

    @property
    def daily_loss_limit_amount(self) -> float:
        """The daily budget in rupees, against ALLOCATION and not against equity.

        Fixed on purpose. A limit that floats with equity shrinks exactly as you lose,
        and one that floats with account funds is ten times too generous.
        """
        return self.allocation * self.daily_loss_limit_pct / 100.0

    # --- Basket ------------------------------------------------------------

    @property
    def basket_symbols(self) -> list[str]:
        """Symbols in priority order. The order is the tie-break, so do not sort it."""
        return [symbol for symbol, _ in self.basket]

    def sector_of(self, symbol: str) -> str:
        """Sector for a basket symbol, or an empty string if it is not in the basket."""
        key = symbol.strip().upper()
        for basket_symbol, sector in self.basket:
            if basket_symbol == key:
                return sector
        return ""

    # --- Checks ------------------------------------------------------------

    def validate(self) -> list[str]:
        """Configuration errors that must stop the session, worst first.

        Returns a list of plain strings rather than raising, so the caller can report
        every problem in one pass instead of one per restart. An empty list means the
        configuration is internally consistent - it says nothing about whether the
        credentials work, which is missing()'s job.
        """
        errors: list[str] = []

        # The binding constraint from Part 5. Three positions at 0.5% is 1.5% against
        # a 2% limit, which leaves headroom for the slippage paid when a breaker
        # flattens at market; five would be 2.5% and breaks the limit outright.
        at_risk = self.max_concurrent_positions * self.risk_fraction_base
        limit = self.daily_loss_limit_pct / 100.0
        if at_risk > limit + _FLOAT_TOLERANCE:
            remedy = ""
            if self.risk_fraction_base > 0 and self.max_concurrent_positions > 0:
                allowed = int((limit + _FLOAT_TOLERANCE) / self.risk_fraction_base)
                smaller = limit / self.max_concurrent_positions
                remedy = (f" Lower MAX_CONCURRENT_POSITIONS to {allowed}, or lower "
                          f"RISK_FRACTION_BASE to {smaller:.5f}.")
            errors.append(
                f"INVARIANT broken: max_concurrent_positions "
                f"({self.max_concurrent_positions}) * risk_fraction_base "
                f"({self.risk_fraction_base}) = {at_risk:.5f}, that is "
                f"{at_risk * 100:.3f}% of allocation ({at_risk * self.allocation:.2f} "
                f"rupees), which exceeds daily_loss_limit_pct / 100 = {limit:.5f} "
                f"({self.daily_loss_limit_pct}%, "
                f"{self.daily_loss_limit_amount:.2f} rupees)." + remedy)

        # The Planner scales risk DOWN from base towards floor. A floor above the
        # ceiling inverts the band and the fallback becomes the largest size.
        if self.risk_fraction_floor > self.risk_fraction_base + _FLOAT_TOLERANCE:
            errors.append(
                f"risk_fraction_floor ({self.risk_fraction_floor}) is above "
                f"risk_fraction_base ({self.risk_fraction_base}); the Planner may "
                f"only scale risk down, so the floor must not exceed the ceiling.")

        if self.allocation <= 0:
            errors.append(
                f"allocation ({self.allocation}) must be greater than zero; every "
                f"risk figure is a fraction of it.")

        if self.start_time >= self.end_time:
            errors.append(
                f"start_time ({self.start_time:%H:%M}) must be before end_time "
                f"({self.end_time:%H:%M}).")
        if self.end_time >= self.squareoff_time:
            errors.append(
                f"end_time ({self.end_time:%H:%M}) must be before squareoff_time "
                f"({self.squareoff_time:%H:%M}); a position opened at or after "
                f"squareoff has no session left to work.")
        if self.start_time < _EARLIEST_START:
            errors.append(
                f"start_time ({self.start_time:%H:%M}) is before "
                f"{_EARLIEST_START:%H:%M}, when the first 5m bar closes; no signal "
                f"can exist earlier than that.")

        if not self.basket:
            errors.append(
                "basket is empty; BASKET must list at least one SYMBOL:SECTOR pair.")
        else:
            seen: set[str] = set()
            duplicates: list[str] = []
            for symbol in self.basket_symbols:
                if symbol in seen:
                    duplicates.append(symbol)
                seen.add(symbol)
            if duplicates:
                # A repeated symbol would run two state machines on one position and
                # double the exposure the sector cap thinks it is limiting.
                errors.append(
                    f"basket has duplicate symbols: {', '.join(sorted(set(duplicates)))}.")

            counts: dict[str, int] = {}
            for _, sector in self.basket:
                counts[sector] = counts.get(sector, 0) + 1
            over = {s: n for s, n in counts.items() if n > self.max_per_sector}
            if over:
                detail = ", ".join(f"{s} has {n}" for s, n in sorted(over.items()))
                errors.append(
                    f"basket breaks max_per_sector ({self.max_per_sector}): {detail}. "
                    f"Two names in one sector is closer to one bet than two.")

        return errors

    def missing(self) -> list[str]:
        """Required credentials that are not set."""
        names: list[str] = []
        if not self.openalgo_api_key:
            names.append("OPENALGO_API_KEY")
        # A local model needs no credential; every hosted provider needs one.
        if not self.is_local_model and not self.resolve_model_api_key():
            names.append("LITELLM_API_KEY")
        return names

    def kill_switch_engaged(self) -> bool:
        """File existence, deliberately nothing more.

        Checked before every order. A pure filesystem test is what makes the switch
        work without a restart and without the process being healthy enough to serve
        an API call.
        """
        return self.kill_switch_file.exists()

    def redacted(self) -> dict:
        def mask(v: str) -> str:
            return f"{v[:4]}...{v[-4:]}" if len(v) > 12 else ("set" if v else "(missing)")

        return {
            "version": get_version(),
            "openalgo_host": self.openalgo_host,
            "openalgo_api_key": mask(self.openalgo_api_key),
            "litellm_model": self.litellm_model,
            "model_provider": self.model_provider or "(none, LiteLLM will infer)",
            "is_local_model": self.is_local_model,
            "model_api_key": mask(self.resolve_model_api_key() or ""),
            "model_api_base": self.resolve_model_api_base() or "(provider default)",
            "litellm_max_tokens": self.litellm_max_tokens,
            "reasoning_effort": self.resolve_reasoning_effort() or "(provider default)",
            "allocation": self.allocation,
            "require_funds_at_least": self.require_funds_at_least,
            "risk_fraction_base": self.risk_fraction_base,
            "risk_fraction_floor": self.risk_fraction_floor,
            "risk_amount_per_trade": self.risk_amount_per_trade,
            "daily_loss_limit_pct": self.daily_loss_limit_pct,
            "daily_loss_limit_amount": self.daily_loss_limit_amount,
            "max_concurrent_positions": self.max_concurrent_positions,
            "max_trades_per_day": self.max_trades_per_day,
            "max_per_sector": self.max_per_sector,
            "market_filter": f"{self.market_filter_symbol}:{self.market_filter_exchange}",
            "trading_enabled": self.trading_enabled,
            "require_analyzer_mode": self.require_analyzer_mode,
            "default_strategy_name": self.default_strategy_name,
            "allowed_exchanges": self.allowed_exchanges,
            "allowed_products": self.allowed_products,
            "kill_switch_file": str(self.kill_switch_file),
            "kill_switch_engaged": self.kill_switch_engaged(),
            "session": (f"{self.start_time:%H:%M} - {self.end_time:%H:%M}, "
                        f"squareoff {self.squareoff_time:%H:%M}"),
            "timeframe": self.timeframe,
            "timezone": self.timezone,
            "basket": [f"{sym}:{sec}" for sym, sec in self.basket],
            "lookback_sessions": [self.lookback_long_sessions,
                                  self.lookback_short_sessions],
            "regime_adx_floor": (self.regime_adx_floor
                                 if self.regime_adx_floor is not None
                                 else "(not measured)"),
            "hysteresis_margin": (self.hysteresis_margin
                                  if self.hysteresis_margin is not None
                                  else "(not measured)"),
            "backend": f"{self.backend_host}:{self.backend_port}",
            "log_level": self.log_level,
            "db_path": str(self.db_path),
            "audit_dir": str(self.audit_dir),
            "bars_dir": str(self.bars_dir),
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = Settings.load()
    s.db_path.parent.mkdir(parents=True, exist_ok=True)
    s.audit_dir.mkdir(parents=True, exist_ok=True)
    s.bars_dir.mkdir(parents=True, exist_ok=True)
    return s


def setup_logging(level: str | int | None = None) -> None:
    """Plain ASCII logging. Must run before any Agent is constructed."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    resolved = level if level is not None else get_settings().log_level
    if isinstance(resolved, str):
        resolved = getattr(logging, resolved.upper(), logging.INFO)

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s %(message)s", "%H:%M:%S")
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(fmt)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(resolved)

    # Agno attaches its own ColoredRichHandler with box-drawing glyphs and does not
    # propagate, so replacing the root handler alone leaves it drawing boxes.
    for name in ("agno", "agno-team", "agno-workflow"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = False
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(fmt)
        lg.addHandler(h)
        lg.setLevel(logging.WARNING)

    for noisy in ("httpx", "httpcore", "LiteLLM", "litellm", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


if __name__ == "__main__":
    import json

    setup_logging()
    settings = get_settings()
    print(json.dumps(settings.redacted(), indent=2, default=str))
    print("missing:", settings.missing() or "none")

    problems = settings.validate()
    if problems:
        print("validate: FAIL")
        for problem in problems:
            print(f"  - {problem}")
    else:
        print("validate: ok, no errors")
    # Usable as a build gate, so a broken configuration must not exit 0.
    raise SystemExit(1 if problems else 0)

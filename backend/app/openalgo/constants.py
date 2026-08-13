"""OpenAlgo enumerations and symbology, narrowed to cash equity intraday.

AutoAgent trades one instrument class and one product: NSE/BSE cash equity on MIS,
flat before the close. Options, futures, currency, commodity and GTT constants from
TradingAgent are deliberately absent - a constant that exists is a constant something
can validate against, and every venue listed here is a venue an order could reach.

Facts this module exists to hold on to:

  - Index exchanges are QUOTE-ONLY. The Part 5 market filter reads NIFTY from
    NSE_INDEX, so an index symbol legitimately flows through the data path and must
    still be refused by the order path. INDEX_EXCHANGES is what makes that refusal
    cheap and deterministic - see is_index_exchange().

  - price_type vs pricetype. The top-level SDK kwarg is `price_type`. Inside the legs
    of a basket, margin or multi-leg payload the server expects `pricetype`, no
    underscore. A wrong spelling in a leg is forwarded verbatim, ignored, and the
    order silently drops to MARKET. AutoAgent places single orders only, so it always
    uses `price_type` - but the trap is recorded here because the first person to add
    a basket will hit it.

  - Searching an index by its spoken name does not rescue a near-miss: "NIFTY 50" on
    NSE_INDEX returns 45 rows led by NIFTY500, NIFTYNXT50 and NIFTY50 USD, while the
    correct answer, plain NIFTY, is nowhere near the top. INDEX_ALIASES maps the
    spoken forms onto the canonical symbol so a lookup fails loudly or not at all.

Broker capability metadata still decides what is actually usable, so these are the
outer bound, not a guarantee.
"""

from __future__ import annotations

# Venues AutoAgent may send an order to. Cash equity only.
TRADABLE_EXCHANGES: tuple[str, ...] = ("NSE", "BSE")

# Quote-only venues. Orders against these must be refused before they reach the
# broker. Retained in full even though only NSE_INDEX is read today, because the
# guard is a denylist and an incomplete denylist is a hole.
INDEX_EXCHANGES: tuple[str, ...] = (
    "NSE_INDEX", "BSE_INDEX", "MCX_INDEX", "GLOBAL_INDEX",
)

ALL_EXCHANGES: tuple[str, ...] = TRADABLE_EXCHANGES + INDEX_EXCHANGES

# Intraday only. CNC and NRML would carry a position overnight, which this agent
# never does, so they are not listed and therefore cannot be validated against.
PRODUCTS: tuple[str, ...] = ("MIS",)

# SL-M is the stop the executor places at the broker on fill; MARKET is the
# squareoff; LIMIT is the entry at touch. SL is listed because the server accepts it.
PRICE_TYPES: tuple[str, ...] = ("MARKET", "LIMIT", "SL", "SL-M")

ACTIONS: tuple[str, ...] = ("BUY", "SELL")

# The timeframe is fixed by the strategies, the warm-up maths and the backtests.
TIMEFRAME: str = "5m"

# The canonical set. The real list is broker-specific - always confirm with
# /intervals. Kept beyond 5m because the historical store and the regime read may
# resample or ask for daily bars.
KNOWN_INTERVALS: tuple[str, ...] = (
    "1m", "3m", "5m", "10m", "15m", "30m", "45m", "60m", "1h", "D", "W", "M",
)

# The index names people actually say, mapped to OpenAlgo's symbol. See the module
# docstring for why a near-miss is not recoverable by search.
INDEX_ALIASES: dict[str, str] = {
    # NSE
    "NIFTY 50": "NIFTY", "NIFTY50": "NIFTY", "NIFTY-50": "NIFTY", "NSE NIFTY": "NIFTY",
    "NIFTY INDEX": "NIFTY", "S&P CNX NIFTY": "NIFTY", "CNX NIFTY": "NIFTY",
    "BANK NIFTY": "BANKNIFTY", "NIFTY BANK": "BANKNIFTY", "NIFTYBANK": "BANKNIFTY",
    "FIN NIFTY": "FINNIFTY", "NIFTY FIN SERVICE": "FINNIFTY",
    "NIFTY FINANCIAL SERVICES": "FINNIFTY",
    "MIDCAP NIFTY": "MIDCPNIFTY", "NIFTY MIDCAP SELECT": "MIDCPNIFTY",
    "MIDCPNIFTY50": "MIDCPNIFTY",
    "NIFTY NEXT 50": "NIFTYNXT50", "NIFTY NEXT50": "NIFTYNXT50", "NEXT 50": "NIFTYNXT50",
    "INDIA VIX": "INDIAVIX", "VIX": "INDIAVIX",
    "NIFTY IT": "NIFTYIT", "NIFTY AUTO": "NIFTYAUTO", "NIFTY PHARMA": "NIFTYPHARMA",
    "NIFTY FMCG": "NIFTYFMCG", "NIFTY METAL": "NIFTYMETAL", "NIFTY ENERGY": "NIFTYENERGY",
    "NIFTY REALTY": "NIFTYREALTY", "NIFTY PSU BANK": "NIFTYPSUBANK",
    "NIFTY PVT BANK": "NIFTYPVTBANK", "NIFTY INFRA": "NIFTYINFRA",
    # BSE
    "BSE SENSEX": "SENSEX", "S&P BSE SENSEX": "SENSEX", "SENSEX 30": "SENSEX",
    "BSE BANKEX": "BANKEX", "SENSEX 50": "SENSEX50",
}

# Which exchange each canonical index quotes on, so a wrong exchange is correctable
# too. MARKET_FILTER_SYMBOL / MARKET_FILTER_EXCHANGE in .env should agree with this.
INDEX_EXCHANGE: dict[str, str] = {
    **{s: "NSE_INDEX" for s in (
        "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50", "INDIAVIX",
        "NIFTYIT", "NIFTYAUTO", "NIFTYPHARMA", "NIFTYFMCG", "NIFTYMETAL",
        "NIFTYENERGY", "NIFTYREALTY", "NIFTYPSUBANK", "NIFTYPVTBANK", "NIFTYINFRA")},
    **{s: "BSE_INDEX" for s in ("SENSEX", "BANKEX", "SENSEX50")},
}


def resolve_index_alias(symbol: str) -> str | None:
    """Map a spoken index name onto its OpenAlgo symbol, or None if it is not an alias."""
    raw = (symbol or "").strip().upper()
    if not raw:
        return None
    if raw in INDEX_EXCHANGE:          # already canonical
        return None
    squashed = " ".join(raw.split())
    return INDEX_ALIASES.get(squashed) or INDEX_ALIASES.get(squashed.replace(" ", ""))


def is_index_exchange(exchange: str) -> bool:
    """True for a quote-only venue. An order against one must never be sent."""
    return (exchange or "").strip().upper() in INDEX_EXCHANGES


def is_tradable_exchange(exchange: str) -> bool:
    return (exchange or "").strip().upper() in TRADABLE_EXCHANGES


def normalize_exchange(exchange: str) -> str:
    return (exchange or "").strip().upper()


def normalize_symbol(symbol: str) -> str:
    return (symbol or "").strip().upper()


def normalize_action(action: str) -> str:
    return (action or "").strip().upper()


def normalize_product(product: str) -> str:
    return (product or "").strip().upper()


def normalize_price_type(price_type: str) -> str:
    """Accepts SLM / SL_M / sl-m and returns the canonical SL-M."""
    raw = (price_type or "").strip().upper().replace("_", "-")
    if raw in ("SLM", "SL M"):
        return "SL-M"
    return raw


def describe_exchange(exchange: str) -> str:
    return {
        "NSE": "NSE equity",
        "BSE": "BSE equity",
        "NSE_INDEX": "NSE indices (quote only)",
        "BSE_INDEX": "BSE indices (quote only)",
        "MCX_INDEX": "MCX sectoral indices (quote only)",
        "GLOBAL_INDEX": "Global indices (quote only)",
    }.get(normalize_exchange(exchange), "unknown or untraded exchange")

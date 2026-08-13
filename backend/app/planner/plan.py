"""The pre-open planner. Runs once at 08:45 and exits.

PLAN.md Part 2 and Part 8. The order of operations is the whole design:

    1. load history for the basket
    2. backtest all three strategies over both windows
    3. read the regime
    4. SELECT DETERMINISTICALLY          <- the decision happens here
    5. ask the model to explain it, and to propose a risk fraction
    6. write the plan artifact for the human to approve

The model enters at step 5, after the decision is already made. It writes the
rationale and it may propose a risk fraction, and that proposal is clamped into
the mandate band and can only ever scale DOWN. It cannot change the strategy, it
cannot widen the risk, and it cannot make the agent trade on a day the selector
declined.

Why the model is not in step 4: the selection has to be replayable. If a model
picked the strategy, the morning choice could not be backtested, two runs on the
same data could disagree, and a bad day could not be explained afterwards.

NO PLAN MEANS NO SESSION. If the model is unavailable at 08:45 the planner still
writes an artifact - with the deterministic selection intact and the rationale
missing - because losing the narrator must not stop the machine. But if the
BACKTEST fails, no artifact is written at all and the session does not run. That
asymmetry is deliberate: the rationale is commentary, the metrics are the
decision.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from ..backtest.metrics import Metrics, compute, format_table
from ..backtest.replay import PortfolioReplay
from ..config import Settings
from ..openalgo.client import OpenAlgoClient, get_client
from ..openalgo.frames import get_frame_cache
from ..strategies import STRATEGIES
from .regime import RegimeRead, read_basket
from .selector import NO_TRADE, Selection, scale_risk, select

log = logging.getLogger(__name__)

# Roughly 75 bars a session, so this covers the long window with warm-up to spare.
LONG_BARS = 7000
SHORT_BARS = 1200


def plans_dir(settings: Settings) -> Path:
    """Beside the database, matching how the API and RiskBudget locate theirs."""
    return Path(settings.db_path).parent / "plans"


def plan_path(settings: Settings, day: date) -> Path:
    return plans_dir(settings) / f"{day.isoformat()}.json"


def load_frames(settings: Settings, bars: int, client: OpenAlgoClient | None = None
                ) -> dict[str, pd.DataFrame]:
    cache = get_frame_cache()
    out: dict[str, pd.DataFrame] = {}
    for symbol in settings.basket_symbols:
        result = cache.get_frame(symbol, "NSE", settings.timeframe, lookback_bars=bars)
        if result.get("ok"):
            out[symbol] = result["frame"]
        else:
            log.warning("no history for %s: %s", symbol, result.get("error"))
    return out


def build_metrics(settings: Settings) -> tuple[list[Metrics], dict[str, pd.DataFrame]]:
    """Backtest every strategy over both windows on one shared timeline."""
    rows: list[Metrics] = []
    long_frames = load_frames(settings, LONG_BARS)
    if not long_frames:
        return rows, {}

    short_frames = {
        sym: frame.tail(SHORT_BARS) for sym, frame in long_frames.items()
    }

    for window, frames in (("long", long_frames), ("short", short_frames)):
        for strategy_id in STRATEGIES:
            result = PortfolioReplay(settings, strategy_id).run(frames)
            rows.append(compute(result, settings.allocation, window=window,
                                scratch_r=settings.scratch_band_r))
    return rows, long_frames


def _market_frame(settings: Settings) -> pd.DataFrame | None:
    result = get_frame_cache().get_frame(
        settings.market_filter_symbol, settings.market_filter_exchange,
        settings.timeframe, lookback_bars=3000,
    )
    return result["frame"] if result.get("ok") else None


def _model_rationale(
    settings: Settings, selection: Selection, regime: RegimeRead, table: str
) -> dict[str, Any]:
    """Ask the model to narrate the decision and propose a risk fraction.

    Everything here is best-effort. A failure returns an empty dict and the plan
    is written without a rationale, because the decision does not depend on it.
    """
    out: dict[str, Any] = {"available": False}
    try:
        from agno.agent import Agent
        from agno.models.litellm import LiteLLM
    except ImportError:
        out["error"] = "agno is not installed"
        return out

    instructions = [
        "You are writing the pre-open note for an autonomous intraday equity agent.",
        "The strategy choice has ALREADY been made by deterministic code. You are "
        "explaining it, not making it. Never suggest a different strategy.",
        "Write at most six sentences, in plain English, for a trader reading at 08:45.",
        "Say what was chosen, why, and what would make today go wrong.",
        "If the decision was to trade nothing, say so plainly and do not argue with it.",
        "Then on a final line, alone, write RISK: followed by a number between "
        f"{settings.risk_fraction_floor} and {settings.risk_fraction_base}. "
        "Propose a lower number when the evidence is thin. You cannot propose more "
        "than the ceiling; anything higher will be clamped.",
        "No emoji, no icons, no markdown headings.",
    ]

    prompt = (
        f"Trading date: {date.today().isoformat()}\n"
        f"Regime: {regime.label} - {regime.detail}\n"
        f"Decision: {selection.strategy_id}\n"
        f"Trade today: {selection.trade_today}\n"
        f"Selector reasoning: {selection.reason}\n\n"
        f"Backtest table:\n{table}\n"
    )

    try:
        agent = Agent(
            model=LiteLLM(
                id=settings.litellm_model,
                api_key=settings.resolve_model_api_key(),
                api_base=settings.resolve_model_api_base() or None,
                max_tokens=settings.litellm_max_tokens,
            ),
            instructions=instructions,
            markdown=False,
            telemetry=False,
            store_events=False,
        )
        response = agent.run(prompt)
        text = (getattr(response, "content", "") or "").strip()
        out["available"] = bool(text)
        out["rationale"] = text
        out["model"] = settings.litellm_model

        # Parse the proposal off the final RISK: line. A model that ignores the
        # format simply does not get a say, which is the safe default.
        proposed = None
        for line in reversed(text.splitlines()):
            if line.strip().upper().startswith("RISK:"):
                try:
                    proposed = float(line.split(":", 1)[1].strip().split()[0])
                except (ValueError, IndexError):
                    proposed = None
                break
        out["risk_fraction_proposed"] = proposed
    except Exception as exc:  # noqa: BLE001
        # A model outage must not stop the machine. It costs the narration only.
        log.warning("planner rationale unavailable: %s", exc)
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def build_plan(settings: Settings, day: date | None = None,
               *, incumbent: str = "") -> dict[str, Any]:
    """Run the whole pre-open routine and return the artifact."""
    day = day or date.today()

    rows, frames = build_metrics(settings)
    if not rows:
        raise RuntimeError(
            "no backtest metrics could be produced, so there is no basis for a "
            "decision. No plan is written and the session will not run."
        )

    regime = read_basket(
        frames,
        adx_floor=settings.regime_adx_floor or 25.0,
        market_frame=_market_frame(settings),
    )

    selection = select(
        rows, regime,
        hysteresis_margin=settings.hysteresis_margin or 0.05,
        incumbent=incumbent,
        risk_fraction_base=settings.risk_fraction_base,
    )

    long_rows = {m.strategy_id: m for m in rows if m.window == "long"}
    deterministic_risk = scale_risk(
        selection, long_rows,
        base=settings.risk_fraction_base, floor=settings.risk_fraction_floor,
    )

    table = format_table(rows)
    narration = _model_rationale(settings, selection, regime, table)

    # The model may only scale DOWN from what the deterministic path already
    # decided. It cannot raise risk by being confident.
    effective = deterministic_risk
    note = "deterministic"
    proposed = narration.get("risk_fraction_proposed")
    if isinstance(proposed, (int, float)) and proposed > 0:
        clamped = max(settings.risk_fraction_floor, min(deterministic_risk, float(proposed)))
        if clamped < deterministic_risk:
            effective, note = clamped, "model proposed a lower risk fraction"
        elif float(proposed) > deterministic_risk:
            note = (
                f"model proposed {proposed} which is above the deterministic "
                f"{deterministic_risk}; clamped down"
            )

    return {
        "trading_date": day.isoformat(),
        "generated_at": datetime.now().isoformat(),
        "selection": selection.as_dict(),
        "strategy_id": selection.strategy_id,
        "trade_today": selection.trade_today,
        "risk_fraction": effective,
        "risk_fraction_proposed": proposed,
        "risk_fraction_note": note,
        # asdict() drops label because it is a property, not a field.
        "regime": {**asdict(regime), "label": regime.label},
        "basket": [f"{sym}:{sec}" for sym, sec in settings.basket],
        "reason": selection.reason,
        "metrics": [m.as_dict() for m in rows],
        "metrics_table": table,
        "rationale": narration.get("rationale", ""),
        "model": narration.get("model", ""),
        "model_available": narration.get("available", False),
        "model_error": narration.get("error", ""),
    }


def write_plan(settings: Settings, plan: dict[str, Any], day: date | None = None) -> Path:
    day = day or date.fromisoformat(plan["trading_date"])
    path = plan_path(settings, day)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan, indent=2, default=str), encoding="utf-8")
    log.info("plan written to %s", path)
    return path


def run_planner(settings: Settings, day: date | None = None,
                *, incumbent: str = "") -> tuple[Path, dict[str, Any]]:
    plan = build_plan(settings, day, incumbent=incumbent)
    return write_plan(settings, plan, day), plan


if __name__ == "__main__":
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    from ..config import get_settings, setup_logging

    setup_logging()
    s = get_settings()
    path, artifact = run_planner(s)
    print(artifact["metrics_table"])
    print()
    print(f"regime      : {artifact['regime']['label']}")
    print(f"decision    : {artifact['strategy_id']}  trade_today={artifact['trade_today']}")
    print(f"risk        : {artifact['risk_fraction']} ({artifact['risk_fraction_note']})")
    print(f"model       : {'yes' if artifact['model_available'] else 'no'} "
          f"{artifact.get('model_error', '')}")
    if artifact.get("rationale"):
        print()
        print(artifact["rationale"])
    print()
    print(f"written to {path}")

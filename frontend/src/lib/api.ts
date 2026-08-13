/** Typed fetch wrappers for the control routes, and the normalizers under them.
 *
 * Two rules shape this module.
 *
 * 1. NOTHING HERE THROWS FOR A COSMETIC FAILURE. This is a control surface for an
 *    agent that trades unattended: if one field of the session payload is malformed,
 *    the halt button must still render. Only the calls whose result the caller acts
 *    on - approve, halt, reduce-only - propagate an error, because a silent failure
 *    there would let a human believe they had stopped something they had not.
 *
 * 2. THE SHAPE IS READ DEFENSIVELY, NOT ASSUMED. The backend may return a payload at
 *    the root, wrapped in {ok, data}, or keyed by name ({plan: ...}). Each normalizer
 *    looks in all of those and reads the common key spellings, because a rename on
 *    the backend should degrade one field rather than blank the whole screen.
 *
 * The route list is fixed (PLAN.md Part 8):
 *   GET  /api/health   GET /api/config   GET /api/plan   POST /api/plan/approve
 *   GET  /api/session  GET /api/trades   POST /api/halt  POST /api/reduce-only
 *   GET  /api/stream   (SSE, see sse.ts)
 */

export type Json = Record<string, unknown>

export const JSON_HEADERS = { "Content-Type": "application/json" }

/** status 0 means the request never reached a server - the usual case being that
 *  the backend is simply not running, which the UI reports rather than throwing. */
export class ApiError extends Error {
  status: number

  constructor(message: string, status: number) {
    super(message)
    this.name = "ApiError"
    this.status = status
  }

  get offline(): boolean {
    return this.status === 0
  }
}

export function asRecord(value: unknown): Json | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null
  return value as Json
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : []
}

/** First defined value across a list of candidate containers and key spellings. */
function pick(sources: (Json | null)[], ...keys: string[]): unknown {
  for (const source of sources) {
    if (!source) continue
    for (const key of keys) {
      const value = source[key]
      if (value !== undefined && value !== null) return value
    }
  }
  return undefined
}

function num(value: unknown, fallback: number | null = null): number | null {
  if (typeof value === "number") return Number.isFinite(value) ? value : fallback
  if (typeof value === "string") {
    const cleaned = value.replace(/[,\s₹]/g, "")
    if (!cleaned) return fallback
    const parsed = Number(cleaned)
    return Number.isFinite(parsed) ? parsed : fallback
  }
  return fallback
}

function str(value: unknown, fallback = ""): string {
  if (typeof value === "string") return value
  if (typeof value === "number" || typeof value === "boolean") return String(value)
  return fallback
}

function bool(value: unknown, fallback = false): boolean {
  if (typeof value === "boolean") return value
  if (typeof value === "string") {
    const lowered = value.trim().toLowerCase()
    if (["true", "yes", "1", "on"].includes(lowered)) return true
    if (["false", "no", "0", "off"].includes(lowered)) return false
  }
  if (typeof value === "number") return value !== 0
  return fallback
}

function strings(value: unknown): string[] {
  return asArray(value).filter((entry): entry is string => typeof entry === "string")
}

/** Pulls a human message out of whatever the backend put in the error body. */
export function messageOf(payload: unknown, fallback: string): string {
  const record = asRecord(payload)
  if (record) {
    for (const key of ["detail", "message", "error", "reason"]) {
      const value = record[key]
      if (typeof value === "string" && value.trim()) return value
    }
    const detail = asRecord(record.detail)
    if (detail) return messageOf(detail, fallback)
  }
  if (typeof payload === "string" && payload.trim()) return payload
  return fallback
}

export function describeError(error: unknown): string {
  if (error instanceof ApiError) return error.message
  if (error instanceof Error) return error.message || error.name
  return String(error)
}

async function request<T = Json>(path: string, init?: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(path, init)
  } catch (error) {
    // TypeError from fetch means the connection failed outright. It is the normal
    // state before the backend is started, so it gets its own status.
    throw new ApiError(
      `the backend at ${path} could not be reached (${describeError(error)})`,
      0
    )
  }
  const text = await response.text()
  let payload: unknown = null
  if (text) {
    try {
      payload = JSON.parse(text)
    } catch {
      payload = null
    }
  }
  if (!response.ok) {
    throw new ApiError(
      messageOf(payload ?? text, `${path} returned status ${response.status}`),
      response.status
    )
  }
  const record = asRecord(payload)
  if (record && record.ok === false) {
    throw new ApiError(messageOf(record, `${path} reported a failure`), response.status)
  }
  return (payload ?? {}) as T
}

/** Unwraps the envelope: a payload may sit at the root, under `data`, or under its
 *  own name. Returns every candidate container so `pick` can search them in order. */
function containers(payload: unknown, ...names: string[]): (Json | null)[] {
  const root = asRecord(payload)
  const found: (Json | null)[] = []
  for (const name of names) {
    const direct = asRecord(root?.[name])
    if (direct) found.push(direct)
    const nested = asRecord(asRecord(root?.data)?.[name])
    if (nested) found.push(nested)
  }
  found.push(asRecord(root?.data))
  found.push(root)
  return found
}

// --------------------------------------------------------------------- health

export type TradingMode = "analyze" | "live" | "unknown"

export interface HealthInfo {
  ok: boolean
  version: string
  mode: TradingMode
  broker: string
  model: string
  openalgoReachable: boolean | null
  tradingEnabled: boolean
  killSwitch: boolean
  missingKeys: string[]
  errors: string[]
}

const ANALYZE_WORDS = ["analyze", "analyse", "analyzer", "analyser", "sandbox", "simulated"]

function readMode(sources: (Json | null)[]): TradingMode {
  const raw = pick(sources, "mode", "analyzer_mode_name", "trading_mode")
  if (typeof raw === "string" && raw.trim()) {
    const lowered = raw.trim().toLowerCase()
    if (ANALYZE_WORDS.some((word) => lowered.includes(word))) return "analyze"
    if (lowered.includes("live") || lowered.includes("real")) return "live"
  }
  const flag = pick(sources, "analyze_mode", "analyzer_mode", "analyze")
  if (typeof flag === "boolean") return flag ? "analyze" : "live"
  return "unknown"
}

export async function getHealth(): Promise<HealthInfo> {
  const payload = await request<unknown>("/api/health")
  const sources = containers(payload, "health")
  const openalgo = pick(sources, "openalgo", "openalgo_reachable", "broker_reachable")
  const openalgoRecord = asRecord(openalgo)
  const reachable =
    typeof openalgo === "boolean"
      ? openalgo
      : typeof openalgoRecord?.ok === "boolean"
        ? (openalgoRecord.ok as boolean)
        : null
  return {
    ok: bool(pick(sources, "ok"), true),
    version: str(pick(sources, "version")),
    mode: readMode(sources),
    broker: str(pick(sources, "broker")),
    model: str(pick(sources, "model", "litellm_model")),
    openalgoReachable: reachable,
    tradingEnabled: bool(pick(sources, "trading_enabled")),
    killSwitch: bool(pick(sources, "kill_switch_engaged", "kill_switch")),
    missingKeys: strings(pick(sources, "missing_keys", "missing")),
    errors: strings(pick(sources, "errors", "config_errors"))
  }
}

// --------------------------------------------------------------------- config

export interface BasketEntry {
  symbol: string
  sector: string
}

export interface ConfigInfo {
  version: string
  allocation: number | null
  riskFractionBase: number | null
  riskFractionFloor: number | null
  riskAmountPerTrade: number | null
  dailyLossLimitPct: number | null
  dailyLossLimitAmount: number | null
  maxConcurrentPositions: number | null
  maxTradesPerDay: number | null
  maxPerSector: number | null
  consecutiveLossPause: number | null
  consecutiveLossHalt: number | null
  startTime: string
  endTime: string
  squareoffTime: string
  timeframe: string
  timezone: string
  tradingEnabled: boolean
  requireAnalyzerMode: boolean
  basket: BasketEntry[]
}

function readBasket(value: unknown): BasketEntry[] {
  const out: BasketEntry[] = []
  for (const entry of asArray(value)) {
    if (typeof entry === "string") {
      // "RELIANCE:ENERGY" is the .env spelling and survives into some payloads.
      const [symbol, sector] = entry.split(":")
      if (symbol?.trim()) {
        out.push({ symbol: symbol.trim().toUpperCase(), sector: (sector ?? "").trim().toUpperCase() })
      }
      continue
    }
    const record = asRecord(entry)
    if (!record) continue
    const symbol = str(pick([record], "symbol", "tradingsymbol", "name")).trim().toUpperCase()
    if (!symbol) continue
    out.push({ symbol, sector: str(pick([record], "sector", "group")).trim().toUpperCase() })
  }
  return out
}

export async function getConfig(): Promise<ConfigInfo> {
  const payload = await request<unknown>("/api/config")
  const sources = containers(payload, "config", "settings")
  return {
    version: str(pick(sources, "version")),
    allocation: num(pick(sources, "allocation")),
    riskFractionBase: num(pick(sources, "risk_fraction_base", "risk_fraction")),
    riskFractionFloor: num(pick(sources, "risk_fraction_floor")),
    riskAmountPerTrade: num(pick(sources, "risk_amount_per_trade", "risk_amount")),
    dailyLossLimitPct: num(pick(sources, "daily_loss_limit_pct")),
    dailyLossLimitAmount: num(pick(sources, "daily_loss_limit_amount", "daily_loss_limit")),
    maxConcurrentPositions: num(pick(sources, "max_concurrent_positions", "position_cap")),
    maxTradesPerDay: num(pick(sources, "max_trades_per_day")),
    maxPerSector: num(pick(sources, "max_per_sector")),
    consecutiveLossPause: num(pick(sources, "consecutive_loss_pause")),
    consecutiveLossHalt: num(pick(sources, "consecutive_loss_halt")),
    startTime: str(pick(sources, "start_time")),
    endTime: str(pick(sources, "end_time")),
    squareoffTime: str(pick(sources, "squareoff_time")),
    timeframe: str(pick(sources, "timeframe"), "5m"),
    timezone: str(pick(sources, "timezone"), "Asia/Kolkata"),
    tradingEnabled: bool(pick(sources, "trading_enabled")),
    requireAnalyzerMode: bool(pick(sources, "require_analyzer_mode"), true),
    basket: readBasket(pick(sources, "basket", "watchlist", "universe"))
  }
}

// ----------------------------------------------------------------------- plan

/** The strategy id the selector returns when nothing is worth trading. Not an
 *  error: PLAN.md Part 4 makes "trade nothing" a first-class outcome, and after
 *  step 4 measured all three strategies at negative expectancy it is currently the
 *  expected one. */
export const NO_TRADE_STRATEGY = "none"

export type PlanStatus = "pending" | "approved" | "rejected" | "expired" | "unknown"

export interface MetricRow {
  strategyId: string
  window: string
  trades: number | null
  winRate: number | null
  expectancyR: number | null
  profitFactor: number | null
  netPnl: number | null
  returnPct: number | null
  maxDrawdownPct: number | null
  sharpe: number | null
  viable: boolean | null
  significant: boolean | null
}

/** The numbers the approval gate turns on. Every one is computed by the backend -
 *  the plan's own prose is never the source, which is the same rule TradingAgent's
 *  confirmation card runs on. `source` records where they were actually read from
 *  so the card can say so rather than implying more authority than it has. */
export interface PlanConsequences {
  worstCaseDailyLoss: number | null
  maxCapitalAtRisk: number | null
  positionCap: number | null
  riskAmountPerTrade: number | null
  allocation: number | null
  dailyLossLimitPct: number | null
  maxTradesPerDay: number | null
  source: "plan" | "config" | "none"
}

export interface Plan {
  tradingDate: string
  mandateVersion: string
  strategyId: string
  tradeToday: boolean
  regime: string
  regimeDetail: string
  reason: string
  rationale: string
  riskFraction: number | null
  incumbent: string
  switched: boolean
  candidates: string[]
  viable: string[]
  basket: BasketEntry[]
  metrics: MetricRow[]
  consequences: PlanConsequences
  startTime: string
  endTime: string
  squareoffTime: string
  status: PlanStatus
  approvedAt: string
  note: string
  publishedAt: string
}

function readMetrics(value: unknown): MetricRow[] {
  const rows: MetricRow[] = []
  for (const entry of asArray(value)) {
    const record = asRecord(entry)
    if (!record) continue
    const strategyId = str(pick([record], "strategy_id", "strategy", "id"))
    if (!strategyId) continue
    rows.push({
      strategyId,
      window: str(pick([record], "window", "lookback")),
      trades: num(pick([record], "trades", "trade_count")),
      winRate: num(pick([record], "win_rate")),
      expectancyR: num(pick([record], "expectancy_r", "expectancy")),
      profitFactor: num(pick([record], "profit_factor")),
      netPnl: num(pick([record], "net_pnl")),
      returnPct: num(pick([record], "return_pct")),
      maxDrawdownPct: num(pick([record], "max_drawdown_pct")),
      sharpe: num(pick([record], "sharpe")),
      viable: typeof pick([record], "is_viable", "viable") === "undefined"
        ? null
        : bool(pick([record], "is_viable", "viable")),
      significant: typeof pick([record], "is_significant", "significant") === "undefined"
        ? null
        : bool(pick([record], "is_significant", "significant"))
    })
  }
  return rows
}

function readStatus(value: unknown): PlanStatus {
  const lowered = str(value).trim().toLowerCase()
  if (["pending", "proposed", "awaiting", "awaiting_approval"].includes(lowered)) return "pending"
  if (["approved", "accepted", "locked"].includes(lowered)) return "approved"
  if (["rejected", "declined", "refused"].includes(lowered)) return "rejected"
  if (["expired", "stale"].includes(lowered)) return "expired"
  return "unknown"
}

function readConsequences(sources: (Json | null)[], config: ConfigInfo | null): PlanConsequences {
  const planSources = [asRecord(pick(sources, "consequences", "computed", "impact")), ...sources]
  const worst = num(pick(planSources, "worst_case_daily_loss", "worst_case_loss", "daily_loss_limit_amount"))
  const atRisk = num(pick(planSources, "max_capital_at_risk", "capital_at_risk", "total_at_risk"))
  const cap = num(pick(planSources, "position_cap", "max_concurrent_positions"))
  const perTrade = num(pick(planSources, "risk_amount_per_trade", "risk_amount"))
  const allocation = num(pick(planSources, "allocation"))
  const limitPct = num(pick(planSources, "daily_loss_limit_pct"))
  const maxTrades = num(pick(planSources, "max_trades_per_day"))

  const anyFromPlan = [worst, atRisk, cap, perTrade].some((value) => value !== null)
  if (anyFromPlan) {
    return {
      worstCaseDailyLoss: worst,
      maxCapitalAtRisk: atRisk,
      positionCap: cap,
      riskAmountPerTrade: perTrade,
      allocation,
      dailyLossLimitPct: limitPct,
      maxTradesPerDay: maxTrades,
      source: "plan"
    }
  }

  // The plan carried no computed consequences. Falling back to /api/config is still
  // reading server numbers rather than inventing any, and the card says which.
  if (!config) {
    return {
      worstCaseDailyLoss: null, maxCapitalAtRisk: null, positionCap: null,
      riskAmountPerTrade: null, allocation: null, dailyLossLimitPct: null,
      maxTradesPerDay: null, source: "none"
    }
  }
  return {
    worstCaseDailyLoss: config.dailyLossLimitAmount,
    maxCapitalAtRisk: null,
    positionCap: config.maxConcurrentPositions,
    riskAmountPerTrade: config.riskAmountPerTrade,
    allocation: config.allocation,
    dailyLossLimitPct: config.dailyLossLimitPct,
    maxTradesPerDay: config.maxTradesPerDay,
    source: "config"
  }
}

export function normalizePlan(payload: unknown, config: ConfigInfo | null): Plan | null {
  const sources = containers(payload, "plan", "mandate", "selection")
  const strategyId = str(pick(sources, "strategy_id", "strategy"))
  const status = readStatus(pick(sources, "status", "state", "approval_status"))
  const tradingDate = str(pick(sources, "trading_date", "date"))
  // A backend with nothing published yet answers with an empty object rather than a
  // 404. Nothing to show is not the same as a broken plan.
  if (!strategyId && !tradingDate && status === "unknown") return null

  const regimeRecord = asRecord(pick(sources, "regime"))
  const regimeSources = regimeRecord ? [regimeRecord, ...sources] : sources

  return {
    tradingDate,
    mandateVersion: str(pick(sources, "mandate_version", "version")),
    strategyId: strategyId || NO_TRADE_STRATEGY,
    tradeToday: bool(pick(sources, "trade_today", "will_trade"), strategyId !== NO_TRADE_STRATEGY && !!strategyId),
    regime: str(pick(regimeSources, "label", "regime")),
    regimeDetail: str(pick(regimeSources, "detail", "regime_detail")),
    reason: str(pick(sources, "reason", "selection_reason")),
    rationale: str(pick(sources, "rationale", "narrative", "journal")),
    riskFraction: num(pick(sources, "risk_fraction", "risk_fraction_used")),
    incumbent: str(pick(sources, "incumbent")),
    switched: bool(pick(sources, "switched")),
    candidates: strings(pick(sources, "candidates")),
    viable: strings(pick(sources, "viable")),
    basket: readBasket(pick(sources, "basket", "watchlist", "universe")),
    metrics: readMetrics(pick(sources, "metrics", "metrics_table", "comparison")),
    consequences: readConsequences(sources, config),
    startTime: str(pick(sources, "start_time")),
    endTime: str(pick(sources, "end_time")),
    squareoffTime: str(pick(sources, "squareoff_time")),
    status,
    approvedAt: str(pick(sources, "approved_at", "decided_at")),
    note: str(pick(sources, "note", "operator_note")),
    publishedAt: str(pick(sources, "published_at", "created_at", "generated_at"))
  }
}

/** A 404 means no plan has been published yet, which is the normal state before
 *  08:45 and must not read as an outage. */
export async function getPlan(config: ConfigInfo | null): Promise<Plan | null> {
  try {
    const payload = await request<unknown>("/api/plan")
    return normalizePlan(payload, config)
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) return null
    throw error
  }
}

export interface DecisionResult {
  ok: boolean
  status: PlanStatus
  message: string
}

/** The approval gate. Throws on failure: a human who clicked Approve must never be
 *  left believing a mandate is live when the POST did not land. */
export async function decidePlan(
  approved: boolean,
  options: { mandateVersion?: string; note?: string } = {}
): Promise<DecisionResult> {
  const payload = await request<unknown>("/api/plan/approve", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({
      approved,
      mandate_version: options.mandateVersion || undefined,
      note: options.note || undefined
    })
  })
  const sources = containers(payload, "plan")
  return {
    ok: bool(pick(sources, "ok"), true),
    status: readStatus(pick(sources, "status", "state")),
    message: str(pick(sources, "message", "detail"))
  }
}

// -------------------------------------------------------------------- session

export type RunState = "idle" | "running" | "paused" | "reduce_only" | "halted" | "unknown"

/** Per-symbol machine states from PLAN.md Part 6. Anything unrecognised is kept as
 *  the raw string rather than mapped to a default: a state the UI does not know
 *  about must not be shown as FLAT. */
export type MachineState = string

export interface PositionRow {
  symbol: string
  sector: string
  side: string
  quantity: number | null
  entryPrice: number | null
  stopPrice: number | null
  lastPrice: number | null
  unrealized: number | null
  rMultiple: number | null
  riskAmount: number | null
  state: MachineState
  entryTime: string
}

export interface SymbolRow {
  symbol: string
  sector: string
  state: MachineState
  note: string
}

export interface BreakerRow {
  name: string
  tripped: boolean
  detail: string
}

export interface SessionState {
  tradingDate: string
  runState: RunState
  haltReason: string
  pausedUntil: string
  strategyId: string
  tradeToday: boolean
  mandateVersion: string
  realizedPnl: number | null
  unrealizedPnl: number | null
  mtm: number | null
  budgetLimit: number | null
  budgetUsed: number | null
  budgetUsedPct: number | null
  budgetRemaining: number | null
  tradeCount: number | null
  maxTradesPerDay: number | null
  consecutiveLosses: number | null
  consecutiveLossHalt: number | null
  maxConcurrentPositions: number | null
  positions: PositionRow[]
  symbols: SymbolRow[]
  breakers: BreakerRow[]
  lastBarTime: string
  updatedAt: string
}

function readPositions(value: unknown): PositionRow[] {
  const rows: PositionRow[] = []
  for (const entry of asArray(value)) {
    const record = asRecord(entry)
    if (!record) continue
    const symbol = str(pick([record], "symbol", "tradingsymbol")).trim().toUpperCase()
    if (!symbol) continue
    rows.push({
      symbol,
      sector: str(pick([record], "sector")).toUpperCase(),
      side: str(pick([record], "side", "action", "direction")).toUpperCase(),
      quantity: num(pick([record], "quantity", "qty", "netqty")),
      entryPrice: num(pick([record], "entry_price", "average_price", "avg_price")),
      stopPrice: num(pick([record], "stop_price", "stop", "trigger_price")),
      lastPrice: num(pick([record], "last_price", "ltp", "mark", "mark_price")),
      unrealized: num(pick([record], "unrealized", "unrealized_pnl", "pnl", "mtm")),
      rMultiple: num(pick([record], "r_multiple", "r")),
      riskAmount: num(pick([record], "risk_amount")),
      state: str(pick([record], "state", "machine_state"), "OPEN").toUpperCase(),
      entryTime: str(pick([record], "entry_time", "entry_ts", "fill_ts"))
    })
  }
  return rows
}

/** The per-symbol machine map. Accepts a list of records or a plain
 *  {SYMBOL: "STATE"} object, because both are natural things to serialise. */
function readSymbols(value: unknown): SymbolRow[] {
  const rows: SymbolRow[] = []
  const list = asArray(value)
  if (list.length) {
    for (const entry of list) {
      const record = asRecord(entry)
      if (!record) continue
      const symbol = str(pick([record], "symbol", "tradingsymbol")).trim().toUpperCase()
      if (!symbol) continue
      rows.push({
        symbol,
        sector: str(pick([record], "sector")).toUpperCase(),
        state: str(pick([record], "state", "machine_state"), "UNKNOWN").toUpperCase(),
        note: str(pick([record], "note", "reason", "detail"))
      })
    }
    return rows
  }
  const record = asRecord(value)
  if (!record) return rows
  for (const [symbol, state] of Object.entries(record)) {
    const nested = asRecord(state)
    rows.push({
      symbol: symbol.toUpperCase(),
      sector: nested ? str(pick([nested], "sector")).toUpperCase() : "",
      state: nested
        ? str(pick([nested], "state", "machine_state"), "UNKNOWN").toUpperCase()
        : str(state, "UNKNOWN").toUpperCase(),
      note: nested ? str(pick([nested], "note", "reason", "detail")) : ""
    })
  }
  return rows
}

function readBreakers(value: unknown): BreakerRow[] {
  const rows: BreakerRow[] = []
  for (const entry of asArray(value)) {
    const record = asRecord(entry)
    if (!record) continue
    const name = str(pick([record], "name", "code", "breaker"))
    if (!name) continue
    rows.push({
      name,
      tripped: bool(pick([record], "tripped", "fired", "active")),
      detail: str(pick([record], "detail", "reason", "message"))
    })
  }
  return rows
}

function readRunState(value: unknown): RunState {
  const lowered = str(value).trim().toLowerCase().replace(/[\s-]/g, "_")
  if (["running", "live", "active", "trading"].includes(lowered)) return "running"
  if (["paused", "pause"].includes(lowered)) return "paused"
  if (["reduce_only", "reduceonly"].includes(lowered)) return "reduce_only"
  if (["halted", "halt", "stopped", "killed"].includes(lowered)) return "halted"
  if (["idle", "flat", "not_started", "pending", "waiting", "closed"].includes(lowered)) return "idle"
  return "unknown"
}

export function normalizeSession(payload: unknown): SessionState | null {
  const sources = containers(payload, "session", "state", "live")
  const budget = asRecord(pick(sources, "budget"))
  const budgetSources = budget ? [budget, ...sources] : sources
  const runState = readRunState(pick(sources, "run_state", "state", "status"))
  const tradingDate = str(pick(sources, "trading_date", "date"))
  const positions = readPositions(pick(sources, "positions", "open_positions"))
  const symbols = readSymbols(pick(sources, "symbols", "machines", "machine_states"))

  if (runState === "unknown" && !tradingDate && !positions.length && !symbols.length) return null

  const realized = num(pick(sources, "realized_pnl", "realised_pnl", "realized"))
  const unrealized = num(pick(sources, "unrealized_pnl", "unrealised_pnl", "unrealized"))
  const mtm = num(pick(sources, "mtm", "mtm_pnl", "net_pnl"))

  return {
    tradingDate,
    runState,
    haltReason: str(pick(sources, "halt_reason", "reason")),
    pausedUntil: str(pick(sources, "paused_until")),
    strategyId: str(pick(sources, "strategy_id", "strategy")),
    tradeToday: bool(pick(sources, "trade_today"), true),
    mandateVersion: str(pick(sources, "mandate_version")),
    realizedPnl: realized,
    unrealizedPnl: unrealized,
    // A backend that sends the two legs but not the total should still fill the
    // headline figure, since MTM is the number the daily limit is measured on.
    mtm: mtm !== null ? mtm : realized !== null || unrealized !== null ? (realized ?? 0) + (unrealized ?? 0) : null,
    budgetLimit: num(pick(budgetSources, "limit", "daily_loss_limit_amount", "daily_loss_limit")),
    budgetUsed: num(pick(budgetSources, "used", "budget_used", "consumed")),
    budgetUsedPct: num(pick(budgetSources, "used_pct", "budget_used_pct", "consumed_pct")),
    budgetRemaining: num(pick(budgetSources, "remaining", "budget_remaining", "remaining_budget")),
    tradeCount: num(pick(sources, "trade_count", "trades_today")),
    maxTradesPerDay: num(pick(sources, "max_trades_per_day", "trade_cap")),
    consecutiveLosses: num(pick(sources, "consecutive_losses", "loss_streak")),
    consecutiveLossHalt: num(pick(sources, "consecutive_loss_halt")),
    maxConcurrentPositions: num(pick(sources, "max_concurrent_positions", "position_cap")),
    positions,
    symbols,
    breakers: readBreakers(pick(sources, "breakers")),
    lastBarTime: str(pick(sources, "last_bar_time", "last_bar_ts", "last_bar")),
    updatedAt: str(pick(sources, "updated_at", "as_of", "timestamp"))
  }
}

export async function getSession(): Promise<SessionState | null> {
  try {
    const payload = await request<unknown>("/api/session")
    return normalizeSession(payload)
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) return null
    throw error
  }
}

/** True when there is something to halt. Anything short of this and the emergency
 *  control is shown disabled rather than hidden, so its position never moves. */
export function isSessionLive(session: SessionState | null): boolean {
  if (!session) return false
  return session.runState === "running" || session.runState === "paused" || session.runState === "reduce_only"
}

// --------------------------------------------------------------------- trades

export interface TradeRow {
  intentId: string
  symbol: string
  side: string
  strategyId: string
  entryTime: string
  entryPrice: number | null
  quantity: number | null
  stopPrice: number | null
  exitTime: string
  exitPrice: number | null
  exitReason: string
  grossPnl: number | null
  costs: number | null
  netPnl: number | null
  rMultiple: number | null
  riskAmount: number | null
  state: string
}

export interface EquityPoint {
  time: string
  /** Cumulative net P&L in rupees at this point on the curve. */
  value: number
  label: string
}

export interface TradesPayload {
  trades: TradeRow[]
  equity: EquityPoint[]
  /** True when the curve came from the backend rather than being accumulated here. */
  equityFromServer: boolean
}

function readTrades(value: unknown): TradeRow[] {
  const rows: TradeRow[] = []
  for (const entry of asArray(value)) {
    const record = asRecord(entry)
    if (!record) continue
    const symbol = str(pick([record], "symbol", "tradingsymbol")).trim().toUpperCase()
    if (!symbol) continue
    rows.push({
      intentId: str(pick([record], "intent_id", "id")),
      symbol,
      side: str(pick([record], "side", "action", "direction")).toUpperCase(),
      strategyId: str(pick([record], "strategy_id", "strategy")),
      entryTime: str(pick([record], "entry_time", "entry_ts")),
      entryPrice: num(pick([record], "entry_price", "fill_price")),
      quantity: num(pick([record], "quantity", "qty", "fill_qty")),
      stopPrice: num(pick([record], "stop_price", "stop")),
      exitTime: str(pick([record], "exit_time", "exit_ts")),
      exitPrice: num(pick([record], "exit_price")),
      exitReason: str(pick([record], "exit_reason", "reason")),
      grossPnl: num(pick([record], "gross_pnl")),
      costs: num(pick([record], "costs", "charges")),
      netPnl: num(pick([record], "net_pnl", "pnl")),
      rMultiple: num(pick([record], "r_multiple", "r")),
      riskAmount: num(pick([record], "risk_amount")),
      state: str(pick([record], "state")).toUpperCase()
    })
  }
  return rows
}

function readEquity(value: unknown): EquityPoint[] {
  const points: EquityPoint[] = []
  for (const entry of asArray(value)) {
    const record = asRecord(entry)
    if (record) {
      const time = str(pick([record], "time", "timestamp", "ts", "exit_time", "date"))
      const amount = num(pick([record], "value", "equity", "cum_pnl", "pnl"))
      if (amount === null) continue
      points.push({ time, value: amount, label: str(pick([record], "label", "symbol")) })
      continue
    }
    // A bare array of numbers is a curve with implicit ordering and no stamps.
    const amount = num(entry)
    if (amount !== null) points.push({ time: "", value: amount, label: "" })
  }
  return points
}

/** Accumulates the curve from the trade list when the backend does not send one.
 *  Ordered by exit time, because a trade only lands on the curve when it closes. */
function equityFromTrades(trades: TradeRow[]): EquityPoint[] {
  const closed = trades
    .filter((trade) => trade.exitTime && trade.netPnl !== null)
    .slice()
    .sort((a, b) => Date.parse(a.exitTime) - Date.parse(b.exitTime))
  let running = 0
  return closed.map((trade) => {
    running += trade.netPnl ?? 0
    return { time: trade.exitTime, value: running, label: trade.symbol }
  })
}

export async function getTrades(): Promise<TradesPayload> {
  const payload = await request<unknown>("/api/trades")
  const root = asRecord(payload)
  const sources = containers(payload, "trades")
  const rawTrades = Array.isArray(payload)
    ? payload
    : pick(sources, "trades", "items", "rows") ?? root?.data
  const trades = readTrades(rawTrades)
  const serverEquity = readEquity(pick(sources, "equity", "equity_curve", "curve"))
  return {
    trades,
    equity: serverEquity.length ? serverEquity : equityFromTrades(trades),
    equityFromServer: serverEquity.length > 0
  }
}

// -------------------------------------------------------------------- control

export interface ActionResult {
  ok: boolean
  state: RunState
  message: string
}

async function control(path: string, reason: string): Promise<ActionResult> {
  const payload = await request<unknown>(path, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({ reason })
  })
  const sources = containers(payload, "session")
  return {
    ok: bool(pick(sources, "ok"), true),
    state: readRunState(pick(sources, "run_state", "state", "status")),
    message: str(pick(sources, "message", "detail", "reason"))
  }
}

/** The emergency stop. Cancels pending orders, flattens, then sets the halt state -
 *  in that order, server side. Throws on failure so the UI can say it did not land
 *  rather than showing a stop that never happened. */
export function postHalt(reason: string): Promise<ActionResult> {
  return control("/api/halt", reason)
}

/** The state between running and halted: may close, may not open. Hard-stopping
 *  while a position is open traps that position; this is the answer to that. */
export function postReduceOnly(reason: string): Promise<ActionResult> {
  return control("/api/reduce-only", reason)
}

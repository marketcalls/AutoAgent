/** The session view. Everything a human needs to decide whether to intervene.
 *
 * The budget bar is the centre of this screen, and it is drawn as CONSUMPTION of a
 * fixed daily loss limit rather than as a P&L line, because that is the shape of the
 * rule underneath: PLAN.md Part 5 spends the limit as a forward-looking budget, and
 * the two graduated responses - halve the risk fraction at 50 percent, flat and
 * halted at 100 - are marks on that bar. A P&L chart would show the same numbers and
 * none of the thresholds.
 *
 * Loss here means realized PLUS unrealized. A board that showed only realized P&L
 * would read calm while three open positions sat deeply under water, which is
 * exactly the moment the operator needs to see it.
 *
 * Numbers are preferred from the server. Where the backend sends the parts but not
 * the total, the total is completed here from those parts and nothing else - no
 * figure on this board is invented locally.
 */

import { Activity, CircleAlert, CircleCheck, Clock } from "lucide-react"

import { RunStateBadge, StateBadge } from "@/components/StateBadge"
import { StatTile } from "@/components/StatTile"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { cn } from "@/lib/utils"
import {
  EMPTY,
  formatAge,
  formatCurrency,
  formatNumber,
  formatPercent,
  formatPrice,
  formatQuantity,
  formatSignedCurrency,
  pnlClass
} from "@/lib/format"
import type { ConfigInfo, SessionState } from "@/lib/api"

/** Where the risk fraction is halved, and where the session stops. Both are
 *  thresholds in the risk engine, not chart decoration. */
const HALVE_AT_PCT = 50

interface LimitRow {
  label: string
  /** The reading, already formatted as "n of m" - the denominator is the point. */
  detail: string
  tripped: boolean
}

function budgetTone(pct: number | null): { fill: string; text: string } {
  if (pct === null) return { fill: "bg-muted", text: "text-muted-foreground" }
  if (pct >= 80) return { fill: "bg-danger", text: "text-danger" }
  if (pct >= HALVE_AT_PCT) return { fill: "bg-warn", text: "text-warn" }
  return { fill: "bg-primary", text: "text-foreground" }
}

function BudgetBar({ pct, tone }: { pct: number | null; tone: string }) {
  const width = pct === null ? 0 : Math.max(0, Math.min(100, pct))
  return (
    <div className="relative h-3 w-full overflow-hidden rounded-full bg-muted">
      <div className={cn("h-full rounded-full transition-[width]", tone)} style={{ width: `${width}%` }} />
      <div
        className="absolute inset-y-0 w-px bg-border"
        style={{ left: `${HALVE_AT_PCT}%` }}
        aria-hidden="true"
      />
    </div>
  )
}

function LimitList({ rows }: { rows: LimitRow[] }) {
  return (
    <div className="grid gap-2 sm:grid-cols-2">
      {rows.map((row) => (
        <div
          key={row.label}
          className={cn(
            "flex items-center justify-between gap-3 rounded-md border px-3 py-2 text-sm",
            row.tripped ? "border-danger-border bg-danger-soft" : "border-border bg-panel"
          )}
        >
          <div className="flex min-w-0 items-center gap-2">
            {row.tripped ? (
              <CircleAlert className="size-4 shrink-0 text-danger" />
            ) : (
              <CircleCheck className="size-4 shrink-0 text-muted-foreground" />
            )}
            <span className="truncate">{row.label}</span>
          </div>
          <span className={cn("tnum shrink-0 text-xs", row.tripped ? "text-danger" : "text-muted-foreground")}>
            {row.detail}
          </span>
        </div>
      ))}
    </div>
  )
}

export interface LiveBoardProps {
  session: SessionState
  config: ConfigInfo | null
  streamConnected: boolean
}

export function LiveBoard({ session, config, streamConnected }: LiveBoardProps) {
  const limit = session.budgetLimit ?? config?.dailyLossLimitAmount ?? null
  // Consumption is the loss side only. A profitable session has consumed nothing,
  // which is different from having a negative consumption.
  const used =
    session.budgetUsed ?? (session.mtm !== null ? Math.max(0, -session.mtm) : null)
  const usedPct =
    session.budgetUsedPct ?? (used !== null && limit ? (100 * used) / limit : null)
  const remaining =
    session.budgetRemaining ?? (limit !== null && used !== null ? Math.max(0, limit - used) : null)
  const tone = budgetTone(usedPct)

  const tradeCap = session.maxTradesPerDay ?? config?.maxTradesPerDay ?? null
  const lossHalt = session.consecutiveLossHalt ?? config?.consecutiveLossHalt ?? null
  const positionCap = session.maxConcurrentPositions ?? config?.maxConcurrentPositions ?? null

  // The per-symbol machine strip falls back to the configured basket so the strip
  // has the same five slots all session, even before the executor reports any.
  const symbolRows = session.symbols.length
    ? session.symbols
    : (config?.basket ?? []).map((entry) => ({
        symbol: entry.symbol,
        sector: entry.sector,
        state: "UNREPORTED",
        note: ""
      }))

  const trippedBreakers = session.breakers.filter((breaker) => breaker.tripped)

  const limitRows: LimitRow[] = [
    {
      label: "Daily loss budget",
      detail:
        usedPct === null
          ? EMPTY
          : `${formatPercent(usedPct, 1)} of ${formatCurrency(limit, 0)}`,
      tripped: usedPct !== null && usedPct >= 100
    },
    {
      label: "Consecutive losses",
      detail:
        session.consecutiveLosses === null
          ? EMPTY
          : `${formatNumber(session.consecutiveLosses, 0)} of ${lossHalt === null ? EMPTY : formatNumber(lossHalt, 0)}`,
      tripped:
        session.consecutiveLosses !== null && lossHalt !== null && session.consecutiveLosses >= lossHalt
    },
    {
      label: "Trades today",
      detail:
        session.tradeCount === null
          ? EMPTY
          : `${formatNumber(session.tradeCount, 0)} of ${tradeCap === null ? EMPTY : formatNumber(tradeCap, 0)}`,
      tripped: session.tradeCount !== null && tradeCap !== null && session.tradeCount >= tradeCap
    },
    {
      label: "Positions open",
      detail: `${formatNumber(session.positions.length, 0)} of ${positionCap === null ? EMPTY : formatNumber(positionCap, 0)}`,
      tripped: positionCap !== null && session.positions.length >= positionCap
    }
  ]

  return (
    <Card>
      <CardHeader className="flex-row flex-wrap items-center justify-between gap-3">
        <div className="flex flex-col gap-1">
          <CardTitle>Session</CardTitle>
          <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            {session.strategyId ? <span className="font-mono">{session.strategyId}</span> : null}
            {session.mandateVersion ? <span>mandate {session.mandateVersion}</span> : null}
            {session.lastBarTime ? (
              <span className="inline-flex items-center gap-1">
                <Clock className="size-3" />
                last bar {formatAge(session.lastBarTime)}
              </span>
            ) : null}
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant={streamConnected ? "muted" : "warn"}>
            <Activity className="size-3" />
            {streamConnected ? "live feed" : "feed dropped"}
          </Badge>
          {session.executorAttached ? null : <Badge variant="warn">no executor attached</Badge>}
          <RunStateBadge state={session.runState} />
        </div>
      </CardHeader>

      <CardContent className="flex flex-col gap-6">
        {session.executorAttached ? null : (
          <div className="rounded-md border border-warn-border bg-warn-soft px-4 py-3 text-sm text-warn">
            <span className="font-medium">No executor is attached. </span>
            The run state and the limits below are real, but the position book and the P&amp;L figures
            are UNKNOWN rather than flat - nothing has handed over a live book to read them from.
            {session.source ? <span className="tnum"> Source: {session.source}.</span> : null}
          </div>
        )}
        {session.haltReason ? (
          <div
            className={cn(
              "rounded-md border px-4 py-3 text-sm",
              session.runState === "halted"
                ? "border-danger-border bg-danger-soft text-danger"
                : "border-warn-border bg-warn-soft text-warn"
            )}
          >
            <span className="font-medium">
              {session.runState === "halted" ? "Halted" : "Restricted"}:{" "}
            </span>
            {session.haltReason}
            {session.pausedUntil ? <span> Resumes {session.pausedUntil}.</span> : null}
          </div>
        ) : null}

        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <StatTile
            label="MTM"
            value={formatSignedCurrency(session.mtm, 0)}
            detail={`realized ${formatSignedCurrency(session.realizedPnl, 0)}, open ${formatSignedCurrency(session.unrealizedPnl, 0)}`}
            tone={pnlClass(session.mtm)}
          />
          <StatTile
            label="Budget consumed"
            value={usedPct === null ? EMPTY : formatPercent(usedPct, 1)}
            detail={
              remaining === null
                ? "of the daily loss limit"
                : `${formatCurrency(remaining, 0)} of room left`
            }
            tone={tone.text}
          />
          <StatTile
            label="Trades"
            value={session.tradeCount === null ? EMPTY : formatNumber(session.tradeCount, 0)}
            detail={tradeCap === null ? "today" : `of ${formatNumber(tradeCap, 0)} allowed today`}
          />
          <StatTile
            label="Consecutive losses"
            value={
              session.consecutiveLosses === null ? EMPTY : formatNumber(session.consecutiveLosses, 0)
            }
            detail={lossHalt === null ? "net of costs" : `halts at ${formatNumber(lossHalt, 0)}`}
            tone={
              session.consecutiveLosses !== null && lossHalt !== null && session.consecutiveLosses >= lossHalt - 1
                ? "text-warn"
                : undefined
            }
          />
        </div>

        <div className="flex flex-col gap-2">
          <div className="flex items-baseline justify-between text-xs">
            <span className="font-medium tracking-wide text-muted-foreground uppercase">
              Daily loss budget
            </span>
            <span className="tnum text-muted-foreground">
              {formatCurrency(used, 0)} spent of {formatCurrency(limit, 0)}
            </span>
          </div>
          <BudgetBar pct={usedPct} tone={tone.fill} />
          <div className="flex justify-between text-[11px] text-muted-foreground">
            <span>0</span>
            <span>risk fraction halves at {HALVE_AT_PCT}%</span>
            <span>flat and halted at 100%</span>
          </div>
        </div>

        <div className="flex flex-col gap-2">
          <div className="text-[11px] font-medium tracking-wide text-muted-foreground uppercase">
            Open positions
          </div>
          {session.positions.length ? (
            <div className="rounded-lg border border-border">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Symbol</TableHead>
                    <TableHead>State</TableHead>
                    <TableHead>Side</TableHead>
                    <TableHead className="text-right">Quantity</TableHead>
                    <TableHead className="text-right">Entry</TableHead>
                    <TableHead className="text-right">Stop</TableHead>
                    <TableHead className="text-right">Last</TableHead>
                    <TableHead className="text-right">Unrealized</TableHead>
                    <TableHead className="text-right">Risk left</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {session.positions.map((position) => (
                    <TableRow key={position.symbol}>
                      <TableCell className="font-mono font-medium">{position.symbol}</TableCell>
                      <TableCell>
                        <StateBadge state={position.state} />
                      </TableCell>
                      <TableCell>
                        <Badge variant={position.side === "SELL" ? "danger" : "success"}>
                          {position.side || EMPTY}
                        </Badge>
                      </TableCell>
                      <TableCell className="tnum text-right">{formatQuantity(position.quantity)}</TableCell>
                      <TableCell className="tnum text-right">{formatPrice(position.entryPrice)}</TableCell>
                      <TableCell className="tnum text-right">{formatPrice(position.stopPrice)}</TableCell>
                      <TableCell className="tnum text-right">{formatPrice(position.lastPrice)}</TableCell>
                      <TableCell className={cn("tnum text-right", pnlClass(position.unrealized))}>
                        {formatSignedCurrency(position.unrealized, 0)}
                      </TableCell>
                      <TableCell
                        className="tnum text-right text-muted-foreground"
                        title="Loss still on the table if the stop fills from here. This is what the budget reserves against."
                      >
                        {formatCurrency(position.worstCaseRemaining, 0)}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          ) : (
            <p className="rounded-lg border border-border bg-panel px-4 py-3 text-sm text-muted-foreground">
              {session.executorAttached
                ? "No open positions."
                : "No position book has been reported. This is unknown, not flat."}
            </p>
          )}
        </div>

        <div className="flex flex-col gap-2">
          <div className="text-[11px] font-medium tracking-wide text-muted-foreground uppercase">
            State machine, one per symbol
          </div>
          {symbolRows.length ? (
            <div className="flex flex-wrap gap-2">
              {symbolRows.map((row) => (
                <div
                  key={row.symbol}
                  className="flex items-center gap-2 rounded-md border border-border bg-panel px-3 py-2"
                  title={row.note || undefined}
                >
                  <span className="font-mono text-sm font-medium">{row.symbol}</span>
                  <StateBadge state={row.state} />
                </div>
              ))}
            </div>
          ) : (
            <p className="text-sm text-muted-foreground">No symbol states reported.</p>
          )}
        </div>

        <div className="flex flex-col gap-2">
          <div className="text-[11px] font-medium tracking-wide text-muted-foreground uppercase">
            Limits and breakers
          </div>
          <LimitList rows={limitRows} />
          {/* Only the tripped ones are named. A wall of untripped badges makes the one
              that matters harder to find, and the numeric state is already above. */}
          {session.breakers.length ? (
            <div className="mt-2 flex flex-wrap items-center gap-2">
              <span className="text-xs text-muted-foreground">Tripped:</span>
              {trippedBreakers.length ? (
                trippedBreakers.map((breaker) => (
                  <Badge key={breaker.name} variant="danger" title={breaker.detail}>
                    {breaker.name}
                  </Badge>
                ))
              ) : (
                <Badge variant="muted">nothing</Badge>
              )}
            </div>
          ) : null}
        </div>
      </CardContent>
    </Card>
  )
}

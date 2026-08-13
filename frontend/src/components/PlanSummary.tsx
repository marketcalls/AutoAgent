/** The evidence behind the morning pick: regime read, metrics table, basket.
 *
 * This is the half of the mandate card that answers "why", and it is deliberately
 * separate from the half that answers "what will it cost me". The metrics table is
 * the thing that makes a "trade nothing" plan legible rather than alarming - it is
 * where a human sees that all three strategies really are under water and that
 * declining to trade is the measured answer, not a failure.
 */

import { Badge } from "@/components/ui/badge"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import {
  EMPTY,
  formatCompactCurrency,
  formatNumber,
  formatPercent,
  formatSigned
} from "@/lib/format"
import type { MetricRow, Plan } from "@/lib/api"

const REGIME_TONE: Record<string, "success" | "warn" | "muted"> = {
  trend_up: "success",
  trend_down: "success",
  chop: "warn"
}

function windowLabel(window: string): string {
  const lowered = window.trim().toLowerCase()
  if (lowered === "long") return "long"
  if (lowered === "short") return "short"
  return window || EMPTY
}

/** Whether the selector counted this strategy as viable, or null when it cannot be
 *  said from the payload.
 *
 *  The metrics rows carry `is_viable` as a Python property, and asdict() drops
 *  properties, so the field is usually absent. The selector's own answer survives
 *  though - it publishes the list of strategies that cleared the gate - and that list
 *  is the server's verdict rather than a rule reimplemented here. It applies to the
 *  LONG window only, because that is the window the viability gate reads; marking a
 *  short-window row from it would be attributing a verdict to the wrong sample. */
function viabilityOf(row: MetricRow, viable: string[], candidates: string[]): boolean | null {
  if (row.viable !== null) return row.viable
  if (row.window.trim().toLowerCase() !== "long") return null
  if (!candidates.includes(row.strategyId)) return null
  return viable.includes(row.strategyId)
}

function MetricsTable({
  rows,
  selected,
  viable,
  candidates
}: {
  rows: MetricRow[]
  selected: string
  viable: string[]
  candidates: string[]
}) {
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Strategy</TableHead>
          <TableHead>Window</TableHead>
          <TableHead className="text-right">Trades</TableHead>
          <TableHead className="text-right">Win rate</TableHead>
          <TableHead className="text-right">Expectancy</TableHead>
          <TableHead className="text-right">Profit factor</TableHead>
          <TableHead className="text-right">Net</TableHead>
          <TableHead className="text-right">Max DD</TableHead>
          <TableHead className="text-right">Sharpe</TableHead>
          <TableHead>Viable</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {rows.map((row) => {
          const isSelected = row.strategyId === selected
          const isViable = viabilityOf(row, viable, candidates)
          return (
            <TableRow key={`${row.strategyId}:${row.window}`} className={isSelected ? "bg-primary-soft" : undefined}>
              <TableCell className="font-mono text-xs">{row.strategyId}</TableCell>
              <TableCell className="text-xs text-muted-foreground">{windowLabel(row.window)}</TableCell>
              <TableCell className="tnum text-right">
                {row.trades === null ? EMPTY : formatNumber(row.trades, 0)}
                {row.significant === false ? (
                  <span className="ml-1 text-xs text-warn" title="Too few trades to be significant">
                    thin
                  </span>
                ) : null}
              </TableCell>
              <TableCell className="tnum text-right">{formatPercent(row.winRate, 1)}</TableCell>
              <TableCell className="tnum text-right">
                {row.expectancyR === null ? EMPTY : `${formatSigned(row.expectancyR, 3)}R`}
              </TableCell>
              <TableCell className="tnum text-right">{formatNumber(row.profitFactor, 2)}</TableCell>
              <TableCell className="tnum text-right">{formatCompactCurrency(row.netPnl)}</TableCell>
              <TableCell className="tnum text-right">{formatPercent(row.maxDrawdownPct, 1)}</TableCell>
              <TableCell className="tnum text-right">{formatNumber(row.sharpe, 2)}</TableCell>
              <TableCell>
                {isViable === null ? (
                  <span className="text-xs text-muted-foreground">{EMPTY}</span>
                ) : (
                  <Badge variant={isViable ? "success" : "muted"}>{isViable ? "yes" : "no"}</Badge>
                )}
              </TableCell>
            </TableRow>
          )
        })}
      </TableBody>
    </Table>
  )
}

export function PlanSummary({ plan }: { plan: Plan }) {
  const regimeTone = REGIME_TONE[plan.regime.trim().toLowerCase()] ?? "muted"

  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-col gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-xs font-medium tracking-wide text-muted-foreground uppercase">Regime</span>
          <Badge variant={regimeTone} className="font-mono">
            {plan.regime || "not reported"}
          </Badge>
          {plan.switched ? <Badge variant="info">switched from {plan.incumbent || "none"}</Badge> : null}
        </div>
        {plan.regimeDetail ? <p className="text-sm text-muted-foreground">{plan.regimeDetail}</p> : null}
      </div>

      {plan.reason ? (
        <div className="rounded-lg border border-border bg-panel px-4 py-3">
          <div className="text-[11px] font-medium tracking-wide text-muted-foreground uppercase">
            Selector reason
          </div>
          <p className="mt-1 text-sm">{plan.reason}</p>
        </div>
      ) : null}

      {plan.rationale ? (
        <div>
          <div className="text-[11px] font-medium tracking-wide text-muted-foreground uppercase">
            Planner rationale
          </div>
          <p className="mt-1 text-sm leading-relaxed text-muted-foreground">{plan.rationale}</p>
        </div>
      ) : null}

      <div>
        <div className="mb-2 text-[11px] font-medium tracking-wide text-muted-foreground uppercase">
          Basket, in tie-break priority order
        </div>
        {plan.basket.length ? (
          <div className="flex flex-wrap gap-2">
            {plan.basket.map((entry, index) => (
              <Badge key={entry.symbol} variant="outline" className="gap-2">
                <span className="tnum text-muted-foreground">{index + 1}</span>
                <span className="font-mono text-foreground">{entry.symbol}</span>
                {entry.sector ? <span className="text-muted-foreground">{entry.sector}</span> : null}
              </Badge>
            ))}
          </div>
        ) : (
          <p className="text-sm text-muted-foreground">The plan carried no basket.</p>
        )}
      </div>

      <div>
        <div className="mb-2 text-[11px] font-medium tracking-wide text-muted-foreground uppercase">
          Strategy metrics, both windows
        </div>
        {plan.metrics.length ? (
          <div className="rounded-lg border border-border">
            <MetricsTable
              rows={plan.metrics}
              selected={plan.strategyId}
              viable={plan.viable}
              candidates={plan.candidates}
            />
          </div>
        ) : (
          <p className="text-sm text-muted-foreground">
            The backend published no metrics table with this plan.
          </p>
        )}
      </div>
    </div>
  )
}

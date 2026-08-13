/** Post-session review: every round trip, with the numbers that decide the day.
 *
 * One row per TRADE, not per order. A trade is three orders - entry, stop, exit - and
 * the intent record is what ties them together (PLAN.md Appendix B), so the intent id
 * is shown where the backend supplies it: it is the key that leads back to the audit
 * rows for the individual orders.
 *
 * Costs get their own column rather than being folded into net. A trade 50 rupees up
 * gross that paid 80 in brokerage is a loss, and it is counted as one by the breaker
 * that halts on three of them - so the column that explains why has to be visible.
 */

import { StatTile } from "@/components/StatTile"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { cn } from "@/lib/utils"
import {
  EMPTY,
  formatClock,
  formatCurrency,
  formatNumber,
  formatPercent,
  formatPrice,
  formatQuantity,
  formatR,
  formatSignedCurrency,
  pnlClass
} from "@/lib/format"
import type { TradeRow } from "@/lib/api"

function totals(trades: TradeRow[]) {
  let gross = 0
  let costs = 0
  let net = 0
  let wins = 0
  let decided = 0
  for (const trade of trades) {
    gross += trade.grossPnl ?? 0
    costs += trade.costs ?? 0
    net += trade.netPnl ?? 0
    if (trade.netPnl === null) continue
    decided += 1
    if (trade.netPnl > 0) wins += 1
  }
  return { gross, costs, net, wins, decided }
}

export function TradeTable({ trades }: { trades: TradeRow[] }) {
  const summary = totals(trades)

  return (
    <Card>
      <CardHeader>
        <CardTitle>Trades</CardTitle>
        <p className="text-xs text-muted-foreground">
          One row per round trip. P&amp;L is net of costs, which is also how the loss streak counts.
        </p>
      </CardHeader>
      <CardContent className="flex flex-col gap-5">
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <StatTile label="Trades" value={formatNumber(trades.length, 0)} detail="closed and open" />
          <StatTile
            label="Net P&L"
            value={formatSignedCurrency(summary.net, 0)}
            detail={`gross ${formatSignedCurrency(summary.gross, 0)}`}
            tone={pnlClass(summary.net)}
          />
          <StatTile
            label="Costs"
            value={formatCurrency(summary.costs, 0)}
            detail="brokerage, taxes and slippage"
          />
          <StatTile
            label="Win rate"
            value={summary.decided ? formatPercent((100 * summary.wins) / summary.decided, 1) : EMPTY}
            detail={`${formatNumber(summary.wins, 0)} of ${formatNumber(summary.decided, 0)} decided`}
          />
        </div>

        {trades.length ? (
          <div className="rounded-lg border border-border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Symbol</TableHead>
                  <TableHead>Side</TableHead>
                  <TableHead className="text-right">Quantity</TableHead>
                  <TableHead>Entry</TableHead>
                  <TableHead className="text-right">Price</TableHead>
                  <TableHead className="text-right">Stop</TableHead>
                  <TableHead>Exit</TableHead>
                  <TableHead className="text-right">Price</TableHead>
                  <TableHead>Reason</TableHead>
                  <TableHead className="text-right">Gross</TableHead>
                  <TableHead className="text-right">Costs</TableHead>
                  <TableHead className="text-right">Net</TableHead>
                  <TableHead className="text-right">R</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {trades.map((trade, index) => (
                  <TableRow key={trade.intentId || `${trade.symbol}:${trade.entryTime}:${index}`}>
                    <TableCell className="font-mono font-medium" title={trade.intentId || undefined}>
                      {trade.symbol}
                    </TableCell>
                    <TableCell>
                      <Badge variant={trade.side === "SELL" ? "danger" : "success"}>
                        {trade.side || EMPTY}
                      </Badge>
                    </TableCell>
                    <TableCell className="tnum text-right">{formatQuantity(trade.quantity)}</TableCell>
                    <TableCell className="tnum text-muted-foreground">{formatClock(trade.entryTime)}</TableCell>
                    <TableCell className="tnum text-right">{formatPrice(trade.entryPrice)}</TableCell>
                    <TableCell className="tnum text-right text-muted-foreground">
                      {formatPrice(trade.stopPrice)}
                    </TableCell>
                    <TableCell className="tnum text-muted-foreground">
                      {trade.exitTime ? formatClock(trade.exitTime) : "open"}
                    </TableCell>
                    <TableCell className="tnum text-right">{formatPrice(trade.exitPrice)}</TableCell>
                    <TableCell className="text-xs text-muted-foreground">
                      {trade.exitReason || EMPTY}
                    </TableCell>
                    <TableCell className={cn("tnum text-right", pnlClass(trade.grossPnl))}>
                      {formatSignedCurrency(trade.grossPnl, 0)}
                    </TableCell>
                    <TableCell className="tnum text-right text-muted-foreground">
                      {formatCurrency(trade.costs, 0)}
                    </TableCell>
                    <TableCell className={cn("tnum text-right font-medium", pnlClass(trade.netPnl))}>
                      {formatSignedCurrency(trade.netPnl, 0)}
                    </TableCell>
                    <TableCell className={cn("tnum text-right", pnlClass(trade.rMultiple))}>
                      {formatR(trade.rMultiple)}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        ) : (
          <p className="rounded-lg border border-border bg-panel px-4 py-3 text-sm text-muted-foreground">
            No trades recorded. On a day the selector declined to trade, this is the correct outcome.
          </p>
        )}
      </CardContent>
    </Card>
  )
}

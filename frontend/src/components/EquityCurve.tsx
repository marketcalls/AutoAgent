/** Cumulative net P&L across the session's closed trades.
 *
 * Choices worth recording:
 *
 *   NET, NOT GROSS. Indian intraday costs are material against a 1.5R target, and a
 *   gross curve is the single easiest way to make a losing strategy look like a
 *   winning one. The backend's own metrics are net; so is this.
 *
 *   X IS TRADE SEQUENCE, NOT WALL CLOCK. Intraday trades cluster - three can close
 *   within a minute and then nothing for two hours - so a time axis would draw a flat
 *   line with a cliff in it. The tooltip carries the real IST timestamp, which is
 *   where the time actually matters.
 *
 *   ZERO IS ALWAYS IN FRAME. An axis that auto-fits to a curve entirely below zero
 *   hides the only line on the chart that means anything.
 *
 *   ONE SERIES, SO NO LEGEND. The title names it. Colour carries polarity only: the
 *   curve is green when the session ends up and red when it ends down, which is the
 *   same rule pnlClass applies to every number on the board.
 */

import {
  Area,
  AreaChart,
  CartesianGrid,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis
} from "recharts"

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { formatCompactCurrency, formatClock, formatSignedCurrency } from "@/lib/format"
import { cn } from "@/lib/utils"
import type { EquityPoint } from "@/lib/api"

interface ChartDatum {
  index: number
  value: number
  time: string
  label: string
}

interface TooltipEntry {
  payload?: ChartDatum
}

function CurveTooltip({ active, payload }: { active?: boolean; payload?: TooltipEntry[] }) {
  const point = active ? payload?.[0]?.payload : undefined
  if (!point) return null
  return (
    <div className="rounded-md border border-border bg-popover px-3 py-2 text-xs shadow-l">
      <div className="tnum font-medium text-popover-foreground">
        {formatSignedCurrency(point.value, 0)}
      </div>
      <div className="mt-0.5 text-muted-foreground">
        trade {point.index}
        {point.label ? ` - ${point.label}` : ""}
        {point.time ? ` - ${formatClock(point.time)}` : ""}
      </div>
    </div>
  )
}

export interface EquityCurveProps {
  points: EquityPoint[]
  /** False when the curve was accumulated from the trade list rather than sent. */
  fromServer: boolean
}

export function EquityCurve({ points, fromServer }: EquityCurveProps) {
  const data: ChartDatum[] = points.map((point, index) => ({
    index: index + 1,
    value: point.value,
    time: point.time,
    label: point.label
  }))
  const final = data.length ? data[data.length - 1].value : 0
  const positive = final >= 0
  const stroke = positive ? "var(--success)" : "var(--danger)"

  return (
    <Card>
      <CardHeader className="flex-row flex-wrap items-baseline justify-between gap-3">
        <div className="flex flex-col gap-1">
          <CardTitle>Equity curve</CardTitle>
          <p className="text-xs text-muted-foreground">
            Cumulative net P&amp;L, after costs, across closed trades.
            {fromServer ? "" : " Accumulated from the trade list, which the backend did not send a curve for."}
          </p>
        </div>
        <div className={cn("tnum text-lg font-semibold", positive ? "text-success" : "text-danger")}>
          {data.length ? formatSignedCurrency(final, 0) : null}
        </div>
      </CardHeader>
      <CardContent>
        {data.length ? (
          <div className="h-64 w-full">
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={data} margin={{ top: 8, right: 8, bottom: 4, left: 8 }}>
                <defs>
                  <linearGradient id="equity-fill" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor={stroke} stopOpacity={0.28} />
                    <stop offset="100%" stopColor={stroke} stopOpacity={0.02} />
                  </linearGradient>
                </defs>
                <CartesianGrid stroke="var(--border)" strokeDasharray="3 3" vertical={false} />
                <XAxis
                  dataKey="index"
                  tickLine={false}
                  axisLine={{ stroke: "var(--border)" }}
                  tick={{ fill: "var(--muted-foreground)", fontSize: 11 }}
                  minTickGap={24}
                />
                <YAxis
                  tickLine={false}
                  axisLine={false}
                  width={68}
                  tick={{ fill: "var(--muted-foreground)", fontSize: 11 }}
                  tickFormatter={(value: number) => formatCompactCurrency(value)}
                  domain={[
                    (min: number) => Math.min(0, min),
                    (max: number) => Math.max(0, max)
                  ]}
                />
                <ReferenceLine y={0} stroke="var(--muted-foreground)" strokeDasharray="4 4" />
                <Tooltip
                  content={<CurveTooltip />}
                  cursor={{ stroke: "var(--muted-foreground)", strokeDasharray: "4 4" }}
                />
                <Area
                  type="monotone"
                  dataKey="value"
                  stroke={stroke}
                  strokeWidth={2}
                  fill="url(#equity-fill)"
                  dot={false}
                  activeDot={{ r: 4, strokeWidth: 0, fill: stroke }}
                  isAnimationActive={false}
                />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        ) : (
          <p className="rounded-lg border border-border bg-panel px-4 py-3 text-sm text-muted-foreground">
            No closed trades yet, so there is no curve to draw.
          </p>
        )}
      </CardContent>
    </Card>
  )
}

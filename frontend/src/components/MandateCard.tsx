/** The pre-session approval gate. The one screen a human is guaranteed to read.
 *
 * Two things this card exists to get right.
 *
 * 1. THE NUMBERS ARE THE BACKEND'S, NOT THE PLAN'S PROSE. Same rule as TradingAgent's
 *    confirmation card: a model can write "a conservative day" above any risk
 *    fraction at all. What is approved here is the worst case in rupees, the capital
 *    at risk and the position cap, and every one of those is read from the server.
 *    Where the plan did not carry them the card says where they came from instead of
 *    quietly computing something that looks authoritative.
 *
 * 2. "TRADE NOTHING" IS A RESULT, NOT AN ERROR. The selector returns strategy "none"
 *    with trade_today false whenever no strategy clears the viability gate, and after
 *    the step 4 backtest measured all three at negative expectancy that is the
 *    expected morning outcome rather than a rare one. It is rendered as a decision
 *    the system made and can defend - the metrics table below is the defence - not as
 *    a red failure. Approving it is approving a flat day.
 */

import { useState } from "react"
import { CircleCheckBig, CircleSlash, TriangleAlert } from "lucide-react"

import { PlanSummary } from "@/components/PlanSummary"
import { StatTile } from "@/components/StatTile"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardFooter, CardHeader, CardTitle } from "@/components/ui/card"
import { Separator } from "@/components/ui/separator"
import {
  EMPTY,
  formatCurrency,
  formatDate,
  formatDateTime,
  formatFractionAsPercent,
  formatNumber,
  formatPercent,
  formatSessionTime
} from "@/lib/format"
import { NO_TRADE_STRATEGY, type ConfigInfo, type Plan, type PlanStatus } from "@/lib/api"

const STATUS_TEXT: Record<PlanStatus, string> = {
  pending: "Awaiting approval",
  approved: "Approved",
  rejected: "Rejected",
  expired: "Expired",
  unknown: "Status not reported"
}

export interface MandateCardProps {
  plan: Plan
  config: ConfigInfo | null
  deciding: boolean
  error: string
  onDecide: (approved: boolean, note: string) => void
}

export function MandateCard({ plan, config, deciding, error, onDecide }: MandateCardProps) {
  const [note, setNote] = useState("")
  const consequences = plan.consequences
  const willTrade = plan.tradeToday && plan.strategyId !== NO_TRADE_STRATEGY
  const decided = plan.status === "approved" || plan.status === "rejected"

  const start = formatSessionTime(plan.startTime || config?.startTime)
  const end = formatSessionTime(plan.endTime || config?.endTime)
  const squareoff = formatSessionTime(plan.squareoffTime || config?.squareoffTime)

  return (
    <Card>
      <CardHeader className="flex-row flex-wrap items-center justify-between gap-3">
        <div className="flex flex-col gap-1">
          <CardTitle>Mandate for {plan.tradingDate ? formatDate(plan.tradingDate) : "today"}</CardTitle>
          <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            <span>The human approves this once. It is not asked again per order.</span>
            {plan.mandateVersion ? (
              <span className="font-mono">version {plan.mandateVersion}</span>
            ) : null}
          </div>
        </div>
        <Badge
          variant={
            plan.status === "approved"
              ? "success"
              : plan.status === "rejected"
                ? "danger"
                : plan.status === "pending"
                  ? "warn"
                  : "outline"
          }
        >
          {STATUS_TEXT[plan.status]}
        </Badge>
      </CardHeader>

      <CardContent className="flex flex-col gap-6">
        {willTrade ? (
          <>
            <div className="rounded-lg border border-success-border bg-success-soft px-5 py-4">
              <div className="flex flex-wrap items-baseline justify-between gap-3">
                <div>
                  <div className="text-[11px] font-medium tracking-wide text-muted-foreground uppercase">
                    Strategy locked for the session
                  </div>
                  <div className="mt-1 font-mono text-lg font-semibold">{plan.strategyId}</div>
                </div>
                <div className="text-right">
                  <div className="text-[11px] font-medium tracking-wide text-muted-foreground uppercase">
                    Risk per trade
                  </div>
                  <div className="tnum mt-1 text-lg font-semibold">
                    {formatFractionAsPercent(consequences.riskFraction ?? plan.riskFraction)}
                  </div>
                  {consequences.riskFractionNote ? (
                    <div className="mt-0.5 text-xs text-muted-foreground">
                      {consequences.riskFractionNote}
                    </div>
                  ) : null}
                </div>
              </div>
            </div>

            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              <StatTile
                label="Worst case loss today"
                value={formatCurrency(consequences.worstCaseDailyLoss, 0)}
                detail={
                  consequences.dailyLossLimitPct !== null
                    ? `${formatPercent(consequences.dailyLossLimitPct, 1)} of allocation`
                    : "the daily budget, not a tripwire"
                }
                tone="text-danger"
              />
              <StatTile
                label="Max capital at risk"
                value={formatCurrency(consequences.maxCapitalAtRisk, 0)}
                detail={
                  consequences.maxCapitalAtRiskPct !== null
                    ? `${formatPercent(consequences.maxCapitalAtRiskPct, 3)} of allocation, all positions at once`
                    : "across all positions open at once"
                }
              />
              <StatTile
                label="Position cap"
                value={
                  consequences.positionCap === null ? EMPTY : formatNumber(consequences.positionCap, 0)
                }
                detail="concurrent positions"
              />
              <StatTile
                label="Risk per trade"
                value={formatCurrency(consequences.riskAmountPerTrade, 0)}
                detail={
                  consequences.allocation !== null
                    ? `on ${formatCurrency(consequences.allocation, 0)} allocated`
                    : undefined
                }
              />
            </div>
          </>
        ) : (
          <div className="rounded-lg border border-warn-border bg-warn-soft px-5 py-4">
            <div className="flex items-start gap-3">
              <CircleSlash className="mt-0.5 size-5 shrink-0 text-warn" />
              <div className="flex flex-col gap-2">
                <div className="text-lg font-semibold">Trade nothing today</div>
                <p className="text-sm">
                  No strategy cleared the viability gate, so the mandate opens no positions. This is the
                  selector working, not failing: choosing the least bad of three losing strategies is
                  choosing how to lose. Approving records the decision and the executor observes the
                  session without placing an order.
                </p>
                <p className="text-sm font-medium">
                  Nothing is committed. The executor places no order, so no capital is at risk and
                  the daily loss budget is untouched.
                </p>
                {consequences.worstCaseDailyLoss !== null ? (
                  <p className="tnum text-xs text-muted-foreground">
                    The mandate limits stay configured at{" "}
                    {formatCurrency(consequences.worstCaseDailyLoss, 0)} for the day and{" "}
                    {formatCurrency(consequences.riskAmountPerTrade, 0)} per trade. None of it will be
                    used today.
                  </p>
                ) : null}
              </div>
            </div>
          </div>
        )}

        {consequences.source === "none" ? (
          <p className="flex items-start gap-2 text-xs text-warn">
            <TriangleAlert className="mt-px size-3.5 shrink-0" />
            The backend published no computed consequences with this plan. Nothing on this card was
            calculated here to fill the gap.
          </p>
        ) : (
          <p className="text-xs text-muted-foreground">
            {consequences.source === "plan"
              ? "Every figure above was computed by the backend for this mandate. The plan's own prose is never the source of these numbers."
              : "The plan carried no computed consequences, so these figures were read from the running configuration on /api/config."}
          </p>
        )}

        <div className="flex flex-wrap gap-x-8 gap-y-2 text-sm">
          <div>
            <span className="text-muted-foreground">Entries open </span>
            <span className="tnum font-medium">{start}</span>
          </div>
          <div>
            <span className="text-muted-foreground">No new entries after </span>
            <span className="tnum font-medium">{end}</span>
          </div>
          <div>
            <span className="text-muted-foreground">Forced flat at </span>
            <span className="tnum font-medium">{squareoff}</span>
          </div>
        </div>

        <Separator />

        <PlanSummary plan={plan} />
      </CardContent>

      <CardFooter className="flex-col items-stretch gap-3">
        {error ? (
          <div className="flex items-start gap-2 rounded-md border border-danger-border bg-danger-soft px-3 py-2 text-sm text-danger">
            <TriangleAlert className="mt-0.5 size-4 shrink-0" />
            <span>{error}</span>
          </div>
        ) : null}

        {decided ? (
          <div className="flex flex-wrap items-center gap-2 text-sm">
            {plan.status === "approved" ? (
              <CircleCheckBig className="size-4 text-success" />
            ) : (
              <CircleSlash className="size-4 text-danger" />
            )}
            <span className="font-medium">
              {plan.status === "approved" ? "Mandate approved" : "Mandate rejected"}
            </span>
            {plan.approvedAt ? (
              <span className="text-muted-foreground">at {formatDateTime(plan.approvedAt)}</span>
            ) : null}
            {plan.note ? <span className="text-muted-foreground">- {plan.note}</span> : null}
          </div>
        ) : (
          <>
            <input
              value={note}
              onChange={(event) => setNote(event.target.value)}
              placeholder="Optional note, recorded with the decision"
              className="h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm outline-none placeholder:text-muted-foreground focus-visible:ring-2 focus-visible:ring-ring"
            />
            <div className="flex flex-wrap gap-3">
              <Button
                variant={willTrade ? "success" : "default"}
                size="lg"
                disabled={deciding}
                onClick={() => onDecide(true, note)}
              >
                <CircleCheckBig />
                {willTrade ? "Approve mandate" : "Approve, trade nothing today"}
              </Button>
              <Button variant="outline" size="lg" disabled={deciding} onClick={() => onDecide(false, note)}>
                <CircleSlash />
                Reject
              </Button>
              {deciding ? <span className="shimmer self-center text-sm">Recording the decision</span> : null}
            </div>
          </>
        )}
      </CardFooter>
    </Card>
  )
}

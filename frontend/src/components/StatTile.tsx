/** A labelled number. The unit of the live board and the mandate card.
 *
 * Deliberately not a chart. Four of these across the top of the board answer "how
 * much have I lost, how much room is left, how many trades, how close to a breaker"
 * faster than any plot would, and PLAN.md Part 8 asks for exactly those four.
 */

import type { ReactNode } from "react"

import { cn } from "@/lib/utils"
import { EMPTY } from "@/lib/format"

export interface StatTileProps {
  label: string
  value: ReactNode
  /** Secondary line: the denominator, the limit, or where the number came from. */
  detail?: ReactNode
  /** Token class for the value, e.g. text-danger. Neutral ink by default. */
  tone?: string
  className?: string
}

export function StatTile({ label, value, detail, tone, className }: StatTileProps) {
  return (
    <div className={cn("rounded-lg border border-border bg-panel px-4 py-3", className)}>
      <div className="text-[11px] font-medium tracking-wide text-muted-foreground uppercase">{label}</div>
      <div className={cn("tnum mt-1.5 text-xl font-semibold", tone)}>{value ?? EMPTY}</div>
      {detail ? <div className="tnum mt-0.5 text-xs text-muted-foreground">{detail}</div> : null}
    </div>
  )
}

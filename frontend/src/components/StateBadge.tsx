/** One place decides what colour a state is.
 *
 * The mapping is the point. Two of these are not cosmetic:
 *
 *   OPEN_UNPROTECTED is danger, always. It means the entry filled and the stop did
 *   not, so the position is naked. PLAN.md Part 6 gives it a five second dwell time
 *   before the executor exits at market; a human glancing at the board has to see it
 *   in that window without reading the word.
 *
 *   UNKNOWN is danger too, not muted. It means the order's fate is genuinely not
 *   known and the rule is reconcile, never blind retry. Rendering it as a neutral
 *   "we are still finding out" would understate it.
 */

import { Badge, type BadgeVariant } from "@/components/ui/badge"
import { cn } from "@/lib/utils"
import type { RunState } from "@/lib/api"

const MACHINE_VARIANTS: Record<string, BadgeVariant> = {
  FLAT: "muted",
  SIGNAL: "info",
  PENDING_ENTRY: "warn",
  PARTIAL: "warn",
  PENDING_EXIT: "warn",
  REJECTED: "warn",
  FILLED: "success",
  OPEN: "success",
  OPEN_UNPROTECTED: "danger",
  UNKNOWN: "danger",
  HALTED: "danger",
  REDUCE_ONLY: "warn"
}

const RUN_VARIANTS: Record<RunState, BadgeVariant> = {
  running: "success",
  paused: "warn",
  reduce_only: "warn",
  halted: "danger",
  idle: "muted",
  unknown: "outline"
}

const RUN_LABELS: Record<RunState, string> = {
  running: "Running",
  paused: "Paused",
  reduce_only: "Reduce only",
  halted: "Halted",
  idle: "Idle",
  unknown: "Unreported"
}

export function machineVariant(state: string): BadgeVariant {
  return MACHINE_VARIANTS[state.trim().toUpperCase()] ?? "outline"
}

export function runStateVariant(state: RunState): BadgeVariant {
  return RUN_VARIANTS[state]
}

export function runStateLabel(state: RunState): string {
  return RUN_LABELS[state]
}

/** A per-symbol machine state. The raw state string is shown, not a friendly
 *  rewrite: PENDING_ENTRY is the name in the intent log and in the code, and a
 *  human comparing the two should see the same word. */
export function StateBadge({ state, className }: { state: string; className?: string }) {
  const upper = state.trim().toUpperCase() || "UNKNOWN"
  return (
    <Badge variant={machineVariant(upper)} className={cn("font-mono text-[11px]", className)}>
      {upper}
    </Badge>
  )
}

export function RunStateBadge({ state, className }: { state: RunState; className?: string }) {
  return (
    <Badge variant={runStateVariant(state)} className={className}>
      {state === "running" ? <span className="pulse-soft size-1.5 rounded-full bg-current" /> : null}
      {runStateLabel(state)}
    </Badge>
  )
}

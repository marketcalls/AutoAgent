/** The emergency stop, and the softer control next to it.
 *
 * Design rules, each with a reason:
 *
 *   ALWAYS VISIBLE, NEVER MOVES. It is rendered whenever a session exists, disabled
 *   rather than hidden when there is nothing to halt, so the target is in the same
 *   place at 09:31 and at 14:44. A control that appears only in an emergency is one
 *   the operator has never used before.
 *
 *   ONE CONFIRMATION, NOT TWO. Halting is destructive - it cancels resting orders and
 *   flattens at market, which costs slippage - but hesitating is worse. A single
 *   confirm, and the armed state disarms itself after ten seconds so a forgotten
 *   click cannot be completed by an accidental one much later.
 *
 *   REDUCE-ONLY SITS BESIDE IT, SMALLER. It is the state between running and stopped:
 *   positions may close, nothing new may open. Most "should I stop this" moments want
 *   this one, and having it here means the halt button is not pressed for a situation
 *   that did not need it.
 */

import { useEffect, useState } from "react"
import { OctagonX, ShieldMinus, TriangleAlert } from "lucide-react"

import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"

/** How long the armed state survives without a decision. */
const DISARM_AFTER_MS = 10_000

export interface HaltButtonProps {
  /** True when there is a live session to stop. */
  live: boolean
  halting: boolean
  reducing: boolean
  error: string
  onHalt: () => void
  onReduceOnly: () => void
}

export function HaltButton({ live, halting, reducing, error, onHalt, onReduceOnly }: HaltButtonProps) {
  const [armed, setArmed] = useState(false)

  useEffect(() => {
    if (!armed) return
    const timer = setTimeout(() => setArmed(false), DISARM_AFTER_MS)
    return () => clearTimeout(timer)
  }, [armed])

  // A session that ends while the control is armed must not leave it armed.
  useEffect(() => {
    if (!live) setArmed(false)
  }, [live])

  return (
    <div
      className={cn(
        "flex flex-col gap-3 rounded-lg border px-4 py-3",
        armed ? "border-danger-border bg-danger-soft" : "border-border bg-panel"
      )}
    >
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0">
          <div className="text-sm font-semibold">
            {armed ? "Confirm the halt" : "Emergency stop"}
          </div>
          <div className="mt-0.5 text-xs text-muted-foreground">
            {armed
              ? "Cancels every resting order, flattens at market, then halts for the session. The halt is sticky across a restart."
              : live
                ? "Stops the session immediately. Flattening at market costs slippage."
                : "No live session to stop."}
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          {armed ? (
            <>
              <Button variant="halt" size="xl" disabled={halting} onClick={onHalt}>
                <OctagonX />
                {halting ? "Halting" : "Halt now"}
              </Button>
              <Button variant="outline" size="lg" disabled={halting} onClick={() => setArmed(false)}>
                Cancel
              </Button>
            </>
          ) : (
            <>
              <Button
                variant="outline"
                size="lg"
                disabled={!live || reducing || halting}
                onClick={onReduceOnly}
                title="Close positions but open nothing new"
              >
                <ShieldMinus />
                {reducing ? "Setting reduce-only" : "Reduce only"}
              </Button>
              <Button variant="halt" size="xl" disabled={!live || halting} onClick={() => setArmed(true)}>
                <OctagonX />
                Halt session
              </Button>
            </>
          )}
        </div>
      </div>

      {error ? (
        <div className="flex items-start gap-2 rounded-md border border-danger-border bg-danger-soft px-3 py-2 text-sm text-danger">
          <TriangleAlert className="mt-0.5 size-4 shrink-0" />
          <span>{error}</span>
        </div>
      ) : null}
    </div>
  )
}

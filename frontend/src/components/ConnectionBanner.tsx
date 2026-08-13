/** What the screen says when it cannot see the backend.
 *
 * This is a control surface for an unattended trading agent, so the failure mode
 * that matters is not "the page broke" - it is "the page looks fine and the numbers
 * on it are twenty minutes old." Two distinct conditions are therefore reported
 * separately and neither is silent:
 *
 *   offline          no route answered at all. Nothing on screen can be trusted, and
 *                    the agent may well still be trading.
 *   stream dropped   the routes answer but the live feed is gone, so the board is a
 *                    snapshot, not a feed.
 */

import { PlugZap, RefreshCw, WifiOff } from "lucide-react"

import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"

export interface ConnectionBannerProps {
  offline: boolean
  streamConnected: boolean
  streamError: string
  message: string
  refreshing: boolean
  onRetry: () => void
}

export function ConnectionBanner({
  offline,
  streamConnected,
  streamError,
  message,
  refreshing,
  onRetry
}: ConnectionBannerProps) {
  if (!offline && streamConnected) return null

  const tone = offline
    ? "border-danger-border bg-danger-soft text-danger"
    : "border-warn-border bg-warn-soft text-warn"

  return (
    <div className={cn("flex flex-wrap items-center gap-3 rounded-lg border px-4 py-3 text-sm", tone)}>
      {offline ? <WifiOff className="size-4 shrink-0" /> : <PlugZap className="size-4 shrink-0" />}
      <div className="min-w-0 flex-1">
        <div className="font-medium">
          {offline
            ? "The backend is not answering on port 8090."
            : "The live feed dropped. The board below is the last state received."}
        </div>
        <div className="mt-0.5 text-xs opacity-80">
          {offline
            ? message || "Start the backend, or check that it is bound to 127.0.0.1:8090."
            : streamError || "Reconnecting."}
          {offline
            ? ". Anything already running keeps running: this page is an observer, and losing it does not stop the agent."
            : null}
        </div>
      </div>
      <Button variant="outline" size="sm" onClick={onRetry} disabled={refreshing}>
        <RefreshCw className={cn(refreshing && "pulse-soft")} />
        {refreshing ? "Retrying" : "Retry"}
      </Button>
    </div>
  )
}

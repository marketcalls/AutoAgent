/** The whole control surface. One page, no router, no state library.
 *
 * The shape of this file follows PLAN.md Part 8: this is an observation and control
 * surface, not a chat app, and it has exactly four jobs - approve the mandate before
 * the session, watch the session, stop the session, and read the session back
 * afterwards. Everything here is component state and one container, the same way
 * TradingAgent runs its entire UI, because this app has less UI than that one.
 *
 * Two behaviours are load-bearing rather than convenient:
 *
 *   THE PAGE DEGRADES, IT DOES NOT THROW. If the backend is not running, every fetch
 *   fails with status 0 and the screen says so. It does not blank, and it does not
 *   imply the agent stopped: this page is an observer, and an unattended executor
 *   keeps running whether or not anybody has a browser open.
 *
 *   THE STREAM IS THE LIVE SOURCE; POLLING IS THE FALLBACK. Session state arrives on
 *   /api/stream. A slow poll refreshes the rest, and speeds up to cover the session
 *   itself only while the stream is down - a board that silently shows twenty minute
 *   old positions is the failure this is guarding against.
 */

import { useCallback, useEffect, useRef, useState } from "react"
import { CircleAlert, Info, TriangleAlert } from "lucide-react"

import { ConnectionBanner } from "@/components/ConnectionBanner"
import { EquityCurve } from "@/components/EquityCurve"
import { HaltButton } from "@/components/HaltButton"
import { LiveBoard } from "@/components/LiveBoard"
import { MandateCard } from "@/components/MandateCard"
import { ThemeToggle } from "@/components/ThemeToggle"
import { TradeTable } from "@/components/TradeTable"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { useEventStream, type Notice } from "@/lib/sse"
import { cn } from "@/lib/utils"
import { formatDate } from "@/lib/format"
import {
  ApiError,
  decidePlan,
  describeError,
  getConfig,
  getHealth,
  getPlan,
  getSession,
  getTrades,
  isSessionLive,
  postHalt,
  postReduceOnly,
  type ConfigInfo,
  type EquityPoint,
  type HealthInfo,
  type Plan,
  type SessionState,
  type TradeRow
} from "@/lib/api"

/** Background refresh cadence. The fast one is only used while the stream is down. */
const POLL_IDLE_MS = 30_000
const POLL_DEGRADED_MS = 8_000

const MAX_NOTICES = 6

function isOffline(error: unknown): boolean {
  return error instanceof ApiError && error.offline
}

export default function App() {
  const [ready, setReady] = useState(false)
  const [offline, setOffline] = useState(false)
  const [offlineMessage, setOfflineMessage] = useState("")
  const [refreshing, setRefreshing] = useState(false)

  const [health, setHealth] = useState<HealthInfo | null>(null)
  const [config, setConfig] = useState<ConfigInfo | null>(null)
  const [plan, setPlan] = useState<Plan | null>(null)
  const [session, setSession] = useState<SessionState | null>(null)
  const [trades, setTrades] = useState<TradeRow[]>([])
  const [equity, setEquity] = useState<EquityPoint[]>([])
  const [equityFromServer, setEquityFromServer] = useState(false)

  const [notices, setNotices] = useState<Notice[]>([])
  const [deciding, setDeciding] = useState(false)
  const [decideError, setDecideError] = useState("")
  const [halting, setHalting] = useState(false)
  const [reducing, setReducing] = useState(false)
  const [controlError, setControlError] = useState("")

  // The stream hands back session frames; the poll needs the newest config to
  // normalize a plan. A ref keeps both out of the effect dependency lists, so a
  // state frame arriving every second does not tear down the poll or the stream.
  const configRef = useRef<ConfigInfo | null>(null)
  configRef.current = config

  const pushNotice = useCallback((notice: Notice) => {
    setNotices((previous) => [notice, ...previous].slice(0, MAX_NOTICES))
  }, [])

  const stream = useEventStream({
    onSession: setSession,
    onNotice: pushNotice
  })

  /** One pass over the read-only routes. Never throws: each route degrades alone. */
  const refresh = useCallback(async (options: { includeSession: boolean }) => {
    setRefreshing(true)
    let reachable = false
    let lastError = ""

    const [healthResult, configResult] = await Promise.allSettled([getHealth(), getConfig()])
    if (healthResult.status === "fulfilled") {
      setHealth(healthResult.value)
      reachable = true
    } else if (isOffline(healthResult.reason)) {
      lastError = describeError(healthResult.reason)
    } else {
      reachable = true
    }
    if (configResult.status === "fulfilled") {
      setConfig(configResult.value)
      configRef.current = configResult.value
      reachable = true
    }

    const jobs: Promise<unknown>[] = [
      getPlan(configRef.current)
        .then((value) => setPlan(value))
        .catch((error) => {
          if (!isOffline(error)) reachable = true
        }),
      getTrades()
        .then((payload) => {
          setTrades(payload.trades)
          setEquity(payload.equity)
          setEquityFromServer(payload.equityFromServer)
          reachable = true
        })
        .catch((error) => {
          if (!isOffline(error)) reachable = true
        })
    ]
    if (options.includeSession) {
      jobs.push(
        getSession()
          .then((value) => {
            // A null session means nothing is running. Keeping the previous one on
            // screen would be a lie, so it is cleared.
            setSession(value)
            reachable = true
          })
          .catch((error) => {
            if (!isOffline(error)) reachable = true
          })
      )
    }
    await Promise.allSettled(jobs)

    setOffline(!reachable)
    setOfflineMessage(reachable ? "" : lastError)
    setRefreshing(false)
    setReady(true)
  }, [])

  useEffect(() => {
    void refresh({ includeSession: true })
  }, [refresh])

  useEffect(() => {
    const period = stream.connected ? POLL_IDLE_MS : POLL_DEGRADED_MS
    const timer = setInterval(() => {
      void refresh({ includeSession: !stream.connected })
    }, period)
    return () => clearInterval(timer)
  }, [refresh, stream.connected])

  const onDecide = useCallback(
    async (approved: boolean, note: string) => {
      setDeciding(true)
      setDecideError("")
      try {
        await decidePlan(approved, { note })
        await refresh({ includeSession: true })
      } catch (error) {
        setDecideError(
          `${approved ? "Approving" : "Rejecting"} the mandate failed: ${describeError(error)}. The mandate is unchanged.`
        )
      } finally {
        setDeciding(false)
      }
    },
    [refresh]
  )

  const onHalt = useCallback(async () => {
    setHalting(true)
    setControlError("")
    try {
      const result = await postHalt("halted from the control surface")
      pushNotice({
        level: "warning",
        message: result.message || "Halt accepted. Orders cancelled, positions flattened.",
        at: Date.now()
      })
      await refresh({ includeSession: true })
    } catch (error) {
      // The only failure that matters on this page: the operator must not be left
      // believing they stopped something that is still running.
      setControlError(
        `The halt did NOT land: ${describeError(error)}. The session may still be trading - use the kill switch file, or stop the backend.`
      )
    } finally {
      setHalting(false)
    }
  }, [pushNotice, refresh])

  const onReduceOnly = useCallback(async () => {
    setReducing(true)
    setControlError("")
    try {
      const result = await postReduceOnly("reduce-only from the control surface")
      pushNotice({
        level: "warning",
        message: result.message || "Reduce-only accepted. Positions may close, nothing new opens.",
        at: Date.now()
      })
      await refresh({ includeSession: true })
    } catch (error) {
      setControlError(`Reduce-only did not land: ${describeError(error)}.`)
    } finally {
      setReducing(false)
    }
  }, [pushNotice, refresh])

  const live = isSessionLive(session)
  const hasSession = session !== null
  const showReview = trades.length > 0 || (hasSession && !live)

  // /api/health reports the credential gaps; /api/config reports what validate()
  // found wrong with the settings and whether the kill switch file exists. Both are
  // reasons a session will not start, so they are shown together.
  const killSwitch = (health?.killSwitch ?? false) || (config?.killSwitch ?? false)
  const missingKeys = health?.missingKeys.length ? health.missingKeys : (config?.missingKeys ?? [])
  const configErrors = config?.errors.length ? config.errors : (health?.errors ?? [])

  if (!ready) {
    return (
      <div className="flex h-full items-center justify-center">
        <span className="shimmer text-sm">Connecting to the backend on port 8090</span>
      </div>
    )
  }

  return (
    <div className="min-h-full">
      <header className="sticky top-0 z-20 border-b border-border bg-background/85 backdrop-blur">
        <div className="mx-auto flex max-w-(--container-board) flex-wrap items-center gap-3 px-6 py-3">
          <div className="flex min-w-0 flex-col">
            <span className="text-sm font-semibold">AutoAgent</span>
            <span className="text-xs text-muted-foreground">
              Autonomous intraday equity. One mandate, approved before the open.
            </span>
          </div>
          <div className="ml-auto flex flex-wrap items-center gap-2">
            {health?.mode === "live" ? (
              <Badge variant="danger">live orders</Badge>
            ) : health?.mode === "analyze" ? (
              <Badge variant="info">analyzer mode</Badge>
            ) : (
              <Badge variant="outline">mode unknown</Badge>
            )}
            {killSwitch ? <Badge variant="danger">kill switch engaged</Badge> : null}
            {health && !health.tradingEnabled ? <Badge variant="muted">trading disabled</Badge> : null}
            {health?.version ? (
              <span className="tnum text-xs text-muted-foreground">v{health.version}</span>
            ) : null}
            <ThemeToggle />
          </div>
        </div>
      </header>

      <main className="mx-auto flex max-w-(--container-board) flex-col gap-6 px-6 py-6">
        <ConnectionBanner
          offline={offline}
          streamConnected={stream.connected}
          streamError={stream.error}
          message={offlineMessage}
          refreshing={refreshing}
          onRetry={() => void refresh({ includeSession: true })}
        />

        {missingKeys.length > 0 || configErrors.length > 0 ? (
          <div className="flex flex-col gap-1 rounded-lg border border-warn-border bg-warn-soft px-4 py-3 text-sm text-warn">
            {missingKeys.length ? (
              <div className="flex items-start gap-2">
                <TriangleAlert className="mt-0.5 size-4 shrink-0" />
                <span>Missing credentials: {missingKeys.join(", ")}.</span>
              </div>
            ) : null}
            {configErrors.map((problem) => (
              <div key={problem} className="flex items-start gap-2">
                <CircleAlert className="mt-0.5 size-4 shrink-0" />
                <span>{problem}</span>
              </div>
            ))}
          </div>
        ) : null}

        {hasSession ? (
          <HaltButton
            live={live}
            halting={halting}
            reducing={reducing}
            error={controlError}
            onHalt={() => void onHalt()}
            onReduceOnly={() => void onReduceOnly()}
          />
        ) : null}

        {notices.length ? (
          <div className="flex flex-col gap-2">
            {notices.map((notice) => (
              <div
                key={`${notice.at}:${notice.message}`}
                className={cn(
                  "flex items-start gap-2 rounded-md border px-3 py-2 text-sm",
                  notice.level === "error"
                    ? "border-danger-border bg-danger-soft text-danger"
                    : notice.level === "warning"
                      ? "border-warn-border bg-warn-soft text-warn"
                      : "border-info-border bg-info-soft text-info"
                )}
              >
                {notice.level === "info" ? (
                  <Info className="mt-0.5 size-4 shrink-0" />
                ) : (
                  <TriangleAlert className="mt-0.5 size-4 shrink-0" />
                )}
                <span>{notice.message}</span>
              </div>
            ))}
          </div>
        ) : null}

        {live && session ? (
          <LiveBoard session={session} config={config} streamConnected={stream.connected} />
        ) : null}

        {plan ? (
          <MandateCard
            plan={plan}
            config={config}
            deciding={deciding}
            error={decideError}
            onDecide={(approved, note) => void onDecide(approved, note)}
          />
        ) : (
          <Card>
            <CardHeader>
              <CardTitle>No mandate published</CardTitle>
            </CardHeader>
            <CardContent className="text-sm text-muted-foreground">
              {offline
                ? "The backend is not answering, so no plan could be read."
                : "The Planner publishes the day's plan at 08:45. Nothing to approve until then."}
            </CardContent>
          </Card>
        )}

        {!live && session ? (
          <LiveBoard session={session} config={config} streamConnected={stream.connected} />
        ) : null}

        {showReview ? (
          <>
            <EquityCurve points={equity} fromServer={equityFromServer} />
            <TradeTable trades={trades} />
          </>
        ) : null}

        <footer className="pb-8 text-xs text-muted-foreground">
          {session?.tradingDate ? <span>Session {formatDate(session.tradingDate)}. </span> : null}
          This page observes and controls. It does not place orders, and closing it does not stop
          the agent.
        </footer>
      </main>
    </div>
  )
}

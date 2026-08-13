/** The streaming client for /api/stream, and the React binding around it.
 *
 * Facts the transport depends on, carried unchanged from TradingAgent because the
 * backend speaks the same dialect:
 *   - The backend writes NO "event:" lines. The discriminator is a "type" field
 *     inside each JSON payload. EventSource would parse the frames but would report
 *     every one as a generic "message", so this uses fetch plus getReader instead.
 *   - Frames are "data: {...}\n\n" and a chunk boundary can land mid-line, so the
 *     trailing partial line is carried across reads.
 *   - Once the response headers are out the status is already 200, so a mid-stream
 *     failure arrives as an "error" frame, or as a silent close, rather than as an
 *     HTTP error.
 *
 * And one fact specific to this app: the stream is the ONLY thing keeping the live
 * board honest during a session, so a dropped connection is a first-class visible
 * state, not something to retry quietly. The hook exposes `connected` for exactly
 * that - a board showing stale positions while claiming to be live is worse than a
 * board that says it lost the feed.
 */

import { useEffect, useRef, useState } from "react"
import { describeError, normalizeSession, type SessionState } from "./api"

export interface StateEvent {
  type: "state"
  [key: string]: unknown
}

export interface NoticeEvent {
  type: "notice"
  level?: "info" | "warning" | "error" | string
  message?: string
  [key: string]: unknown
}

export interface ErrorEvent {
  type: "error"
  message?: string
  kind?: string | null
  [key: string]: unknown
}

export interface PingEvent {
  type: "ping"
  [key: string]: unknown
}

export type StreamEvent = StateEvent | NoticeEvent | ErrorEvent | PingEvent

export interface Notice {
  level: string
  message: string
  at: number
}

function dispatch(line: string, onEvent: (event: StreamEvent) => void): void {
  const trimmed = line.trimEnd()
  if (!trimmed.startsWith("data:")) return
  const raw = trimmed.slice(5).trim()
  if (!raw) return
  let parsed: unknown
  try {
    parsed = JSON.parse(raw)
  } catch {
    // A frame that does not parse is dropped. The stream is still usable and the
    // session is still running, so failing the whole read would lose the feed over
    // one bad frame.
    return
  }
  if (!parsed || typeof parsed !== "object") return
  const candidate = parsed as { type?: unknown }
  if (typeof candidate.type !== "string") return
  onEvent(parsed as StreamEvent)
}

/** Reads one connection to completion. Returns when the server closes the stream. */
export async function consumeStream(
  signal: AbortSignal,
  onEvent: (event: StreamEvent) => void
): Promise<void> {
  const response = await fetch("/api/stream", {
    signal,
    headers: { Accept: "text/event-stream" }
  })
  if (!response.ok) {
    onEvent({
      type: "error",
      message: `the stream refused the connection with status ${response.status}`,
      kind: `http_${response.status}`
    })
    return
  }
  const body = response.body
  if (!body) {
    onEvent({ type: "error", message: "the stream arrived with no readable body", kind: "no_body" })
    return
  }
  const reader = body.getReader()
  const decoder = new TextDecoder()
  let buffer = ""
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const lines = buffer.split("\n")
    buffer = lines.pop() ?? ""
    for (const line of lines) dispatch(line, onEvent)
  }
  buffer += decoder.decode()
  // A stream cut short can leave one complete frame without its trailing newline.
  dispatch(buffer, onEvent)
}

export function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError"
}

/** Backoff between reconnects, in milliseconds. Capped low: this is a localhost
 *  connection and the operator is waiting on it, so a long backoff would leave the
 *  board dark for no reason. */
const BACKOFF_MS = [1000, 2000, 4000, 8000, 15000]

export interface StreamHandlers {
  onSession: (session: SessionState) => void
  onNotice: (notice: Notice) => void
}

export interface StreamStatus {
  connected: boolean
  /** Populated while disconnected; cleared on a successful connection. */
  error: string
  attempts: number
  lastEventAt: number | null
}

/** Keeps one stream open for the life of the component, reconnecting with backoff.
 *
 * Handlers are held in a ref so that a parent re-render (which happens on every
 * state frame) does not tear the connection down and open a new one.
 */
export function useEventStream(handlers: StreamHandlers): StreamStatus {
  const [status, setStatus] = useState<StreamStatus>({
    connected: false,
    error: "",
    attempts: 0,
    lastEventAt: null
  })
  const handlersRef = useRef(handlers)
  handlersRef.current = handlers

  useEffect(() => {
    const controller = new AbortController()
    let stopped = false
    let attempt = 0

    const wait = (ms: number) =>
      new Promise<void>((resolve) => {
        const timer = setTimeout(resolve, ms)
        controller.signal.addEventListener("abort", () => {
          clearTimeout(timer)
          resolve()
        })
      })

    const run = async () => {
      while (!stopped) {
        try {
          await consumeStream(controller.signal, (event) => {
            if (stopped) return
            attempt = 0
            setStatus((prev) =>
              prev.connected && !prev.error
                ? { ...prev, lastEventAt: Date.now() }
                : { connected: true, error: "", attempts: 0, lastEventAt: Date.now() }
            )
            if (event.type === "state") {
              // The state may be the frame itself or nested under a key. normalizeSession
              // searches both, so the frame is handed over whole.
              const session = normalizeSession(event)
              if (session) handlersRef.current.onSession(session)
              return
            }
            if (event.type === "notice") {
              const notice = event as NoticeEvent
              handlersRef.current.onNotice({
                level: typeof notice.level === "string" ? notice.level : "info",
                message: typeof notice.message === "string" ? notice.message : "",
                at: Date.now()
              })
              return
            }
            if (event.type === "error") {
              const failure = event as ErrorEvent
              handlersRef.current.onNotice({
                level: "error",
                message:
                  typeof failure.message === "string" && failure.message
                    ? failure.message
                    : "the backend reported an error on the stream",
                at: Date.now()
              })
              return
            }
            // "ping" only proves the connection is alive, which the status above
            // has already recorded.
          })
          if (stopped) return
          // A clean close is still a lost feed, so it backs off like a failure.
          setStatus((prev) => ({
            ...prev,
            connected: false,
            error: "the backend closed the stream",
            attempts: attempt + 1
          }))
        } catch (error) {
          if (stopped || isAbortError(error)) return
          setStatus((prev) => ({
            ...prev,
            connected: false,
            error: describeError(error),
            attempts: attempt + 1
          }))
        }
        const delay = BACKOFF_MS[Math.min(attempt, BACKOFF_MS.length - 1)]
        attempt += 1
        await wait(delay)
      }
    }

    void run()
    return () => {
      stopped = true
      controller.abort()
    }
  }, [])

  return status
}

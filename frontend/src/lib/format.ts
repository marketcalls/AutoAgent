/** Number, money and time rendering for Indian markets.
 *
 * Ported from TradingAgent, where these rules were learned from the payloads they
 * have to display. They are unchanged because the backend is the same shape:
 *
 *   - Indian grouping is 2-2-3 (12,34,567.89), never 3-3-3, so every number goes
 *     through Intl.NumberFormat("en-IN") rather than the browser's own locale.
 *   - Large money reads as lakh and crore, not million and billion. A 10,00,000
 *     allocation is "10.00 L" to an Indian reader and "1.00 M" to nobody here.
 *   - OpenAlgo returns numbers as strings often enough ("1,234.50", "", "-") that
 *     every entry point coerces defensively and falls back to a dash.
 *   - Exchange times are IST wherever the browser sits, so every time render pins
 *     timeZone to Asia/Kolkata. A naive "2026-08-13 15:04:05" carries no offset and
 *     is therefore assumed to already be IST.
 *
 * One addition for this app: R multiples. Position size is derived from a fixed
 * rupee risk, so a trade's result in R is the comparable number across symbols and
 * the one every risk rule is written in.
 */

const LOCALE = "en-IN"
const TIMEZONE = "Asia/Kolkata"

/** Unicode rupee sign. A currency symbol, not an icon. */
export const RUPEE = "₹"

/** What every formatter returns when a value cannot be read as a number or date. */
export const EMPTY = "-"

export function toNumber(value: unknown): number | null {
  if (typeof value === "number") return Number.isFinite(value) ? value : null
  if (typeof value === "string") {
    const cleaned = value.replace(/[,\s]/g, "").replace(RUPEE, "")
    if (!cleaned) return null
    const parsed = Number(cleaned)
    return Number.isFinite(parsed) ? parsed : null
  }
  return null
}

const formatters = new Map<string, Intl.NumberFormat>()

function grouped(minimum: number, maximum: number): Intl.NumberFormat {
  const key = `${minimum}:${maximum}`
  let formatter = formatters.get(key)
  if (!formatter) {
    formatter = new Intl.NumberFormat(LOCALE, {
      minimumFractionDigits: minimum,
      maximumFractionDigits: maximum
    })
    formatters.set(key, formatter)
  }
  return formatter
}

export function formatNumber(value: unknown, decimals = 2): string {
  const parsed = toNumber(value)
  if (parsed === null) return EMPTY
  return grouped(decimals, decimals).format(parsed)
}

/** Quantities are whole shares most of the time; fractional ones still need to show. */
export function formatQuantity(value: unknown): string {
  const parsed = toNumber(value)
  if (parsed === null) return EMPTY
  return grouped(0, Number.isInteger(parsed) ? 0 : 4).format(parsed)
}

export function formatPrice(value: unknown, decimals = 2): string {
  return formatNumber(value, decimals)
}

export function formatCurrency(value: unknown, decimals = 2): string {
  const parsed = toNumber(value)
  if (parsed === null) return EMPTY
  const sign = parsed < 0 ? "-" : ""
  return `${sign}${RUPEE}${grouped(decimals, decimals).format(Math.abs(parsed))}`
}

/** Always carries an explicit + or -, because a P&L figure with no sign is ambiguous. */
export function formatSignedCurrency(value: unknown, decimals = 2): string {
  const parsed = toNumber(value)
  if (parsed === null) return EMPTY
  const sign = parsed > 0 ? "+" : parsed < 0 ? "-" : ""
  return `${sign}${RUPEE}${grouped(decimals, decimals).format(Math.abs(parsed))}`
}

export function formatSigned(value: unknown, decimals = 2): string {
  const parsed = toNumber(value)
  if (parsed === null) return EMPTY
  const sign = parsed > 0 ? "+" : parsed < 0 ? "-" : ""
  return `${sign}${grouped(decimals, decimals).format(Math.abs(parsed))}`
}

/** A plain percentage: 47.5%. Use formatSignedPercent for anything that can fall. */
export function formatPercent(value: unknown, decimals = 1): string {
  const parsed = toNumber(value)
  if (parsed === null) return EMPTY
  return `${grouped(decimals, decimals).format(parsed)}%`
}

export function formatSignedPercent(value: unknown, decimals = 2): string {
  const parsed = toNumber(value)
  if (parsed === null) return EMPTY
  return `${formatSigned(parsed, decimals)}%`
}

/** A fraction stored as 0.005 shown as the 0.50% every risk rule is written in. */
export function formatFractionAsPercent(value: unknown, decimals = 3): string {
  const parsed = toNumber(value)
  if (parsed === null) return EMPTY
  return `${grouped(decimals, decimals).format(parsed * 100)}%`
}

/** Trade results in R. Sizing fixes the rupee risk, so R is the comparable unit. */
export function formatR(value: unknown, decimals = 2): string {
  const parsed = toNumber(value)
  if (parsed === null) return EMPTY
  return `${formatSigned(parsed, decimals)}R`
}

/** Lakh and crore grouping for headline figures: 1.25 Cr rather than 12,500,000. */
export function formatCompact(value: unknown): string {
  const parsed = toNumber(value)
  if (parsed === null) return EMPTY
  const sign = parsed < 0 ? "-" : ""
  const size = Math.abs(parsed)
  if (size >= 1e7) return `${sign}${grouped(2, 2).format(size / 1e7)} Cr`
  if (size >= 1e5) return `${sign}${grouped(2, 2).format(size / 1e5)} L`
  if (size >= 1e3) return `${sign}${grouped(2, 2).format(size / 1e3)} K`
  return `${sign}${grouped(0, 2).format(size)}`
}

export function formatCompactCurrency(value: unknown): string {
  const parsed = toNumber(value)
  if (parsed === null) return EMPTY
  const sign = parsed < 0 ? "-" : ""
  return `${sign}${RUPEE}${formatCompact(Math.abs(parsed))}`
}

/** Tailwind text colour for a signed figure. Flat zero stays neutral, not green. */
export function pnlClass(value: unknown): string {
  const parsed = toNumber(value)
  if (parsed === null || parsed === 0) return "text-muted-foreground"
  return parsed > 0 ? "text-success" : "text-danger"
}

export function toDate(value: unknown): Date | null {
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value
  if (typeof value === "number") {
    // Epoch seconds and epoch milliseconds both turn up; anything under 1e11 is seconds.
    const millis = Math.abs(value) < 1e11 ? value * 1000 : value
    const parsed = new Date(millis)
    return Number.isNaN(parsed.getTime()) ? null : parsed
  }
  if (typeof value === "string") {
    const text = value.trim()
    if (!text) return null
    if (/^-?\d+(\.\d+)?$/.test(text)) return toDate(Number(text))
    // "2026-08-13 15:04:05" is not ISO 8601 and Safari refuses it outright.
    const parsed = new Date(text.includes("T") ? text : text.replace(" ", "T"))
    return Number.isNaN(parsed.getTime()) ? null : parsed
  }
  return null
}

const dateTimeFormat = new Intl.DateTimeFormat(LOCALE, {
  timeZone: TIMEZONE,
  day: "2-digit",
  month: "short",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false
})

const timeFormat = new Intl.DateTimeFormat(LOCALE, {
  timeZone: TIMEZONE,
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false
})

const clockFormat = new Intl.DateTimeFormat(LOCALE, {
  timeZone: TIMEZONE,
  hour: "2-digit",
  minute: "2-digit",
  hour12: false
})

const dateFormat = new Intl.DateTimeFormat(LOCALE, {
  timeZone: TIMEZONE,
  day: "2-digit",
  month: "short",
  year: "numeric"
})

export function formatDateTime(value: unknown): string {
  const parsed = toDate(value)
  return parsed ? dateTimeFormat.format(parsed) : EMPTY
}

export function formatTime(value: unknown): string {
  const parsed = toDate(value)
  return parsed ? timeFormat.format(parsed) : EMPTY
}

/** HH:MM. Bar closes land on the minute, so seconds are noise in a trade list. */
export function formatClock(value: unknown): string {
  const parsed = toDate(value)
  return parsed ? clockFormat.format(parsed) : EMPTY
}

export function formatDate(value: unknown): string {
  const parsed = toDate(value)
  return parsed ? dateFormat.format(parsed) : EMPTY
}

/** The session clock arrives as "09:30" or "09:30:00"; both render as 09:30. */
export function formatSessionTime(value: unknown): string {
  if (typeof value !== "string") return EMPTY
  const match = value.trim().match(/^(\d{1,2}):(\d{2})/)
  if (!match) return value.trim() || EMPTY
  return `${match[1].padStart(2, "0")}:${match[2]}`
}

/** Seconds since a stamp, as "4s ago" / "3m ago". Used for feed staleness only. */
export function formatAge(value: unknown): string {
  const parsed = toDate(value)
  if (!parsed) return EMPTY
  const seconds = Math.max(0, Math.round((Date.now() - parsed.getTime()) / 1000))
  if (seconds < 60) return `${seconds}s ago`
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`
  return `${Math.round(seconds / 3600)}h ago`
}

/** Turns snake_case machine states and codes into something readable, without
 *  losing the identity of the state - PENDING_ENTRY stays recognisably that. */
export function labelize(key: string): string {
  const spaced = key
    .trim()
    .replace(/[_-]+/g, " ")
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .replace(/\s+/g, " ")
    .trim()
  if (!spaced) return key
  return spaced.charAt(0).toUpperCase() + spaced.slice(1).toLowerCase()
}

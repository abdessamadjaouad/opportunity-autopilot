export const displayTimezone = 'Africa/Casablanca'

export function label(value: unknown, fallback = 'Unknown'): string {
  if (value === undefined || value === null || value === '') return fallback
  if (typeof value !== 'string') return valueText(value)
  return value.replaceAll('_', ' ').replace(/^\w/, first => first.toUpperCase())
}

export function valueText(value: unknown): string {
  if (value === undefined || value === null || value === '') return 'Not provided'
  if (typeof value === 'string') return value
  if (typeof value === 'boolean') return value ? 'Yes' : 'No'
  if (typeof value === 'number') return String(value)
  return JSON.stringify(value, null, 2)
}

export function dateTime(value?: string, includeTime = true): string {
  if (!value) return 'Not yet'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return 'Unknown date'
  return new Intl.DateTimeFormat('en-GB', {
    day: 'numeric', month: 'short', year: 'numeric',
    ...(includeTime ? { hour: '2-digit', minute: '2-digit' } : {}),
    timeZone: displayTimezone,
  }).format(date)
}

export function shortDate(value?: string): string {
  if (!value) return 'Unknown'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? 'Unknown' : new Intl.DateTimeFormat('en-GB', { day: 'numeric', month: 'short', timeZone: displayTimezone }).format(date)
}

export function freshness(value?: string, now = Date.now()): string {
  if (!value) return 'Not verified'
  const minutes = Math.floor((now - new Date(value).getTime()) / 60000)
  if (!Number.isFinite(minutes) || minutes < 0) return 'Verification needs review'
  if (minutes < 1) return 'Verified just now'
  if (minutes < 60) return `Verified ${minutes}m ago${minutes > 30 ? ' · stale' : ''}`
  if (minutes < 1440) return `Verified ${Math.floor(minutes / 60)}h ago · stale`
  return `Verified ${Math.floor(minutes / 1440)}d ago · stale`
}

export function safeExternalUrl(value?: string): string | undefined {
  if (!value) return undefined
  try {
    const url = new URL(value)
    return ['https:', 'http:'].includes(url.protocol) && !url.username && !url.password ? url.href : undefined
  } catch { return undefined }
}

export function csv(value: string): string[] { return [...new Set(value.split(',').map(s => s.trim()).filter(Boolean))] }
export function bytes(value: number): string { return value < 1024 ? `${value} B` : value < 1024 * 1024 ? `${(value / 1024).toFixed(1)} KB` : `${(value / (1024 * 1024)).toFixed(1)} MB` }
export function number(value?: number): string { return value === undefined ? '—' : new Intl.NumberFormat('en-GB').format(value) }
export function amount(value?: number): string { return value === undefined ? '—' : new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(value / 100) }
export function initials(name?: string): string { return name?.split(/\s+/).filter(Boolean).slice(0, 2).map(word => word[0]).join('').toUpperCase() || 'OA' }

export const tracks = [
  ['data_ai', 'Data / AI / MLOps'], ['software', 'Software / DevOps'],
  ['data_bi', 'Data / BI'], ['academic', 'Funded research'],
] as const
export const families = [
  ['data_engineering', 'Data Engineering'], ['ai_mlops', 'AI / MLOps'],
  ['software_devops', 'Software / DevOps'], ['data_bi', 'Data / BI'], ['academic', 'Academic research'],
] as const
export const applicationStates = ['discovered', 'assessing', 'drafting', 'ready', 'submitting', 'sent_unconfirmed', 'submitted_confirmed', 'needs_human', 'submission_uncertain', 'interview', 'offer', 'rejected', 'withdrawn', 'skipped']

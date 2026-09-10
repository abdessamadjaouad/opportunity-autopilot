import { useEffect, useState } from 'react'

let csrfToken: string | undefined
export function setCsrfToken(token?: string) { csrfToken = token }

export class ApiError extends Error {
  status: number
  constructor(message: string, status: number) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers)
  const method = options.method?.toUpperCase() ?? 'GET'
  if (options.body && !(options.body instanceof FormData)) headers.set('Content-Type', 'application/json')
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method) && csrfToken) headers.set('X-CSRF-Token', csrfToken)
  let response: Response
  try {
    response = await fetch(`/api/v1${path}`, { ...options, headers, credentials: 'same-origin' })
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error
    throw new ApiError('The local API is unavailable. Check that the backend is running, then retry.', 0)
  }
  const isJson = response.headers.get('content-type')?.includes('application/json')
  const body = isJson ? await response.json() : null
  if (!response.ok) {
    if (response.status === 401 && path !== '/login' && path !== '/session') {
      window.dispatchEvent(new Event('session-expired'))
    }
    const detail = body?.detail
    const message = typeof detail === 'string' ? detail
      : Array.isArray(detail) ? detail.map((item: { loc?: unknown[]; msg?: string }) => `${item.loc?.slice(1).join('.') ?? 'Input'}: ${item.msg ?? 'Invalid value'}`).join('; ')
      : `Request could not be completed (${response.status}).`
    throw new ApiError(message, response.status)
  }
  if (response.status !== 204 && !isJson) throw new ApiError('The API returned an unexpected response. Check the local API configuration.', response.status)
  return body as T
}

export function mutation<T = unknown>(path: string, body: unknown = {}, method = 'POST') {
  return api<T>(path, { method, body: JSON.stringify(body) })
}

export function useResource<T>(path: string | null, revision = 0) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(Boolean(path))
  useEffect(() => {
    const abort = new AbortController()
    if (!path) { setData(null); setError(null); setLoading(false); return }
    setLoading(true)
    setError(null)
    api<T>(path, { signal: abort.signal })
      .then(value => { if (!abort.signal.aborted) setData(value) })
      .catch((reason: Error) => { if (!abort.signal.aborted) { setData(null); setError(reason.message) } })
      .finally(() => { if (!abort.signal.aborted) setLoading(false) })
    return () => abort.abort()
  }, [path, revision])
  return { data, loading, error }
}

export type RunAction = <T>(action: () => Promise<T>, message: string, after?: (value: T) => void) => Promise<T | undefined>

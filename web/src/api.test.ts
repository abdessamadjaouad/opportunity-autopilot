import { afterEach, expect, test, vi } from 'vitest'
import { api, mutation, setCsrfToken } from './api'

afterEach(() => { vi.unstubAllGlobals(); setCsrfToken() })

test('mutations carry CSRF and same-origin credentials', async () => {
  const fetch = vi.fn().mockResolvedValue(Response.json({ saved: true }))
  vi.stubGlobal('fetch', fetch)
  setCsrfToken('fixture-csrf')
  expect(await mutation('/controls', { paused: true }, 'PATCH')).toEqual({ saved: true })
  const [url, options] = fetch.mock.calls[0]
  expect(url).toBe('/api/v1/controls')
  expect(options.credentials).toBe('same-origin')
  expect(options.headers.get('X-CSRF-Token')).toBe('fixture-csrf')
  expect(options.headers.get('Content-Type')).toBe('application/json')
  expect(JSON.parse(options.body)).toEqual({ paused: true })
})

test('PDF uploads leave multipart boundary generation to the browser', async () => {
  const fetch = vi.fn().mockResolvedValue(Response.json({ stored: true }))
  vi.stubGlobal('fetch', fetch)
  const body = new FormData()
  body.append('file', new Blob(['fixture'], { type: 'application/pdf' }), 'fixture.pdf')
  await api('/attachments', { method: 'POST', body })
  expect(fetch.mock.calls[0][1].headers.has('Content-Type')).toBe(false)
})

test('expired sessions clear the private workspace', async () => {
  const dispatchEvent = vi.fn()
  vi.stubGlobal('window', { dispatchEvent })
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(Response.json({ detail: 'Session expired' }, { status: 401 })))
  await expect(api('/applications')).rejects.toMatchObject({ status: 401, message: 'Session expired' })
  expect(dispatchEvent.mock.calls[0][0].type).toBe('session-expired')
})

test('HTML fallback cannot masquerade as a successful API response', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('<html/>', { headers: { 'Content-Type': 'text/html' } })))
  await expect(api('/applications')).rejects.toThrow('unexpected response')
})

test('cancelled requests retain cancellation semantics', async () => {
  const error = new DOMException('Cancelled', 'AbortError')
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(error))
  await expect(api('/applications')).rejects.toBe(error)
})

test('validation failures expose field errors', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(Response.json({ detail: [
    { loc: ['body', 'daily_submission_cap'], msg: 'Must be nonnegative' },
  ] }, { status: 422 })))
  await expect(mutation('/controls')).rejects.toMatchObject({ status: 422, message: 'daily_submission_cap: Must be nonnegative' })
})

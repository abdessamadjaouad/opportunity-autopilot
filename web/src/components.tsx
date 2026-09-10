import { useEffect, useId, useRef, useState } from 'react'
import type { ButtonHTMLAttributes, ReactNode } from 'react'
import { api, mutation } from './api'
import { bytes, dateTime, label, safeExternalUrl, valueText } from './format'
import type { Artifact, Data, Packet, TimelineEvent } from './types'
import { useWorkspace } from './workspace'

export type IconName = 'compass' | 'briefcase' | 'hand' | 'file' | 'sliders' | 'arrow' | 'plus' | 'search' | 'check' | 'close' | 'external' | 'refresh' | 'pause' | 'play' | 'shield' | 'chevron' | 'logout' | 'clock' | 'download' | 'bell' | 'globe' | 'mail' | 'link' | 'upload' | 'info'
const iconPaths: Record<IconName, ReactNode> = {
  compass: <><circle cx="12" cy="12" r="9"/><path d="m16 8-2.5 5.5L8 16l2.5-5.5Z"/></>,
  briefcase: <><rect x="3" y="7" width="18" height="14" rx="2"/><path d="M8 7V5a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M3 12l9 3 9-3M12 12v4"/></>,
  hand: <><path d="M8 12V5a2 2 0 0 1 4 0v7-9a2 2 0 0 1 4 0v9-5a2 2 0 0 1 4 0v8c0 4-3 6-6 6h-1c-2 0-3-1-4-2l-5-6a2 2 0 0 1 3-3l1 2Z"/></>,
  file: <><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z"/><path d="M14 2v6h6M8 13h8M8 17h5"/></>,
  sliders: <><path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 10h6M9 8h6M17 16h6"/></>,
  arrow: <path d="M4 12h16m-6-6 6 6-6 6"/>, plus: <path d="M12 5v14M5 12h14"/>,
  search: <><circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/></>,
  check: <path d="m5 12 4 4L19 6"/>, close: <path d="m6 6 12 12M6 18 18 6"/>,
  external: <><path d="M14 3h7v7M21 3 10 14M10 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-5"/></>,
  refresh: <><path d="M20 7v5h-5M4 17v-5h5"/><path d="M6 6a8 8 0 0 1 13 2l1 4M4 12l1 4a8 8 0 0 0 13 2"/></>,
  pause: <><path d="M8 5v14M16 5v14"/></>, play: <path d="m8 4 12 8-12 8Z"/>,
  shield: <><path d="m12 2 8 4v6c0 5-8 10-8 10S4 17 4 12V6Z"/><path d="m8 12 3 3 5-6"/></>,
  chevron: <path d="m9 5 7 7-7 7"/>, logout: <><path d="M9 3H5v18h4M13 7l5 5-5 5M8 12h13"/></>,
  clock: <><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></>,
  download: <><path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5"/></>,
  bell: <><path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4"/></>,
  globe: <><circle cx="12" cy="12" r="9"/><ellipse cx="12" cy="12" rx="4" ry="9"/><path d="M3 12h18"/></>,
  mail: <><rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3 6 9 7 9-7"/></>,
  link: <><path d="m10 13 4-4M8 15l-2 2a3.5 3.5 0 0 1-5-5l5-5a3.5 3.5 0 0 1 5 0M13 17a3.5 3.5 0 0 0 5 0l5-5a3.5 3.5 0 0 0-5-5l-2 2" transform="translate(0 -1) scale(.95)"/></>,
  upload: <><path d="M12 16V3m-5 5 5-5 5 5M4 16v5h16v-5"/></>,
  info: <><circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7h.01"/></>,
}
export function Icon({ name, size = 20, className = '' }: { name: IconName; size?: number; className?: string }) {
  return <svg className={`icon ${className}`} width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{iconPaths[name]}</svg>
}

export function Button({ children, kind = 'secondary', icon, className = '', ...props }: ButtonHTMLAttributes<HTMLButtonElement> & { kind?: 'primary' | 'secondary' | 'ghost' | 'danger'; icon?: IconName }) {
  return <button type="button" className={`button button-${kind} ${className}`} {...props}>{icon && <Icon name={icon} size={17}/>}<span>{children}</span></button>
}
export function Status({ value, children }: { value?: unknown; children?: ReactNode }) {
  const status = typeof value === 'string' ? value.toLowerCase() : 'unknown'
  const tone = ['pass', 'open', 'confirmed', 'submitted_confirmed', 'healthy', 'strong', 'validated', 'connected', 'completed', 'resolved', 'offer'].includes(status) ? 'positive'
    : ['fail', 'closed', 'expired', 'rejected', 'failed', 'error', 'invalid', 'submission_uncertain', 'conflicting'].includes(status) ? 'negative'
    : ['unknown', 'unverified', 'unconfirmed', 'needs_human', 'sent_unconfirmed', 'plausible', 'pending', 'stale'].includes(status) ? 'warning' : 'neutral'
  return <span className={`status status-${tone}`}><span className="status-dot"/>{children ?? label(value)}</span>
}
export function Field({ label: title, hint, children, className = '' }: { label: string; hint?: string; children: ReactNode; className?: string }) {
  return <label className={`field ${className}`}><span className="field-label">{title}</span>{children}{hint && <span className="field-hint">{hint}</span>}</label>
}
export function Search({ value, onChange, placeholder = 'Search opportunities' }: { value: string; onChange: (value: string) => void; placeholder?: string }) {
  return <label className="search"><Icon name="search" size={18}/><span className="sr-only">{placeholder}</span><input type="search" value={value} onChange={e => onChange(e.target.value)} placeholder={placeholder}/></label>
}
export function Empty({ icon = 'compass', title, children, action }: { icon?: IconName; title: string; children: ReactNode; action?: ReactNode }) {
  return <div className="empty-state"><span className="empty-icon"><Icon name={icon} size={29}/></span><h3>{title}</h3><p>{children}</p>{action}</div>
}
export function ErrorPanel({ error, retry }: { error: string; retry?: () => void }) {
  return <div className="error-panel" role="alert"><Icon name="info"/><div><strong>Could not load this information</strong><p>{error}</p></div>{retry && <Button onClick={retry} icon="refresh">Retry</Button>}</div>
}
export function Loading({ label: title = 'Loading workspace…' }: { label?: string }) {
  return <div className="loading" role="status"><span className="spinner"/>{title}</div>
}
export function PageHeading({ eyebrow, title, children, action }: { eyebrow?: string; title: string; children?: ReactNode; action?: ReactNode }) {
  return <header className="page-heading"><div>{eyebrow && <div className="eyebrow">{eyebrow}</div>}<h1>{title}</h1>{children && <p>{children}</p>}</div>{action && <div className="heading-actions">{action}</div>}</header>
}
export function Section({ title, subtitle, action, children, className = '' }: { title: string; subtitle?: string; action?: ReactNode; children: ReactNode; className?: string }) {
  return <section className={`panel ${className}`}><div className="section-heading"><div><h2>{title}</h2>{subtitle && <p>{subtitle}</p>}</div>{action}</div>{children}</section>
}
export function Tabs({ tabs, value, onChange }: { tabs: { id: string; label: string; count?: number }[]; value: string; onChange: (id: string) => void }) {
  return <div className="tabs" role="tablist">{tabs.map(tab => <button role="tab" key={tab.id} aria-selected={tab.id === value} className={tab.id === value ? 'active' : ''} onClick={() => onChange(tab.id)}>{tab.label}{tab.count !== undefined && <span className="tab-count">{tab.count}</span>}</button>)}</div>
}
export function Dialog({ title, children, onClose, wide = false }: { title: string; children: ReactNode; onClose: () => void; wide?: boolean }) {
  const ref = useRef<HTMLDialogElement>(null)
  const id = useId()
  useEffect(() => { const dialog = ref.current; if (dialog && !dialog.open) dialog.showModal(); return () => dialog?.close() }, [])
  return <dialog ref={ref} aria-labelledby={id} className={wide ? 'dialog dialog-wide' : 'dialog'} onCancel={onClose} onClick={e => { if (e.target === e.currentTarget) onClose() }}><div className="dialog-inner"><div className="dialog-heading"><h2 id={id}>{title}</h2><button type="button" className="icon-button" aria-label="Close dialog" onClick={onClose}><Icon name="close"/></button></div>{children}</div></dialog>
}
export function ExternalLink({ url, children = 'View official source' }: { url?: string; children?: ReactNode }) {
  const safe = safeExternalUrl(url)
  return safe ? <a href={safe} target="_blank" rel="noopener noreferrer" className="external-link">{children}<Icon name="external" size={14}/></a> : <span className="muted">No valid source link</span>
}
export function Timeline({ events = [] }: { events?: TimelineEvent[] }) {
  if (!events.length) return <p className="panel-empty">No recorded events yet. Every application step will be retained here.</p>
  return <ol className="timeline">{events.map((event, i) => <li key={event.id ?? i}><span className="timeline-marker"/><div><div className="timeline-top"><strong>{label(event.type ?? event.event_type ?? event.kind ?? event.action, 'Recorded event')}</strong><time>{dateTime(event.created_at)}</time></div><p>{event.message ?? event.note ?? event.description ?? valueText(event.data)}</p></div></li>)}</ol>
}
export function Details({ data, empty = 'No evidence recorded yet.' }: { data?: Data | unknown[] | null; empty?: string }) {
  if (!data || !Object.keys(data).length) return <p className="muted">{empty}</p>
  if (Array.isArray(data)) return <ul className="evidence-list">{data.map((item, index) => <li key={index}><pre className="plain-data">{valueText(item)}</pre></li>)}</ul>
  return <dl className="data-list">{Object.entries(data).map(([key, value]) => <div key={key}><dt>{label(key)}</dt><dd>{valueText(value)}</dd></div>)}</dl>
}
export function FactLine({ title, children }: { title: string; children: ReactNode }) {
  return <div className="fact-line"><span>{title}</span><strong>{children}</strong></div>
}
export function DownloadButton({ artifact }: { artifact: Artifact }) {
  const { run, busy } = useWorkspace()
  async function download() {
    const ticket = await mutation<{ url: string; expires_in: number }>(`/artifacts/${artifact.id}/ticket`)
    const url = new URL(ticket.url, window.location.origin)
    if (url.origin !== window.location.origin || !url.pathname.startsWith('/api/')) throw new Error('The API returned an invalid private download link.')
    const response = await fetch(url.href, { credentials: 'same-origin' })
    if (!response.ok) throw new Error('The private download expired or is unavailable. Please try again.')
    const blob = await response.blob()
    const objectUrl = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = objectUrl; anchor.download = artifact.filename; anchor.click()
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 30_000)
  }
  return <button type="button" className="artifact" disabled={busy} onClick={() => void run(download, 'Private document downloaded.')}><span className="artifact-icon"><Icon name="file"/></span><span><strong>{artifact.filename}</strong><small>{bytes(artifact.size_bytes)} · {artifact.mime_type === 'application/pdf' ? 'PDF' : artifact.mime_type}</small></span><Icon name="download" size={18}/></button>
}
export function PacketCard({ packet }: { packet: Packet }) {
  const [open, setOpen] = useState(false)
  return <article className="packet-card"><div className="packet-heading"><span className="file-tile"><Icon name="file" size={23}/></span><div><h3>{packet.title || `${label(packet.family, 'Application')} packet`}</h3><p>{label(packet.language)} · {dateTime(packet.created_at)}</p></div><Status value={packet.status}/></div><div className="artifact-list">{packet.artifacts?.map(artifact => <div key={artifact.id}><DownloadButton artifact={artifact}/><PreviewButton artifact={artifact}/></div>)}{!packet.artifacts?.length && <p className="muted">No downloadable artifacts have been created.</p>}</div>{!!packet.blockers?.length && <div className="inline-notice"><strong>Preparation needs review</strong><Details data={packet.blockers}/></div>}<button className="text-button" type="button" onClick={() => setOpen(!open)} aria-expanded={open}>{open ? 'Hide' : 'Review'} manifest and validation<Icon name="chevron" size={14}/></button>{open && <div className="packet-manifest"><h4>Validation</h4><Details data={packet.validation ?? packet.validations}/><h4>Immutable manifest</h4><Details data={packet.manifest}/><p className="micro muted">Packet ID: {packet.id}</p></div>}</article>
}

export function PreviewButton({ artifact }: { artifact: Artifact }) {
  const { run, busy } = useWorkspace()
  const [url, setUrl] = useState<string | null>(null)
  useEffect(() => () => { if (url) URL.revokeObjectURL(url) }, [url])
  if (artifact.mime_type !== 'application/pdf') return null
  async function preview() {
    const ticket = await mutation<{ url: string }>(`/artifacts/${artifact.id}/ticket`)
    const destination = new URL(ticket.url, window.location.origin)
    if (destination.origin !== window.location.origin || !destination.pathname.startsWith('/api/')) throw new Error('Invalid artifact link')
    const response = await fetch(destination, { credentials: 'same-origin' })
    if (!response.ok) throw new Error('Preview expired; retry')
    setUrl(URL.createObjectURL(await response.blob()))
  }
  return <><Button kind="ghost" disabled={busy} onClick={() => void run(preview, 'Private preview opened.')}>Preview PDF</Button>{url && <Dialog title={artifact.filename} wide onClose={() => setUrl(null)}><iframe title="Private PDF preview" src={url} style={{ width: '100%', height: '65vh', border: 0 }}/><DownloadButton artifact={artifact}/></Dialog>}</>
}

export function CopyButton({ value, label: title = 'Copy answer' }: { value: string; label?: string }) {
  const { run } = useWorkspace()
  return <Button kind="ghost" onClick={() => void run(async () => { if (!navigator.clipboard) throw new Error('Clipboard access is unavailable here. Select and copy the text instead.'); await navigator.clipboard.writeText(value) }, 'Copied to clipboard.')}>{title}</Button>
}

export async function uploadAttachment(kind: string, file: File) {
  const form = new FormData(); form.set('kind', kind); form.set('file', file)
  return api('/profile/attachments', { method: 'POST', body: form })
}

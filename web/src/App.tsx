import { useCallback, useEffect, useState } from 'react'
import type { FormEvent, ReactNode } from 'react'
import { api, mutation, setCsrfToken, useResource } from './api'
import type { RunAction } from './api'
import { Button, ErrorPanel, Icon, Loading } from './components'
import type { IconName } from './components'
import { initials, number } from './format'
import type { Dashboard, Session } from './types'
import { WorkspaceContext } from './workspace'
import { ApplicationsView, HumanTasksView, OpportunitiesView } from './views/Opportunities'
import { DocumentsView } from './views/Documents'
import { AutomationView } from './views/Automation'

const navigation: { id: string; title: string; short: string; icon: IconName }[] = [
  { id: 'opportunities', title: 'New opportunities', short: 'Discover', icon: 'compass' },
  { id: 'applications', title: 'Applications', short: 'Applications', icon: 'briefcase' },
  { id: 'human-tasks', title: 'Needs human intervention', short: 'Your input', icon: 'hand' },
  { id: 'documents', title: 'Documents & profile', short: 'Profile', icon: 'file' },
  { id: 'automation', title: 'Sources & automation', short: 'Automation', icon: 'sliders' },
]

function route() {
  const [page, id] = window.location.hash.slice(1).split('/')
  return { page: navigation.some(item => item.id === page) ? page : 'opportunities', id }
}

export default function App() {
  const [session, setSession] = useState<Session | null>(null)
  const [sessionError, setSessionError] = useState<string | null>(null)
  const [sessionLoading, setSessionLoading] = useState(true)
  const [location, setLocation] = useState(route)
  const [revision, setRevision] = useState(0)
  const [pending, setPending] = useState(0)
  const [notice, setNotice] = useState<{ text: string; error: boolean } | null>(null)
  const refresh = useCallback(() => setRevision(value => value + 1), [])
  const navigate = useCallback((path: string) => { window.location.hash = path; window.scrollTo({ top: 0, behavior: 'instant' }) }, [])
  const refreshSession = useCallback(async () => {
    setSessionLoading(true); setSessionError(null)
    try { const value = await api<Session>('/session'); setSession(value); setCsrfToken(value.csrf_token) }
    catch (error) { setSessionError((error as Error).message) }
    finally { setSessionLoading(false) }
  }, [])
  useEffect(() => { void refreshSession() }, [refreshSession])
  useEffect(() => {
    const change = () => setLocation(route())
    const expired = () => { setCsrfToken(undefined); setSession({ authenticated: false, setup_required: false, live_enabled: false }); setNotice({ text: 'Your session expired. Sign in to continue; saved work is retained.', error: true }) }
    window.addEventListener('hashchange', change); window.addEventListener('session-expired', expired)
    return () => { window.removeEventListener('hashchange', change); window.removeEventListener('session-expired', expired) }
  }, [])
  useEffect(() => { if (notice && !notice.error) { const timeout = window.setTimeout(() => setNotice(null), 6500); return () => window.clearTimeout(timeout) } }, [notice])
  const run: RunAction = useCallback(async (action, message, after) => {
    setPending(value => value + 1); setNotice(null)
    try { const result = await action(); after?.(result); setNotice({ text: message, error: false }); refresh(); return result }
    catch (error) { setNotice({ text: error instanceof Error ? error.message : 'The action could not be completed.', error: true }); return undefined }
    finally { setPending(value => value - 1) }
  }, [refresh])
  const dashboard = useResource<Dashboard>(session?.authenticated ? '/dashboard' : null, revision)
  const owner = typeof session?.owner === 'string' ? session.owner : session?.owner?.name

  let content: ReactNode
  if (sessionLoading) content = <div className="auth-page"><Loading label="Opening your private workspace…"/></div>
  else if (sessionError) content = <div className="auth-page"><div className="auth-card"><Brand/><ErrorPanel error={sessionError} retry={() => void refreshSession()}/></div></div>
  else if (!session?.authenticated) content = <Login setupRequired={Boolean(session?.setup_required)} onSession={value => { setSession(value); setCsrfToken(value.csrf_token); setNotice(null); refresh() }} onRefresh={refreshSession}/>
  else content = <WorkspaceContext.Provider value={{ revision, refresh, run, busy: pending > 0, session, navigate }}>
    <div className="workspace">
      <a className="skip-link" href="#main-content" onClick={event => { event.preventDefault(); document.getElementById('main-content')?.focus() }}>Skip to content</a>
      <aside className="sidebar"><Brand/><div className="workspace-label">YOUR WORKSPACE</div><nav aria-label="Main navigation">{navigation.map(item => <button key={item.id} className={`nav-item ${location.page === item.id ? 'active' : ''}`} onClick={() => navigate(item.id)} aria-current={location.page === item.id ? 'page' : undefined}><Icon name={item.icon}/><span>{item.title}</span>{item.id === 'human-tasks' && !!dashboard.data?.counts.needs_human && <span className="nav-count">{dashboard.data.counts.needs_human}</span>}</button>)}</nav><div className="sidebar-footer"><div className="sidebar-note"><Icon name="shield" size={18}/><div><strong>Private by design</strong><p>Your facts, with sources.<br/>Every application accounted for.</p></div></div><div className="owner"><span className="avatar">{initials(owner)}</span><div><strong>{owner || 'Your private workspace'}</strong><span>Owner account</span></div><button className="icon-button" aria-label="Sign out" onClick={() => void run(() => mutation('/logout'), 'Signed out.', () => { setCsrfToken(undefined); setSession({ authenticated: false, setup_required: false, live_enabled: false }) })}><Icon name="logout" size={18}/></button></div></div></aside>
      <div className="main-shell"><div className="topbar"><div className="breadcrumb"><span>Workspace</span><Icon name="chevron" size={13}/><strong>{navigation.find(item => item.id === location.page)?.title}</strong></div><div className="topbar-actions"><span className="mode-indicator"><span/>{dashboard.data?.controls.paused ? 'Paused' : session.live_enabled ? 'Policy gated' : 'Draft only'}</span><Button kind="ghost" icon="refresh" onClick={refresh} aria-label="Refresh workspace">Refresh</Button><Button icon={dashboard.data?.controls.paused ? 'play' : 'pause'} disabled={pending > 0 || !dashboard.data} onClick={() => void run(() => mutation('/controls', { paused: !dashboard.data?.controls.paused }, 'PATCH'), dashboard.data?.controls.paused ? 'Submission pause cleared. Current policy and safeguards apply.' : 'New submissions paused. Already in-flight requests cannot be recalled.')}>{dashboard.data?.controls.paused ? 'Resume' : 'Pause'}</Button></div></div>
        <main id="main-content" tabIndex={-1}>
          {!session.live_enabled && <div className="safety-strip"><Icon name="shield" size={16}/><span><strong>Draft-only workspace.</strong> Live submissions are disabled. Spending requires separate authorization.</span><button type="button" onClick={() => navigate('automation')}>View controls<Icon name="arrow" size={14}/></button></div>}
          {dashboard.error && <ErrorPanel error={dashboard.error} retry={refresh}/>}
          {location.page === 'opportunities' && <OpportunitiesView id={location.id} dashboard={dashboard.data}/>}
          {location.page === 'applications' && <ApplicationsView id={location.id}/>}
          {location.page === 'human-tasks' && <HumanTasksView/>}
          {location.page === 'documents' && <DocumentsView/>}
          {location.page === 'automation' && <AutomationView/>}
        </main><footer className="page-footer"><span><span className="footer-dot"/>Private workspace</span><span>All times in Africa/Casablanca · Source timezones retained</span><span>{dashboard.data ? `${number(dashboard.data.coverage.registered)} registered sources` : 'Coverage unavailable'}</span><button className="text-button" onClick={() => void run(() => mutation('/logout'), 'Signed out.', () => { setCsrfToken(undefined); setSession({ authenticated: false, setup_required: false, live_enabled: false }) })}>Sign out</button></footer>
      </div>
      <nav className="mobile-nav" aria-label="Mobile navigation">{navigation.map(item => <button key={item.id} aria-current={location.page === item.id ? 'page' : undefined} className={location.page === item.id ? 'active' : ''} onClick={() => navigate(item.id)}><Icon name={item.icon} size={21}/><span>{item.short}</span></button>)}</nav>
    </div>
  </WorkspaceContext.Provider>
  return <>{content}{notice && <div className={`toast ${notice.error ? 'toast-error' : ''}`} role={notice.error ? 'alert' : 'status'}><Icon name={notice.error ? 'info' : 'check'} size={20}/><p>{notice.text}</p><button className="icon-button" onClick={() => setNotice(null)} aria-label="Dismiss notification"><Icon name="close" size={17}/></button></div>}</>
}

function Brand() { return <div className="brand"><span className="brand-mark"><svg width="24" height="24" viewBox="0 0 30 30" fill="none" aria-hidden="true"><path d="m5 24 10-18 10 18M10 17h10" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/></svg></span><div><strong>Opportunity</strong><span>AUTOPILOT</span></div></div> }

function Login({ setupRequired, onSession, onRefresh }: { setupRequired: boolean; onSession: (session: Session) => void; onRefresh: () => Promise<void> }) {
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  async function submit(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError(null)
    try { const session = await mutation<Session>('/login', { password }); setPassword(''); onSession(session) }
    catch (reason) { setError((reason as Error).message); setPassword('') }
    finally { setBusy(false) }
  }
  return <div className="auth-page"><div className="auth-intro"><Brand/><div className="eyebrow">A MORE INTENTIONAL NEXT CHAPTER</div><h1>Your next opportunity,<br/><span>thoughtfully pursued.</span></h1><p>A private place to discover roles, prepare truthful applications and keep every next step in view.</p><div className="auth-promises"><span><Icon name="compass"/>Opportunities beyond borders</span><span><Icon name="file"/>Materials grounded in your experience</span><span><Icon name="shield"/>You stay in control</span></div><div className="auth-orbit" aria-hidden="true"><div/><div/><div/><span><Icon name="compass" size={46}/></span></div></div><div className="auth-card"><span className="eyebrow">OWNER ACCESS</span><h2>{setupRequired ? 'Set up your workspace' : 'Welcome back'}</h2><p>{setupRequired ? 'Create your owner password securely in the terminal before opening your private dashboard.' : 'Sign in to your private Opportunity Autopilot workspace.'}</p>{setupRequired ? <><div className="inline-notice">Run .venv/bin/python -m app setup in the application directory. There is no public registration. Your password is never stored in this page or shared with external services.</div><Button kind="primary" icon="refresh" onClick={() => void onRefresh()}>Check setup again</Button></> : <form onSubmit={submit}><fieldset disabled={busy}><label className="field"><span className="field-label">Owner password</span><input type="password" autoComplete="current-password" required value={password} onChange={event => setPassword(event.target.value)} autoFocus placeholder="Enter your owner password"/></label>{error && <p className="form-error" role="alert">{error}</p>}<Button type="submit" kind="primary" className="full-width" icon="arrow">{busy ? 'Signing in…' : 'Open workspace'}</Button></fieldset></form>}<div className="auth-security"><Icon name="shield" size={16}/><span>Private session · Secure same-origin authentication</span></div></div></div>
}

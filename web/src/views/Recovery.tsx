import { useState } from 'react'
import { mutation, useResource } from '../api'
import { Button, Details, Dialog, Field, Loading } from '../components'
import type { Application, Data, HumanTask } from '../types'
import { useWorkspace } from '../workspace'

export function ReceiptAction({ application }: { application: Application }) {
  const [open, setOpen] = useState(false)
  if (!['submission_uncertain', 'sent_unconfirmed'].includes(application.state)) return null
  return <><Button onClick={() => setOpen(true)}>Record receipt or retry decision</Button>{open && <ReceiptForm applicationId={application.id} uncertain={application.state === 'submission_uncertain'} onClose={() => setOpen(false)}/>}</>
}

export function ReceiptForm({ applicationId, uncertain = true, onClose }: { applicationId: string; uncertain?: boolean; onClose: () => void }) {
  const { run, busy } = useWorkspace()
  const [decision, setDecision] = useState('confirm_receipt')
  const [evidence, setEvidence] = useState('')
  const [risk, setRisk] = useState(false)
  return <Dialog title="Resolve submission evidence" onClose={onClose}><form onSubmit={e => { e.preventDefault(); void run(() => mutation(`/applications/${applicationId}/reconciliation-decision`, { decision, evidence, accept_duplicate_risk: risk }), 'Decision recorded. Any retry still requires a fresh packet and current policy.', onClose) }}><fieldset disabled={busy}><Field label="Decision"><select value={decision} onChange={e => setDecision(e.target.value)}><option value="confirm_receipt">I have an employer receipt</option>{uncertain && <option value="authorize_retry">Authorize retry despite uncertain acceptance</option>}</select></Field><Field label="Evidence" hint="Identify the receipt, reference and date, or explain your provider checks."><textarea required minLength={20} rows={5} value={evidence} onChange={e => setEvidence(e.target.value)}/></Field>{decision === 'authorize_retry' && <label className="checkbox-label consent-checkbox"><input type="checkbox" required checked={risk} onChange={e => setRisk(e.target.checked)}/>I understand the previous attempt may have been accepted. I explicitly authorize a possible duplicate. Known provider acceptance still prevents retry.</label>}<div className="dialog-actions"><Button onClick={onClose}>Cancel</Button><Button type="submit" kind="primary">Record evidence decision</Button></div></fieldset></form></Dialog>
}

export function AssociateMessageForm({ task, onClose }: { task: HumanTask; onClose: () => void }) {
  const { run, busy, revision } = useWorkspace()
  const messageId = String(task.context?.email_id || '')
  const message = useResource<Data>(messageId ? `/messages/${messageId}` : null, revision)
  const applications = useResource<Application[]>('/applications', revision)
  const [selected, setSelected] = useState('')
  const [evidence, setEvidence] = useState('')
  return <Dialog title="Associate incoming message" wide onClose={onClose}>{message.data ? <Details data={{ sender: message.data.sender, subject: message.data.subject, body: message.data.body }}/> : <Loading/>}<form onSubmit={e => { e.preventDefault(); void run(() => mutation(`/messages/${messageId}/associate`, { application_id: selected, evidence }), 'Message association recorded with your evidence.', onClose) }}><fieldset disabled={busy}><Field label="Matching application"><select required value={selected} onChange={e => setSelected(e.target.value)}><option value="">Select application</option>{applications.data?.map(a => <option key={a.id} value={a.id}>{a.employer} · {a.title}</option>)}</select></Field><Field label="Association evidence"><textarea required minLength={20} value={evidence} onChange={e => setEvidence(e.target.value)} placeholder="Matching role reference, sender and conversation…"/></Field><div className="dialog-actions"><Button onClick={onClose}>Cancel</Button><Button type="submit" kind="primary">Associate message</Button></div></fieldset></form></Dialog>
}

export type Data = Record<string, unknown>

export interface Session {
  authenticated: boolean
  setup_required: boolean
  csrf_token?: string
  owner?: string | { name?: string }
  live_enabled: boolean
  paid_services_enabled?: boolean
}

export interface RecordBase { id: string; created_at?: string }
export interface Controls {
  paused?: boolean
  daily_cap?: number
  daily_budget_cents?: number
  monthly_budget_cents?: number
  daily_request_cap?: number
  daily_token_cap?: number
  retention_days?: number
}
export interface Notification extends RecordBase {
  status?: string
  urgency?: string
  title?: string
  message?: string
  body?: string
  severity?: string
  read_at?: string
  read?: boolean
}
export interface Dashboard {
  counts: { opportunities: number; applications: number; needs_human: number; confirmed: number; uncertain: number }
  controls: Controls
  coverage: { registered: number; healthy: number; failed: number; last_check?: string }
  notifications: Notification[]
  recent_opportunities: Opportunity[]
}
export interface TimelineEvent extends RecordBase {
  type?: string
  event_type?: string
  kind?: string
  action?: string
  message?: string
  note?: string
  description?: string
  data?: Data
}
export interface Requirement extends Data {
  key?: string
  name?: string
  label?: string
  description?: string
  mandatory?: boolean
  status?: string
  evidence?: unknown
  reason?: string
}
export interface Assessment extends Data {
  eligibility?: string
  relocation?: string
  fit?: string
  technical_fit?: string
  research_fit?: string
  reasons?: unknown[]
  gaps?: unknown[]
  matches?: unknown[]
  requirements?: Requirement[]
}
export interface Occurrence extends RecordBase {
  source_id?: string
  source_name?: string
  url?: string
  first_seen_at?: string
  last_seen_at?: string
}
export interface Opportunity extends RecordBase {
  title: string
  employer: string
  country?: string
  track?: string
  route?: string
  url?: string
  availability?: string
  description?: string
  language?: string
  first_seen_at?: string
  last_seen_at?: string
  last_verified_at?: string
  source_posted_at?: string
  deadline?: string
  deadline_text?: string
  deadline_timezone?: string
  content_hash?: string
  fit?: string
  eligibility?: string
  relocation?: string
  assessment?: Assessment
  application_id?: string
  application_state?: string
  is_fixture?: boolean
  history_import?: boolean
  occurrences?: Occurrence[]
  requirements?: Requirement[]
  application?: Application
  packets?: Packet[]
  tasks?: HumanTask[]
  timeline?: TimelineEvent[]
}
export interface Application extends RecordBase {
  opportunity_id: string
  title: string
  employer: string
  country?: string
  state: string
  updated_at?: string
  packet_id?: string
  route?: string
  timeline?: TimelineEvent[]
  packets?: Packet[]
  tasks?: HumanTask[]
  attempts?: Data[]
  messages?: Data[]
  manual_evidence?: string
  manual_reference?: string
  history_import?: boolean
}
export interface HumanTask extends RecordBase {
  application_id?: string
  opportunity_id?: string
  title: string
  reason: string
  question?: string
  priority?: string | number
  status: string
  context?: Data & { url?: string; documents?: unknown[]; answers?: unknown }
  resolution?: string
}
export interface ProfileFact extends RecordBase {
  key: string
  label?: string
  value: unknown
  status: string
  sources: unknown[]
  owner_correction?: unknown
  revision?: string | number
  category?: string
  employment_type?: string
}
export interface Answer extends RecordBase {
  key: string
  value: unknown
  context?: string | Data
  sensitive?: boolean
  consent?: boolean
}
export interface Profile {
  candidate: { id: string; name: string; revision: string | number; preferences: Data }
  facts: ProfileFact[]
  answers: Answer[]
  revisions: Data[]
  unresolved_count: number
}
export interface Artifact extends RecordBase {
  filename: string
  mime_type: string
  sha256: string
  size_bytes: number
}
export interface Packet extends RecordBase {
  application_id: string
  title?: string
  status: string
  language?: string
  family?: string
  manifest?: Data
  validation?: Data | unknown[]
  validations?: Data | unknown[]
  blockers?: unknown[]
  artifacts?: Artifact[]
}
export interface Source extends RecordBase {
  name: string
  connector: string
  url?: string
  board_id?: string
  enabled: boolean
  permission_status?: string
  reviewed_at?: string
  poll_interval_seconds: number
  last_success_at?: string
  next_check_at?: string
  status?: string
  error?: string
  failures?: number
  occurrence_count?: number
}
export interface Policy extends Data {
  revision?: string
  mode?: string
  approved?: boolean
  allowed_countries?: string[]
  allowed_tracks?: string[]
  allowed_routes?: string[]
  allowed_domains?: string[]
  allowed_document_families?: string[]
  approved_answers?: unknown
  reviewed_packet_ids?: string[]
}
export interface Connection extends RecordBase {
  provider?: string
  name?: string
  status?: string
  configured?: boolean
  error?: string
  last_checked_at?: string
  scopes?: string[]
}
export interface Automation {
  controls: Controls
  policy: Policy
  policy_history: Policy[]
  connections: Connection[]
  budget: { daily_cents: number; monthly_cents: number; requests: number; tokens: number }
  jobs: Data[]
  source_runs: Data[]
  metrics: Data
  notifications: Notification[]
}

# Opportunity Autopilot

Private, evidence-backed opportunity discovery and application workflow.

Opportunity Autopilot imports a candidate profile from existing documents, discovers opportunities, explains eligibility, prepares tailored application packets, tracks every application, and preserves uncertain work in a resumable human-intervention queue. It is designed to fail closed: drafts are never shown as sent, uncertain submissions are never shown as confirmed, and external actions require explicit policy and runtime authorization.

## Features

- Private owner-only dashboard with persistent SQLite or PostgreSQL storage
- Profile import, source citations, conflict reconciliation, and revision history
- Greenhouse, Lever, Ashby, research-board, RSS/Atom, HTML, and email-alert discovery
- Conservative deduplication with complete source provenance
- Three-valued eligibility matching with evidence and explicit unknowns
- Tailored French and English CVs, cover letters, and supporting documents
- Email, reviewed browser-adapter, and manual application routes
- Transactional outbox, quotas, budgets, reconciliation, and duplicate-send protection
- Resumable **Needs human intervention** queue for CAPTCHA, MFA, consent, missing facts, changed forms, and uncertain outcomes
- Background scheduling, health checks, retention, encrypted backups, notifications, pause controls, and recovery after worker interruption

## Quick start

### Requirements

- Python 3.12 or newer
- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- Node.js 24 or newer with npm
- XeLaTeX and Lato fonts for PDF generation
- Bubblewrap on Linux for isolated document rendering

### Omarchy / Arch Linux

Opportunity Autopilot was developed and tested on Omarchy. Install every required system package through Omarchy's package command:

```bash
omarchy pkg add python uv nodejs npm bubblewrap texlive-xetex texlive-latexextra texlive-fontsextra poppler
```

`texlive-fontsextra` supplies the Lato files used by generated PDFs. Omarchy already provides the supported Arch Linux environment; no Hyprland or desktop configuration changes are required. Continue with step 1 below after the command finishes.

### Debian / Ubuntu

Install the system packages with:

```bash
sudo apt update
sudo apt install bubblewrap texlive-xetex texlive-latex-extra fonts-lato poppler-utils
```

### 1. Open the project

```bash
cd opportunity-autopilot
```

### 2. Install Python dependencies

```bash
uv sync --frozen
```

### 3. Install and build the dashboard

```bash
npm --prefix web ci
npm --prefix web run build
```

### 4. Install Chromium for browser adapters and browser tests

```bash
.venv/bin/playwright install chromium
```

### 5. Create local configuration

```bash
cp .env.example .env
```

The defaults use a private local SQLite database and keep live submissions and paid services disabled.

### 6. Start the app

```bash
./run.sh
```

The first run asks for an owner password in the terminal. Use at least 12 characters. The password is not entered into configuration files or sent to an external service.

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). `run.sh` starts the dashboard API and background worker together. Press `Ctrl+C` to stop both.

## Import a profile

Set the directory containing the candidate's CVs and supporting documents in `.env`:

```dotenv
OA_PORTFOLIO_ROOT=/absolute/path/to/portfolio
```

Then run:

```bash
.venv/bin/python -m app import-profile
```

Review imported facts and conflicts in **Documents & profile** before preparing an application. Imported claims remain unconfirmed until reconciled.

## Safety model

The default configuration is safe for local exploration:

```dotenv
OA_LIVE_SUBMISSIONS_ENABLED=false
OA_PAID_SERVICES_ENABLED=false
```

Keep both values disabled until the connected account, approved policy, quotas, destinations, document families, and recovery workflow have been reviewed. Enabling a flag alone does not bypass the policy, freshness, eligibility, connection, budget, pause, packet-integrity, or duplicate-send gates.

The application uses these states deliberately:

| State | Meaning |
| --- | --- |
| Draft | Materials exist locally; no submission was attempted |
| Ready | Every current policy and safety gate passed |
| Sent, unconfirmed | A provider accepted the request; employer receipt is not proven |
| Submitted, confirmed | A receipt or correctly associated acknowledgment exists |
| Submission uncertain | An external effect may have occurred; automatic retry is blocked |
| Needs human intervention | The workflow saved its progress and requires an owner decision |

Fixtures and simulators are visibly marked and cannot be loaded as production browser adapters.

## Configuration

Copy `.env.example` and change only what you need. Store secrets in owner-readable files and reference their paths; do not place secret values directly in `.env`.

| Variable | Purpose | Default |
| --- | --- | --- |
| `OA_PUBLIC_URL` | Browser origin and OAuth callback base | `http://127.0.0.1:8000` |
| `OA_DATA_DIR` | Database, artifacts, and local secrets | `./data` |
| `OA_PORTFOLIO_ROOT` | Documents used for profile import | Parent directory |
| `OA_DATABASE_URL` | SQLAlchemy SQLite or PostgreSQL URL | Local SQLite |
| `OA_DATABASE_URL_FILE` | File containing the database URL | Unconfigured |
| `OA_GOOGLE_CLIENT_ID` | Gmail OAuth client ID | Unconfigured |
| `OA_GOOGLE_CLIENT_SECRET_FILE` | Gmail OAuth client-secret file | Unconfigured |
| `OA_BROWSER_ADAPTER_REGISTRY` | Reviewed browser-adapter registry | Unconfigured |
| `OA_SEARCH_API_KEY_FILE` | Optional search-provider key file | Unconfigured |
| `OA_OPENAI_API_KEY_FILE` | Optional structured-extraction key file | Unconfigured |
| `OA_BACKUP_KEY_FILE` | External encryption key for backups | Unconfigured |
| `OA_BACKUP_DIRECTORY` | Private backup destination | Unconfigured |
| `OA_LIVE_SUBMISSIONS_ENABLED` | Permit policy-gated external submissions | `false` |
| `OA_PAID_SERVICES_ENABLED` | Permit configured paid integrations | `false` |

Gmail uses OAuth onboarding from the dashboard. Never copy passwords, session cookies, refresh tokens, or API keys into chat or application forms.

## Integration status

| Component | Status |
| --- | --- |
| Local dashboard, SQLite, profile workflow, matching, documents, and tracking | Tested |
| PostgreSQL migrations and concurrent control updates | Tested with an isolated database |
| Greenhouse, Lever, Ashby, Cambridge, RSS/Atom, HTML, and email-alert readers | Tested with local fixtures; selected public read-only sources supported |
| Gmail OAuth, sending, mailbox polling, and reconciliation | Tested with simulated or mocked providers; account unconfigured by default |
| Browser submission | Tested against local fixture forms; each real site needs a reviewed adapter registry entry |
| Search and model-assisted extraction | Optional; unconfigured and paid work disabled by default |
| Docker Compose deployment | Configuration provided; requires operator-managed secrets and infrastructure validation |
| Live submissions | Disabled until explicitly configured and authorized |

## Commands

```bash
# Set or replace the local owner password
.venv/bin/python -m app setup

# Run API and background worker
.venv/bin/python -m app start

# Run only the API
.venv/bin/python -m app serve

# Run only the worker
.venv/bin/python -m app worker

# Process one bounded worker tick
.venv/bin/python -m app tick

# Apply database migrations
.venv/bin/python -m app migrate
```

## Testing

```bash
# Python unit and integration tests
.venv/bin/pytest -q

# Python lint
.venv/bin/ruff check app tests migrations

# Frontend tests and production build
npm --prefix web test
npm --prefix web run build
```

The browser tests require Playwright Chromium. The PostgreSQL concurrency test runs when `TEST_POSTGRES_URL` points to an isolated disposable database:

```bash
TEST_POSTGRES_URL='postgresql+psycopg://user@localhost/test_database' .venv/bin/pytest -m postgres -q
```

Never point the PostgreSQL test at a database containing useful data.

## Production deployment

`compose.yaml` provides a production-oriented stack with the API, Celery worker, scheduler, PostgreSQL, Redis, and local HTTPS through Caddy. It requires an external private secrets directory containing:

```text
db_password
database_url
app_key
owner.json
```

Set `OA_SECRETS_DIR`, `OA_PUBLIC_URL`, and `OA_UID` before running Docker Compose. Keep live submissions and spending disabled during deployment validation. Validate backups and restore into an empty destination before relying on unattended operation.

## Project layout

```text
app/api/             Private HTTP API and authentication boundary
app/applications/    Dispatch, reconciliation, follow-ups, notifications
app/browser/         Reviewed native-form browser adapters
app/connectors/      Read-only opportunity source connectors
app/core/            Immutable safety and readiness decisions
app/documents/       Evidence-backed document generation
app/matching/        Eligibility and fit assessment
app/opportunities/   Discovery, verification, and deduplication
app/profile/         Profile import and reconciliation
app/workers/         Scheduling, budgets, health, and recovery
migrations/          Alembic database schema
ops/                 Backup and deployment support
tests/               Unit, integration, browser, and PostgreSQL tests
web/                 React and TypeScript dashboard
```

## Contributing

Create a focused branch, add tests for behavioral changes, run the checks above, and open a pull request explaining the trigger, resulting behavior, and validation. Preserve the fail-closed submission states and never use real applications or recruiter messages as test traffic.

For security reports, use a private repository security advisory when available. Do not include credentials, candidate documents, or live application data in an issue.

# RGS

A self-hosted live call companion with streaming transcription, speaker labels and a question-detection gate, exposed through a web UI and authenticated MCP tools. The default demonstration is intentionally **synthetic and offline**; it does not transcribe microphone speech or answer factual questions.

![Status: experimental](https://img.shields.io/badge/status-experimental-orange) ![License: Apache--2.0](https://img.shields.io/badge/license-Apache--2.0-blue)

Created at [Exowatt](https://exowatt.com).

## Status

Experimental. This initial export preserves the live pipeline, browser UI and MCP surface. Offline mock transcription returns a fixed invented utterance for non-silent audio; the mock gate recognizes questions and emits a trigger, **not a reliable answer**. A real installation needs explicitly configured providers, consent and a secure identity proxy.

## Install

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/), Node.js 22+, npm and Docker (or PostgreSQL 16+). `forwarded-auth` is pinned to the [standalone OSS repository](https://github.com/exowatt-oss/forwarded-auth), which must be made public **before** this repo is published; while it remains private, outsiders cannot install RGS. Once published, installation requires no private registry or Exowatt service.

```sh
git clone https://github.com/exowatt-oss/rgs.git
cd rgs
docker compose -f docker-compose.dev.yml up -d
cp .env.example .env
set -a; . ./.env; set +a
(cd backend && uv sync --locked && uv run alembic upgrade head)
(cd frontend && npm ci)
```

## Usage / Quickstart

In two terminals (source `.env` in the backend terminal only):

```sh
set -a; . ./.env; set +a
(cd backend && uv run uvicorn app.main:app --host 127.0.0.1 --port 8000)
# other terminal:
(cd frontend && npm run dev -- --host 127.0.0.1)
```

Visit `http://localhost:5173/?mock=1` for four **entirely fictional**, replayable conversations with trigger/finding examples; this browser-only view works even without a backend. Without `?mock=1`, the backend uses the explicit local dev identity in `.env`, the mock ASR emits the same synthetic question for non-silent PCM and the mock gate raises a trigger. Audio is not retained by default; finalized text is stored in local PostgreSQL. Do not mistake the mock result for actual speech recognition. The included [agent guide](docs/agents/live-listen.md) describes the authenticated MCP tools; connect to `/mcp` on port 8000.

## Configuration

- **Identity:** `forwarded-auth` defaults to strict verified bearer/JWKS mode, never to unsigned proxy headers. Configure `FORWARDED_AUTH_ISSUER`, `FORWARDED_AUTH_AUDIENCE` and `FORWARDED_AUTH_ALLOWED_EMAIL_DOMAINS` for deployment; optionally configure an IdP login URL as frontend `VITE_AUTH_LOGIN_URL` at build time. A deployment exposing `/mcp` **must** set `FORWARDED_AUTH_TRUSTED_HOSTNAMES=your-rgs.example.org` (comma-separated for multiple public hostnames); without it, forwarded/OIDC-mode MCP requests are rejected. Localhost, `127.0.0.1`, `[::1]` and `testserver` are not trusted by default in those modes; do not enable `FORWARDED_AUTH_ALLOW_LOOPBACK_HOSTS` on a deployment. `CORS_ORIGINS` defaults empty in production (same-origin only); configure an explicit JSON list if a separate trusted frontend origin needs access. No user becomes an admin automatically: set comma-separated `ADMIN_EMAILS` to verified human addresses only when cross-session admin access is intended. Admins can read all stored transcripts. Machine identities are denied by the public core until an explicit authorization adapter exists. The `.env.example` two-step `FORWARDED_AUTH_MODE=none` / `FORWARDED_AUTH_ALLOW_INSECURE_NONE_MODE=1` is **only for loopback development**. Never run Vite with `--host 0.0.0.0`, or expose it/the backend through a LAN proxy: Vite's `/api` proxy otherwise relays the fixed dev user to remote clients. The backend rejects remote Host/client/Origin/Referer/forwarding headers in none mode, but no app can protect against a local proxy deliberately stripping all client metadata. Production startup rejects none mode.
- **Transcription:** `ASR_PROVIDER=mock` by default. `ASR_PROVIDER=nemotron` requires `NEMOTRON_BASE_URL` (and optional `NEMOTRON_API_KEY`) to a compatible `/invocations` endpoint; an operator may alternatively configure `NEMOTRON_AWS_*` for SageMaker. `LIVE_DIARIZER=nemotron` uses the model's speaker tags; optional `sortformer` needs an operator-supplied endpoint. No GPU/model weights are distributed here. Optional Phonon-2 on-device words are available **only in the [Copper browser](https://github.com/copper-browser/Copper)** with its voice API enabled and site consent; ordinary browsers use the configured server ASR instead.
- **Question gate:** `LIVE_GATE_PROVIDER=mock` by default. `jev` requires your own `JEV_API_KEY` for TypeSafe's HTTP System One endpoint; `openai` requires your own `OPENAI_DECISIONS_API_KEY`, `OPENAI_DECISIONS_MODEL` and `OPENAI_DECISIONS_BASE_URL` for a compatible Decisions API (not the normal Chat Completions endpoint). Sessions may choose any provider explicitly configured on the server; these external services receive selected transcript lines. The code calls HTTP APIs directly: no TypeSafe SDK is bundled or installed. Review the vendor's terms before opting in.
- **Optional capabilities:** `LIVE_NAMING_ENABLED=false`, `LIVE_FALLBACK_AFTER_S=0` and `LIVE_AUDIO_RETENTION_EMAILS=` by default. To enable the built-in answerer, set an operator-owned Anthropic-compatible `RGS_ANSWER_API_URL`, `RGS_ANSWER_API_KEY` and a positive fallback delay. If `LIVE_NAMING_ENABLED=true`, the naming pass also sends selected transcripts **and local user names/emails** to that API; enable it only with participant and directory consent. Naming suggestions match spoken introductions against the local signed-in user table. Speaker renaming works manually; directory/Slack/agent delegation and hosted recap integration require a separate operator-maintained overlay (the public recap route returns 501). The offline demo requires none. Do not enable outbound providers or audio retention without participant consent and a retention policy.

## Development

```sh
(cd backend && uv run pytest -q)           # Postgres from docker compose
(cd backend && uv run ruff check src tests)
(cd backend && uv build)
(cd frontend && npm run build && npm run test -- --maxWorkers=4)
```

The CI job provisions PostgreSQL and installs dependencies from public package sources. This project has no private service tests. Synthetic audio and transcript fixtures are used in the public tests; original call fixtures, company directory data, SSO configuration, branding, deployment manifests and production workflows are deliberately excluded.

## License

Original code is [Apache-2.0](LICENSE), Copyright Exowatt, Inc. See [NOTICE](NOTICE): psycopg/psycopg-binary are dynamically used LGPL-3.0-only dependencies, and the Inter frontend dependency carries the SIL OFL 1.1. JetBrains Mono font files and Exowatt artwork are not shipped.

## Contributing

See [CONTRIBUTING](https://github.com/exowatt-oss/.github/blob/main/CONTRIBUTING.md). All contributions need DCO sign-off (`git commit -s`); no CLA.

## Security

Report vulnerabilities via [private vulnerability reporting](https://github.com/exowatt-oss/rgs/security/advisories/new) after the repo is public, following the [organization security policy](https://github.com/exowatt-oss/.github/blob/main/SECURITY.md). Never include real call transcripts or participant details in issues.

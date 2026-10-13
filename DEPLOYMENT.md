# GitHub-backed deployment

The current application is a Python server with SQLite, encrypted connection grants, and staged uploads. It needs a persistent volume and a single running worker. A static website or an ephemeral serverless filesystem does not provide the required persistence.

## GitHub

Use the intended repository and folder rather than copying this into an unrelated project. A private repository is recommended for the internal portal. Once the exact repository is known, preserve its existing files, inspect any `AGENTS.md`, put this project at the agreed path, and commit the source. The included workflow expects the project at the repository root; use `defaults.run.working-directory` and appropriate paths if installing into a monorepo subfolder.

Only commit the source, templates, lock files, tests, and deployment configuration. `.env`, `local-data`, `.venv`, tokens, keys, uploads, and runtime databases are excluded. GitHub Actions uses fixture credentials only; no real-account secrets are needed for checks.

## Docker host

Build the included Dockerfile, mount persistent storage at `/data`, and supply runtime secrets. The container prepares the volume's ownership then drops to a dedicated non-root user. It starts one Uvicorn worker and omits access logs so OAuth callback codes are not printed.

Required settings:

| Setting | Purpose |
| --- | --- |
| `APP_ENV=production` | Enforce production secret/origin checks |
| `PUBLIC_BASE_URL` | Actual HTTPS portal origin, without a path |
| `PORTAL_USERNAME` | Finance portal login name |
| `PORTAL_PASSWORD` | Private finance portal password |
| `FERNET_KEY` | Stable key for encrypted grants |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | Google OAuth web client |
| `DATA_DIR=/data` | Persistent SQLite/uploads directory |

Create a Fernet key once with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. Store it in the host's secret settings and retain a secure backup outside Git. Changing it makes stored grants unreadable.

## Optional Render deployment

`render.yaml` is an optional blueprint, not an already deployed service. Applying it creates paid compute and a persistent disk; inspect the selected plan in your account before applying. It uses the repository's Dockerfile, one service instance, a 1 GB `/data` disk, `/health`, and deployment after GitHub checks pass.

1. Connect the intended private GitHub repository to your Render account.
2. Create a Blueprint from that repository. For a monorepo, adapt root paths first.
3. Supply the unsynchronised environment settings listed in the blueprint. `PUBLIC_BASE_URL` must be the actual portal's HTTPS origin.
4. Add that origin's `/auth/google/callback` to the Google OAuth client and ensure the Google MCP prerequisites are satisfied.
5. Deploy, log in as finance, and complete the account connection/source verification steps in [CONNECTIONS.md](CONNECTIONS.md).
6. Validate a restart retains state and keep a backup of the encryption key and the SQLite database using SQLite's backup API. Do not copy a live database without its transaction state.

The Docker build and hosted sign-in/read/write paths have not been verified until performed in that account. Do not label a deployment complete from local tests alone.

References: https://render.com/docs/blueprint-spec and https://render.com/docs/disks.

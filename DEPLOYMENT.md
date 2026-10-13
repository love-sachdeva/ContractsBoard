# GitHub-backed deployment

The repository has two Python entrypoints. `app:app` is the original local SQLite server and needs a persistent volume and one worker. `hosted_app:app` serves the same portal on Vercel with Notion and Drive as the primary sources and a small managed store for grants and workflow metadata.

## Vercel: hosted MCP portal without a container

The `hosted_app:app` FastAPI entrypoint uses Notion as the partner source and Drive as the document source. Upstash Redis stores encrypted OAuth grants, selected source IDs, small review and activity records, a Drive listing index, and temporary upload chunks. SQLite and a persistent disk are not used by this entrypoint. The local `app:app` launcher still works as a separate development mode.

Vercel detects the Python entrypoint from `pyproject.toml`; `vercel.json` sets the function duration. The browser sends each contract in 1 MB chunks, then one function assembles it and calls the Drive MCP `create_file` tool. The 20 MB portal limit remains in effect. Chunks expire after 30 minutes and are removed after completion. Review metadata in Redis is not present in the Notion page; retain Redis if you need the review history.

1. Use a Vercel **Pro** team for this Onnivation business portal. The currently connected Vercel account is on Hobby, which Vercel restricts to personal, non-commercial projects. Connect `love-sachdeva/ContractsBoard` as a Python project on the Pro team.
2. Provision an Upstash Redis store in the Vercel Marketplace and link it to the project. Ensure the project has `UPSTASH_REDIS_REST_URL` and `UPSTASH_REDIS_REST_TOKEN` for production.
3. Set `APP_ENV=production`, `PUBLIC_BASE_URL=https://<permanent-production-host>`, `PORTAL_USERNAME=finance`, `PORTAL_PASSWORD`, `FERNET_KEY`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, and `DEMO_MODE=0` as project environment variables. Generate a Fernet key once and retain it across deployments. `PUBLIC_BASE_URL` must be the permanent production alias, not a changing preview URL.
4. In the Google Cloud Web OAuth client, register `<PUBLIC_BASE_URL>/auth/google/callback` exactly. Deploy, log in, connect Notion and Drive under Settings, select the existing sources, then click Refresh & verify sources.
5. Test one status move and one harmless PDF/DOCX upload in the live accounts, check the exact Notion page and Drive folder, then verify grant persistence after a redeploy. Keep a secure backup of `FERNET_KEY`.

The hosted entrypoint has been locally tested against fixture providers and a fake key-value store; a production build, live OAuth, and live write checks still require the Pro project and connected accounts. Do not route live finance traffic to a preview deployment with a different OAuth origin.

## Small setup without Docker or Render

The simplest way to try the real integrations is to run the Python process on your own computer and expose port 3000 through a stable HTTPS tunnel. The computer must stay on while the finance portal is in use; its `local-data` directory holds the database, uploads, and encrypted grants. For an always-on portal, run the same process on a small Linux VM instead (instructions below). Neither path uses a container.

### On the existing Mac, for a live trial

1. Install Tailscale and sign in. Enable Funnel in the Tailscale admin DNS settings, then run `tailscale funnel --bg 3000`. `tailscale funnel status` shows the stable `https://...ts.net` URL. Funnel makes the portal reachable from the internet, so set a strong portal password before enabling it.
2. In the project's `.env`, set `APP_ENV=production`, `PUBLIC_BASE_URL` to that exact HTTPS origin (no trailing path), `DEMO_MODE=0`, `PORTAL_PASSWORD` to a strong password, `FERNET_KEY` to a stable Fernet key, and your `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`. Keep `.env` and `local-data` off GitHub. Preserve any existing Fernet key if this instance already holds grants.
3. Install locked dependencies in a virtual environment and run one worker, bound only to loopback:

   ```sh
   python3 -m venv .venv
   .venv/bin/python -m pip install -r requirements.lock
   .venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 3000 --workers 1 --no-access-log
   ```

4. Register `<PUBLIC_BASE_URL>/auth/google/callback` in Google Cloud's Web OAuth client. Open the HTTPS portal, sign in, and use Settings to connect Notion and Drive and verify the sources. The OAuth redirect must use the HTTPS URL even though Uvicorn listens locally.

After a reboot, restart the Python process. Tailscale Funnel resumes if configured with `--bg`, but it cannot serve the portal if the Python process or Mac is off. A temporary/random tunnel URL is unsuitable for OAuth because the registered callback and `PUBLIC_BASE_URL` must match.

### On a small Linux VM, for an always-on portal

Use a VM with Python 3.12+, a persistent disk, and a DNS name. Clone this repository into `/opt/contractsboard`, create `/opt/contractsboard/.venv`, install `requirements.lock`, and keep `/opt/contractsboard/.env` private. Set `DATA_DIR=/var/lib/contractsboard` in that file, create that directory owned by a dedicated service user, and set the production variables described below. Run the process with systemd using the example in [`deploy/contractsboard.service`](deploy/contractsboard.service); replace the user and paths if your host differs. Point the DNS name at the VM and serve HTTPS through Caddy using [`deploy/Caddyfile`](deploy/Caddyfile), substituting the real hostname. The app listens on `127.0.0.1:3000`; only Caddy accepts outside connections. Register `https://your-hostname/auth/google/callback` in Google Cloud and set `PUBLIC_BASE_URL=https://your-hostname`. Keep one worker and back up `DATA_DIR` and the Fernet key.

The VM and Mac options run the existing code directly; they still require an always-running Python process and persistent local storage. An ordinary static GitHub Pages/Vercel upload cannot execute this application's OAuth callback and upload routes.

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

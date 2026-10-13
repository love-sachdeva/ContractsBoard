# Onnivation Partnership Portal

Finance portal with a partnership board, contract uploads, review notes, and an activity log. Notion and Google Drive operations use their hosted MCP servers through the official Python MCP SDK. Refresh reads source data; user-initiated status moves and uploads perform narrowly defined writes.

## Run or upgrade locally

Keep your existing `.env` and `local-data/` when replacing source files. Stop the server, update the code, and run from the project directory:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.lock
python scripts/setup_local.py
uvicorn app:app --host 127.0.0.1 --port 3000 --workers 1 --no-access-log
```

Open http://localhost:3000. Setup creates local credentials once and preserves an existing `.env`. `PUBLIC_BASE_URL` must match the URL you open, including its port. The application loads `.env` on startup; edits require a restart. Use the password shown by setup or your existing `PORTAL_PASSWORD`.

If upgrading from the original ZIP, add `PUBLIC_BASE_URL=http://localhost:3000` to the existing `.env`. Keep the existing `FERNET_KEY` if present. Legacy REST/Google OAuth grants are not reused by MCP: reconnect both accounts from Settings. Old local records remain in the database. Example rows marked `demo` are excluded from live publishing. If you already edited examples in the old version, inspect those unlinked drafts before publishing.

## Connection experience

1. Open Settings and confirm the Notion data source ID, contracts folder, and upload folder. Select an existing Drive folder by URL or ID; the portal does not create another upload folder.
2. Select **Connect Notion**, sign in, and authorise the workspace. Notion MCP dynamically registers its client; no integration token is needed.
3. Select **Connect Google Drive**, sign in, and approve access. Google administrator prerequisites are in [CONNECTIONS.md](CONNECTIONS.md).
4. Select **Refresh & verify sources**. Only a successful source read/folder check changes a connection to Verified. An OAuth grant alone does not mean a source is available.

These are **team-scoped connections behind one finance login**. Every person given that login can use the connected grants. They are not individual per-visitor sessions or SSO. Use a dedicated finance account and distribute the login only to the intended team. Disconnect removes the portal grant; revoke consent at the provider to remove provider authorisation too.

## Workflow

- Move a linked partner between **To pick up**, **Saket review**, and **On hold**, or change its contract stage. The portal reads existing properties, sends the selected change, and verifies the saved value before reporting success.
- Add a partner to the verified Notion source, or keep a local draft while disconnected.
- Refresh only reads sources. It never publishes demo records or pending edits. **Publish pending drafts** sends user-created drafts and recorded pending fields explicitly.
- Upload a PDF/DOCX. It is staged locally before uploading to the selected Drive folder, then linked to its Notion partner. Partial failure remains visible. Version notes are retained locally.
- Retrying an already-uploaded Drive file only retries its Notion link. The same document hash for a partner is recorded once.
- If a response is lost after a create/upload, the portal will not repeat it automatically. **Reconcile draft/upload** verifies the existing provider record and links it without a duplicate.
- Historical contracts are indexed read-only, including nested folders. Approved source templates are downloaded through Drive MCP when available. Clause extraction is heuristic and needs human review. NDA/other agreements have no automatic reseller baseline.

## GitHub and deployment

Source is ready for a dedicated private GitHub repository. `.env`, databases, keys, staged uploads, virtual environments, and caches are excluded from Git. GitHub Actions checks Python, JavaScript syntax, and local MCP fixtures on pushes and pull requests.

[DEPLOYMENT.md](DEPLOYMENT.md) covers Docker hosting and the optional Render blueprint. Keep state on a persistent volume and use one instance/worker to serialize rotating OAuth refresh tokens. Secrets must remain stable across redeployments.

## Verification

```bash
pip install -r requirements-dev.txt
pytest -q
python scripts/check_frontend.py
```

Tests use an isolated local MCP server and fixture credentials. They verify SDK transport, imports, status writes/readback, uploads/linking/duplicates/retries, source isolation, reconciliation, OAuth state/cookie binding, encrypted grants, dynamic registration reuse, and refresh-token rotation. They do not establish real account authorisation, Google preview membership, or deployed website behaviour. Browser visual QA and the Docker build must be checked in the target environment.

## Integration boundaries

- Official Drive MCP currently requires Workspace Developer Preview access, Drive/Drive MCP service enablement, and Google OAuth client configuration.
- This adapter imports Notion sources up to 100 rows using MCP rows mode. More rows produce an incomplete-source error before importing anything. Workspace plan/quota limitations remain visible as provider failures.
- Live tool schemas are discovered and validated. A schema/permission mismatch fails visibly rather than silently falling back to REST or claiming success.
- Provider errors/timeouts never trigger automatic write replays. Refresh rotation is serialized per provider; OAuth flow state expires after 10 minutes.

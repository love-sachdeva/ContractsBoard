# Administrator connection setup

## Notion

Hosted endpoint: `https://mcp.notion.com/mcp`.

The portal discovers Notion OAuth metadata, dynamically registers a client for its exact callback URI, and uses Authorization Code + PKCE. Registration credentials and OAuth grants are encrypted in SQLite. Reconnection reuses client registration credentials for the same origin. There is no `NOTION_TOKEN` requirement.

Keep the source private. The authorising finance account needs access to the partnership data source and permission to edit it. Configure the source ID in Settings; this is the `collection://...` data source ID, not an arbitrary published page ID.

Expected properties: **Partner** (title), **Queue** (select), **Contract stage** (select), **Notes** (rich text), **Contract link** (URL). Optional properties: **Owner**, **Needs verification**. Queue options: To pick up, Saket review, On hold. Stage options: Not received, NDA / LOI, With partner, Negotiating, For signature, Signed. The portal does not silently change the database schema.

MCP query access depends on the workspace plan and quota. Rows mode is currently limited to 100 rows in this adapter; an incomplete source fails visibly. Changes are verified by fetching the page again. Real provider schemas and workspace access must be validated after authorisation.

## Google Drive

Hosted endpoint: `https://drivemcp.googleapis.com/mcp/v1`.

1. Obtain Google Workspace Developer Preview access for the account/organisation used by the portal.
2. Select a Google Cloud project and enable `drive.googleapis.com` and `drivemcp.googleapis.com`.
3. Configure OAuth consent for the organisation. If using an external testing app, add the authorising account as a test user.
4. Create a **Web application** OAuth client. Register the exact callback:
   - Local: `http://localhost:3000/auth/google/callback`.
   - Hosted: `<PUBLIC_BASE_URL>/auth/google/callback`.
5. Put `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` in local `.env` or the host's secret settings. Do not place them in GitHub, browser JavaScript, or a chat message.
6. Sign in through **Connect Google Drive**. The requested scopes are `drive.readonly` and `drive.file`, matching Google's MCP setup guide.
7. Select the existing contracts and upload folders in Settings. Refresh verifies read access to the contracts folder and `canAddChildren` on the upload folder. Folder ID validation prevents arbitrary query injection.

Upload uses MCP `create_file`, with the chosen folder as `parentId`, binary content base64-encoded, and Google-native conversion disabled to preserve the PDF/DOCX. Downloads and historical discovery also use MCP. Organisation restrictions may make a file ineligible for MCP even when visible in Drive itself.

## Tokens and storage

Set `FERNET_KEY` once and preserve it. Production refuses to start without it. Development can create a local `local-data/fernet.key` if none is configured. OAuth tokens, registration credentials, and transient flow state are encrypted. A provider grant is reused until it expires or is disconnected; rotated refresh tokens are saved before subsequent calls.

Run one worker and one instance. Multi-instance deployments need a distributed grant lock and shared state before they can safely use this implementation. Access tokens and OAuth callback query strings must not be logged: the supplied start commands disable Uvicorn access logs.

## Go-live read/write checks

After the host and account access are configured:

1. Connect each account and verify both selected sources.
2. Check a known partner and contract against the source directly.
3. Use an explicitly selected test partner to move status; verify the exact Notion page changed and unrelated fields remain intact.
4. Upload a harmless test DOCX; verify one Drive file in the selected folder and its Notion link. Submit it again and confirm no duplicate.
5. Restart the service; confirm the data, keys, grants, and uploaded file record persist.

## Official references

- https://developers.notion.com/guides/mcp/build-mcp-client
- https://developers.notion.com/guides/mcp/mcp-supported-tools
- https://developers.google.com/workspace/drive/api/guides/configure-mcp-server
- https://developers.google.com/workspace/drive/api/reference/mcp/tools_list/create_file

"""Team-scoped OAuth grants and MCP calls. Credentials never reach browser code."""
import base64
import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlencode, urlparse

import httpx
import jsonschema
import requests
from anyio.from_thread import start_blocking_portal
from cryptography.fernet import Fernet
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

ENDPOINTS = {"notion": "https://mcp.notion.com/mcp", "google": "https://drivemcp.googleapis.com/mcp/v1"}
GOOGLE_SCOPES = "https://www.googleapis.com/auth/drive.readonly https://www.googleapis.com/auth/drive.file"
ALLOWED_TOOLS = {
    "notion": {"notion-get-self", "notion-fetch", "fetch", "notion-query-data-sources", "notion-update-page", "notion-create-pages", "notion-get-async-task"},
    "google": {"search_files", "get_file_metadata", "create_file", "download_file_content"},
}


class ConnectionError(RuntimeError):
    pass


class WriteUncertainError(ConnectionError):
    pass


def unpack_result(result):
    """Prefer structured data; preserve XML/text for Notion's documented fetch shape."""
    data = result.model_dump(by_alias=True, exclude_none=True) if hasattr(result, "model_dump") else result
    if data.get("isError"):
        # Never echo provider responses which may contain secrets or document text.
        raise ConnectionError("The MCP tool reported an error. Check access, workspace plan, and selected source.")
    value = data.get("structuredContent")
    if value is None:
        texts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
        if len(texts) == 1:
            try:
                value = json.loads(texts[0])
            except ValueError:
                value = {"text": texts[0]}
        else:
            value = {"text": "\n".join(texts)}
    if isinstance(value, dict) and (value.get("error") or value.get("status") in ("error", "failed")):
        raise ConnectionError("Provider rejected the operation. Check permissions and reconnect if needed.")
    return value


def fit_arguments(schema, args):
    """Some Notion releases wrap arguments in 'data'; use the advertised schema."""
    props = schema.get("properties", {})
    if "data" in props and not any(k in props for k in args):
        args = {"data": args}
    elif any(k in props for k in args):
        # Google tool descriptions sometimes use snake_case, JSON schemas camelCase.
        args = dict(args)
    if not ("data" in args):
        converted = {}
        for key, value in args.items():
            snake = "".join("_" + c.lower() if c.isupper() else c for c in key)
            converted[key if key in props else snake if snake in props else key] = value
        args = converted
    try:
        jsonschema.validate(args, schema)
    except jsonschema.ValidationError as exc:
        raise ConnectionError("MCP tool schema differs from this portal adapter; operation was not sent.") from exc
    return args


class Connections:
    def __init__(self, database, data_dir, config):
        self.db = Path(database)
        self.config = config
        if config('APP_ENV') == 'production' and not config('PUBLIC_BASE_URL').startswith('https://'):
            raise ConnectionError('Set an HTTPS PUBLIC_BASE_URL before production startup.')
        self.locks = {name: threading.RLock() for name in ENDPOINTS}
        key = config("FERNET_KEY")
        if not key:
            if config("APP_ENV", "development") == "production":
                raise ConnectionError("Set FERNET_KEY before production startup.")
            key_path = Path(data_dir) / "fernet.key"
            if not key_path.exists():
                try:
                    fd = os.open(key_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                except FileExistsError:
                    pass
                else:
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(Fernet.generate_key())
            key = key_path.read_text().strip()
        self.cipher = Fernet(key.encode())
        with self.database() as db:
            db.execute("CREATE TABLE IF NOT EXISTS oauth_flows (state_hash TEXT PRIMARY KEY, provider TEXT, cookie_hash TEXT, payload TEXT, expires REAL)")

    def database(self):
        return sqlite3.connect(self.db, timeout=30)

    def read(self, key, default=None):
        with self.database() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", ("mcp:" + key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(self.cipher.decrypt(row[0].encode()))
        except Exception as exc:
            raise ConnectionError("Stored connection cannot be decrypted. Restore the original encryption key.") from exc

    def write(self, key, value):
        encrypted = self.cipher.encrypt(json.dumps(value).encode()).decode()
        with self.database() as db:
            db.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", ("mcp:" + key, encrypted))

    def connected(self, provider):
        grant = self.read(provider + ":grant", {})
        return bool(grant.get("access_token")) and not grant.get("reauth_required", False)

    def base_url(self):
        base = self.config("PUBLIC_BASE_URL", "http://localhost:3000").rstrip("/")
        parsed = urlparse(base)
        if parsed.query or parsed.fragment or parsed.path not in ("", "/") or parsed.username or not parsed.hostname:
            raise ConnectionError("PUBLIC_BASE_URL must be an origin, without a path or query.")
        local_hosts = ('localhost', '127.0.0.1', '::1')
        if self.config('APP_ENV', 'development') == 'development':
            local_hosts += ('terminal.local',)  # Internal development preview only.
        if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in local_hosts):
            raise ConnectionError("Use HTTPS for hosted connections.")
        return base

    def redirect_uri(self, provider):
        return self.base_url() + "/auth/" + provider + "/callback"

    @staticmethod
    def checked_notion_url(url):
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname != "mcp.notion.com" or parsed.username or parsed.port not in (None, 443):
            raise ConnectionError("Unexpected Notion OAuth endpoint; connection was not sent.")
        return url

    def metadata(self, provider):
        if provider == "google":
            return {"authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth", "token_endpoint": "https://oauth2.googleapis.com/token"}
        try:
            response = requests.get("https://mcp.notion.com/.well-known/oauth-authorization-server", timeout=20, allow_redirects=False)
        except requests.RequestException as exc:
            raise ConnectionError('Cannot reach Notion. Check network access and try again.') from exc
        if response.status_code != 200:
            raise ConnectionError("Could not discover Notion authorisation settings.")
        metadata = response.json()
        for key in ("authorization_endpoint", "token_endpoint", "registration_endpoint"):
            if metadata.get(key):
                self.checked_notion_url(metadata[key])
        if not metadata.get("authorization_endpoint") or not metadata.get("token_endpoint"):
            raise ConnectionError("Notion OAuth metadata is incomplete.")
        return metadata

    def credentials(self, provider, metadata):
        if provider == "google":
            client = {"client_id": self.config("GOOGLE_CLIENT_ID"), "client_secret": self.config("GOOGLE_CLIENT_SECRET")}
            if not all(client.values()):
                raise ConnectionError("Administrator setup needed: Google OAuth Client ID and Client Secret.")
            return client
        key = "notion:client:" + self.redirect_uri("notion")
        client = self.read(key)
        if client:
            return client
        endpoint = metadata.get("registration_endpoint")
        if not endpoint:
            raise ConnectionError("Notion did not advertise dynamic client registration.")
        response = requests.post(endpoint, json={"client_name": "Onnivation Partnership Portal", "redirect_uris": [self.redirect_uri("notion")], "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"], "token_endpoint_auth_method": "none"}, timeout=25, allow_redirects=False)
        if response.status_code not in (200, 201):
            raise ConnectionError("Notion client registration failed. Check workspace access.")
        client = response.json()
        if not client.get("client_id"):
            raise ConnectionError("Notion registration did not return a client ID.")
        self.write(key, client)
        return client

    def begin(self, provider):
        if provider not in ENDPOINTS:
            raise ConnectionError("Unknown provider.")
        with self.locks[provider]:
            metadata = self.metadata(provider)
            client = self.credentials(provider, metadata)
            state, cookie, verifier = (secrets.token_urlsafe(32) for _ in range(3))
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
            payload = {"metadata": metadata, "client": client, "verifier": verifier, "redirect_uri": self.redirect_uri(provider)}
            with self.database() as db:
                db.execute("DELETE FROM oauth_flows WHERE expires < ?", (time.time(),))
                db.execute("INSERT INTO oauth_flows VALUES(?,?,?,?,?)", (hashlib.sha256(state.encode()).hexdigest(), provider, hashlib.sha256(cookie.encode()).hexdigest(), self.cipher.encrypt(json.dumps(payload).encode()).decode(), time.time() + 600))
            params = {"response_type": "code", "client_id": client["client_id"], "redirect_uri": payload["redirect_uri"], "state": state, "code_challenge": challenge, "code_challenge_method": "S256", "scope": GOOGLE_SCOPES if provider == "google" else "default"}
            if provider == "google":
                params.update(access_type="offline", prompt="consent")
            return metadata["authorization_endpoint"] + "?" + urlencode(params), cookie

    def finish(self, provider, state, cookie, code, error=""):
        if not state or not cookie or provider not in ENDPOINTS:
            raise ConnectionError("This connection request expired. Start again from Settings.")
        with self.locks[provider]:
            state_hash = hashlib.sha256(state.encode()).hexdigest()
            with self.database() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT provider,cookie_hash,payload,expires FROM oauth_flows WHERE state_hash=?", (state_hash,)).fetchone()
                if not row or row[0] != provider or row[3] < time.time() or not secrets.compare_digest(row[1], hashlib.sha256(cookie.encode()).hexdigest()):
                    raise ConnectionError("Connection request does not match this browser. Start again from Settings.")
                db.execute("DELETE FROM oauth_flows WHERE state_hash=?", (state_hash,))
            if error or not code:
                raise ConnectionError("Authorisation was not completed. You can reconnect from Settings.")
            flow = json.loads(self.cipher.decrypt(row[2].encode()))
            params = {"grant_type": "authorization_code", "code": code, "code_verifier": flow["verifier"], "redirect_uri": flow["redirect_uri"], "client_id": flow["client"]["client_id"]}
            if flow["client"].get("client_secret"):
                params["client_secret"] = flow["client"]["client_secret"]
            tokens = self.exchange(flow["metadata"]["token_endpoint"], params)
            if not tokens.get("refresh_token"):
                raise ConnectionError("No offline grant was returned. Reconnect and authorise offline access.")
            tokens.update(metadata=flow["metadata"], client=flow["client"], connected_at=time.time(), expires_at=time.time() + tokens.get("expires_in", 3600), reauth_required=False)
            self.write(provider + ":grant", tokens)
            self.write(provider + ":health", {"verified": False, "error": "Connected. Verify the selected source to complete setup."})

    @staticmethod
    def exchange(endpoint, params):
        try:
            response = requests.post(endpoint, data=params, timeout=25, allow_redirects=False)
        except requests.RequestException as exc:
            raise ConnectionError('OAuth exchange did not complete. Start a new connection request.') from exc
        if response.status_code != 200:
            try:
                code = response.json().get("error", "oauth_error")
            except ValueError:
                code = "oauth_error"
            if code == "invalid_grant":
                raise ConnectionError("reauth_required")
            raise ConnectionError("OAuth exchange failed (" + str(response.status_code) + "). Check administrator configuration.")
        tokens = response.json()
        if not tokens.get("access_token") or tokens.get("token_type", "Bearer").lower() != "bearer":
            raise ConnectionError("Provider did not return a usable access token.")
        return tokens

    def access_token(self, provider):
        # Caller holds the provider lock through token refresh and MCP invocation.
        grant = self.read(provider + ":grant", {})
        if not grant.get("access_token") or grant.get("reauth_required"):
            raise ConnectionError("Reconnect " + ("Notion" if provider == "notion" else "Google Drive") + " in Settings.")
        if grant.get("expires_at", 0) > time.time() + 90:
            return grant["access_token"]
        params = {"grant_type": "refresh_token", "refresh_token": grant["refresh_token"], "client_id": grant["client"]["client_id"]}
        if grant["client"].get("client_secret"):
            params["client_secret"] = grant["client"]["client_secret"]
        try:
            fresh = self.exchange(grant["metadata"]["token_endpoint"], params)
        except ConnectionError as exc:
            if str(exc) == "reauth_required":
                grant.update(reauth_required=True, access_token="", refresh_token="")
                self.write(provider + ":grant", grant)
                self.write(provider + ":health", {"verified": False, "error": "Session expired. Reconnect in Settings."})
            raise
        grant.update(fresh)
        grant["expires_at"] = time.time() + fresh.get("expires_in", 3600)
        self.write(provider + ":grant", grant)  # Atomically retain rotated refresh token.
        return grant["access_token"]

    async def invoke(self, provider, access, tool_name=None, arguments=None):
        # Official SDK handles initialization, negotiated version, SSE, and session IDs.
        with_timeout = timedelta(seconds=120)
        async with httpx.AsyncClient(headers={"Authorization": "Bearer " + access}, timeout=httpx.Timeout(30, read=120)) as client:
            async with streamable_http_client(ENDPOINTS[provider], http_client=client) as (read, write, _):
                async with ClientSession(read, write, read_timeout_seconds=with_timeout) as session:
                    await session.initialize()
                    tools, cursor = [], None
                    for _ in range(20):
                        result = await session.list_tools(cursor=cursor)
                        tools.extend(result.tools)
                        cursor = result.nextCursor
                        if not cursor:
                            break
                    else:
                        raise ConnectionError("MCP tool discovery exceeded its limit.")
                    if tool_name is None:
                        return [{"name": t.name, "inputSchema": t.inputSchema} for t in tools if t.name in ALLOWED_TOOLS[provider]]
                    tool = next((t for t in tools if t.name == tool_name), None)
                    if not tool and tool_name == "notion-fetch":
                        tool = next((t for t in tools if t.name == "fetch"), None)
                    if not tool:
                        raise ConnectionError("Required MCP tool is unavailable: " + tool_name)
                    args = fit_arguments(tool.inputSchema, arguments or {})
                    result = await session.call_tool(tool.name, args, read_timeout_seconds=with_timeout)
                    value = unpack_result(result)
                    # A write may return a pollable task even when synchronous was requested.
                    task = value.get("async_task") if isinstance(value, dict) else None
                    if isinstance(value, dict) and value.get("object") == "async_task":
                        task = value
                    for _ in range(15):
                        if not task:
                            break
                        status = task.get("status")
                        if status == "succeeded":
                            value = task.get("result", {})
                            task = None
                            break
                        if status == "failed":
                            raise ConnectionError("Notion did not save the change.")
                        import anyio
                        await anyio.sleep(min(max(task.get("poll_after_seconds", 2), 1), 5))
                        value = unpack_result(await session.call_tool("notion-get-async-task", {"task_id": task["id"]}))
                        task = value
                    if task:
                        raise ConnectionError("Write completion is uncertain. Refresh the source before retrying.")
                    if isinstance(value, dict) and value.get("warnings"):
                        raise ConnectionError("Notion returned write warnings. Verify the page before retrying.")
                    return value
    def call(self, provider, tool_name, arguments):
        if provider not in ENDPOINTS or tool_name not in ALLOWED_TOOLS[provider]:
            raise ConnectionError("MCP operation is not allowed.")
        with self.locks[provider]:
            access = self.access_token(provider)
            try:
                with start_blocking_portal() as portal:
                    return portal.call(self.invoke, provider, access, tool_name, arguments)
            except ConnectionError:
                raise
            except Exception as exc:
                # Never replay writes on transport errors, or leak access tokens in URLs.
                error_type = WriteUncertainError if tool_name in ('notion-create-pages','notion-update-page','create_file') else ConnectionError
                raise error_type("MCP request did not complete. Check access and refresh before retrying a write.") from exc

    def list_tools(self, provider):
        with self.locks[provider]:
            with start_blocking_portal() as portal:
                return portal.call(self.invoke, provider, self.access_token(provider))

    def status(self, provider):
        health = self.read(provider + ":health", {})
        grant = self.read(provider + ":grant", {})
        return {"connected": self.connected(provider), "verified": bool(health.get("verified")) and self.connected(provider), "error": health.get("error", ""), "workspace": grant.get("workspace_name", ""), "endpoint": ENDPOINTS[provider], "ready": provider == "notion" or bool(self.config("GOOGLE_CLIENT_ID") and self.config("GOOGLE_CLIENT_SECRET")), "redirect_uri": self.redirect_uri(provider)}

    def disconnect(self, provider):
        with self.locks[provider]:
            with self.database() as db:
                db.execute("DELETE FROM settings WHERE key IN (?,?)", ("mcp:" + provider + ":grant", "mcp:" + provider + ":health"))
                db.execute("DELETE FROM oauth_flows WHERE provider=?", (provider,))

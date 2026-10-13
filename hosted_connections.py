"""Hosted MCP grants: the original OAuth/MCP client with Redis persistence."""
import base64
import hashlib
import secrets
import time
from urllib.parse import urlencode

from anyio.from_thread import start_blocking_portal
from mcp_connections import Connections, ConnectionError, WriteUncertainError, ENDPOINTS, GOOGLE_SCOPES, ALLOWED_TOOLS


class HostedConnections(Connections):
    def __init__(self, store, config):
        self.store = store
        self.config = config
        if config("APP_ENV") != "production" or not config("PUBLIC_BASE_URL", "").startswith("https://"):
            raise ConnectionError("The hosted portal needs APP_ENV=production and an HTTPS PUBLIC_BASE_URL.")

    def read(self, key, default=None):
        return self.store.get("mcp:" + key, default)

    def write(self, key, value):
        self.store.put("mcp:" + key, value)

    def begin(self, provider):
        if provider not in ENDPOINTS:
            raise ConnectionError("Unknown provider.")
        with self.store.lock(provider):
            metadata = self.metadata(provider)
            client = self.credentials(provider, metadata)
            state, cookie, verifier = (secrets.token_urlsafe(32) for _ in range(3))
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
            flow = {"metadata": metadata, "client": client, "verifier": verifier, "redirect_uri": self.redirect_uri(provider), "provider": provider, "cookie_hash": hashlib.sha256(cookie.encode()).hexdigest()}
            self.store.put("flow:" + hashlib.sha256(state.encode()).hexdigest(), flow, ttl=600)
            params = {"response_type": "code", "client_id": client["client_id"], "redirect_uri": flow["redirect_uri"], "state": state, "code_challenge": challenge, "code_challenge_method": "S256", "scope": GOOGLE_SCOPES if provider == "google" else "default"}
            if provider == "google":
                params.update(access_type="offline", prompt="consent")
            return metadata["authorization_endpoint"] + "?" + urlencode(params), cookie

    def finish(self, provider, state, cookie, code, error=""):
        if not state or not cookie or provider not in ENDPOINTS:
            raise ConnectionError("This connection request expired. Start again from Settings.")
        with self.store.lock(provider):
            flow = self.store.pop("flow:" + hashlib.sha256(state.encode()).hexdigest())
            if not flow or flow["provider"] != provider or not secrets.compare_digest(flow["cookie_hash"], hashlib.sha256(cookie.encode()).hexdigest()):
                raise ConnectionError("Connection request does not match this browser. Start again from Settings.")
            if error or not code:
                raise ConnectionError("Authorisation was not completed. You can reconnect from Settings.")
            params = {"grant_type": "authorization_code", "code": code, "code_verifier": flow["verifier"], "redirect_uri": flow["redirect_uri"], "client_id": flow["client"]["client_id"]}
            if flow["client"].get("client_secret"):
                params["client_secret"] = flow["client"]["client_secret"]
            tokens = self.exchange(flow["metadata"]["token_endpoint"], params)
            if not tokens.get("refresh_token"):
                raise ConnectionError("No offline grant was returned. Reconnect and authorise offline access.")
            tokens.update(metadata=flow["metadata"], client=flow["client"], connected_at=time.time(), expires_at=time.time() + tokens.get("expires_in", 3600), reauth_required=False)
            self.write(provider + ":grant", tokens)
            self.write(provider + ":health", {"verified": False, "error": "Connected. Verify the selected source to complete setup."})

    def call(self, provider, tool_name, arguments):
        if provider not in ENDPOINTS or tool_name not in ALLOWED_TOOLS[provider]:
            raise ConnectionError("MCP operation is not allowed.")
        with self.store.lock(provider):
            access = self.access_token(provider)
            try:
                with start_blocking_portal() as portal:
                    return portal.call(self.invoke, provider, access, tool_name, arguments)
            except ConnectionError:
                raise
            except Exception as exc:
                error_type = WriteUncertainError if tool_name in ("notion-create-pages", "notion-update-page", "create_file") else ConnectionError
                raise error_type("MCP request did not complete. Check access and refresh before retrying a write.") from exc

    def list_tools(self, provider):
        with self.store.lock(provider):
            with start_blocking_portal() as portal:
                return portal.call(self.invoke, provider, self.access_token(provider))

    def disconnect(self, provider):
        with self.store.lock(provider):
            self.store.delete("mcp:" + provider + ":grant")
            self.store.delete("mcp:" + provider + ":health")

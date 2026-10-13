"""Hosted portal contract flow, with a fake durable store and MCP provider."""
import asyncio
import base64
import contextlib
import importlib
import io
import sys
import uuid
import zipfile

from cryptography.fernet import Fernet
from fastapi import HTTPException
from fastapi.testclient import TestClient
from mcp_connections import WriteUncertainError
from hosted_store import Store


class FakeStore:
    def __init__(self):
        self.data = {}
    def get(self, key, default=None):
        return self.data.get(key, default)
    def put(self, key, value, ttl=None, only_new=False):
        if only_new and key in self.data:
            return False
        self.data[key] = value
        return True
    def delete(self, key):
        self.data.pop(key, None)
    @contextlib.contextmanager
    def lock(self, key, **kwargs):
        yield


def test_hosted_store_encrypts_and_consumes_flow_once(monkeypatch):
    storage = {}
    def post(url, headers, json, timeout):
        verb, key, *rest = json
        if verb == "GET":
            result = storage.get(key)
        elif verb == "GETDEL":
            result = storage.pop(key, None)
        elif verb == "SET":
            value = rest[0]
            if "NX" in rest and key in storage:
                result = None
            else:
                storage[key] = value
                result = "OK"
        elif verb == "DEL":
            result = int(storage.pop(key, None) is not None)
        elif verb == "EVAL":
            result = 1
        else:
            raise AssertionError(verb)
        class Response:
            def raise_for_status(self):
                pass
            def json(self):
                return {"result": result}
        return Response()
    monkeypatch.setattr("hosted_store.requests.post", post)
    client = Store(url="https://example.upstash.io", token="test", key=Fernet.generate_key().decode())
    client.put("oauth", {"secret": "sensitive"}, ttl=600)
    assert "sensitive" not in storage["contractsboard:oauth"]
    assert client.pop("oauth") == {"secret": "sensitive"}
    assert client.pop("oauth") is None


def test_hosted_sources_and_chunked_upload(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://portal.example.com")
    monkeypatch.setenv("PORTAL_PASSWORD", "strong-test-password")
    monkeypatch.setenv("UPSTASH_REDIS_REST_URL", "https://fake.upstash.io")
    monkeypatch.setenv("UPSTASH_REDIS_REST_TOKEN", "fake")
    monkeypatch.setenv("FERNET_KEY", Fernet.generate_key().decode())
    sys.modules.pop("hosted_app", None)
    hosted = importlib.import_module("hosted_app")
    state = FakeStore()
    hosted.store = state
    hosted.connections.store = state
    pid = str(uuid.uuid4())
    drive_id = "drive-file-123456789"
    props = {"Partner": "Example partner", "Queue": "To pick up", "Contract stage": "Negotiating", "Notes": "Follow up", "Contract link": ""}
    calls = []

    def connected(provider):
        return True
    def status(provider):
        return {"connected": True, "verified": True, "error": "", "workspace": "", "endpoint": provider, "ready": True, "redirect_uri": "https://portal.example.com/auth/" + provider + "/callback"}
    def call(provider, name, args):
        calls.append((provider, name, args))
        if name == "notion-query-data-sources":
            return {"results": [{"id": pid, "properties": props}], "has_more": False}
        if name == "notion-fetch":
            return {"properties": props}
        if name == "notion-update-page":
            props.update(args["properties"])
            return {"ok": True}
        if name == "get_file_metadata":
            return {"mimeType": "application/vnd.google-apps.folder", "canAddChildren": True}
        if name == "create_file":
            return {"id": drive_id, "viewUrl": "https://drive.google.com/file/d/" + drive_id + "/view"}
        if name == "search_files":
            return {"files": []}
        raise AssertionError(name)

    monkeypatch.setattr(hosted.connections, "connected", connected)
    monkeypatch.setattr(hosted.connections, "status", status)
    monkeypatch.setattr(hosted.connections, "call", call)
    monkeypatch.setattr(hosted.connections, "write", lambda key, value: state.put("mcp:" + key, value))
    state.put("setting:notion_data_source_id", str(uuid.uuid4()))
    state.put("setting:drive_folder_id", "contracts-folder-1234")
    state.put("setting:upload_folder_id", "uploads-folder-123456")
    web = TestClient(hosted.app)
    auth = {"Authorization": "Basic " + base64.b64encode(b"finance:strong-test-password").decode()}
    assert web.get("/api/overview").status_code == 401
    assert web.get("/api/overview", headers=auth).status_code == 200
    assert web.post("/api/settings", json={}, headers={**auth, "Origin": "https://evil.example"}).status_code == 403
    class Request:
        def __init__(self, json_data=None, body_data=b""):
            self.json_data, self.body_data = json_data, body_data
        async def json(self):
            return self.json_data
        async def body(self):
            return self.body_data

    assert hosted.overview()["partners"][0]["id"] == pid
    moved = asyncio.run(hosted.move(pid, Request({"queue": "Saket review"})))
    assert moved["sync_state"] == "live" and props["Queue"] == "Saket review"

    with io.BytesIO() as stream:
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("word/document.xml", "<document><p>Indemnification and territory.</p></document>")
        document = stream.getvalue()
    started = asyncio.run(hosted.start_upload(Request({"name": "agreement.docx", "size": len(document), "partner_id": pid, "document_type": "NDA"})))
    uid = started["id"]
    assert asyncio.run(hosted.upload_chunk(uid, 0, Request(body_data=document)))["saved"]
    completed = hosted.complete_upload(uid)
    assert completed["status"] == "synced" and props["Contract link"].endswith("/view")
    assert len([c for c in calls if c[1] == "create_file"]) == 1
    assert hosted.complete_upload(uid) == completed and len([c for c in calls if c[1] == "create_file"]) == 1

    # An uncertain MCP write is never repeated; a matching remote file is reconciled.
    second = document + b"different version"
    started = asyncio.run(hosted.start_upload(Request({"name": "revised.docx", "size": len(second), "partner_id": pid, "document_type": "NDA"})))
    uncertain_id = started["id"]
    asyncio.run(hosted.upload_chunk(uncertain_id, 0, Request(body_data=second)))
    monkeypatch.setattr(hosted.services, "upload_drive", lambda *args: (_ for _ in ()).throw(WriteUncertainError("timeout")))
    for _ in range(2):
        try:
            hosted.complete_upload(uncertain_id)
            assert False, "uncertain create cannot report success"
        except HTTPException as exc:
            assert exc.status_code == 409
    monkeypatch.setattr(hosted.services, "file_bytes", lambda fid: second)
    previous_call = hosted.connections.call
    def reconciled_call(provider, name, args):
        if name == "get_file_metadata":
            return {"parentId": "uploads-folder-123456", "viewUrl": "https://drive.google.com/file/d/existing-file-12345/view"}
        return previous_call(provider, name, args)
    monkeypatch.setattr(hosted.connections, "call", reconciled_call)
    result = asyncio.run(hosted.resolve_uncertain_upload(uncertain_id, Request({"drive_id": "existing-file-12345"})))
    assert result["reconciled"] and result["status"] == "synced"
    assert len([c for c in calls if c[1] == "create_file"]) == 1

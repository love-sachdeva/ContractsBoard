"""Vercel entrypoint. Notion owns partners; Drive owns documents."""
import base64
import hashlib
import io
import json
import os
import re
import secrets
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from dotenv import load_dotenv

from hosted_connections import HostedConnections
from hosted_store import Store, StoreError
from mcp_connections import ConnectionError, WriteUncertainError
from mcp_services import FOLDER, QUEUES, STAGES, Services, notion_id, properties, scalar
from hosted_review import analyze_document

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")
def config(k, default=""):
    return os.getenv(k, default)
def now():
    return datetime.now(timezone.utc).isoformat()

if not config("PORTAL_PASSWORD"):
    raise RuntimeError("Set PORTAL_PASSWORD before starting the hosted portal.")
store = Store()
connections = HostedConnections(store, config)
def setting(key, default=""):
    return store.get("setting:" + key, default)
def put_setting(key, value):
    store.put("setting:" + key, value)
services = Services(connections, setting, put_setting, config, None, None, None, None)
app = FastAPI(title="Onnivation Partnership Command Center")

@app.middleware("http")
async def guard(request: Request, call_next):
    if request.url.path in ("/health", "/favicon.ico", "/auth/google/callback", "/auth/notion/callback"):
        return await call_next(request)
    value = request.headers.get("Authorization", "")
    try:
        scheme, encoded = value.split(" ", 1)
        user, pwd = base64.b64decode(encoded).decode().split(":", 1)
        valid = scheme.lower() == "basic" and secrets.compare_digest(user, config("PORTAL_USERNAME", "finance")) and secrets.compare_digest(pwd, config("PORTAL_PASSWORD"))
    except Exception:
        valid = False
    if not valid:
        return JSONResponse({"error": "Authentication required"}, status_code=401, headers={"WWW-Authenticate": 'Basic realm="Onnivation Finance Portal"'})
    if request.method in ("POST", "PATCH", "DELETE"):
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") != connections.base_url():
            return JSONResponse({"error": "Origin rejected"}, status_code=403)
        if request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"error": "Cross-site request rejected"}, status_code=403)
    response = await call_next(request)
    response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY"})
    return response

@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")

@app.get("/health")
def health():
    return {"ok": True}

@app.exception_handler(ConnectionError)
async def connection_failure(request: Request, exc: ConnectionError):
    return JSONResponse({"detail": str(exc)}, status_code=503)

@app.exception_handler(StoreError)
async def store_failure(request: Request, exc: StoreError):
    return JSONResponse({"detail": str(exc)}, status_code=503)

def summary():
    n, g = connections.status("notion"), connections.status("google")
    return {"notion": n["connected"], "google": g["connected"], "notion_verified": n["verified"], "google_verified": g["verified"], "notion_status": n, "google_status": g, "notion_last_sync": setting("notion_sync_at"), "drive_last_sync": setting("drive_sync_at")}

def partners():
    if not services.source() or not connections.connected("notion"):
        return []
    result = []
    for item in services.notion_rows():
        if not isinstance(item, dict):
            raise ConnectionError("Unexpected Notion row shape.")
        props = {key: scalar(value) for key, value in item.get("properties", item).items()}
        if not props.get("Partner"):
            raise ConnectionError("The selected Notion source needs a Partner title property.")
        page_id = notion_id(item.get("id") or item.get("page_id") or item.get("url") or item.get("page_url"))
        queue, stage = props.get("Queue") or QUEUES[0], props.get("Contract stage") or STAGES[0]
        if queue not in QUEUES or stage not in STAGES:
            raise ConnectionError("Unsupported queue or stage option in Notion.")
        result.append({"id": page_id, "notion_id": page_id, "notion_source_id": services.source(), "name": props["Partner"], "queue": queue, "stage": stage, "notes": props.get("Notes") or "", "owner": props.get("Owner") or "", "contract_url": props.get("Contract link") or "", "notion_url": item.get("url") or item.get("page_url") or "https://www.notion.so/" + page_id.replace("-", ""), "verification": int(bool(props.get("Needs verification"))), "source": "notion", "sync_state": "live", "updated": now(), "pending_properties": "{}"})
    return sorted(result, key=lambda p: ({"Saket review": 0, "To pick up": 1, "On hold": 2}[p["queue"]], p["name"].casefold()))

def partner(pid):
    return next((p for p in partners() if p["id"] == pid), None)

def event(pid, action, detail):
    # Activity is convenience metadata; the underlying Notion/Drive changes are authoritative.
    with store.lock("events"):
        events = store.get("events", [])
        events.insert(0, {"id": str(uuid.uuid4()), "partner_id": pid, "actor": "Finance", "action": action, "detail": detail[:500], "created": now()})
        store.put("events", events[:100])

def files():
    return store.get("files", [])

def update_files(items):
    store.put("files", items[:500])

def scan_drive(ps):
    """Read the selected Drive tree and rebuild the display index; files stay in Drive."""
    todo = [services.folder(False)]
    visited = set()
    collected = []
    while todo:
        if len(visited) >= 100:
            raise ConnectionError("Folder scan exceeded 100 folders. Choose a narrower source.")
        folder = todo.pop(0)
        if folder in visited:
            continue
        visited.add(folder)
        cursor, seen = None, set()
        while True:
            args = {"query": "parentId = '" + folder + "'", "pageSize": 100, "excludeContentSnippets": True}
            if cursor:
                args["pageToken"] = cursor
            result = connections.call("google", "search_files", args)
            if not isinstance(result, dict) or not isinstance(result.get("files"), list):
                raise ConnectionError("Drive returned an unexpected file list.")
            for item in result["files"]:
                if item.get("mimeType") == "application/vnd.google-apps.folder":
                    todo.append(item["id"])
                else:
                    collected.append(item)
            cursor = result.get("nextPageToken")
            if not cursor:
                break
            if cursor in seen or len(seen) >= 100:
                raise ConnectionError("Drive pagination did not complete.")
            seen.add(cursor)
    existing = {f["drive_id"]: f for f in files() if f.get("drive_id") and f.get("source") == "portal"}
    by_link = {p["contract_url"]: p["id"] for p in ps if p["contract_url"]}
    for item in collected:
        fid = item.get("id")
        name = item.get("title", "")
        if not fid or (not name.lower().endswith((".pdf", ".docx", ".doc", ".xlsx")) and item.get("mimeType") != "application/vnd.google-apps.document"):
            continue
        if fid in existing:
            continue
        url = item.get("viewUrl") or "https://drive.google.com/file/d/" + fid + "/view"
        pid = by_link.get(url)
        if not pid:
            matches = [p for p in ps if name.casefold().startswith(p["name"].casefold())]
            pid = max(matches, key=lambda p: len(p["name"]))["id"] if matches else None
        existing[fid] = {"id": fid, "partner_id": pid, "name": name, "hash": "", "drive_id": fid, "drive_url": url, "document_type": "Historical contract", "source": "drive", "sync_state": "indexed", "created": item.get("modifiedTime") or now(), "note": ""}
    listing = sorted(existing.values(), key=lambda f: f.get("created", ""), reverse=True)
    update_files(listing)
    return len(collected)

@app.get("/api/overview")
def overview():
    status = summary()
    try:
        ps = partners()
    except ConnectionError as exc:
        ps = []
        status["notion_verified"] = False
        status["notion_status"] = {**status["notion_status"], "verified": False, "error": str(exc)}
    fs = files()
    events = store.get("events", [])
    by_id = {p["id"]: p["name"] for p in ps}
    for e in events:
        e["partner"] = by_id.get(e.get("partner_id"), "")
    mode = "live" if status["notion_verified"] and status["google_verified"] else "partial" if status["notion"] or status["google"] else "local"
    return {"partners": ps, "files": fs, "events": events, "deviation_count": sum(d.get("decision") == "Needs review" for f in fs for d in store.get("review:" + f["id"], [])), "integrations": status, "mode": mode, "hosted": True, "settings": {"notion_data_source_id": services.source(), "drive_folder_id": services.folder(), "upload_folder_id": services.folder(True)}}

@app.post("/api/sync")
def sync():
    result = {}
    ps = []
    try:
        if not services.source() or not connections.connected("notion"):
            raise ConnectionError("Connect Notion and choose the data source first.")
        ps = partners()
        put_setting("notion_sync_at", now())
        connections.write("notion:health", {"verified": True, "error": "", "checked_at": now()})
        result["notion"] = {"success": True, "processed": len(ps)}
    except Exception as exc:
        connections.write("notion:health", {"verified": False, "error": str(exc)[:500], "checked_at": now()})
        result["notion"] = {"success": False, "error": str(exc)}
    try:
        services.verify_folder(False)
        services.verify_folder(True)
        count = scan_drive(ps)
        put_setting("drive_sync_at", now())
        connections.write("google:health", {"verified": True, "error": "", "checked_at": now()})
        result["drive"] = {"success": True, "processed": count}
    except Exception as exc:
        connections.write("google:health", {"verified": False, "error": str(exc)[:500], "checked_at": now()})
        result["drive"] = {"success": False, "error": str(exc)}
    return result

@app.post("/api/partners")
async def create_partner(request: Request):
    name = str((await request.json()).get("name") or "").strip()
    if not 1 <= len(name) <= 200:
        raise HTTPException(400, "Partner name must be 1–200 characters")
    with store.lock("partners"):
        existing = next((p for p in partners() if p["name"].casefold() == name.casefold()), None)
        if existing:
            return {"partner": existing, "existing": True}
        if not services.source() or not connections.connected("notion"):
            raise HTTPException(409, "Connect Notion and select the partnership source first.")
        props = {"Partner": name, "Queue": QUEUES[0], "Contract stage": STAGES[0], "Notes": "Created from portal"}
        try:
            value = connections.call("notion", "notion-create-pages", {"parent": {"data_source_id": notion_id(services.source())}, "pages": [{"properties": props}], "allow_async": False})
        except WriteUncertainError as exc:
            raise HTTPException(409, "Create completion is uncertain. Refresh Notion before trying again.") from exc
        pages = value.get("pages") if isinstance(value, dict) else None
        if not pages or len(pages) != 1:
            raise HTTPException(409, "Create completion is uncertain. Refresh Notion before trying again.")
        pid = notion_id(pages[0].get("id") or pages[0].get("url"))
        actual = properties(connections.call("notion", "notion-fetch", {"id": pid}))
        if actual.get("Partner") != name:
            raise HTTPException(409, "The created page could not be verified. Refresh Notion.")
        event(pid, "Created", "New partner added in Notion")
        return {"partner": partner(pid), "existing": False}

async def change(pid, request, field):
    value = str((await request.json()).get(field) or "")
    if value not in (QUEUES if field == "queue" else STAGES):
        raise HTTPException(400, "Invalid " + field)
    with store.lock("partners"):
        p = partner(pid)
        if not p:
            raise HTTPException(404, "Partner not found")
        key = "Queue" if field == "queue" else "Contract stage"
        services.update_notion(pid, {key: value})
        event(pid, "Queue change" if field == "queue" else "Contract stage", p[field] + " → " + value)
        return {field: value, "sync_state": "live", "warning": ""}

@app.post("/api/partners/{pid}/move")
async def move(pid: str, request: Request):
    return await change(pid, request, "queue")

@app.post("/api/partners/{pid}/stage")
async def stage(pid: str, request: Request):
    return await change(pid, request, "stage")

@app.get("/api/settings")
def settings():
    return {**summary(), "notion_data_source_id": services.source(), "drive_folder_id": services.folder(), "upload_folder_id": services.folder(True), "notion_db": setting("notion_database_url", config("NOTION_DATABASE_URL"))}

@app.post("/api/settings")
async def save_settings(request: Request):
    data = await request.json()
    if not set(data).issubset({"notion_data_source_id", "drive_folder_id", "upload_folder_id"}):
        raise HTTPException(400, "Unknown setting")
    for key, raw in data.items():
        value = str(raw).strip()
        if key == "notion_data_source_id":
            value = notion_id(value)
        else:
            match = re.search(r"/folders/([A-Za-z0-9_-]+)", value)
            value = match[1] if match else value
            if not FOLDER.fullmatch(value):
                raise HTTPException(400, "Enter a valid Drive folder URL or ID")
        if setting(key) != value:
            put_setting(key, value)
            connections.write(("notion" if key == "notion_data_source_id" else "google") + ":health", {"verified": False, "error": "Source changed. Refresh to verify access."})
    return {"saved": True}

@app.post("/api/connections/{provider}/start")
def start_connection(provider: str):
    try:
        url, cookie = connections.begin(provider)
    except ConnectionError as exc:
        raise HTTPException(503, str(exc)) from exc
    response = JSONResponse({"authorization_url": url})
    response.set_cookie("oauth_" + provider, cookie, max_age=600, httponly=True, secure=True, samesite="lax", path="/auth/" + provider + "/callback")
    return response

@app.get("/auth/{provider}/callback")
def callback(provider: str, request: Request, code: str = "", state: str = "", error: str = ""):
    try:
        connections.finish(provider, state, request.cookies.get("oauth_" + provider, ""), code, error)
        event(None, "Connection authorised", provider + " access authorised; verify the selected source")
        location = "/?connected=" + provider
    except (ConnectionError, StoreError) as exc:
        location = "/?connection_error=" + urlencode({"message": str(exc)})[8:]
    response = RedirectResponse(location, status_code=303)
    response.delete_cookie("oauth_" + provider, path="/auth/" + provider + "/callback")
    response.headers.update({"Referrer-Policy": "no-referrer", "Cache-Control": "no-store"})
    return response

@app.post("/api/connections/{provider}/disconnect")
def disconnect(provider: str):
    if provider not in ("notion", "google"):
        raise HTTPException(404, "Unknown provider")
    connections.disconnect(provider)
    return {"disconnected": True}

@app.post("/api/partners/publish-pending")
def publish_pending():
    return {"results": []}

@app.post("/api/partners/{pid}/resolve")
def resolve_partner(pid: str):
    raise HTTPException(400, "Hosted changes save to Notion directly. Refresh the source to inspect an uncertain write.")


MAX_UPLOAD = 20 * 1024 * 1024
CHUNK = 1024 * 1024
DOCTYPES = ("Reseller agreement", "LOI", "NDA", "Other agreement")

def valid_file(name, size):
    if not name or Path(name).name != name or len(name) > 240 or not name.lower().endswith((".pdf", ".docx")):
        raise HTTPException(400, "Choose a PDF or DOCX with a valid filename")
    if not 0 < size <= MAX_UPLOAD:
        raise HTTPException(413, "20 MB upload limit")

@app.post("/api/upload/start")
async def start_upload(request: Request):
    data = await request.json()
    name = str(data.get("name") or "")
    size = int(data.get("size") or 0)
    valid_file(name, size)
    pid = str(data.get("partner_id") or "")
    if not partner(pid):
        raise HTTPException(404, "Select a live Notion partner first")
    doctype = str(data.get("document_type") or "Reseller agreement")
    if doctype not in DOCTYPES:
        raise HTTPException(400, "Unknown document type")
    if not connections.status("google")["verified"]:
        raise HTTPException(409, "Connect and verify Google Drive before uploading")
    uid = str(uuid.uuid4())
    store.put("upload:meta:" + uid, {"name": name, "size": size, "partner_id": pid, "document_type": doctype, "note": str(data.get("note") or "")[:2000], "parts": (size + CHUNK - 1) // CHUNK}, ttl=1800)
    return {"id": uid, "chunk_size": CHUNK}

@app.post("/api/upload/{uid}/chunk/{part}")
async def upload_chunk(uid: str, part: int, request: Request):
    try:
        uuid.UUID(uid)
    except ValueError:
        raise HTTPException(400, "Invalid upload ID")
    meta = store.get("upload:meta:" + uid)
    if not meta or not 0 <= part < meta["parts"]:
        raise HTTPException(404, "Upload expired")
    raw = await request.body()
    expected = min(CHUNK, meta["size"] - part * CHUNK)
    if len(raw) != expected:
        raise HTTPException(400, "Unexpected chunk size")
    store.put("upload:chunk:" + uid + ":" + str(part), base64.b64encode(raw).decode(), ttl=1800)
    return {"saved": True}

@app.post("/api/upload/{uid}/complete")
def complete_upload(uid: str):
    try:
        uuid.UUID(uid)
    except ValueError:
        raise HTTPException(400, "Invalid upload ID")
    with store.lock("upload:" + uid):
        previous = store.get("upload:result:" + uid)
        if previous:
            return previous
        meta = store.get("upload:meta:" + uid)
        if not meta:
            raise HTTPException(404, "Upload expired. Start again.")
        if store.get("upload:uncertain:" + uid):
            raise HTTPException(409, "Drive upload completion is uncertain. Inspect the folder before trying again.")
        content = []
        for n in range(meta["parts"]):
            chunk = store.get("upload:chunk:" + uid + ":" + str(n))
            if chunk is None:
                raise HTTPException(409, "Upload is incomplete")
            content.append(base64.b64decode(chunk, validate=True))
        raw = b"".join(content)
        valid_file(meta["name"], len(raw))
        if len(raw) != meta["size"]:
            raise HTTPException(409, "Upload size changed")
        if meta["name"].lower().endswith(".pdf") and not raw.startswith(b"%PDF"):
            raise HTTPException(400, "File is not a PDF")
        if meta["name"].lower().endswith(".docx"):
            try:
                with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                    if "word/document.xml" not in archive.namelist() or sum(i.file_size for i in archive.infolist()) > 80 * 1024 * 1024:
                        raise ValueError()
            except Exception as exc:
                raise HTTPException(400, "Invalid or oversized DOCX contents") from exc
        digest = hashlib.sha256(raw).hexdigest()
        with store.lock("files"):
            existing = next((f for f in files() if f.get("partner_id") == meta["partner_id"] and f.get("hash") == digest), None)
            if existing:
                result = {"duplicate": True, "file": existing, "status": existing["sync_state"], "warnings": []}
                store.put("upload:result:" + uid, result, ttl=86400)
                return result
            p = partner(meta["partner_id"])
            if not p:
                raise HTTPException(409, "This partner is no longer in the selected Notion source")
            mime = "application/pdf" if meta["name"].lower().endswith(".pdf") else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            services.verify_folder(True)
            store.put("upload:uncertain:" + uid, {**meta, "hash": digest}, ttl=86400)
            try:
                created = services.upload_drive(meta["name"], raw, mime)
            except WriteUncertainError as exc:
                raise HTTPException(409, "Drive upload completion is uncertain. Inspect the upload folder before retrying.") from exc
            fid = created["id"]
            # Drive has the document; retaining this metadata makes recovery possible if Notion fails.
            record = {"id": fid, "partner_id": p["id"], "name": meta["name"], "hash": digest, "drive_id": fid, "drive_url": created["webViewLink"], "document_type": meta["document_type"], "source": "portal", "sync_state": "notion_pending", "created": now(), "note": meta["note"]}
            listing = files()
            listing.insert(0, record)
            update_files(listing)
            store.delete("upload:uncertain:" + uid)
            warnings = []
            try:
                services.update_notion(p["id"], {"Contract link": record["drive_url"]})
                record["sync_state"] = "synced"
                update_files([record if f["id"] == fid else f for f in files()])
            except Exception as exc:
                warnings.append(str(exc))
            try:
                analysis = analyze_document(raw, meta["name"], meta["document_type"], services, config)
            except Exception as exc:
                analysis = {"error": str(exc), "manual_review": True, "deviations": []}
            store.put("review:" + fid, analysis.get("deviations", []))
            record["analysis"] = json.dumps({k: v for k, v in analysis.items() if k != "deviations"})
            update_files([record if f["id"] == fid else f for f in files()])
            event(p["id"], "Uploaded", meta["name"] + " | " + record["sync_state"])
            result = {"file": record, "analysis": analysis, "warnings": warnings, "status": record["sync_state"]}
            store.put("upload:result:" + uid, result, ttl=86400)
            for n in range(meta["parts"]):
                store.delete("upload:chunk:" + uid + ":" + str(n))
            store.delete("upload:meta:" + uid)
            return result

@app.post("/api/upload/{uid}/resolve")
async def resolve_uncertain_upload(uid: str, request: Request):
    try:
        uuid.UUID(uid)
    except ValueError:
        raise HTTPException(400, "Invalid upload ID")
    value = await request.json()
    drive_id = str(value.get("drive_id") or "").strip()
    if not FOLDER.fullmatch(drive_id):
        raise HTTPException(400, "Enter the existing Drive file ID")
    with store.lock("upload:" + uid):
        prior = store.get("upload:result:" + uid)
        if prior:
            return prior
        pending = store.get("upload:uncertain:" + uid)
        if not pending:
            raise HTTPException(404, "There is no uncertain upload to reconcile")
        metadata = connections.call("google", "get_file_metadata", {"fileId": drive_id})
        if metadata.get("parentId") != services.folder(True):
            raise HTTPException(400, "File is not in the selected upload folder")
        content = services.file_bytes(drive_id)
        if hashlib.sha256(content).hexdigest() != pending["hash"]:
            raise HTTPException(400, "Drive content differs from the uploaded document")
        p = partner(pending["partner_id"])
        if not p:
            raise HTTPException(409, "Partner is no longer in the selected Notion source")
        url = metadata.get("viewUrl") or "https://drive.google.com/file/d/" + drive_id + "/view"
        with store.lock("files"):
            existing = next((f for f in files() if f.get("drive_id") == drive_id), None)
            record = existing or {"id": drive_id, "partner_id": p["id"], "name": pending["name"], "hash": pending["hash"], "drive_id": drive_id, "drive_url": url, "document_type": pending["document_type"], "source": "portal", "sync_state": "notion_pending", "created": now(), "note": pending["note"]}
            if not existing:
                update_files([record, *files()])
            warnings = []
            try:
                services.update_notion(p["id"], {"Contract link": url})
                record["sync_state"] = "synced"
            except Exception as exc:
                warnings.append(str(exc))
            try:
                analysis = analyze_document(content, pending["name"], pending["document_type"], services, config)
            except Exception as exc:
                analysis = {"error": str(exc), "manual_review": True, "deviations": []}
            store.put("review:" + drive_id, analysis.get("deviations", []))
            record["analysis"] = json.dumps({k: v for k, v in analysis.items() if k != "deviations"})
            update_files([record if f["id"] == drive_id else f for f in files()])
            result = {"file": record, "analysis": analysis, "warnings": warnings, "status": record["sync_state"], "reconciled": True}
            store.put("upload:result:" + uid, result, ttl=86400)
            store.delete("upload:uncertain:" + uid)
            for n in range(pending["parts"]):
                store.delete("upload:chunk:" + uid + ":" + str(n))
            store.delete("upload:meta:" + uid)
            event(p["id"], "Upload reconciled", "Existing Drive file content verified")
            return result

@app.get("/api/files/{fid}/review")
def review(fid: str):
    item = next((f for f in files() if f["id"] == fid), None)
    if not item:
        raise HTTPException(404, "No file")
    return {"file": item, "deviations": store.get("review:" + fid, [])}

@app.post("/api/deviations/{did}/decision")
async def decision(did: str, request: Request):
    data = await request.json()
    choice = data.get("decision")
    if choice not in ("Approved exception", "Needs review", "Negotiation requested", "Aligned"):
        raise HTTPException(400, "Invalid review state")
    with store.lock("review"):
        for item in files():
            findings = store.get("review:" + item["id"], [])
            for finding in findings:
                if finding["id"] == did:
                    finding.update(decision=choice, reviewer=str(data.get("reviewer") or "Finance")[:100], reviewed=now())
                    store.put("review:" + item["id"], findings)
                    event(item.get("partner_id"), "Clause decision", choice)
                    return {"saved": True, "decision": choice}
    raise HTTPException(404, "Review item missing")

@app.post("/api/files/{fid}/retry")
def retry_file(fid: str):
    with store.lock("files"):
        item = next((f for f in files() if f["id"] == fid), None)
        if not item:
            raise HTTPException(404, "No file")
        if item["sync_state"] != "notion_pending":
            return {"success": item["sync_state"] == "synced", "warnings": [], "file": item}
        p = partner(item["partner_id"])
        if not p:
            raise HTTPException(409, "Notion partner is unavailable")
        services.update_notion(p["id"], {"Contract link": item["drive_url"]})
        item["sync_state"] = "synced"
        update_files([item if f["id"] == fid else f for f in files()])
        return {"success": True, "warnings": [], "file": item}

@app.post("/api/files/{fid}/resolve")
async def resolve_file(fid: str, request: Request):
    raise HTTPException(400, "This hosted upload has a Drive ID after completion. Inspect the selected folder for uncertain uploads before starting a new one.")

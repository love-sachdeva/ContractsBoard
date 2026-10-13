"""Restricted, deterministic operations on the portal's configured sources."""
import base64
import json
import re
import uuid
from datetime import datetime, timezone

from mcp_connections import ConnectionError, WriteUncertainError

UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|[0-9a-fA-F]{32}")
FOLDER = re.compile(r"^[A-Za-z0-9_-]{10,200}$")
QUEUES = ("To pick up", "Saket review", "On hold")
STAGES = ("Not received", "NDA / LOI", "With partner", "Negotiating", "For signature", "Signed")


def now():
    return datetime.now(timezone.utc).isoformat()


def notion_id(value):
    matches = UUID.findall(str(value))
    if not matches:
        raise ConnectionError("Notion did not return a valid page ID.")
    return str(uuid.UUID(matches[-1]))


def scalar(value):
    if isinstance(value, dict):
        typ = value.get("type")
        if typ in ("select", "status"):
            return (value.get(typ) or {}).get("name", "")
        if typ in ("title", "rich_text"):
            return "".join(x.get("plain_text") or x.get("text", {}).get("content", "") for x in value.get(typ, []))
        if typ == "checkbox":
            return bool(value.get("checkbox"))
        if typ == "url":
            return value.get("url") or ""
    return value


def properties(value):
    """Notion fetch can supply JSON properties or properties inside XML text."""
    if isinstance(value, dict):
        if isinstance(value.get("properties"), dict):
            return {k: scalar(v) for k, v in value["properties"].items()}
        for key in ("text", "content"):
            text = value.get(key)
            if isinstance(text, str):
                match = re.search(r"<properties[^>]*>\s*(\{.*?\})\s*</properties>", text, re.S)
                if match:
                    return {k: scalar(v) for k, v in json.loads(match[1]).items()}
        if isinstance(value.get("page"), dict):
            return properties(value["page"])
    raise ConnectionError("Notion page properties could not be read. Nothing was changed.")


class Services:
    def __init__(self, connections, setting, put_setting, config, rows, one, execute, log):
        self.connections, self.setting, self.put_setting, self.config = connections, setting, put_setting, config
        self.rows, self.one, self.execute, self.log = rows, one, execute, log

    def source(self):
        return self.setting("notion_data_source_id", self.config("NOTION_DATA_SOURCE_ID"))

    def folder(self, upload=False):
        key = "upload_folder_id" if upload else "drive_folder_id"
        env = "GOOGLE_UPLOAD_FOLDER_ID" if upload else "GOOGLE_DRIVE_FOLDER_ID"
        default = ""
        value = self.setting(key, self.config(env, default))
        if value and not FOLDER.fullmatch(value):
            raise ConnectionError("Choose a valid Google Drive folder in Settings.")
        return value

    def notion_rows(self):
        source = notion_id(self.source())
        result = self.connections.call("notion", "notion-query-data-sources", {"mode": "rows", "data_source_url": "collection://" + source, "limit": 100})
        if not isinstance(result, dict) or not isinstance(result.get("results"), list):
            raise ConnectionError("Notion did not return database rows. Check query access for this workspace.")
        # Rows mode has no cursor. Never turn an incomplete import into apparent success.
        if result.get("has_more"):
            raise ConnectionError("Source has more than 100 rows. This MCP rows adapter cannot import the complete source yet; no rows were changed.")
        return result["results"]

    def sync_notion(self):
        incoming = self.notion_rows()
        normalized = []
        for item in incoming:
            if not isinstance(item, dict):
                raise ConnectionError("Unexpected Notion row shape; import stopped.")
            props = {k: scalar(v) for k, v in item.get("properties", item).items()}
            if not props.get("Partner"):
                raise ConnectionError("Expected a Partner title property. Select the correct partnership data source.")
            page_url = item.get("url") or item.get("page_url") or props.get("url")
            page_id = item.get("id") or item.get("page_id") or page_url
            page_id = notion_id(page_id)
            queue, stage = props.get("Queue") or "To pick up", props.get("Contract stage") or "Not received"
            if queue not in QUEUES or stage not in STAGES:
                raise ConnectionError("Source contains a queue or contract stage this portal does not support.")
            normalized.append((page_id, page_url or "https://www.notion.so/" + page_id.replace("-", ""), props, queue, stage))
        count = 0
        for page_id, page_url, props, queue, stage in normalized:
            # Never match a demo seed by company name to a real workspace record.
            match = self.one("SELECT * FROM partners WHERE notion_id=? AND source!='demo'", (page_id,))
            if match and match["sync_state"] == "pending":
                continue
            values = (str(props["Partner"]), queue, stage, str(props.get("Notes") or ""), str(props.get("Owner") or ""), page_id, page_url, str(props.get("Contract link") or ""), int(bool(props.get("Needs verification"))), "live", now())
            if match:
                self.execute("UPDATE partners SET name=?,queue=?,stage=?,notes=?,owner=?,notion_id=?,notion_url=?,contract_url=?,verification=?,sync_state=?,updated=? WHERE id=?", (*values, match["id"]))
            else:
                self.execute("INSERT INTO partners(name,queue,stage,notes,owner,notion_id,notion_url,contract_url,verification,sync_state,updated,id,source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (*values, str(uuid.uuid4()), "notion"))
            self.execute("UPDATE partners SET notion_source_id=? WHERE notion_id=?",(self.source(),page_id))
            count += 1
        self.put_setting("notion_sync_at", now())
        self.connections.write("notion:health", {"verified": True, "error": "", "checked_at": now()})
        return count

    def update_notion(self, page_id, props):
        if not page_id:
            raise ConnectionError("Publish this local draft before linking a document or changing its live status.")
        # Confirm existing source properties before issuing a write.
        existing = properties(self.connections.call("notion", "notion-fetch", {"id": page_id}))
        if not all(k in existing for k in props):
            raise ConnectionError("Notion page is missing expected properties. Nothing was changed.")
        self.connections.call("notion", "notion-update-page", {"page_id": page_id, "command": "update_properties", "properties": props})
        actual = properties(self.connections.call("notion", "notion-fetch", {"id": page_id}))
        if any(scalar(actual.get(k)) != v for k, v in props.items()):
            raise ConnectionError("Notion write could not be verified. Refresh the source before retrying.")

    def create_notion(self, partner):
        if partner["source"] == "demo":
            raise ConnectionError("Demo records cannot be published to Notion.")
        # Verify query access to the selected source before creating a live record.
        incoming = self.notion_rows()
        duplicates = [r for r in incoming if str(scalar(r.get("Partner", r.get("properties", {}).get("Partner", "")))).casefold() == partner["name"].casefold()]
        if duplicates:
            raise ConnectionError("A live partner with this name already exists. Refresh and use that record.")
        props = {"Partner": partner["name"], "Queue": partner["queue"], "Contract stage": partner["stage"], "Notes": partner["notes"]}
        if partner.get("contract_url"):
            props["Contract link"] = partner["contract_url"]
        # Record the attempt before dispatch; an uncertain create must not be replayed.
        self.execute("UPDATE partners SET sync_state='publishing' WHERE id=?", (partner["id"],))
        try:
            result = self.connections.call("notion", "notion-create-pages", {"parent": {"data_source_id": notion_id(self.source())}, "pages": [{"properties": props}], "allow_async": False})
        except ConnectionError as exc:
            if not isinstance(exc, WriteUncertainError):
                self.execute("UPDATE partners SET sync_state='pending' WHERE id=?",(partner['id'],))
            raise
        pages = result.get("pages") if isinstance(result, dict) else None
        if not pages or len(pages) != 1:
            raise ConnectionError("Create completion is uncertain. Inspect Notion before retrying.")
        page_id = notion_id(pages[0].get("id") or pages[0].get("url"))
        actual = properties(self.connections.call("notion", "notion-fetch", {"id": page_id}))
        if actual.get("Partner") != partner["name"]:
            raise ConnectionError("Created partner could not be verified. Inspect Notion before retrying.")
        self.execute("UPDATE partners SET notion_id=?,notion_url=?,source='notion',sync_state='live',pending_properties='{}',notion_source_id=? WHERE id=?", (page_id, pages[0].get("url") or "https://www.notion.so/" + page_id.replace("-", ""), self.source(), partner["id"]))
        self.log(partner["id"], "Published", "Local partner created and verified in Notion through MCP")

    def publish_pending(self):
        out = []
        for partner in self.rows("SELECT * FROM partners WHERE source!='demo' AND sync_state='pending' AND (notion_id IS NULL OR notion_source_id=?)",(self.source(),)):
            try:
                if partner["notion_id"]:
                    props = json.loads(partner.get("pending_properties") or "{}")
                    if not props:
                        raise ConnectionError("No pending fields recorded. Inspect this legacy draft before publishing.")
                    self.update_notion(partner["notion_id"], props)
                    self.execute("UPDATE partners SET sync_state='live',pending_properties='{}' WHERE id=?", (partner["id"],))
                else:
                    self.create_notion(partner)
                out.append({"partner": partner["name"], "success": True})
            except Exception as exc:
                out.append({"partner": partner["name"], "success": False, "error": str(exc)})
        return out

    def verify_folder(self, upload=False):
        folder_id = self.folder(upload)
        if not folder_id:
            raise ConnectionError("Choose your existing Drive folder in Settings before syncing or uploading.")
        value = self.connections.call("google", "get_file_metadata", {"fileId": folder_id})
        if not isinstance(value, dict) or value.get("mimeType") != "application/vnd.google-apps.folder":
            raise ConnectionError("The chosen Drive destination is not an accessible folder.")
        if upload and not value.get("canAddChildren"):
            raise ConnectionError("Your Google account cannot upload into the selected folder. Grant access to this folder and reconnect if necessary.")
        return value

    def sync_drive(self):
        self.verify_folder(False)
        self.verify_folder(True)
        todo, visited, collected = [self.folder(False)], set(), []
        while todo:
            if len(visited) >= 100:
                raise ConnectionError("Folder scan exceeded 100 folders. Choose a narrower contract folder.")
            folder = todo.pop(0)
            if folder in visited:
                continue
            visited.add(folder)
            cursor, seen = None, set()
            while True:
                args = {"query": "parentId = '" + folder + "'", "pageSize": 100, "excludeContentSnippets": True}
                if cursor:
                    args["pageToken"] = cursor
                result = self.connections.call("google", "search_files", args)
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
        count = 0
        for item in collected:
            name = item.get("title", "")
            if not name.lower().endswith((".pdf", ".docx", ".doc", ".xlsx")) and item.get("mimeType") != "application/vnd.google-apps.document":
                continue
            if self.one("SELECT id FROM files WHERE drive_id=?", (item["id"],)):
                continue
            matches = self.rows("SELECT * FROM partners WHERE source!='demo' AND LOWER(?) LIKE LOWER(name)||'%' ORDER BY LENGTH(name) DESC LIMIT 1", (name,))
            pid = matches[0]["id"] if matches else None
            self.execute("INSERT INTO files(id,partner_id,name,hash,drive_id,drive_url,path,document_type,source,sync_state,created,analysis) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (str(uuid.uuid4()), pid, name, "", item["id"], item.get("viewUrl") or "https://drive.google.com/file/d/" + item["id"] + "/view", "", "Historical contract", "drive", "indexed", item.get("modifiedTime") or now(), ""))
            count += 1
        self.put_setting("drive_sync_at", now())
        self.connections.write("google:health", {"verified": True, "error": "", "checked_at": now()})
        return count

    def upload_drive(self, name, raw, mime):
        self.verify_folder(True)
        result = self.connections.call("google", "create_file", {"title": name, "contentMimeType": mime, "base64Content": base64.b64encode(raw).decode(), "parentId": self.folder(True), "disableConversionToGoogleType": True})
        if not isinstance(result, dict) or not result.get("id"):
            raise ConnectionError("Upload completion is uncertain. Inspect the Drive folder before retrying.")
        return {"id": result["id"], "name": result.get("title", name), "webViewLink": result.get("viewUrl") or "https://drive.google.com/file/d/" + result["id"] + "/view"}

    def file_bytes(self, file_id):
        metadata = self.connections.call("google", "get_file_metadata", {"fileId": file_id})
        args = {"fileId": file_id}
        if metadata.get("mimeType") == "application/vnd.google-apps.document":
            args["exportMimeType"] = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        result = self.connections.call("google", "download_file_content", args)
        if not isinstance(result, dict) or not isinstance(result.get("content"), str):
            raise ConnectionError("Drive did not return downloadable template content.")
        return base64.b64decode(result["content"], validate=True)

"""Art Restyler app: local server + UI window. Run: python server.py"""
import argparse
import json
import mimetypes
import os
import socket
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import engine
import scanner

PORT = int(scanner.CONFIG.get("port", 8765))
URL = f"http://127.0.0.1:{PORT}/"
WEB_DIR = os.path.join(scanner.TOOL_DIR, "web")
EDITABLE = ("category", "subcategory", "mode", "notes", "custom_prompt")
MODES = ("regenerate", "part_sheet", "recolor", "skip")
APP_BROWSERS = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
)

scan_state = {"running": False, "log": []}
server = None


def say(text):
    if sys.stdout:  # no console when started from the desktop shortcut
        print(text, flush=True)


def run_scan():
    scan_state.update(running=True, log=[])
    try:
        scanner.scan(log=scan_state["log"].append)
    except Exception as e:  # surfaced in the UI
        scan_state["log"].append(f"Scan failed: {e}")
    finally:
        scan_state["running"] = False


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_json(self, data, status=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, cache=False, download=None):
        if not os.path.isfile(path):
            return self.send_error(404)
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type",
                         mimetypes.guess_type(path)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=3600" if cache else "no-store")
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{download}"')
        self.end_headers()
        self.wfile.write(body)

    def local_request(self):
        # Reject DNS rebinding and cross-origin browser requests to the local API.
        allowed = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
        if self.headers.get("Host") not in allowed:
            self.send_error(403, "Local host required")
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://{host}" for host in allowed}:
            self.send_error(403, "Cross-origin requests are not allowed")
            return False
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            self.send_error(403, "Cross-site requests are not allowed")
            return False
        return True

    def do_GET(self):
        if not self.local_request():
            return
        path = urlparse(self.path).path
        parts = path.strip("/").split("/")
        if path == "/":
            return self.send_file(os.path.join(WEB_DIR, "index.html"))
        if path == "/api/assets":
            db = engine.connect()
            rows = [dict(r) for r in db.execute(
                "SELECT * FROM assets WHERE missing=0 ORDER BY name COLLATE NOCASE")]
            db.close()
            return self.send_json({
                "assets": rows, "project": scanner.PROJECT_ROOT,
                "settings": engine.public_settings(), "inbox": engine.INBOX_DIR,
                "sources": engine.available_sources()})
        if parts[:2] == ["api", "asset"] and len(parts) == 3:
            guid = parts[2]
            db = engine.connect()
            slices = [dict(r) for r in db.execute(
                "SELECT * FROM slices WHERE texture_guid=? ORDER BY name", (guid,))]
            usages = [dict(r) for r in db.execute(
                "SELECT kind, file FROM usages WHERE texture_guid=? ORDER BY kind, file",
                (guid,))]
            attempts = [dict(r) for r in db.execute(
                "SELECT * FROM attempts WHERE guid=? ORDER BY n DESC", (guid,))]
            for a in attempts:
                a["checks"] = json.loads(a["checks"])
            db.close()
            return self.send_json({"slices": slices, "usages": usages,
                                   "attempts": attempts})
        if path == "/api/manual/batch":
            from urllib.parse import parse_qs
            limit = int((parse_qs(urlparse(self.path).query).get("limit") or ["0"])[0]) or None
            db = engine.connect()
            text, n = engine.batch_prompt(db, limit)
            db.close()
            return self.send_json({"prompt": text, "count": n})
        if parts[:2] == ["api", "manual"] and len(parts) == 3:
            db = engine.connect()
            asset = engine.get_asset(db, parts[2])
            if not asset:
                db.close()
                return self.send_error(404)
            engine.prepare_input(asset)
            prompt = engine.current_prompt(db, asset)
            db.close()
            engine.state["current_manual"] = parts[2]
            return self.send_json({"prompt": prompt})
        if parts[0] == "img" and len(parts) == 2:
            db = engine.connect()
            asset = engine.get_asset(db, parts[1])
            db.close()
            if not asset:
                return self.send_error(404)
            return self.send_file(engine.original_path(asset), cache=True)
        if parts[0] == "work" and len(parts) == 3:
            guid, name = parts[1], os.path.basename(parts[2])
            if not (len(guid) == 32 and guid.isalnum()):
                return self.send_error(404)
            download = None
            if name == "input.png":
                db = engine.connect()
                asset = engine.get_asset(db, guid)
                db.close()
                if not asset:
                    return self.send_error(404)
                engine.prepare_input(asset)
                if "download" in self.path:
                    download = f"{asset['name']}_input.png"
            return self.send_file(os.path.join(engine.WORK_DIR, guid, name),
                                  download=download)
        if path == "/api/scan":
            return self.send_json(scan_state)
        if path == "/api/progress":
            data = engine.progress()
            data["inbox"] = engine.state["inbox_log"][-1:]
            return self.send_json(data)
        self.send_error(404)

    def do_POST(self):
        if not self.local_request():
            return
        path = urlparse(self.path).path
        parts = path.strip("/").split("/")
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            if parts[:2] == ["api", "import"] and len(parts) == 3:
                return self.send_json(engine.import_manual(parts[2], raw))
            body = json.loads(raw or b"{}")
            if path == "/api/update":
                return self.update_assets(body)
            if path == "/api/queue":
                n = engine.queue(body.get("guids", []), body.get("source", ""))
                return self.send_json({"queued": n})
            if path == "/api/review":
                engine.review(body["guid"], body["verdict"], body.get("reason", ""))
                return self.send_json({"ok": True})
            if path == "/api/apply":
                return self.send_json(engine.apply(body.get("guids")))
            if path == "/api/rollback":
                return self.send_json(engine.rollback(body.get("guids")))
            if path == "/api/settings":
                engine.save_settings(body)
                return self.send_json(engine.public_settings())
            if path == "/api/settings/test-key":
                try:
                    return self.send_json(engine.check_openai_key(body.get("key") or None))
                except engine.ProviderError as e:
                    return self.send_json({"ok": False, "error": str(e)})
            if path == "/api/manual/close":
                engine.state["current_manual"] = None
                return self.send_json({"ok": True})
            if path == "/api/scan":
                if not scan_state["running"]:
                    threading.Thread(target=run_scan, daemon=True).start()
                return self.send_json({"started": True})
            if path == "/api/quit":
                self.send_json({"ok": True})
                threading.Thread(target=stop_process, daemon=True).start()
                return
        except (ValueError, KeyError, OSError) as e:
            return self.send_json({"error": str(e) or type(e).__name__}, 400)
        self.send_error(404)

    def update_assets(self, body):
        guids = [g for g in body.get("guids", []) if isinstance(g, str)]
        fields = {k: str(v) for k, v in body.get("fields", {}).items() if k in EDITABLE}
        if "mode" in fields and fields["mode"] not in MODES:
            return self.send_json({"error": "bad mode"}, 400)
        if not guids or not fields:
            return self.send_json({"error": "nothing to update"}, 400)
        sets = ", ".join(f"{k}=?" for k in fields)
        if set(fields) - {"notes", "custom_prompt"}:
            sets += ", manual=1"
        db = engine.connect()
        db.executemany(f"UPDATE assets SET {sets} WHERE guid=?",
                       [(*fields.values(), g) for g in guids])
        db.commit()
        db.close()
        return self.send_json({"updated": len(guids)})


def open_window():
    """Opens the UI as its own app window when Edge/Chrome is available."""
    if "--no-browser" in sys.argv:
        return
    for exe in APP_BROWSERS:
        if os.path.exists(exe):
            subprocess.Popen([exe, f"--app={URL}", "--window-size=1500,950"])
            return
    webbrowser.open(URL)


def stop_process():
    """Quit for real: shutting the HTTP loop down is not enough on Windows - the old
    process kept living and a relaunch bound the same port next to it (SO_REUSEADDR),
    so several servers raced over the inbox with different code."""
    server.shutdown()
    os._exit(0)


class Server(ThreadingHTTPServer):
    allow_reuse_address = False  # a second instance must fail to bind, never share the port


def already_running():
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def main():
    global server
    parser = argparse.ArgumentParser(description="Local Unity art restyling workbench")
    parser.add_argument("--no-browser", action="store_true", help="Do not open a browser window")
    parser.parse_args()
    try:
        scanner.validate_project()
    except ValueError as exc:
        parser.error(str(exc))
    if already_running():
        parser.error(f"Port {PORT} is in use. Stop the other server or change project.json.")
    if not os.path.exists(scanner.DB_PATH):
        say("First run: scanning the project (about a minute)...")
        scanner.scan(log=say)
    try:
        server = Server(("127.0.0.1", PORT), Handler)
    except OSError as exc:
        parser.error(f"Cannot bind port {PORT}: {exc}")
    engine.start_background()
    say(f"Art Restyler running at {URL}  (Ctrl+C to stop)")
    open_window()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    os._exit(0)


if __name__ == "__main__":
    main()

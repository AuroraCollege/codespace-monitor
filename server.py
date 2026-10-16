#!/usr/bin/env python3
"""Small in-memory relay and teacher dashboard server."""

from __future__ import annotations

import json
import os
import posixpath
import queue
import re
import threading
import time
from datetime import datetime, timezone
from fnmatch import fnmatch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, urlparse


MAX_REQUEST_BYTES = 8 * 1024 * 1024
MAX_FILE_BYTES = 200 * 1024
MAX_FILES_PER_STUDENT = 300
MAX_TOTAL_BYTES = 6 * 1024 * 1024
RATE_LIMIT_REQUESTS = 180
RATE_LIMIT_WINDOW = 60
ONLINE_SECONDS = 45
IDLE_SECONDS = 120
SENSITIVE_NAMES = (
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "*.crt", "*.cer",
    "*credentials*", "*secret*", "id_rsa*", "id_ed25519*",
)
EXCLUDED_DIRS = {
    ".git", ".devcontainer", ".ssh", ".aws", ".config", "node_modules", "venv",
    ".venv", "__pycache__", "dist", "build", "target", ".next", ".pytest_cache",
    "coverage", "vendor",
}

students: dict[str, dict] = {}
request_times: dict[str, list[float]] = {}
subscribers: set[queue.Queue] = set()
state_lock = threading.RLock()
subscriber_lock = threading.Lock()
ROOT = Path(__file__).resolve().parent


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_sensitive(path: str) -> bool:
    name = PurePosixPath(path).name.lower()
    return any(fnmatch(name, pattern) for pattern in SENSITIVE_NAMES)


def valid_path(value: object) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 1024 or "\x00" in value or "\\" in value:
        return None
    if re.match(r"^[A-Za-z]:", value):
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        return None
    normalized = posixpath.normpath(value)
    if normalized != value or any(part in EXCLUDED_DIRS for part in path.parts) or is_sensitive(value):
        return None
    return value


def public_student(username: str, student: dict, include_files: bool = True) -> dict:
    now = time.time()
    age = now - student["last_heartbeat"]
    status = "offline" if age > ONLINE_SECONDS else "idle" if now - student["last_change_epoch"] > IDLE_SECONDS else "online"
    result = {
        "username": username,
        "name": student["name"],
        "status": status,
        "last_heartbeat": student.get("last_heartbeat_iso"),
        "last_change": student.get("last_change"),
        "last_change_epoch": student.get("last_change_epoch"),
    }
    if include_files:
        result["files"] = student["files"]
    return result


def publish(event: dict) -> None:
    with subscriber_lock:
        for subscriber in tuple(subscribers):
            try:
                subscriber.put_nowait(event)
            except queue.Full:
                try:
                    subscriber.get_nowait()
                    subscriber.put_nowait(event)
                except (queue.Empty, queue.Full):
                    pass


class MonitorHandler(BaseHTTPRequestHandler):
    server_version = "StudentMonitor/1.0"

    def log_message(self, format: str, *args: object) -> None:
        if os.environ.get("MONITOR_ACCESS_LOG") == "1":
            super().log_message(format, *args)

    def authorized_teacher(self, query: dict[str, list[str]]) -> bool:
        key = os.environ.get("TEACHER_KEY", "")
        return bool(key) and query.get("key", [""])[0] == key

    def send_json(self, status: int, body: object) -> None:
        payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:
        if self.path.split("?", 1)[0] != "/api/sync":
            self.send_error(404)
            return
        class_code = os.environ.get("CLASS_CODE", "")
        if not class_code or self.headers.get("X-Class-Code", "") != class_code:
            self.send_json(401, {"error": "invalid class code"})
            return
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            length = -1
        if length < 0 or length > MAX_REQUEST_BYTES:
            self.send_json(413, {"error": "request size is invalid or too large"})
            return
        try:
            payload = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.send_json(400, {"error": "invalid JSON"})
            return
        if not isinstance(payload, dict):
            self.send_json(400, {"error": "expected an object"})
            return
        username = payload.get("username")
        action = payload.get("action")
        if not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,39}", username):
            self.send_json(400, {"error": "invalid username"})
            return
        if action not in {"snapshot", "update", "heartbeat"}:
            self.send_json(400, {"error": "invalid action"})
            return
        incoming = payload.get("files", [])
        deleted = payload.get("deleted", [])
        if not isinstance(incoming, list) or not isinstance(deleted, list):
            self.send_json(400, {"error": "files and deleted must be lists"})
            return

        changed_paths: list[str] = []
        changed_files: dict[str, dict] = {}
        event: dict
        with state_lock:
            cutoff = time.time() - RATE_LIMIT_WINDOW
            recent_requests = [timestamp for timestamp in request_times.get(username, []) if timestamp > cutoff]
            if len(recent_requests) >= RATE_LIMIT_REQUESTS:
                request_times[username] = recent_requests
                self.send_json(429, {"error": "student request rate exceeded"})
                return
            recent_requests.append(time.time())
            request_times[username] = recent_requests
            student = students.get(username)
            if student is None:
                student = {"name": username, "files": {}, "last_change": None, "last_change_epoch": time.time(), "last_heartbeat": 0}
                students[username] = student
            display_name = payload.get("name")
            student["name"] = display_name[:80] if isinstance(display_name, str) and display_name.strip() else username

            if action in {"snapshot", "update"}:
                if action == "snapshot":
                    student["files"] = {}
                total_bytes = sum(len(file["content"].encode("utf-8")) for file in student["files"].values())
                for item in incoming:
                    if not isinstance(item, dict):
                        continue
                    path = valid_path(item.get("path"))
                    content = item.get("content")
                    if path is None or not isinstance(content, str) or "\x00" in content or len(content.encode("utf-8")) > MAX_FILE_BYTES:
                        continue
                    existing_bytes = len(student["files"].get(path, {}).get("content", "").encode("utf-8"))
                    next_total = total_bytes - existing_bytes + len(content.encode("utf-8"))
                    if (len(student["files"]) >= MAX_FILES_PER_STUDENT and path not in student["files"]) or next_total > MAX_TOTAL_BYTES:
                        continue
                    changed_paths.append(path)
                    student["files"][path] = {"content": content, "updated_at": now_iso()}
                    changed_files[path] = student["files"][path]
                    total_bytes = next_total
                for deleted_path in deleted:
                    path = valid_path(deleted_path)
                    if path and path in student["files"]:
                        del student["files"][path]
                        changed_paths.append(path)
                if changed_paths or action == "snapshot":
                    student["last_change"] = now_iso()
                    student["last_change_epoch"] = time.time()

            student["last_heartbeat"] = time.time()
            student["last_heartbeat_iso"] = now_iso()
            event = {
                "type": "student",
                "student": public_student(username, student, include_files=False),
                "files": dict(student["files"]) if action == "snapshot" else dict(changed_files),
                "snapshot": action == "snapshot",
                "deleted": [path for path in changed_paths if path not in changed_files],
                "changed": list(changed_paths),
            }
        publish(event)
        self.send_json(200, {"ok": True})

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if not self.authorized_teacher(query):
            self.send_error(403, "teacher key required")
            return
        if parsed.path == "/api/students":
            with state_lock:
                response = [public_student(username, student) for username, student in students.items()]
            self.send_json(200, response)
            return
        if parsed.path == "/api/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            event_queue: queue.Queue = queue.Queue(maxsize=256)
            with subscriber_lock:
                subscribers.add(event_queue)
                with state_lock:
                    for username, student in students.items():
                        event_queue.put_nowait({
                            "type": "student",
                            "student": public_student(username, student, include_files=False),
                            "files": student["files"],
                            "snapshot": True,
                            "deleted": [],
                            "changed": list(student["files"]),
                        })
            try:
                self.wfile.write(b": connected\n\n")
                self.wfile.flush()
                while True:
                    try:
                        event = event_queue.get(timeout=15)
                        data = json.dumps(event, separators=(",", ":"))
                        self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                with subscriber_lock:
                    subscribers.discard(event_queue)
            return
        if parsed.path in {"/", "/index.html", "/app.js", "/style.css"}:
            files = {
                "/": ("dashboard/index.html", "text/html; charset=utf-8"),
                "/index.html": ("dashboard/index.html", "text/html; charset=utf-8"),
                "/app.js": ("dashboard/app.js", "text/javascript; charset=utf-8"),
                "/style.css": ("dashboard/style.css", "text/css; charset=utf-8"),
            }
            relative, content_type = files[parsed.path]
            try:
                with (ROOT / relative).open("rb") as source:
                    body = source.read()
            except OSError:
                self.send_error(500, "dashboard assets are missing")
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    if not os.environ.get("TEACHER_KEY"): 
        raise SystemExit("Set TEACHER_KEY before starting the server.")
    if os.environ.get("CLASS_CODE"):
            raise SystemExit("Set CLASS_CODE before starting the server.")
    print(f"Student Monitor listening on 0.0.0.0:{port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), MonitorHandler).serve_forever()
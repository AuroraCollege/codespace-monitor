#!/usr/bin/env python3
"""Poll a student workspace and send safe text-file updates to the monitor."""

from __future__ import annotations

import fnmatch
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path


# Keep file-sharing policy centralized here so templates can review it easily.
EXCLUDED_DIRS = {
    ".git", ".devcontainer", ".ssh", ".aws", ".config", "node_modules", "venv",
    ".venv", "__pycache__", "dist", "build", "target", ".next", ".pytest_cache",
    "coverage", "vendor",
}
SENSITIVE_PATTERNS = (
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "*.crt", "*.cer",
    "*credentials*", "*secret*", "id_rsa*", "id_ed25519*",
)
ALLOWED_EXTENSIONS = {
    ".py", ".pyw", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".vue", ".svelte",
    ".html", ".htm", ".css", ".scss", ".sass", ".less", ".json", ".jsonc", ".md",
    ".markdown", ".txt", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".xml", ".sql",
    ".sh", ".bash", ".zsh", ".java", ".kt", ".kts", ".c", ".h", ".cc", ".cpp",
    ".hpp", ".cs", ".go", ".rs", ".rb", ".php", ".swift", ".r", ".R", ".dockerfile",
    ".makefile", ".properties", ".graphql", ".gql", ".ipynb",
}
ALLOWED_NAMES = {
    "Dockerfile", "Makefile", "GNUmakefile", "Procfile", "Justfile", "Gemfile",
    "Rakefile", "Cargo.lock", "go.mod", "go.sum", "requirements.txt", "Pipfile",
    "Pipfile.lock", "CMakeLists.txt", ".gitignore", ".gitattributes", ".editorconfig",
}
MAX_FILE_BYTES = 200 * 1024
MAX_FILES = 300
MAX_TOTAL_BYTES = 3 * 1024 * 1024
POLL_SECONDS = 0.5
DEBOUNCE_SECONDS = 1.0
HEARTBEAT_SECONDS = 15
DISCOVERY_URL = os.environ.get(
    "SERVER_DISCOVERY_URL",
    "https://raw.githubusercontent.com/OWNER/monitor-server/main/server.json",
)
CACHE_PATH = Path.home() / ".cache" / "student-monitor" / "server-url"


def read_gitignore(root: Path) -> list[str]:
    try:
        lines = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]


def ignored_by_gitignore(relative: str, rules: list[str]) -> bool:
    ignored = False
    for raw_rule in rules:
        negate = raw_rule.startswith("!")
        rule = raw_rule[1:] if negate else raw_rule
        if not rule:
            continue
        directory_rule = rule.endswith("/")
        rule = rule.rstrip("/")
        anchored = rule.startswith("/")
        rule = rule.lstrip("/")
        if "/" in rule or anchored:
            matched = fnmatch.fnmatchcase(relative, rule) or relative.startswith(rule + "/")
        else:
            matched = any(fnmatch.fnmatchcase(part, rule) for part in relative.split("/"))
        if directory_rule and matched:
            matched = True
        if matched:
            ignored = not negate
    return ignored


def allowed_file(relative: str, rules: list[str]) -> bool:
    path = Path(relative)
    if any(part in EXCLUDED_DIRS for part in path.parts):
        return False
    name = path.name.lower()
    if any(fnmatch.fnmatchcase(name, pattern.lower()) for pattern in SENSITIVE_PATTERNS):
        return False
    if ignored_by_gitignore(relative, rules):
        return False
    return path.name in ALLOWED_NAMES or path.suffix.lower() in ALLOWED_EXTENSIONS


def scan_workspace(root: Path) -> dict[str, tuple[int, int, str]]:
    rules = read_gitignore(root)
    found: dict[str, tuple[int, int, str]] = {}
    total_bytes = 0
    for current, directories, filenames in os.walk(root):
        directories[:] = sorted(name for name in directories if name not in EXCLUDED_DIRS)
        for filename in sorted(filenames):
            full_path = Path(current) / filename
            relative = full_path.relative_to(root).as_posix()
            if not allowed_file(relative, rules) or len(found) >= MAX_FILES:
                continue
            try:
                stat = full_path.stat()
                if stat.st_size > MAX_FILE_BYTES or total_bytes + stat.st_size > MAX_TOTAL_BYTES:
                    continue
                content = full_path.read_bytes()
                if b"\x00" in content:
                    continue
                text = content.decode("utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            found[relative] = (stat.st_mtime_ns, stat.st_size, text)
            total_bytes += stat.st_size
    return found


def discover_url(refresh: bool = False) -> str | None:
    cached = None
    try:
        cached = CACHE_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    if refresh or not cached:
        try:
            request = urllib.request.Request(DISCOVERY_URL, headers={"Cache-Control": "no-cache"})
            with urllib.request.urlopen(request, timeout=3) as response:
                body = json.loads(response.read(16 * 1024).decode("utf-8"))
            url = body.get("url") if isinstance(body, dict) else None
            if isinstance(url, str) and url.startswith("https://"):
                CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
                CACHE_PATH.write_text(url.rstrip("/"), encoding="utf-8")
                return url.rstrip("/")
        except (OSError, ValueError, urllib.error.URLError):
            pass
    return cached if cached and cached.startswith("https://") else None


def request_sync(server_url: str, payload: dict, class_code: str) -> bool:
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        server_url + "/api/sync",
        data=data,
        headers={"Content-Type": "application/json", "X-Class-Code": class_code},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def serialized_files(snapshot: dict[str, tuple[int, int, str]]) -> list[dict[str, str]]:
    return [{"path": path, "content": value[2]} for path, value in snapshot.items()]


def run() -> None:
    root = Path(os.environ.get("WORKSPACE_FOLDER", os.getcwd())).resolve()
    username = os.environ.get("GITHUB_USER", "").strip()
    class_code = os.environ.get("CLASS_CODE", "")
    display_name = os.environ.get("STUDENT_DISPLAY_NAME", "").strip()
    if not username or not class_code:
        print("Student Monitor is not configured: set GITHUB_USER and CLASS_CODE.", flush=True)
        return
    print("Student Monitor is active: saved source files in this workspace are shared with your teacher.", flush=True)
    print("Status: connecting", flush=True)

    current = scan_workspace(root)
    server_url = discover_url(refresh=True)
    next_heartbeat = 0.0
    retry_delay = 1.0
    last_sent: dict[str, tuple[int, int, str]] = {}
    has_synced = False
    change_started: dict[str, float] = {}
    last_status = "offline"

    while True:
        now = time.monotonic()
        try:
            latest = scan_workspace(root)
            changed = {path for path, item in latest.items() if path not in current or item[:2] != current[path][:2]}
            deleted = set(current) - set(latest)
            for path in changed:
                change_started[path] = now
            for path in deleted:
                change_started.setdefault(path, now)
            current = latest

            ready = {path for path in change_started if now - change_started[path] >= DEBOUNCE_SECONDS}
            reconnecting = now >= next_heartbeat
            if reconnecting or ready:
                if reconnecting and not server_url:
                    server_url = discover_url(refresh=True)
                payload_files = [
                    {"path": path, "content": current[path][2]}
                    for path in ready if path in current
                ]
                delete_paths = [path for path in ready if path not in current]
                action = "update" if ready else "heartbeat"
                if not has_synced:
                    action = "snapshot"
                    payload_files = serialized_files(current)
                    delete_paths = []

                sent = bool(server_url) and request_sync(server_url, {
                    "username": username,
                    "name": display_name or username,
                    "action": action,
                    "files": payload_files,
                    "deleted": delete_paths,
                }, class_code)
                if sent:
                    last_sent = current.copy()
                    has_synced = True
                    for path in ready:
                        change_started.pop(path, None)
                    next_heartbeat = now + HEARTBEAT_SECONDS
                    retry_delay = 1.0
                    if last_status != "online":
                        print("Status: syncing", flush=True)
                        last_status = "online"
                else:
                    server_url = discover_url(refresh=True)
                    next_heartbeat = now + retry_delay
                    retry_delay = min(retry_delay * 2, 30.0)
                    if last_status != "offline":
                        print("Status: offline; retrying", flush=True)
                        last_status = "offline"
                    if has_synced:
                        has_synced = False
                        last_sent = {}
                        change_started.update({path: now - DEBOUNCE_SECONDS for path in current})
        except Exception as error:
            print(f"Student Monitor sync error: {type(error).__name__}", flush=True)
            next_heartbeat = time.monotonic() + retry_delay
            retry_delay = min(retry_delay * 2, 30.0)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    run()
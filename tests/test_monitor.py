import importlib.util
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

import server


AGENT_PATH = Path(__file__).resolve().parents[1] / "student-template" / "agent.py"
AGENT_SPEC = importlib.util.spec_from_file_location("student_agent", AGENT_PATH)
agent = importlib.util.module_from_spec(AGENT_SPEC)
AGENT_SPEC.loader.exec_module(agent)


class MonitorServerTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict("os.environ", {"CLASS_CODE": "class-test", "TEACHER_KEY": "teacher-test"})
        self.environment.start()
        server.students.clear()
        server.request_times.clear()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.MonitorHandler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.httpd.server_port}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        self.environment.stop()

    def request(self, path, payload=None, class_code=None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json"} if data is not None else {}
        if class_code is not None:
            headers["X-Class-Code"] = class_code
        request = Request(self.base_url + path, data=data, headers=headers)
        try:
            with urlopen(request, timeout=2) as response:
                return response.status, response.read()
        except HTTPError as error:
            status, body = error.code, error.read()
            error.close()
            return status, body

    def test_teacher_api_requires_key(self):
        self.assertEqual(self.request("/api/students")[0], 403)
        status, body = self.request("/api/students?key=teacher-test")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), [])

    def test_class_code_and_unsafe_paths_are_rejected(self):
        payload = {
            "username": "student_1",
            "action": "snapshot",
            "files": [
                {"path": "src/main.py", "content": "print('ok')"},
                {"path": ".env.local", "content": "TOKEN=secret"},
                {"path": "../outside.py", "content": "outside"},
                {"path": "C:/outside.py", "content": "outside"},
                {"path": "node_modules/pkg/index.js", "content": "ignored"},
            ],
        }
        self.assertEqual(self.request("/api/sync", payload)[0], 401)
        self.assertEqual(self.request("/api/sync", payload, "class-test")[0], 200)
        self.assertEqual(set(server.students["student_1"]["files"]), {"src/main.py"})

    def test_update_deletes_files(self):
        snapshot = {"username": "learner", "action": "snapshot", "files": [{"path": "main.py", "content": "one"}]}
        update = {"username": "learner", "action": "update", "files": [], "deleted": ["main.py"]}
        self.assertEqual(self.request("/api/sync", snapshot, "class-test")[0], 200)
        self.assertEqual(self.request("/api/sync", update, "class-test")[0], 200)
        self.assertEqual(server.students["learner"]["files"], {})

    def test_student_rate_limit(self):
        heartbeat = {"username": "learner", "action": "heartbeat"}
        with patch.object(server, "RATE_LIMIT_REQUESTS", 1):
            self.assertEqual(self.request("/api/sync", heartbeat, "class-test")[0], 200)
            self.assertEqual(self.request("/api/sync", heartbeat, "class-test")[0], 429)

    def test_sse_sends_snapshot_then_file_delta(self):
        stream = urlopen(self.base_url + "/api/events?key=teacher-test", timeout=2)
        self.addCleanup(stream.close)
        self.assertEqual(stream.readline(), b": connected\n")
        self.assertEqual(stream.readline(), b"\n")

        def read_event():
            while True:
                line = stream.readline()
                if line.startswith(b"data: "):
                    return json.loads(line.decode().removeprefix("data: "))

        snapshot = {"username": "learner", "action": "snapshot", "files": [{"path": "main.py", "content": "one"}]}
        self.assertEqual(self.request("/api/sync", snapshot, "class-test")[0], 200)
        first_event = read_event()
        self.assertTrue(first_event["snapshot"])
        self.assertEqual(first_event["files"]["main.py"]["content"], "one")

        update = {"username": "learner", "action": "update", "files": [{"path": "main.py", "content": "two"}], "deleted": []}
        self.assertEqual(self.request("/api/sync", update, "class-test")[0], 200)
        second_event = read_event()
        self.assertFalse(second_event["snapshot"])
        self.assertEqual(set(second_event["files"]), {"main.py"})
        self.assertEqual(second_event["files"]["main.py"]["content"], "two")


class AgentFilterTests(unittest.TestCase):
    def test_scan_keeps_safe_text_and_skips_excluded_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".gitignore").write_text("ignored.py\nprivate/\n", encoding="utf-8")
            (root / "main.py").write_text("print('safe')", encoding="utf-8")
            (root / "ignored.py").write_text("ignored", encoding="utf-8")
            (root / ".env.local").write_text("TOKEN=secret", encoding="utf-8")
            (root / "api-secret.txt").write_text("secret", encoding="utf-8")
            (root / "binary.py").write_bytes(b"\x00\x01")
            (root / "large.py").write_text("x" * (agent.MAX_FILE_BYTES + 1), encoding="utf-8")
            (root / "private").mkdir()
            (root / "private" / "notes.py").write_text("ignored", encoding="utf-8")
            (root / "node_modules").mkdir()
            (root / "node_modules" / "package.json").write_text("{}", encoding="utf-8")

            files = agent.scan_workspace(root)

        self.assertEqual(set(files), {".gitignore", "main.py"})


if __name__ == "__main__":
    unittest.main()
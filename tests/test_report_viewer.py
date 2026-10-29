"""End-to-end tests: run report_viewer.py as a server and as the agent/comments/inbox-watch commands, in temporary
folders. Standard library only:  python -m unittest discover -s tests -v
Read-aloud is tested only if setup-readaloud.sh has been run (it needs the Piper voice)."""
import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "report_viewer.py"
POSIX = os.name == "posix"           # agent wake-up needs fcntl
TTS = (ROOT / "readaloud" / ".venv").is_dir() and (ROOT / "readaloud" / "voices" / "en_US-lessac-medium.onnx").is_file()

# A one-page PDF with the text "Hello report viewer" (enough for PDF.js and PyMuPDF).
def make_pdf(text="Hello report viewer"):
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>",
            None,
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    stream = f"BT /F1 18 Tf 20 100 Td ({text}) Tj ET".encode()
    objs[3] = b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % off for off in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run(*args, env=None, stdin=None, check=True, timeout=60):
    p = subprocess.run([sys.executable, str(SCRIPT), *args], input=stdin, capture_output=True, text=True,
                       env={**os.environ, **(env or {})}, timeout=timeout)
    if check and p.returncode != 0:
        raise AssertionError(f"{args} exited {p.returncode}: {p.stderr}")
    return p


class ServerCase(unittest.TestCase):
    """Starts one server for the class, with a command-line folder and one registered agent."""
    extra_env = {}
    extra_args = []

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="rv-test-")).resolve()
        cls.data = cls.tmp / "data"
        cls.papers = cls.tmp / "papers"
        (cls.papers / "sub").mkdir(parents=True)
        (cls.papers / ".hidden").mkdir()
        (cls.papers / "first_paper.pdf").write_bytes(make_pdf())
        (cls.papers / "sub" / "second.pdf").write_bytes(make_pdf("Second"))
        (cls.papers / ".hidden" / "secret.pdf").write_bytes(make_pdf("Hidden"))
        cls.agent_dir = cls.tmp / "agent-out"
        cls.agent_dir.mkdir()
        (cls.agent_dir / "agent_report.pdf").write_bytes(make_pdf("Agent"))
        cls.sid = str(uuid.uuid4())
        cls.env = {"REPORT_VIEWER_DATA": str(cls.data), "CLAUDE_CODE_SESSION_ID": "", "REPORT_VIEWER_PASSWORD": "",
                   **cls.extra_env}
        run("agent", "register", "Thesis Helper", "--folder", str(cls.agent_dir), "--session", cls.sid, env=cls.env)
        cls.port = free_port()
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.proc = subprocess.Popen([sys.executable, str(SCRIPT), "--port", str(cls.port), f"Papers={cls.papers}",
                                     *cls.extra_args], env={**os.environ, **cls.env},
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        cls.proc.stdout.readline()          # the start-up line, printed once the socket is bound
        if cls.proc.poll() is not None:
            raise RuntimeError(f"server didn't start: {cls.proc.stdout.read()}")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(10)
        cls.proc.stdout.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def request(self, path, data=None, headers=None, method=None):
        """(status, headers, body bytes) for a request; never raises on an HTTP error status."""
        h = {"Host": f"127.0.0.1:{self.port}", **self.auth(), **(headers or {})}
        req = urllib.request.Request(self.base + path, data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def auth(self):
        return {}

    def get_json(self, path):
        code, _, body = self.request(path)
        self.assertEqual(code, 200, body)
        return json.loads(body)

    def token(self):
        _, _, body = self.request("/")
        return body.decode().split("const TOKEN='", 1)[1].split("'", 1)[0]

    def post(self, path, payload, token=None, origin=None, raw=None, ctype="application/json"):
        headers = {"X-Token": self.token() if token is None else token, "Content-Type": ctype,
                   "Origin": origin or self.base}
        data = raw if raw is not None else json.dumps(payload).encode()
        code, _, body = self.request(path, data=data, headers=headers, method="POST")
        return code, json.loads(body) if body.startswith(b"{") else body

    def docs(self):
        return {d["name"]: (s, d) for s in self.get_json("/api/reports")["sections"] for d in s["docs"]}


class TestServer(ServerCase):
    def test_index_sections(self):
        r = self.get_json("/api/reports")
        self.assertEqual([s["label"] for s in r["sections"]], ["Uploaded", "Thesis Helper", "Papers"])
        names = {d["name"]: s["label"] for s in r["sections"] for d in s["docs"]}
        self.assertEqual(names, {"first_paper.pdf": "Papers", "second.pdf": "Papers", "agent_report.pdf": "Thesis Helper"})
        self.assertEqual(r["sections"][1]["agent"], "thesis-helper")
        self.assertEqual(r["readaloud"], TTS)

    def test_pages_and_security_headers(self):
        for path in ("/", "/reports", "/reports/view?id=0000000000000000"):
            code, h, body = self.request(path)
            self.assertEqual(code, 200)
            self.assertIn("text/html", h["Content-Type"])
            self.assertIn("default-src 'self'", h["Content-Security-Policy"])
            self.assertEqual(h["X-Content-Type-Options"], "nosniff")
            self.assertNotIn(b"__TOKEN__", body)

    def test_pdf_and_pdfjs_served(self):
        _, d = self.docs()["first_paper.pdf"]
        code, h, body = self.request(f"/reports/file?id={d['id']}")
        self.assertEqual((code, h["Content-Type"]), (200, "application/pdf"))
        self.assertEqual(body, (self.papers / "first_paper.pdf").read_bytes())
        self.assertEqual(self.request("/reports/file?id=0123456789abcdef")[0], 404)
        self.assertEqual(self.request("/pdfjs/pdf.min.mjs")[0], 200)
        for bad in ("/pdfjs/../report_viewer.py", "/pdfjs/%2e%2e/report_viewer.py", "/pdfjs/LICENSE"):
            self.assertEqual(self.request(bad)[0], 404, bad)

    def test_host_header_checked(self):
        self.assertEqual(self.request("/", headers={"Host": "evil.example:8090"})[0], 403)
        self.assertEqual(self.request("/", headers={"Host": f"localhost:{self.port}"})[0], 200)

    def test_post_needs_token_and_origin(self):
        _, d = self.docs()["first_paper.pdf"]
        body = {"id": d["id"], "op": "add", "text": "x"}
        self.assertEqual(self.post("/api/comments", body, token="wrong")[0], 403)
        self.assertEqual(self.post("/api/comments", body, origin="http://evil.example")[0], 403)

    def test_upload_and_remove(self):
        code, r = self.post("/api/upload?name=My%20Notes.pdf", None, raw=make_pdf("Upload"), ctype="application/pdf")
        self.assertEqual(code, 200, r)
        code, r2 = self.post("/api/upload?name=My%20Notes.pdf", None, raw=make_pdf("Again"), ctype="application/pdf")
        self.assertEqual(code, 200, r2)
        self.assertEqual(r2["message"], "Uploaded My Notes (2).pdf.")
        code, r3 = self.post("/api/upload?name=evil.pdf", None, raw=b"<html>not a pdf</html>", ctype="application/pdf")
        self.assertEqual(code, 415, r3)
        code, r4 = self.post("/api/upload?name=..%2F..%2Fescape.pdf", None, raw=make_pdf(), ctype="application/pdf")
        self.assertEqual((code, r4["message"]), (200, "Uploaded escape.pdf."))
        docs = self.docs()
        self.assertEqual(docs["My Notes.pdf"][0]["label"], "Uploaded")
        self.assertTrue((self.data / "uploads" / "escape.pdf").is_file())
        # Remove moves the file aside; files outside the uploads folder can't be removed.
        self.assertEqual(self.post("/api/upload-remove", {"id": r["id"]})[0], 200)
        self.assertNotIn("My Notes.pdf", self.docs())
        self.assertEqual(len(list((self.data / "uploads" / ".deleted").glob("* My Notes.pdf"))), 1)
        self.assertEqual(self.post("/api/upload-remove", {"id": docs["first_paper.pdf"][1]["id"]})[0], 404)
        self.assertTrue((self.papers / "first_paper.pdf").is_file())

    def test_comment_threads(self):
        _, d = self.docs()["second.pdf"]
        rid = d["id"]
        self.assertEqual(self.post("/api/comments", {"id": rid, "op": "add", "text": ""})[0], 400)
        code, _ = self.post("/api/comments", {"id": rid, "op": "add", "text": "Check this", "page": 1,
                                              "rects": [[0.1, 0.2, 0.3, 0.25]], "quote": "Second"})
        self.assertEqual(code, 200)
        t = self.get_json(f"/api/comments?id={rid}")["threads"][0]
        self.assertEqual((t["page"], t["quote"], t["status"]), (1, "Second", "open"))
        for op in ("reply", "resolve", "reopen"):
            self.assertEqual(self.post("/api/comments", {"id": rid, "op": op, "thread": t["id"], "text": "more"})[0], 200)
        t = self.get_json(f"/api/comments?id={rid}")["threads"][0]
        self.assertEqual((len(t["comments"]), t["status"]), (2, "open"))
        self.assertEqual(self.post("/api/comments", {"id": rid, "op": "delete", "thread": t["id"]})[0], 200)
        self.assertEqual(self.get_json(f"/api/comments?id={rid}")["threads"], [])
        self.assertEqual(self.post("/api/comments", {"id": "nope", "op": "add", "text": "x"})[0], 404)

    def test_ask_and_agent_reply(self):
        _, d = self.docs()["agent_report.pdf"]
        rid = d["id"]
        self.assertEqual(self.post("/api/comments", {"id": rid, "op": "ask", "to": "thesis-helper"})[0], 400)  # none waiting
        self.post("/api/comments", {"id": rid, "op": "add", "text": "Why is the error bar so large?"})
        c = self.get_json(f"/api/comments?id={rid}")
        self.assertEqual(c["owner"], "thesis-helper")
        self.assertEqual(c["choices"], [{"name": "thesis-helper", "label": "Thesis Helper"}])
        self.assertEqual(self.post("/api/comments", {"id": rid, "op": "ask", "to": "nobody"})[0], 400)
        code, r = self.post("/api/comments", {"id": rid, "op": "ask", "to": "thesis-helper"})
        self.assertEqual(code, 200, r)
        ask = self.get_json(f"/api/comments?id={rid}")["asks"][-1]
        self.assertEqual(ask["status"], "manual")          # no hook is listening
        self.assertIn("comments", ask["prompt"])
        # The agent sees the comment and replies from its session.
        env = {**self.env, "CLAUDE_CODE_SESSION_ID": self.sid}
        out = run("comments", env=env).stdout
        self.assertIn("Why is the error bar so large?", out)
        tid = self.get_json(f"/api/comments?id={rid}")["threads"][0]["id"]
        run("comments", "reply", f"{rid}/{tid}", "-", env=env, stdin="It's the 2-sigma interval.\n")
        c = self.get_json(f"/api/comments?id={rid}")
        self.assertEqual(c["threads"][0]["comments"][-1]["by"], "agent")
        self.assertEqual(c["threads"][0]["comments"][-1]["name"], "Thesis Helper")
        self.assertEqual(c["asks"][-1]["status"], "answered")
        self.assertIn("No comments are waiting", run("comments", env=env).stdout)

    @unittest.skipUnless(POSIX, "agent wake-up needs fcntl (macOS or Linux)")
    def test_inbox_watch_wakes_agent(self):
        _, d = self.docs()["first_paper.pdf"]
        rid = d["id"]
        # Start with an empty inbox (another test may have queued a request, which would wake the hook at once).
        (self.data / "inbox" / f"{self.sid}.jsonl").unlink(missing_ok=True)
        hook = subprocess.Popen([sys.executable, str(SCRIPT), "inbox-watch"], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                env={**os.environ, **self.env, "CLAUDE_PID": str(os.getpid())})
        hook.stdin.write(json.dumps({"session_id": self.sid, "hook_event_name": "Stop"}))
        hook.stdin.close()
        try:
            lock = self.data / "inbox" / f"{self.sid}.lock"
            for _ in range(100):            # wait until the watcher holds its lock
                if lock.exists():
                    break
                time.sleep(0.1)
            time.sleep(0.5)
            # A second watcher for the same session exits at once.
            self.assertEqual(run("inbox-watch", env=self.env, stdin=json.dumps({"session_id": self.sid}),
                                 check=False, timeout=15).returncode, 0)
            self.post("/api/comments", {"id": rid, "op": "add", "text": "Wake up"})
            code, r = self.post("/api/comments", {"id": rid, "op": "ask", "to": "thesis-helper"})
            self.assertEqual((code, self.get_json(f"/api/comments?id={rid}")["asks"][-1]["status"]), (200, "sent"))
            self.assertEqual(hook.wait(30), 2)
            err = hook.stderr.read()
            self.assertIn("Ask to respond", err)
            self.assertIn("first paper", err)
            self.assertNotIn("Wake up", err)        # the notice never carries the comment text
            self.assertEqual(self.get_json(f"/api/comments?id={rid}")["asks"][-1]["status"], "delivered")
        finally:
            if hook.poll() is None:
                hook.kill()
            hook.stdout.close()
            hook.stderr.close()

    @unittest.skipUnless(TTS, "read-aloud isn't installed (run setup-readaloud.sh)")
    def test_readaloud(self):
        _, d = self.docs()["first_paper.pdf"]
        w = self.get_json(f"/api/tts/words?id={d['id']}")
        self.assertEqual([x["text"] for x in w["pages"][0]["words"]], ["Hello", "report", "viewer"])
        s = self.get_json(f"/api/tts/sentence?id={d['id']}&page=0&word=0")
        self.assertGreater(len(base64.b64decode(s["audio_wav_b64"])), 1000)
        self.assertEqual(len(s["words"]), 3)

    @unittest.skipIf(TTS, "read-aloud is installed")
    def test_readaloud_missing(self):
        _, d = self.docs()["first_paper.pdf"]
        self.assertEqual(self.request(f"/api/tts/words?id={d['id']}")[0], 503)


class TestPassword(ServerCase):
    extra_env = {"REPORT_VIEWER_PASSWORD": "correct horse"}

    def auth(self):
        return {"Authorization": "Basic " + base64.b64encode(b"anyone:correct horse").decode()}

    def test_password(self):
        self.assertEqual(self.request("/api/reports")[0], 200)
        self.assertEqual(self.request("/api/reports", headers={"Authorization": ""})[0], 401)
        bad = "Basic " + base64.b64encode(b"anyone:wrong").decode()
        self.assertEqual(self.request("/api/reports", headers={"Authorization": bad})[0], 401)


class TestCommands(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rv-test-")).resolve()
        self.env = {"REPORT_VIEWER_DATA": str(self.tmp / "data"), "CLAUDE_CODE_SESSION_ID": "",
                    "REPORT_VIEWER_PASSWORD": ""}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_network_needs_password(self):
        p = run("--host", "0.0.0.0", "--port", str(free_port()), env=self.env, check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("needs a password", p.stderr)

    def test_register_checks(self):
        sid = str(uuid.uuid4())
        self.assertIn("no Claude Code session id", run("agent", "register", "X", env=self.env, check=False).stderr)
        p = run("agent", "register", "X", "--folder", str(Path.home()), "--session", sid, env=self.env, check=False)
        self.assertIn("too broad", p.stderr)
        run("agent", "register", "Lab Notes", "--folder", str(self.tmp), "--session", sid, env=self.env)
        run("agent", "register", "Lab Notes", "--folder", str(self.tmp), "--session", sid, env=self.env)
        self.assertEqual(run("agent", "list", env=self.env).stdout.count("Lab Notes"), 1)
        run("agent", "unregister", "Lab Notes", env=self.env)
        self.assertIn("No agents registered", run("agent", "list", env=self.env).stdout)

    def test_hook_config(self):
        hooks = json.loads(run("agent", "hook-config", env=self.env).stdout)["hooks"]
        for event in ("SessionStart", "Stop"):
            h = hooks[event][0]["hooks"][0]
            self.assertTrue(h["asyncRewake"])
            self.assertIn("inbox-watch", h["command"])
            self.assertIn("--data", h["command"])

    def test_inbox_watch_ignores_unregistered(self):
        p = run("inbox-watch", env=self.env, stdin=json.dumps({"session_id": str(uuid.uuid4())}), check=False)
        self.assertEqual((p.returncode, p.stderr), (0, ""))


if __name__ == "__main__":
    unittest.main()

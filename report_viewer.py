#!/usr/bin/env python3
"""report_viewer: a small web page for reading PDFs, commenting on them, having them read aloud, and asking a
Claude Code session to answer the comments.

  python3 report_viewer.py [FOLDER | LABEL=FOLDER] ...       serve the page (default http://127.0.0.1:8090/)

For Claude Code sessions ("agents"; run these inside the session, e.g. through its Bash tool):
  python3 report_viewer.py agent register "Name" [--folder DIR]...   list this session on the page (folder default: cwd)
  python3 report_viewer.py agent unregister ["Name"]                  remove it
  python3 report_viewer.py agent list
  python3 report_viewer.py agent hook-config                          the settings.json hooks that let the page wake it
  python3 report_viewer.py comments [--all]                          comments waiting for this session
  python3 report_viewer.py comments show <doc-id>                    every thread on one PDF
  python3 report_viewer.py comments reply <doc-id>/<thread-id> "text"     answer a thread ("-" reads stdin)
  python3 report_viewer.py inbox-watch                               the hook itself (reads the hook JSON on stdin)

The page lists the PDFs uploaded through it, each registered agent's PDFs, and the PDFs in the folders given on the
command line, newest first. A PDF opens in a PDF.js viewer: select text and comment on it, reply, resolve, delete.
"Ask to respond" sends the waiting comments to an agent: the page writes a line to that agent's inbox, and the
inbox-watch hook (SessionStart/Stop, asyncRewake) wakes the session, which reads and answers with `comments`.
"Read aloud" uses the bundled Piper read-along service in readaloud/ (install it with setup-readaloud.sh); the page
starts it on first use and it exits by itself after 15 idle minutes.

Writes only to DATA (default ~/.report-viewer: comments, uploads, agents.json, inbox, readaloud.log). Never modifies,
moves or deletes the PDFs in the folders; "Remove" on an upload moves it to a hidden .deleted folder.
Every change is a POST that needs the page's token and a matching Origin. Listens on 127.0.0.1 unless --host says
otherwise, and then a password (REPORT_VIEWER_PASSWORD) is required unless --no-auth is given.
Python 3.9+, stdlib only (read-aloud has its own venv). Agents and their wake-up need macOS or Linux.
"""
import argparse
import atexit
import base64
import contextlib
import hashlib
import http.server
import ipaddress
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlparse

try:
    import fcntl            # the server, the `comments` command and the hook share lock files
except ImportError:         # Windows: no agents; comments use an in-process lock only
    fcntl = None

HERE = Path(__file__).resolve().parent
PDFJS = HERE / "pdfjs"      # Mozilla PDF.js (Apache-2.0), see pdfjs/SOURCE.txt
PDFJS_TYPES = {".mjs": "text/javascript", ".js": "text/javascript", ".wasm": "application/wasm",
               ".bcmap": "application/octet-stream", ".pfb": "application/octet-stream",
               ".ttf": "font/ttf", ".icc": "application/octet-stream"}
TTS_DIR = HERE / "readaloud"
TTS_VOICE = "en_US-lessac-medium"
HOME = Path.home()
# Folder names never searched: inputs, tooling, dependencies, and hidden folders (checked separately).
SKIP = {"raw", "uploads", "node_modules", "__pycache__", "site-packages", "venv"}
CACHE_S = 30
UPLOAD_LABEL = "Uploaded"
RID_RE = re.compile(r"^[0-9a-f]{16}$")
SID_RE = re.compile(r"^[0-9a-f-]{36}$")
MAX_TEXT = 5000
SCRIPT = "python3 " + shlex.quote(str(HERE / "report_viewer.py"))     # how agents are told to run this file

# Set by configure() and main().
DATA = COMMENTS_DIR = UPLOADS_DIR = INBOX_DIR = AGENTS_FILE = None
UPLOAD_MAX = 100 * 2 ** 20
FOLDERS = []                # [(label, Path)] from the command line
TITLE = "Reports"
TOKEN = secrets.token_urlsafe(24)
PASSWORD = None             # HTTP Basic password, or None
HOSTS = None                # allowed Host headers, or None for any


def configure(data, uploads=True, max_mb=100):
    global DATA, COMMENTS_DIR, UPLOADS_DIR, INBOX_DIR, AGENTS_FILE, UPLOAD_MAX
    DATA = Path(data).expanduser().resolve()
    COMMENTS_DIR, INBOX_DIR, AGENTS_FILE = DATA / "comments", DATA / "inbox", DATA / "agents.json"
    UPLOADS_DIR = DATA / "uploads" if uploads else None
    UPLOAD_MAX = max_mb * 2 ** 20


def default_data():
    return os.environ.get("REPORT_VIEWER_DATA", "~/.report-viewer")


def tilde(p):
    s = str(p)
    return "~" + s[len(str(HOME)):] if s == str(HOME) or s.startswith(str(HOME) + os.sep) else s


@contextlib.contextmanager
def locked(lockfile, write):
    lockfile.parent.mkdir(parents=True, exist_ok=True)
    with open(lockfile, "a") as lock:
        if fcntl:
            fcntl.flock(lock, fcntl.LOCK_EX if write else fcntl.LOCK_SH)
        yield


# ---------------------------------------------------------------- agents (Claude Code sessions)
# DATA/agents.json: {"agents": [{"name", "label", "session", "folders": [...], "registered"}]}. Each agent registers
# itself from inside its session, which supplies CLAUDE_CODE_SESSION_ID.

def slug(label):
    return re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:40] or "agent"


@contextlib.contextmanager
def agent_store(write=False):
    with locked(DATA / ".agents.lock", write):
        try:
            d = json.loads(AGENTS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            d = {"agents": []}
        yield d
        if write:
            tmp = AGENTS_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(d, indent=1), encoding="utf-8")
            os.replace(tmp, AGENTS_FILE)


def agents():
    with agent_store() as d:
        return d["agents"]


def agent_by(name=None, session=None):
    return next((a for a in agents() if (name and a["name"] == name) or (session and a["session"] == session)), None)


def me():
    """The registered agent for the Claude Code session this command runs in, or None."""
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    return agent_by(session=sid) if sid else None


# ---------------------------------------------------------------- the PDF index

_index = {"t": 0, "sections": [], "files": {}}
_index_lock = threading.Lock()


def doc_id(path):
    return hashlib.sha256(str(path).encode()).hexdigest()[:16]


def scan_folders(folders, files):
    """Every PDF (regular file, not a symlink) under folders, newest first; adds each to files {id: path}."""
    docs = []
    for root in folders:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP and not d.startswith(".")]
            for fn in filenames:
                p = Path(dirpath) / fn
                if p.suffix.lower() != ".pdf" or p.is_symlink() or fn.startswith("."):
                    continue
                rid = doc_id(p)
                if rid in files:
                    continue
                try:
                    st = p.stat()
                except OSError:
                    continue
                files[rid] = p
                docs.append({"id": rid, "name": fn, "title": p.stem.replace("_", " "), "where": tilde(p.parent),
                             "mtime": st.st_mtime, "size": st.st_size})
    docs.sort(key=lambda r: r["mtime"], reverse=True)
    return docs


def scan():
    """Uploads first, then each agent's PDFs, then the command-line folders. A PDF is listed once, first match."""
    files, sections = {}, []
    if UPLOADS_DIR:
        sections.append({"label": UPLOAD_LABEL, "agent": "", "uploads": True, "folders": [tilde(UPLOADS_DIR)],
                         "docs": scan_folders([UPLOADS_DIR], files)})
    for a in agents():
        folders = [Path(f) for f in a["folders"]]
        sections.append({"label": a["label"], "agent": a["name"], "folders": [tilde(f) for f in folders],
                         "docs": scan_folders(folders, files)})
    for label, folder in FOLDERS:
        sections.append({"label": label, "agent": "", "folders": [tilde(folder)], "docs": scan_folders([folder], files)})
    return sections, files


def index(fresh=False):
    with _index_lock:
        if fresh or time.time() - _index["t"] > CACHE_S:
            _index["sections"], _index["files"] = scan()
            _index["t"] = time.time()
        return _index["sections"], _index["files"]


def doc_file(rid):
    """The PDF for an id from the index only (never a path from the request); rescans once for new files."""
    return index()[1].get(rid) or index(fresh=True)[1].get(rid)


def doc_entry(rid):
    """(section, doc) for an id, or (None, None)."""
    doc_file(rid)
    for s in index()[0]:
        for r in s["docs"]:
            if r["id"] == rid:
                return s, r
    return None, None


def pdf_version(path):
    """Cheap fingerprint of a PDF's content (mtime + size): a comment made on another version is flagged."""
    try:
        st = path.stat()
    except OSError:
        return None
    return f"{int(st.st_mtime)}-{st.st_size}"


def pdfjs_file(rel):
    """A file from the vendored PDF.js folder, or None. No path from the request escapes PDFJS."""
    try:
        p = (PDFJS / rel).resolve()
    except (OSError, ValueError):
        return None
    if not p.is_relative_to(PDFJS.resolve()) or p.suffix not in PDFJS_TYPES or not p.is_file():
        return None
    return p


# ---------------------------------------------------------------- uploads

_upload_lock = threading.Lock()


def place(tmp, dest):
    """Put tmp at dest without ever replacing an existing file (FileExistsError if dest is taken)."""
    try:
        os.link(tmp, dest)
    except FileExistsError:
        raise
    except OSError:         # no hard links on this filesystem: exclusive create + copy
        with open(dest, "xb") as out, open(tmp, "rb") as src:
            shutil.copyfileobj(src, out)


def save_upload(name, length, rfile):
    """Store one uploaded PDF in UPLOADS_DIR under a cleaned-up version of its name, never replacing a file.
    Returns (http code, message, id or None). The body is streamed to disk, not held in memory."""
    if not UPLOADS_DIR:
        return 404, "Uploads are turned off.", None
    if length <= 0:
        return 400, "The file is empty.", None
    if length > UPLOAD_MAX:
        return 413, f"That file is too big: the limit is {UPLOAD_MAX // 2 ** 20} MB.", None
    stem = re.sub(r"[^\w.,()+\- ]+", " ", Path(name.replace("\\", "/")).stem)       # keep letters, digits, _ . , ( ) + -
    stem = re.sub(r"\s+", " ", stem).strip(" ._-")[:120] or "document"
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = UPLOADS_DIR / f".part-{secrets.token_hex(6)}"      # hidden, so the scanner never lists a partial file
    try:
        with open(tmp, "xb") as f:
            left = length
            while left:
                chunk = rfile.read(min(left, 2 ** 20))
                if not chunk:
                    return 400, "The upload was cut short; try again.", None
                f.write(chunk)
                left -= len(chunk)
        with open(tmp, "rb") as f:
            if b"%PDF-" not in f.read(1024):
                return 415, "That isn't a PDF. Only PDF files can be uploaded.", None
        with _upload_lock:
            dest, n = UPLOADS_DIR / f"{stem}.pdf", 2
            while True:
                try:
                    place(tmp, dest)
                    break
                except FileExistsError:
                    dest, n = UPLOADS_DIR / f"{stem} ({n}).pdf", n + 1
    except OSError as e:
        return 500, f"Couldn't save the file: {e.strerror or e}", None
    finally:
        tmp.unlink(missing_ok=True)
    index(fresh=True)
    return 200, f"Uploaded {dest.name}.", doc_id(dest)


def remove_upload(rid):
    """Move an uploaded PDF into UPLOADS_DIR/.deleted (recoverable). Only files in UPLOADS_DIR itself."""
    path = doc_file(rid) if UPLOADS_DIR and RID_RE.match(rid or "") else None
    if not path or path.parent.resolve() != UPLOADS_DIR.resolve():
        return 404, "No such uploaded document."
    trash = UPLOADS_DIR / ".deleted"
    trash.mkdir(exist_ok=True)
    try:
        os.replace(path, trash / f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())} {path.name}")
    except OSError as e:
        return 500, f"Couldn't remove it: {e.strerror or e}"
    index(fresh=True)
    return 200, f"Removed {path.name}."


# ---------------------------------------------------------------- comments
# One JSON file per PDF in COMMENTS_DIR: {"path", "title", "threads": [...], "asks": [...]}, written under a lock
# file shared with the `comments` command and the hook, so none of them overwrites another's change.

_comment_lock = threading.Lock()


@contextlib.contextmanager
def comment_store(rid, write=False):
    if not RID_RE.match(rid or ""):
        raise ValueError("bad document id")
    with _comment_lock, locked(COMMENTS_DIR / ".lock", write):
        path = COMMENTS_DIR / f"{rid}.json"
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            d = {"threads": [], "asks": []}
        yield d
        if write:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(d, indent=1), encoding="utf-8")
            os.replace(tmp, path)


def waiting(thread):
    """An open thread whose last word is the reader's is waiting for a reply."""
    return thread["status"] == "open" and bool(thread["comments"]) and thread["comments"][-1]["by"] == "user"


def clean_name(s, default):
    return re.sub(r"\s+", " ", str(s or "")).strip()[:60] or default


def comment_edit(rid, op, body):
    """Apply one change from the page. Returns (http code, message)."""
    sec, rep = doc_entry(rid)
    if not rep:
        return 404, "No such document."
    text = str(body.get("text") or "").strip()[:MAX_TEXT]
    name = clean_name(body.get("name"), "You")
    now = time.time()
    with comment_store(rid, write=True) as d:
        d["path"], d["title"], d["owner"] = str(doc_file(rid)), rep["title"], sec["agent"]
        th = next((t for t in d["threads"] if t["id"] == body.get("thread")), None)
        if op == "add":
            if not text:
                return 400, "Write a comment first."
            try:
                page = int(body["page"]) if body.get("page") else None
                rects = [[round(float(v), 5) for v in r[:4]] for r in (body.get("rects") or [])[:200]]
            except (TypeError, ValueError, KeyError):
                return 400, "Bad highlight."
            d["threads"].append({"id": "t" + secrets.token_hex(4), "page": page, "rects": rects,
                                 "quote": str(body.get("quote") or "")[:2000], "version": pdf_version(doc_file(rid)),
                                 "status": "open", "created": now,
                                 "comments": [{"by": "user", "name": name, "text": text, "t": now}]})
            return 200, "Comment added."
        if not th and op != "ask":
            return 404, "That comment thread no longer exists."
        if op == "reply":
            if not text:
                return 400, "Write a reply first."
            th["comments"].append({"by": "user", "name": name, "text": text, "t": now})
            th["status"] = "open"
            return 200, "Reply added."
        if op in ("resolve", "reopen"):
            th["status"] = "resolved" if op == "resolve" else "open"
            return 200, "Resolved." if op == "resolve" else "Reopened."
        if op == "delete":
            d["threads"].remove(th)
            return 200, "Deleted."
        if op == "ask":
            ids = [t["id"] for t in d["threads"] if waiting(t)]
            if not ids:
                return 400, "No open comments are waiting for a reply."
            agent = agent_by(name=str(body.get("to") or ""))
            if not agent:
                return 400, "Choose which agent to ask."
            ask = {"id": "a" + secrets.token_hex(4), "t": now, "threads": ids, "status": "queued", "note": "",
                   "to": agent["name"], "label": agent["label"]}
            d["asks"] = (d.get("asks") or [])[-19:] + [ask]
    if op == "ask":
        status, note = deliver_ask(agent, rep, ask, own=agent["name"] == sec["agent"])
        with comment_store(rid, write=True) as d:
            for a in d.get("asks", []):
                if a["id"] == ask["id"]:
                    a.update(status=status, note=note, prompt=ask.get("prompt", ""))
        return (200 if status != "failed" else 502), note
    return 400, "Unknown change."


def inbox_listening(sid):
    """Is an inbox-watch hook waiting for this session? (It holds the session's lock while it waits.)"""
    lockfile = INBOX_DIR / f"{sid}.lock"
    if not fcntl or not lockfile.exists():
        return False
    with open(lockfile, "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(f, fcntl.LOCK_UN)
    return False


def deliver_ask(agent, rep, ask, own):
    """Tell an agent that comments are waiting. Returns (status, message for the page). Writes a line to the
    agent's inbox, which its inbox-watch hook turns into a wake-up. Neither carries the comment text: the agent reads
    it with `comments`. A paste-in prompt is kept on the ask as a fallback, shown when no hook is listening."""
    n = len(ask["threads"])
    what = f"your report \"{rep['title']}\"" if own else f"the document \"{rep['title']}\""
    ask["prompt"] = (f"I've left {n} comment{'s' if n != 1 else ''} on {what} in the report viewer. "
                     f"Run `{SCRIPT} comments` to read them, then answer each one with `{SCRIPT} comments reply`.")
    if not fcntl:
        return "manual", f"Automatic wake-up needs macOS or Linux. Paste this into {agent['label']}'s chat:"
    sid = agent["session"]
    with locked(INBOX_DIR / f"{sid}.box.lock", True), open(INBOX_DIR / f"{sid}.jsonl", "a") as f:
        f.write(json.dumps({"doc": rep["id"], "ask": ask["id"], "title": rep["title"], "waiting": n, "t": ask["t"]}) + "\n")
    if inbox_listening(sid):
        return "sent", f"Sent to {agent['label']}; it should wake within a few seconds."
    return "manual", (f"Queued. {agent['label']} isn't listening right now (no hook, or the session is closed); it will "
                      f"pick this up after its next turn. To reach it sooner, paste this into its chat:")


# ---------------------------------------------------------------- read-aloud
# The Piper read-along service in readaloud/ (readalong_server.py, from the PDF Reader project). The page starts it
# as a child process on first use, on a free local port, allowed to read only the folders listed on the page, and
# only ever passes it paths from the index. It exits by itself after 15 idle minutes.

_tts = {"proc": None, "url": None, "roots": []}
_tts_lock = threading.Lock()


def tts_python():
    for p in (TTS_DIR / ".venv/bin/python", TTS_DIR / ".venv/Scripts/python.exe"):
        if p.is_file():
            return p
    return None


def tts_installed():
    return bool(tts_python()) and (TTS_DIR / "voices" / f"{TTS_VOICE}.onnx").is_file()


def tts_healthy():
    if not _tts["url"]:
        return False
    try:
        with urllib.request.urlopen(_tts["url"] + "/health", timeout=1) as r:
            return bool(json.loads(r.read()).get("model_loaded"))
    except Exception:
        return False


def tts_stop():
    p = _tts["proc"]
    if p and p.poll() is None:
        p.terminate()
        try:
            p.wait(5)
        except subprocess.TimeoutExpired:
            p.kill()
    _tts.update(proc=None, url=None, roots=[])


def tts_ready(path, wait_s=60):
    """True once the service answers for a PDF at path; (re)starts it if it isn't running or can't read path."""
    def covers():
        return any(Path(path).resolve().is_relative_to(r) for r in _tts["roots"])
    if tts_healthy() and covers():
        return True
    with _tts_lock:
        if tts_healthy() and covers():
            return True
        tts_stop()
        roots = sorted({Path(f).expanduser().resolve() for s in index()[0] for f in s["folders"] if Path(f).expanduser().is_dir()})
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        cmd = [str(tts_python()), "-u", "readalong_server.py", "--host", "127.0.0.1", "--port", str(port),
               "--threads", str(min(4, os.cpu_count() or 1)), "--idle-exit", "900",
               "--voice", TTS_VOICE, "--voice-dir", str(TTS_DIR / "voices")]
        for r in roots:
            cmd += ["--allow-root", str(r)]
        DATA.mkdir(parents=True, exist_ok=True)
        with open(DATA / "readaloud.log", "ab") as log:
            _tts["proc"] = subprocess.Popen(cmd, cwd=TTS_DIR, stdout=log, stderr=log, stdin=subprocess.DEVNULL)
        _tts.update(url=f"http://127.0.0.1:{port}", roots=roots)
        end = time.time() + wait_s
        while time.time() < end and _tts["proc"].poll() is None:
            if tts_healthy():
                return True
            time.sleep(0.3)
    return False


def tts_get(path, params, timeout=60):
    """(http code, body bytes) from the service; errors come back as JSON {error}."""
    try:
        with urllib.request.urlopen(f"{_tts['url']}{path}?{urlencode(params)}", timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() or json.dumps({"error": f"read-aloud service error {e.code}"}).encode()
    except Exception as e:
        return 502, json.dumps({"error": f"read-aloud service unreachable: {e}"}).encode()


# ---------------------------------------------------------------- web server

class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "report-viewer"

    def log_message(self, *a):
        pass

    CSP = "default-src 'self'; img-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'"
    # The viewer loads PDF.js from /pdfjs/ (module + worker + wasm decoders), draws into canvases, plays blob audio.
    CSP_PDF = ("default-src 'self'; img-src 'self' blob: data:; font-src 'self' blob: data:; style-src 'unsafe-inline'; "
               "script-src 'self' 'unsafe-inline' 'wasm-unsafe-eval'; worker-src 'self'; connect-src 'self'; media-src 'self' blob:")

    def send(self, code, body, ctype, cache="no-store", csp=None, extra=()):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", csp or self.CSP)
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def json(self, code, obj):
        self.send(code, json.dumps(obj), "application/json")

    def allowed(self):
        """Host check (against DNS rebinding) and the optional password. Sends the refusal itself."""
        if HOSTS is not None and (self.headers.get("Host") or "").lower() not in HOSTS:
            self.close_connection = True
            self.send(403, "Forbidden: unexpected Host header (see --allow-host).\n", "text/plain")
            return False
        if PASSWORD is not None:
            auth = self.headers.get("Authorization") or ""
            try:
                given = base64.b64decode(auth[6:]).decode().partition(":")[2] if auth.startswith("Basic ") else ""
            except ValueError:
                given = ""
            if not secrets.compare_digest(given.encode(), PASSWORD.encode()):
                self.close_connection = True
                self.send(401, "Password required.\n", "text/plain",
                          extra=[("WWW-Authenticate", 'Basic realm="report viewer", charset="UTF-8"')])
                return False
        return True

    def page(self, html, csp=None):
        html = html.replace("__TOKEN__", TOKEN).replace("__TITLE__", json.dumps(TITLE)[1:-1].replace("<", "\\u003c"))
        self.send(200, html, "text/html; charset=utf-8", csp=csp)

    def do_GET(self):
        if not self.allowed():
            return
        u = urlparse(self.path)
        q = parse_qs(u.query)
        rid = q.get("id", [""])[0]
        if u.path in ("/", "/index.html", "/reports", "/reports/"):
            self.page(LIST_PAGE)
        elif u.path == "/reports/view":
            self.page(VIEWER_PAGE, csp=self.CSP_PDF)
        elif u.path == "/api/reports":
            self.json(200, {"title": TITLE, "sections": index()[0], "max_mb": UPLOAD_MAX // 2 ** 20,
                            "readaloud": tts_installed(), "time": time.time()})
        elif u.path == "/api/comments":
            sec, rep = doc_entry(rid)
            if not rep:
                self.json(404, {"error": "No such document."})
                return
            with comment_store(rid) as d:
                self.json(200, {"owner": sec["agent"], "version": pdf_version(doc_file(rid)), "threads": d["threads"],
                                "asks": d.get("asks", []), "choices": [{"name": a["name"], "label": a["label"]} for a in agents()],
                                "auto": bool(fcntl), "register": f'{SCRIPT} agent register "Name"'})
        elif u.path in ("/api/tts/words", "/api/tts/sentence"):
            path = doc_file(rid)
            if not path:
                self.json(404, {"error": "No such document."})
                return
            if not tts_installed():
                self.json(503, {"error": "Read-aloud isn't installed. Run setup-readaloud.sh in the report-viewer folder."})
                return
            if not tts_ready(path):
                self.json(503, {"error": f"Read-aloud didn't start; see {tilde(DATA / 'readaloud.log')}."})
                return
            params = {"pdf": str(path)}
            if u.path.endswith("sentence"):
                try:
                    params.update(page=int(q["page"][0]), word=int(q["word"][0]))
                except (KeyError, ValueError):
                    self.json(400, {"error": "page and word are required."})
                    return
            code, body = tts_get("/" + u.path.rsplit("/", 1)[1], params)
            self.send(code, body, "application/json")
        elif u.path == "/reports/file":
            path = doc_file(rid)
            try:
                data = path.read_bytes() if path else None
            except OSError:
                data = None
            if data is None:
                self.send(404, "No such document (it may have been moved or deleted).\n", "text/plain")
                return
            disp = "attachment" if "download" in q else "inline"
            self.send(200, data, "application/pdf", cache="private, no-cache",
                      extra=[("Content-Disposition", f"{disp}; filename*=UTF-8''{quote(path.name)}")])
        elif u.path.startswith("/pdfjs/"):
            p = pdfjs_file(u.path[len("/pdfjs/"):])
            if not p:
                self.send(404, "not found\n", "text/plain")
                return
            self.send(200, p.read_bytes(), PDFJS_TYPES[p.suffix], cache="private, max-age=604800, immutable",
                      csp=self.CSP_PDF)
        else:
            self.send(404, "not found\n", "text/plain")

    def do_POST(self):
        if not self.allowed():
            return
        u = urlparse(self.path)
        if u.path not in ("/api/comments", "/api/upload", "/api/upload-remove"):
            self.close_connection = True
            self.send(404, "not found\n", "text/plain")
            return
        origin = self.headers.get("Origin")
        host = self.headers.get("Host") or ""
        if (not secrets.compare_digest((self.headers.get("X-Token") or "").encode(), TOKEN.encode())
                or (origin and origin not in (f"http://{host}", f"https://{host}"))):
            self.close_connection = True
            self.json(403, {"ok": False, "message": "Bad token or origin; reload the page."})
            return
        if u.path == "/api/upload":       # raw PDF body, name in the query string
            try:
                n = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                n = 0
            code, msg, rid = save_upload(parse_qs(u.query).get("name", [""])[0], n, self.rfile)
            if code != 200:
                self.close_connection = True      # the rest of the body may be unread
            self.json(code, {"ok": code == 200, "message": msg, "id": rid})
            return
        try:
            n = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(min(n, 64000)) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except Exception:
            self.json(400, {"ok": False, "message": "Bad request."})
            return
        if u.path == "/api/comments":
            try:
                code, msg = comment_edit(str(body.get("id")), str(body.get("op")), body)
            except ValueError:
                code, msg = 400, "Bad document id."
        else:
            code, msg = remove_upload(str(body.get("id")))
        self.json(code, {"ok": code == 200, "message": msg})


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# ---------------------------------------------------------------- `agent` command

def agent_main(argv):
    ap = argparse.ArgumentParser(prog="report_viewer.py agent", description="List a Claude Code session on the page.")
    ap.add_argument("--data", default=default_data(), help="the server's data folder (default ~/.report-viewer)")
    ap.add_argument("cmd", choices=["register", "unregister", "list", "hook-config"])
    ap.add_argument("label", nargs="?", help="register: the name shown on the page; unregister: name (default: this session)")
    ap.add_argument("--folder", action="append", default=[], help="register: a folder with this agent's PDFs (repeatable; "
                                                                    "default: the current folder)")
    ap.add_argument("--session", default=os.environ.get("CLAUDE_CODE_SESSION_ID", ""),
                    help="register: the Claude Code session id (default: $CLAUDE_CODE_SESSION_ID)")
    a = ap.parse_args(argv)
    configure(a.data)
    if a.cmd == "register":
        if not a.label:
            sys.exit('usage: report_viewer.py agent register "Name" [--folder DIR]...')
        if not SID_RE.match(a.session):
            sys.exit("report_viewer: no Claude Code session id: run this inside the Claude session (its Bash tool sets "
                     "CLAUDE_CODE_SESSION_ID), or pass --session")
        folders = [Path(f).expanduser().resolve() for f in (a.folder or ["."])]
        for f in folders:
            if not f.is_dir():
                sys.exit(f"report_viewer: not a folder: {f}")
            if f == HOME or f == Path(f.anchor):
                sys.exit(f"report_viewer: {f} is too broad; give the folder where this agent keeps its PDFs")
        label, name = clean_name(a.label, "Agent"), slug(a.label)
        with agent_store(write=True) as d:
            d["agents"] = [x for x in d["agents"] if x["name"] != name and x["session"] != a.session]
            d["agents"].append({"name": name, "label": label, "session": a.session,
                                "folders": [str(f) for f in folders], "registered": time.time()})
        print(f"Registered {label} (session {a.session[:8]}…) with {', '.join(tilde(f) for f in folders)}.")
        print("Its PDFs appear on the page within 30 s. For the page to wake this session, the hooks from\n"
              f"  {SCRIPT} agent hook-config\nmust be in ~/.claude/settings.json (once per computer).")
    elif a.cmd == "unregister":
        with agent_store(write=True) as d:
            keep = [x for x in d["agents"] if not (x["name"] == slug(a.label) if a.label else x["session"] == a.session)]
            gone = len(d["agents"]) - len(keep)
            d["agents"] = keep
        print(f"Removed {gone} agent{'s' if gone != 1 else ''}.")
    elif a.cmd == "list":
        for x in agents():
            print(f"{x['label']}  (name {x['name']}, session {x['session']}, registered {when(x['registered'])})")
            for f in x["folders"]:
                print(f"    {tilde(f)}")
        if not agents():
            print("No agents registered.")
    else:
        cmd = f"{shlex.quote(sys.executable)} {shlex.quote(str(HERE / 'report_viewer.py'))} inbox-watch"
        if DATA != Path(os.path.expanduser("~/.report-viewer")).resolve():
            cmd += f" --data {shlex.quote(str(DATA))}"
        hook = [{"hooks": [{"type": "command", "command": cmd, "asyncRewake": True, "timeout": 86400}]}]
        print(json.dumps({"hooks": {"SessionStart": hook, "Stop": hook}}, indent=2))
        print("\nMerge this into ~/.claude/settings.json (keep any hooks already there), then restart the Claude sessions.",
              file=sys.stderr)


# ---------------------------------------------------------------- `comments` command

def when(t):
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(t))


def all_comment_files():
    """[(doc id, comment file contents)] for every PDF that has comment threads."""
    out = []
    for f in sorted(COMMENTS_DIR.glob("*.json")) if COMMENTS_DIR.is_dir() else []:
        if RID_RE.match(f.stem):
            with comment_store(f.stem) as d:
                if d["threads"]:
                    out.append((f.stem, d))
    return out


def print_thread(rid, t):
    where = f"page {t['page']}" if t["page"] else "general"
    print(f"  [{rid}/{t['id']}] {where} · {t['status']}{' · WAITING FOR REPLY' if waiting(t) else ''}")
    if t.get("quote"):
        print(f"    highlighted: \"{t['quote']}\"")
    for c in t["comments"]:
        print(f"    {c['name']} ({when(c['t'])}): " + c["text"].replace("\n", "\n      "))


def comments_main(argv):
    ap = argparse.ArgumentParser(prog="report_viewer.py comments", description="Read and answer comments left on the page.")
    ap.add_argument("--data", default=default_data(), help="the server's data folder (default ~/.report-viewer)")
    ap.add_argument("--all", action="store_true", help="every waiting comment, whoever it was sent to")
    ap.add_argument("--name", help="reply: name shown on the reply (default: this session's registered name)")
    ap.add_argument("cmd", nargs="?", choices=["list", "show", "reply"], default="list")
    ap.add_argument("target", nargs="?", help="show: <doc-id>; reply: <doc-id>/<thread-id>")
    ap.add_argument("text", nargs="?", help="reply text, or - to read it from stdin")
    a = ap.parse_args(argv)
    configure(a.data)
    who = me()
    if a.cmd == "list":
        if not who and not a.all:
            sys.exit("report_viewer: this session isn't registered (see `agent register`); use --all to see every comment")
        found = False
        for rid, d in all_comment_files():
            ts = [t for t in d["threads"] if waiting(t)]
            if not a.all:       # comments on its own PDFs, and those the reader sent it
                if d.get("owner") != who["name"]:
                    mine = {i for x in d.get("asks", []) if x.get("to") == who["name"] for i in x["threads"]}
                    ts = [t for t in ts if t["id"] in mine]
            if ts:
                found = True
                print(f"{d.get('title', rid)}\n  file: {tilde(d.get('path', '?'))}  (doc id {rid})")
                for t in ts:
                    print_thread(rid, t)
                print()
        if not found:
            print("No comments are waiting for a reply." if a.all else f"No comments are waiting for {who['label']}.")
        else:
            print(f'Answer each one with: {SCRIPT} comments reply <doc-id>/<thread-id> "your reply"')
    elif a.cmd == "show":
        if not a.target or not RID_RE.match(a.target):
            sys.exit("usage: report_viewer.py comments show <doc-id>")
        with comment_store(a.target) as d:
            print(f"{d.get('title', a.target)}\n  file: {tilde(d.get('path', '?'))}")
            for t in d["threads"]:
                print_thread(a.target, t)
            if not d["threads"]:
                print("  (no comments)")
    else:
        rid, _, tid = (a.target or "").partition("/")
        if not RID_RE.match(rid) or not tid or a.text is None:
            sys.exit('usage: report_viewer.py comments reply <doc-id>/<thread-id> "text"')
        name = clean_name(a.name, "") or (who and who["label"])
        if not name:
            sys.exit("report_viewer: this session isn't registered; pass --name")
        text = (sys.stdin.read() if a.text == "-" else a.text).strip()[:MAX_TEXT]
        if not text:
            sys.exit("Empty reply.")
        now = time.time()
        with comment_store(rid, write=True) as d:
            th = next((t for t in d["threads"] if t["id"] == tid), None)
            if not th:
                sys.exit(f"No thread {tid} on document {rid}.")
            th["comments"].append({"by": "agent", "name": name, "text": text, "t": now})
            for x in d.get("asks", []):     # a request is answered once none of its threads is still waiting
                if x["status"] in ("queued", "manual", "sent", "delivered") and \
                        not any(waiting(t) for t in d["threads"] if t["id"] in x["threads"]):
                    x["status"], x["note"] = "answered", f"{name} replied {when(now)}."
        print(f"Reply posted to {rid}/{tid} as {name}.")


# ---------------------------------------------------------------- `inbox-watch` hook
# Claude Code runs this on SessionStart and Stop with "asyncRewake": true. It waits in the background for a line in
# INBOX/<session-id>.jsonl (written when the reader presses "Ask to respond"), then exits 2 with a fixed notice on
# stderr, which wakes the session. The notice never carries the comment text. One watcher per session (it holds
# INBOX/<session-id>.lock); it only arms for registered sessions and exits when its Claude process is gone.

POLL_S = 2
MAX_WAIT_S = 23 * 3600      # under the hook's timeout; the next Stop or SessionStart arms a fresh watcher


def claude_pid():
    """pid of the claude process this hook belongs to: $CLAUDE_PID, else the nearest ancestor named claude (Linux)."""
    try:
        return int(os.environ["CLAUDE_PID"])
    except (KeyError, ValueError):
        pass
    pid = os.getppid()
    while pid > 1:
        try:
            args = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            if args and os.path.basename(args[0].decode(errors="replace")) == "claude":
                return pid
            pid = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            return None
    return None


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def inbox_main(argv):
    ap = argparse.ArgumentParser(prog="report_viewer.py inbox-watch")
    ap.add_argument("--data", default=default_data())
    configure(ap.parse_args(argv).data)
    try:
        sid = str(json.load(sys.stdin).get("session_id", ""))
    except Exception:
        return 0
    agent = agent_by(session=sid) if SID_RE.match(sid) and fcntl else None
    if not agent:
        return 0
    owner = claude_pid()
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    lock = open(INBOX_DIR / f"{sid}.lock", "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return 0                      # another watcher is already listening for this session
    box = INBOX_DIR / f"{sid}.jsonl"
    end = time.time() + MAX_WAIT_S
    while time.time() < end:
        if owner and not alive(owner):
            return 0                  # the session ended
        if box.exists() and box.stat().st_size > 0:
            with locked(INBOX_DIR / f"{sid}.box.lock", True):
                lines = box.read_text(encoding="utf-8").splitlines()
                box.unlink()
            items = []
            for line in lines:
                try:
                    items.append(json.loads(line))
                except ValueError:
                    pass
            if not items:
                continue
            for it in items:          # record on each ask that it reached the agent (best effort)
                try:
                    with comment_store(it["doc"], write=True) as d:
                        for x in d.get("asks", []):
                            if x["id"] == it["ask"] and x["status"] in ("sent", "queued", "manual"):
                                x["status"], x["note"] = "delivered", f"Delivered to {agent['label']} at {time.strftime('%H:%M', time.gmtime())} UTC."
                except Exception:
                    pass
            titles = sorted({it.get("title", "a document") for it in items})
            n = sum(int(it.get("waiting", 0)) for it in items)
            sys.stderr.write(
                f"Report viewer: the user pressed \"Ask to respond\" for {agent['label']}. "
                f"{n} comment{'s are' if n != 1 else ' is'} waiting on: " + "; ".join(f'"{t}"' for t in titles) + ". "
                f"Run `{SCRIPT} comments` to read them, then answer each with "
                f"`{SCRIPT} comments reply <doc-id>/<thread-id> \"...\"`. Your replies appear on the page. "
                "(This notice comes from the report viewer's inbox-watch hook; the comments are the user's own.)\n")
            return 2
        time.sleep(POLL_S)
    return 0


# ---------------------------------------------------------------- pages

BASE_CSS = r"""
:root{--bg:#f6f7f9;--card:#fff;--ink:#1b1f24;--mute:#5f6b78;--line:#e3e7ec;--bad:#c0392b;--link:#3b6fd8}
@media (prefers-color-scheme:dark){:root{--bg:#111418;--card:#1a1f25;--ink:#e6e9ed;--mute:#9aa5b1;--line:#2a313a;--bad:#ff6b5e;--link:#6f9bff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.4 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
a{color:var(--link)}.note{color:var(--mute);font-size:12px}.err{color:var(--bad)}
"""

LIST_PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Reports</title>
<style>""" + BASE_CSS + r"""
header{padding:18px 16px 8px}h1{font-size:28px;line-height:1.1;margin:0;font-weight:700}
main{max-width:900px;padding:4px 16px 24px}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin:12px 0}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.04em;color:var(--mute);margin:0 0 4px;display:flex;justify-content:space-between;gap:8px}
ul{list-style:none;margin:0;padding:0}li{padding:9px 0;border-bottom:1px solid var(--line)}li:last-child{border-bottom:0}
li a{font-size:15px;font-weight:600;text-decoration:none;overflow-wrap:anywhere}li a:hover{text-decoration:underline}
li .note{margin-top:2px;overflow-wrap:anywhere;font-variant-numeric:tabular-nums}
details summary{cursor:pointer}
button{font:inherit;font-size:13px;padding:5px 11px;border-radius:6px;border:1px solid var(--line);background:var(--card);color:var(--ink);cursor:pointer}
button.primary{background:var(--link);border-color:var(--link);color:#fff;font-weight:600}button.danger{color:var(--bad)}button:disabled{opacity:.5}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:8px 0 4px}
li.up{display:flex;justify-content:space-between;align-items:center;gap:10px}li.up>div{min-width:0}
</style></head><body>
<header><h1 id="h1">Reports</h1></header>
<main><div class="note">Uploaded PDFs, then each agent's PDFs and the other folders, newest first. New files show up within 30 s.</div><div id="list"><p class="note">loading…</p></div></main>
<script>
const TOKEN='__TOKEN__',TITLE='__TITLE__';let MAX_MB=100;
document.title=TITLE;document.getElementById('h1').textContent=TITLE;
const $=id=>document.getElementById(id);
function el(tag,attrs={},...kids){const e=document.createElement(tag);for(const[k,v]of Object.entries(attrs))e.setAttribute(k,v);for(const k of kids)e.append(k instanceof Node?k:document.createTextNode(k??''));return e}
const size=b=>b>=1e6?(b/1e6).toFixed(1)+' MB':Math.max(1,Math.round(b/1e3))+' kB';
const when=t=>new Date(t*1000).toISOString().slice(0,16).replace('T',' ')+' UTC';
const plural=(n,w)=>n+' '+w+(n==1?'':'s');
async function load(){
 let d;try{d=await (await fetch('/api/reports',{cache:'no-store'})).json()}catch(e){$('list').replaceChildren(el('p',{class:'note err'},'Could not load the list: '+e));return}
 MAX_MB=d.max_mb;
 const rest=d.sections.filter(s=>!s.uploads),withR=rest.filter(s=>s.docs.length),none=rest.filter(s=>!s.docs.length);
 const out=d.sections.filter(s=>s.uploads).map(upSection).concat(withR.map(s=>el('section',{},el('h2',{},el('span',{},s.label),el('span',{},plural(s.docs.length,'PDF'))),
  el('ul',{},...s.docs.map(r=>el('li',{},el('a',{href:'/reports/view?id='+r.id},r.title),el('div',{class:'note'},`${when(r.mtime)} · ${size(r.size)} · ${r.where}`)))))));
 if(none.length)out.push(el('section',{},el('details',{},el('summary',{class:'note'},'No PDFs yet: '+none.map(s=>s.label).join(', ')),
  ...none.map(s=>el('div',{class:'note'},`${s.label}: searched ${s.folders.join(', ')}`)))));
 if(!out.length)out.push(el('p',{class:'note'},'Nothing to show yet: start the server with a folder, or register an agent.'));
 $('list').replaceChildren(...out)}
// ---- uploads: raw PDF bodies to /api/upload (XHR for a progress figure); Remove moves the file to .deleted/
let UP={text:'',err:false},BUSY=false;
function setUp(text,err=false){UP={text,err};const m=$('upmsg');if(m){m.textContent=text;m.className='note'+(err?' err':'')}}
function upSection(s){
 const inp=el('input',{type:'file',accept:'application/pdf,.pdf',multiple:'',hidden:''});
 const btn=el('button',{type:'button',class:'primary'},'Upload PDF');btn.onclick=()=>inp.click();btn.disabled=BUSY;
 inp.onchange=async()=>{const fs=[...inp.files];inp.value='';if(!fs.length)return;BUSY=true;btn.disabled=true;
  const done=[];for(const f of fs)if(await upload(f))done.push(f.name);BUSY=false;
  if(done.length&&fs.length>1)setUp(`Uploaded ${done.length} of ${fs.length} files.`,done.length<fs.length);load()};
 const items=s.docs.map(r=>{const rm=el('button',{type:'button',class:'danger'},'Remove');
  rm.onclick=async()=>{if(!confirm(`Remove “${r.name}”? It moves to a hidden .deleted folder, so it can be restored.`))return;rm.disabled=true;
   let j;try{const x=await fetch('/api/upload-remove',{method:'POST',headers:{'Content-Type':'application/json','X-Token':TOKEN},body:JSON.stringify({id:r.id})});
    j=await x.json()}catch(e){j={ok:false,message:'Remove failed: '+e}}setUp(j.message,!j.ok);load()};
  return el('li',{class:'up'},el('div',{},el('a',{href:'/reports/view?id='+r.id},r.title),el('div',{class:'note'},`${when(r.mtime)} · ${size(r.size)}`)),rm)});
 const m=el('div',{class:'note'+(UP.err?' err':''),id:'upmsg'},UP.text);
 return el('section',{},el('h2',{},el('span',{},s.label),el('span',{},s.docs.length?plural(s.docs.length,'PDF'):'')),
  el('div',{class:'row'},btn,el('span',{class:'note'},`PDF, up to ${MAX_MB} MB. Highlight and comment, then choose which agent to ask.`)),m,inp,
  items.length?el('ul',{},...items):el('p',{class:'note'},'Nothing uploaded yet.'))}
function upload(f){return new Promise(res=>{
 if(f.size>MAX_MB*1048576){setUp(`${f.name} is too big: the limit is ${MAX_MB} MB.`,true);return res(false)}
 if(!/\.pdf$/i.test(f.name)&&f.type!=='application/pdf'){setUp(`${f.name} isn't a PDF.`,true);return res(false)}
 const x=new XMLHttpRequest();x.open('POST','/api/upload?name='+encodeURIComponent(f.name));
 x.setRequestHeader('X-Token',TOKEN);x.setRequestHeader('Content-Type','application/pdf');
 x.upload.onprogress=e=>{if(e.lengthComputable)setUp(`Uploading ${f.name}: ${Math.round(e.loaded/e.total*100)}%`)};
 x.onload=()=>{let j;try{j=JSON.parse(x.responseText)}catch(e){j={ok:false,message:`Upload failed (${x.status}).`}}setUp(j.message,!j.ok);res(!!j.ok)};
 x.onerror=()=>{setUp(`${f.name}: the upload failed (connection error).`,true);res(false)};
 setUp(`Uploading ${f.name}…`);x.send(f)})}
load();setInterval(()=>{if(!document.hidden&&!BUSY)load()},30000);
</script></body></html>
"""

VIEWER_PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Report</title>
<style>""" + BASE_CSS + r"""
[hidden]{display:none!important}
.bar{position:sticky;top:0;z-index:3;background:var(--card);border-bottom:1px solid var(--line);padding:8px 16px;display:flex;align-items:center;gap:6px 12px;flex-wrap:wrap}
.t{flex:1 1 200px;min-width:0}.t b{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tools{display:flex;align-items:center;gap:6px;font-variant-numeric:tabular-nums;flex-wrap:wrap}
button{font:inherit;font-size:13px;min-width:34px;padding:4px 9px;border-radius:6px;border:1px solid var(--line);background:var(--card);color:var(--ink);cursor:pointer}
button.primary{background:var(--link);border-color:var(--link);color:#fff;font-weight:600}button.danger{color:var(--bad)}button:disabled{opacity:.5}
#pages{padding:12px 16px 40px;overflow-x:auto}
.page{position:relative;margin:0 auto 12px;background:#fff;box-shadow:0 1px 3px rgba(0,0,0,.25);--scale-round-x:1px;--scale-round-y:1px}
.cv{position:absolute;inset:0}.cv canvas{display:block;width:100%;height:100%}
.hls{position:absolute;inset:0;pointer-events:none;z-index:1}
.hl{position:absolute;background:rgba(255,196,0,.4);mix-blend-mode:multiply;border-radius:2px}.hl.res{background:rgba(120,120,120,.2)}.hl.act{background:rgba(255,140,0,.55);outline:2px solid #e08a00}
/* PDF.js text layer (from pdf_viewer.css, flattened): invisible text over the canvas, so it can be selected */
.textLayer{position:absolute;text-align:initial;inset:0;overflow:clip;opacity:1;line-height:1;letter-spacing:normal;word-spacing:normal;-webkit-text-size-adjust:none;text-size-adjust:none;forced-color-adjust:none;transform-origin:0 0;caret-color:CanvasText;z-index:2;
 --min-font-size:1;--text-scale-factor:calc(var(--total-scale-factor) * var(--min-font-size));--min-font-size-inv:calc(1 / var(--min-font-size))}
.textLayer span,.textLayer br{color:transparent;position:absolute;white-space:pre;cursor:text;transform-origin:0% 0%;-webkit-user-select:text;user-select:text}
.textLayer>:not(.markedContent),.textLayer .markedContent span:not(.markedContent){z-index:1;--font-height:0;font-size:calc(var(--text-scale-factor) * var(--font-height));
 --scale-x:1;--rotate:0deg;transform:rotate(var(--rotate)) scaleX(var(--scale-x)) scale(var(--min-font-size-inv))}
.textLayer .markedContent{display:contents}.textLayer span[role=img]{-webkit-user-select:none;user-select:none;cursor:default}
.textLayer ::selection{background:rgba(0,90,255,.3);color:transparent}.textLayer br::selection{background:transparent}
.textLayer .endOfContent{display:block;position:absolute;inset:100% 0 0;z-index:0;cursor:default;-webkit-user-select:none;user-select:none}.textLayer.selecting .endOfContent{top:0}
#selbar{position:fixed;left:0;right:0;bottom:18px;display:flex;justify-content:center;z-index:4;pointer-events:none}#selbar button{pointer-events:auto;font-size:15px;padding:9px 16px;box-shadow:0 2px 8px rgba(0,0,0,.3)}
#ttsbar{position:fixed;left:0;right:0;bottom:0;z-index:5;background:var(--card);border-top:1px solid var(--line);padding:8px 16px;display:flex;gap:8px;align-items:center;flex-wrap:wrap;box-shadow:0 -2px 8px rgba(0,0,0,.15)}
#ttsbar #tplay{min-width:76px}#tstat{flex:1 1 140px;min-width:0}
body.tts #selbar{bottom:74px}body.tts #pages{padding-bottom:96px}
.ttsl{position:absolute;inset:0;pointer-events:none;z-index:1}.ttsw{position:absolute;background:rgba(0,140,255,.3);mix-blend-mode:multiply;outline:1.5px solid rgba(0,110,220,.7);border-radius:2px}
#panel{position:fixed;top:0;right:0;bottom:0;width:min(420px,100vw);background:var(--card);border-left:1px solid var(--line);z-index:6;display:flex;flex-direction:column;box-shadow:-2px 0 12px rgba(0,0,0,.2)}
.phd{display:flex;justify-content:space-between;align-items:center;padding:10px 14px;border-bottom:1px solid var(--line)}
#pbody{overflow-y:auto;padding:10px 14px 30px;flex:1}
.ask{margin-bottom:10px}.ask .note{margin-top:6px}
.row{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin:6px 0}
.card{border:1px solid var(--line);border-radius:8px;padding:8px 10px;margin:10px 0}.card.act{border-color:#e08a00}.card.res{opacity:.7}
.card .hd{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:4px}.card .hd a{margin-left:auto;font-size:13px}
.chip{font-size:11px;padding:1px 7px;border-radius:9px;background:var(--line);color:var(--mute)}.chip.w{background:#f9eeda;color:#8a5a00}.chip.ok{background:#e5f4e4;color:#1a6b3c}
blockquote{margin:4px 0 6px;padding:2px 8px;border-left:3px solid #e0b400;color:var(--mute);font-style:italic;max-height:6.5em;overflow:auto;overflow-wrap:anywhere}
.c{margin:6px 0}.c .who{font-size:12px;color:var(--mute)}.c .tx{white-space:pre-wrap;overflow-wrap:anywhere}.c.ag{border-left:3px solid var(--link);padding-left:8px}
textarea{width:100%;font:inherit;padding:6px;border-radius:6px;border:1px solid var(--line);background:var(--bg);color:var(--ink);resize:vertical}
select{font:inherit;font-size:13px;padding:3px;border-radius:6px;border:1px solid var(--line);background:var(--card);color:var(--ink)}
code{font-size:12px;overflow-wrap:anywhere}
dialog{border:1px solid var(--line);border-radius:10px;background:var(--card);color:var(--ink);width:min(520px,calc(100vw - 24px));padding:14px}dialog::backdrop{background:rgba(0,0,0,.35)}
@media (max-width:480px){#sub{display:none}.bar{padding:6px 12px}#pages{padding:8px 0 40px}}
</style></head><body>
<div class="bar"><a href="/reports">← List</a><div class="t"><b id="title">Loading…</b><span class="note" id="sub"></span></div>
<div class="tools"><span class="note" id="pg"></span><button id="out" type="button" aria-label="Zoom out">−</button><button id="fit" type="button">Fit</button><button id="in" type="button" aria-label="Zoom in">+</button><button id="rbtn" type="button">Read aloud</button><button id="cbtn" type="button">Comments</button><a id="dl" href="#">Download</a></div></div>
<div id="pages"></div>
<div id="selbar" hidden><button id="selbtn" type="button" class="primary">Comment on selection</button></div>
<div id="ttsbar" hidden><button id="tplay" type="button" class="primary">Play</button><span class="note" id="tstat">Tap a word to read from there, or Play to start at this page.</span>
<label class="note">Speed <select id="tspeed"><option>0.75</option><option selected>1</option><option>1.25</option><option>1.5</option><option>1.75</option><option>2</option></select></label>
<button id="tclose" type="button" aria-label="Close read aloud">✕</button></div>
<aside id="panel" hidden><div class="phd"><b>Comments</b><button id="pclose" type="button">Close</button></div>
<div id="pbody"><div id="pask" class="ask"></div><div class="row"><button id="general" type="button">Add a general comment</button>
<label class="note"><input type="checkbox" id="showres"> Show resolved</label></div><div id="plist"></div></div></aside>
<dialog id="newc"><form method="dialog"><b>New comment</b><blockquote id="nq"></blockquote>
<textarea id="nt" rows="4" placeholder="Your comment"></textarea><div class="note err" id="nmsg"></div>
<div class="row"><button id="nsave" class="primary">Save</button><button id="ncancel" type="button">Cancel</button></div></form></dialog>
<script type="module">
import * as pdfjs from '/pdfjs/pdf.min.mjs';
pdfjs.GlobalWorkerOptions.workerSrc='/pdfjs/pdf.worker.min.mjs';
const TOKEN='__TOKEN__';
const $=id=>document.getElementById(id);
function el(tag,attrs={},...kids){const e=document.createElement(tag);for(const[k,v]of Object.entries(attrs))e.setAttribute(k,v);for(const k of kids)e.append(k instanceof Node?k:document.createTextNode(k??''));return e}
const id=new URLSearchParams(location.search).get('id')||'';
const url='/reports/file?id='+encodeURIComponent(id);
$('dl').href=url+'&download=1';
fetch('/api/reports').then(r=>r.json()).then(d=>{if(!d.readaloud)$('rbtn').title='Read-aloud isn’t installed (run setup-readaloud.sh)';
 for(const s of d.sections)for(const r of s.docs)if(r.id===id){
 document.title=r.title;$('title').textContent=r.title;$('sub').textContent=`${s.label} · ${new Date(r.mtime*1000).toISOString().slice(0,10)} · ${r.where}`}}).catch(()=>{});
const MAX_PX=16e6;                 // cap per-canvas pixels so big pages don't exhaust phone memory
let zoom=1,fitW=0,pages=[];const visible=new Set();
const box=$('pages');
function measure(){const cs=getComputedStyle(box);return Math.min(box.clientWidth-parseFloat(cs.paddingLeft)-parseFloat(cs.paddingRight),1000)}
function layout(){fitW=measure();for(const p of pages){const w=fitW*zoom;p.div.style.width=w+'px';p.div.style.height=(w*p.h/p.w)+'px'}}
async function render(p){
 const key=fitW*zoom;if(p.key===key)return;p.key=key;if(p.task){p.task.cancel();p.task=null}
 const css=key/p.w,dpr=Math.min(window.devicePixelRatio||1,2);let scale=css*dpr;
 if(p.w*p.h*scale*scale>MAX_PX)scale=Math.sqrt(MAX_PX/(p.w*p.h));
 const vp=p.page.getViewport({scale});const c=document.createElement('canvas');c.width=Math.floor(vp.width);c.height=Math.floor(vp.height);
 const task=p.page.render({canvas:c,viewport:vp});p.task=task;
 try{await task.promise}catch(e){if(!(e instanceof pdfjs.RenderingCancelledException))p.cv.textContent='Page failed to render: '+e.message;return}
 if(p.task!==task)return;p.cv.replaceChildren(c);p.task=null;
 // selectable text at CSS scale, laid over the canvas
 p.div.style.setProperty('--total-scale-factor',css);
 const tl=document.createElement('div');tl.className='textLayer';
 try{await new pdfjs.TextLayer({textContentSource:p.page.streamTextContent(),container:tl,viewport:p.page.getViewport({scale:css})}).render()}catch(e){return}
 if(p.key===key){p.tl?.remove();p.tl=tl;p.div.append(tl)}}
function release(p){if(p.task){p.task.cancel();p.task=null}p.key=null;p.cv.replaceChildren();p.tl?.remove();p.tl=null}
function current(){let best=1;for(let i=0;i<pages.length;i++){if(pages[i].div.getBoundingClientRect().top<window.innerHeight/2)best=i+1;else break}$('pg').textContent=`${best} / ${pages.length}`}
function rezoom(z){const f=(window.scrollY+window.innerHeight/2)/document.body.scrollHeight;zoom=Math.min(4,Math.max(.5,z));layout();
 window.scrollTo(window.scrollX,f*document.body.scrollHeight-window.innerHeight/2);for(const p of visible)render(p);current()}
$('in').onclick=()=>rezoom(zoom*1.25);$('out').onclick=()=>rezoom(zoom/1.25);$('fit').onclick=()=>rezoom(1);
let rt;window.addEventListener('resize',()=>{clearTimeout(rt);rt=setTimeout(()=>{if(Math.abs(measure()-fitW)>20)rezoom(zoom)},200)});
let ticking=false;window.addEventListener('scroll',()=>{if(!ticking){ticking=true;requestAnimationFrame(()=>{current();ticking=false})}},{passive:true});

// ---- comments: highlights are stored as fractions of the page, so they survive zooming
let C={threads:[],asks:[],choices:[],owner:'',version:''},ACTIVE=null,PENDING=null,NEW=null,SHOWRES=false,ASKTO=null;const drafts={};
const fmt=t=>new Date(t*1000).toISOString().slice(5,16).replace('T',' ');
const waitingT=t=>t.status==='open'&&t.comments.length&&t.comments.at(-1).by==='user';
async function loadC(){try{const r=await fetch('/api/comments?id='+encodeURIComponent(id),{cache:'no-store'});if(!r.ok)return;C=await r.json()}catch(e){return}drawHL();drawPanel()}
async function post(op,extra={}){let j;try{const r=await fetch('/api/comments',{method:'POST',headers:{'Content-Type':'application/json','X-Token':TOKEN},body:JSON.stringify({id,op,...extra})});
 try{j=await r.json()}catch(e){j={ok:false,message:'Request failed ('+r.status+')'}}}catch(e){j={ok:false,message:'Request failed: '+e}}
 document.activeElement?.blur?.();await loadC();return j}
function drawHL(){pages.forEach((p,i)=>{const hs=C.threads.filter(t=>t.page===i+1&&(t.status==='open'||SHOWRES||t.id===ACTIVE));
 p.hls.replaceChildren(...hs.flatMap(t=>t.rects.map(r=>{const d=el('div',{class:'hl'+(t.status==='resolved'?' res':'')+(t.id===ACTIVE?' act':'')});
  Object.assign(d.style,{left:r[0]*100+'%',top:r[1]*100+'%',width:r[2]*100+'%',height:r[3]*100+'%'});return d})))})}
function bt(label,fn,cls=''){const b=el('button',{type:'button',class:cls},label);b.onclick=async()=>{b.disabled=true;try{await fn()}finally{b.disabled=false}};return b}
function card(t){
 const c=el('div',{class:'card'+(t.id===ACTIVE?' act':'')+(t.status==='resolved'?' res':''),'data-t':t.id});
 const hd=el('div',{class:'hd'},el('b',{},t.page?`Page ${t.page}`:'General'),
  t.status==='resolved'?el('span',{class:'chip'},'resolved'):waitingT(t)?el('span',{class:'chip w'},'waiting for a reply'):el('span',{class:'chip ok'},'answered'));
 if(t.version&&C.version&&t.version!==C.version)hd.append(el('span',{class:'chip',title:'The PDF has changed since this comment; the highlight may not line up.'},'earlier version'));
 if(t.page){const a=el('a',{href:'#'},'Show');a.onclick=e=>{e.preventDefault();ACTIVE=t.id;drawHL();if(innerWidth<700)$('panel').hidden=true;scrollToThread(t.id)};hd.append(a)}
 c.append(hd);if(t.quote)c.append(el('blockquote',{},t.quote));
 for(const m of t.comments)c.append(el('div',{class:'c'+(m.by==='user'?'':' ag')},el('div',{class:'who'},`${m.name} · ${fmt(m.t)} UTC`),el('div',{class:'tx'},m.text)));
 const ta=el('textarea',{rows:'2',placeholder:'Reply…'});ta.value=drafts[t.id]||'';ta.oninput=()=>{drafts[t.id]=ta.value};
 const msg=el('div',{class:'note err'});
 c.append(ta,el('div',{class:'row'},
  bt('Reply',async()=>{const text=ta.value.trim();if(!text)return;const j=await post('reply',{thread:t.id,text});if(j.ok)delete drafts[t.id];else msg.textContent=j.message}),
  bt(t.status==='open'?'Resolve':'Reopen',()=>post(t.status==='open'?'resolve':'reopen',{thread:t.id})),
  bt('Delete',async()=>{if(confirm('Delete this comment thread?'))await post('delete',{thread:t.id})},'danger')),msg);
 return c}
function drawPanel(){
 const open=C.threads.filter(t=>t.status==='open').length,wait=C.threads.filter(waitingT).length;
 $('cbtn').textContent=open?`Comments (${open})`:'Comments';
 if($('panel').hidden)return;
 const ae=document.activeElement;if(ae&&(ae.tagName==='TEXTAREA'||ae.tagName==='SELECT')&&$('panel').contains(ae))return;   // don't redraw under someone typing
 const ask=[],ch=C.choices||[];
 if(wait&&!ch.length)ask.push(el('div',{class:'note'},'No agents are registered, so nobody can be asked yet. In a Claude Code session, run ',el('code',{},C.register),'.'));
 else if(wait){   // the PDF's own agent is the default; otherwise the last one asked
  if(ASKTO===null){try{ASKTO=localStorage.getItem('askto')||''}catch(e){ASKTO=''}}
  const sel=el('select',{'aria-label':'Agent to ask'},el('option',{value:''},'Choose an agent…'),...ch.map(c=>el('option',{value:c.name},c.label)));
  sel.value=C.owner&&ch.some(c=>c.name===C.owner)?C.owner:ASKTO;if(sel.value!==ASKTO&&!C.owner)sel.value='';sel.onchange=()=>{ASKTO=sel.value};
  ask.push(el('div',{class:'row'},sel,bt(`Ask to respond (${wait})`,async()=>{if(!sel.value){$('askmsg').textContent='Choose which agent to ask.';return}
   const name=sel.selectedOptions[0].textContent;if(!confirm(`Send ${wait} waiting comment${wait==1?'':'s'} to ${name}?`))return;
   try{localStorage.setItem('askto',sel.value)}catch(e){}
   const j=await post('ask',{to:sel.value});$('askmsg')&&($('askmsg').textContent=j.message)},'primary')))}
 else ask.push(el('div',{class:'note'},C.threads.length?'No comments are waiting for a reply.':'Select text in the PDF and tap “Comment on selection”, or add a general comment. Then ask an agent to respond.'));
 const last=(C.asks||[]).at(-1);
 ask.push(el('div',{class:'note'+(last&&last.status==='failed'?' err':''),id:'askmsg'},last?`Last request ${fmt(last.t)} UTC to ${last.label||last.to}: ${last.note||last.status}`:''));
 if(last&&last.prompt&&last.status==='manual'){   // no automatic delivery: a fixed message to paste into the agent's chat
  const ta=el('textarea',{rows:'3',readonly:''});ta.value=last.prompt;
  ask.push(ta,el('div',{class:'row'},bt('Copy',async()=>{ta.select();let ok=false;try{ok=document.execCommand('copy')}catch(e){}
   $('askmsg').textContent=ok?'Copied. Paste it into the agent’s chat.':'Select the text above and copy it.'})))}
 $('pask').replaceChildren(...ask);
 const nres=C.threads.filter(t=>t.status==='resolved').length;$('showres').parentElement.lastChild.textContent=` Show resolved (${nres})`;
 const list=C.threads.filter(t=>t.status==='open'||SHOWRES).sort((a,b)=>(a.page??1e9)-(b.page??1e9)||((a.rects[0]?.[1]??0)-(b.rects[0]?.[1]??0)));
 $('plist').replaceChildren(...list.map(card))}
function openPanel(){$('panel').hidden=false;drawPanel()}
function scrollToThread(tid){const t=C.threads.find(x=>x.id===tid);if(!t||!t.page||!pages[t.page-1])return;const r=pages[t.page-1].div.getBoundingClientRect();
 window.scrollTo({top:window.scrollY+r.top+(t.rects[0]?.[1]||0)*r.height-120,behavior:'smooth'})}
function openThread(tid){ACTIVE=tid;openPanel();drawHL();document.querySelector(`.card[data-t="${tid}"]`)?.scrollIntoView({block:'nearest',behavior:'smooth'})}
$('cbtn').onclick=()=>{if($('panel').hidden)openPanel();else $('panel').hidden=true};$('pclose').onclick=()=>{$('panel').hidden=true};
$('showres').onchange=e=>{SHOWRES=e.target.checked;drawHL();drawPanel()};
box.addEventListener('click',e=>{const s=getSelection();if(s&&!s.isCollapsed)return;const pd=e.target.closest('.page');if(!pd)return;
 const i=+pd.dataset.i,b=pd.getBoundingClientRect(),x=(e.clientX-b.left)/b.width,y=(e.clientY-b.top)/b.height;
 const t=C.threads.find(t=>t.page===i+1&&(t.status==='open'||SHOWRES)&&t.rects.some(r=>x>=r[0]&&x<=r[0]+r[2]&&y>=r[1]-.003&&y<=r[1]+r[3]+.003));if(t)return openThread(t.id);
 if(T.on)ttsClick(i,x,y)});

// ---- read aloud: the Piper read-along service through /api/tts. One sentence per clip, two prefetched; the
// highlight follows audio.currentTime (the real playback clock), so speed changes keep it in step.
const T={on:false,pages:null,version:null,queue:[],cur:null,nextPos:null,playing:false,speed:1,gen:0,fetching:0,hlw:null,userScroll:0};
const audio=new Audio();audio.preservesPitch=true;
const tstat=(msg,err=false)=>{$('tstat').textContent=msg;$('tstat').className='note'+(err?' err':'')};
const tbtn=()=>{$('tplay').textContent=T.playing?'Pause':'Play'};
async function ttsWords(){if(T.pages)return T.pages;const r=await fetch('/api/tts/words?id='+encodeURIComponent(id),{cache:'no-store'});
 const j=await r.json();if(!r.ok)throw new Error(j.error||('error '+r.status));T.pages=j.pages;T.version=j.version;return T.pages}
async function fetchClip(pos,gen){const r=await fetch(`/api/tts/sentence?id=${encodeURIComponent(id)}&page=${pos.page}&word=${pos.word}`,{cache:'no-store'});
 const j=await r.json();if(!r.ok)throw new Error(j.error||('error '+r.status));if(gen!==T.gen)return null;
 if(j.version&&j.version!==T.version){T.pages=null;await ttsWords()}      // the PDF changed: refetch word boxes
 let src=null;if(j.audio_wav_b64){const b=atob(j.audio_wav_b64),u=new Uint8Array(b.length);for(let k=0;k<b.length;k++)u[k]=b.charCodeAt(k);
  src=URL.createObjectURL(new Blob([u],{type:'audio/wav'}))}
 return {src,words:j.words||[],next:j.next,page:pos.page}}
async function pump(gen){if(gen!==T.gen||T.fetching===gen)return;T.fetching=gen;
 try{while(gen===T.gen&&T.nextPos&&T.queue.length<2){const c=await fetchClip(T.nextPos,gen);if(!c||gen!==T.gen)return;
   T.queue.push(c);T.nextPos=c.next;if(!T.cur&&T.playing)playNext(gen)}}
 catch(e){if(gen===T.gen){tstat(e.message,true);if(!T.cur){T.playing=false;tbtn()}}}
 finally{if(T.fetching===gen)T.fetching=0}}
function playNext(gen){if(gen!==T.gen)return;if(T.cur&&T.cur.src)URL.revokeObjectURL(T.cur.src);T.cur=T.queue.shift()||null;
 if(!T.cur){if(!T.nextPos&&T.fetching!==gen){T.playing=false;tbtn();drawWord(null);tstat('Finished.')}else tstat('Loading…');return}
 pump(gen);
 if(!T.cur.src||!T.cur.words.length||T.cur.words.at(-1).end<=0)return setTimeout(()=>playNext(gen),0);   // nothing speakable
 audio.src=T.cur.src;audio.playbackRate=T.speed;audio.preservesPitch=true;
 audio.play().then(()=>tstat(`Reading page ${T.cur.page+1}`)).catch(e=>{T.playing=false;tbtn();tstat('Tap Play to start the audio.',true)})}
audio.onended=()=>playNext(T.gen);
function startAt(page,word){T.gen++;audio.pause();for(const c of T.queue)c.src&&URL.revokeObjectURL(c.src);T.queue=[];
 if(T.cur&&T.cur.src)URL.revokeObjectURL(T.cur.src);T.cur=null;T.nextPos={page,word};T.fetching=0;T.playing=true;tbtn();tstat('Loading…');pump(T.gen)}
function drawWord(w){for(const p of pages)p.ttsl.replaceChildren();if(!w||!T.pages)return;
 const pg=T.pages[w.page],wd=pg&&(pg.words[w.i]?.i===w.i?pg.words[w.i]:pg.words.find(x=>x.i===w.i));if(!wd||!pages[w.page])return;
 const [x0,y0,x1,y1]=wd.bbox,d=el('div',{class:'ttsw'});
 Object.assign(d.style,{left:x0/pg.width*100+'%',top:y0/pg.height*100+'%',width:(x1-x0)/pg.width*100+'%',height:(y1-y0)/pg.height*100+'%'});
 pages[w.page].ttsl.append(d);
 if(Date.now()-T.userScroll>4000){const r=d.getBoundingClientRect(),top=60,bot=window.innerHeight-110;   // follow along unless the reader is scrolling
  if(r.top<top||r.bottom>bot)window.scrollTo({top:window.scrollY+r.top-window.innerHeight*0.35,behavior:'smooth'})}}
(function hlTick(){requestAnimationFrame(hlTick);if(!T.cur)return;const t=audio.currentTime;
 const w=T.cur.words.find(w=>t>=w.start&&t<w.end)||null;if(w===T.hlw)return;T.hlw=w;if(w)drawWord(w)})();
async function ttsClick(i,fx,fy){let P;try{P=await ttsWords()}catch(e){return tstat(e.message,true)}
 const pg=P[i];if(!pg||!pg.words.length)return;const x=fx*pg.width,y=fy*pg.height;let best=null,bd=1e9;
 for(const w of pg.words){const [x0,y0,x1,y1]=w.bbox,dx=Math.max(x0-x,0,x-x1),dy=Math.max(y0-y,0,y-y1),dd=Math.hypot(dx,dy);if(dd<bd){bd=dd;best=w}}
 if(best&&bd<30)startAt(i,best.i)}
async function ttsOpen(){T.on=true;document.body.classList.add('tts');$('ttsbar').hidden=false;$('rbtn').classList.add('primary');
 tstat('Starting the speech service…');try{await ttsWords();tstat('Tap a word to read from there, or Play to start at this page.')}catch(e){tstat(e.message,true)}}
function ttsClose(){T.gen++;audio.pause();T.playing=false;T.cur=null;T.queue=[];T.on=false;drawWord(null);tbtn();
 document.body.classList.remove('tts');$('ttsbar').hidden=true;$('rbtn').classList.remove('primary')}
$('rbtn').onclick=()=>T.on?ttsClose():ttsOpen();$('tclose').onclick=ttsClose;
$('tplay').onclick=()=>{if(T.playing){audio.pause();T.playing=false;tbtn();tstat('Paused.');return}
 if(T.cur&&T.cur.src&&!audio.ended){T.playing=true;tbtn();audio.play().catch(()=>{});tstat(`Reading page ${T.cur.page+1}`);return}
 startAt(Math.max(0,parseInt($('pg').textContent)-1||0),0)};
$('tspeed').onchange=e=>{T.speed=+e.target.value;audio.playbackRate=T.speed};
for(const ev of ['wheel','touchmove','keydown'])window.addEventListener(ev,()=>{T.userScroll=Date.now()},{passive:true});
if('mediaSession' in navigator){navigator.mediaSession.setActionHandler('play',()=>{if(!T.playing)$('tplay').click()});
 navigator.mediaSession.setActionHandler('pause',()=>{if(T.playing)$('tplay').click()})}
let st;document.addEventListener('selectionchange',()=>{clearTimeout(st);st=setTimeout(checkSel,250)});
function checkSel(){const s=getSelection(),hide=()=>{PENDING=null;$('selbar').hidden=true};
 if(!s||!s.rangeCount||s.isCollapsed)return hide();
 const r=s.getRangeAt(0),n=r.startContainer,pd=(n.nodeType===1?n:n.parentElement)?.closest('.page');if(!pd||!box.contains(pd))return hide();
 const b=pd.getBoundingClientRect();
 const rs=[...r.getClientRects()].map(q=>({l:Math.max(q.left,b.left),t:Math.max(q.top,b.top),r:Math.min(q.right,b.right),b:Math.min(q.bottom,b.bottom)}))
  .filter(q=>q.r-q.l>1&&q.b-q.t>1&&q.b-q.t<b.height*.08).sort((a,c)=>a.t-c.t||a.l-c.l);
 const m=[];for(const q of rs){const L=m.at(-1);if(L&&Math.abs(q.t-L.t)<(L.b-L.t)*.5&&q.l<=L.r+(L.b-L.t)){L.r=Math.max(L.r,q.r);L.b=Math.max(L.b,q.b);L.t=Math.min(L.t,q.t)}else m.push({...q})}
 const quote=s.toString().replace(/\s+/g,' ').trim();if(!m.length||!quote)return hide();
 PENDING={page:+pd.dataset.i+1,quote:quote.slice(0,2000),rects:m.map(q=>[(q.l-b.left)/b.width,(q.t-b.top)/b.height,(q.r-q.l)/b.width,(q.b-q.t)/b.height].map(v=>+v.toFixed(5)))};
 $('selbar').hidden=false}
function newComment(sel){NEW=sel;$('nq').textContent=sel.quote;$('nq').hidden=!sel.quote;$('nt').value='';$('nmsg').textContent='';$('newc').showModal();$('nt').focus()}
$('selbtn').addEventListener('pointerdown',e=>e.preventDefault());   // keep the selection while tapping
$('selbtn').onclick=()=>{if(PENDING)newComment({...PENDING})};
$('general').onclick=()=>newComment({page:null,rects:[],quote:''});
$('ncancel').onclick=()=>$('newc').close();
$('nsave').onclick=async e=>{e.preventDefault();const text=$('nt').value.trim();if(!text){$('nmsg').textContent='Write a comment first.';return}
 $('nsave').disabled=true;const j=await post('add',{...NEW,text});$('nsave').disabled=false;
 if(!j.ok){$('nmsg').textContent=j.message;return}$('newc').close();getSelection().removeAllRanges();$('selbar').hidden=true;openPanel()};
setInterval(()=>{if(!document.hidden)loadC()},20000);

async function main(){
 let doc;try{doc=await pdfjs.getDocument({url,cMapUrl:'/pdfjs/cmaps/',cMapPacked:true,standardFontDataUrl:'/pdfjs/standard_fonts/',
  wasmUrl:'/pdfjs/wasm/',iccUrl:'/pdfjs/iccs/',disableRange:true}).promise}
 catch(e){$('title').textContent='Could not open this PDF';box.replaceChildren(el('p',{class:'err'},e.message));return}
 for(let i=1;i<=doc.numPages;i++){const page=await doc.getPage(i);const v=page.getViewport({scale:1});
  const div=el('div',{class:'page'}),cv=el('div',{class:'cv'}),hls=el('div',{class:'hls'}),ttsl=el('div',{class:'ttsl'});div.append(cv,hls,ttsl);
  pages.push({page,div,cv,hls,ttsl,tl:null,w:v.width,h:v.height,key:null,task:null});box.append(div)}
 layout();
 const io=new IntersectionObserver(es=>{for(const e of es){const p=pages[+e.target.dataset.i];if(e.isIntersecting){visible.add(p);render(p)}else{visible.delete(p);release(p)}}},{rootMargin:'150% 0px'});
 pages.forEach((p,i)=>{p.div.dataset.i=i;io.observe(p.div)});current();loadC()}
main();
</script></body></html>
"""


# ---------------------------------------------------------------- start-up

def is_loopback(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def main():
    global FOLDERS, TITLE, PASSWORD, HOSTS
    sub = sys.argv[1] if len(sys.argv) > 1 else ""
    if sub in ("comments", "agent", "inbox-watch"):
        return {"comments": comments_main, "agent": agent_main, "inbox-watch": inbox_main}[sub](sys.argv[2:])
    ap = argparse.ArgumentParser(description="Serve a page for reading, commenting on and listening to the PDFs in "
                                             "some folders, and asking Claude Code sessions to answer the comments.",
                                 epilog="Also: report_viewer.py agent --help, report_viewer.py comments --help")
    ap.add_argument("folders", nargs="*", metavar="[LABEL=]FOLDER",
                    help="folders to list (searched recursively); the section is named LABEL or the folder's name")
    ap.add_argument("--host", default="127.0.0.1", help="address to listen on (default 127.0.0.1, this computer only)")
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--title", default="Reports", help="heading on the list page")
    ap.add_argument("--data", default=default_data(), help="where comments, uploads and agents are kept (default ~/.report-viewer)")
    ap.add_argument("--no-uploads", action="store_true", help="hide the Upload PDF section")
    ap.add_argument("--max-upload-mb", type=int, default=100)
    ap.add_argument("--allow-host", action="append", default=[], metavar="HOST[:PORT]",
                    help="extra Host header to accept when listening on 127.0.0.1 (e.g. behind a reverse proxy)")
    ap.add_argument("--no-auth", action="store_true",
                    help="allow listening on a network address without REPORT_VIEWER_PASSWORD (anyone who can reach "
                         "the port can read, comment, upload and wake your agents)")
    a = ap.parse_args()
    configure(a.data, uploads=not a.no_uploads, max_mb=a.max_upload_mb)
    TITLE = a.title
    for spec in a.folders:
        label, sep, folder = spec.partition("=")
        if not sep or not label.strip():
            label, folder = "", spec
        p = Path(folder).expanduser().resolve()
        if not p.is_dir():
            sys.exit(f"report_viewer: not a folder: {folder}")
        FOLDERS.append((label.strip() or p.name or str(p), p))
    PASSWORD = os.environ.get("REPORT_VIEWER_PASSWORD") or None
    if is_loopback(a.host):
        HOSTS = {f"{h}:{a.port}" for h in ("127.0.0.1", "localhost", "[::1]")} | {h.lower() for h in a.allow_host}
    elif PASSWORD is None and not a.no_auth:
        sys.exit("report_viewer: listening on a network address needs a password: set REPORT_VIEWER_PASSWORD "
                 "(or pass --no-auth if you really want it open to anyone who can reach the port)")
    srv = Server((a.host, a.port), Handler)
    atexit.register(tts_stop)          # don't leave the read-aloud child running
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    shown = f"[{a.host}]" if ":" in a.host else a.host
    print(f"report viewer on http://{shown}:{a.port}/  ({len(FOLDERS)} folder(s), {len(agents())} agent(s); data in "
          f"{tilde(DATA)}; read-aloud {'ready' if tts_installed() else 'not installed'}"
          f"{'; password required' if PASSWORD else ''})  Ctrl-C to stop", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())

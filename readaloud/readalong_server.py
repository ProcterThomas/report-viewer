#!/usr/bin/env python3
"""Local text-to-speech service for read-along in a web PDF viewer.

Serves the same word extraction and phoneme-aligned narration as the
desktop read-along (pdf_align.py), one utterance per request, over a small
JSON API meant to sit behind a proxy that does auth:

  GET /health
      {"ok": true, "model_loaded": true}
  GET /words?pdf=<abs path>
      {"version", "pages": [{"width", "height",
                             "words": [{"i", "text", "bbox", "is_equation"}]}]}
      bbox is [x0, y0, x1, y1] in points on the page as displayed (top-left
      origin, y down, page rotation applied); scale by viewport width/width.
  GET /sentence?pdf=<abs path>&page=<p>&word=<i>
      {"version", "sample_rate", "audio_wav_b64",
       "words": [{"page", "i", "start", "end"}], "next": {"page", "word"} | null}
      One utterance starting at that word; times are seconds into the clip.

Errors are JSON {"error": ...}; a 413 for an utterance too long to
synthesize within the memory budget also carries "next", to skip past it.
Only PDFs under --allow-root directories are read. Synthesis is serialized
behind one lock: Piper phonemizes with espeak-ng, which is not thread-safe.
"""
from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import io
import json
import logging
import sys
import threading
import time
import wave
from collections import OrderedDict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pymupdf

from pdf_align import PageWords, extract_pages, load_voice, narrate_words, utterance_end

REPO_DIR = Path(__file__).resolve().parent
DEFAULT_VOICE_DIR = REPO_DIR / "voices"
DEFAULT_VOICE = "en_US-lessac-medium"
MAX_UTTERANCE_WORDS = 40
# Piper's peak memory grows with utterance length: ~650 MB RSS at 370
# phonemes and ~950 MB at 620 (measured, this voice). The test manuscript's
# longest capped utterance is 361 phonemes; 500 keeps peaks well inside a
# 1200 MB memory limit.
MAX_UTTERANCE_PHONEMES = 500
DOC_CACHE_SIZE = 4

try:
    _libc = ctypes.CDLL("libc.so.6")
except OSError:  # not glibc
    _libc = None


def release_freed_memory() -> None:
    """Hand freed heap back to the OS. Utterances vary in size, so without
    this glibc keeps the high-water mark of every size seen (measured over a
    whole-document walk: peak 1075 MB with ONNX's arena, 945 MB without,
    866 MB without plus this; resting 447 MB)."""
    if _libc is not None:
        _libc.malloc_trim(0)
CLIP_CACHE_SIZE = 64

log = logging.getLogger("readalong")


class RequestError(Exception):
    def __init__(self, status: HTTPStatus, message: str, **extra) -> None:
        super().__init__(message)
        self.status = status
        self.body = {"error": message, **extra}


class Document:
    def __init__(self, version: str, pages: list[PageWords], payload: bytes) -> None:
        self.version = version
        self.pages = pages
        self.payload = payload

    def next_position(self, page: int, word: int) -> dict | None:
        """The first word at or after (page, word), skipping pages with no words."""
        while page < len(self.pages):
            if word < len(self.pages[page].words):
                return {"page": page, "word": word}
            page, word = page + 1, 0
        return None


class ReadAlongService:
    def __init__(self, voice, allow_roots: list[Path]) -> None:
        self.voice = voice
        self.allow_roots = [root.resolve() for root in allow_roots]
        # One lock for all PDF and synthesis work: espeak-ng (via Piper) and
        # PyMuPDF are both unsafe to use from several threads at once.
        self.lock = threading.Lock()
        self.docs: OrderedDict[str, Document] = OrderedDict()
        self.clips: OrderedDict[tuple, bytes] = OrderedDict()

    def resolve_pdf(self, raw: str | None) -> Path:
        if not raw:
            raise RequestError(HTTPStatus.BAD_REQUEST, "missing pdf parameter")
        path = Path(raw)
        if not path.is_absolute():
            raise RequestError(HTTPStatus.BAD_REQUEST, "pdf must be an absolute path")
        path = path.resolve()
        if not any(path.is_relative_to(root) for root in self.allow_roots):
            raise RequestError(HTTPStatus.FORBIDDEN, "pdf is outside the allowed roots")
        if path.suffix.lower() != ".pdf" or not path.is_file():
            raise RequestError(HTTPStatus.NOT_FOUND, "no such pdf")
        return path

    def document(self, path: Path) -> Document:
        """Words for `path`, cached per (path, mtime, size). Call under self.lock."""
        stat = path.stat()
        version = hashlib.sha1(f"{path}|{stat.st_mtime_ns}|{stat.st_size}".encode()).hexdigest()[:16]
        key = str(path)
        cached = self.docs.get(key)
        if cached is not None and cached.version == version:
            self.docs.move_to_end(key)
            return cached

        pages = extract_pages(path)
        payload_pages = []
        with pymupdf.open(str(path)) as pdf:
            for page_words, page in zip(pages, pdf):
                # Word boxes come in unrotated page space; map them onto the
                # page as displayed, which is what a viewer draws.
                matrix = page.rotation_matrix
                words = []
                for i, w in enumerate(page_words.words):
                    rect = pymupdf.Rect(w.bbox) * matrix
                    words.append(
                        {
                            "i": i,
                            "text": w.text,
                            "bbox": [round(v, 2) for v in (rect.x0, rect.y0, rect.x1, rect.y1)],
                            "is_equation": w.is_equation,
                        }
                    )
                payload_pages.append(
                    {"width": round(page.rect.width, 2), "height": round(page.rect.height, 2), "words": words}
                )
        payload = json.dumps({"version": version, "pages": payload_pages}).encode()
        doc = Document(version, pages, payload)
        self.docs[key] = doc
        while len(self.docs) > DOC_CACHE_SIZE:
            self.docs.popitem(last=False)
        return doc

    def words(self, query: dict) -> bytes:
        path = self.resolve_pdf(query.get("pdf"))
        with self.lock:
            return self.document(path).payload

    def sentence(self, query: dict) -> bytes:
        path = self.resolve_pdf(query.get("pdf"))
        try:
            page_no, start = int(query.get("page", "")), int(query.get("word", ""))
        except ValueError:
            raise RequestError(HTTPStatus.BAD_REQUEST, "page and word must be integers") from None
        with self.lock:
            doc = self.document(path)
            if not (0 <= page_no < len(doc.pages)) or not (0 <= start < len(doc.pages[page_no].words)):
                raise RequestError(HTTPStatus.BAD_REQUEST, "page/word out of range")
            key = (str(path), doc.version, page_no, start)
            cached = self.clips.get(key)
            if cached is not None:
                self.clips.move_to_end(key)
                return cached
            try:
                body = json.dumps(self._synthesize(doc, page_no, start)).encode()
            finally:
                release_freed_memory()
            self.clips[key] = body
            while len(self.clips) > CLIP_CACHE_SIZE:
                self.clips.popitem(last=False)
            return body

    def _synthesize(self, doc: Document, page_no: int, start: int) -> dict:
        words = doc.pages[page_no].words
        end = utterance_end(words, start, MAX_UTTERANCE_WORDS)
        # Refuse rather than grow: narrow the window until the utterance fits
        # the phoneme budget; a single word that alone exceeds it is skipped.
        while True:
            sentences = [s for s in self.voice.phonemize(" ".join(w.text for w in words[start:end])) if s]
            if not sentences or len(sentences[0]) <= MAX_UTTERANCE_PHONEMES:
                break
            if end - start == 1:
                raise RequestError(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    "utterance too long to synthesize",
                    next=doc.next_position(page_no, start + 1),
                )
            end = start + (end - start) // 2

        narration = narrate_words(self.voice, words[start:end])
        try:
            chunk = next(narration)
        finally:
            narration.close()
        n = max(len(chunk.words), 1)
        timed = chunk.words or []
        out_words = [
            {"page": page_no, "i": start + k, "start": round(w.start, 4), "end": round(w.end, 4)}
            for k, w in enumerate(timed)
        ]
        if not out_words:
            duration = len(chunk.audio) / 2 / chunk.sample_rate
            out_words = [{"page": page_no, "i": start, "start": 0.0, "end": round(duration, 4)}]

        wav = io.BytesIO()
        with wave.open(wav, "wb") as f:
            f.setnchannels(1)
            f.setsampwidth(2)
            f.setframerate(chunk.sample_rate)
            f.writeframes(chunk.audio)
        return {
            "version": doc.version,
            "sample_rate": chunk.sample_rate,
            "audio_wav_b64": base64.b64encode(wav.getvalue()).decode("ascii"),
            "words": out_words,
            "next": doc.next_position(page_no, start + n),
        }


def make_handler(service: ReadAlongService, activity: dict):
    class Handler(BaseHTTPRequestHandler):
        server_version = "readalong"

        def do_GET(self) -> None:  # noqa: N802
            started = time.monotonic()
            with activity["lock"]:
                activity["busy"] += 1
            url = urlparse(self.path)
            status = HTTPStatus.OK
            try:
                query = {k: v[0] for k, v in parse_qs(url.query).items()}
                if url.path == "/health":
                    body = json.dumps({"ok": True, "model_loaded": service.voice is not None}).encode()
                elif url.path == "/words":
                    body = service.words(query)
                elif url.path == "/sentence":
                    body = service.sentence(query)
                else:
                    raise RequestError(HTTPStatus.NOT_FOUND, "unknown endpoint")
            except RequestError as error:
                status, body = error.status, json.dumps(error.body).encode()
            except Exception as error:  # noqa: BLE001
                log.exception("internal error on %s", url.path)
                status, body = HTTPStatus.INTERNAL_SERVER_ERROR, json.dumps({"error": f"internal error: {error}"}).encode()
            finally:
                with activity["lock"]:
                    activity["busy"] -= 1
                    activity["last"] = time.monotonic()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            # Endpoint, status and timing only: query strings carry PDF paths.
            log.info("%s %d %.0fms", url.path, status, (time.monotonic() - started) * 1000)

        def log_message(self, format, *args) -> None:  # noqa: A002
            pass  # replaced by the path-free line above

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8091)
    parser.add_argument("--threads", type=int, default=4, help="ONNX CPU threads (default 4)")
    parser.add_argument("--allow-root", action="append", type=Path, required=True,
                        help="directory whose PDFs may be read (repeatable)")
    parser.add_argument("--idle-exit", type=float, default=0,
                        help="exit (status 0) after this many seconds with no requests; 0 = never")
    parser.add_argument("--voice", default=DEFAULT_VOICE)
    parser.add_argument("--voice-dir", type=Path, default=DEFAULT_VOICE_DIR)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)

    model = args.voice_dir / f"{args.voice}.onnx"
    config = args.voice_dir / f"{args.voice}.onnx.json"
    if not model.is_file() or not config.is_file():
        parser.error(f"voice files not found: {model} / {config}")
    for root in args.allow_root:
        if not root.is_dir():
            parser.error(f"--allow-root is not a directory: {root}")

    started = time.monotonic()
    voice = load_voice(model, config, intra_op_threads=args.threads, cpu_mem_arena=False)
    log.info("voice loaded in %.1fs", time.monotonic() - started)

    service = ReadAlongService(voice, args.allow_root)
    activity = {"busy": 0, "last": time.monotonic(), "lock": threading.Lock()}
    server = ThreadingHTTPServer((args.host, args.port), make_handler(service, activity))
    server.daemon_threads = True

    if args.idle_exit > 0:
        def watch_idle() -> None:
            while True:
                time.sleep(min(5.0, args.idle_exit / 4))
                with activity["lock"]:
                    idle = activity["busy"] == 0 and time.monotonic() - activity["last"] > args.idle_exit
                if idle:
                    log.info("idle for %.0fs, exiting", args.idle_exit)
                    server.shutdown()
                    return

        threading.Thread(target=watch_idle, daemon=True).start()

    log.info("listening on %s:%d", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

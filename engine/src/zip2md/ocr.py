"""OCR back-ends.

Primary: Apple Vision via the bundled ``ocr-helper`` (on-device, fast, supports
Indonesian, detects paragraphs/lists/tables). It runs as long-lived worker
processes speaking JSON lines, so the Vision model is loaded only once.

Reliability:
* every request has a time limit — a stuck worker is killed and the request is
  retried once on a fresh worker, so one bad page can never hang the whole run;
* the very first request runs alone, so a cold start (Vision compiling its model
  after an install/update) is not fought over by several processes at once;
* every worker first reads a built-in reference page (and again every
  ``SELFTEST_EVERY`` pages). Vision runs its models on the Neural Engine, compiled
  once per executable identity (~35 s the first time, by a system service). While
  that is not ready or has gone wrong, a helper returns a few stray words per page
  or never answers — with no error — although other programs read the same page
  perfectly. A worker that fails the test is restarted and re-tested until it
  passes (``STABILIZE_BUDGET``). Only if that never happens a copy of the helper
  under another name is tried: a different identity is not affected by the fault,
  but needs its own ~35 s compilation the first time, so it lives at a fixed place
  in ~/Library/Caches and is the last resort, not the first.

Fallback: Tesseract (if installed), used when the helper is missing.
"""

from __future__ import annotations

import itertools
import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .paths import cache_dir

# seconds; overridable for tests via ZIP2MD_OCR_TIMEOUT / ZIP2MD_OCR_COLD_TIMEOUT
REQUEST_TIMEOUT = float(os.environ.get("ZIP2MD_OCR_TIMEOUT", 90))             # a normal page
COLD_START_TIMEOUT = float(os.environ.get("ZIP2MD_OCR_COLD_TIMEOUT", 240))    # may include model compilation
SELFTEST_TIMEOUT = float(os.environ.get("ZIP2MD_OCR_SELFTEST_TIMEOUT", 45))
FIRST_SELFTEST_TIMEOUT = float(os.environ.get("ZIP2MD_OCR_FIRST_SELFTEST_TIMEOUT", 90))  # may compile
STABILIZE_BUDGET = float(os.environ.get("ZIP2MD_OCR_STABILIZE", 60))   # waiting for a failing helper
SELFTEST_EVERY = 25         # pages between two self-tests of the same worker
# A worker normally stays near 100 MB. One that grows past this (a leak in a system
# framework, a gigantic image) is retired after its current page and replaced, so
# memory can never pile up across a long run and push the Mac into swap.
WORKER_MEM_LIMIT_MB = int(os.environ.get("ZIP2MD_OCR_MEM_LIMIT_MB", 700))


@dataclass
class OCRResult:
    markdown: str
    confidence: float
    engine: str


class OCRError(Exception):
    pass


def find_helper() -> Path | None:
    env = os.environ.get("ZIP2MD_OCR_HELPER")
    candidates = [Path(env)] if env else []
    exe_dir = Path(sys.executable).resolve().parent
    candidates += [
        exe_dir / "ocr-helper",                          # frozen engine next to helper
        exe_dir.parent / "ocr-helper",
        exe_dir.parent / "MacOS" / "ocr-helper",         # inside Zip2Markdown.app
        exe_dir.parent.parent / "MacOS" / "ocr-helper",
    ]
    here = Path(__file__).resolve()
    for up in here.parents[:5]:
        candidates.append(up / "app" / ".build" / "release" / "ocr-helper")
    which = shutil.which("ocr-helper")
    if which:
        candidates.append(Path(which))
    for c in candidates:
        if c.is_file() and os.access(c, os.X_OK):
            return c
    return None


class _Worker:
    """One ``ocr-helper --serve`` process.

    Answers are read by a background thread into a queue, so waiting for one has a
    real time limit even if the process (or something it spawned) keeps the pipe open.
    """

    def __init__(self, helper: Path):
        self.proc = subprocess.Popen(
            [str(helper), "--serve"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", bufsize=1, start_new_session=True,
        )
        self._answers: queue.Queue[str | None] = queue.Queue()
        self.served = 0             # pages since the last self-test
        self.tested = False
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        try:
            for line in self.proc.stdout:           # type: ignore[union-attr]
                self._answers.put(line)
        except Exception:
            pass
        self._answers.put(None)                     # EOF: process died

    def alive(self) -> bool:
        return self.proc.poll() is None

    def kill(self) -> None:
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)   # the helper and anything it started
        except (ProcessLookupError, PermissionError, OSError):
            try:
                self.proc.kill()
            except Exception:
                pass

    def ask(self, req: dict, timeout: float) -> dict:
        assert self.proc.stdin
        try:
            self.proc.stdin.write(json.dumps(req) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as e:
            raise OCRError(f"ocr-helper terputus: {e}") from e
        deadline = time.monotonic() + timeout
        while True:
            try:
                line = self._answers.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                self.kill()
                raise OCRError(f"OCR melebihi batas waktu {int(timeout)} detik") from None
            if line is None:
                raise OCRError("ocr-helper berhenti tiba-tiba")
            try:
                resp = json.loads(line)
            except json.JSONDecodeError:
                continue                             # stray output — keep waiting
            if resp.get("id") == req.get("id"):      # ignore answers to abandoned requests
                return resp

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.wait(timeout=1)
        except Exception:
            self.kill()


class VisionOCR:
    name = "Apple Vision"
    renders_pdf = True          # the helper can rasterise PDF pages itself

    MAX_FAILURES_IN_A_ROW = 4   # after this OCR is considered broken: fail fast, don't wait

    def __init__(self, helper: Path, langs: list[str], fast: bool = False, workers: int = 3):
        self.helper = helper
        self.langs = langs
        self.fast = fast
        self.on_status = lambda message: None   # "" = back to normal
        self._pool: queue.Queue[_Worker | None] = queue.Queue()
        self._all: list[_Worker] = []
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._first_done = threading.Event()
        self._first_lock = threading.Lock()
        self._closed = False
        self.retries = 0
        self.unstable = 0           # times a worker failed the self-test or stopped answering
        self.suspect_pages = 0      # pages read by a worker that then failed a self-test
        self.untrusted = False      # nothing passed the self-test: results may be incomplete
        self._failures_in_a_row = 0
        self._executable = helper   # switched to the alternate copy as a last resort
        self._alternate_tried = False
        self._trouble_since: float | None = None
        self._selftest = os.environ.get("ZIP2MD_OCR_SELFTEST", "1") != "0"
        for _ in range(max(1, workers)):
            self._pool.put(None)        # started on first use: no OCR, no processes

    def recognize(self, image: Path, page: int | None = None, dpi: float | None = None,
                  frame: int | None = None) -> OCRResult:
        """OCR an image file, page ``page`` (0-based) of the PDF at ``image``, or
        frame ``frame`` of a multi-page TIFF."""
        req = {"path": str(image), "langs": self.langs, "fast": self.fast}
        if page is not None:
            req["page"] = page
            req["dpi"] = dpi or 300
        if frame:
            req["frame"] = frame
        if not self._first_done.is_set():
            with self._first_lock:                      # cold start: one request at a time
                if not self._first_done.is_set():
                    try:
                        return self._request(req, COLD_START_TIMEOUT, FIRST_SELFTEST_TIMEOUT)
                    finally:
                        self._first_done.set()
        return self._request(req, REQUEST_TIMEOUT, SELFTEST_TIMEOUT)

    # ---- workers

    def _alternate(self) -> Path | None:
        """A copy of the helper under another name, at a fixed place so that its
        one-time model compilation is kept for later runs."""
        folder = cache_dir()
        if folder is None:
            return None
        try:
            copy = folder / "ocr-helper-alt"
            src = self.helper.stat()
            if not copy.exists() or (copy.stat().st_size, int(copy.stat().st_mtime)) != (src.st_size, int(src.st_mtime)):
                tmp = folder / f"ocr-helper-alt.{os.getpid()}"
                shutil.copy2(self.helper, tmp)
                tmp.chmod(0o755)
                os.replace(tmp, copy)
            return copy
        except OSError:
            return None

    def _spawn(self, dead: _Worker | None = None) -> _Worker:
        if dead is not None:
            dead.kill()
        with self._lock:
            if self._closed:
                raise OCRError("OCR sudah ditutup")
            try:
                w = _Worker(self._executable)
            except OSError as e:
                raise OCRError(f"ocr-helper tidak bisa dijalankan: {e}") from e
            self._all = [x for x in self._all if x is not dead] + [w]
        return w

    def _passes_selftest(self, w: _Worker, timeout: float) -> bool:
        with self._lock:
            req = {"id": f"t{next(self._ids)}", "selftest": True, "langs": self.langs, "fast": self.fast}
        try:
            resp = w.ask(req, timeout)
        except OCRError:
            return False
        if resp.get("engine") != "selftest":
            return True                 # an older helper without the test: nothing to conclude
        return bool(resp.get("ok"))

    def _healthy(self, w: _Worker | None, timeout: float) -> _Worker:
        """A worker that has just proven it reads the reference page."""
        if w is not None and w.alive() and w.tested and w.served < SELFTEST_EVERY:
            return w
        if w is None or not w.alive():
            w = self._spawn(w)
        if not self._selftest:
            w.tested = True
            return w
        while True:
            pages = w.served
            if self._passes_selftest(w, timeout):
                w.tested, w.served = True, 0
                with self._lock:
                    recovered, self._trouble_since = self._trouble_since is not None, None
                if recovered:
                    self.on_status("")
                return w
            with self._lock:
                self.unstable += 1
                self.suspect_pages += pages
                if self._trouble_since is None:
                    self._trouble_since = time.monotonic()
                waited = time.monotonic() - self._trouble_since
                switch = waited >= STABILIZE_BUDGET and not self._alternate_tried
                give_up = waited >= STABILIZE_BUDGET and self._alternate_tried
                if switch:
                    self._alternate_tried = True
            if give_up:
                self.untrusted = True       # used anyway; the result says so
                w = self._spawn(w)
                w.tested, w.served = True, 0
                return w
            if switch:
                alt = self._alternate()
                if alt is not None:
                    self.on_status("Mesin OCR belum pulih — mencoba salinan cadangan (bisa ±1 menit)…")
                    self._executable = alt
                    timeout = max(timeout, FIRST_SELFTEST_TIMEOUT)
            else:
                self.on_status("Menunggu mesin OCR siap…")
                time.sleep(2)               # the model may still be compiling
            w = self._spawn(w)

    def _request(self, req: dict, timeout: float, selftest_timeout: float) -> OCRResult:
        if self._failures_in_a_row >= self.MAX_FAILURES_IN_A_ROW:
            raise OCRError("OCR dihentikan setelah gagal berulang kali")
        w = self._pool.get()
        try:
            if self._failures_in_a_row >= self.MAX_FAILURES_IN_A_ROW:   # broke while we queued
                raise OCRError("OCR dihentikan setelah gagal berulang kali")
            w = self._healthy(w, selftest_timeout)
            with self._lock:
                req = {**req, "id": str(next(self._ids))}
            try:
                resp = w.ask(req, timeout)
            except OCRError:
                # stuck or crashed worker: start a fresh one (which has to pass the
                # self-test again) and try this page once more
                with self._lock:
                    self.retries += 1
                w = self._spawn(w)
                try:
                    w = self._healthy(w, SELFTEST_TIMEOUT)
                    resp = w.ask(req, timeout)
                except OCRError:
                    with self._lock:
                        self._failures_in_a_row += 1
                    w = self._spawn(w)          # leave a fresh worker for the next page
                    raise
            self._failures_in_a_row = 0
            w.served += 1
            if int(resp.get("mem_mb") or 0) > WORKER_MEM_LIMIT_MB:
                self._retire(w)
                w = None                    # a fresh one is started on next use
            if not resp.get("ok"):
                raise OCRError(resp.get("error") or "OCR gagal")
            return OCRResult(resp.get("markdown", ""), float(resp.get("confidence", 0)),
                             resp.get("engine", "vision"))
        finally:
            self._pool.put(w)

    def _retire(self, w: _Worker) -> None:
        with self._lock:
            self._all = [x for x in self._all if x is not w]
        w.close()

    def close(self) -> None:
        with self._lock:
            self._closed = True         # no worker may be started from here on
            workers = list(self._all)
        for w in workers:
            w.close()


TESS_LANG = {"id": "ind", "en": "eng", "ms": "msa", "fr": "fra", "de": "deu", "es": "spa",
             "pt": "por", "it": "ita", "nl": "nld", "zh": "chi_sim", "ja": "jpn", "ko": "kor",
             "ar-SA": "ara", "ru": "rus", "th": "tha", "vi-VT": "vie", "tr": "tur"}


class TesseractOCR:
    name = "Tesseract"
    renders_pdf = False

    def __init__(self, exe: str, langs: list[str]):
        self.exe = exe
        try:
            out = subprocess.run([exe, "--list-langs"], capture_output=True, text=True).stdout
            have = set(out.split()[1:]) if out else set()
        except OSError:
            have = set()
        wanted = [TESS_LANG.get(l, l) for l in langs]
        use = [l for l in wanted if l in have] or (["eng"] if "eng" in have else [])
        self.lang = "+".join(use) or "eng"

    def recognize(self, image: Path, page: int | None = None, dpi: float | None = None,
                  frame: int | None = None) -> OCRResult:
        if frame:       # the caller re-encodes that frame to PNG and asks again
            raise OCRError("Cannot read image: frame of a multi-page file")
        try:
            p = subprocess.run([self.exe, str(image), "stdout", "-l", self.lang, "--psm", "3"],
                               capture_output=True, text=True, encoding="utf-8", timeout=REQUEST_TIMEOUT * 2)
        except subprocess.TimeoutExpired as e:
            raise OCRError("tesseract melebihi batas waktu") from e
        if p.returncode != 0:
            raise OCRError(p.stderr.strip()[:300] or "tesseract gagal")
        # Tesseract emits one line per text line; blank lines separate paragraphs.
        paras = [" ".join(l.strip() for l in block.splitlines() if l.strip())
                 for block in p.stdout.split("\n\n")]
        return OCRResult("\n\n".join(x for x in paras if x), 0.0, "tesseract")

    def close(self) -> None:
        pass


def make_ocr(langs: list[str], fast: bool, workers: int):
    helper = find_helper()
    if helper:
        return VisionOCR(helper, langs, fast, workers)
    tess = shutil.which("tesseract") or (
        "/opt/homebrew/bin/tesseract" if Path("/opt/homebrew/bin/tesseract").exists() else None)
    if tess:
        return TesseractOCR(tess, langs)
    return None

"""Command line interface.

    zip2md arsip.zip                       # -> arsip.md next to the ZIP
    zip2md arsip.zip -o hasil.md --force-ocr
    zip2md folder/ file.pdf -o gabungan.md
    zip2md arsip.zip --json                # JSON-lines progress (used by the Mac app)
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from pathlib import Path

from . import __version__
from .pipeline import Options, emergency_cleanup, run


def _human(event: dict) -> None:
    e = event["event"]
    if e == "stage":
        print(event["message"], file=sys.stderr)
    elif e == "start":
        print(f"{event['total']} file akan dikonversi (OCR: {event['ocr'] or 'tidak ada'})", file=sys.stderr)
    elif e == "file":
        icon = {"ok": "✓", "ocr": "✓", "error": "✗", "skipped": "–"}[event["status"]]
        extra = f" [{event['method']}]" if event.get("method") else ""
        msg = f" — {event['message']}" if event.get("message") else ""
        print(f"  [{event['done']}/{event['total']}] {icon} {event['path']}{extra}{msg}", file=sys.stderr)
    elif e == "warning":
        print(f"⚠️  {event['message']}", file=sys.stderr)
    elif e == "done":
        print(f"\nSelesai dalam {event['seconds']} dtk → {event['output']}\n"
              f"  {event['files_converted']} dikonversi, {event['files_ocr']} dengan OCR, "
              f"{event['files_failed']} gagal, {event['files_skipped']} dilewati", file=sys.stderr)


def _json(event: dict) -> None:
    sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def warmup() -> int:
    """Load everything once (run by the app at launch, in the background).

    After an install or update macOS verifies every bundled library the first time
    it is loaded, and Vision may compile its text model. Doing that here means the
    user's first real conversion is already fast."""
    import tempfile
    import time
    t0 = time.time()
    import pymupdf            # noqa: F401
    import pymupdf4llm        # noqa: F401
    from markitdown import MarkItDown
    from PIL import Image, ImageDraw
    MarkItDown(enable_plugins=False)
    from .ocr import make_ocr
    ocr = make_ocr(["id", "en"], False, 1)
    ok = ocr is not None
    if ocr is not None:
        with tempfile.TemporaryDirectory(prefix="zip2md_warm_") as d:
            img = Image.new("RGB", (640, 160), "white")
            ImageDraw.Draw(img).text((20, 60), "Tes OCR 123", fill="black")
            path = Path(d) / "warm.png"
            img.save(path)
            try:
                ocr.recognize(path)
            except Exception:
                ok = False
            finally:
                ocr.close()
    _json({"event": "ready", "ocr": ok, "seconds": round(time.time() - t0, 1)})
    return 0


def main(argv: list[str] | None = None) -> int:
    if "--warmup" in (argv if argv is not None else sys.argv[1:]):
        return warmup()
    ap = argparse.ArgumentParser(prog="zip2md", description="Konversi ZIP/dokumen campuran menjadi satu file Markdown (dengan OCR).")
    ap.add_argument("inputs", nargs="+", type=Path, help="File ZIP, folder, atau file dokumen")
    ap.add_argument("-o", "--output", type=Path, help="File .md hasil (default: <nama input>.md)")
    ap.add_argument("--langs", default="id,en", help="Bahasa OCR, kode BCP-47 dipisah koma (default: id,en)")
    ap.add_argument("--fast", action="store_true", help="OCR mode cepat (akurasi lebih rendah)")
    ap.add_argument("--force-ocr", action="store_true", help="OCR semua halaman PDF walau sudah ada teks")
    ap.add_argument("--no-embedded-ocr", action="store_true", help="Jangan OCR gambar di dalam Word/PowerPoint/PDF")
    ap.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 4))
    ap.add_argument("--ocr-workers", type=int, default=0, help="Jumlah proses OCR (default: otomatis)")
    ap.add_argument("--title", help="Judul dokumen Markdown")
    ap.add_argument("--json", action="store_true", help="Progres dalam format JSON lines (stdout)")
    ap.add_argument("--version", action="version", version=f"zip2md {__version__}")
    a = ap.parse_args(argv)

    first = a.inputs[0].expanduser().resolve()
    output = a.output or first.with_suffix(".md") if first.is_file() else (a.output or first.parent / f"{first.name}.md")
    opts = Options(
        inputs=a.inputs, output=output.expanduser().resolve(),
        langs=[l.strip() for l in a.langs.split(",") if l.strip()],
        fast=a.fast, force_ocr=a.force_ocr, ocr_embedded=not a.no_embedded_ocr,
        workers=a.workers, ocr_workers=a.ocr_workers, title=a.title,
    )
    emit = _json if a.json else _human
    missing = [str(p) for p in a.inputs if not p.expanduser().exists()]
    if missing:
        msg = "Input tidak ditemukan: " + ", ".join(missing)
        emit({"event": "error", "message": msg}) if a.json else print(msg, file=sys.stderr)
        return 1

    def on_term(signum, frame):
        emergency_cleanup()
        os._exit(130)

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)

    try:
        run(opts, emit)
    except SystemExit as e:
        if isinstance(e.code, str):
            emit({"event": "error", "message": e.code}) if a.json else print(e.code, file=sys.stderr)
            return 1
        raise
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        emit({"event": "error", "message": msg}) if a.json else print(msg, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Orchestration: collect -> convert (parallel) -> assemble one Markdown file."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import __version__, mdutil
from .archive import ArchiveError, Skipped, SourceFile, collect
from .converters import (LEGACY_EXT, Context, ConversionError, Converted, LegacyBatch,
                         Unsupported, convert, kill_children)
from .ocr import make_ocr

Emit = Callable[[dict], None]


@dataclass
class Options:
    inputs: list[Path]
    output: Path
    langs: list[str] = field(default_factory=lambda: ["id", "en"])
    fast: bool = False
    force_ocr: bool = False
    ocr_embedded: bool = True
    workers: int = 4
    ocr_workers: int = 0          # 0 = one per performance core
    title: str | None = None


def default_ocr_workers() -> int:
    """One Vision worker per performance core (measured on M3: 1 worker 174 ms/page,
    4 workers 71 ms/page; each worker only needs ~80 MB)."""
    try:
        import subprocess
        n = int(subprocess.run(["/usr/sbin/sysctl", "-n", "hw.perflevel0.physicalcpu"],
                               capture_output=True, text=True, timeout=5).stdout.strip())
    except Exception:
        n = (os.cpu_count() or 4) // 2
    return max(2, min(6, n))


@dataclass
class Item:
    src: SourceFile
    result: Converted | None = None
    error: str | None = None
    unsupported: bool = False
    seconds: float = 0.0


def human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            s = f"{size:.0f}" if unit == "B" else f"{size:.1f}"
            return s.replace(".", ",") + " " + unit
        size /= 1024
    return f"{n} B"


_ACTIVE: dict = {}


def emergency_cleanup() -> None:
    """Called on SIGTERM / cancel from the GUI."""
    ocr = _ACTIVE.get("ocr")
    if ocr:
        try:
            ocr.close()
        except Exception:
            pass
    kill_children()                     # LibreOffice; nothing new is started after this
    tmp = _ACTIVE.get("tmp")
    for _ in range(20):                 # worker threads may still be writing for a moment
        if not tmp or not os.path.exists(tmp):
            break
        shutil.rmtree(tmp, ignore_errors=True)
        time.sleep(0.05)


def run(opts: Options, emit: Emit) -> dict:
    started = time.time()
    tmp = Path(tempfile.mkdtemp(prefix="zip2md_"))
    _ACTIVE["tmp"] = tmp
    ocr = None
    try:
        emit({"event": "stage", "stage": "extract", "message": "Mengekstrak arsip…"})
        try:
            col = collect(opts.inputs, tmp / "src")
        except ArchiveError as e:
            raise SystemExit(f"Gagal membaca input: {e}")

        ocr_workers = opts.ocr_workers or default_ocr_workers()
        ocr = make_ocr(opts.langs, opts.fast, ocr_workers)
        _ACTIVE["ocr"] = ocr
        ocr_name = getattr(ocr, "name", None)
        if ocr is None:
            emit({"event": "warning", "message": "OCR tidak tersedia — gambar & PDF scan tidak akan terbaca"})

        ctx = Context(ocr=ocr, tmp=tmp / "work", force_ocr=opts.force_ocr,
                      ocr_embedded=opts.ocr_embedded, ocr_threads=ocr_workers)
        ctx.tmp.mkdir(parents=True, exist_ok=True)

        items = [Item(f) for f in col.files]
        # Old Office formats: one LibreOffice session for all of them, started now so it
        # runs while everything else converts.
        is_legacy = lambda it: it.src.path.suffix.lower() in LEGACY_EXT
        ctx.legacy = LegacyBatch(ctx)
        ctx.legacy.start([it.src.path for it in items if is_legacy(it)])
        total = len(items)
        emit({"event": "start", "total": total, "skipped": len(col.skipped), "ocr": ocr_name})
        if hasattr(ocr, "on_status"):       # "waiting for the OCR engine" and back
            ocr.on_status = lambda m: emit({"event": "stage", "stage": "ocr",
                                            "message": m or f"Mengonversi {total} file…"})

        def work(item: Item) -> Item:
            t0 = time.time()
            try:
                item.result = convert(item.src.path, ctx)
            except Unsupported as e:
                item.error, item.unsupported = str(e), True
            except ConversionError as e:
                item.error = str(e)
            except Exception as e:  # never let one file kill the whole run
                item.error = f"{type(e).__name__}: {str(e)[:300]}"
            if item.error:  # one line, no temp-folder paths
                item.error = " ".join(item.error.replace(str(tmp), "…").split())
            item.seconds = time.time() - t0
            return item

        done = 0
        # at least one file thread per OCR process, or a folder of photos could
        # not keep all of them busy
        with ThreadPoolExecutor(max(1, opts.workers, ocr_workers if ocr else 1)) as ex:
            futures = []
            # legacy files go last: they wait for the LibreOffice batch anyway
            for it in sorted(items, key=is_legacy):
                futures.append(ex.submit(work, it))
            for fut in as_completed(futures):
                it = fut.result()
                done += 1
                status = "skipped" if it.unsupported else "error" if it.error else (
                    "ocr" if it.result and it.result.ocr_used else "ok")
                emit({"event": "file", "done": done, "total": total, "path": it.src.display,
                      "status": status, "seconds": round(it.seconds, 2),
                      "method": it.result.method if it.result else None,
                      "message": it.error or ", ".join(it.result.warnings) if (it.error or (it.result and it.result.warnings)) else None})

        retries = getattr(ocr, "retries", 0)
        if retries:
            emit({"event": "warning",
                  "message": f"{retries} permintaan OCR diulang karena worker macet/berhenti"})
        notes: list[str] = []
        unstable, suspect = getattr(ocr, "unstable", 0), getattr(ocr, "suspect_pages", 0)
        if getattr(ocr, "untrusted", False):
            msg = ("OCR di Mac ini tidak lolos uji-diri — teks hasil OCR mungkin tidak lengkap."
                   " Restart Mac, lalu konversi ulang")
            notes.append(msg)
            emit({"event": "warning", "message": msg})
        elif unstable:
            msg = f"Mesin OCR bermasalah {unstable}× dan dimulai ulang otomatis"
            if suspect:
                msg += (f"; hingga {suspect} halaman yang dibaca sebelum itu mungkin tidak lengkap"
                        " — sebaiknya konversi ulang")
                notes.append(msg)
            emit({"event": "warning", "message": msg})
        emit({"event": "stage", "stage": "assemble", "message": "Menyusun Markdown…"})
        title = opts.title or (opts.inputs[0].name if len(opts.inputs) == 1 else "Gabungan dokumen")
        md, stats = assemble(title, items, col.skipped, opts, ocr_name, notes)
        stats["seconds"] = round(time.time() - started, 1)

        opts.output.parent.mkdir(parents=True, exist_ok=True)
        partial = opts.output.with_name(opts.output.name + ".part")
        partial.write_text(md, encoding="utf-8")
        os.replace(partial, opts.output)          # atomic: never leave a half-written file
        stats["output"] = str(opts.output)
        stats["bytes"] = opts.output.stat().st_size
        emit({"event": "done", **stats})
        return stats
    finally:
        if ocr:
            ocr.close()
        _ACTIVE.clear()
        shutil.rmtree(tmp, ignore_errors=True)


def _yaml(v) -> str:
    return json.dumps(v, ensure_ascii=False) if isinstance(v, str) else str(v)


def assemble(title: str, items: list[Item], skipped: list[Skipped], opts: Options,
             ocr_name: str | None, notes: list[str] | None = None) -> tuple[str, dict]:
    ok = [it for it in items if it.result is not None]
    failed = [it for it in items if it.error and not it.unsupported]
    unsupported = [it for it in items if it.unsupported]
    ocr_count = sum(1 for it in ok if it.result.ocr_used)

    stats = {"files_total": len(items) + len(skipped), "files_converted": len(ok),
             "files_ocr": ocr_count, "files_failed": len(failed),
             "files_skipped": len(unsupported) + len(skipped)}

    from importlib.metadata import PackageNotFoundError, version
    try:
        mid_ver = version("markitdown")
    except PackageNotFoundError:
        mid_ver = "?"

    now = datetime.now().astimezone()
    front = {
        "title": title,
        "source": [p.name for p in opts.inputs] if len(opts.inputs) > 1 else opts.inputs[0].name,
        "generated": now.isoformat(timespec="seconds"),
        "generator": f"Zip2Markdown {__version__} (markitdown {mid_ver}; OCR: {ocr_name or 'tidak ada'})",
        "ocr_languages": ",".join(opts.langs),
        **stats,
    }
    out: list[str] = ["---"]
    for k, v in front.items():
        if isinstance(v, list):
            out.append(f"{k}:")
            out += [f"  - {_yaml(x)}" for x in v]
        else:
            out.append(f"{k}: {_yaml(v)}")
    out += ["---", "", f"# {title}", ""]

    out.append(
        f"> Dokumen ini menggabungkan **{len(ok)} file** dari `{front['source'] if isinstance(front['source'], str) else ', '.join(front['source'])}`"
        f" menjadi satu Markdown. **{ocr_count} file** dibaca dengan OCR ({ocr_name or '—'});"
        " teks hasil OCR dibuat otomatis dan bisa mengandung kesalahan."
        " Setiap file berada di bagian `##` sendiri; batas halaman PDF ditandai dengan komentar `<!-- Halaman N -->`."
    )
    for note in notes or []:
        out += ["", f"> ⚠️ {note}"]
    out.append("")

    # ---- table of contents
    out += ["## Daftar Isi", ""]
    for n, it in enumerate(ok, 1):
        r = it.result
        tag = " · OCR" if r.ocr_used else ""
        out.append(f"{n}. [{it.src.display}](#file-{n}) — {r.kind}{tag}")
    if failed or unsupported or skipped:
        out.append(f"{len(ok) + 1}. [Laporan pemrosesan](#laporan-pemrosesan)")
    out.append("")

    # ---- one section per file
    for n, it in enumerate(ok, 1):
        r = it.result
        meta = [f"**Jenis:** {r.kind}", f"**Ukuran:** {human_size(it.src.size)}", *r.details,
                f"**Metode:** {r.method}"]
        pct = [c for c in r.ocr_conf if c > 0]
        if pct:
            meta.append(f"**Keyakinan OCR:** {round(100 * sum(pct) / len(pct))}%")
        body = mdutil.close_fences(mdutil.normalize(mdutil.demote_headings(r.markdown, 2)))
        if not body.strip():
            body = "*(Tidak ada teks yang dapat diekstrak dari file ini.)*"
        out += ["---", "", f'<a id="file-{n}"></a>', "", f"## {n}. {it.src.display}", "",
                "> " + " · ".join(meta)]
        for w in r.warnings:
            out.append(f">\n> ⚠️ {w}")
        out += ["", body, ""]

    # ---- processing report
    if failed or unsupported or skipped:
        out += ["---", "", '<a id="laporan-pemrosesan"></a>', "", "## Laporan pemrosesan", ""]
        rows = [["File", "Status", "Keterangan"]]
        rows += [[f"`{it.src.display}`", "Gagal", it.error] for it in failed]
        rows += [[f"`{it.src.display}`", "Dilewati", it.error] for it in unsupported]
        rows += [[f"`{s.display}`", "Dilewati", s.reason] for s in skipped]
        out += [mdutil.table(rows), ""]

    return mdutil.normalize("\n".join(out)) + "\n", stats

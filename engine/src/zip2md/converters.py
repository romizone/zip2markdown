"""Per-file converters: every function returns a :class:`Converted` Markdown body."""

from __future__ import annotations

import csv
import email
import email.policy
import fcntl
import io
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import mdutil
from .ocr import OCRError, OCRResult
from .paths import cache_dir

# PyMuPDF is not thread-safe: every PyMuPDF call goes through this lock.
# OCR (the slow part) runs outside the lock, in parallel.
PDF_LOCK = threading.Lock()
PDF_TEXT_CHUNK = 8      # pages of text extraction per lock hold, so PDFs take turns

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".jpe", ".jfif", ".tif", ".tiff", ".bmp", ".gif",
             ".webp", ".heic", ".heif", ".jp2"}
MARKITDOWN_EXT = {
    ".docx": "Word", ".docm": "Word", ".pptx": "PowerPoint", ".pptm": "PowerPoint",
    ".ppsx": "PowerPoint", ".xlsx": "Excel", ".xlsm": "Excel", ".xls": "Excel 97-2003",
    ".html": "HTML", ".htm": "HTML", ".xhtml": "HTML", ".epub": "EPUB",
    ".msg": "Email Outlook", ".ipynb": "Jupyter Notebook",
}
# legacy / other office formats -> converted by LibreOffice to a modern format first
LEGACY_EXT = {
    ".doc": ("docx", "Word 97-2003"), ".dot": ("docx", "Word template"),
    ".rtf": ("docx", "Rich Text"), ".odt": ("docx", "OpenDocument Text"),
    ".wpd": ("docx", "WordPerfect"), ".wps": ("docx", "Works"), ".pages": ("docx", "Apple Pages"),
    ".ppt": ("pptx", "PowerPoint 97-2003"), ".pps": ("pptx", "PowerPoint 97-2003"),
    ".pot": ("pptx", "PowerPoint template"), ".odp": ("pptx", "OpenDocument Presentation"),
    ".key": ("pptx", "Apple Keynote"),
    ".ods": ("xlsx", "OpenDocument Spreadsheet"), ".xlsb": ("xlsx", "Excel Binary"),
    ".numbers": ("xlsx", "Apple Numbers"),
}
CODE_LANG = {
    ".py": "python", ".js": "javascript", ".ts": "typescript", ".java": "java", ".c": "c",
    ".h": "c", ".cpp": "cpp", ".cs": "csharp", ".go": "go", ".rb": "ruby", ".php": "php",
    ".swift": "swift", ".kt": "kotlin", ".rs": "rust", ".sql": "sql", ".sh": "bash",
    ".bat": "bat", ".ps1": "powershell", ".css": "css", ".scss": "scss", ".r": "r",
    ".yaml": "yaml", ".yml": "yaml", ".toml": "toml", ".ini": "ini", ".cfg": "ini",
    ".conf": "ini", ".xml": "xml", ".svg": "xml", ".log": "text", ".tex": "latex",
}
TEXT_EXT = {".txt", ".text", ".md", ".markdown", ".csv", ".tsv", ".json", ".eml"} | set(CODE_LANG)
OOXML_EXT = {".docx", ".docm", ".pptx", ".pptm", ".ppsx", ".xlsx", ".xlsm"}
OLE_MAGIC = b"\xd0\xcf\x11\xe0"
OFFICE_MEDIA_EXT = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif"}
BINARY_EXT = {
    ".mp3": "audio", ".wav": "audio", ".m4a": "audio", ".aac": "audio", ".flac": "audio", ".ogg": "audio",
    ".mp4": "video", ".mov": "video", ".avi": "video", ".mkv": "video", ".wmv": "video", ".m4v": "video",
    ".exe": "program", ".dll": "program", ".app": "program", ".dmg": "image disk", ".iso": "image disk",
    ".pkg": "installer", ".msi": "installer", ".apk": "aplikasi Android",
    ".rar": "arsip RAR", ".7z": "arsip 7-Zip", ".tar": "arsip TAR", ".gz": "arsip GZip", ".bz2": "arsip BZip2",
    ".psd": "Photoshop", ".ai": "Illustrator", ".sketch": "Sketch", ".fig": "Figma",
    ".dwg": "AutoCAD", ".ttf": "font", ".otf": "font", ".woff": "font", ".db": "database",
    ".sqlite": "database", ".mdb": "Access", ".accdb": "Access", ".emf": "gambar vektor", ".wmf": "gambar vektor",
}


def ram_gb() -> float:
    """Installed memory in GB (16 if it cannot be read)."""
    try:
        return os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / 2**30
    except (ValueError, OSError):
        return 16.0


class ConversionError(Exception):
    pass


class Unsupported(Exception):
    pass


@dataclass
class Context:
    ocr: object | None                 # VisionOCR / TesseractOCR / None
    tmp: Path
    force_ocr: bool = False
    ocr_embedded: bool = True
    min_text_chars: int = 40
    dpi: int = 300
    ocr_threads: int = 3
    log: Callable[[str], None] = lambda s: None
    legacy: "LegacyBatch | None" = None

    def tmpfile(self, suffix: str) -> Path:
        return self.tmp / f"{uuid.uuid4().hex}{suffix}"


@dataclass
class Converted:
    markdown: str
    kind: str
    method: str
    details: list[str] = field(default_factory=list)
    ocr_conf: list[float] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ocr_used(self) -> bool:
        return "OCR" in self.method


# --------------------------------------------------------------------------- helpers

def _run_ocr(ctx: Context, image: Path, frame: int = 0) -> OCRResult:
    if ctx.ocr is None:
        raise ConversionError("OCR tidak tersedia (ocr-helper/Tesseract tidak ditemukan)")
    try:
        return ctx.ocr.recognize(image, frame=frame)
    except OCRError as e:
        if "Cannot read image" in str(e):
            # Re-encode exotic images (CMYK JPEG, 16-bit TIFF, ...) to PNG and retry.
            png = _to_png(image, ctx, frame)
            if png:
                try:
                    return ctx.ocr.recognize(png)
                except OCRError as e2:
                    raise ConversionError(f"OCR gagal: {e2}") from e2
        raise ConversionError(f"OCR gagal: {e}") from e


def _to_png(image: Path, ctx: Context, frame: int = 0) -> Path | None:
    from PIL import Image
    try:
        with Image.open(image) as im:
            im.seek(frame)
            im = im.convert("RGB") if im.mode not in ("RGB", "L") else im.copy()
            out = ctx.tmpfile(".png")
            im.save(out)
            return out
    except Exception:
        return None


def _quote(md: str) -> str:
    """Blockquote OCR text; headings inside a quote become bold lines."""
    lines = []
    for l in md.strip().split("\n"):
        h = re.match(r"^#{1,6}\s+(.*)$", l)
        if h:
            l = f"**{h.group(1).strip()}**"
        lines.append(("> " + l) if l.strip() else ">")
    return "\n".join(lines)


def _pct(conf: list[float]) -> str:
    vals = [c for c in conf if c > 0]
    return f"{round(100 * sum(vals) / len(vals))}%" if vals else ""


# --------------------------------------------------------------------------- images

def convert_image(path: Path, ctx: Context) -> Converted:
    from PIL import Image
    details: list[str] = []
    frames = 1
    try:
        with Image.open(path) as im:
            details.append(f"{im.width}×{im.height} px")
            frames = getattr(im, "n_frames", 1) if path.suffix.lower() in (".tif", ".tiff") else 1
            if im.width < 16 or im.height < 16:
                return Converted("", "Gambar", "dilewati (terlalu kecil)", details)
    except Exception:
        pass  # HEIC etc. — Pillow can't open it, Vision can.

    if frames <= 1:
        r = _run_ocr(ctx, path)
        warn = ["Keyakinan OCR rendah — periksa hasilnya"] if 0 < r.confidence < LOW_CONFIDENCE else []
        return Converted(r.markdown, "Gambar", "OCR", details, [r.confidence], warn)

    # multi-page TIFF (typical fax / scanner output): the helper reads each page
    # straight from the file; one unreadable page does not lose the others
    details.append(f"{frames} halaman")

    def one(i: int):
        try:
            return _run_ocr(ctx, path, i), None
        except ConversionError as e:
            return None, str(e)

    with ThreadPoolExecutor(ctx.ocr_threads) as ex:
        results = list(ex.map(one, range(frames)))
    parts, conf, warnings, weak = [], [], [], []
    for i, (r, err) in enumerate(results, 1):
        if r is None:
            warnings.append(f"Halaman {i}: {err}")
            parts.append(f"<!-- Halaman {i} -->")
            continue
        pct = f" ({round(r.confidence * 100)}%)" if r.confidence else ""
        parts.append(f"<!-- Halaman {i} · OCR{pct} -->\n\n{r.markdown}")
        conf.append(r.confidence)
        if 0 < r.confidence < LOW_CONFIDENCE:
            weak.append(i)
    if not conf:
        raise ConversionError(warnings[0] if warnings else "OCR gagal")
    if weak:
        warnings.append(_weak_pages_warning(weak))
    return Converted("\n\n".join(parts), "Gambar (TIFF multi-halaman)", "OCR", details, conf, warnings)


# --------------------------------------------------------------------------- PDF

LOW_CONFIDENCE = 0.5      # below this the OCR text is flagged for manual checking


def _weak_pages_warning(pages: list[int]) -> str:
    shown = ", ".join(map(str, sorted(pages)[:12])) + ("…" if len(pages) > 12 else "")
    return f"Keyakinan OCR rendah di halaman {shown} — periksa hasilnya"


def _coverage(page, infos) -> float:
    area = abs(page.rect)
    if not area:
        return 0.0
    covered = 0.0
    for info in infos:
        r = page.rect & info["bbox"]
        covered += abs(r) if not r.is_empty else 0
    return min(1.0, covered / area)


def _hidden_text_share(page) -> float:
    """Share of the page's characters drawn invisibly (text render mode 3).
    A scan with an invisible text layer was already OCR'd by scanner software —
    usually much worse than Apple Vision, and with no layout information."""
    total = hidden = 0
    try:
        for span in page.get_texttrace():
            n = len(span.get("chars", ()))
            total += n
            if span.get("type") == 3 or span.get("opacity", 1) == 0:
                hidden += n
    except Exception:
        return 0.0
    return hidden / total if total else 0.0


def _scan_dpi(infos, cap: int) -> int:
    """Resolution the scan was made at. Rendering above it only invents pixels
    (measured: no accuracy gain, 2-4x more work), below it throws detail away."""
    best_area, best_dpi = 0.0, float(cap)
    for info in infos:
        x0, y0, x1, y1 = info["bbox"]
        sq_inch = abs((x1 - x0) * (y1 - y0)) / 5184.0
        if sq_inch > best_area and info.get("width") and info.get("height"):
            best_area = sq_inch
            best_dpi = (info["width"] * info["height"] / sq_inch) ** 0.5
    return int(max(110, min(cap, round(best_dpi))))


def _extract_pdf_image(doc, xref: int, ctx: Context) -> Path | None:
    """Save an embedded picture itself (not a render of the page region, which
    would also contain any text drawn on top of it)."""
    import pymupdf
    try:
        raw = doc.extract_image(xref)
        if raw and (raw.get("width", 0) < 200 or raw.get("height", 0) < 60):
            return None
        # a plain JPEG can be handed to Vision as stored: no decode + PNG encode
        if raw and raw.get("ext") == "jpeg" and not raw.get("smask") \
                and raw.get("colorspace") in (1, 3) and raw.get("image"):
            out = ctx.tmpfile(".jpg")
            out.write_bytes(raw["image"])
            return out
    except Exception:
        pass
    try:
        pix = pymupdf.Pixmap(doc, xref)
        if pix.width < 200 or pix.height < 60:
            return None
        if pix.alpha:
            pix = pymupdf.Pixmap(pix, 0)
        if pix.colorspace is None or pix.colorspace.n not in (1, 3):
            pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
        out = ctx.tmpfile(".png")
        pix.save(out)
        return out
    except Exception:
        return None


def _render_page_png(path: Path, index: int, dpi: int, ctx: Context) -> Path:
    import pymupdf
    with PDF_LOCK:
        doc = pymupdf.open(path)
        try:
            if doc.needs_pass:
                doc.authenticate("")
            out = ctx.tmpfile(".png")
            doc[index].get_pixmap(dpi=dpi, alpha=False).save(out)
            return out
        finally:
            doc.close()


def _layout_markdown(doc, pages: list[int]) -> dict[int, str]:
    """Markdown per text page (pymupdf4llm layout engine), ``{page index: markdown}``.

    Long documents are parsed a few pages per lock hold, so other PDFs in the same
    archive get their turn instead of waiting for this one to finish. Heading levels
    are then ranked by font size over ALL pages — exactly what the library does when
    it is given the whole document — so chunking never changes the result.
    Called WITHOUT the lock held."""
    import pymupdf4llm
    opts = dict(page_chunks=True, header=False, footer=False, show_progress=False)

    def whole() -> dict[int, str]:
        with PDF_LOCK:
            chunks = pymupdf4llm.to_markdown(doc, pages=pages, use_ocr=False, **opts)
        return {c["metadata"]["page_number"] - 1: c["text"] for c in chunks}

    if len(pages) <= PDF_TEXT_CHUNK or not getattr(pymupdf4llm, "_use_layout", False):
        return whole()
    try:
        from pymupdf4llm.helpers import document_layout as dl
        merged, parsed_pages = None, []
        for c0 in range(0, len(pages), PDF_TEXT_CHUNK):
            with PDF_LOCK:
                part = dl.parse_document(doc, pages=pages[c0:c0 + PDF_TEXT_CHUNK],
                                         force_text=True, use_ocr=False, show_progress=False)
            parsed_pages += part.pages
            merged = merged or part
            time.sleep(0.001)           # let a waiting PDF take the lock
        sizes = {b.max_fontsize for pg in parsed_pages for b in pg.boxes
                 if b.boxclass in ("title", "section-header")}
        if sizes:
            dl.update_header_tags(parsed_pages, sizes)
        merged.pages = parsed_pages
        with PDF_LOCK:
            chunks = merged.to_markdown(**opts)
        out = {c["metadata"]["page_number"] - 1: c["text"] for c in chunks}
        if set(out) != set(pages):
            raise ValueError("page mismatch")
        return out
    except Exception:
        return whole()      # library internals changed: the plain public call still works


def _has_drawings(page) -> bool:
    try:
        return bool(page.get_cdrawings())
    except Exception:
        return True


def convert_pdf(path: Path, ctx: Context) -> Converted:
    import pymupdf

    ocr_pages: dict[int, int] = {}                 # page index -> dpi
    inline_imgs: dict[int, list[Path]] = {}
    text_pages: list[int] = []
    md_pages: dict[int, str] = {}
    warnings: list[str] = []
    relayered = 0

    with PDF_LOCK:
        try:
            doc = pymupdf.open(path)
        except Exception as e:
            raise ConversionError("PDF rusak atau bukan PDF yang valid") from e
        if doc.needs_pass and not doc.authenticate(""):
            doc.close()
            raise ConversionError("PDF dilindungi password")

    # The helper rasterises PDF pages itself (CoreGraphics), so rendering is parallel
    # and needs no temp files.
    def ocr_page(i: int, dpi: int) -> OCRResult:
        if getattr(ctx.ocr, "renders_pdf", False):
            try:
                return ctx.ocr.recognize(path, page=i, dpi=dpi)
            except OCRError as e:
                if "Cannot render PDF" not in str(e):
                    raise ConversionError(f"OCR gagal: {e}") from e
        return _run_ocr(ctx, _render_page_png(path, i, dpi, ctx))   # PyMuPDF as plan B

    def work(job):
        kind, i, arg = job
        try:
            return job, (ocr_page(i, arg) if kind == "page" else _run_ocr(ctx, arg)), None
        except ConversionError as e:
            return job, None, str(e)
        except Exception as e:
            return job, None, f"{type(e).__name__}: {e}"

    try:
        with PDF_LOCK:
            n = doc.page_count

            # pass 1: what is on every page
            facts = []
            xref_pages: dict[int, int] = {}
            for page in doc:
                try:
                    text = page.get_text("text").strip()
                    infos = page.get_image_info(xrefs=True)
                except Exception:
                    text, infos = "", []
                for x in {i.get("xref", 0) for i in infos}:
                    xref_pages[x] = xref_pages.get(x, 0) + 1
                facts.append((text, infos))

            # pass 2: decide per page — text layer or OCR
            for i, (text, infos) in enumerate(facts):
                page = doc[i]
                coverage = _coverage(page, infos)
                is_scan = coverage > 0.85
                hidden_layer = is_scan and bool(text) and _hidden_text_share(page) > 0.8
                needs_ocr = (
                    ctx.force_ocr
                    or len(text) < ctx.min_text_chars
                    or mdutil.garbage_ratio(text) > 0.2
                    or (is_scan and len(text) < 300)      # scan with a thin stamp/header layer
                    or hidden_layer                       # scan + old invisible OCR layer
                )
                # a nearly empty page with no picture and no drawing has nothing to OCR
                if needs_ocr and not ctx.force_ocr and not infos and mdutil.garbage_ratio(text) <= 0.2 \
                        and not _has_drawings(page):
                    needs_ocr = False
                if needs_ocr and ctx.ocr is not None:
                    longest = max(page.rect.width, page.rect.height) / 72
                    cap = int(min(ctx.dpi, 6000 / max(longest, 1)))
                    ocr_pages[i] = _scan_dpi(infos, cap) if is_scan else cap
                    relayered += hidden_layer and not ctx.force_ocr
                    continue
                if needs_ocr and not text:
                    warnings.append(f"Halaman {i + 1} tanpa teks dan OCR tidak tersedia")
                text_pages.append(i)
                # big pictures inside a text page (e.g. a scanned attachment). Pictures
                # repeated on 3+ pages are letterheads/backgrounds — not worth reading.
                if ctx.ocr_embedded and ctx.ocr is not None:
                    seen: set[int] = set()
                    for info in infos:
                        xref = info.get("xref", 0)
                        r = page.rect & info["bbox"]
                        if r.is_empty or abs(r) < 0.12 * abs(page.rect) or xref in seen:
                            continue
                        if xref and xref_pages.get(xref, 0) >= 3:
                            continue
                        seen.add(xref)
                        out = _extract_pdf_image(doc, xref, ctx) if xref else None
                        if out:
                            inline_imgs.setdefault(i, []).append(out)

        jobs: list[tuple[str, int, object]] = [("page", i, d) for i, d in ocr_pages.items()]
        jobs += [("img", i, p) for i, ps in inline_imgs.items() for p in ps]

        with ThreadPoolExecutor(ctx.ocr_threads) as ex:
            # OCR starts now and runs while the text pages are extracted below
            futures = [ex.submit(work, job) for job in jobs]

            if text_pages:
                try:
                    md_pages.update(_layout_markdown(doc, text_pages))
                except Exception as e:  # layout engine failed — plain text is still useful
                    warnings.append(f"Ekstraksi tata letak gagal ({type(e).__name__}); memakai teks biasa")
                    for i in text_pages:
                        md_pages[i] = mdutil.escape_html_like(facts[i][0])
            for i in text_pages:        # the layout engine returned nothing for a page that has text
                if not md_pages.get(i, "").strip() and facts[i][0]:
                    md_pages[i] = mdutil.escape_html_like(facts[i][0])

            page_ocr: dict[int, OCRResult] = {}
            img_ocr: dict[int, list[str]] = {}
            conf: list[float] = []
            failed_pages: list[int] = []
            for fut in futures:
                (kind, i, _), res, err = fut.result()
                if err:
                    warnings.append(f"Halaman {i + 1}: {err}")
                    if kind == "page":
                        failed_pages.append(i)
                    continue
                if kind == "page":
                    page_ocr[i] = res
                    conf.append(res.confidence)
                elif mdutil.word_count(res.markdown) >= 3 and res.confidence >= 0.3:
                    img_ocr.setdefault(i, []).append(res.markdown)
                    conf.append(res.confidence)
    finally:
        with PDF_LOCK:
            doc.close()

    parts = []
    for i in range(n):
        if i in page_ocr:
            r = page_ocr[i]
            pct = f" ({round(r.confidence * 100)}%)" if r.confidence else ""
            body = r.markdown
            marker = f"<!-- Halaman {i + 1} · OCR{pct} -->"
        else:
            # an OCR failure still falls back to whatever text layer the page has
            body = md_pages.get(i) or (facts[i][0] if i in failed_pages else "")
            marker = f"<!-- Halaman {i + 1} -->"
        for t in img_ocr.get(i, []):
            body += "\n\n> **Teks dalam gambar (OCR):**\n>\n" + _quote(t)
        parts.append(f"{marker}\n\n{body.strip()}" if n > 1 else body.strip())

    weak = [i + 1 for i, r in page_ocr.items() if 0 < r.confidence < LOW_CONFIDENCE]
    if weak:
        warnings.append(_weak_pages_warning(weak))

    details = [f"{n} halaman"]
    if page_ocr and len(page_ocr) < n:
        method = "teks + OCR"
        details.append(f"{len(page_ocr)} halaman di-OCR")
    elif page_ocr:
        method = "OCR (PDF hasil scan)"
    else:
        method = "teks PDF" + (" + OCR gambar" if img_ocr else "")
    if relayered:
        details.append(f"{relayered} halaman punya lapisan teks lama, dibaca ulang")
    return Converted("\n\n".join(parts), "PDF", method, details, conf, warnings)


# --------------------------------------------------------------------------- Office

_tls = threading.local()


def _markitdown():
    if not hasattr(_tls, "md"):
        from markitdown import MarkItDown
        _tls.md = MarkItDown(enable_plugins=False)
    return _tls.md


def _clean_excel(md: str) -> str:
    md = re.sub(r"(?<=\|) ?NaN ?(?=\|)", "  ", md)
    md = re.sub(r"Unnamed: \d+", "", md)
    return md


def _office_media_ocr(path: Path, ctx: Context) -> tuple[dict[int, list[str]], list[str], list[float]]:
    """OCR pictures embedded in DOCX/PPTX/XLSX. Returns ({slide_no: [md]}, [md], conf)."""
    from PIL import Image

    per_slide: dict[int, list[str]] = {}
    loose: list[str] = []
    conf: list[float] = []
    try:
        z = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        return per_slide, loose, conf
    with z:
        media = [n for n in z.namelist()
                 if re.match(r"^(word|ppt|xl)/media/", n) and Path(n).suffix.lower() in OFFICE_MEDIA_EXT]
        if not media:
            return per_slide, loose, conf
        # map pptx media -> first slide that uses it
        slide_of: dict[str, int] = {}
        for n in z.namelist():
            m = re.match(r"^ppt/slides/_rels/slide(\d+)\.xml\.rels$", n)
            if m:
                for target in re.findall(r'Target="\.\./media/([^"]+)"', z.read(n).decode("utf-8", "ignore")):
                    slide_of.setdefault("ppt/media/" + target, int(m.group(1)))
        files: list[tuple[str, Path]] = []
        for name in media:
            out = ctx.tmpfile(Path(name).suffix.lower())
            out.write_bytes(z.read(name))
            try:
                with Image.open(out) as im:
                    if im.width < 200 or im.height < 60 or im.width * im.height < 60_000:
                        continue  # icons, bullets, logos
            except Exception:
                continue
            files.append((name, out))

    def work(item):
        name, p = item
        try:
            return name, _run_ocr(ctx, p)
        except ConversionError:
            return name, None

    with ThreadPoolExecutor(ctx.ocr_threads) as ex:
        for name, r in ex.map(work, files):
            if not r or mdutil.word_count(r.markdown) < 3:
                continue
            conf.append(r.confidence)
            block = f"> **Teks dalam gambar `{Path(name).name}` (OCR):**\n>\n" + _quote(r.markdown)
            if name in slide_of:
                per_slide.setdefault(slide_of[name], []).append(block)
            else:
                loose.append(block)
    return per_slide, loose, conf


_WORD_LIST_MARK = re.compile(r"<!\[if !supportLists\]>(.*?)<!\[endif\]>", re.S | re.I)
_WORD_CONDITION = re.compile(r"<!\[(?:if [^\]]*|endif)\]>", re.I)
# Glyphs Word writes as list bullets (from the Symbol / Wingdings fonts, or plain ones)
_WORD_BULLETS = {"·", "o", "§", "Ø", "ü", "•", "▪", "◦", "-", "–"}


def _clean_word_html(html: str) -> str:
    """HTML from Word (copied to the clipboard or saved as .htm) writes each list
    number or bullet as text inside ``<![if !supportLists]> … <![endif]>``. HTML
    parsers keep "if !supportLists" and "endif" as words, so a numbered list came
    out as "if !supportLists1.   endifItem". Keep the marker, drop the wrapper."""
    def marker(m: re.Match) -> str:
        import html as html_lib
        text = html_lib.unescape(re.sub(r"<[^>]+>", "", m.group(1))).replace("\xa0", " ").strip()
        if not text:
            return ""
        return "- " if text in _WORD_BULLETS else text + " "
    return _WORD_CONDITION.sub("", _WORD_LIST_MARK.sub(marker, html))


def _without_word_markup(path: Path, ctx: Context) -> Path:
    """``path``, or a cleaned UTF-8 copy when it is HTML written by Word."""
    if path.suffix.lower() not in (".html", ".htm", ".xhtml"):
        return path
    try:
        if b"<![if" not in path.read_bytes():
            return path
        text, _ = read_text(path)
    except (OSError, Unsupported):
        return path
    # the copy is UTF-8: its charset declaration must say so, or the parser decodes it wrongly
    text = re.sub(r"(charset\s*=\s*[\"']?)[\w-]+", r"\1utf-8", _clean_word_html(text), flags=re.I)
    clean = ctx.tmpfile(path.suffix.lower())
    clean.write_text(text, encoding="utf-8")
    return clean


def convert_markitdown(path: Path, ctx: Context, kind: str | None = None) -> Converted:
    ext = path.suffix.lower()
    kind = kind or MARKITDOWN_EXT.get(ext, ext.lstrip(".").upper())
    try:
        res = _markitdown().convert(str(_without_word_markup(path, ctx)))
    except Exception as e:
        if ext in OOXML_EXT and _magic(path).startswith(OLE_MAGIC):
            # a password turns the ZIP-based file into an encrypted OLE container
            raise ConversionError("File dilindungi password (terenkripsi)") from e
        raise ConversionError(f"{type(e).__name__}: {str(e)[:300]}") from e
    md = mdutil.strip_local_images(mdutil.strip_data_uri_images(res.markdown or ""))
    md = mdutil.promote_empty_table_headers(md)
    if ext in (".html", ".htm", ".xhtml"):     # incl. text pasted from Word/web in the app
        md = mdutil.escape_html_like_md(md)
    if ext in (".xlsx", ".xlsm", ".xls", ".ods", ".xlsb", ".numbers"):
        md = _clean_excel(md)
    method = "markitdown"
    conf: list[float] = []
    if ctx.ocr_embedded and ctx.ocr is not None and ext in OOXML_EXT:
        per_slide, loose, conf = _office_media_ocr(path, ctx)
        if per_slide:
            chunks = re.split(r"(<!-- Slide number: \d+ -->)", md)
            out, current = [], 0
            for c in chunks:
                m = re.match(r"<!-- Slide number: (\d+) -->", c)
                if m:
                    current = int(m.group(1))
                    out.append(c)
                    continue
                extra = per_slide.pop(current, [])
                out.append(c.strip() + ("\n\n" + "\n\n".join(extra) if extra else "") + "\n\n")
            md = "".join(out)
            loose += [b for blocks in per_slide.values() for b in blocks]
        if loose:
            md += "\n\n" + "\n\n".join(loose)
        if conf:
            method = "markitdown + OCR gambar"
    # blank lines around slide markers so they never glue onto neighbouring text
    md = re.sub(r"[ \t]*\n*[ \t]*(<!-- Slide number: \d+ -->)[ \t]*\n*", r"\n\n\1\n\n", md)
    return Converted(md, kind, method, [], conf)


def _find_soffice() -> str | None:
    for c in (shutil.which("soffice"), "/Applications/LibreOffice.app/Contents/MacOS/soffice"):
        if c and Path(c).exists():
            return c
    return None


CHILDREN: set[subprocess.Popen] = set()     # external converters, killed on cancel
CANCELLED = threading.Event()               # set on cancel: start nothing new
_CHILD_LOCK = threading.Lock()


def kill_children() -> None:
    """Cancel: stop LibreOffice and make sure no thread starts another one."""
    CANCELLED.set()
    with _CHILD_LOCK:
        for child in list(CHILDREN):
            try:
                child.kill()
            except Exception:
                pass

_LO_LOCKS = {t: threading.Lock() for t in ("docx", "pptx", "xlsx")}
# LibreOffice sessions at once (~300 MB each); one at a time on an 8 GB Mac
_LO_PARALLEL = threading.Semaphore(1 if ram_gb() <= 8 else 2)


def _soffice_run(soffice: str, profile: Path, target: str, outdir: Path, files: list[Path],
                 timeout: float) -> None:
    cmd = [soffice, f"-env:UserInstallation={profile.as_uri()}", "--headless", "--norestore",
           "--convert-to", target, "--outdir", str(outdir), *map(str, files)]
    with _CHILD_LOCK:
        if CANCELLED.is_set():
            return
        p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        CHILDREN.add(p)
    try:
        p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()
    finally:
        CHILDREN.discard(p)


def _soffice(soffice: str, ctx: Context, target: str, outdir: Path, files: list[Path], timeout: float) -> None:
    """Run one LibreOffice conversion session.

    LibreOffice spends ~1.4 s creating a user profile when it starts with a new one,
    so each target format keeps its profile in ~/Library/Caches between runs. A
    profile is used by one LibreOffice at a time (file lock: another Zip2Markdown
    may be running); if it is busy or turns out broken, a throw-away one is used."""
    fresh = ctx.tmp / f"lo_profile_{uuid.uuid4().hex}"
    cache = cache_dir()
    with _LO_LOCKS[target], _LO_PARALLEL:
        if cache is not None:
            try:
                lock = open(cache / f"lo_{target}.lock", "w")
            except OSError:
                lock = None
            if lock is not None:
                with lock:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        locked = True
                    except OSError:
                        locked = False
                    if locked:
                        profile = cache / f"lo_profile_{target}"
                        _soffice_run(soffice, profile, target, outdir, files, timeout)
                        if outdir.exists() and any(outdir.glob(f"*.{target}")):
                            return
                        shutil.rmtree(profile, ignore_errors=True)   # maybe damaged: rebuild next time
        _soffice_run(soffice, fresh, target, outdir, files, timeout)


class LegacyBatch:
    """Converts every legacy Office file in ONE LibreOffice session per target
    format (docx / pptx / xlsx), the sessions side by side. Starting LibreOffice
    costs ~1 s; doing it once instead of once per file makes a folder of old .doc
    files several times faster."""

    CHUNK = 40

    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.ready: dict[Path, Path] = {}
        self.done: dict[str, threading.Event] = {t: threading.Event() for t in _LO_LOCKS}

    def start(self, paths: list[Path]) -> None:
        soffice = _find_soffice() if paths else None
        groups: dict[str, list[Path]] = {}
        for src in paths:
            groups.setdefault(LEGACY_EXT[src.suffix.lower()][0], []).append(src)
        for target, event in self.done.items():
            if soffice and target in groups:
                threading.Thread(target=self._run, args=(soffice, target, groups[target]), daemon=True).start()
            else:
                event.set()

    def _run(self, soffice: str, target: str, paths: list[Path]) -> None:
        try:
            stage = self.ctx.tmp / f"lo_in_{target}"
            stage.mkdir(parents=True, exist_ok=True)
            items: list[tuple[Path, Path]] = []
            for k, src in enumerate(paths):
                staged = stage / f"{k:05d}{src.suffix.lower()}"      # unique, ASCII-only names
                try:
                    staged.symlink_to(src)
                except OSError:
                    shutil.copy(src, staged)
                items.append((src, staged))
            for c in range(0, len(items), self.CHUNK):
                chunk = items[c:c + self.CHUNK]
                outdir = self.ctx.tmp / f"lo_out_{target}_{c}"
                _soffice(soffice, self.ctx, target, outdir, [st for _, st in chunk],
                         timeout=90 + 30 * len(chunk))
                for src, staged in chunk:
                    produced = outdir / f"{staged.stem}.{target}"
                    if produced.exists() and produced.stat().st_size > 0:
                        self.ready[src] = produced
        except Exception:
            pass        # anything missing is converted one by one later
        finally:
            self.done[target].set()

    def get(self, path: Path) -> Path | None:
        target = LEGACY_EXT.get(path.suffix.lower(), ("",))[0]
        if target in self.done:
            self.done[target].wait()
        return self.ready.get(path)


def convert_legacy(path: Path, ctx: Context) -> Converted:
    ext = path.suffix.lower()
    target, kind = LEGACY_EXT[ext]
    # Prefer LibreOffice (keeps tables/headings); fall back to macOS textutil for DOC/RTF/ODT.
    produced = ctx.legacy.get(path) if ctx.legacy else None
    soffice = _find_soffice()
    if produced is None and soffice:
        outdir = ctx.tmp / f"lo_{uuid.uuid4().hex}"
        _soffice(soffice, ctx, target, outdir, [path], timeout=180)
        found = list(outdir.glob(f"*.{target}")) if outdir.exists() else []
        produced = found[0] if found else None
    if produced is not None:
        c = convert_markitdown(produced, ctx, kind)
        c.method = f"LibreOffice → {target} → markitdown" + (" + OCR gambar" if c.ocr_conf else "")
        return c
    if ext in (".doc", ".rtf", ".odt", ".dot"):
        out = ctx.tmpfile(".html")
        p = subprocess.run(["/usr/bin/textutil", "-convert", "html", "-output", str(out), str(path)],
                           capture_output=True, text=True, timeout=120)
        if p.returncode == 0 and out.exists():
            c = convert_markitdown(out, ctx, kind)
            c.method = "textutil → HTML → markitdown"
            return c
    if soffice:
        raise ConversionError(f"{kind}: LibreOffice tidak berhasil mengonversi file ini")
    raise Unsupported(f"{kind}: butuh LibreOffice untuk dikonversi (brew install --cask libreoffice)")


# --------------------------------------------------------------------------- text-like

def read_text(path: Path) -> tuple[str, str]:
    data = path.read_bytes()
    for bom, enc in ((b"\xef\xbb\xbf", "utf-8-sig"), (b"\xff\xfe", "utf-16"), (b"\xfe\xff", "utf-16")):
        if data.startswith(bom):
            return data.decode(enc, errors="replace"), enc
    head = data[:8192]
    # NUL bytes or many control chars => binary, even if it happens to be valid UTF-8
    ctrl = sum(1 for b in head if b < 9 or 13 < b < 32)
    if b"\x00" in head or (head and ctrl / len(head) > 0.05):
        raise Unsupported("File biner, bukan teks")
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    from charset_normalizer import from_bytes
    best = from_bytes(data).best()
    if best is not None:
        enc = best.encoding.lower().replace("_", "")
        # Short Latin texts are often mis-guessed as cp1250/latin-2/etc.
        # Windows-1252 is by far the most common legacy encoding here.
        if re.match(r"^(cp125[0-8]|iso8859\d+|latin\d|maccentraleurope|macroman)$", enc):
            try:
                return data.decode("cp1252"), "cp1252"
            except UnicodeDecodeError:
                pass
        return str(best), best.encoding
    return data.decode("latin-1"), "latin-1"


def _sniff_delimiter(sample: str, ext: str) -> str:
    if ext == ".tsv":
        return "\t"
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        counts = {d: sample.count(d) for d in ",;\t|"}
        return max(counts, key=counts.get)


def convert_text(path: Path, ctx: Context) -> Converted:
    ext = path.suffix.lower()
    if ext == ".eml":
        return convert_eml(path, ctx)
    text, enc = read_text(path)
    details = [f"encoding {enc}"] if enc not in ("utf-8", "ascii") else []

    if ext in (".csv", ".tsv"):
        delim = _sniff_delimiter(text[:8192], ext)
        rows = list(csv.reader(io.StringIO(text), delimiter=delim))
        details.append(f"{max(len(rows) - 1, 0)} baris")
        return Converted(mdutil.table(rows), "CSV" if ext == ".csv" else "TSV", "tabel", details)

    if ext == ".json":
        try:
            text = json.dumps(json.loads(text), indent=2, ensure_ascii=False)
        except ValueError:
            details.append("JSON tidak valid — ditampilkan apa adanya")
        return Converted(mdutil.fence(text, "json"), "JSON", "teks", details)

    if ext in (".md", ".markdown"):
        m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
        if m:  # keep YAML front matter of the source, but as a code block
            text = mdutil.fence(m.group(1), "yaml") + "\n\n" + text[m.end():]
        return Converted(text, "Markdown", "teks", details)

    if ext in CODE_LANG:
        return Converted(mdutil.fence(text, CODE_LANG[ext]), ext.lstrip(".").upper(), "teks", details)

    # Plain text: keep paragraphs, but stop lines from turning into headings/rules.
    safe = re.sub(r"^(\s{0,3})(#|={3,}|-{3,}|\*{3,})", r"\1\\\2", text, flags=re.M)
    return Converted(mdutil.escape_html_like(safe), "Teks", "teks", details)


def convert_eml(path: Path, ctx: Context, depth: int = 0) -> Converted:
    msg = email.message_from_bytes(path.read_bytes(), policy=email.policy.default)
    rows = [["Header", "Nilai"]] + [[h, str(msg.get(h, ""))] for h in ("From", "To", "Cc", "Date", "Subject") if msg.get(h)]
    body = msg.get_body(preferencelist=("plain", "html"))
    text = ""
    if body is not None:
        content = body.get_content()
        if body.get_content_type() == "text/html":
            tmp = ctx.tmpfile(".html")
            tmp.write_text(content, encoding="utf-8")
            text = convert_markitdown(tmp, ctx, "HTML").markdown
        else:
            text = mdutil.escape_html_like(content)
    md = mdutil.table(rows) + "\n\n" + text

    # Attachments are documents too: convert each one and put it under the mail.
    names: list[str] = []
    sections: list[str] = []
    conf: list[float] = []
    warnings: list[str] = []
    used_ocr = False
    folder = ctx.tmp / f"eml_{uuid.uuid4().hex}"
    for k, part in enumerate(msg.iter_attachments()):
        name = part.get_filename() or f"lampiran-{k + 1}"
        try:
            if part.get_content_type() == "message/rfc822":     # a forwarded e-mail
                data = part.get_payload(0).as_bytes()
                name = name if name.lower().endswith(".eml") else name + ".eml"
            else:
                data = part.get_payload(decode=True)
        except Exception:
            data = None
        if not data:
            names.append(name)
            continue
        safe = re.sub(r"[/\\:\x00]", "_", name)[-120:] or f"lampiran-{k + 1}"
        target = folder / f"{k:03d}" / safe
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        ext = target.suffix.lower()
        inline = part.get_content_disposition() == "inline" or part.get("Content-ID")
        if ext in IMAGE_EXT and (inline or not _worth_reading(target)):
            continue                        # signature logos, tracking pixels, icons
        names.append(name)
        if depth >= 2 or ext == ".zip":
            continue
        try:
            c = convert_eml(target, ctx, depth + 1) if ext == ".eml" else convert(target, ctx)
        except Unsupported:
            continue
        except Exception as e:
            reason = str(e) if isinstance(e, ConversionError) else f"{type(e).__name__}: {str(e)[:200]}"
            warnings.append(f"Lampiran {name}: {reason}")
            continue
        if not c.markdown.strip():
            continue
        used_ocr = used_ocr or c.ocr_used
        conf += c.ocr_conf
        warnings += [f"Lampiran {name}: {w}" for w in c.warnings]
        sections.append(f"# Lampiran: {name}\n\n> **Jenis:** {c.kind} · **Metode:** {c.method}\n\n"
                        + mdutil.close_fences(mdutil.demote_headings(c.markdown, 1)))
    if names:
        md += "\n\n**Lampiran:** " + ", ".join(f"`{a}`" for a in names)
    if sections:
        md += "\n\n" + "\n\n".join(sections)
    method = "teks" + (" + lampiran" if sections else "") + (" (OCR)" if used_ocr else "")
    details = [f"{len(names)} lampiran"] if names else []
    return Converted(md, "Email (.eml)", method, details, conf, warnings)


def _worth_reading(image: Path) -> bool:
    """False for icons, bullets and logos (same limits as pictures inside Office files)."""
    from PIL import Image
    try:
        with Image.open(image) as im:
            return im.width >= 200 and im.height >= 60 and im.width * im.height >= 60_000
    except Exception:
        return True         # HEIC etc.: let Vision decide


# --------------------------------------------------------------------------- dispatch

def _magic(path: Path, n: int = 16) -> bytes:
    try:
        with open(path, "rb") as fh:
            return fh.read(n)
    except OSError:
        return b""


def sniff(path: Path) -> str:
    """Guess an extension for files that have none (or an unknown one)."""
    head = _magic(path, 16)
    if head.startswith(b"%PDF"):
        return ".pdf"
    if head.startswith(b"\x89PNG"):
        return ".png"
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return ".tif"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if head.startswith(b"BM"):
        return ".bmp"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    if head[4:8] == b"ftyp" and head[8:12] in (b"heic", b"heix", b"mif1", b"msf1", b"hevc"):
        return ".heic"
    if head.startswith(b"{\\rtf"):
        return ".rtf"
    if head.startswith(b"PK\x03\x04"):                 # ZIP container: which Office format?
        try:
            with zipfile.ZipFile(path) as z:
                names = z.namelist()
        except Exception:
            return ""
        for prefix, ext in (("word/", ".docx"), ("ppt/", ".pptx"), ("xl/", ".xlsx")):
            if any(n.startswith(prefix) for n in names):
                return ext
        if "mimetype" in names and "META-INF/container.xml" in names:
            return ".epub"
        return ""
    if head.startswith(OLE_MAGIC):                      # old Office: Word, Excel or PowerPoint?
        try:
            data = path.read_bytes()[:4_000_000]
        except OSError:
            return ".doc"
        for marker, ext in (("WordDocument", ".doc"), ("PowerPoint Document", ".ppt"),
                            ("Workbook", ".xls"), ("Book", ".xls")):
            if marker.encode("utf-16-le") in data:
                return ext
        return ".doc"
    return ""


def convert(path: Path, ctx: Context) -> Converted:
    ext = path.suffix.lower()
    if ext in BINARY_EXT:
        raise Unsupported(f"Format {ext} ({BINARY_EXT[ext]}) tidak berisi teks dokumen")
    if path.stat().st_size == 0:
        raise Unsupported("File kosong (0 byte)")
    if ext not in IMAGE_EXT | set(MARKITDOWN_EXT) | set(LEGACY_EXT) | TEXT_EXT | {".pdf"}:
        guessed = sniff(path)
        if guessed:
            ext = guessed
        else:
            try:
                read_text(path)
                ext = ".txt"
            except Unsupported:
                raise Unsupported(f"Format {path.suffix or '(tanpa ekstensi)'} tidak didukung")

    if ext == ".pdf":
        return convert_pdf(path, ctx)
    if ext in IMAGE_EXT:
        return convert_image(path, ctx)
    if path.suffix.lower() != ext:       # sniffed: the converters go by the extension
        fixed = ctx.tmpfile(ext)
        try:
            fixed.symlink_to(path)
        except OSError:
            shutil.copy(path, fixed)
        path = fixed
    if ext in MARKITDOWN_EXT:
        return convert_markitdown(path, ctx)
    if ext in LEGACY_EXT:
        return convert_legacy(path, ctx)
    if ext in TEXT_EXT:
        return convert_text(path, ctx)
    raise Unsupported(f"Format {ext} tidak didukung")

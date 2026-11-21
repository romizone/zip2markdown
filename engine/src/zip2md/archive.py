"""Safe collection of input files from a ZIP, a folder, or loose files.

Best practices applied:
* Zip-slip protection (no writing outside the extraction folder).
* Zip-bomb limits (total size, file count, compression ratio).
* Correct decoding of non-UTF-8 file names written by Windows tools.
* Nested ZIPs are expanded (up to a depth limit).
* OS junk (``__MACOSX``, ``.DS_Store``, ``Thumbs.db``, Office lock files) is ignored.
* Natural sort so "file2" comes before "file10".
"""

from __future__ import annotations

import os
import re
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

MAX_TOTAL_BYTES = 8 * 1024**3       # 8 GB uncompressed
MAX_FILES = 50_000
MAX_RATIO = 250                     # uncompressed / compressed, per entry
MAX_NESTING = 3

JUNK_NAMES = {".ds_store", "thumbs.db", "desktop.ini", ".localized", "icon\r"}
JUNK_DIRS = {"__macosx", ".git", ".svn", "__pycache__", ".trashes", ".spotlight-v100", ".fseventsd"}


@dataclass
class SourceFile:
    path: Path                 # where the file is on disk
    display: str               # logical path shown in the Markdown ("a.zip › b/c.pdf")
    size: int = 0
    group: int = 0             # which input it came from: inputs keep the order given


@dataclass
class Skipped:
    display: str
    reason: str


@dataclass
class Collection:
    files: list[SourceFile] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)


def natural_key(s: str) -> list:
    return [int(t) if t.isdigit() else t.casefold() for t in re.split(r"(\d+)", s)]


def is_junk(rel: str) -> bool:
    parts = PurePosixPath(rel).parts
    if any(p.casefold() in JUNK_DIRS for p in parts[:-1]):
        return True
    name = parts[-1] if parts else rel
    low = name.casefold()
    return low in JUNK_NAMES or name.startswith("._") or name.startswith("~$") or low.endswith(".tmp")


def decode_name(info: zipfile.ZipInfo) -> str:
    """Return the real file name of a ZIP entry.

    Entries without the UTF-8 flag are decoded by Python as cp437. Many tools
    (Windows Explorer, older macOS) actually wrote UTF-8 or a local code page,
    so we re-try those encodings.
    """
    name = info.filename
    if info.flag_bits & 0x800:
        return name
    try:
        raw = name.encode("cp437")
    except UnicodeEncodeError:
        return name
    for enc in ("utf-8", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return name


def safe_relpath(name: str) -> str | None:
    """Normalise an entry name; return None if it tries to escape the target folder."""
    name = name.replace("\\", "/")
    parts = []
    for part in name.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            return None
        # strip characters macOS/Windows file systems dislike
        parts.append(part.replace(":", "_").replace("\x00", ""))
    return "/".join(parts) if parts else None


class ArchiveError(Exception):
    pass


def extract_zip(zip_path: Path, dest: Path, display_prefix: str, out: Collection,
                depth: int = 0, budget: list[int] | None = None) -> None:
    budget = budget if budget is not None else [0, 0]  # [bytes, files]
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as e:
        raise ArchiveError(f"ZIP rusak atau bukan ZIP: {e}") from e

    with zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = decode_name(info)
            rel = safe_relpath(name)
            disp = f"{display_prefix}{rel or name}"
            if rel is None:
                out.skipped.append(Skipped(disp, "Path tidak aman (zip-slip) — dilewati"))
                continue
            if is_junk(rel):
                continue
            if info.flag_bits & 0x1:
                out.skipped.append(Skipped(disp, "File terenkripsi/berpassword"))
                continue
            if info.compress_size and info.file_size / max(info.compress_size, 1) > MAX_RATIO \
                    and info.file_size > 50 * 1024**2:
                out.skipped.append(Skipped(disp, "Rasio kompresi mencurigakan (zip bomb?)"))
                continue
            budget[0] += info.file_size
            budget[1] += 1
            if budget[0] > MAX_TOTAL_BYTES or budget[1] > MAX_FILES:
                raise ArchiveError("Arsip terlalu besar (batas 8 GB / 50.000 file)")

            target = (dest / rel).resolve()
            if not str(target).startswith(str(dest.resolve()) + os.sep):
                out.skipped.append(Skipped(disp, "Path tidak aman (zip-slip) — dilewati"))
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                with zf.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst, 1024 * 1024)
            except Exception as e:  # CRC error, unsupported compression, ...
                out.skipped.append(Skipped(disp, f"Gagal mengekstrak: {e}"))
                continue

            if target.suffix.lower() == ".zip":
                if depth + 1 > MAX_NESTING:
                    out.skipped.append(Skipped(disp, "ZIP bersarang terlalu dalam"))
                    continue
                inner = target.with_name(target.name + ".__unzipped__")
                inner.mkdir(exist_ok=True)
                try:
                    extract_zip(target, inner, f"{disp} › ", out, depth + 1, budget)
                except ArchiveError as e:
                    out.skipped.append(Skipped(disp, str(e)))
                continue

            out.files.append(SourceFile(target, disp, info.file_size))


def _strip_common_top(files: list[SourceFile], skipped: list[Skipped], prefix: str) -> None:
    """If every file of a ZIP sits under one top folder (typical "Compress folder"
    ZIP), drop that folder from the displayed paths — it only adds noise."""
    rests = [f.display[len(prefix):] for f in files]
    tops = {r.split("/", 1)[0] for r in rests}
    if len(tops) != 1 or not all("/" in r for r in rests):
        return
    top = tops.pop()
    if " › " in top:
        return
    cut = len(prefix) + len(top) + 1
    for f in files:
        f.display = prefix + f.display[cut:]
    for sk in skipped:
        if sk.display.startswith(prefix + top + "/"):
            sk.display = prefix + sk.display[cut:]


def collect(inputs: list[Path], workdir: Path) -> Collection:
    """Collect every convertible file from ZIPs, folders and loose files.
    Any mix works: several ZIPs, folders and single documents together."""
    out = Collection()
    seen: set[Path] = set()
    for n, inp in enumerate(inputs):
        inp = inp.expanduser().resolve()
        if inp in seen:                 # the same input given twice
            continue
        seen.add(inp)
        first_new = len(out.files)
        if not inp.exists():
            out.skipped.append(Skipped(str(inp), "Tidak ditemukan"))
            continue
        if inp.is_dir():
            for root, dirs, files in os.walk(inp):
                dirs[:] = [d for d in dirs if d.casefold() not in JUNK_DIRS and not d.startswith(".")]
                for f in files:
                    p = Path(root) / f
                    rel = p.relative_to(inp).as_posix()
                    if is_junk(rel):
                        continue
                    prefix = f"{inp.name}/" if len(inputs) > 1 else ""
                    if p.suffix.lower() == ".zip":
                        dest = workdir / f"in{n}" / rel
                        dest.mkdir(parents=True, exist_ok=True)
                        try:
                            extract_zip(p, dest, f"{prefix}{rel} › ", out, 1)
                        except ArchiveError as e:
                            out.skipped.append(Skipped(prefix + rel, str(e)))
                    else:
                        out.files.append(SourceFile(p, prefix + rel, p.stat().st_size))
        elif inp.suffix.lower() == ".zip":
            dest = workdir / f"in{n}"
            dest.mkdir(parents=True, exist_ok=True)
            prefix = f"{inp.name} › " if len(inputs) > 1 else ""
            first_skip = len(out.skipped)
            try:
                extract_zip(inp, dest, prefix, out)
            except ArchiveError as e:
                if len(inputs) == 1:
                    raise
                out.skipped.append(Skipped(inp.name, str(e)))   # the other inputs still convert
            _strip_common_top(out.files[first_new:], out.skipped[first_skip:], prefix)
        else:
            out.files.append(SourceFile(inp, inp.name, inp.stat().st_size))
        for f in out.files[first_new:]:
            f.group = n

    # inputs in the order given; natural order ("file2" before "file10") inside each
    out.files.sort(key=lambda f: (f.group, natural_key(f.display)))
    return out

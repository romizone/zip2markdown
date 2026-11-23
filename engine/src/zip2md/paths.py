"""Where Zip2Markdown keeps things between runs."""

from __future__ import annotations

import os
from pathlib import Path


def same_file(a: Path, b: Path) -> bool:
    """True when both paths name one existing file (also across letter case on APFS)."""
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def free_output(candidate: Path, inputs: list[Path]) -> Path:
    """``candidate``, or ``<name> (Markdown).md`` beside it when it is one of the
    inputs: converting ``catatan.md`` must never overwrite ``catatan.md`` itself."""
    stem, n = candidate.stem, 1
    while any(same_file(candidate, p) for p in inputs):
        n += 1
        suffix = " (Markdown)" if n == 2 else f" (Markdown {n - 1})"
        candidate = candidate.with_name(f"{stem}{suffix}.md")
    return candidate


def cache_dir() -> Path | None:
    """~/Library/Caches/Zip2Markdown (LibreOffice profiles, the spare OCR helper),
    or None when it cannot be created. ``ZIP2MD_CACHE`` overrides it (tests)."""
    try:
        d = Path(os.environ.get("ZIP2MD_CACHE") or Path.home() / "Library" / "Caches" / "Zip2Markdown")
        d.mkdir(parents=True, exist_ok=True)
        return d
    except OSError:
        return None

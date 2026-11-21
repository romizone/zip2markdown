"""Where Zip2Markdown keeps things between runs."""

from __future__ import annotations

import os
from pathlib import Path


def cache_dir() -> Path | None:
    """~/Library/Caches/Zip2Markdown (LibreOffice profiles, the spare OCR helper),
    or None when it cannot be created. ``ZIP2MD_CACHE`` overrides it (tests)."""
    try:
        d = Path(os.environ.get("ZIP2MD_CACHE") or Path.home() / "Library" / "Caches" / "Zip2Markdown")
        d.mkdir(parents=True, exist_ok=True)
        return d
    except OSError:
        return None

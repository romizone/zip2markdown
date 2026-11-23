"""Markdown hygiene helpers used when merging many documents into one file."""

from __future__ import annotations

import re
import unicodedata

_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})(.*)$")
_ATX = re.compile(r"^(\s{0,3})(#{1,6})(\s+|$)(.*)$")
_DATA_URI_IMG = re.compile(r"!\[([^\]]*)\]\(data:[^)]*\)")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b\ufeff]")
_PUA = re.compile(r"[\ue000-\uf8ff]")


class Fences:
    """Tracks fenced code blocks the way CommonMark does: a block opened with four
    backticks is NOT closed by a line of three, and ``` never closes ~~~."""

    def __init__(self) -> None:
        self.open: tuple[str, int] | None = None

    def feed(self, line: str) -> bool:
        """True if ``line`` belongs to a code block (fence lines included)."""
        m = _FENCE.match(line)
        if self.open is None:
            if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
                self.open = (m.group(1)[0], len(m.group(1)))
                return True
            return False
        if m and m.group(1)[0] == self.open[0] and len(m.group(1)) >= self.open[1] \
                and not m.group(2).strip():
            self.open = None
        return True


def close_fences(md: str) -> str:
    """Close a code fence left open, so one document can never swallow the
    documents after it into a code block."""
    f = Fences()
    for line in md.split("\n"):
        f.feed(line)
    return md + "\n" + f.open[0] * f.open[1] if f.open else md


def normalize(md: str) -> str:
    """Unicode NFC, LF line endings, no control chars, no trailing spaces,
    at most one blank line in a row (outside code fences)."""
    md = unicodedata.normalize("NFC", md.replace("\r\n", "\n").replace("\r", "\n"))
    md = _CTRL.sub("", md).replace("\u00a0", " ")
    out: list[str] = []
    fences = Fences()
    blank = 0
    for line in md.split("\n"):
        if fences.feed(line):
            blank = 0
        else:
            line = line.rstrip()
            if not line:
                blank += 1
                if blank > 1:
                    continue
            else:
                blank = 0
        out.append(line)
    return "\n".join(out).strip()


def demote_headings(md: str, by: int) -> str:
    """Shift ATX headings down by ``by`` levels so every source document sits
    neatly under its own section. Levels beyond H6 become bold paragraphs."""
    if by <= 0:
        return md
    out: list[str] = []
    fences = Fences()
    for line in md.split("\n"):
        m = None if fences.feed(line) else _ATX.match(line)
        if m:
            level = len(m.group(2)) + by
            text = m.group(4).strip().rstrip("#").strip()
            if not text:
                continue
            out.append(("#" * level + " " + text) if level <= 6 else f"**{text}**")
        else:
            out.append(line)
    return "\n".join(out)


def strip_data_uri_images(md: str) -> str:
    """Base64 images make the Markdown huge and are useless as text."""
    return _DATA_URI_IMG.sub(lambda m: f"*[gambar: {m.group(1)}]*" if m.group(1) else "", md)


_LOCAL_IMG = re.compile(r"!\[([^\]]*)\]\((?!https?://)[^)]*\)")


def strip_local_images(md: str) -> str:
    """Image links to files inside the original document can't resolve in a
    single Markdown file — keep only the alt text as a hint."""
    def repl(m: re.Match) -> str:
        alt = m.group(1).strip()
        return f"*[gambar: {alt}]*" if alt and not re.fullmatch(r"[\w-]*\.(png|jpe?g|gif|emf|wmf|bmp|tiff?)", alt, re.I) else "*[gambar]*"
    return _LOCAL_IMG.sub(repl, md)


_ROW = re.compile(r"^\s*\|.*\|\s*$")
_SEP = re.compile(r"^\s*\|(\s*:?-{3,}:?\s*\|)+\s*$")


def promote_empty_table_headers(md: str) -> str:
    """Word tables without a header row come out as an empty header + data.
    Promote the first data row to be the header instead."""
    lines = md.split("\n")
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        a = lines[i]
        if i + 2 < n and a.lstrip().startswith("|") and not a.replace("|", "").strip() \
                and _ROW.match(a) and _SEP.match(lines[i + 1]) and _ROW.match(lines[i + 2]):
            out += [lines[i + 2], lines[i + 1]]
            i += 3
            continue
        out.append(a)
        i += 1
    return "\n".join(out)


_TAG_LIKE = re.compile(r"<(?=[A-Za-z/!?])")


def escape_html_like(text: str) -> str:
    """Plain text is not HTML: "<isi nama>" must stay visible when rendered."""
    return _TAG_LIKE.sub(r"\\<", text)


_FENCE_LINE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_CODE_SPAN = re.compile(r"(`+)(?:(?!\1).)+?\1")
_TAG_LIKE_TEXT = re.compile(r"<(?=[A-Za-z/!?])(?!br\s*/?>)", re.I)


def escape_html_like_md(md: str) -> str:
    """``escape_html_like`` for Markdown converted from HTML: a "<" left in it was
    text in the source ("&lt;isi nama&gt;"), real tags are already converted. Code
    blocks, code spans and the <br> of table cells are left alone."""
    out: list[str] = []
    fence_mark: str | None = None
    for line in md.split("\n"):
        m = _FENCE_LINE.match(line)
        if fence_mark is not None:
            if m and m.group(1)[0] == fence_mark[0] and len(m.group(1)) >= len(fence_mark):
                fence_mark = None
            out.append(line)
            continue
        if m:
            fence_mark = m.group(1)
            out.append(line)
            continue
        parts, last = [], 0
        for span in _CODE_SPAN.finditer(line):
            parts += [_TAG_LIKE_TEXT.sub(r"\\<", line[last:span.start()]), span.group(0)]
            last = span.end()
        parts.append(_TAG_LIKE_TEXT.sub(r"\\<", line[last:]))
        out.append("".join(parts))
    return "\n".join(out)


def fence(text: str, lang: str = "") -> str:
    """Wrap text in a code fence that is longer than any backtick run inside it."""
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    ticks = "`" * max(3, longest + 1)
    return f"{ticks}{lang}\n{text.rstrip()}\n{ticks}"


def escape_cell(value: object) -> str:
    s = "" if value is None else str(value)
    s = _TAG_LIKE.sub(r"\\<", s.replace("\\", "\\\\"))
    return s.replace("|", "\\|").replace("\r", "").replace("\n", "<br>").strip()


def table(rows: list[list[object]]) -> str:
    rows = [r for r in rows if any(str(c).strip() for c in r if c is not None)]
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [list(r) + [""] * (width - len(r)) for r in rows]
    head = "| " + " | ".join(escape_cell(c) or " " for c in rows[0]) + " |"
    sep = "|" + " --- |" * width
    body = ["| " + " | ".join(escape_cell(c) for c in r) + " |" for r in rows[1:]]
    return "\n".join([head, sep, *body])


def garbage_ratio(text: str) -> float:
    """Share of characters that look like a broken PDF text layer
    (private-use glyphs, replacement chars, lone control chars)."""
    if not text:
        return 0.0
    bad = len(_PUA.findall(text)) + text.count("\ufffd")
    return bad / len(text)


def word_count(md: str) -> int:
    return len(re.findall(r"\w+", md))

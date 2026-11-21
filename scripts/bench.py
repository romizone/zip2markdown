"""OCR accuracy + speed benchmark with known ground-truth text.

    engine/.venv/bin/python scripts/bench.py gen  <dir>     # create test cases
    engine/.venv/bin/python scripts/bench.py run  <dir>     # convert + score every case

Scores (per case):
  recall   share of ground-truth words found in the output (bag of words)
  order    similarity of the word *sequence* (penalises wrong reading order)
"""

from __future__ import annotations

import difflib
import io
import json
import random
import re
import shutil
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "engine" / "src"))

FONT = "/System/Library/Fonts/Supplemental/Arial.ttf"
BOLD = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
SERIF = "/System/Library/Fonts/Supplemental/Times New Roman.ttf"

WORDS = """pemerintah daerah menetapkan peraturan baru tentang pengelolaan anggaran belanja tahun depan
kepala dinas menyampaikan laporan kegiatan kepada seluruh pegawai dalam rapat koordinasi bulanan
perusahaan wajib menyerahkan dokumen perizinan sebelum tanggal jatuh tempo yang telah ditentukan
masyarakat diharapkan berpartisipasi aktif mendukung program pembangunan infrastruktur desa
hasil pemeriksaan menunjukkan bahwa sebagian besar persyaratan administrasi sudah terpenuhi
pembayaran dilakukan melalui transfer bank setelah faktur diterima oleh bagian keuangan
surat keputusan ini berlaku sejak ditandatangani dan akan ditinjau kembali apabila diperlukan
peserta pelatihan memperoleh sertifikat kompetensi setelah menyelesaikan ujian akhir
sekretaris mencatat notulen pertemuan serta mendistribusikan salinan kepada pihak terkait
pengadaan barang dan jasa dilaksanakan sesuai ketentuan perundang-undangan yang berlaku""".split()

SPECIALS = ["Rp 1.250.000", "Rp 87.500", "No. 045/SK/XI/2026", "NIK 3174092305880004", "12,5%",
            "tanggal 17 Agustus 2026", "pukul 09.30 WIB", "Jl. Merdeka No. 21", "(021) 555-0182",
            "NPWP 01.234.567.8-901.000", "Pasal 14 ayat (2)", "tahun 2025/2026"]


def sentences(rng: random.Random, n: int) -> list[str]:
    out = []
    for _ in range(n):
        k = rng.randint(8, 16)
        words = [rng.choice(WORDS) for _ in range(k)]
        if rng.random() < 0.6:
            words.insert(rng.randint(2, k - 1), rng.choice(SPECIALS))
        s = " ".join(words)
        out.append(s[0].upper() + s[1:] + ".")
    return out


def wrap(d, text: str, font, width: int) -> list[str]:
    lines, cur = [], ""
    for w in text.split():
        t = (cur + " " + w).strip()
        if d.textlength(t, font=font) <= width:
            cur = t
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def page(text_paras: list[str], *, title: str | None = None, size=(1654, 2339), font_px=34,
         margin=140, columns=1, face=FONT, hyphenate=False, rng: random.Random | None = None):
    """Render paragraphs like a printed/scanned page. Returns a PIL image."""
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(img)
    f = ImageFont.truetype(face, font_px)
    y0 = margin
    if title:
        d.text((margin, y0), title, fill="black", font=ImageFont.truetype(BOLD, int(font_px * 1.6)))
        y0 += int(font_px * 3)
    gap = 70
    col_w = (size[0] - 2 * margin - gap * (columns - 1)) // columns
    lh = int(font_px * 1.5)
    # lay out all lines, flowing column after column
    lines: list[str] = []
    for p in text_paras:
        ls = wrap(d, p, f, col_w)
        if hyphenate and rng:
            ls = _hyphenate(ls, rng)
        lines += ls + [""]
    per_col = (size[1] - y0 - margin) // lh
    for c in range(columns):
        x = margin + c * (col_w + gap)
        for i, line in enumerate(lines[c * per_col:(c + 1) * per_col]):
            d.text((x, y0 + i * lh), line, fill="black", font=f)
    assert len(lines) <= per_col * columns, "text does not fit the page"
    return img


def _hyphenate(lines: list[str], rng: random.Random) -> list[str]:
    """Move the first syllables of the next line's first word up, with a hyphen."""
    out = list(lines)
    for i in range(len(out) - 1):
        nxt = out[i + 1].split()
        if not nxt or len(nxt[0]) < 7 or not nxt[0].isalpha() or rng.random() < 0.3:
            continue
        cut = rng.randint(3, len(nxt[0]) - 3)
        out[i] = out[i] + " " + nxt[0][:cut] + "-"
        out[i + 1] = " ".join([nxt[0][cut:]] + nxt[1:])
    return out


def degrade(img, rng: random.Random, angle=2.5, noise=18, blur=0.8, quality=40):
    import numpy as np
    from PIL import Image, ImageFilter
    img = img.rotate(angle, expand=True, fillcolor="white", resample=Image.BICUBIC)
    img = img.filter(ImageFilter.GaussianBlur(blur))
    arr = np.asarray(img).astype("int16")
    nrng = np.random.default_rng(rng.randint(0, 1 << 30))
    arr = arr + nrng.normal(0, noise, arr.shape)
    arr = (arr * 0.85 + 25).clip(0, 255).astype("uint8")   # washed-out contrast
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, "JPEG", quality=quality)
    buf.seek(0)
    return Image.open(buf).convert("RGB")


def table_page(rng: random.Random):
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (1654, 2339), "white")
    d = ImageDraw.Draw(img)
    d.text((140, 140), "Daftar Realisasi Anggaran", fill="black", font=ImageFont.truetype(BOLD, 52))
    head = ["No", "Uraian", "Volume", "Harga Satuan", "Jumlah"]
    widths = [90, 520, 190, 280, 290]
    rows = [head]
    items = ["Kertas HVS", "Tinta printer", "Sewa gedung", "Konsumsi rapat", "Honor narasumber",
             "Transportasi", "Alat tulis", "Jasa kebersihan", "Pemeliharaan AC", "Langganan internet"]
    for i, it in enumerate(items, 1):
        vol = rng.randint(1, 40)
        price = rng.randint(15, 950) * 1000
        rows.append([str(i), it, f"{vol} unit", f"Rp {price:,}".replace(",", "."),
                     f"Rp {vol * price:,}".replace(",", ".")])
    y = 280
    for r, row in enumerate(rows):
        x = 140
        for c, cell in enumerate(row):
            d.rectangle([x, y, x + widths[c], y + 74], outline="black", width=3)
            d.text((x + 14, y + 18), cell, fill="black", font=ImageFont.truetype(BOLD if r == 0 else FONT, 32))
            x += widths[c]
        y += 74
    truth = " ".join(" ".join(r) for r in rows)
    return img, "Daftar Realisasi Anggaran " + truth


def gen(out: Path) -> None:
    import pymupdf
    from PIL import Image
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    rng = random.Random(7)
    truth: dict[str, str] = {}
    expect: dict[str, str] = {}

    def paras(n_par=4, n_sent=3):
        return [" ".join(sentences(rng, n_sent)) for _ in range(n_par)]

    def save(name, img, text, **kw):
        img.save(out / name, **kw)
        truth[name] = text

    p = paras(); save("img_clean.png", page(p, title="Nota Dinas"), "Nota Dinas " + " ".join(p))
    p = paras(); im = page(p, title="Surat Edaran")
    save("img_lowres_100dpi.png", im.resize((827, 1170), Image.LANCZOS), "Surat Edaran " + " ".join(p))
    p = paras(3); im = page(p, title="Berita Acara")
    save("img_lowres_72dpi.jpg", im.resize((595, 842), Image.LANCZOS), "Berita Acara " + " ".join(p), quality=70)
    for deg in (90, 180, 270):
        p = paras(); im = page(p, title="Laporan Bulanan")
        save(f"img_rot{deg}.png", im.rotate(deg, expand=True), "Laporan Bulanan " + " ".join(p))
    p = paras(); save("img_skew_noise.jpg", degrade(page(p, title="Risalah Rapat"), rng),
                      "Risalah Rapat " + " ".join(p), quality=45)
    p = paras(8, 3); save("img_two_column.png", page(p, title="Buletin Kantor", columns=2, font_px=28),
                          "Buletin Kantor " + " ".join(p))
    p = paras(9, 4); save("img_small_font.png", page(p, font_px=22, face=SERIF), " ".join(p))
    p = paras(5, 3); save("img_hyphenated.png", page(p, hyphenate=True, rng=rng, face=SERIF), " ".join(p))
    im, t = table_page(rng); save("img_table.png", im, t)
    # photo stored sideways with an EXIF orientation tag (typical phone picture)
    p = paras(); im = page(p, title="Kwitansi")
    exif = Image.Exif(); exif[0x0112] = 6            # 6 = rotate 90° CW to display
    save("img_exif_rotated.jpg", im.rotate(90, expand=True), "Kwitansi " + " ".join(p), quality=85, exif=exif)

    # ---- PDFs
    def scan_pdf(name, n_pages, dpi=150, quality=60, mutate=None):
        doc = pymupdf.open()
        texts = []
        for i in range(n_pages):
            p = paras(4)
            texts.append(f"Halaman Arsip {i + 1} " + " ".join(p))
            im = page(p, title=f"Halaman Arsip {i + 1}")
            im = im.resize((int(8.27 * dpi), int(11.69 * dpi)), Image.LANCZOS)
            buf = io.BytesIO(); im.convert("L").save(buf, "JPEG", quality=quality)
            pg = doc.new_page(width=595, height=842)
            pg.insert_image(pg.rect, stream=buf.getvalue())
            if mutate:
                mutate(pg, texts[-1])
        doc.save(out / name, deflate=True)
        truth[name] = " ".join(texts)

    scan_pdf("pdf_scan_30p.pdf", 30)

    def bad_layer(pg, text):   # invisible, low-quality OCR layer like cheap scanner software
        garbled = "".join(rng.choice("lI1|rnm0O ") if rng.random() < 0.35 else ch for ch in text)
        pg.insert_textbox(pymupdf.Rect(40, 40, 555, 800), garbled, fontsize=7, render_mode=3)
    scan_pdf("pdf_scan_hidden_bad_text.pdf", 2, mutate=bad_layer)
    expect["pdf_scan_hidden_bad_text.pdf"] = "OCR"

    # page scanned sideways: image rotated, viewer fixes it with /Rotate
    doc = pymupdf.open(); p = paras(); im = page(p, title="Lampiran Peta")
    buf = io.BytesIO(); im.rotate(90, expand=True).convert("L").save(buf, "JPEG", quality=70)
    pg = doc.new_page(width=842, height=595); pg.insert_image(pg.rect, stream=buf.getvalue()); pg.set_rotation(270)
    doc.save(out / "pdf_scan_rotate_flag.pdf"); truth["pdf_scan_rotate_flag.pdf"] = "Lampiran Peta " + " ".join(p)

    # sideways scan WITHOUT a rotate flag (most annoying real-world case)
    doc = pymupdf.open(); p = paras(); im = page(p, title="Lampiran Denah")
    buf = io.BytesIO(); im.rotate(90, expand=True).convert("L").save(buf, "JPEG", quality=70)
    pg = doc.new_page(width=842, height=595); pg.insert_image(pg.rect, stream=buf.getvalue())
    doc.save(out / "pdf_scan_sideways.pdf"); truth["pdf_scan_sideways.pdf"] = "Lampiran Denah " + " ".join(p)

    # born-digital, 30 pages
    doc = pymupdf.open(); texts = []
    for i in range(30):
        p = paras(5); texts.append(f"Bab {i + 1} Ketentuan Umum " + " ".join(p))
        pg = doc.new_page(width=595, height=842)
        pg.insert_text((60, 70), f"Bab {i + 1} Ketentuan Umum", fontsize=18, fontname="hebo")
        pg.insert_textbox(pymupdf.Rect(60, 95, 535, 800), "\n\n".join(p), fontsize=10.5)
    doc.save(out / "pdf_text_30p.pdf", deflate=True); truth["pdf_text_30p.pdf"] = " ".join(texts)
    expect["pdf_text_30p.pdf"] = "teks PDF"

    # born-digital text over a full-page background picture: must NOT be OCR'd
    doc = pymupdf.open(); p = paras(3)
    bg = Image.new("RGB", (620, 877), (236, 242, 250)); buf = io.BytesIO(); bg.save(buf, "PNG")
    pg = doc.new_page(width=595, height=842); pg.insert_image(pg.rect, stream=buf.getvalue())
    pg.insert_textbox(pymupdf.Rect(60, 80, 535, 800), "\n\n".join(p), fontsize=11)
    doc.save(out / "pdf_text_on_background.pdf"); truth["pdf_text_on_background.pdf"] = " ".join(p)
    expect["pdf_text_on_background.pdf"] = "teks PDF"

    # ---- 12 legacy .doc files (speed of the LibreOffice path)
    soffice = shutil.which("soffice")
    if soffice:
        tmp = out / "_html"; tmp.mkdir()
        for i in range(12):
            p = paras(3)
            (tmp / f"doc_legacy_{i + 1:02d}.html").write_text(
                f"<html><body><h1>Memo {i + 1}</h1>" + "".join(f"<p>{x}</p>" for x in p) + "</body></html>",
                encoding="utf-8")
            truth[f"doc_legacy_{i + 1:02d}.doc"] = f"Memo {i + 1} " + " ".join(p)
        subprocess.run([soffice, "--headless", "--convert-to", "doc:MS Word 97", "--outdir", str(out),
                        *map(str, sorted(tmp.glob("*.html")))], check=True, capture_output=True, timeout=300)
        shutil.rmtree(tmp)

    # ---- harder images (added last so earlier cases stay byte-identical)
    p = paras(6, 3); save("img_hard_photo.jpg", degrade(page(p, font_px=26, face=SERIF), rng, angle=5, noise=26, blur=1.2, quality=30),
                          " ".join(p), quality=40)
    p = paras(6, 3); im = page(p, font_px=24)
    save("img_lowres_small.png", im.resize((827, 1170), Image.LANCZOS), " ".join(p))
    p = paras(5, 3); im = page(p, font_px=28)
    low = Image.eval(im.convert("L"), lambda v: 150 + v * 75 // 255)      # grey text on light grey
    save("img_low_contrast.png", low, " ".join(p))
    p = paras(5, 3); im = degrade(page(p, font_px=30), rng, angle=-8, noise=10, blur=0.5, quality=60)
    save("img_skew_8deg.jpg", im, " ".join(p), quality=70)

    (out / "truth.json").write_text(json.dumps({"truth": truth, "expect": expect}, ensure_ascii=False, indent=1))
    print(f"{len(truth)} cases in {out}")


# ------------------------------------------------------------------ scoring

def words(text: str) -> list[str]:
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    text = re.sub(r"[|#*>`_]", " ", text)
    return re.findall(r"[0-9a-zà-ÿ]+", text.lower())


def score(truth: str, got: str) -> tuple[float, float]:
    t, g = words(truth), words(got)
    if not t:
        return 0.0, 0.0
    ct, cg = Counter(t), Counter(g)
    recall = sum(min(n, cg[w]) for w, n in ct.items()) / len(t)
    order = difflib.SequenceMatcher(None, t, g, autojunk=False).ratio()
    return recall, order


def run(out: Path, only: str | None = None) -> None:
    from zip2md.converters import Context, convert
    from zip2md.ocr import make_ocr
    import tempfile

    meta = json.loads((out / "truth.json").read_text())
    truth, expect = meta["truth"], meta["expect"]
    tmp = Path(tempfile.mkdtemp(prefix="bench_"))
    ocr = make_ocr(["id", "en"], False, 3)
    ctx = Context(ocr=ocr, tmp=tmp)
    rows, t_all = [], time.time()
    dump = out / "_out"; dump.mkdir(exist_ok=True)
    for name in sorted(truth):
        if only and only not in name:
            continue
        if name.startswith("doc_legacy"):
            continue
        t0 = time.time()
        try:
            r = convert(out / name, ctx)
            md, method = r.markdown, r.method
        except Exception as e:
            md, method = "", f"ERROR {type(e).__name__}: {e}"
        dt = time.time() - t0
        (dump / (name + ".md")).write_text(md)
        rec, order = score(truth[name], md)
        flag = "" if name not in expect or expect[name] in method else f"  !! expected {expect[name]}"
        rows.append((name, rec, order, dt, method + flag))
    ocr.close()
    shutil.rmtree(tmp, ignore_errors=True)

    print(f"{'case':34} {'recall':>7} {'order':>7} {'sec':>6}  method")
    for name, rec, order, dt, method in rows:
        print(f"{name:34} {rec:7.1%} {order:7.1%} {dt:6.1f}  {method}")
    if rows:
        n = len(rows)
        print(f"{'MEAN':34} {sum(r[1] for r in rows) / n:7.1%} {sum(r[2] for r in rows) / n:7.1%} "
              f"{sum(r[3] for r in rows):6.1f}")

    # legacy .doc batch through the full pipeline (what the app really does)
    docs = sorted(out.glob("doc_legacy_*.doc"))
    if docs and not only:
        from zip2md.pipeline import Options, run as run_pipeline
        t0 = time.time()
        res = out / "_out" / "legacy.md"
        run_pipeline(Options(inputs=docs, output=res), lambda e: None)
        got = res.read_text()
        rec, order = score(" ".join(truth[d.name] for d in docs), got.split("## Daftar Isi")[-1])
        print(f"{'12 x .doc (pipeline)':34} {rec:7.1%} {'':>7} {time.time() - t0:6.1f}")
    print(f"total wall time: {time.time() - t_all:.1f}s")


if __name__ == "__main__":
    cmd, d = sys.argv[1], Path(sys.argv[2]).resolve()
    gen(d) if cmd == "gen" else run(d, sys.argv[3] if len(sys.argv) > 3 else None)

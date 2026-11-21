"""Generate a sample ZIP containing every supported file type (for testing).

Usage:  engine/.venv/bin/python scripts/make_samples.py <output_dir>
Creates <output_dir>/sampel.zip
"""

from __future__ import annotations

import csv
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pymupdf
from openpyxl import Workbook
from PIL import Image, ImageDraw, ImageFont
from pptx import Presentation
from pptx.util import Inches

FONT = "/System/Library/Fonts/Supplemental/Arial.ttf"
BOLD = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"

PARAGRAPH = (
    "Laporan ini menjelaskan hasil kegiatan pelatihan pegawai yang dilaksanakan "
    "di Jakarta pada bulan Oktober 2026. Seluruh peserta mengikuti materi dengan "
    "baik dan memperoleh sertifikat kelulusan."
)


def scan_page(title: str, lines: list[str], table: list[list[str]] | None = None) -> Image.Image:
    """Render a page that looks like a scanned document (image only, no text layer)."""
    img = Image.new("RGB", (1654, 2339), "white")  # A4 @ 200 dpi
    d = ImageDraw.Draw(img)
    d.text((140, 140), title, fill="black", font=ImageFont.truetype(BOLD, 56))
    f = ImageFont.truetype(FONT, 34)
    y = 260
    for line in lines:
        d.text((140, y), line, fill="black", font=f)
        y += 52
    if table:
        y += 40
        col_w = 440
        for r, row in enumerate(table):
            for c, cell in enumerate(row):
                x = 140 + c * col_w
                d.rectangle([x, y, x + col_w, y + 70], outline="black", width=3)
                d.text((x + 16, y + 16), cell, fill="black",
                       font=ImageFont.truetype(BOLD if r == 0 else FONT, 32))
            y += 70
    return img


def main(out: Path) -> None:
    src = out / "sampel"
    if src.exists():
        shutil.rmtree(src)
    (src / "Dokumen").mkdir(parents=True)
    (src / "Gambar").mkdir()
    (src / "PDF").mkdir()
    (src / "Data").mkdir()

    # --- Word (.docx, .doc, .rtf) via macOS textutil from HTML
    html = src / "tmp.html"
    html.write_text(
        "<html><body><h1>Surat Keputusan</h1><h2>Pertimbangan</h2>"
        f"<p>{PARAGRAPH}</p><ul><li>Poin pertama</li><li>Poin kedua</li></ul>"
        "<table border=1><tr><th>Nama</th><th>Jabatan</th></tr>"
        "<tr><td>Budi Santoso</td><td>Kepala Bagian</td></tr>"
        "<tr><td>Siti Aminah</td><td>Staf Keuangan</td></tr></table></body></html>",
        encoding="utf-8",
    )
    soffice = shutil.which("soffice")
    if soffice:  # LibreOffice writes real Word headings & tables
        for fmt, name in (("docx:MS Word 2007 XML", "Surat Keputusan.docx"), ("doc:MS Word 97", "Surat Lama.doc")):
            subprocess.run([soffice, "--headless", "--convert-to", fmt, "--outdir", str(out), str(html)],
                           check=True, capture_output=True, timeout=120)
            (out / ("tmp." + fmt.split(":")[0])).rename(src / "Dokumen" / name)
    else:
        subprocess.run(["textutil", "-convert", "docx", str(html), "-output",
                        str(src / "Dokumen" / "Surat Keputusan.docx")], check=True)
        subprocess.run(["textutil", "-convert", "doc", str(html), "-output",
                        str(src / "Dokumen" / "Surat Lama.doc")], check=True)
    subprocess.run(["textutil", "-convert", "rtf", str(html), "-output",
                    str(src / "Dokumen" / "Catatan.rtf")], check=True)
    html.unlink()

    # --- PowerPoint (.pptx) with an embedded "scanned" image
    prs = Presentation()
    s = prs.slides.add_slide(prs.slide_layouts[1])
    s.shapes.title.text = "Rencana Kerja 2027"
    s.placeholders[1].text = "Target utama\nDigitalisasi arsip\nPelatihan OCR"
    s2 = prs.slides.add_slide(prs.slide_layouts[5])
    s2.shapes.title.text = "Lampiran Gambar"
    emb = scan_page("Memo Internal", ["Rapat koordinasi diadakan hari Senin.",
                                      "Mohon kehadiran seluruh kepala divisi."]).crop((0, 0, 1654, 500))
    emb_path = out / "emb.png"
    emb.save(emb_path)
    s2.shapes.add_picture(str(emb_path), Inches(0.5), Inches(1.8), width=Inches(9))
    s2.notes_slide.notes_text_frame.text = "Catatan pembicara: jelaskan memo ini."
    prs.save(src / "Dokumen" / "Presentasi.pptx")
    emb_path.unlink()

    # --- Excel (.xlsx) two sheets
    wb = Workbook()
    ws = wb.active
    ws.title = "Anggaran"
    ws.append(["Kegiatan", "Bulan", "Biaya (Rp)"])
    ws.append(["Pelatihan", "Oktober", 15000000])
    ws.append(["Pengadaan", "November", 32500000])
    ws2 = wb.create_sheet("Pegawai")
    ws2.append(["NIP", "Nama"])
    ws2.append(["1987001", "Budi Santoso"])
    wb.save(src / "Data" / "Anggaran.xlsx")

    # --- CSV / JSON / TXT / MD
    with open(src / "Data" / "peserta.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["No", "Nama", "Kota"])
        w.writerow([1, "Andi", "Bandung"])
        w.writerow([2, "Dewi", "Surabaya"])
    (src / "Data" / "konfigurasi.json").write_text(
        json.dumps({"aplikasi": "Arsip", "versi": 2}, indent=2), encoding="utf-8")
    (src / "Dokumen" / "catatan.txt").write_text(
        "Catatan rapat\n\nRapat dimulai pukul 09.00 WIB.\nDihadiri 12 orang.\n", encoding="utf-8")
    (src / "Dokumen" / "windows-1252.txt").write_bytes(
        "Café résumé — naïve façade\n".encode("cp1252"))
    (src / "Dokumen" / "README.md").write_text(
        "# Panduan\n\n## Langkah 1\n\nBuka aplikasi.\n", encoding="utf-8")

    # --- Images (scans)
    scan_page("Nota Dinas", PARAGRAPH_LINES,
              [["Barang", "Jumlah", "Harga"], ["Kertas A4", "10 rim", "Rp 550.000"],
               ["Tinta", "4 botol", "Rp 320.000"]]).save(src / "Gambar" / "nota_dinas.png")
    scan_page("Pengumuman", ["Kantor tutup pada tanggal 25 Desember 2026.",
                             "Layanan daring tetap berjalan."]).convert("L").save(
        src / "Gambar" / "pengumuman.jpg", quality=80)
    # a photo with no text at all
    Image.new("RGB", (800, 600), (40, 120, 200)).save(src / "Gambar" / "foto_kosong.jpg")

    # --- PDFs
    # 1) born-digital text PDF
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_text((72, 72), "Laporan Tahunan 2026", fontsize=20)
    p.insert_textbox(pymupdf.Rect(72, 100, 520, 400), PARAGRAPH * 2, fontsize=11)
    doc.save(src / "PDF" / "laporan_digital.pdf")

    # 2) scanned PDF (image only, 2 pages)
    pages = [scan_page("Berita Acara", PARAGRAPH_LINES),
             scan_page("Daftar Hadir", ["Rapat tanggal 3 November 2026"],
                       [["Nama", "Instansi", "Tanda Tangan"], ["Rina", "Dinas Pendidikan", "ttd"],
                        ["Joko", "Dinas Kesehatan", "ttd"]])]
    pages[0].save(src / "PDF" / "hasil_scan.pdf", save_all=True, append_images=pages[1:],
                  resolution=200)

    # 3) mixed PDF: page 1 text, page 2 scanned image
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_text((72, 72), "Halaman teks digital", fontsize=16)
    p.insert_textbox(pymupdf.Rect(72, 100, 520, 300), PARAGRAPH, fontsize=11)
    p2 = doc.new_page()
    buf = out / "page.png"
    scan_page("Lampiran Scan", ["Ini adalah halaman hasil pemindaian."]).save(buf)
    p2.insert_image(p2.rect, filename=str(buf))
    doc.save(src / "PDF" / "campuran.pdf")
    buf.unlink()

    # --- Legacy PowerPoint (.ppt) via LibreOffice, if available
    if soffice:
        subprocess.run([soffice, "--headless", "--convert-to", "ppt", "--outdir",
                        str(src / "Dokumen"), str(src / "Dokumen" / "Presentasi.pptx")],
                       check=False, capture_output=True, timeout=120)
        lp = src / "Dokumen" / "Presentasi.ppt"
        if lp.exists():
            lp.rename(src / "Dokumen" / "Presentasi Lama.ppt")

    # --- Nested zip
    with zipfile.ZipFile(src / "Arsip Lama.zip", "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("memo.txt", "Memo dari arsip lama.\n")
        z.write(src / "Gambar" / "pengumuman.jpg", "scan/pengumuman_lama.jpg")

    # --- Final zip, with macOS junk entries like a real Finder zip
    final = out / "sampel.zip"
    with zipfile.ZipFile(final, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(src.rglob("*")):
            if f.is_file():
                z.write(f, f.relative_to(src.parent))
        z.writestr("__MACOSX/sampel/._catatan.txt", b"\x00\x05\x16\x07junk")
        z.writestr("sampel/.DS_Store", b"\x00\x00\x00\x01Bud1")
    print(final)


PARAGRAPH_LINES = [
    "Laporan ini menjelaskan hasil kegiatan pelatihan pegawai",
    "yang dilaksanakan di Jakarta pada bulan Oktober 2026.",
    "Seluruh peserta mengikuti materi dengan baik dan",
    "memperoleh sertifikat kelulusan.",
]

if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())

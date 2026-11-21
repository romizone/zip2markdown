# Zip2Markdown

Aplikasi desktop macOS yang mengubah **satu file ZIP berisi dokumen campuran** (Word, PowerPoint, Excel, PDF termasuk hasil scan, gambar, teks, CSV, email, dll.) menjadi **satu file Markdown** yang rapi. Semua gambar dan PDF hasil scan di-OCR otomatis, **sepenuhnya offline** dengan Apple Vision.

## Instalasi dari DMG

Butuh **macOS 26 atau lebih baru** di Mac **Apple Silicon** (M1 dan seterusnya).

1. Unduh `Zip2Markdown-<versi>.dmg` dari halaman *Releases*, lalu buka.
2. Tarik **Zip2Markdown** ke folder **Applications** di jendela yang muncul.
3. Buka Zip2Markdown dari Applications. Pada pembukaan pertama app menaruh ikonnya di **Dock** (sekali saja — kalau ikon itu Anda keluarkan dari Dock, ia tidak ditambahkan lagi), dan menyiapkan mesin OCR ±35 detik.

App ini ditandatangani *ad-hoc*, belum dinotarisasi Apple, jadi macOS menolak membukanya saat pertama kali diunduh dari internet. Buka **System Settings → Privacy & Security**, gulir ke bawah, klik **Open Anyway** untuk Zip2Markdown; atau jalankan sekali:

```bash
xattr -dr com.apple.quarantine /Applications/Zip2Markdown.app
```

LibreOffice (opsional) hanya diperlukan untuk format Office lama: `brew install --cask libreoffice`.

## Cara pakai

1. Pasang dari DMG (di atas), atau build sendiri: `scripts/build_app.sh` (`--install` untuk sekalian menyalin ke /Applications).
2. Buka Zip2Markdown.
3. Tarik & lepas ZIP / folder / dokumen ke mana saja di jendela, atau klik **Pilih File…**, atau klik kanan file di Finder → *Open With* → Zip2Markdown.
4. Klik **Konversi ke Markdown**. Hasil `.md` disimpan di samping file input (bisa diubah lewat **Ubah…**).

**Banyak input sekaligus.** Beberapa ZIP, folder, dan file lepas (PDF, Word, Excel, gambar, …) bisa dimasukkan bersamaan — tarik semuanya sekaligus, tarik lagi untuk menambah, atau pilih beberapa di dialog (⌘-klik). Semuanya digabung menjadi satu Markdown (`Gabungan dokumen.md`), berurutan sesuai daftar input; isi tiap ZIP diberi awalan nama ZIP-nya (`arsip.zip › folder/file.pdf`). Satu ZIP yang rusak tidak menggagalkan input lain — ia hanya dicatat di *Laporan pemrosesan*.

> Setelah build/instal baru, macOS mengompilasi model OCR untuk app ini dan memindai library yang dibundel saat pertama kali dimuat (±35 detik, sekali saja). App melakukan pemanasan ini otomatis di latar belakang begitu dibuka: tunggu sampai tulisan di kiri bawah berubah dari **Menyiapkan mesin OCR…** menjadi **Mesin siap**, dan konversi pertama pun langsung cepat.

### CLI (tanpa GUI)

```bash
scripts/zip2md arsip.zip                    # -> arsip.md
scripts/zip2md arsip.zip -o hasil.md --force-ocr --langs id,en
scripts/zip2md folder/ file.pdf -o gabungan.md
dist/Zip2Markdown.app/Contents/Resources/engine/zip2md arsip.zip   # versi yang dibundel
```

Opsi: `--langs` (bahasa OCR, default `id,en`; string kosong = deteksi otomatis), `--force-ocr`, `--no-embedded-ocr`, `--fast`, `--title`, `--json` (progres JSON-lines).

## Format yang didukung

| Jenis | Ekstensi | Cara konversi |
|---|---|---|
| Word / PowerPoint / Excel | docx, docm, pptx, pptm, ppsx, xlsx, xlsm, xls | markitdown (Microsoft) + OCR gambar di dalamnya |
| Office lama & lainnya | doc, dot, rtf, odt, wpd, wps, pages, ppt, pps, pot, odp, key, ods, xlsb, numbers | LibreOffice → format modern → markitdown (cadangan: textutil macOS untuk doc/rtf/odt) |
| PDF | pdf | Per halaman: teks digital → PyMuPDF4LLM (tata letak, tabel, judul); halaman scan → OCR; gambar besar di halaman teks → OCR |
| Gambar | png, jpg, jpeg, tif/tiff (multi-halaman), bmp, gif, webp, heic, heif, jp2 | OCR Apple Vision (paragraf, daftar dengan penanda aslinya, **tabel**) |
| Teks & data | txt, md, csv/tsv (deteksi `,` `;` tab), json, xml, yaml, log, kode sumber | Deteksi encoding otomatis; CSV jadi tabel; kode dalam code block |
| Lainnya | html, epub, msg, eml (**lampirannya ikut dikonversi**), ipynb, ZIP bersarang | markitdown / parser email / ekstraksi rekursif |

File tanpa ekstensi (atau berekstensi tak dikenal) dikenali dari isinya: PDF, gambar, Word/Excel/PowerPoint lama maupun baru, RTF, EPUB. Audio, video, program, dan arsip RAR/7z dilewati, lalu dicatat di *Laporan pemrosesan*. File Office berpassword dilaporkan sebagai "dilindungi password".

## Praktik terbaik yang diterapkan pada output Markdown

- **Front matter YAML** (judul, sumber, waktu, versi alat, bahasa OCR, statistik) agar mudah diproses mesin/LLM.
- **Daftar isi** dengan anchor eksplisit (`<a id="file-N">`) yang stabil di semua renderer.
- **Satu bagian `##` per file**, dengan baris metadata (jenis, ukuran, halaman, metode, keyakinan OCR).
- **Hierarki judul konsisten**: judul di dalam dokumen digeser 2 level (H1 → H3) agar tidak bertabrakan dengan struktur utama. Level di atas H6 diubah jadi teks tebal.
- **Batas halaman PDF** ditandai `<!-- Halaman N -->`. Halaman hasil OCR diberi label `<!-- Halaman N · OCR (98%) -->`.
- **Tabel** tetap tabel Markdown, termasuk tabel dari hasil scan. Kolom/baris kosong dibuang, dan header kosong dari Word dinaikkan.
- **Normalisasi**: Unicode NFC, akhir baris LF, karakter kontrol & spasi di akhir baris dihapus, baris kosong berlebih dirapatkan.
- **Tidak ada gambar base64 / link gambar rusak**: gambar diganti teks OCR-nya atau penanda `*[gambar]*`.
- **Teks biasa aman**: baris yang diawali `#`, `---`, `===` di-escape agar tidak berubah menjadi judul, dan teks seperti `<isi nama>` di-escape agar tidak hilang sebagai tag HTML saat dirender (berlaku juga untuk hasil OCR dan sel tabel).
- **Penanda daftar dipertahankan**: `a.`, `b.`, `4.`, `1.1` pada hasil OCR ditulis seperti di dokumen ("huruf a" tetap huruf a; daftar lanjutan tetap mulai dari 4).
- **Code block tidak bocor**: pagar kode yang tidak ditutup di satu file ditutup otomatis, sehingga file berikutnya tidak ikut menjadi kode.
- **Laporan pemrosesan** di akhir: file gagal/dilewati beserta alasannya. Satu file rusak tidak menghentikan proses.
- **Penulisan atomik**: hasil ditulis ke `.part` lalu di-rename, jadi tidak pernah ada file setengah jadi.

## Kualitas OCR, kecepatan, dan ketahanan

Diukur dengan `scripts/bench.py` (22 kasus dengan teks acuan + 12 file `.doc`, di MacBook M3 8 GB):

| | Versi awal | 1.0 | 1.1 (sekarang) |
|---|---|---|---|
| Kata terbaca benar (rata-rata) | 97,1% | 99,5% | **99,7%** |
| Urutan baca benar (rata-rata) | 78,3% | 99,5% | **99,7%** |
| PDF scan 30 halaman | 10,0 dtk | 2,7 dtk | 2,9 dtk |
| 12 file `.doc` lama | 16,2 dtk | 6,4 dtk | **3,8 dtk** |
| ZIP sampel (19 file campuran) | ±10 dtk | 4,0 dtk | **1,7 dtk** |

Yang membuat perbedaan pada kualitas:

- **Teks kecil terbaca**: `minimumTextHeightFraction = 0`. Nilai bawaan Vision (1/32 tinggi gambar) membuat huruf kecil pada halaman penuh salah baca (26% → 99%), dan angka satu digit di tabel hilang.
- **Urutan baca bawaan Vision dipertahankan**: halaman dua kolom dan scan terputar 90°/180°/270° terbaca berurutan (31–58% → 97–100%). Tabel dan daftar disisipkan di posisi paragraf pertamanya.
- **Kata terpenggal disambung**: `pelak-` + `sanaan` → `pelaksanaan`, sedangkan kata ulang `anak-` + `anak` tetap `anak-anak`.
- **Salah baca khas diperbaiki**: `ı` (i tanpa titik) pada teks Indonesia/Inggris selalu salah baca dari `i`.
- **Scan dengan lapisan teks lama** (teks tak terlihat buatan software scanner) dibaca ulang dengan Vision, bukan dipercaya begitu saja.
- **Gambar di halaman teks** diambil langsung dari PDF (JPEG dipakai apa adanya, tanpa encode ulang), sehingga teks di atas gambar latar tidak terbaca dobel. Gambar yang berulang di 3+ halaman (kop surat, latar) dilewati.
- **Halaman PDF nyaris kosong** tanpa gambar/gambar vektor tidak di-OCR sia-sia; teks pendeknya dipakai langsung. Halaman yang dikosongkan mesin tata letak padahal ada teksnya memakai teks biasa.
- **Judul palsu dicegah**: kalimat panjang yang dilabeli "judul" oleh Vision tidak dijadikan heading. Baris berkeyakinan sangat rendah (noise) dibuang.

Yang membuat perbedaan pada kecepatan:

- **Render halaman PDF di helper** (CoreGraphics): paralel, tanpa file PNG sementara, pada resolusi asli scan.
- **Satu proses OCR per performance core**, dijalankan hanya saat dibutuhkan (ZIP tanpa gambar/scan tidak menyalakan OCR sama sekali).
- **OCR berjalan bersamaan dengan ekstraksi teks** pada PDF campuran, dan PDF panjang melepas giliran setiap 8 halaman sehingga PDF lain tidak menunggunya selesai. Level judul tetap dihitung dari seluruh dokumen, jadi hasilnya identik dengan ekstraksi sekali jalan.
- **LibreOffice**: satu sesi per format tujuan untuk semua file Office lama, sesi berjalan berdampingan, dan profilnya disimpan di `~/Library/Caches/Zip2Markdown` — menghemat ±1,4 dtk per sesi yang sebelumnya dipakai membuat profil baru di setiap konversi.
- **TIFF multi-halaman** dibaca langsung per halaman oleh helper, tanpa konversi ke PNG.

Ketahanan terhadap error:

- **Uji-diri OCR.** Vision menjalankan modelnya di Neural Engine, yang dikompilasi sekali per identitas executable oleh layanan sistem (±35 dtk pada pemakaian pertama). Selama kompilasi itu belum siap atau bermasalah, helper hanya mengembalikan beberapa kata per halaman (teramati: 7 dari 166 kata) atau tidak pernah menjawab — tanpa pesan error apa pun. Karena itu setiap proses OCR lebih dulu membaca halaman acuan bawaan yang teksnya diketahui, lalu mengulanginya tiap 25 halaman. Proses yang gagal dimulai ulang dan diuji lagi sampai lolos (maks. 60 dtk, progres menampilkan "Menunggu mesin OCR siap…"). Jika tetap gagal, dicoba salinan helper bernama lain di `~/Library/Caches/Zip2Markdown` (identitas lain tidak ikut terganggu, tetapi perlu kompilasi ±35 dtk sendiri — karena itu ia pilihan terakhir). Kejadiannya dilaporkan di progres; halaman yang mungkin terdampak, atau kondisi "tidak ada yang lolos uji-diri", ditulis sebagai peringatan di awal file hasil.
- Setiap permintaan OCR punya **batas waktu**. Proses yang macet dimatikan dan permintaannya **diulang sekali** di proses baru (yang harus lolos uji-diri dulu).
- Setelah 4 kegagalan beruntun, OCR dihentikan cepat (tidak menunggu berjam-jam), dan file terkait dicatat di laporan.
- Permintaan OCR pertama berjalan sendirian (start dingin tidak diperebutkan beberapa proses).
- Halaman PDF yang gagal di-OCR tetap memakai lapisan teks yang ada. Jika helper gagal merender PDF, PyMuPDF jadi cadangan.
- Halaman dengan keyakinan OCR < 50% diberi peringatan **"periksa hasilnya"** di file hasil (PDF, gambar, dan TIFF).
- Profil LibreOffice yang rusak dibuang dan konversi diulang dengan profil baru; dua Zip2Markdown yang berjalan bersamaan tidak berebut profil.
- Tombol Batal mematikan OCR dan LibreOffice yang sedang berjalan, mencegah proses baru dimulai, dan membersihkan folder sementara.

Catatan: memaksa Vision ke CPU/GPU (`ZIP2MD_OCR_DEVICE=cpu`) memang lepas dari Neural Engine dan sedikit lebih cepat, tetapi akurasinya jatuh pada foto yang sulit (99,6% → 6,6% pada kasus uji), jadi bawaan tetap `auto`.

```bash
engine/.venv/bin/python scripts/bench.py gen /tmp/bench   # buat kasus uji + teks acuan
engine/.venv/bin/python scripts/bench.py run /tmp/bench   # akurasi & waktu per kasus
```

## Keamanan ZIP

Proteksi zip-slip, batas zip-bomb (8 GB / 50.000 file / rasio kompresi), dekode nama file non-UTF-8 (Windows), ZIP bersarang hingga 3 level, serta file sampah (`__MACOSX`, `.DS_Store`, `Thumbs.db`, `~$lock`) diabaikan.

## Arsitektur

```
app/                      Swift Package (macOS 26+)
  Sources/Zip2MarkdownApp  GUI SwiftUI: drag & drop, opsi, progres, pratinjau, ikon Dock
  Sources/OCRHelper        ocr-helper: Apple Vision RecognizeDocumentsRequest -> Markdown
engine/                   Mesin Python (uv): zip2md
  src/zip2md/archive.py    ekstraksi ZIP yang aman
  src/zip2md/converters.py konverter per jenis file
  src/zip2md/ocr.py        klien ocr-helper (proses persisten, paralel, uji-diri) + cadangan Tesseract
  src/zip2md/pipeline.py   orkestrasi paralel + penyusunan Markdown
  src/zip2md/mdutil.py     normalisasi & utilitas Markdown
  src/zip2md/paths.py      lokasi cache (profil LibreOffice, helper cadangan)
scripts/
  build_app.sh             build .app mandiri (Swift + PyInstaller + ikon + codesign)
  make_dmg.sh              kemas dist/Zip2Markdown.app menjadi DMG tarik-ke-Applications
  zip2md                   menjalankan mesin dari source (dev)
  make_samples.py          membuat ZIP uji berisi semua jenis file
  bench.py                 benchmark akurasi OCR & kecepatan dengan teks acuan
```

`Zip2Markdown.app` berisi GUI, `ocr-helper`, dan mesin Python yang sudah dibekukan, sehingga **tidak perlu Python** di Mac tujuan. LibreOffice hanya diperlukan untuk format Office lama (`.doc`, `.ppt`, `.odt`, …).

## Pengembangan

```bash
scripts/build_app.sh && scripts/make_dmg.sh            # app + DMG di dist/
cd engine && uv sync                                   # dependensi Python
cd app && swift build -c release                       # GUI + ocr-helper
engine/.venv/bin/python scripts/make_samples.py /tmp/x # ZIP uji
scripts/zip2md /tmp/x/sampel.zip                       # tes mesin
swift run --package-path app Zip2MarkdownApp           # GUI mode dev
```

Catatan: folder `~/Documents` disinkronkan iCloud dan menandai file `.pth` di venv sebagai *hidden*, sehingga Python mengabaikannya. Karena itu `scripts/zip2md` memakai `PYTHONPATH`, dan `build_app.sh` merakit & menandatangani app di `$TMPDIR`.

## Lisensi komponen

markitdown (MIT), Apple Vision (sistem), PyMuPDF & PyMuPDF4LLM (**AGPL-3.0** atau lisensi komersial Artifex: aman untuk pemakaian pribadi/internal, tapi perhatikan jika aplikasi didistribusikan), LibreOffice (MPL, opsional, tidak dibundel).

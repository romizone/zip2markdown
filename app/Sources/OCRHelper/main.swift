// ocr-helper — on-device OCR with Apple Vision, emitting Markdown.
//
// Usage:
//   ocr-helper --list-langs
//   ocr-helper [--langs id,en] [--fast] <image> [<image>...]   # prints Markdown
//   ocr-helper --serve                                          # JSON-lines on stdin/stdout
//
// Serve protocol (one JSON object per line):
//   in : {"id":"1","path":"/x.png","langs":["id","en"],"fast":false}
//   in : {"id":"2","path":"/x.pdf","page":0,"dpi":300, ...}      # render a PDF page, then OCR
//   in : {"id":"3","path":"/fax.tiff","frame":2, ...}            # one page of a multi-page TIFF
//   in : {"id":"4","selftest":true, ...}                         # read the built-in reference page
//   out: {"id":"1","ok":true,"markdown":"...","confidence":0.93,"engine":"vision-document"}
//
// Quality notes (all measured with scripts/bench.py):
// * minimumTextHeightFraction = 0. The default (1/32 of the image height) makes Vision
//   ignore or misread small print on full pages: 26% -> 99% word recall on 8pt text.
// * Paragraphs are emitted in Vision's own reading order, which already handles
//   multi-column pages and sideways / upside-down scans. Tables and lists are
//   slotted in where their first paragraph appears.
// * Words hyphenated across line ends are re-joined; near-zero-confidence lines dropped.
// * PDF pages are rendered here with CoreGraphics: no temp PNGs, and rendering runs in
//   parallel across helper processes instead of behind the Python PDF lock.

import CoreGraphics
import CoreML
import CoreText
import Foundation
import ImageIO
import Vision

// MARK: - Models

struct OCRRequest: Decodable {
    let id: String
    var path: String?
    var langs: [String]?
    var fast: Bool?
    var page: Int?        // 0-based PDF page; when set, `path` is a PDF
    var dpi: Double?
    var frame: Int?       // 0-based frame of a multi-page TIFF
    var selftest: Bool?   // read the built-in reference page instead of `path`
}

struct OCRResponse: Encodable {
    let id: String
    let ok: Bool
    var markdown: String = ""
    var confidence: Double = 0
    var engine: String = ""
    var error: String?
    var mem_mb: Int = footprintMB()   // lets the engine recycle a worker that grew too big
}

/// This process's memory footprint (what Activity Monitor shows as "Memory"), in MB.
func footprintMB() -> Int {
    var info = rusage_info_v4()
    let rc = withUnsafeMutablePointer(to: &info) {
        $0.withMemoryRebound(to: rusage_info_t?.self, capacity: 1) {
            proc_pid_rusage(getpid(), RUSAGE_INFO_V4, $0)
        }
    }
    return rc == 0 ? Int(info.ri_phys_footprint / 1_048_576) : 0
}

struct OCRResult {
    var markdown: String
    var confidence: Double
    var engine: String
}

enum OCRError: Error, CustomStringConvertible {
    case unreadableImage(String)
    case pdf(String)
    var description: String {
        switch self {
        case .unreadableImage(let p): return "Cannot read image: \(p)"
        case .pdf(let m): return "Cannot render PDF: \(m)"
        }
    }
}

/// Which processor runs the Vision models: "cpu", "gpu", "ane" or "auto" (Vision decides).
let computeChoice = (ProcessInfo.processInfo.environment["ZIP2MD_OCR_DEVICE"] ?? "auto").lowercased()

func pick(_ devices: [MLComputeDevice]) -> MLComputeDevice? {
    devices.first { device in
        switch (device, computeChoice) {
        case (.cpu, "cpu"), (.gpu, "gpu"), (.neuralEngine, "ane"): return true
        default: return false
        }
    }
}

/// Lines below this confidence are almost always noise (texture, stamps, photos).
let minLineConfidence: Float = 0.2

// MARK: - Image loading

/// Loads one frame of an image with EXIF orientation applied, so rotated
/// phone photos are recognised upright.
func loadImage(_ path: String, frame: Int = 0) throws -> (CGImage, CGImagePropertyOrientation) {
    let url = URL(fileURLWithPath: path) as CFURL
    guard let src = CGImageSourceCreateWithURL(url, nil),
          frame >= 0, frame < max(CGImageSourceGetCount(src), 1),
          let img = CGImageSourceCreateImageAtIndex(src, frame, nil)
    else { throw OCRError.unreadableImage(path) }
    var orientation = CGImagePropertyOrientation.up
    if let props = CGImageSourceCopyPropertiesAtIndex(src, frame, nil) as? [CFString: Any],
       let raw = props[kCGImagePropertyOrientation] as? UInt32,
       let o = CGImagePropertyOrientation(rawValue: raw) {
        orientation = o
    }
    return (img, orientation)
}

// MARK: - PDF page rendering

/// Opened fresh for every page, on purpose. A CGPDFDocument keeps every picture it
/// has decoded for as long as it lives: kept open across requests, one worker grew
/// from 150 MB to 1.3 GB within 30 scanned pages (measured), and with one worker per
/// performance core that alone exhausted an 8 GB Mac. Opening is lazy (no page is
/// parsed), so re-opening costs nothing measurable.
func pdfDocument(_ path: String) throws -> CGPDFDocument {
    guard let doc = CGPDFDocument(URL(fileURLWithPath: path) as CFURL) else {
        throw OCRError.pdf("cannot open")
    }
    if doc.isEncrypted && !doc.isUnlocked && !doc.unlockWithPassword("") {
        throw OCRError.pdf("password protected")
    }
    return doc
}

func renderPDFPage(path: String, page: Int, dpi: Double) throws -> CGImage {
    try autoreleasepool { try renderPDFPageUnpooled(path: path, page: page, dpi: dpi) }
}

private func renderPDFPageUnpooled(path: String, page: Int, dpi: Double) throws -> CGImage {
    guard let pg = try pdfDocument(path).page(at: page + 1) else { throw OCRError.pdf("no page \(page + 1)") }
    let box = pg.getBoxRect(.cropBox)
    let sideways = pg.rotationAngle % 180 != 0
    let wPt = sideways ? box.height : box.width
    let hPt = sideways ? box.width : box.height
    guard wPt > 1, hPt > 1 else { throw OCRError.pdf("empty page") }
    var scale = max(dpi, 72) / 72
    let longest = max(wPt, hPt) * scale
    if longest > 6000 { scale *= 6000 / longest }
    let w = Int((wPt * scale).rounded()), h = Int((hPt * scale).rounded())
    guard let ctx = CGContext(data: nil, width: w, height: h, bitsPerComponent: 8, bytesPerRow: 0,
                              space: CGColorSpaceCreateDeviceRGB(),
                              bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue)
    else { throw OCRError.pdf("no bitmap context") }
    ctx.setFillColor(CGColor(red: 1, green: 1, blue: 1, alpha: 1))
    ctx.fill(CGRect(x: 0, y: 0, width: w, height: h))
    ctx.interpolationQuality = .high
    ctx.scaleBy(x: scale, y: scale)
    // Handles /Rotate and a crop box that does not start at the origin.
    ctx.concatenate(pg.getDrawingTransform(.cropBox, rect: CGRect(x: 0, y: 0, width: wPt, height: hPt),
                                           rotate: 0, preserveAspectRatio: true))
    ctx.drawPDFPage(pg)
    guard let img = ctx.makeImage() else { throw OCRError.pdf("render failed") }
    return img
}

// MARK: - Markdown helpers

func isLetter(_ c: Character?) -> Bool { c?.isLetter ?? false }

/// Join the lines of one paragraph, undoing end-of-line hyphenation:
///   "pelak-" + "sanaan"  -> "pelaksanaan"   (word split by the typesetter)
///   "anak-"  + "anak"    -> "anak-anak"     (Indonesian reduplication keeps its hyphen)
///   "se-"    + "Indonesia" / "ke-" + "2" -> hyphen kept, no space
func joinLines(_ lines: [String]) -> String {
    var out = ""
    for raw in lines {
        let line = raw.trimmingCharacters(in: .whitespaces)
        if line.isEmpty { continue }
        if out.isEmpty { out = line; continue }
        let chars = Array(out)
        if chars.count >= 2, chars.last == "-", isLetter(chars[chars.count - 2]), let first = line.first,
           first.isLetter || first.isNumber {
            let lastWord = String(out.dropLast().split(whereSeparator: { !$0.isLetter }).last ?? "")
            let nextWord = String(line.prefix(while: { $0.isLetter }))
            let reduplication = !lastWord.isEmpty && lastWord.lowercased() == nextWord.lowercased()
            if first.isLowercase && !reduplication {
                out.removeLast()            // drop the typesetter's hyphen
            }
            out += line
        } else {
            out += " " + line
        }
    }
    return out
}

func cleanInline(_ s: String) -> String {
    s.replacingOccurrences(of: "\r", with: "")
        .split(separator: "\n", omittingEmptySubsequences: true)
        .map { $0.trimmingCharacters(in: .whitespaces) }
        .joined(separator: " ")
        .trimmingCharacters(in: .whitespaces)
}

func escapeCell(_ s: String) -> String {
    cleanInline(s).replacingOccurrences(of: "|", with: "\\|")
}

func markdownTable(_ input: [[String]]) -> String {
    let rows = input.filter { $0.contains { !$0.isEmpty } }
    let fullWidth = rows.map(\.count).max() ?? 0
    guard fullWidth > 0, !rows.isEmpty else { return "" }
    var padded = rows.map { $0 + Array(repeating: "", count: fullWidth - $0.count) }
    // drop columns that are empty in every row (table borders detected as cells)
    let keep = (0..<fullWidth).filter { c in padded.contains { !$0[c].isEmpty } }
    padded = padded.map { r in keep.map { r[$0] } }
    let width = keep.count
    guard width > 0 else { return "" }
    var out: [String] = []
    out.append("| " + padded[0].joined(separator: " | ") + " |")
    out.append("|" + Array(repeating: " --- |", count: width).joined())
    for r in padded.dropFirst() {
        out.append("| " + r.joined(separator: " | ") + " |")
    }
    return out.joined(separator: "\n")
}

/// Recognised text is plain text, not Markdown/HTML: "<isi nama>" must not vanish as
/// an unknown HTML tag when the result is rendered.
func escapeText(_ md: String) -> String {
    md.replacingOccurrences(of: "<(?=[A-Za-z/!?])", with: "\\\\<", options: .regularExpression)
}

/// With Indonesian/English text a dotless "ı" is always a misread "i".
func fixGlyphs(_ s: String, langs: [String]) -> String {
    if langs.contains(where: { $0.hasPrefix("tr") || $0.hasPrefix("az") }) || langs.isEmpty { return s }
    return s.replacingOccurrences(of: "ı", with: "i")
}

/// Bounding box in normalized coordinates (origin lower-left).
func box(_ region: NormalizedRegion) -> CGRect {
    region.normalizedPath.boundingBox
}

// MARK: - Document recognition (macOS 26+)

@available(macOS 26.0, *)
func recognizeDocument(_ image: CGImage, _ orientation: CGImagePropertyOrientation,
                       langs: [String]) async throws -> OCRResult {
    var request = RecognizeDocumentsRequest()
    request.textRecognitionOptions.recognitionLanguages = langs.map { Locale.Language(identifier: $0) }
    request.textRecognitionOptions.useLanguageCorrection = true
    request.textRecognitionOptions.automaticallyDetectLanguage = langs.isEmpty
    request.textRecognitionOptions.minimumTextHeightFraction = 0     // read small print too
    for (stage, devices) in request.supportedComputeStageDevices {
        if let device = pick(devices) { request.setComputeDevice(device, for: stage) }
    }

    let observations = try await request.perform(on: image, orientation: orientation)
    guard let doc = observations.first?.document else {
        return OCRResult(markdown: "", confidence: 0, engine: "vision-document")
    }

    // Tables and lists own a region of the page; paragraphs inside it are duplicates.
    struct Region { let rect: CGRect; let md: String; var emitted = false }
    var regions: [Region] = []
    for table in doc.tables {
        let rows = table.rows.map { row in row.map { escapeCell($0.content.text.transcript) } }
        let md = markdownTable(rows)
        if !md.isEmpty {
            regions.append(Region(rect: box(table.boundingRegion).insetBy(dx: -0.005, dy: -0.005), md: md))
        }
    }
    for list in doc.lists {
        // Keep the markers as printed: "a." / "4." / "1.1" carry meaning (legal texts
        // refer to "huruf a", a list may continue from the previous page at 4).
        var items: [(marker: String, number: Int?, text: String)] = []
        for item in list.items {
            let marker = item.markerString.trimmingCharacters(in: .whitespaces)
            var lines = item.content.text.lines.map { cleanInline($0.transcript) }
            if let first = lines.first, !marker.isEmpty, first.hasPrefix(marker) {
                lines[0] = String(first.dropFirst(marker.count))
            }
            var text = joinLines(lines)
            if text.isEmpty { text = cleanInline(item.itemString) }
            guard !text.isEmpty else { continue }
            var number: Int?
            var bullet = marker.isEmpty
            switch item.markerType {
            case .bullet?, .hyphen?: bullet = true
            case .decimal?: number = Int(marker.filter(\.isNumber))
            default: break
            }
            items.append((bullet ? "" : marker, number, text))
        }
        // A real Markdown ordered list only when the numbers run 4, 5, 6, ...: renderers
        // renumber from the first item, which would silently "fix" a gap.
        let numbers = items.compactMap(\.number)
        let consecutive = numbers.count == items.count && !numbers.isEmpty
            && zip(numbers, numbers.dropFirst()).allSatisfy { $1 == $0 + 1 }
        let lines = items.map { it in
            if it.marker.isEmpty { return "- \(it.text)" }
            return consecutive ? "\(it.number!). \(it.text)" : "- \(it.marker) \(it.text)"
        }
        if !lines.isEmpty {
            regions.append(Region(rect: box(list.boundingRegion).insetBy(dx: -0.005, dy: -0.005),
                                  md: lines.joined(separator: "\n")))
        }
    }

    // Line height in pixels (rotation-proof), used to spot headings.
    let W = CGFloat(image.width), H = CGFloat(image.height)
    func lineHeight(_ l: RecognizedTextObservation) -> CGFloat {
        hypot((l.topLeft.x - l.bottomLeft.x) * W, (l.topLeft.y - l.bottomLeft.y) * H)
    }
    let heights = doc.paragraphs.flatMap { $0.lines.map(lineHeight) }.sorted()
    let medianHeight = heights.isEmpty ? 0 : heights[heights.count / 2]

    // Vision sometimes labels a whole sentence as the "title" — only accept heading-like text.
    var titleText = doc.title.map { cleanInline($0.transcript) } ?? ""
    if titleText.count > 90 || titleText.split(separator: " ").count > 12
        || titleText.last.map({ ".,;:".contains($0) }) == true {
        titleText = ""
    }
    var titleUsed = false
    var blocks: [String] = []
    var confidences: [Double] = []

    // Vision's paragraph order IS the reading order (columns, rotated pages) — keep it.
    for p in doc.paragraphs {
        let b = box(p.boundingRegion)
        let center = CGPoint(x: b.midX, y: b.midY)
        if let i = regions.firstIndex(where: { $0.rect.contains(center) }) {
            if !regions[i].emitted {
                blocks.append(regions[i].md)
                regions[i].emitted = true
            }
            continue
        }
        let good = p.lines.filter { $0.confidence >= minLineConfidence }
        let text = p.lines.isEmpty ? cleanInline(p.transcript) : joinLines(good.map { cleanInline($0.transcript) })
        if text.isEmpty { continue }
        for line in good { confidences.append(Double(line.confidence)) }

        if !titleUsed, !titleText.isEmpty, text == titleText {
            blocks.append("# " + text)
            titleUsed = true
            continue
        }
        // A short, single, clearly larger line is a heading.
        if good.count == 1, medianHeight > 0, heights.count >= 4, text.count <= 90,
           lineHeight(good[0]) >= medianHeight * 1.3,
           let last = text.last, !".,;:".contains(last) {
            blocks.append("## " + text)
            continue
        }
        // not a heading, whatever the text starts with
        blocks.append(text.hasPrefix("#") ? "\\" + text : text)
    }
    for r in regions where !r.emitted { blocks.append(r.md) }

    let conf = confidences.isEmpty
        ? (blocks.isEmpty ? 0 : Double(observations.first?.confidence ?? 0))
        : confidences.reduce(0, +) / Double(confidences.count)
    let md = fixGlyphs(escapeText(blocks.joined(separator: "\n\n")), langs: langs)
    return OCRResult(markdown: md, confidence: conf, engine: "vision-document")
}

// MARK: - Line recognition (fallback)

func recognizeLines(_ image: CGImage, _ orientation: CGImagePropertyOrientation,
                    langs: [String], fast: Bool) async throws -> OCRResult {
    var request = RecognizeTextRequest()
    request.recognitionLevel = fast ? .fast : .accurate
    request.usesLanguageCorrection = true
    request.minimumTextHeightFraction = 0
    for (stage, devices) in request.supportedComputeStageDevices {
        if let device = pick(devices) { request.setComputeDevice(device, for: stage) }
    }
    if langs.isEmpty {
        request.automaticallyDetectsLanguage = true
    } else {
        request.recognitionLanguages = langs.map { Locale.Language(identifier: $0) }
    }
    let lines = try await request.perform(on: image, orientation: orientation)

    // Group lines into paragraphs using the vertical gap between them.
    var paragraphs: [[String]] = []
    var lastBottom: CGFloat?
    var lastHeight: CGFloat = 0
    var confs: [Double] = []
    let sorted = lines.sorted { box($0.boundingRegion).maxY > box($1.boundingRegion).maxY }
    for line in sorted {
        let b = box(line.boundingRegion)
        let text = cleanInline(line.transcript)
        guard !text.isEmpty, line.confidence >= minLineConfidence else { continue }
        confs.append(Double(line.confidence))
        if let bottom = lastBottom, bottom - b.maxY < max(lastHeight, b.height) * 0.9,
           !paragraphs.isEmpty {
            paragraphs[paragraphs.count - 1].append(text)
        } else {
            paragraphs.append([text])
        }
        lastBottom = b.minY
        lastHeight = b.height
    }
    let md = fixGlyphs(escapeText(paragraphs.map(joinLines)
        .map { $0.hasPrefix("#") ? "\\" + $0 : $0 }.joined(separator: "\n\n")), langs: langs)
    let conf = confs.isEmpty ? 0 : confs.reduce(0, +) / Double(confs.count)
    return OCRResult(markdown: md, confidence: conf, engine: "vision-text")
}

func ocr(path: String, langs: [String], fast: Bool, page: Int? = nil, dpi: Double? = nil,
         frame: Int? = nil) async throws -> OCRResult {
    let image: CGImage
    var orientation = CGImagePropertyOrientation.up
    if let page {
        image = try renderPDFPage(path: path, page: page, dpi: dpi ?? 300)
    } else {
        (image, orientation) = try autoreleasepool { try loadImage(path, frame: frame ?? 0) }
    }
    if !fast, #available(macOS 26.0, *) {
        let r = try await recognizeDocument(image, orientation, langs: langs)
        if !r.markdown.isEmpty { return r }
    }
    return try await recognizeLines(image, orientation, langs: langs, fast: fast)
}

// MARK: - Self-test
//
// Vision runs its text models on the Neural Engine. Now and then that goes wrong for
// one process (seen in testing: the same binary returned 7 of 166 words, or never
// answered) while other processes on the same Mac read the same page perfectly.
// Nothing in the result says so, so each worker proves itself on a page whose text
// is known before it is trusted with real documents, and again every so often.

let selfTestLines = [
    "Pemerintah daerah menetapkan peraturan baru tentang pengelolaan anggaran",
    "Kepala dinas menyampaikan laporan kegiatan kepada seluruh pegawai",
    "Pembayaran dilakukan melalui transfer bank setelah faktur diterima",
    "Nomor 045/SK/XI/2026 tanggal 17 Agustus 2026 sebesar Rp 1.250.000",
    "Surat keputusan ini berlaku sejak ditandatangani oleh pejabat berwenang",
    "Peserta pelatihan memperoleh sertifikat setelah menyelesaikan ujian akhir",
    "Masyarakat diharapkan berpartisipasi aktif mendukung program pembangunan",
    "Hasil pemeriksaan menunjukkan sebagian besar persyaratan sudah terpenuhi",
]

func selfTestImage() -> CGImage? {
    let w = 1500, h = 120 + selfTestLines.count * 70
    guard let ctx = CGContext(data: nil, width: w, height: h, bitsPerComponent: 8, bytesPerRow: 0,
                              space: CGColorSpaceCreateDeviceRGB(),
                              bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue) else { return nil }
    ctx.setFillColor(CGColor(red: 1, green: 1, blue: 1, alpha: 1))
    ctx.fill(CGRect(x: 0, y: 0, width: w, height: h))
    let font = CTFontCreateWithName("Helvetica" as CFString, 32, nil)
    let attrs = [kCTFontAttributeName: font,
                 kCTForegroundColorAttributeName: CGColor(red: 0, green: 0, blue: 0, alpha: 1)] as CFDictionary
    for (i, text) in selfTestLines.enumerated() {
        guard let str = CFAttributedStringCreate(nil, text as CFString, attrs) else { return nil }
        ctx.textPosition = CGPoint(x: 70, y: CGFloat(h - 100 - i * 70))
        CTLineDraw(CTLineCreateWithAttributedString(str), ctx)
    }
    return ctx.makeImage()
}

func words(_ s: String) -> [String] {
    s.lowercased().split(whereSeparator: { !$0.isLetter }).map(String.init).filter { $0.count >= 4 }
}

/// Share of the reference page's words that came back (0...1).
func selfTest(langs: [String], fast: Bool) async -> Double {
    guard let image = selfTestImage() else { return 1 }      // cannot draw: nothing to conclude
    var text = ""
    if !fast, #available(macOS 26.0, *) {
        text = (try? await recognizeDocument(image, .up, langs: langs))?.markdown ?? ""
    } else {
        text = (try? await recognizeLines(image, .up, langs: langs, fast: fast))?.markdown ?? ""
    }
    let got = Set(words(text))
    let want = selfTestLines.flatMap(words)
    return Double(want.filter(got.contains).count) / Double(max(want.count, 1))
}

// MARK: - Entry point

let defaultLangs = ["id", "en"]
var args = Array(CommandLine.arguments.dropFirst())

func writeLine(_ s: String) {
    FileHandle.standardOutput.write((s + "\n").data(using: .utf8)!)
}

if args.contains("--list-devices") {
    if #available(macOS 26.0, *) {
        for (stage, devices) in RecognizeDocumentsRequest().supportedComputeStageDevices {
            print(stage, devices.map { d -> String in
                switch d { case .cpu: "cpu"; case .gpu: "gpu"; case .neuralEngine: "ane"; @unknown default: "?" }
            })
        }
    }
    exit(0)
}

if args.contains("--selftest") {
    let recall = await selfTest(langs: defaultLangs, fast: args.contains("--fast"))
    print(String(format: "self-test: %.0f%%", recall * 100))
    exit(recall >= 0.85 ? 0 : 1)
}

if args.contains("--list-langs") {
    if #available(macOS 26.0, *) {
        print(RecognizeDocumentsRequest().supportedRecognitionLanguages.map(\.minimalIdentifier).joined(separator: ","))
    } else {
        print(RecognizeTextRequest().supportedRecognitionLanguages.map(\.minimalIdentifier).joined(separator: ","))
    }
    exit(0)
}

if args.contains("--serve") {
    let encoder = JSONEncoder()
    let decoder = JSONDecoder()
    while let line = readLine(strippingNewline: true) {
        guard !line.trimmingCharacters(in: .whitespaces).isEmpty else { continue }
        var response: OCRResponse
        do {
            let req = try decoder.decode(OCRRequest.self, from: Data(line.utf8))
            if req.selftest == true {
                let recall = await selfTest(langs: req.langs ?? defaultLangs, fast: req.fast ?? false)
                var r = OCRResponse(id: req.id, ok: recall >= 0.85, confidence: recall, engine: "selftest")
                if !r.ok { r.error = "self-test: only \(Int(recall * 100))% of the reference page was read" }
                let data = (try? encoder.encode(r)) ?? Data("{\"id\":\"?\",\"ok\":false}".utf8)
                writeLine(String(decoding: data, as: UTF8.self))
                continue
            }
            do {
                let r = try await ocr(path: req.path ?? "", langs: req.langs ?? defaultLangs, fast: req.fast ?? false,
                                      page: req.page, dpi: req.dpi, frame: req.frame)
                response = OCRResponse(id: req.id, ok: true, markdown: r.markdown,
                                       confidence: r.confidence, engine: r.engine)
            } catch {
                response = OCRResponse(id: req.id, ok: false, error: "\(error)")
            }
        } catch {
            response = OCRResponse(id: "?", ok: false, error: "Bad request: \(error)")
        }
        let data = (try? encoder.encode(response)) ?? Data("{\"id\":\"?\",\"ok\":false}".utf8)
        writeLine(String(decoding: data, as: UTF8.self))
    }
    exit(0)
}

var langs = defaultLangs
var fast = false
var paths: [String] = []
var i = 0
while i < args.count {
    switch args[i] {
    case "--langs":
        i += 1
        if i < args.count { langs = args[i].split(separator: ",").map(String.init) }
    case "--fast":
        fast = true
    default:
        paths.append(args[i])
    }
    i += 1
}

if paths.isEmpty {
    FileHandle.standardError.write("usage: ocr-helper [--langs id,en] [--fast] <image>... | --serve | --list-langs\n".data(using: .utf8)!)
    exit(2)
}

var failed = false
for p in paths {
    do {
        let r = try await ocr(path: p, langs: langs, fast: fast)
        writeLine(r.markdown)
        FileHandle.standardError.write("[\(r.engine) confidence=\(String(format: "%.2f", r.confidence))]\n".data(using: .utf8)!)
    } catch {
        FileHandle.standardError.write("error: \(error)\n".data(using: .utf8)!)
        failed = true
    }
}
exit(failed ? 1 : 0)

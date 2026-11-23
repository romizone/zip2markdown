import AppKit
import Foundation
import Observation
import UniformTypeIdentifiers

struct LogEntry: Identifiable, Hashable {
    enum Status: String { case ok, ocr, error, skipped, info, warning }
    let id = UUID()
    let status: Status
    let path: String
    let detail: String?
}

struct Summary: Hashable {
    var converted = 0, ocr = 0, failed = 0, skipped = 0
    var seconds = 0.0
    var bytes = 0
}

enum OCRLanguage: String, CaseIterable, Identifiable {
    case idEn = "id,en", id = "id", en = "en", auto = ""
    var id: String { rawValue }
    var label: String {
        switch self {
        case .idEn: "Indonesia + Inggris"
        case .id: "Indonesia"
        case .en: "Inggris"
        case .auto: "Deteksi otomatis"
        }
    }
}

@MainActor
@Observable
final class ConverterModel {
    static let shared = ConverterModel()

    enum Phase: Equatable { case idle, running, finished, failed(String) }

    var inputs: [URL] = []
    /// Titles of pasted/typed text inputs (they live in `PastedStore.folder`).
    var labels: [URL: String] = [:]
    var output: URL?
    var outputIsCustom = false

    var language: OCRLanguage = .idEn {
        didSet { UserDefaults.standard.set(language.rawValue, forKey: "ocrLanguage") }
    }
    var forceOCR = false
    var embeddedOCR = true {
        didSet { UserDefaults.standard.set(embeddedOCR, forKey: "embeddedOCR") }
    }
    var fastOCR = false

    enum Engine { case warming, ready, unavailable }
    var engine: Engine = .warming

    var phase: Phase = .idle
    var stage = ""
    var total = 0
    var done = 0
    var log: [LogEntry] = []
    var summary = Summary()

    private let runner = EngineRunner()
    private var cancelled = false

    init() {
        let d = UserDefaults.standard
        if let raw = d.string(forKey: "ocrLanguage"), let l = OCRLanguage(rawValue: raw) { language = l }
        if d.object(forKey: "embeddedOCR") != nil { embeddedOCR = d.bool(forKey: "embeddedOCR") }
        PastedStore.clearAll()          // left over from an earlier session
        EngineRunner.warmUp { ok in
            Task { @MainActor in ConverterModel.shared.engine = ok ? .ready : .unavailable }
        }
    }

    var isRunning: Bool { phase == .running }
    var progress: Double { total == 0 ? 0 : Double(done) / Double(total) }

    // MARK: Inputs

    func add(_ urls: [URL]) {
        guard !isRunning else { return }
        for u in urls where !inputs.contains(u) { inputs.append(u) }
        refreshOutput()
        if phase != .running { phase = .idle }
    }

    func remove(_ url: URL) {
        guard !isRunning else { return }
        inputs.removeAll { $0 == url }
        labels[url] = nil
        PastedStore.delete(url)
        refreshOutput()
    }

    func clear() {
        for url in inputs { PastedStore.delete(url) }
        inputs = []
        labels = [:]
        output = nil
        outputIsCustom = false
        phase = .idle
        log = []
        total = 0
        done = 0
    }

    private func refreshOutput() {
        if !outputIsCustom { output = Self.defaultOutput(for: inputs, labels: labels) }
    }

    // MARK: Pasted & typed text

    func isPasted(_ url: URL) -> Bool { PastedStore.contains(url) }

    func label(for url: URL) -> String {
        labels[url] ?? url.lastPathComponent
    }

    /// ⌘V or the "Tempel" button: files on the clipboard are added as files; formatted
    /// text (Word, Pages, web) keeps its headings, lists and tables; plain text stays text.
    @discardableResult
    func pasteFromClipboard() -> Bool {
        guard !isRunning, let clip = PastedStore.readClipboard() else { return false }
        do {
            switch clip {
            case .files(let urls):
                add(urls)
            case .rich(let html, let plain):
                addPasted(try PastedStore.save(html: html), title: PastedStore.title(from: plain, fallback: nextTitle()))
            case .plain(let text):
                addPasted(try PastedStore.save(text: text, markdown: false),
                          title: PastedStore.title(from: text, fallback: nextTitle()))
            }
            return true
        } catch {
            phase = .failed("Teks tidak bisa disimpan: \(error.localizedDescription)")
            return false
        }
    }

    func newDraft() -> PasteDraft {
        PasteDraft(url: nil, title: nextTitle(), text: "", markdown: false, rich: false)
    }

    func draft(for url: URL) -> PasteDraft {
        let kind = PastedStore.kind(of: url)
        return PasteDraft(url: url, title: label(for: url),
                          text: kind == .rich ? "" : PastedStore.text(of: url),
                          markdown: kind == .markdown, rich: kind == .rich)
    }

    /// Saves the text editor sheet: a new input, or changes to an existing one (same place in the list).
    func save(_ d: PasteDraft) {
        guard !isRunning else { return }
        let title = d.title.trimmingCharacters(in: .whitespacesAndNewlines)
        do {
            if let url = d.url, let i = inputs.firstIndex(of: url) {
                var target = url
                if !d.rich { target = try PastedStore.update(url, text: d.text, markdown: d.markdown) }
                inputs[i] = target
                labels[url] = nil
                labels[target] = title.isEmpty ? PastedStore.title(from: d.text, fallback: nextTitle()) : title
                refreshOutput()
            } else if !d.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                addPasted(try PastedStore.save(text: d.text, markdown: d.markdown),
                          title: title.isEmpty ? PastedStore.title(from: d.text, fallback: nextTitle()) : title)
            }
        } catch {
            phase = .failed("Teks tidak bisa disimpan: \(error.localizedDescription)")
        }
    }

    private func addPasted(_ url: URL, title: String) {
        labels[url] = title
        add([url])
    }

    private func nextTitle() -> String {
        "Teks \(inputs.filter(isPasted).count + 1)"
    }

    static func defaultOutput(for inputs: [URL], labels: [URL: String] = [:]) -> URL? {
        guard let first = inputs.first else { return nil }
        let base: String
        if PastedStore.contains(first) {
            // a file name from the title: no "/" or ":", which Finder cannot show
            base = (labels[first] ?? "Teks tempelan")
                .replacingOccurrences(of: "/", with: "-").replacingOccurrences(of: ":", with: "-")
                .trimmingCharacters(in: CharacterSet(charactersIn: ". …"))
        } else {
            base = first.hasDirectoryPath ? first.lastPathComponent : first.deletingPathExtension().lastPathComponent
        }
        let name = inputs.count > 1 ? "Gabungan dokumen" : (base.isEmpty ? "Teks tempelan" : base)
        // Saved next to the first real file; pasted text alone goes to Documents
        // (never into the app's cache folder, which is emptied).
        let dir = inputs.first { !PastedStore.contains($0) }?.deletingLastPathComponent()
            ?? FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
        // Converting "catatan.md" must never overwrite "catatan.md" itself.
        // Compared without letter case: APFS volumes usually ignore it.
        let taken = Set(inputs.map { $0.standardizedFileURL.path.lowercased() })
        var candidate = dir.appendingPathComponent(name + ".md")
        var n = 1
        while taken.contains(candidate.standardizedFileURL.path.lowercased()) {
            n += 1
            let suffix = n == 2 ? " (Markdown)" : " (Markdown \(n - 1))"
            candidate = dir.appendingPathComponent(name + suffix + ".md")
        }
        return candidate
    }

    func chooseInputs() {
        let panel = NSOpenPanel()
        panel.title = "Pilih ZIP, folder, atau dokumen"
        panel.message = "Pilih satu atau beberapa sekaligus (⌘-klik atau ⇧-klik)"
        panel.allowsMultipleSelection = true
        panel.canChooseDirectories = true
        panel.canChooseFiles = true
        if panel.runModal() == .OK { add(panel.urls) }
    }

    func chooseOutput() {
        let panel = NSSavePanel()
        panel.title = "Simpan hasil Markdown"
        panel.allowedContentTypes = [UTType(filenameExtension: "md") ?? .plainText]
        panel.nameFieldStringValue = output?.lastPathComponent ?? "hasil.md"
        if let dir = output?.deletingLastPathComponent() { panel.directoryURL = dir }
        if panel.runModal() == .OK, let url = panel.url {
            output = url
            outputIsCustom = true
        }
    }

    // MARK: Conversion

    func start() {
        guard !inputs.isEmpty, let output, !isRunning else { return }
        phase = .running
        cancelled = false
        stage = "Menyiapkan…"
        log = []
        total = 0
        done = 0
        summary = Summary()

        let settings = EngineRunner.Settings(
            inputs: inputs, labels: labels, output: output, langs: language.rawValue,
            forceOCR: forceOCR, embeddedOCR: embeddedOCR, fast: fastOCR)
        do {
            try runner.start(settings, onEvent: { event in
                let e = EventBox(event)
                Task { @MainActor in ConverterModel.shared.handle(e) }
            }, onExit: { status, stderr in
                Task { @MainActor in ConverterModel.shared.finished(status: status, stderr: stderr) }
            })
        } catch {
            phase = .failed(error.localizedDescription)
        }
    }

    func cancel() {
        cancelled = true
        runner.cancel()
        stage = "Membatalkan…"
    }

    private func handle(_ box: EventBox) {
        let e = box.dict
        if cancelled { return }         // late events must not undo "Membatalkan…"
        switch e["event"] as? String {
        case "stage":
            stage = e["message"] as? String ?? ""
        case "start":
            total = e["total"] as? Int ?? 0
            stage = total == 0 ? "Tidak ada file yang bisa dikonversi" : "Mengonversi \(total) file…"
            if (e["ocr"] as? String) == nil {
                log.append(LogEntry(status: .warning, path: "OCR tidak tersedia", detail: "Gambar & PDF scan tidak akan terbaca"))
            }
        case "file":
            done = e["done"] as? Int ?? done
            let status = LogEntry.Status(rawValue: e["status"] as? String ?? "ok") ?? .ok
            let detail = (e["message"] as? String) ?? (e["method"] as? String)
            log.append(LogEntry(status: status, path: e["path"] as? String ?? "?", detail: detail))
        case "warning":
            log.append(LogEntry(status: .warning, path: e["message"] as? String ?? "", detail: nil))
        case "error":
            phase = .failed(e["message"] as? String ?? "Terjadi kesalahan")
        case "done":
            summary = Summary(
                converted: e["files_converted"] as? Int ?? 0,
                ocr: e["files_ocr"] as? Int ?? 0,
                failed: e["files_failed"] as? Int ?? 0,
                skipped: e["files_skipped"] as? Int ?? 0,
                seconds: e["seconds"] as? Double ?? 0,
                bytes: e["bytes"] as? Int ?? 0)
            stage = "Selesai"
        default:
            break
        }
    }

    private func finished(status: Int32, stderr: String) {
        if cancelled {                  // whatever the exit code: the user stopped it
            phase = .idle
            return
        }
        if case .failed = phase { return }
        if status == 0 {
            phase = .finished
            NSSound(named: "Glass")?.play()
        } else {
            let msg = stderr.split(separator: "\n").suffix(3).joined(separator: "\n")
            phase = .failed(msg.isEmpty ? "Mesin konversi berhenti (kode \(status))" : msg)
        }
    }

    // MARK: Result actions

    func openResult() {
        guard let output else { return }
        NSWorkspace.shared.open(output)
    }

    func revealResult() {
        guard let output else { return }
        NSWorkspace.shared.activateFileViewerSelecting([output])
    }

    func copyResult() {
        guard let output, let text = try? String(contentsOf: output, encoding: .utf8) else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
    }
}

/// JSON dictionaries are not Sendable; this box carries one across actors.
struct EventBox: @unchecked Sendable {
    let dict: [String: Any]
    init(_ d: [String: Any]) { dict = d }
}

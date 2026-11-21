import Foundation

/// Runs the Python conversion engine (`zip2md --json`) and streams its
/// JSON-lines progress events back to the caller.
final class EngineRunner: @unchecked Sendable {
    struct Settings: Sendable {
        var inputs: [URL]
        var output: URL
        var langs: String          // "id,en", "" = auto-detect
        var forceOCR: Bool
        var embeddedOCR: Bool
        var fast: Bool
    }

    private var process: Process?
    private var buffer = Data()
    private var stderrTail = Data()
    private let lock = NSLock()

    // MARK: Locating the engine

    /// Root of the source checkout (only used for development builds).
    private static let repoRoot: URL = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent()   // Zip2MarkdownApp
        .deletingLastPathComponent()   // Sources
        .deletingLastPathComponent()   // app
        .deletingLastPathComponent()   // repo

    static func engineCommand() -> (URL, [String])? {
        let fm = FileManager.default
        if let env = ProcessInfo.processInfo.environment["ZIP2MD_ENGINE"], fm.isExecutableFile(atPath: env) {
            return (URL(fileURLWithPath: env), [])
        }
        // 1) Bundled, frozen engine: Zip2Markdown.app/Contents/Resources/engine/zip2md
        if let res = Bundle.main.resourceURL {
            let bundled = res.appendingPathComponent("engine/zip2md")
            if fm.isExecutableFile(atPath: bundled.path) { return (bundled, []) }
        }
        // 2) Development: run straight from the checkout
        let dev = repoRoot.appendingPathComponent("scripts/zip2md")
        if fm.isExecutableFile(atPath: dev.path) { return (dev, []) }
        return nil
    }

    static func ocrHelper() -> URL? {
        let fm = FileManager.default
        let candidates = [
            Bundle.main.executableURL?.deletingLastPathComponent().appendingPathComponent("ocr-helper"),
            repoRoot.appendingPathComponent("app/.build/release/ocr-helper"),
            repoRoot.appendingPathComponent("app/.build/debug/ocr-helper"),
        ].compactMap { $0 }
        return candidates.first { fm.isExecutableFile(atPath: $0.path) }
    }

    private static func environment() -> [String: String] {
        var env = ProcessInfo.processInfo.environment
        // GUI apps get a minimal PATH; add Homebrew so LibreOffice/Tesseract are found.
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        if let helper = ocrHelper() { env["ZIP2MD_OCR_HELPER"] = helper.path }
        return env
    }

    /// Loads the engine and the OCR model once, in the background, right after launch.
    /// After an install/update macOS checks every bundled library on first use and
    /// Vision may compile its model; doing it here keeps the first conversion fast.
    static func warmUp(completion: @escaping @Sendable (Bool) -> Void) {
        guard let (exe, prefix) = engineCommand() else { completion(false); return }
        let p = Process()
        p.executableURL = exe
        p.arguments = prefix + ["--warmup"]
        p.environment = environment()
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        p.qualityOfService = .utility
        p.terminationHandler = { completion($0.terminationStatus == 0) }
        do { try p.run() } catch { completion(false) }
    }

    // MARK: Running

    func start(_ s: Settings,
               onEvent: @escaping @Sendable ([String: Any]) -> Void,
               onExit: @escaping @Sendable (Int32, String) -> Void) throws {
        guard let (exe, prefix) = Self.engineCommand() else {
            throw NSError(domain: "Zip2Markdown", code: 1, userInfo: [
                NSLocalizedDescriptionKey: "Mesin konversi tidak ditemukan. Jalankan scripts/build_app.sh terlebih dahulu."])
        }
        var args = prefix + s.inputs.map(\.path)
        args += ["-o", s.output.path, "--json", "--langs", s.langs]
        if s.forceOCR { args.append("--force-ocr") }
        if !s.embeddedOCR { args.append("--no-embedded-ocr") }
        if s.fast { args.append("--fast") }

        let p = Process()
        p.executableURL = exe
        p.arguments = args
        p.environment = Self.environment()

        let out = Pipe(), err = Pipe()
        p.standardOutput = out
        p.standardError = err
        buffer = Data()
        stderrTail = Data()

        out.fileHandleForReading.readabilityHandler = { [weak self] h in
            let chunk = h.availableData
            guard let self, !chunk.isEmpty else { return }
            for line in self.appendAndSplit(chunk) {
                if let obj = try? JSONSerialization.jsonObject(with: line) as? [String: Any] {
                    onEvent(obj)
                }
            }
        }
        err.fileHandleForReading.readabilityHandler = { [weak self] h in
            let chunk = h.availableData
            guard let self, !chunk.isEmpty else { return }
            self.lock.lock()
            self.stderrTail.append(chunk)
            if self.stderrTail.count > 8192 { self.stderrTail = self.stderrTail.suffix(8192) }
            self.lock.unlock()
        }
        p.terminationHandler = { [weak self] proc in
            out.fileHandleForReading.readabilityHandler = nil
            err.fileHandleForReading.readabilityHandler = nil
            // flush whatever is left in the pipe
            let rest = out.fileHandleForReading.readDataToEndOfFile()
            if let self, !rest.isEmpty {
                for line in self.appendAndSplit(rest + Data("\n".utf8)) {
                    if let obj = try? JSONSerialization.jsonObject(with: line) as? [String: Any] { onEvent(obj) }
                }
            }
            let tail = self.map { s in s.lock.withLock { String(decoding: s.stderrTail, as: UTF8.self) } } ?? ""
            onExit(proc.terminationStatus, tail)
        }
        try p.run()
        process = p
    }

    private func appendAndSplit(_ chunk: Data) -> [Data] {
        lock.lock(); defer { lock.unlock() }
        buffer.append(chunk)
        var lines: [Data] = []
        while let nl = buffer.firstIndex(of: 0x0A) {
            let line = buffer[buffer.startIndex..<nl]
            if !line.isEmpty { lines.append(Data(line)) }
            buffer.removeSubrange(buffer.startIndex...nl)
        }
        return lines
    }

    func cancel() {
        process?.terminate()   // SIGTERM -> engine cleans its temp folder and exits
    }
}

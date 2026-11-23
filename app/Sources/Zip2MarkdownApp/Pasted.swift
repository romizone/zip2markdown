import AppKit
import Foundation

/// Text pasted or typed in the app. Each one becomes a small file the engine converts
/// like any other input; the engine shows it under its title (`--label`), not the file name.
enum PastedStore {
    enum Kind { case plain, markdown, rich }

    /// Clipboard content, most useful form first.
    enum Clip {
        case files([URL])
        case rich(html: Data, plain: String)   // Word, Pages, web pages: headings, lists, tables kept
        case plain(String)
    }

    static let folder: URL = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0]
        .appendingPathComponent("Zip2Markdown/Tempelan", isDirectory: true)

    static func contains(_ url: URL) -> Bool {
        url.standardizedFileURL.path.hasPrefix(folder.standardizedFileURL.path + "/")
    }

    static func kind(of url: URL) -> Kind {
        switch url.pathExtension.lowercased() {
        case "html": .rich
        case "md": .markdown
        default: .plain
        }
    }

    /// Pasted items live only as long as the input list that shows them.
    static func clearAll() {
        try? FileManager.default.removeItem(at: folder)
    }

    static func delete(_ url: URL) {
        guard contains(url) else { return }
        try? FileManager.default.removeItem(at: url.deletingLastPathComponent())
    }

    static func save(text: String, markdown: Bool) throws -> URL {
        try save(Data(text.utf8), ext: markdown ? "md" : "txt")
    }

    static func save(html: Data) throws -> URL {
        // Pasteboard HTML often has no charset; without one a converter may guess wrong.
        let body = String(data: html, encoding: .utf8)
            ?? String(data: html, encoding: .windowsCP1252)
            ?? String(decoding: html, as: UTF8.self)
        return try save(Data(("<meta charset=\"utf-8\">\n" + body).utf8), ext: "html")
    }

    /// Replaces the text of an existing plain/Markdown item (the file name follows the format).
    static func update(_ url: URL, text: String, markdown: Bool) throws -> URL {
        let target = url.deletingLastPathComponent().appendingPathComponent(markdown ? "teks.md" : "teks.txt")
        try Data(text.utf8).write(to: target, options: .atomic)
        if target.path != url.path { try? FileManager.default.removeItem(at: url) }
        return target
    }

    static func text(of url: URL) -> String {
        (try? String(contentsOf: url, encoding: .utf8)) ?? ""
    }

    private static func save(_ data: Data, ext: String) throws -> URL {
        let dir = folder.appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let url = dir.appendingPathComponent("teks.\(ext)")
        try data.write(to: url, options: .atomic)
        return url
    }

    static func readClipboard() -> Clip? {
        let pb = NSPasteboard.general
        if let urls = pb.readObjects(forClasses: [NSURL.self],
                                     options: [.urlReadingFileURLsOnly: true]) as? [URL], !urls.isEmpty {
            return .files(urls)
        }
        let plain = pb.string(forType: .string) ?? ""
        if let html = pb.data(forType: .html) {
            return .rich(html: html, plain: plain)
        }
        // RTF only (TextEdit, some apps): turn it into HTML so formatting survives.
        let attributed: NSAttributedString? =
            pb.data(forType: .rtf).flatMap { NSAttributedString(rtf: $0, documentAttributes: nil) }
            ?? pb.data(forType: .rtfd).flatMap { NSAttributedString(rtfd: $0, documentAttributes: nil) }
        if let attributed, attributed.length > 0,
           let html = try? attributed.data(from: NSRange(location: 0, length: attributed.length),
                                           documentAttributes: [.documentType: NSAttributedString.DocumentType.html]) {
            return .rich(html: html, plain: attributed.string)
        }
        if !plain.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return .plain(plain)
        }
        return nil
    }

    /// First non-empty line, shortened: a title the user recognises in the list and the result.
    static func title(from text: String, fallback: String) -> String {
        let line = text.split(whereSeparator: \.isNewline)
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .first { !$0.isEmpty }?
            .trimmingCharacters(in: CharacterSet(charactersIn: "#*>-•· \t")) ?? ""
        guard !line.isEmpty else { return fallback }
        return line.count > 60 ? String(line.prefix(57)) + "…" : line
    }
}

/// What the text editor sheet edits: a new item (`url == nil`) or an existing one.
struct PasteDraft: Identifiable {
    let id = UUID()
    var url: URL?
    var title: String
    var text: String
    var markdown: Bool
    var rich: Bool          // formatted text: only the title can be changed here
}

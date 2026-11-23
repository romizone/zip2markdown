import SwiftUI

struct ContentView: View {
    @Bindable var model: ConverterModel
    @State private var dropTargeted = false
    @State private var showPreview = false
    @State private var draft: PasteDraft?

    var body: some View {
        VStack(spacing: 0) {
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    dropZone
                    if !model.inputs.isEmpty { inputList }
                    options
                    outputRow
                }
                .padding(20)
            }
            .frame(maxHeight: model.phase == .idle ? .infinity : 400)

            if model.phase != .idle {
                Divider()
                progressPanel
            }

            Divider()
            footer
        }
        .frame(minWidth: 620, minHeight: 640)
        // files can be dropped anywhere in the window, any number at once
        .dropDestination(for: URL.self) { urls, _ in
            model.add(urls)
            return !urls.isEmpty
        } isTargeted: { dropTargeted = $0 }
        .sheet(isPresented: $showPreview) { PreviewSheet(url: model.output) }
        .sheet(item: $draft) { d in
            TextDraftSheet(draft: d) { model.save($0) }
        }
    }

    // MARK: Drop zone

    private var dropZone: some View {
        VStack(spacing: 10) {
            Image(systemName: "doc.zipper")
                .font(.system(size: 40, weight: .light))
                .foregroundStyle(dropTargeted ? Color.accentColor : .secondary)
            Text(model.inputs.isEmpty
                 ? "Tarik & lepas file ZIP, folder, atau dokumen ke sini"
                 : "Tarik & lepas lagi untuk menambah file")
                .font(.headline)
            Text("Bisa banyak sekaligus: ZIP, folder, file lepas, dan teks tempelan (⌘V) — semuanya digabung jadi satu Markdown")
                .font(.caption)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
            Text("Word · PowerPoint · Excel · PDF (termasuk hasil scan) · gambar · teks · CSV · email")
                .font(.caption)
                .foregroundStyle(.tertiary)
            HStack {
                Button(model.inputs.isEmpty ? "Pilih File…" : "Tambah File…") { model.chooseInputs() }
                Button {
                    if !model.pasteFromClipboard() { NSSound.beep() }
                } label: {
                    Label("Tempel", systemImage: "doc.on.clipboard")
                }
                .help("Tambahkan isi clipboard (⌘V): teks biasa, teks berformat dari Word/Pages/web, atau file")
                Button {
                    draft = model.newDraft()
                } label: {
                    Label("Tulis Teks…", systemImage: "square.and.pencil")
                }
                .help("Ketik atau tempel teks, lalu beri judul")
            }
            .disabled(model.isRunning)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 26)
        .background(
            RoundedRectangle(cornerRadius: 14)
                .fill(dropTargeted ? Color.accentColor.opacity(0.08) : Color.secondary.opacity(0.05))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 14)
                .strokeBorder(style: StrokeStyle(lineWidth: 1.5, dash: [6, 4]))
                .foregroundStyle(dropTargeted ? Color.accentColor : Color.secondary.opacity(0.4))
        )
    }

    private var inputList: some View {
        GroupBox {
            ScrollView {
            VStack(alignment: .leading, spacing: 6) {
                ForEach(model.inputs, id: \.self) { url in
                    let pasted = model.isPasted(url)
                    HStack {
                        Image(systemName: icon(for: url)).foregroundStyle(.secondary).frame(width: 18)
                        Text(model.label(for: url)).lineLimit(1).truncationMode(.middle)
                        Spacer()
                        Text(pasted ? pastedNote(url) : url.deletingLastPathComponent().path(percentEncoded: false))
                            .font(.caption).foregroundStyle(.tertiary).lineLimit(1).truncationMode(.head)
                            .frame(maxWidth: 220, alignment: .trailing)
                        if pasted {
                            Button { draft = model.draft(for: url) } label: { Image(systemName: "pencil.circle") }
                                .buttonStyle(.plain).foregroundStyle(.secondary)
                                .help("Ubah judul atau isi")
                                .disabled(model.isRunning)
                        }
                        Button { model.remove(url) } label: { Image(systemName: "xmark.circle.fill") }
                            .buttonStyle(.plain).foregroundStyle(.secondary)
                            .disabled(model.isRunning)
                    }
                }
            }
            .padding(4)
            }
            .frame(maxHeight: 150)      // a long list scrolls instead of pushing the options away
        } label: {
            Text("Input (\(model.inputs.count)) — digabung sesuai urutan ini")
        }
    }

    private func pastedNote(_ url: URL) -> String {
        switch PastedStore.kind(of: url) {
        case .rich: "teks berformat (tempelan)"
        case .markdown: "Markdown (tempelan)"
        case .plain: "teks (tempelan)"
        }
    }

    private func icon(for url: URL) -> String {
        if model.isPasted(url) { return "doc.on.clipboard" }
        if url.hasDirectoryPath { return "folder" }
        switch url.pathExtension.lowercased() {
        case "zip": return "doc.zipper"
        case "pdf": return "doc.richtext"
        case "png", "jpg", "jpeg", "heic", "tif", "tiff": return "photo"
        default: return "doc"
        }
    }

    // MARK: Options

    private var options: some View {
        GroupBox("Opsi OCR") {
            VStack(alignment: .leading, spacing: 10) {
                Picker("Bahasa teks", selection: $model.language) {
                    ForEach(OCRLanguage.allCases) { Text($0.label).tag($0) }
                }
                .frame(maxWidth: 320)
                Toggle("OCR gambar di dalam Word, PowerPoint & PDF", isOn: $model.embeddedOCR)
                Toggle("Paksa OCR semua halaman PDF (abaikan lapisan teks yang ada)", isOn: $model.forceOCR)
                Toggle("Mode cepat (akurasi lebih rendah, tanpa deteksi tabel)", isOn: $model.fastOCR)
                Text("OCR berjalan sepenuhnya di Mac ini dengan Apple Vision — tidak ada file yang dikirim ke internet.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            .padding(6)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .disabled(model.isRunning)
    }

    private var outputRow: some View {
        GroupBox("Hasil") {
            HStack {
                Image(systemName: "doc.text").foregroundStyle(.secondary)
                Text(model.output?.path(percentEncoded: false) ?? "—")
                    .lineLimit(1).truncationMode(.middle)
                    .foregroundStyle(model.output == nil ? .secondary : .primary)
                Spacer()
                Button("Ubah…") { model.chooseOutput() }
                    .disabled(model.inputs.isEmpty || model.isRunning)
            }
            .padding(6)
        }
    }

    // MARK: Progress

    private var progressPanel: some View {
        VStack(alignment: .leading, spacing: 10) {
            switch model.phase {
            case .running:
                HStack {
                    Text(model.stage).font(.headline)
                    Spacer()
                    if model.total > 0 { Text("\(model.done) / \(model.total)").monospacedDigit().foregroundStyle(.secondary) }
                }
                if model.total > 0 {
                    ProgressView(value: model.progress)
                } else {
                    ProgressView().progressViewStyle(.linear)
                }
            case .finished:
                resultBanner
            case .failed(let msg):
                Label {
                    Text(msg).textSelection(.enabled)
                } icon: {
                    Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.red)
                }
            case .idle:
                EmptyView()
            }

            if !model.log.isEmpty {
                ScrollViewReader { proxy in
                    List(model.log) { entry in
                        LogRow(entry: entry).id(entry.id)
                    }
                    .listStyle(.plain)
                    .frame(minHeight: 120)
                    .onChange(of: model.log.count) {
                        if let last = model.log.last { proxy.scrollTo(last.id, anchor: .bottom) }
                    }
                }
            }
        }
        .padding(16)
    }

    private var resultBanner: some View {
        VStack(alignment: .leading, spacing: 10) {
            Label("Selesai — 1 file Markdown dibuat", systemImage: "checkmark.circle.fill")
                .font(.headline).foregroundStyle(.green)
            HStack(spacing: 16) {
                stat("\(model.summary.converted)", "dikonversi")
                stat("\(model.summary.ocr)", "dengan OCR")
                if model.summary.failed > 0 { stat("\(model.summary.failed)", "gagal", .red) }
                if model.summary.skipped > 0 { stat("\(model.summary.skipped)", "dilewati", .orange) }
                stat(ByteCountFormatter.string(fromByteCount: Int64(model.summary.bytes), countStyle: .file), "ukuran")
                stat(String(format: "%.1f dtk", model.summary.seconds), "waktu")
            }
            HStack {
                Button("Buka File") { model.openResult() }.keyboardShortcut("o")
                Button("Tampilkan di Finder") { model.revealResult() }
                Button("Pratinjau") { showPreview = true }
                Button("Salin Isi") { model.copyResult() }
            }
        }
    }

    private func stat(_ value: String, _ label: String, _ color: Color = .primary) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            Text(value).font(.title3.weight(.semibold)).foregroundStyle(color).monospacedDigit()
            Text(label).font(.caption).foregroundStyle(.secondary)
        }
    }

    // MARK: Footer

    private var footer: some View {
        HStack {
            if !model.inputs.isEmpty && !model.isRunning {
                Button("Kosongkan") { model.clear() }
            }
            switch model.engine {
            case .warming:
                ProgressView().controlSize(.small)
                Text("Menyiapkan mesin OCR…").font(.caption).foregroundStyle(.secondary)
            case .ready:
                Label("Mesin siap", systemImage: "bolt.fill").font(.caption).foregroundStyle(.secondary)
            case .unavailable:
                Label("Mesin konversi tidak ditemukan", systemImage: "exclamationmark.triangle")
                    .font(.caption).foregroundStyle(.orange)
            }
            Spacer()
            if model.isRunning {
                Button("Batal", role: .cancel) { model.cancel() }
                    .keyboardShortcut(.cancelAction)
            } else {
                Button {
                    model.start()
                } label: {
                    Label("Konversi ke Markdown", systemImage: "arrow.right.doc.on.clipboard")
                        .padding(.horizontal, 6)
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.large)
                .keyboardShortcut(.defaultAction)
                .disabled(model.inputs.isEmpty || model.output == nil)
            }
        }
        .padding(.horizontal, 20)
        .padding(.vertical, 14)
    }
}

/// Type or paste text, give it a title; it is merged like any other input.
struct TextDraftSheet: View {
    @State var draft: PasteDraft
    let onSave: (PasteDraft) -> Void
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(draft.url == nil ? "Tambah teks" : "Ubah teks").font(.headline)
            TextField("Judul", text: $draft.title, prompt: Text("Judul bagian di hasil Markdown"))
            if draft.rich {
                Label("Teks berformat dari Word/Pages/web: judul, daftar, dan tabelnya dipertahankan. "
                      + "Isinya tidak bisa diubah di sini — hapus lalu tempel ulang untuk menggantinya.",
                      systemImage: "info.circle")
                    .font(.callout).foregroundStyle(.secondary)
            } else {
                TextEditor(text: $draft.text)
                    .font(.system(.body, design: draft.markdown ? .monospaced : .default))
                    .frame(minHeight: 260)
                    .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(Color.secondary.opacity(0.3)))
                Toggle("Teks ini sudah berformat Markdown (judul #, daftar, tabel dipakai apa adanya)",
                       isOn: $draft.markdown)
                    .font(.callout)
            }
            HStack {
                Spacer()
                Button("Batal", role: .cancel) { dismiss() }
                    .keyboardShortcut(.cancelAction)
                Button(draft.url == nil ? "Tambahkan" : "Simpan") {
                    onSave(draft)
                    dismiss()
                }
                .keyboardShortcut(.defaultAction)
                .disabled(!draft.rich && draft.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
        }
        .padding(20)
        .frame(minWidth: 560, minHeight: draft.rich ? 0 : 420)
    }
}

struct LogRow: View {
    let entry: LogEntry

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Image(systemName: symbol).foregroundStyle(color).frame(width: 16)
            Text(entry.path).lineLimit(1).truncationMode(.middle)
            Spacer(minLength: 8)
            if let d = entry.detail {
                Text(d).font(.caption).foregroundStyle(entry.status == .error ? .red : .secondary)
                    .lineLimit(1).truncationMode(.tail)
            }
        }
        .help(entry.detail ?? entry.path)
    }

    private var symbol: String {
        switch entry.status {
        case .ok: "checkmark.circle"
        case .ocr: "text.viewfinder"
        case .error: "xmark.octagon"
        case .skipped: "minus.circle"
        case .warning: "exclamationmark.triangle"
        case .info: "info.circle"
        }
    }

    private var color: Color {
        switch entry.status {
        case .ok: .green
        case .ocr: .blue
        case .error: .red
        case .skipped: .secondary
        case .warning: .orange
        case .info: .secondary
        }
    }
}

struct PreviewSheet: View {
    let url: URL?
    @Environment(\.dismiss) private var dismiss
    @State private var text = ""
    @State private var styled: NSAttributedString?
    @State private var rendered = true

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text(url?.lastPathComponent ?? "").font(.headline)
                Spacer()
                Picker("", selection: $rendered) {
                    Text("Tampilan").tag(true)
                    Text("Sumber").tag(false)
                }
                .pickerStyle(.segmented).frame(width: 180)
                Button("Tutup") { dismiss() }.keyboardShortcut(.cancelAction)
            }
            .padding(12)
            Divider()
            // NSTextView lays out lazily; a SwiftUI Text of a few hundred KB lays out
            // everything at once on the main thread and beachballs the app.
            LargeTextView(text: rendered ? (styled ?? NSAttributedString(string: text, attributes: Self.plain))
                                         : NSAttributedString(string: text, attributes: Self.mono))
        }
        .frame(minWidth: 720, minHeight: 560)
        .task {
            guard let url else { return }
            // Reading and Markdown parsing happen off the main thread.
            let (s, parsed) = await Task.detached(priority: .userInitiated) { () -> (String, AttributedString?) in
                guard var s = try? String(contentsOf: url, encoding: .utf8) else { return ("", nil) }
                if s.utf8.count > 2_000_000 {
                    s = String(s.prefix(1_000_000)) + "\n\n… (dipotong untuk pratinjau)"
                }
                return (s, try? AttributedString(markdown: s, options: .init(
                    interpretedSyntax: .inlineOnlyPreservingWhitespace)))
            }.value
            text = s
            styled = parsed.map(Self.styled)
        }
    }

    /// Bold / italic / code from the Markdown, as fonts NSTextView can draw.
    private static func styled(_ a: AttributedString) -> NSAttributedString {
        let out = NSMutableAttributedString(string: String(a.characters), attributes: plain)
        let base = NSFont.systemFont(ofSize: 13)
        var location = 0
        for run in a.runs {
            let length = String(a[run.range].characters).utf16.count
            defer { location += length }
            let range = NSRange(location: location, length: length)
            if let link = run.link { out.addAttribute(.link, value: link, range: range) }
            guard let intent = run.inlinePresentationIntent else { continue }
            var font = base
            if intent.contains(.code) {
                font = NSFont.monospacedSystemFont(ofSize: 12, weight: .regular)
            } else {
                var traits: NSFontDescriptor.SymbolicTraits = []
                if intent.contains(.stronglyEmphasized) { traits.insert(.bold) }
                if intent.contains(.emphasized) { traits.insert(.italic) }
                if !traits.isEmpty {
                    font = NSFont(descriptor: base.fontDescriptor.withSymbolicTraits(traits), size: 13) ?? base
                }
            }
            out.addAttribute(.font, value: font, range: range)
        }
        return out
    }

    private static var plain: [NSAttributedString.Key: Any] {
        [.font: NSFont.systemFont(ofSize: 13), .foregroundColor: NSColor.textColor]
    }
    private static var mono: [NSAttributedString.Key: Any] {
        [.font: NSFont.monospacedSystemFont(ofSize: 12, weight: .regular), .foregroundColor: NSColor.textColor]
    }
}

/// Read-only, selectable text view that stays fast for multi-megabyte documents.
struct LargeTextView: NSViewRepresentable {
    let text: NSAttributedString

    func makeNSView(context: Context) -> NSScrollView {
        let scroll = NSTextView.scrollableTextView()
        if let tv = scroll.documentView as? NSTextView {
            tv.isEditable = false
            tv.isSelectable = true
            tv.isRichText = true
            tv.drawsBackground = false
            tv.textContainerInset = NSSize(width: 16, height: 16)
            tv.layoutManager?.allowsNonContiguousLayout = true
        }
        return scroll
    }

    func updateNSView(_ scroll: NSScrollView, context: Context) {
        guard let tv = scroll.documentView as? NSTextView, let storage = tv.textStorage,
              !storage.isEqual(to: text) else { return }
        storage.setAttributedString(text)
        tv.scroll(.zero)
    }
}

// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "Zip2Markdown",
    platforms: [.macOS("26.0")],
    targets: [
        // Native SwiftUI desktop app (drag & drop ZIP -> one Markdown file).
        .executableTarget(
            name: "Zip2MarkdownApp",
            path: "Sources/Zip2MarkdownApp"
        ),
        // Small CLI around Apple Vision OCR, used by the Python engine.
        .executableTarget(
            name: "ocr-helper",
            path: "Sources/OCRHelper"
        ),
    ]
)

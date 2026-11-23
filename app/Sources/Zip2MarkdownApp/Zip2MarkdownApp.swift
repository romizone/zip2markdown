import AppKit
import SwiftUI

@main
struct Zip2MarkdownApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate

    var body: some Scene {
        Window("Zip2Markdown", id: "main") {
            ContentView(model: ConverterModel.shared)
        }
        .windowResizability(.contentMinSize)
        .commands {
            CommandGroup(replacing: .newItem) {
                Button("Buka…") { ConverterModel.shared.chooseInputs() }
                    .keyboardShortcut("o", modifiers: [.command, .shift])
            }
        }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        // Needed when launched as a bare executable (swift run) instead of a .app bundle.
        NSApp.setActivationPolicy(.regular)
        NSApp.activate()
        DockPin.pinOnce()       // first launch from /Applications: keep an icon in the Dock
        installPasteShortcut()
    }

    /// ⌘V in the main window adds the clipboard as an input. While a text field or the
    /// text editor sheet has focus, ⌘V keeps its normal meaning.
    private func installPasteShortcut() {
        NSEvent.addLocalMonitorForEvents(matching: .keyDown) { event in
            guard event.modifierFlags.intersection(.deviceIndependentFlagsMask) == .command,
                  event.charactersIgnoringModifiers?.lowercased() == "v" else { return event }
            // local monitors run on the main thread
            let handled: Bool = MainActor.assumeIsolated {
                guard let window = NSApp.keyWindow, window.attachedSheet == nil, window.sheetParent == nil,
                      !(window.firstResponder is NSText) else { return false }
                if !ConverterModel.shared.pasteFromClipboard() { NSSound.beep() }
                return true
            }
            return handled ? nil : event
        }
    }

    /// Files dropped on the Dock icon or opened with "Open With → Zip2Markdown".
    func application(_ application: NSApplication, open urls: [URL]) {
        MainActor.assumeIsolated { ConverterModel.shared.add(urls) }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    /// Quitting mid-run must not leave the engine, its OCR workers and LibreOffice
    /// running in the background.
    func applicationWillTerminate(_ notification: Notification) {
        MainActor.assumeIsolated {
            if ConverterModel.shared.isRunning { ConverterModel.shared.cancel() }
        }
    }
}

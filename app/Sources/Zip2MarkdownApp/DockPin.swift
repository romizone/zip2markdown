import AppKit

/// Adds the app to the Dock the first time it is opened from an Applications folder,
/// so that "drag to Applications, open once" leaves a Dock icon behind.
///
/// It happens once only: if the user later drags the icon out of the Dock, it stays out.
enum DockPin {
    private static let doneKey = "dockPinned"

    static func pinOnce() {
        let dock = "com.apple.dock" as CFString
        let listKey = "persistent-apps" as CFString
        let defaults = UserDefaults.standard
        guard !defaults.bool(forKey: doneKey) else { return }

        // Only an installed copy: not one run from the disk image or a build folder.
        let path = Bundle.main.bundlePath
        let folders = ["/Applications/", NSHomeDirectory() + "/Applications/"]
        guard path.hasSuffix(".app"), folders.contains(where: path.hasPrefix) else { return }
        defaults.set(true, forKey: doneKey)

        CFPreferencesAppSynchronize(dock)
        // No readable list means "don't touch": writing a fresh one would wipe the user's Dock.
        guard var apps = CFPreferencesCopyAppValue(listKey, dock) as? [[String: Any]] else { return }

        let url = URL(fileURLWithPath: path, isDirectory: true).absoluteString
        let bundleID = Bundle.main.bundleIdentifier ?? ""
        let present = apps.contains { tile in
            let data = tile["tile-data"] as? [String: Any]
            if !bundleID.isEmpty, data?["bundle-identifier"] as? String == bundleID { return true }
            return (data?["file-data"] as? [String: Any])?["_CFURLString"] as? String == url
        }
        guard !present else { return }

        apps.append([
            "tile-type": "file-tile",
            "tile-data": [
                "file-label": "Zip2Markdown",
                "bundle-identifier": bundleID,
                "file-data": ["_CFURLString": url, "_CFURLStringType": 15],
            ] as [String: Any],
        ])
        CFPreferencesSetAppValue(listKey, apps as CFArray, dock)
        guard CFPreferencesAppSynchronize(dock) else { return }

        // The Dock only reads its list when it starts, and a Dock that is asked to quit
        // (or that updates itself for the app that has just launched) saves its own
        // list over ours. So it gets no chance to: stop it outright, right away —
        // launchd starts a new one at once, which then reads the new list.
        let restart = Process()
        restart.executableURL = URL(fileURLWithPath: "/usr/bin/killall")
        restart.arguments = ["-KILL", "Dock"]
        try? restart.run()
    }
}

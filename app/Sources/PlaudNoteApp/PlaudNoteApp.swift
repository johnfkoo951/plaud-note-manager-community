import AppKit
import SwiftUI

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationShouldSaveApplicationState(_ app: NSApplication) -> Bool {
        false
    }

    func applicationShouldRestoreApplicationState(_ app: NSApplication) -> Bool {
        false
    }
}

@main
struct PlaudNoteApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate

    init() {
        UserDefaults.standard.set(false, forKey: "NSQuitAlwaysKeepsWindows")
        NSApplication.shared.setActivationPolicy(.regular)
        DispatchQueue.main.async {
            NSApplication.shared.activate(ignoringOtherApps: true)
        }
    }

    var body: some Scene {
        WindowGroup(DistributionProfile.appName) {
            ContentView()
                .frame(minWidth: 1280, minHeight: 800)
        }
        .defaultSize(width: 1360, height: 840)
        .windowStyle(.hiddenTitleBar)
        .commands {
            CommandGroup(replacing: .appSettings) {
                // Standard macOS Settings shortcut. ⌘. also still works via
                // the toolbar gear button in ContentView.
                Button("Settings…") {
                    NotificationCenter.default.post(name: .openPlaudSettings,
                                                    object: nil)
                }
                .keyboardShortcut(",", modifiers: .command)
            }
            if !DistributionProfile.isCommunity {
                CommandGroup(after: .textEditing) {
                    Button("Command Palette…") {
                        NotificationCenter.default.post(
                            name: .togglePlaudCommandPalette, object: nil
                        )
                    }
                    .keyboardShortcut("k", modifiers: .command)
                }
            }
        }
    }
}

import AppKit
import SwiftUI

struct CommunitySettingsSheet: View {
    let dismiss: () -> Void

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                VStack(alignment: .leading, spacing: 3) {
                    Text("Community Edition")
                        .font(.title2.bold())
                    Text("A privacy-limited build for the CMDS collection workshop")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Spacer()
            }
            .padding(20)

            Divider()

            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    settingsCard("Included") {
                        Label("Plaud Web Login with automatic token renewal", systemImage: "key.fill")
                        Label("Recording sync, local search, playback, folders, and export",
                              systemImage: "waveform")
                    }

                    settingsCard("Privacy boundary") {
                        Label("Credentials stay in this edition's macOS Keychain item",
                              systemImage: "lock.shield")
                        Label("Recordings and cache stay in this edition's Application Support folder",
                              systemImage: "externaldrive")
                        Label("External AI, ElevenLabs, Obsidian, Claude, and browser-profile scanning are disabled",
                              systemImage: "hand.raised.fill")
                    }

                    settingsCard("Local data") {
                        Text(RuntimePaths.appSupportRoot.path)
                            .font(.system(.caption, design: .monospaced))
                            .foregroundStyle(.secondary)
                            .textSelection(.enabled)
                        Button {
                            try? FileManager.default.createDirectory(
                                at: RuntimePaths.appSupportRoot,
                                withIntermediateDirectories: true
                            )
                            NSWorkspace.shared.activateFileViewerSelecting(
                                [RuntimePaths.appSupportRoot]
                            )
                        } label: {
                            Label("Show in Finder", systemImage: "folder")
                        }
                    }

                    Text("Plaud Note Manager Community uses Plaud's web API and is not an official Plaud application. Only use it with your own account and recordings.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .padding(20)
            }

            Divider()

            HStack {
                Text(AppVersion.longLine)
                    .font(.caption)
                    .foregroundStyle(.tertiary)
                Spacer()
                Button("Done") { dismiss() }
                    .keyboardShortcut(.defaultAction)
            }
            .padding(.horizontal, 20)
            .padding(.vertical, 14)
        }
        .frame(width: 650, height: 560)
    }

    private func settingsCard<Content: View>(
        _ title: String, @ViewBuilder content: () -> Content
    ) -> some View {
        VStack(alignment: .leading, spacing: 9) {
            Text(title).font(.headline)
            content()
                .font(.callout)
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(AppUI.subtleFill, in: RoundedRectangle(cornerRadius: AppUI.radius))
    }
}

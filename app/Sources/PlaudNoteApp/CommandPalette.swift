import AppKit
import SwiftUI

extension Notification.Name {
    /// Posted by the ⌘K menu command; ContentView toggles the palette overlay.
    static let togglePlaudCommandPalette = Notification.Name("plaud.toggleCommandPalette")
}

/// One executable entry in the ⌘K palette.
private struct PaletteAction: Identifiable {
    let id: String
    let title: String
    let systemImage: String
    /// Single-key trigger shown on the right (Superhuman style). Active while
    /// the palette is open AND the search field is empty — not global.
    var keyHint: String? = nil
    var isEnabled: Bool = true
    /// Performs the action. Returns true when the palette should close.
    let run: @MainActor () -> Bool
}

/// Superhuman Command-style ⌘K palette: floating dark translucent panel with
/// an autofocused search field, ↑↓ navigation, Enter to run, Esc to close.
/// "Move to Folder…" switches the same panel into a folder-pick mode.
struct CommandPaletteOverlay: View {
    @ObservedObject var store: FileStore
    @Binding var isPresented: Bool

    private enum Mode {
        case actions
        case folderPick
    }

    @State private var mode: Mode = .actions
    @State private var query: String = ""
    @State private var highlighted: Int = 0
    @FocusState private var searchFocused: Bool

    var body: some View {
        ZStack(alignment: .top) {
            // Dim + click-away layer.
            Color.black.opacity(0.18)
                .ignoresSafeArea()
                .contentShape(Rectangle())
                .onTapGesture { isPresented = false }

            panel
                .padding(.top, 96)
        }
        .onExitCommand { handleEscape() }
    }

    // MARK: Panel

    private var panel: some View {
        VStack(spacing: 0) {
            HStack(spacing: 10) {
                Image(systemName: mode == .actions ? "command" : "folder")
                    .font(.system(size: 14, weight: .semibold))
                    .foregroundStyle(.secondary)
                TextField(
                    mode == .actions ? "Type a command…" : "Move to folder…",
                    text: $query
                )
                .textFieldStyle(.plain)
                .font(.system(size: 16))
                .focused($searchFocused)
                .onSubmit { runHighlighted() }
                .onKeyPress(phases: .down) { press in
                    handleKey(press)
                }
                Text(mode == .actions ? "esc" : "esc back")
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundStyle(.tertiary)
                    .padding(.horizontal, 5)
                    .padding(.vertical, 2)
                    .background(Color.white.opacity(0.07), in: RoundedRectangle(cornerRadius: 4))
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 12)

            if let context = contextLine {
                HStack(spacing: 5) {
                    Image(systemName: "waveform")
                        .font(.system(size: 9, weight: .semibold))
                    Text(context)
                        .lineLimit(1)
                        .truncationMode(.middle)
                    Spacer(minLength: 0)
                }
                .font(.system(size: 10.5))
                .foregroundStyle(.tertiary)
                .padding(.horizontal, 14)
                .padding(.bottom, 8)
            }

            Divider()

            actionList
        }
        .frame(width: 560)
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 12))
        .overlay(
            RoundedRectangle(cornerRadius: 12)
                .stroke(Color.white.opacity(0.12), lineWidth: 1)
        )
        .shadow(color: .black.opacity(0.38), radius: 30, y: 14)
        // Superhuman Command look: the palette is always dark, regardless of
        // the app appearance.
        .environment(\.colorScheme, .dark)
        .onAppear {
            highlighted = firstEnabledIndex(in: visibleItems)
            DispatchQueue.main.async { searchFocused = true }
        }
        .onChange(of: query) { _, _ in
            highlighted = firstEnabledIndex(in: visibleItems)
        }
    }

    /// Which recording the file actions will hit, shown under the search box.
    private var contextLine: String? {
        guard mode == .actions else { return nil }
        guard store.selectedID != nil else { return "No recording selected" }
        let name = store.selectedFile?.filename ?? store.selectedID ?? ""
        return name.isEmpty ? nil : name
    }

    private var actionList: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(spacing: 1) {
                    let items = visibleItems
                    if items.isEmpty {
                        Text("No matching commands")
                            .font(.system(size: 12))
                            .foregroundStyle(.tertiary)
                            .frame(maxWidth: .infinity)
                            .padding(.vertical, 18)
                    }
                    ForEach(Array(items.enumerated()), id: \.element.id) { idx, action in
                        actionRow(action, isHighlighted: idx == highlighted)
                            .id(action.id)
                            .onTapGesture { execute(action) }
                            .onHover { inside in
                                if inside && action.isEnabled { highlighted = idx }
                            }
                    }
                }
                .padding(6)
            }
            .frame(maxHeight: 340)
            .onChange(of: highlighted) { _, newValue in
                let items = visibleItems
                guard items.indices.contains(newValue) else { return }
                proxy.scrollTo(items[newValue].id)
            }
        }
    }

    private func actionRow(_ action: PaletteAction, isHighlighted: Bool) -> some View {
        HStack(spacing: 10) {
            Image(systemName: action.systemImage)
                .font(.system(size: 12.5, weight: .medium))
                .symbolRenderingMode(.hierarchical)
                .foregroundStyle(.secondary)
                .frame(width: 20)
            Text(action.title)
                .font(.system(size: 13))
                .lineLimit(1)
            Spacer(minLength: 8)
            if let hint = action.keyHint {
                Text(hint)
                    .font(.system(size: 10.5, weight: .semibold, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .frame(minWidth: 14)
                    .padding(.horizontal, 4)
                    .padding(.vertical, 2)
                    .background(
                        Color.white.opacity(0.08),
                        in: RoundedRectangle(cornerRadius: 4)
                    )
            }
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 7)
        .contentShape(RoundedRectangle(cornerRadius: 6))
        .background(
            isHighlighted ? Color.white.opacity(0.10) : Color.clear,
            in: RoundedRectangle(cornerRadius: 6)
        )
        .opacity(action.isEnabled ? 1 : 0.35)
    }

    // MARK: Keyboard

    private func handleKey(_ press: KeyPress) -> KeyPress.Result {
        switch press.key {
        case .upArrow:
            moveHighlight(-1)
            return .handled
        case .downArrow:
            moveHighlight(1)
            return .handled
        case .escape:
            handleEscape()
            return .handled
        default:
            // Single-key triggers (E, S, V, U, M, T, O, W, R) fire only while
            // the search field is empty; otherwise keystrokes just filter.
            guard mode == .actions, query.isEmpty,
                  press.modifiers.isEmpty || press.modifiers == .capsLock
            else { return .ignored }
            let ch = press.characters.lowercased()
            guard !ch.isEmpty,
                  let action = visibleItems.first(where: {
                      $0.isEnabled && $0.keyHint?.lowercased() == ch
                  })
            else { return .ignored }
            execute(action)
            return .handled
        }
    }

    private func handleEscape() {
        if mode == .folderPick {
            switchMode(.actions)
        } else {
            isPresented = false
        }
    }

    private func moveHighlight(_ delta: Int) {
        let items = visibleItems
        guard !items.isEmpty else { return }
        var idx = highlighted
        // Skip disabled entries; the `idx != highlighted` guard terminates
        // the loop when every entry is disabled.
        repeat {
            idx = (idx + delta + items.count) % items.count
        } while !items[idx].isEnabled && idx != highlighted
        highlighted = idx
    }

    private func runHighlighted() {
        let items = visibleItems
        guard items.indices.contains(highlighted), items[highlighted].isEnabled
        else { return }
        execute(items[highlighted])
    }

    private func execute(_ action: PaletteAction) {
        guard action.isEnabled else { return }
        if action.run() {
            isPresented = false
        }
    }

    private func switchMode(_ newMode: Mode) {
        mode = newMode
        query = ""
        highlighted = 0
    }

    private func firstEnabledIndex(in items: [PaletteAction]) -> Int {
        items.firstIndex { $0.isEnabled } ?? 0
    }

    // MARK: Items

    private var visibleItems: [PaletteAction] {
        let source = mode == .folderPick ? folderItems : actionItems
        let needle = query.trimmingCharacters(in: .whitespaces).lowercased()
        guard !needle.isEmpty else { return source }
        return source.filter { $0.title.lowercased().contains(needle) }
    }

    /// File operations + navigation entries. Recomputed on every store
    /// publish, so enabled/disabled state tracks selection and running jobs.
    private var actionItems: [PaletteAction] {
        let fileID = store.selectedID
        let file = store.selectedFile
        let hasSelection = fileID != nil
        let isArchived = file?.usageStatus == "archived"
        let hasFolder = fileID.map { !store.folderIDs(for: $0).isEmpty } ?? false
        let metadataRunning = fileID.map {
            store.metadataGeneratingIDs.contains($0)
        } ?? false
        let transcribeRunning = fileID.map {
            store.transcribingIDs.contains($0)
        } ?? false

        var items: [PaletteAction] = []

        items.append(PaletteAction(
            id: "archive",
            title: isArchived ? "Unarchive" : "Archive",
            systemImage: isArchived ? "tray.and.arrow.up" : "archivebox",
            keyHint: "E",
            isEnabled: hasSelection
        ) {
            guard let fileID else { return true }
            Task {
                await store.setUsageStatus(
                    fileID, status: isArchived ? "unused" : "archived"
                )
            }
            return true
        })

        let isStarred = file?.starred ?? false
        items.append(PaletteAction(
            id: "star",
            title: isStarred ? "Unstar" : "Star",
            systemImage: isStarred ? "star.slash" : "star",
            keyHint: "S",
            isEnabled: hasSelection
        ) {
            guard let fileID else { return true }
            store.toggleStar(fileID)
            return true
        })

        items.append(PaletteAction(
            id: "move",
            title: "Move to Folder…",
            systemImage: "folder",
            keyHint: "V",
            isEnabled: hasSelection
        ) {
            switchMode(.folderPick)
            return false
        })

        items.append(PaletteAction(
            id: "unfile",
            title: "Unfile",
            systemImage: "folder.badge.minus",
            keyHint: "U",
            isEnabled: hasSelection && hasFolder
        ) {
            guard let fileID else { return true }
            store.assignFolder(fileID, folderID: nil)
            return true
        })

        items.append(PaletteAction(
            id: "metadata",
            title: "Generate Metadata",
            systemImage: "sparkles",
            keyHint: "M",
            isEnabled: hasSelection && !metadataRunning
        ) {
            guard let fileID else { return true }
            // No explicit model: the CLI resolves the configured classify
            // model (Settings > Auto-classify) — single source of truth.
            Task { await store.generateMetadata(fileID) }
            return true
        })

        items.append(PaletteAction(
            id: "transcribe",
            title: "Transcribe with CMDS",
            systemImage: "waveform",
            keyHint: "T",
            isEnabled: hasSelection && !transcribeRunning
        ) {
            guard let fileID else { return true }
            // Default options — speaker count Auto (0), same as the CMDS
            // tab's Transcribe button default.
            Task { await store.transcribeWithElevenLabs(fileID) }
            return true
        })

        items.append(PaletteAction(
            id: "obsidian",
            title: "Send to Vault (Integrated · 즉시)",
            systemImage: "paperplane",
            keyHint: "O",
            isEnabled: hasSelection
        ) {
            guard let fileID else { return true }
            Task { await store.vaultSend(fileID) }
            return true
        })

        items.append(PaletteAction(
            id: "obsidian-wiki",
            title: "Send to Wiki Vault",
            systemImage: "books.vertical",
            isEnabled: hasSelection
        ) {
            guard let fileID else { return true }
            Task { await store.vaultSend(fileID, to: "wiki") }
            return true
        })

        items.append(PaletteAction(
            id: "obsidian-claude",
            title: "Send to Vault via Claude (headless)",
            systemImage: "wand.and.stars",
            isEnabled: hasSelection
        ) {
            guard let fileID else { return true }
            Task { await store.vaultSend(fileID, via: "claude") }
            return true
        })

        items.append(PaletteAction(
            id: "plaud-web",
            title: "Open in Plaud Web",
            systemImage: "safari",
            keyHint: "W",
            isEnabled: hasSelection
        ) {
            guard let fileID else { return true }
            store.openInPlaudWeb(fileID)
            return true
        })

        items.append(PaletteAction(
            id: "copy-url",
            title: "Copy Plaud URL",
            systemImage: "link",
            isEnabled: hasSelection
        ) {
            guard let fileID else { return true }
            store.copyPlaudWebURL(fileID)
            return true
        })

        items.append(PaletteAction(
            id: "rename",
            title: "Rename…",
            systemImage: "pencil",
            keyHint: "R",
            isEnabled: hasSelection
        ) {
            guard let fileID else { return true }
            let current = file?.filename ?? ""
            if let newName = promptForFilename(current: current) {
                store.renameFile(fileID, to: newName)
            }
            return true
        })

        items.append(PaletteAction(
            id: "refetch",
            title: "Re-fetch Detail",
            systemImage: "arrow.clockwise",
            isEnabled: hasSelection
        ) {
            guard let fileID else { return true }
            Task { await store.refetchDetail(fileID) }
            return true
        })

        items.append(PaletteAction(
            id: "classify-undo",
            title: "Undo Auto-Classify",
            systemImage: "arrow.uturn.backward",
            isEnabled: store.lastClassifyApply != nil && !store.classifyRunning
        ) {
            Task { await store.classifyUndo() }
            return true
        })

        // Navigation: jump the sidebar selection.
        items.append(navItem(id: "nav-all", title: "Go to: All Files",
                             systemImage: "tray.full", target: .allFiles))
        items.append(navItem(id: "nav-unfiled", title: "Go to: Unfiled",
                             systemImage: "tray", target: .unfiled))
        items.append(navItem(id: "nav-starred", title: "Go to: Starred",
                             systemImage: "star", target: .starred))
        items.append(navItem(id: "nav-trash", title: "Go to: Trash",
                             systemImage: "trash", target: .trash))
        for folder in store.folders {
            items.append(navItem(
                id: "nav-folder-\(folder.id)",
                title: "Go to: \(folder.name)",
                systemImage: folder.sfSymbol,
                target: .folder(folder.id)
            ))
        }

        return items
    }

    private func navItem(id: String, title: String, systemImage: String,
                         target: SidebarItem) -> PaletteAction {
        PaletteAction(id: id, title: title, systemImage: systemImage) {
            store.sidebar = target
            return true
        }
    }

    /// Folder-pick mode: Unfiled + every folder, filtered by the same search
    /// field. Enter assigns via the single-folder radio write path.
    private var folderItems: [PaletteAction] {
        guard let fileID = store.selectedID else { return [] }
        let assigned = store.folderIDs(for: fileID)

        var items: [PaletteAction] = [
            PaletteAction(
                id: "pick-unfiled",
                title: assigned.isEmpty ? "Unfiled ✓" : "Unfiled",
                systemImage: "tray"
            ) {
                store.assignFolder(fileID, folderID: nil)
                return true
            }
        ]
        for folder in store.folders {
            let isCurrent = assigned.contains(folder.id)
            items.append(PaletteAction(
                id: "pick-\(folder.id)",
                title: isCurrent ? "\(folder.name) ✓" : folder.name,
                systemImage: folder.sfSymbol
            ) {
                store.assignFolder(fileID, folderID: folder.id)
                return true
            })
        }
        return items
    }
}

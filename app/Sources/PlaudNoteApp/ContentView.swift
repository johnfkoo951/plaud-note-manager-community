import AppKit
import AVKit
import SwiftUI

private enum ContentViewMode: String, CaseIterable, Identifiable {
    case raw
    case rendered

    var id: String { rawValue }
    var title: String { self == .raw ? "Raw" : "Rendered" }
}

private enum AppearanceMode: String, CaseIterable, Identifiable {
    case system
    case dark
    case light

    var id: String { rawValue }
    var title: String {
        switch self {
        case .system: return "System"
        case .dark: return "Dark"
        case .light: return "Light"
        }
    }

    var colorScheme: ColorScheme? {
        switch self {
        case .system: return nil
        case .dark: return .dark
        case .light: return .light
        }
    }
}

enum AppUI {
    static let radius: CGFloat = 12          // Updated for 2026 rounded modern look
    static let tightRadius: CGFloat = 8
    static let panelPadding: CGFloat = 12

    // Spacing scale — use these instead of scattered literals so padding
    // stays consistent across the app.
    static let spacingXS: CGFloat = 4
    static let spacingS: CGFloat = 6
    static let spacingM: CGFloat = 12
    static let spacingL: CGFloat = 16
    static let spacingXL: CGFloat = 20

    static let sectionFont = Font.system(size: 13, weight: .semibold)
    static let rowTitleFont = Font.system(size: 14, weight: .semibold)
    static let bodyFont = Font.system(size: 13)
    static let metaFont = Font.system(size: 11.5, weight: .medium)
    static let controlFont = Font.system(size: 12.5, weight: .semibold)

    static let brandGreen = Color(red: 0.075, green: 0.271, blue: 0.220)
    static let accentPink = Color(red: 0.847, green: 0.365, blue: 0.525)

    // Legacy fills (kept for compatibility)
    static let subtleFill = Color(NSColor.controlBackgroundColor).opacity(0.58)
    static let selectedFill = Color(NSColor.controlBackgroundColor).opacity(0.95)
    static let cardFill = Color(NSColor.controlBackgroundColor).opacity(0.44)
    static let cardStroke = Color(NSColor.separatorColor).opacity(0.42)

    // 2026 Liquid Glass / modern Apple design (WWDC25+)
    // Use .regularMaterial for floating controls, glass for nav/toolbars
    // .glassEffect() for interactive floating elements (macOS Tahoe+)
    static let glassRadius: CGFloat = 12
    static let glassStroke = Color.white.opacity(0.25)  // subtle highlight for glass
}

private func formatRecordingDurationMs(_ ms: Double?) -> String {
    guard let ms, ms > 0 else { return "—" }
    let s = Int(ms / 1000.0)
    if s >= 3600 { return "\(s/3600)h \((s%3600)/60)m" }
    if s >= 60 { return "\(s/60)m \(s%60)s" }
    return "\(s)s"
}

/// Full Plaud-Web-parity duration WITH seconds: "1h 28m 48s", "8m 55s", "14s".
private func formatRecordingDurationFull(_ ms: Double?) -> String {
    guard let ms, ms > 0 else { return "—" }
    let s = Int(ms / 1000.0)
    if s >= 3600 { return "\(s/3600)h \((s%3600)/60)m \(s%60)s" }
    if s >= 60 { return "\(s/60)m \(s%60)s" }
    return "\(s)s"
}

/// Full "2026-06-12 11:58:16" timestamp for `.help(...)` tooltips.
private func formatRecordingTimestampFull(_ date: Date?) -> String {
    guard let date else { return "Recording start time unknown" }
    let f = DateFormatter()
    f.dateFormat = "yyyy-MM-dd HH:mm:ss"
    return f.string(from: date)
}

/// App version info from Info.plist (set by the package build script), with
/// `swift run` fallbacks. The build script sets CFBundleShortVersionString,
/// CFBundleVersion (git commit count), and a custom "PlaudGitSHA" key.
enum AppVersion {
    private static func info(_ key: String) -> String? {
        let v = Bundle.main.infoDictionary?[key] as? String
        let trimmed = v?.trimmingCharacters(in: .whitespacesAndNewlines)
        return (trimmed?.isEmpty ?? true) ? nil : trimmed
    }
    static var shortVersion: String { info("CFBundleShortVersionString") ?? "dev" }
    static var build: String { info("CFBundleVersion") ?? "0" }
    static var sha: String { info("PlaudGitSHA") ?? "local" }

    /// "Plaud Note Manager v0.4.0 (build 142 · a1b2c3d)" for the settings footer.
    static var longLine: String {
        "Plaud Note Manager v\(shortVersion) (build \(build) · \(sha))"
    }
    /// "v0.4.0 (142)" for the tiny sidebar footer.
    static var shortLine: String {
        "v\(shortVersion) (\(build))"
    }
}

/// Recording start as a compact, locale-aware "Jun 14 · 12:20" for tiles.
private func formatRecordingStart(_ date: Date?) -> String {
    guard let date else { return "—" }
    let day = date.formatted(.dateTime.month(.abbreviated).day())
    let time = date.formatted(.dateTime.hour().minute())
    return "\(day) · \(time)"
}

/// Human-friendly relative age, e.g. "오늘", "어제", "3일 전", "2주 전".
private func relativeAge(_ date: Date?, now: Date = Date()) -> String? {
    guard let date else { return nil }
    let cal = Calendar.current
    if cal.isDateInToday(date) { return "오늘" }
    if cal.isDateInYesterday(date) { return "어제" }
    let days = cal.dateComponents([.day], from: cal.startOfDay(for: date),
                                  to: cal.startOfDay(for: now)).day ?? 0
    if days < 0 { return nil }
    if days < 7 { return "\(days)일 전" }
    if days < 30 { return "\(days / 7)주 전" }
    if days < 365 { return "\(days / 30)개월 전" }
    return "\(days / 365)년 전"
}

/// Full, readable recording start for the header caption — weekday included.
private func formatRecordingStartLong(_ date: Date?) -> String? {
    guard let date else { return nil }
    let base = date.formatted(
        .dateTime.year().month(.abbreviated).day().weekday(.abbreviated).hour().minute()
    )
    if let age = relativeAge(date) { return "\(base) · \(age)" }
    return base
}

@MainActor
func promptForFilename(current: String) -> String? {
    let alert = NSAlert()
    alert.messageText = "Rename session"
    alert.informativeText = "Enter a new name for this Plaud recording."
    alert.alertStyle = .informational
    alert.addButton(withTitle: "Rename")
    alert.addButton(withTitle: "Cancel")
    let input = NSTextField(frame: NSRect(x: 0, y: 0, width: 320, height: 24))
    input.stringValue = current
    input.placeholderString = "Session name"
    alert.accessoryView = input
    alert.window.initialFirstResponder = input
    guard alert.runModal() == .alertFirstButtonReturn else { return nil }
    let next = input.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
    return (next.isEmpty || next == current) ? nil : next
}

private struct ToolbarIconLabel: View {
    let systemName: String
    var active: Bool = false

    var body: some View {
        Image(systemName: systemName)
            .symbolRenderingMode(.hierarchical)
            .font(.system(size: 17, weight: .semibold))
            .imageScale(.medium)
            .foregroundStyle(active ? .primary : .secondary)
            .frame(width: 32, height: 32)
            .background(
                active ? AppUI.selectedFill : AppUI.subtleFill,
                in: RoundedRectangle(cornerRadius: 8)
            )
            .contentShape(RoundedRectangle(cornerRadius: 8))
    }
}

private struct ToolbarProgressLabel: View {
    let text: String?

    var body: some View {
        HStack(spacing: 5) {
            ProgressView()
                .controlSize(.small)
                .frame(width: 14, height: 14)
            if let text {
                Text(text)
                    .font(AppUI.metaFont)
                    .foregroundStyle(.secondary)
            }
        }
        .frame(minWidth: text == nil ? 32 : 58, minHeight: 32)
        .padding(.horizontal, text == nil ? 0 : 6)
        .background(AppUI.subtleFill, in: RoundedRectangle(cornerRadius: AppUI.radius))
    }
}

// MARK: - Auth Status Indicator

/// Visual mapping for a Plaud auth `state` string. Centralizes color, SF Symbol,
/// and a human label so the toolbar dot, popover header, and tooltip stay
/// consistent.
private enum AuthStateStyle {
    case valid, expiring, expired, unconfigured, unknown

    init(_ state: String?) {
        switch state {
        case "valid": self = .valid
        case "expiring": self = .expiring
        case "expired": self = .expired
        // Server refused the token even though its JWT still claims time —
        // same user-visible severity as an outright expiry.
        case "rejected": self = .expired
        case "unconfigured": self = .unconfigured
        default: self = .unknown
        }
    }

    var color: Color {
        switch self {
        case .valid: return .green
        case .expiring: return .orange
        case .expired, .unconfigured: return .red
        case .unknown: return .gray
        }
    }

    var symbolName: String {
        switch self {
        case .valid: return "checkmark.shield.fill"
        case .expiring: return "exclamationmark.shield.fill"
        case .expired, .unconfigured: return "xmark.shield.fill"
        case .unknown: return "questionmark.circle.fill"
        }
    }

    var label: String {
        switch self {
        case .valid: return "Valid"
        case .expiring: return "Expiring soon"
        case .expired: return "Expired"
        case .unconfigured: return "Not configured"
        case .unknown: return "Unknown"
        }
    }

    /// States that warrant the re-authenticate hint.
    var needsReauth: Bool {
        switch self {
        case .expired, .expiring, .unconfigured: return true
        case .valid, .unknown: return false
        }
    }
}

/// Format an epoch-second timestamp as a compact local date+time, or "—".
private func formatAuthEpoch(_ epoch: Int?) -> String {
    guard let epoch else { return "—" }
    let date = Date(timeIntervalSince1970: TimeInterval(epoch))
    let formatter = DateFormatter()
    formatter.dateFormat = "yyyy-MM-dd HH:mm"
    return formatter.string(from: date)
}

/// Toolbar dot + compact remaining-time label, opening a detail popover on tap.
private struct AuthStatusIndicator: View {
    @ObservedObject var store: FileStore
    @State private var showPopover = false
    @State private var showAuthSheet = false
    @State private var verifying = false

    private var style: AuthStateStyle { AuthStateStyle(store.auth?.state) }

    /// Compact trailing text: remaining time for valid/expiring, else "auth".
    private var compactText: String {
        guard let auth = store.auth else { return "auth" }
        switch AuthStateStyle(auth.state) {
        case .valid, .expiring:
            if let human = auth.remainingHuman, !human.isEmpty {
                // "21h 36m" -> "21h" keeps the toolbar tight.
                return human.split(separator: " ").first.map(String.init) ?? human
            }
            return "auth"
        default:
            return "auth"
        }
    }

    private var tooltip: String {
        guard let auth = store.auth else { return "Plaud auth status (loading…)" }
        var parts = ["Plaud auth: \(AuthStateStyle(auth.state).label)"]
        if let human = auth.remainingHuman, !human.isEmpty,
           AuthStateStyle(auth.state).needsReauth == false {
            parts.append("\(human) left")
        }
        if auth.expiresAt != nil {
            parts.append("expires \(formatAuthEpoch(auth.expiresAt))")
        }
        return parts.joined(separator: " · ")
    }

    var body: some View {
        Button {
            showPopover.toggle()
        } label: {
            HStack(spacing: 5) {
                Circle()
                    .fill(style.color)
                    .frame(width: 9, height: 9)
                Text(compactText)
                    .font(AppUI.metaFont)
                    .foregroundStyle(.secondary)
            }
            .frame(minWidth: 48, minHeight: 32)
            .padding(.horizontal, 6)
            .background(AppUI.subtleFill, in: RoundedRectangle(cornerRadius: AppUI.radius))
            .contentShape(RoundedRectangle(cornerRadius: AppUI.radius))
        }
        .buttonStyle(.plain)
        .help(tooltip)
        .popover(isPresented: $showPopover, arrowEdge: .bottom) {
            popoverContent
        }
        .sheet(isPresented: $showAuthSheet) {
            PlaudAuthSheet(store: store) {
                showAuthSheet = false
                showPopover = false
            }
        }
        .onChange(of: store.authRecoveryPhase) { _, phase in
            guard phase == .needsInteractive else { return }
            showPopover = false
            showAuthSheet = true
            Task { @MainActor in store.markInteractiveAuthPresented() }
        }
    }

    private var popoverContent: some View {
        VStack(alignment: .leading, spacing: AppUI.spacingM) {
            HStack(spacing: 8) {
                Image(systemName: style.symbolName)
                    .font(.system(size: 16, weight: .semibold))
                    .symbolRenderingMode(.hierarchical)
                    .foregroundStyle(style.color)
                Text("Plaud Auth — \(style.label)")
                    .font(.headline)
                Spacer(minLength: 12)
            }

            if let auth = store.auth {
                VStack(alignment: .leading, spacing: 6) {
                    if let detail = auth.detail.isEmpty ? nil : auth.detail {
                        Text(detail)
                            .font(AppUI.metaFont)
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    detailRow("Workspace", auth.workspaceID ?? "—")
                    detailRow("Member", auth.memberID ?? "—")
                    detailRow("Role", auth.role ?? "—")
                    detailRow("Issued", formatAuthEpoch(auth.issuedAt))
                    detailRow("Expires", formatAuthEpoch(auth.expiresAt))
                    if let human = auth.remainingHuman, !human.isEmpty {
                        detailRow("Remaining", human)
                    }
                    if let live = auth.liveOK {
                        detailRow("Live ping", live ? "reachable" : "rejected")
                    }
                    if auth.autoRefresh != nil {
                        detailRow("Auto-refresh", autoRefreshText(auth))
                    }
                }
            } else {
                Text("Loading auth status…")
                    .font(AppUI.metaFont)
                    .foregroundStyle(.secondary)
            }

            Divider()
            if store.auth?.autoRefreshReady == true {
                VStack(alignment: .leading, spacing: 6) {
                    Text(style.needsReauth ? "Renew token" : "Headless refresh")
                        .font(AppUI.controlFont)
                    Text("Tokens renew automatically in the background — no browser needed. Re-authenticate only if refresh stops working.")
                        .font(AppUI.metaFont)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                    HStack(spacing: 8) {
                        Button {
                            Task { await store.refreshWorkspaceToken() }
                        } label: {
                            HStack(spacing: 6) {
                                if store.refreshingWorkspaceToken {
                                    ProgressView().controlSize(.small)
                                }
                                Label("Refresh Token Now", systemImage: "arrow.triangle.2.circlepath")
                            }
                        }
                        .buttonStyle(.borderedProminent)
                        .disabled(store.refreshingWorkspaceToken || store.refreshingAuth)
                        .help("Mint a fresh 24h token from the stored workspace refresh token")

                        Button {
                            showAuthSheet = true
                        } label: {
                            Label("Re-authenticate…", systemImage: "key.viewfinder")
                        }
                    }
                }
            } else {
                VStack(alignment: .leading, spacing: 6) {
                    Text(style.needsReauth ? "Re-authenticate" : "Update credentials")
                        .font(AppUI.controlFont)
                    Text("One-time setup: sign in once with Plaud Web Login. The verified rotating session is kept in macOS Keychain; after that no browser is needed.")
                        .font(AppUI.metaFont)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                    Button {
                        showAuthSheet = true
                    } label: {
                        Label(
                            style.needsReauth ? "Authenticate with Plaud" : "Update Plaud Credentials",
                            systemImage: "key.viewfinder"
                        )
                    }
                    .buttonStyle(.borderedProminent)
                }
            }

            Divider()
            HStack(spacing: 8) {
                Button {
                    NSWorkspace.shared.open(URL(string: "https://web.plaud.ai/")!)
                } label: {
                    Label("Open Plaud", systemImage: "safari")
                }
                .help("Open web.plaud.ai in your browser")

                Button {
                    verifying = true
                    Task {
                        await store.refreshAuth(live: true)
                        verifying = false
                    }
                } label: {
                    HStack(spacing: 6) {
                        if verifying {
                            ProgressView().controlSize(.small)
                        }
                        Text("Verify now")
                    }
                }
                .disabled(verifying || store.refreshingAuth)
                .help("Ping the Plaud API to confirm the token still works")
            }
        }
        .padding(AppUI.spacingL)
        .frame(width: 360)
    }

    /// Human copy for the popover's Auto-refresh row.
    private func autoRefreshText(_ auth: AuthStatus) -> String {
        switch auth.autoRefresh {
        case "ready":
            if let epoch = auth.refreshExpiresAt {
                return "on — armed until \(formatAuthEpoch(epoch))"
            }
            return "on (headless)"
        case "expiring":
            return "on — refresh token expiring soon, re-login recommended"
        case "expired":
            return "off — refresh token expired, re-login needed"
        case "not_bootstrapped":
            return "off — sign in once via Plaud Web Login to enable"
        case "disabled":
            return "off — disabled by PLAUD_AUTO_REFRESH"
        case "store_unavailable":
            return "unavailable — unlock macOS Keychain"
        default:
            return auth.autoRefresh ?? "—"
        }
    }

    private func detailRow(_ label: String, _ value: String) -> some View {
        HStack(alignment: .top, spacing: 8) {
            Text(label)
                .font(AppUI.metaFont)
                .foregroundStyle(.secondary)
                .frame(width: 78, alignment: .leading)
            Text(value)
                .font(AppUI.metaFont)
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}

/// Resolve the provider + configured API model id used for global AI actions
/// (metadata generation, meeting-note). Reads `data/config.json` via
/// `loadAppConfig()`. Defaults to the `claude` provider — the CLI's own
/// default — and that provider's configured model id (empty if unset, in which
/// case the CLI falls back to its built-in default).
@MainActor
func defaultAIModelChoice(provider: String = "claude") -> (provider: String, modelID: String) {
    let config = Database.shared.loadAppConfig()
    let modelID = (config.models[provider] ?? "")
        .trimmingCharacters(in: .whitespacesAndNewlines)
    return (provider, modelID)
}

struct ContentView: View {
    @StateObject private var store = FileStore()
    @State private var showSettings: Bool = false
    @State private var showCommandPalette: Bool = false
    @AppStorage("appearanceMode") private var appearanceModeRaw: String =
        AppearanceMode.system.rawValue

    var body: some View {
        ZStack {
            mainSplitView

            if store.authRecoveryPhase == .webSession {
                PlaudSilentRecoveryView(store: store)
            }

            // ⌘K command palette, floating above the whole split view.
            if !DistributionProfile.isCommunity, showCommandPalette {
                CommandPaletteOverlay(store: store, isPresented: $showCommandPalette)
            }
        }
        .preferredColorScheme(
            (AppearanceMode(rawValue: appearanceModeRaw) ?? .system).colorScheme
        )
        .onReceive(
            NotificationCenter.default.publisher(for: .togglePlaudCommandPalette)
        ) { _ in
            if !DistributionProfile.isCommunity { showCommandPalette.toggle() }
        }
    }

    /// The original split view with its toolbar/sheets/alerts attached, kept
    /// on the NavigationSplitView itself so toolbar placement is unchanged by
    /// the palette's ZStack wrapper.
    private var mainSplitView: some View {
        // Wider-detail default layout: comfortable sidebar + file list, the
        // detail pane takes the rest so the source tab row never wraps.
        NavigationSplitView {
            SidebarView(store: store)
                .navigationSplitViewColumnWidth(min: 200, ideal: 230, max: 280)
                // 2026 Liquid Glass: .backgroundExtensionEffect() on macOS 26+ for floating sidebar
        } content: {
            FileListView(store: store)
                .navigationSplitViewColumnWidth(min: 300, ideal: 340, max: 420)
        } detail: {
            DetailView(store: store)
        }
        .toolbar {
            ToolbarItemGroup {
                Button { showSettings = true } label: {
                    ToolbarIconLabel(systemName: "gearshape")
                }
                .keyboardShortcut(".", modifiers: .command)
                .help("Settings (Command + , or Command + .)")
                Button {
                    Task { await store.deepSync() }
                } label: {
                    if store.deepSyncRunning {
                        ToolbarProgressLabel(
                            text: "\(store.cacheStatus.cached)/\(store.cacheStatus.total)"
                        )
                    } else {
                        ToolbarIconLabel(systemName: "square.and.arrow.down")
                    }
                }
                .help("Pre-cache transcripts + summaries for every file so clicks are instant.")

                Button {
                    Task { await store.classifyPreview() }
                } label: {
                    if store.classifyRunning {
                        ToolbarProgressLabel(text: nil)
                    } else {
                        ToolbarIconLabel(systemName: "wand.and.stars")
                    }
                }
                .disabled(store.classifyRunning)
                .help("Suggest one of your existing Plaud folders, then preview before applying.")

                Button {
                    Task { await store.sync() }
                } label: {
                    if store.isSyncing {
                        ToolbarProgressLabel(text: nil)
                    } else {
                        ToolbarIconLabel(systemName: "arrow.clockwise")
                    }
                }
                .disabled(store.isSyncing)

                AuthStatusIndicator(store: store)
            }
        }
        .sheet(isPresented: $showSettings) {
            if DistributionProfile.isCommunity {
                CommunitySettingsSheet(store: store) { showSettings = false }
            } else {
                SettingsSheet(store: store) { showSettings = false }
            }
        }
        .onReceive(NotificationCenter.default.publisher(for: .openPlaudSettings)) { _ in
            showSettings = true
        }
        .alert(
            "Plaud command failed",
            isPresented: Binding(
                get: { store.lastCommandError != nil },
                set: { if !$0 { store.lastCommandError = nil } }
            )
        ) {
            Button("OK") { store.lastCommandError = nil }
        } message: {
            Text(store.lastCommandError ?? "")
        }
        .alert(
            "Auto-classify",
            isPresented: Binding(
                get: { store.classifyResult != nil },
                set: { if !$0 { store.classifyResult = nil } }
            )
        ) {
            Button("OK") { store.classifyResult = nil }
        } message: {
            Text(store.classifyResult ?? "")
        }
        .sheet(isPresented: Binding(
            get: { store.classifyPlans != nil },
            set: { if !$0 { store.dismissClassifyPreview() } }
        )) {
            if let plans = store.classifyPlans {
                ClassifyPreviewSheet(
                    store: store,
                    plans: plans,
                    planID: store.classifyPlanID
                ) {
                    store.dismissClassifyPreview()
                }
            }
        }
    }
}

// MARK: - Classify Preview Sheet

/// Preview-before-apply for auto-classification. Lists the dry-run plans
/// sorted by confidence, pre-checks confident matches, and applies
/// only the rows the user keeps checked.
private struct ClassifyPreviewSheet: View {
    @ObservedObject var store: FileStore
    let plans: [FileStore.ClassifyPlan]
    let planID: String?
    let dismiss: () -> Void

    /// File ids the user has selected to apply. Community uses the same 0.6
    /// threshold as the guarded Python apply path.
    @State private var checked: Set<String> = []

    private var minimumConfidence: Double {
        DistributionProfile.isCommunity ? 0.6 : 0.5
    }

    private var lowConfidenceCount: Int {
        plans.filter { $0.confidence < minimumConfidence }.count
    }
    private var targetFolderCount: Int {
        Set(plans.filter { checked.contains($0.fileID) }.map { $0.folderName }).count
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                VStack(alignment: .leading, spacing: 2) {
                    Text("자동 분류 미리보기").font(.title3.bold())
                    Text(headerLine)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Button { dismiss() } label: {
                    Image(systemName: "xmark.circle.fill")
                        .font(.system(size: 18, weight: .semibold))
                        .symbolRenderingMode(.hierarchical)
                }
                .buttonStyle(.plain)
                .foregroundStyle(.secondary)
                .keyboardShortcut(.cancelAction)
            }
            .padding(.horizontal, AppUI.spacingXL)
            .padding(.vertical, AppUI.spacingL)

            Divider()

            ScrollView {
                LazyVStack(spacing: 1) {
                    ForEach(plans) { plan in
                        planRow(plan)
                    }
                }
                .padding(AppUI.spacingS)
            }

            Divider()

            HStack {
                Button("취소") { dismiss() }
                    .keyboardShortcut(.cancelAction)
                Spacer()
                Button("적용 (\(checked.count)개)") {
                    let ids = checked.sorted()
                    let approvedPlanID = planID
                    dismiss()
                    Task {
                        await store.applyClassify(
                            fileIDs: ids,
                            planID: approvedPlanID
                        )
                    }
                }
                .keyboardShortcut(.defaultAction)
                .controlSize(.large)
                .disabled(
                    checked.isEmpty || store.classifyRunning
                        || (DistributionProfile.isCommunity && planID == nil)
                )
            }
            .padding(.horizontal, AppUI.spacingXL)
            .padding(.vertical, 14)
        }
        .frame(width: 640, height: 560)
        .onAppear {
            checked = Set(plans.filter { $0.confidence >= minimumConfidence }.map { $0.fileID })
        }
    }

    private var headerLine: String {
        "\(targetFolderCount)개 폴더로 이동 예정 · \(lowConfidenceCount)개는 신뢰도 낮아 제외"
    }

    private func planRow(_ plan: FileStore.ClassifyPlan) -> some View {
        let isChecked = checked.contains(plan.fileID)
        let isEligible = plan.confidence >= minimumConfidence
        return Button {
            guard isEligible else { return }
            if isChecked { checked.remove(plan.fileID) }
            else { checked.insert(plan.fileID) }
        } label: {
            HStack(spacing: 10) {
                Image(systemName: isChecked ? "checkmark.square.fill" : "square")
                    .font(.system(size: 15, weight: .semibold))
                    .foregroundStyle(isChecked ? Color.accentColor : Color.secondary)
                VStack(alignment: .leading, spacing: 2) {
                    Text(plan.title)
                        .font(.system(size: 13, weight: .medium))
                        .lineLimit(1)
                    HStack(spacing: 4) {
                        Image(systemName: "arrow.right")
                            .font(.system(size: 9, weight: .bold))
                            .foregroundStyle(.tertiary)
                        Label(plan.folderName, systemImage: "folder.fill")
                            .font(.system(size: 11))
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                    }
                    if DistributionProfile.isCommunity {
                        let source = plan.source == "llm" ? "AI" : "로컬"
                        let detail = [source, plan.reason, plan.error]
                            .filter { !$0.isEmpty }
                            .joined(separator: " · ")
                        Text(detail)
                            .font(.system(size: 10))
                            .foregroundStyle(plan.error.isEmpty ? Color.secondary : Color.orange)
                            .lineLimit(2)
                    }
                }
                Spacer(minLength: 8)
                confidencePill(plan.confidence)
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 7)
            .contentShape(RoundedRectangle(cornerRadius: AppUI.tightRadius))
            .help(plan.reason.isEmpty ? plan.folderName : plan.reason)
        }
        .buttonStyle(.plain)
        .disabled(!isEligible)
    }

    private func confidencePill(_ value: Double) -> some View {
        let pct = Int((value * 100).rounded())
        let color: Color = value >= 0.7 ? AnuPalette.green
            : (value >= minimumConfidence ? AnuPalette.yellow : Color.gray)
        return Text("\(pct)%")
            .font(.system(size: 11, weight: .bold))
            .foregroundStyle(color)
            .padding(.horizontal, 7)
            .padding(.vertical, 2)
            .background(color.opacity(0.18), in: Capsule())
    }
}

// MARK: - Settings Sheet

private struct SettingsSheet: View {
    @ObservedObject var store: FileStore
    let dismiss: () -> Void

    @State private var config: Database.AppConfig = Database.shared.loadAppConfig()
    @State private var presets: [ModelPresetVM] = Database.shared.loadModelPresets()
    @AppStorage("defaultContentViewMode") private var defaultViewModeRaw: String =
        ContentViewMode.rendered.rawValue
    @AppStorage("appearanceMode") private var appearanceModeRaw: String =
        AppearanceMode.system.rawValue

    private let models = ["claude", "codex", "gemini", "grok"]
    private let backends = ["cli", "api"]
    private let envHints: [String: String] = [
        "claude": "ANTHROPIC_API_KEY",
        "codex":  "OPENAI_API_KEY",
        "gemini": "GEMINI_API_KEY",
        "grok": "XAI_API_KEY",
    ]

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text("Settings")
                    .font(.title3.bold())
                Spacer()
                Button { dismiss() } label: {
                    Image(systemName: "xmark.circle.fill")
                        .font(.system(size: 18, weight: .semibold))
                        .symbolRenderingMode(.hierarchical)
                }
                .buttonStyle(.plain)
                .foregroundStyle(.secondary)
                .help("Close settings")
            }
            .padding(.horizontal, AppUI.spacingXL)
            .padding(.vertical, AppUI.spacingL)

            Divider()

            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    section("View") {
                        settingsRow("Default view") {
                            Picker("", selection: $defaultViewModeRaw) {
                                ForEach(ContentViewMode.allCases) { mode in
                                    Text(mode.title).tag(mode.rawValue)
                                }
                            }
                            .pickerStyle(.segmented)
                            .labelsHidden()
                            .frame(width: 220)
                        }

                        settingsRow("Appearance") {
                            Picker("", selection: $appearanceModeRaw) {
                                ForEach(AppearanceMode.allCases) { mode in
                                    Text(mode.title).tag(mode.rawValue)
                                }
                            }
                            .pickerStyle(.segmented)
                            .labelsHidden()
                            .frame(width: 260)
                        }

                        Text("Command + , (or Command + .) opens this panel. Rendered view "
                             + "uses chat bubbles for transcripts and AnuPpuccin-flavored "
                             + "Markdown for notes.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }

                    section("Model backends") {
                        VStack(alignment: .leading, spacing: 10) {
                            ForEach(models, id: \.self) { m in
                                backendRow(m)
                            }
                        }
                        Text("CLI mode uses `claude` / `codex` / `gemini` / `grok` shell "
                             + "commands (OAuth or whatever the CLI is logged in with — "
                             + "Grok via Grok Build `grok -p` with a SuperGrok subscription, "
                             + "no API cost). API mode uses the env key shown next to each "
                             + "row. Presets are read from the CMDS API Information folder.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }

                    section("Auto-classify") {
                        settingsRow("Classify model") {
                            Picker("", selection: Binding(
                                get: { config.classifyModel },
                                set: { newVal in
                                    config.classifyModel = newVal
                                    Task { await store.setClassifyModel(newVal) }
                                }
                            )) {
                                ForEach(models, id: \.self) { Text($0).tag($0) }
                            }
                            .pickerStyle(.segmented)
                            .labelsHidden()
                            .frame(width: 280)
                        }
                        Text(classifyModelCaption)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }

                    section("Metadata") {
                        settingsRow("Metadata model") {
                            Picker("", selection: Binding(
                                get: { config.metadataModel },
                                set: { newVal in
                                    config.metadataModel = newVal
                                    Task { await store.setMetadataModel(newVal) }
                                }
                            )) {
                                ForEach(models, id: \.self) { Text($0).tag($0) }
                            }
                            .pickerStyle(.segmented)
                            .labelsHidden()
                            .frame(width: 280)
                        }
                        settingsRow("Auto-generate") {
                            Toggle("", isOn: Binding(
                                get: { config.autoMetadata },
                                set: { newVal in
                                    config.autoMetadata = newVal
                                    Task { await store.setAutoMetadata(newVal) }
                                }
                            ))
                            .toggleStyle(.switch)
                            .labelsHidden()
                        }
                        Text(metadataModelCaption)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }

                    section("Output folders") {
                        pathRow("Transcripts", kind: "transcripts",
                                defaultText: "data/transcripts/")
                        pathRow("Summaries", kind: "summaries",
                                defaultText: "data/summaries/")
                        pathRow("Integrated", kind: "integrated",
                                defaultText: "data/integrated/")
                        Text("Empty = use the project default. "
                             + "Paths starting with `~/` are expanded.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
                .padding(AppUI.spacingXL)
                .frame(maxWidth: .infinity, alignment: .leading)
            }

            Divider()

            HStack {
                Text(AppVersion.longLine)
                    .font(.caption)
                    .foregroundStyle(.tertiary)
                Spacer()
                Button("Done") { dismiss() }
                    .keyboardShortcut(.defaultAction)
                    .controlSize(.large)
            }
            .padding(.horizontal, AppUI.spacingXL)
            .padding(.vertical, 14)
        }
        .frame(width: 760, height: 640)
        .onAppear {
            presets = Database.shared.loadModelPresets()
        }
    }

    /// Which subscription/API the configured classify model will bill,
    /// resolved from the backends map — e.g. "via claude CLI (구독)" or
    /// "via xAI API ($XAI_API_KEY)".
    private var classifyModelCaption: String {
        let model = config.classifyModel
        let providerLabels = [
            "claude": "Anthropic", "codex": "OpenAI",
            "gemini": "Google", "grok": "xAI",
        ]
        let route: String
        if (config.backends[model] ?? "cli") == "api" {
            route = "via \(providerLabels[model] ?? model) API "
                + "($\(envHints[model] ?? ""))"
        } else {
            route = "via \(model) CLI (구독)"
        }
        return "Auto-classify runs \(route). "
            + "Used whenever no explicit model override is given."
    }

    /// Billing route for the metadata model (mirrors classifyModelCaption).
    private var metadataModelCaption: String {
        let model = config.metadataModel
        let providerLabels = [
            "claude": "Anthropic", "codex": "OpenAI",
            "gemini": "Google", "grok": "xAI",
        ]
        let route: String
        if (config.backends[model] ?? "cli") == "api" {
            route = "via \(providerLabels[model] ?? model) API "
                + "($\(envHints[model] ?? ""))"
        } else {
            route = "via \(model) CLI (구독)"
        }
        return "Metadata generation runs \(route). With Auto-generate on, "
            + "freshly synced recordings get metadata without a manual click "
            + "(folder placement stays suggestion-only)."
    }

    private func section<Content: View>(
        _ title: String, @ViewBuilder content: () -> Content
    ) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title).font(AppUI.sectionFont)
            content()
        }
        .padding(AppUI.spacingM)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(AppUI.subtleFill)
        .cornerRadius(AppUI.radius)
    }

    private func settingsRow<Content: View>(
        _ label: String,
        @ViewBuilder content: () -> Content
    ) -> some View {
        HStack(spacing: 12) {
            Text(label)
                .frame(width: 120, alignment: .leading)
                .foregroundStyle(.secondary)
            content()
            Spacer(minLength: 0)
        }
        .font(AppUI.bodyFont)
    }

    private func backendRow(_ model: String) -> some View {
        HStack(spacing: 10) {
            Text(model)
                .frame(width: 72, alignment: .leading)
                .font(.body.monospaced())
            Picker("", selection: Binding(
                get: { config.backends[model] ?? "cli" },
                set: { newVal in
                    config.backends[model] = newVal
                    Task { await store.setBackend(model, newVal) }
                }
            )) {
                ForEach(backends, id: \.self) { Text($0).tag($0) }
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .frame(width: 118)
            TextField("model id", text: Binding(
                get: { config.models[model] ?? "" },
                set: { config.models[model] = $0 }
            ), onCommit: {
                Task { await store.setModelId(model, config.models[model] ?? "") }
            })
            .textFieldStyle(.roundedBorder)
            .frame(minWidth: 240)
            Menu {
                ForEach(presets.filter { $0.provider == model }) { preset in
                    Button(preset.apiName) {
                        config.models[model] = preset.apiName
                        Task { await store.setModelId(model, preset.apiName) }
                    }
                }
            } label: {
                Image(systemName: "list.bullet.rectangle")
                    .frame(width: 28, height: 24)
            }
            .menuStyle(.borderlessButton)
            .help("Pick from CMDS API Information presets")
            Text(config.backends[model] == "api" ? "via $\(envHints[model] ?? "")" : "")
                .font(.caption2)
                .foregroundStyle(.tertiary)
                .frame(width: 118, alignment: .leading)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func pathRow(_ label: String, kind: String,
                         defaultText: String) -> some View {
        HStack(spacing: 10) {
            Text(label)
                .frame(width: 110, alignment: .leading)
                .foregroundStyle(.secondary)
            TextField(defaultText, text: Binding(
                get: { config.paths[kind] ?? "" },
                set: { config.paths[kind] = $0 }
            ), onCommit: {
                Task { await store.setPath(kind, config.paths[kind] ?? "") }
            })
            .textFieldStyle(.roundedBorder)
            Button {
                let panel = NSOpenPanel()
                panel.canChooseFiles = false
                panel.canChooseDirectories = true
                panel.canCreateDirectories = true
                if panel.runModal() == .OK, let url = panel.url {
                    let p = url.path
                    config.paths[kind] = p
                    Task { await store.setPath(kind, p) }
                }
            } label: {
                Image(systemName: "folder.badge.plus")
            }
            .buttonStyle(.borderless)
            .help("Pick a folder")
        }
    }
}

// MARK: - Sidebar

/// File-list row density. Compact keeps every recording on two tight lines;
/// comfortable spreads it over three with a larger title for easier scanning.
enum RowDensity: String, CaseIterable, Identifiable {
    case compact
    case comfortable
    var id: String { rawValue }
    var title: String { self == .compact ? "2줄 (조밀)" : "3줄 (여유)" }
    var symbol: String {
        self == .compact ? "list.dash" : "list.bullet.below.rectangle"
    }
}

/// Plaud's folder palette (matches the web app's Edit folder swatches).
let PLAUD_COLORS: [String] = [
    "#4f4f4f", "#4c8eff", "#5dc967", "#f5a93c",
    "#4fcfd0", "#ee6e72", "#b566f5",
]

/// How the sidebar Tags section is ordered.
private enum TagSortMode: String, CaseIterable {
    case frequency  // count desc
    case alphabetical  // A–Z, COLLATE NOCASE

    var next: TagSortMode { self == .frequency ? .alphabetical : .frequency }
    var symbolName: String {
        self == .frequency ? "arrow.down.right.circle" : "textformat"
    }
    var help: String {
        self == .frequency ? "정렬: 빈도순 (탭하여 가나다순으로)"
                           : "정렬: 가나다순 (탭하여 빈도순으로)"
    }
}

private struct SidebarView: View {
    @ObservedObject var store: FileStore
    @State private var editing: FolderVM?
    @State private var creating: Bool = false
    @State private var createName: String = ""
    /// Folder id currently hovered by a file drag (nil = none). Drives the
    /// subtle drop highlight on the targeted folder row.
    @State private var dropTargetID: String?
    @State private var unfiledTargeted: Bool = false
    @AppStorage("tagSortMode") private var tagSortModeRaw: String =
        TagSortMode.frequency.rawValue
    @AppStorage("tagNestedView") private var tagNestedView: Bool = false
    /// Collapsed parent prefixes in the nested tag tree (default: expanded).
    @State private var collapsedTagParents: Set<String> = []

    private var tagSortMode: TagSortMode {
        TagSortMode(rawValue: tagSortModeRaw) ?? .frequency
    }

    var body: some View {
        List(selection: Binding(
            get: { store.sidebar },
            set: { if let v = $0 { store.sidebar = v } }
        )) {
            Section {
                row(.allFiles, "tray.full", "All files", store.categoryCounts.all)
                // Dropping a file on Unfiled clears its folder assignment.
                row(.unfiled, "tray", "Unfiled", store.categoryCounts.unfiled)
                    .background(
                        unfiledTargeted ? Color.accentColor.opacity(0.18) : Color.clear,
                        in: RoundedRectangle(cornerRadius: AppUI.tightRadius)
                    )
                    .dropDestination(for: String.self) { items, _ in
                        guard let fileID = items.first, !fileID.isEmpty else { return false }
                        store.assignFolder(fileID, folderID: nil)
                        return true
                    } isTargeted: { unfiledTargeted = $0 }
                row(.starred, "star.fill", "Starred", store.categoryCounts.starred,
                    tint: FileRow.starGold)
                row(.trash, "trash", "Trash", store.categoryCounts.trash)
            }
            Section {
                ForEach(store.folders) { folder in
                    HStack {
                        Image(systemName: folder.sfSymbol)
                            .foregroundStyle(folder.color.flatMap { Color(hex: $0) } ?? .secondary)
                            .frame(width: 18)
                        Text(folder.name)
                            .font(AppUI.rowTitleFont)
                            .lineLimit(1)
                        Spacer()
                        Text("\(folder.count)")
                            .font(AppUI.metaFont)
                            .foregroundStyle(.tertiary)
                            .frame(width: 34, alignment: .trailing)
                    }
                    .padding(.vertical, 2)
                    .background(
                        dropTargetID == folder.id
                            ? Color.accentColor.opacity(0.18)
                            : Color.clear,
                        in: RoundedRectangle(cornerRadius: AppUI.tightRadius)
                    )
                    .tag(SidebarItem.folder(folder.id))
                    .contextMenu {
                        Button("Edit folder") { editing = folder }
                        Button("Delete", role: .destructive) {
                            Task { await store.deleteFolder(folder.id) }
                        }
                    }
                    // Drop a dragged file row here to re-file it (single-folder
                    // replace semantics, same as the radio menus).
                    .dropDestination(for: String.self) { items, _ in
                        guard let fileID = items.first, !fileID.isEmpty else { return false }
                        store.assignFolder(fileID, folderID: folder.id)
                        return true
                    } isTargeted: { targeted in
                        if targeted {
                            dropTargetID = folder.id
                        } else if dropTargetID == folder.id {
                            dropTargetID = nil
                        }
                    }
                }
            } header: {
                HStack {
                    Text("Folders")
                        .font(AppUI.sectionFont)
                    Spacer()
                    Button {
                        createName = ""
                        creating = true
                    } label: {
                        Image(systemName: "plus").font(AppUI.sectionFont)
                    }
                    .buttonStyle(.borderless)
                    .help("New folder")
                }
                .padding(.vertical, 2)
            }

            if !store.tagCounts.isEmpty {
                Section {
                    // Pinned tags stay flat at the very top (no nesting).
                    ForEach(pinnedTagEntries, id: \.tag) { entry in
                        tagRow(entry.tag, count: entry.count, pinned: true)
                    }
                    if tagNestedView {
                        ForEach(tagTree, id: \.id) { node in
                            nestedTagRows(node)
                        }
                    } else {
                        ForEach(unpinnedSortedTags, id: \.tag) { entry in
                            tagRow(entry.tag, count: entry.count, pinned: false)
                        }
                    }
                } header: {
                    HStack(spacing: 6) {
                        Text("Tags")
                            .font(AppUI.sectionFont)
                        Spacer()
                        Button {
                            tagSortModeRaw = tagSortMode.next.rawValue
                        } label: {
                            Image(systemName: tagSortMode.symbolName)
                                .font(AppUI.sectionFont)
                        }
                        .buttonStyle(.borderless)
                        .help(tagSortMode.help)
                        Button {
                            tagNestedView.toggle()
                        } label: {
                            Image(systemName: tagNestedView
                                  ? "list.bullet.indent" : "list.bullet")
                                .font(AppUI.sectionFont)
                        }
                        .buttonStyle(.borderless)
                        .help(tagNestedView
                              ? "중첩 보기 켜짐 (탭하여 평면 목록으로)"
                              : "중첩 보기 꺼짐 (탭하여 parent/child 트리로)")
                    }
                    .padding(.vertical, 2)
                }
            }
        }
        .listStyle(.sidebar)
        .safeAreaInset(edge: .bottom) {
            HStack {
                Text(AppVersion.shortLine)
                    .font(.system(size: 9.5))
                    .foregroundStyle(.tertiary)
                Spacer()
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 4)
        }
        .sheet(item: $editing) { folder in
            EditFolderSheet(folder: folder, store: store) { editing = nil }
        }
        .sheet(isPresented: $creating) {
            createSheet()
        }
    }

    private func createSheet() -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("New folder").font(.headline)
            TextField("Name", text: $createName)
                .textFieldStyle(.roundedBorder)
                .frame(minWidth: 280)
            HStack {
                Spacer()
                Button("Cancel") { creating = false }
                Button("Create") {
                    Task {
                        await store.createFolder(name: createName)
                        creating = false
                    }
                }.keyboardShortcut(.defaultAction).disabled(createName.isEmpty)
            }
        }.padding(AppUI.spacingXL)
    }

    @ViewBuilder
    private func row(_ item: SidebarItem, _ symbol: String,
                     _ title: String, _ count: Int,
                     tint: Color? = nil) -> some View {
        HStack {
            if let tint {
                Label {
                    Text(title)
                } icon: {
                    Image(systemName: symbol).foregroundStyle(tint)
                }
                .font(AppUI.rowTitleFont)
            } else {
                Label(title, systemImage: symbol)
                    .font(AppUI.rowTitleFont)
            }
            Spacer()
            Text("\(count)")
                .font(AppUI.metaFont)
                .foregroundStyle(.tertiary)
                .frame(width: 34, alignment: .trailing)
        }
        .padding(.vertical, 2)
        .tag(item)
    }

    /// Pinned tags in config order (each with its current count), shown flat at
    /// the top of the section.
    private var pinnedTagEntries: [(tag: String, count: Int)] {
        let counts = Dictionary(store.tagCounts.map { ($0.tag, $0.count) },
                                uniquingKeysWith: { a, _ in a })
        return store.pinnedTags.map { (tag: $0, count: counts[$0] ?? 0) }
    }

    /// Non-pinned tags, ordered by the active sort mode. The DB already returns
    /// them count-desc / NOCASE-tie-broken, so frequency is the identity and
    /// alphabetical is a NOCASE re-sort.
    private var unpinnedSortedTags: [(tag: String, count: Int)] {
        let pinnedSet = Set(store.pinnedTags)
        let rest = store.tagCounts.filter { !pinnedSet.contains($0.tag) }
        switch tagSortMode {
        case .frequency:
            return rest
        case .alphabetical:
            return rest.sorted {
                $0.tag.localizedCaseInsensitiveCompare($1.tag) == .orderedAscending
            }
        }
    }

    /// A node in the nested tag tree. A top-level segment (e.g. `AI`) with its
    /// summed count (itself + all `AI/...` descendants) and the full child tag
    /// strings under it. A tag with no `/` is a childless leaf at top level.
    private struct TagNode {
        let id: String          // top-level segment, also the parent prefix
        let totalCount: Int     // self + descendants
        let selfCount: Int      // exact-`id` tag count (0 if only descendants)
        /// Full child tags (e.g. `AI/agent`), sorted per the active mode.
        let children: [(tag: String, count: Int)]
        var hasChildren: Bool { !children.isEmpty }
    }

    /// Build the parent/child tree from the non-pinned tags, ordered by the
    /// active sort mode at both levels.
    private var tagTree: [TagNode] {
        let entries = unpinnedSortedTags
        // Group by the first `/`-segment, preserving the (already-sorted)
        // order of first appearance for parents.
        var order: [String] = []
        var groups: [String: [(tag: String, count: Int)]] = [:]
        for entry in entries {
            let parent = entry.tag.split(separator: "/", maxSplits: 1)
                .first.map(String.init) ?? entry.tag
            if groups[parent] == nil { order.append(parent) }
            groups[parent, default: []].append(entry)
        }

        return order.map { parent in
            let members = groups[parent] ?? []
            let total = members.reduce(0) { $0 + $1.count }
            let selfCount = members.first { $0.tag == parent }?.count ?? 0
            // Children are every member that isn't the bare parent tag.
            var children = members.filter { $0.tag != parent }
            if tagSortMode == .alphabetical {
                children.sort {
                    $0.tag.localizedCaseInsensitiveCompare($1.tag) == .orderedAscending
                }
            }  // frequency: members already came in count-desc order
            return TagNode(id: parent, totalCount: total, selfCount: selfCount,
                           children: children)
        }
    }

    /// Render one parent node and (when expanded) its children.
    @ViewBuilder
    private func nestedTagRows(_ node: TagNode) -> some View {
        if node.hasChildren {
            let collapsed = collapsedTagParents.contains(node.id)
            parentTagRow(node, collapsed: collapsed)
            if !collapsed {
                ForEach(node.children, id: \.tag) { child in
                    tagRow(child.tag, count: child.count, pinned: false,
                           indent: 1, displayName: leafSegment(child.tag))
                }
            }
        } else {
            // Childless top-level tag: a plain leaf (exact match).
            tagRow(node.id, count: node.totalCount, pinned: false)
        }
    }

    /// A parent row: disclosure triangle + summed count, selecting the prefix
    /// filter (`.tagPrefix`). The triangle toggles expand/collapse.
    @ViewBuilder
    private func parentTagRow(_ node: TagNode, collapsed: Bool) -> some View {
        HStack(spacing: 4) {
            Button {
                if collapsed { collapsedTagParents.remove(node.id) }
                else { collapsedTagParents.insert(node.id) }
            } label: {
                Image(systemName: collapsed ? "chevron.right" : "chevron.down")
                    .font(.system(size: 9, weight: .bold))
                    .foregroundStyle(.tertiary)
                    .frame(width: 12)
            }
            .buttonStyle(.plain)
            Image(systemName: "tag")
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(Color.secondary)
                .frame(width: 16)
            Text("#\(node.id)")
                .font(AppUI.rowTitleFont)
                .lineLimit(1)
                .truncationMode(.tail)
            Spacer()
            Text("\(node.totalCount)")
                .font(AppUI.metaFont)
                .foregroundStyle(.tertiary)
                .frame(width: 34, alignment: .trailing)
        }
        .padding(.vertical, 2)
        .tag(SidebarItem.tagPrefix(node.id))
    }

    /// Last `/`-segment of a tag, used as the indented child label.
    private func leafSegment(_ tag: String) -> String {
        tag.split(separator: "/").last.map(String.init) ?? tag
    }

    @ViewBuilder
    private func tagRow(_ tag: String, count: Int, pinned: Bool,
                        indent: Int = 0, displayName: String? = nil) -> some View {
        HStack {
            if indent > 0 {
                // Align the child label under the parent's tag glyph.
                Spacer().frame(width: CGFloat(indent) * 16 + 12)
            }
            Image(systemName: pinned ? "pin.fill" : "tag")
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(pinned ? FileRow.starGold : Color.secondary)
                .frame(width: 18)
            Text("#\(displayName ?? tag)")
                .font(AppUI.rowTitleFont)
                .lineLimit(1)
                .truncationMode(.tail)
            Spacer()
            Text("\(count)")
                .font(AppUI.metaFont)
                .foregroundStyle(.tertiary)
                .frame(width: 34, alignment: .trailing)
        }
        .padding(.vertical, 2)
        .tag(SidebarItem.tag(tag))
        .contextMenu {
            if pinned {
                Button {
                    Task { await store.unpinTag(tag) }
                } label: {
                    Label("고정 해제", systemImage: "pin.slash")
                }
            } else {
                Button {
                    Task { await store.pinTag(tag) }
                } label: {
                    Label("고정", systemImage: "pin")
                }
            }
        }
    }
}

// MARK: - Folder radio menu

/// Shared menu body for assigning a file's single Plaud folder. Radio
/// behavior: a checkmark marks the current folder only; picking a folder
/// replaces the assignment, and picking the current folder again (or the
/// explicit "Unfiled" item at the top) clears it.
private struct FolderRadioMenuItems: View {
    @ObservedObject var store: FileStore
    let fileID: String

    var body: some View {
        let assigned = store.folderIDs(for: fileID)
        Button {
            store.assignFolder(fileID, folderID: nil)
        } label: {
            if assigned.isEmpty {
                Label("Unfiled", systemImage: "checkmark")
            } else {
                Label("Unfiled", systemImage: "tray")
            }
        }
        Divider()
        ForEach(store.folders) { folder in
            let isCurrent = assigned.contains(folder.id)
            Button {
                // Re-clicking the current folder clears the assignment.
                store.assignFolder(fileID, folderID: isCurrent ? nil : folder.id)
            } label: {
                if isCurrent {
                    Label(folder.name, systemImage: "checkmark")
                } else {
                    Text(folder.name)
                }
            }
        }
    }
}

// MARK: - File List

/// Identifiable wrapper so a plain file-id string can drive `.sheet(item:)`.
private struct MoveSheetTarget: Identifiable {
    let id: String
}

private struct FileListView: View {
    @ObservedObject var store: FileStore
    /// File currently picking a destination folder via the swipe "Move…"
    /// action (nil = sheet closed).
    @State private var moveTarget: MoveSheetTarget?
    /// Persisted search scope: false = 파일명, true = 전체내용. Mirrored into the
    /// store so the filter/debounce logic can read it off the main actor.
    @AppStorage("searchScope") private var searchScopeContent: Bool = false
    /// Row density — compact (2-line) or comfortable (3-line, larger title).
    @AppStorage("rowDensity") private var rowDensityRaw: String = RowDensity.compact.rawValue
    private var rowDensity: RowDensity { RowDensity(rawValue: rowDensityRaw) ?? .compact }

    /// Trimmed query for the empty-state check.
    private var trimmedQuery: String {
        store.search.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// True when content search is active, settled (not running), and produced
    /// zero visible rows — drives the dedicated empty state.
    private var contentSearchEmpty: Bool {
        searchScopeContent && !trimmedQuery.isEmpty
            && !store.contentSearchRunning
            && store.contentSearchQuery == trimmedQuery
            && store.files.isEmpty
    }

    var body: some View {
        VStack(spacing: 0) {
            LibraryOverview(store: store)
                .padding(.horizontal, AppUI.spacingM)
                .padding(.top, AppUI.spacingM)
                .padding(.bottom, 10)

            if store.lastClassifyApply != nil {
                ClassifyUndoBanner(store: store)
                    .padding(.horizontal, AppUI.spacingM)
                    .padding(.bottom, 8)
            }

            // Polished search bar: rounded container with consistent internal treatment
            HStack(spacing: 8) {
                // Magnifier doubles as a scope menu (파일명 ⇄ 전체내용).
                Menu {
                    Picker("", selection: $searchScopeContent) {
                        Label("파일명", systemImage: "textformat")
                            .tag(false)
                        Label("전체 내용", systemImage: "doc.text.magnifyingglass")
                            .tag(true)
                    }
                    .pickerStyle(.inline)
                    .labelsHidden()
                } label: {
                    Image(systemName: searchScopeContent
                          ? "doc.text.magnifyingglass" : "magnifyingglass")
                        .foregroundStyle(searchScopeContent
                                         ? AppUI.accentPink : .secondary)
                        .frame(width: 18)
                }
                .menuStyle(.borderlessButton)
                .menuIndicator(.hidden)
                .fixedSize()
                .help("검색 범위: 파일명 또는 전체 내용")

                TextField(searchScopeContent ? "전체 내용 검색" : "파일명 검색",
                          text: $store.search)
                    .textFieldStyle(.plain)
                    .font(AppUI.bodyFont)
                    .frame(maxWidth: .infinity)

                if store.contentSearchRunning {
                    ProgressView()
                        .controlSize(.small)
                        .frame(width: 14, height: 14)
                }

                // Row density picker (2줄 ⇄ 3줄) — choose list compactness.
                Menu {
                    Picker("행 밀도", selection: $rowDensityRaw) {
                        ForEach(RowDensity.allCases) { d in
                            Label(d.title, systemImage: d.symbol).tag(d.rawValue)
                        }
                    }
                    .pickerStyle(.inline)
                } label: {
                    Image(systemName: rowDensity.symbol)
                        .foregroundStyle(.secondary)
                        .frame(width: 18)
                }
                .menuStyle(.borderlessButton)
                .menuIndicator(.hidden)
                .fixedSize()
                .help("행 밀도: 2줄(조밀) 또는 3줄(여유)")
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 8)
            // Clean modern search container – matches the dark theme and metric cards above.
            // Uses subtle dark fill with rounded corners for a cohesive header area.
            .background(
                RoundedRectangle(cornerRadius: 10)
                    .fill(Color(NSColor.controlBackgroundColor).opacity(0.75))
            )
            .overlay(
                RoundedRectangle(cornerRadius: 10)
                    .stroke(Color.white.opacity(0.08), lineWidth: 0.5)
            )
            // Keep the store's scope mirror in sync with the persisted toggle.
            .onAppear { store.contentSearchScope = searchScopeContent }
            .onChange(of: searchScopeContent) { _, newValue in
                store.contentSearchScope = newValue
            }

            Divider()

            List(selection: $store.selectedID) {
                ForEach(store.files) { file in
                    FileRow(file: file,
                            isSelected: store.selectedID == file.id,
                            density: rowDensity,
                            snippet: searchScopeContent
                                ? store.contentSnippets[file.id] : nil,
                            onToggleStar: { store.toggleStar(file.id) })
                        .tag(Optional(file.id))
                        // Flat rows own their padding; zero insets keep the
                        // accent bar + hover fill flush with the list edges.
                        .listRowInsets(EdgeInsets(top: 0, leading: 0, bottom: 0, trailing: 0))
                        .listRowSeparator(.hidden)
                        .listRowBackground(Color.clear)
                        // Drag onto a sidebar folder (or Unfiled) to re-file.
                        // Payload is the plain file id string; click-to-select
                        // is untouched because the drag only starts on movement.
                        // Drag and swipeActions coexist in List — the swipe
                        // only triggers on a horizontal two-finger gesture.
                        .draggable(file.id)
                        .contextMenu {
                            Button {
                                store.toggleStar(file.id)
                            } label: {
                                Label(file.starred ? "Unstar" : "Star",
                                      systemImage: file.starred ? "star.slash" : "star")
                            }
                            Divider()
                            Button("Rename…") {
                                if let newName = promptForFilename(current: file.filename ?? "") {
                                    store.renameFile(file.id, to: newName)
                                }
                            }
                            Button("Open in Plaud Web") {
                                store.openInPlaudWeb(file.id)
                            }
                            Button("Copy Plaud URL") {
                                store.copyPlaudWebURL(file.id)
                            }
                            Divider()
                            // Radio semantics: a file lives in at most ONE
                            // folder (Plaud Web breaks with several). Clicking
                            // a folder replaces the assignment; clicking the
                            // current folder again (or Unfiled) clears it.
                            Menu("Move to folder") {
                                FolderRadioMenuItems(store: store, fileID: file.id)
                            }
                            if !DistributionProfile.isCommunity {
                                Button("Send to Obsidian") {
                                    Task { await store.sendToObsidian(file.id) }
                                }
                            }
                            Button("Transcribe with ElevenLabs…") {
                                Task { await store.transcribeWithElevenLabs(file.id) }
                            }
                            .disabled(store.transcribingIDs.contains(file.id))
                        }
                        .swipeActions(edge: .leading, allowsFullSwipe: true) {
                            // Toggle archived <-> unused via the existing
                            // usage-status path. No delete swipe: Plaud has
                            // no server delete API.
                            let isArchived = file.usageStatus == "archived"
                            Button {
                                Task {
                                    await store.setUsageStatus(
                                        file.id,
                                        status: isArchived ? "unused" : "archived"
                                    )
                                }
                            } label: {
                                Label(isArchived ? "Unarchive" : "Archive",
                                      systemImage: isArchived
                                          ? "tray.and.arrow.up" : "archivebox")
                            }
                            .tint(.gray)
                            // Second leading action — never triggered by a
                            // full swipe (that stays Archive).
                            Button {
                                store.toggleStar(file.id)
                            } label: {
                                Label(file.starred ? "Unstar" : "Star",
                                      systemImage: file.starred
                                          ? "star.slash" : "star.fill")
                            }
                            .tint(.yellow)
                        }
                        .swipeActions(edge: .trailing, allowsFullSwipe: false) {
                            Button {
                                moveTarget = MoveSheetTarget(id: file.id)
                            } label: {
                                Label("Move…", systemImage: "folder")
                            }
                            .tint(.indigo)
                            if !file.folderNames.isEmpty {
                                Button {
                                    store.assignFolder(file.id, folderID: nil)
                                } label: {
                                    Label("Unfile", systemImage: "folder.badge.minus")
                                }
                                .tint(.orange)
                            }
                        }
                }
            }
            .listStyle(.plain)
            .overlay {
                if contentSearchEmpty {
                    contentSearchEmptyState
                }
            }
        }
        .sheet(item: $moveTarget) { target in
            MoveToFolderSheet(store: store, fileID: target.id) { moveTarget = nil }
        }
        // Selecting a recording dismisses the auto-classify undo banner.
        .onChange(of: store.selectedID) { _, _ in
            if !DistributionProfile.isCommunity, !store.classifyUndoNeedsRetry {
                store.dismissClassifyUndo()
            }
        }
    }

    /// Shown when 전체내용 search settles with zero hits. Notes that only
    /// cached recordings are indexed (uncached ones can't be searched yet).
    private var contentSearchEmptyState: some View {
        VStack(spacing: 10) {
            Image(systemName: "doc.text.magnifyingglass")
                .font(.system(size: 28, weight: .regular))
                .symbolRenderingMode(.hierarchical)
                .foregroundStyle(.secondary)
            Text("전체 내용에서 '\(trimmedQuery)' 결과 없음")
                .font(AppUI.rowTitleFont)
                .multilineTextAlignment(.center)
            Text("캐시된 녹음만 검색됩니다 — 아직 받아오지 않은 녹음은 색인되지 않아요.")
                .font(AppUI.metaFont)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(AppUI.spacingXL)
        .frame(maxWidth: 280)
        .background(AppUI.cardFill, in: RoundedRectangle(cornerRadius: AppUI.radius))
    }
}

/// Transient bar offering to undo the last auto-classify. Auto-dismisses
/// after ~12s (or when a recording is selected — handled by the list).
private struct ClassifyUndoBanner: View {
    @ObservedObject var store: FileStore

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: "wand.and.stars")
                .font(.system(size: 12, weight: .semibold))
                .foregroundStyle(AppUI.accentPink)
            Text(
                store.classifyApplyRecoveryRequired
                    ? "중단된 폴더 적용 (store.lastClassifyApply?.count ?? 0)개 복구 필요"
                    : store.classifyUndoNeedsRetry
                    ? "\(store.lastClassifyApply?.count ?? 0)개 되돌리기 재시도 필요"
                    : "\(store.lastClassifyApply?.count ?? 0)개 자동 분류됨"
            )
                .font(.system(size: 12, weight: .medium))
                .lineLimit(1)
            Spacer(minLength: 6)
            Button {
                Task { await store.classifyUndo() }
            } label: {
                Label(
                    store.classifyApplyRecoveryRequired
                        ? "상태 안정화"
                        : store.classifyUndoNeedsRetry ? "재시도" : "되돌리기",
                    systemImage: "arrow.uturn.backward"
                )
                    .font(.system(size: 11.5, weight: .semibold))
            }
            .buttonStyle(.plain)
            .foregroundStyle(Color.accentColor)
            .disabled(store.classifyRunning)
            if !DistributionProfile.isCommunity {
                Button {
                    store.dismissClassifyUndo()
                } label: {
                    Image(systemName: "xmark")
                        .font(.system(size: 10, weight: .bold))
                        .foregroundStyle(.tertiary)
                }
                .buttonStyle(.plain)
                .help("배너 닫기")
            }
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 7)
        .background(AppUI.accentPink.opacity(0.10), in: RoundedRectangle(cornerRadius: AppUI.radius))
        .overlay(
            RoundedRectangle(cornerRadius: AppUI.radius)
                .stroke(AppUI.accentPink.opacity(0.30), lineWidth: 1)
        )
        .task(id: store.lastClassifyApply?.at) {
            guard !DistributionProfile.isCommunity else { return }
            // Auto-dismiss after ~12s unless replaced by a newer apply.
            let stamp = store.lastClassifyApply?.at
            try? await Task.sleep(nanoseconds: 12_000_000_000)
            if store.lastClassifyApply?.at == stamp,
               !store.classifyUndoNeedsRetry {
                store.dismissClassifyUndo()
            }
        }
    }
}

/// Compact destination picker for the swipe "Move…" action: Unfiled + every
/// folder, single-folder radio semantics (same write path as the menus).
private struct MoveToFolderSheet: View {
    @ObservedObject var store: FileStore
    let fileID: String
    let dismiss: () -> Void

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text("Move to folder").font(.headline)
                Spacer()
                Button { dismiss() } label: {
                    Image(systemName: "xmark.circle.fill")
                        .font(.system(size: 15, weight: .semibold))
                        .symbolRenderingMode(.hierarchical)
                }
                .buttonStyle(.plain)
                .foregroundStyle(.secondary)
                .keyboardShortcut(.cancelAction)
            }
            .padding(.horizontal, AppUI.spacingL)
            .padding(.vertical, AppUI.spacingM)

            Divider()

            ScrollView {
                VStack(spacing: 2) {
                    destinationRow(folder: nil)
                    ForEach(store.folders) { folder in
                        destinationRow(folder: folder)
                    }
                }
                .padding(AppUI.spacingS)
            }
            .frame(maxHeight: 340)
        }
        .frame(width: 300)
    }

    private func destinationRow(folder: FolderVM?) -> some View {
        let assigned = store.folderIDs(for: fileID)
        let isCurrent = folder.map { assigned.contains($0.id) } ?? assigned.isEmpty
        return Button {
            store.assignFolder(fileID, folderID: folder?.id)
            dismiss()
        } label: {
            HStack(spacing: 8) {
                Image(systemName: folder?.sfSymbol ?? "tray")
                    .foregroundStyle(
                        folder?.color.flatMap { Color(hex: $0) } ?? .secondary
                    )
                    .frame(width: 18)
                Text(folder?.name ?? "Unfiled")
                    .font(AppUI.bodyFont)
                    .lineLimit(1)
                Spacer(minLength: 8)
                if isCurrent {
                    Image(systemName: "checkmark")
                        .font(.system(size: 11, weight: .bold))
                        .foregroundStyle(.secondary)
                }
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 6)
            .contentShape(RoundedRectangle(cornerRadius: AppUI.tightRadius))
        }
        .buttonStyle(.plain)
    }
}

private struct LibraryOverview: View {
    @ObservedObject var store: FileStore

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(DistributionProfile.isCommunity ? "COMMUNITY" : "CMDSPACE")
                        .font(.system(size: 10, weight: .heavy))
                        .foregroundStyle(AppUI.brandGreen)
                    Text("Recordings")
                        .font(.system(size: 22, weight: .bold))
                }
                Spacer()
                Image(systemName: "waveform.badge.sparkles")
                    .font(.system(size: 18, weight: .semibold))
                    .symbolRenderingMode(.hierarchical)
                    .foregroundStyle(AppUI.accentPink)
            }

            HStack(spacing: 7) {
                MiniMetricCard(
                    label: "All",
                    value: "\(store.categoryCounts.all)",
                    systemName: "tray.full",
                    color: AppUI.brandGreen
                )
                MiniMetricCard(
                    label: "Unfiled",
                    value: "\(store.categoryCounts.unfiled)",
                    systemName: "tray",
                    color: AnuPalette.sky
                )
                MiniMetricCard(
                    label: "Cached",
                    value: "\(store.cacheStatus.cached)/\(store.cacheStatus.total)",
                    systemName: "bolt.horizontal.circle",
                    color: AppUI.accentPink
                )
            }

            if !DistributionProfile.isCommunity {
                elevenLabsLine
            }
        }
        .padding(12)
        // 2026 Liquid Glass inspired: regularMaterial + brand tint overlay + glass stroke
        .background(
            RoundedRectangle(cornerRadius: AppUI.glassRadius)
                .fill(.regularMaterial)
        )
        .overlay(
            RoundedRectangle(cornerRadius: AppUI.glassRadius)
                .stroke(AppUI.glassStroke, lineWidth: 0.6)
        )
    }

    /// ElevenLabs STT credit balance + next reset — the budget behind
    /// CMDS transcription and the dual pipeline. Hidden until fetched;
    /// turns orange under 10% remaining.
    @ViewBuilder
    private var elevenLabsLine: some View {
        if let el = store.elevenLabs, el.status == "ok",
           let remaining = el.remaining, let limit = el.limit, limit > 0 {
            let low = Double(remaining) / Double(limit) < 0.1
            HStack(spacing: 5) {
                Image(systemName: "waveform.circle")
                    .font(.system(size: 10.5, weight: .semibold))
                Text("ElevenLabs \(Self.compact(remaining)) / \(Self.compact(limit)) 크레딧")
                    .font(.system(size: 10.5, weight: .semibold))
                if let reset = el.resetAt, reset > 0 {
                    Text("· 갱신 \(Self.resetLabel(reset))")
                        .font(.system(size: 10.5, weight: .medium))
                        .foregroundStyle(.secondary)
                }
                Spacer(minLength: 0)
            }
            .foregroundStyle(low ? Color.orange : Color.secondary)
            .help(
                "CMDS 전사(ElevenLabs Scribe)·듀얼 파이프라인이 소비하는 크레딧"
                + (el.tier.map { " — \($0) 플랜" } ?? "")
            )
        }
    }

    private static func compact(_ n: Int) -> String {
        n >= 10_000 ? "\(Int((Double(n) / 1000).rounded()))k" : "\(n)"
    }

    private static func resetLabel(_ unix: Int) -> String {
        let date = Date(timeIntervalSince1970: TimeInterval(unix))
        let fmt = DateFormatter()
        fmt.dateFormat = "M/d"
        return fmt.string(from: date)
    }
}

private struct MiniMetricCard: View {
    let label: String
    let value: String
    let systemName: String
    let color: Color
    /// When true the whole tile is tinted with `color` — used for the Status
    /// tile so an applied usage status is unmistakable.
    var tinted: Bool = false

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(spacing: 4) {
                Image(systemName: systemName)
                    .font(.system(size: 10.5, weight: .semibold))
                    .symbolRenderingMode(.hierarchical)
                Text(label)
                    .font(.system(size: 10.5, weight: .bold))
                    .lineLimit(1)
            }
            .foregroundStyle(color)
            Text(value)
                .font(.system(size: 14, weight: .bold))
                .foregroundStyle(tinted ? color : .primary)
                .lineLimit(1)
                .minimumScaleFactor(0.78)
        }
        .frame(maxWidth: .infinity, minHeight: 52, alignment: .leading)
        .padding(.horizontal, 8)
        .padding(.vertical, 7)
        .background(
            tinted ? color.opacity(0.14) : Color(NSColor.controlBackgroundColor).opacity(0.42),
            in: RoundedRectangle(cornerRadius: 8)
        )
        .overlay(
            RoundedRectangle(cornerRadius: 8)
                .stroke(tinted ? color.opacity(0.45) : AppUI.cardStroke.opacity(0.7), lineWidth: 1)
        )
    }
}

/// Flat, dense, Superhuman-flavored row: no card chrome, full-width fill,
/// selection shown by a 3pt accent bar on the left edge + a soft neutral
/// fill. Two lines only — title + date on top, metadata chips below.
private struct FileRow: View {
    let file: PlaudFileVM
    let isSelected: Bool
    /// 2-line (compact) or 3-line (comfortable) layout.
    var density: RowDensity = .compact
    /// FTS snippet (with « » around the matched span) shown only in
    /// content-search mode; nil otherwise.
    var snippet: String? = nil
    /// Clicking the dot/star slot toggles the star (small tap target only,
    /// so row selection elsewhere is untouched).
    let onToggleStar: () -> Void
    @State private var hovering = false

    private var comfortable: Bool { density == .comfortable }

    /// Leading inset for line 2 so it aligns with the title after the
    /// 11pt state-dot slot + 8pt spacing.
    private static let metaIndent: CGFloat = 19
    private static let metaFont = Font.system(size: 11)
    /// Warm gold for the starred state — pretty in dark mode, not neon.
    static let starGold = Color(red: 0.95, green: 0.72, blue: 0.25)

    var body: some View {
        VStack(alignment: .leading, spacing: comfortable ? 4 : 3) {
            HStack(spacing: 8) {
                stateDot
                Text(file.filename ?? "(untitled)")
                    .font(.system(size: 13.5, weight: .semibold))
                    .lineLimit(comfortable ? 2 : 1)
                    .truncationMode(.tail)
                    .foregroundStyle(Color.primary)
                Spacer(minLength: 8)
                if let date = file.createdAt {
                    Text(Self.formatRowDate(date))
                        .font(Self.metaFont)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .help(formatRecordingTimestampFull(date))
                }
            }
            // One packed metadata line in BOTH modes — comfortable just lets the
            // title wrap to 2 lines and uses a larger font, so a row is at most
            // 3 lines (title×2 + meta) instead of ballooning to 4.
            metaLine
            if let snippet, !snippet.isEmpty {
                snippetLine(snippet)
            }
        }
        .padding(.horizontal, AppUI.spacingM)
        .padding(.vertical, comfortable ? 10 : 7)
        .frame(maxWidth: .infinity, alignment: .leading)
        .contentShape(Rectangle())
        .background(rowFill)
        .overlay(alignment: .leading) {
            // Superhuman-style selection: a slim accent bar hugging the left
            // edge instead of a loud filled card. Text colors stay unchanged.
            if isSelected {
                RoundedRectangle(cornerRadius: 1.5)
                    .fill(Color.accentColor)
                    .frame(width: 3)
                    .padding(.vertical, 3)
            }
        }
        .onHover { hovering = $0 }
    }

    private var rowFill: Color {
        if isSelected { return Color.primary.opacity(0.09) }
        if hovering { return Color.primary.opacity(0.04) } // subtle modern hover (glass-like)
        return Color.clear
    }

    /// 분석 전(Plaud 요약 없음) → 빨강 · 분석됨+안읽음 → 초록 · 읽음 → 회색.
    static let pendingRed = Color(red: 0.91, green: 0.43, blue: 0.41)
    static let unreadGreen = Color(red: 0.29, green: 0.78, blue: 0.47)

    /// State dot: gold star = starred; otherwise the pipeline/review color
    /// from `dotColor` (red = transferred-only, green = analyzed-unread,
    /// gray = read). Clicking the slot toggles the star.
    @ViewBuilder
    private var stateDot: some View {
        Group {
            if file.starred {
                Image(systemName: "star.fill")
                    .font(.system(size: 9, weight: .semibold))
                    .foregroundStyle(Self.starGold)
                    .help("즐겨찾기")
            } else {
                Circle()
                    .fill(dotColor)
                    .frame(width: dotSize, height: dotSize)
                    .help(dotHelp)
            }
        }
        .frame(width: 11, height: 11)
        .contentShape(Rectangle())
        .onTapGesture { onToggleStar() }
    }

    /// Pipeline + review state folded into the dot color (replaces the loud
    /// "Generated" badge): 녹음→전송→(Plaud)전사→앱 리뷰. "전사됨" = the recording's
    /// detail (incl. transcript) is cached locally — a transcript-without-Plaud-
    /// summary file still counts as analyzed (that was the red-when-cached bug).
    private var dotColor: Color {
        if !file.hasContent { return Self.pendingRed }    // 데이터 미수신
        if file.seenAt == nil { return Self.unreadGreen } // 전사 완료, 안 읽음
        return Color.gray.opacity(0.34)                   // 읽음
    }

    private var dotSize: CGFloat {
        // Seen (done) is intentionally smaller/quieter than the active states.
        (file.hasContent && file.seenAt != nil) ? 5 : 7
    }

    private var dotHelp: String {
        if !file.hasContent { return "데이터 미수신 · 클릭하면 Plaud에서 받아옴" }
        if file.seenAt == nil { return "전사 완료 · 아직 안 읽음" }
        return "읽음 (리뷰 완료)"
    }

    /// ⚡ dual-transcribe stage marker for list rows: gray while in progress,
    /// orange when waiting on speaker confirmation, blue when vault-ready,
    /// green once the final note is in the vault.
    private var dualColor: Color? {
        guard !DistributionProfile.isCommunity else { return nil }
        switch file.dualStatus {
        case nil: return nil
        case "relabel-pending": return .orange
        case "vault-ready": return .blue
        case "vault-sent": return .green
        default: return .secondary
        }
    }

    @ViewBuilder
    private var metaLine: some View {
        if file.durationMs != nil || folderLabel != nil
            || meaningfulUsage != nil || !file.primaryTags.isEmpty
            || dualColor != nil {
            HStack(spacing: 6) {
                if file.durationMs != nil {
                    Text(formatRecordingDurationFull(file.durationMs))
                        .font(Self.metaFont)
                        .foregroundStyle(.secondary)
                }
                if let dualColor {
                    Image(systemName: "bolt.badge.waveform")
                        .font(.system(size: 9.5, weight: .semibold))
                        .foregroundStyle(dualColor)
                        .help("Dual: \(file.dualStatus ?? "")")
                }
                if let folderLabel {
                    if file.durationMs != nil { dotSeparator }
                    folderChip(folderLabel)
                }
                if let usage = meaningfulUsage {
                    usageChip(usage)
                }
                ForEach(Array(file.primaryTags.prefix(comfortable ? 3 : 2)), id: \.self) { tag in
                    tagChip(tagLabel(tag))
                }
            }
            .padding(.leading, Self.metaIndent)
            .lineLimit(1)
        }
    }

    /// One-line FTS snippet. The CLI wraps the matched span in « », which we
    /// render as an accent-colored, slightly heavier run; surrounding context
    /// stays secondary. Newlines collapse to spaces so it stays single-line.
    private func snippetLine(_ raw: String) -> some View {
        let flat = raw
            .replacingOccurrences(of: "\n", with: " ")
            .replacingOccurrences(of: "\r", with: " ")
        return Text(Self.highlightedSnippet(flat))
            .font(Self.metaFont)
            .lineLimit(1)
            .truncationMode(.tail)
            .padding(.leading, Self.metaIndent)
    }

    /// Build an AttributedString from a `«…»`-marked snippet: matched spans get
    /// the accent color + medium weight, everything else is secondary. The
    /// markers themselves are stripped.
    private static func highlightedSnippet(_ text: String) -> AttributedString {
        var result = AttributedString()
        var insideMatch = false
        var buffer = ""

        func flush() {
            guard !buffer.isEmpty else { return }
            var run = AttributedString(buffer)
            if insideMatch {
                run.foregroundColor = AppUI.accentPink
                run.font = Self.metaFont.weight(.semibold)
            } else {
                run.foregroundColor = .secondary
            }
            result.append(run)
            buffer = ""
        }

        for ch in text {
            if ch == "«" {
                flush(); insideMatch = true
            } else if ch == "»" {
                flush(); insideMatch = false
            } else {
                buffer.append(ch)
            }
        }
        flush()
        return result
    }

    private var dotSeparator: some View {
        Text("·")
            .font(Self.metaFont)
            .foregroundStyle(.tertiary)
    }

    /// Only surface a usage chip for statuses the user actively set — the
    /// default "Not Used" stays invisible so idle rows aren't noisy.
    private var meaningfulUsage: UsageStatusOption? {
        guard let o = usageOption, o.dbValue != "unused" else { return nil }
        return o
    }

    /// Filled, color-coded usage pill so an applied status is easy to spot.
    private func usageChip(_ o: UsageStatusOption) -> some View {
        HStack(spacing: 3) {
            Image(systemName: o.symbolName)
                .font(.system(size: 8.5, weight: .semibold))
            Text(o.title)
                .font(Self.metaFont)
                .lineLimit(1)
        }
        .foregroundStyle(o.color)
        .padding(.horizontal, 5)
        .padding(.vertical, 1.5)
        .background(o.color.opacity(0.16), in: Capsule())
        .help(o.title)
    }

    /// Plaud-Web-parity row date+time on one line:
    /// today → "11:58"; this year → "Jun 12, 11:58"; older → "Jun 12 '25".
    private static func formatRowDate(_ date: Date) -> String {
        let calendar = Calendar.current
        if calendar.isDateInToday(date) {
            return date.formatted(date: .omitted, time: .shortened)
        }
        if calendar.isDate(date, equalTo: Date(), toGranularity: .year) {
            let day = date.formatted(.dateTime.month(.abbreviated).day())
            let time = date.formatted(.dateTime.hour().minute())
            return "\(day), \(time)"
        }
        let day = date.formatted(.dateTime.month(.abbreviated).day())
        let yy = date.formatted(.dateTime.year(.twoDigits))
        return "\(day) '\(yy)"
    }

    private var folderLabel: String? {
        guard let first = file.folderNames.first else { return nil }
        let extraCount = file.folderNames.count - 1
        return extraCount > 0 ? "\(first) +\(extraCount)" : first
    }

    private var folderTint: Color {
        file.folderColor.flatMap { Color(hex: $0) } ?? .secondary
    }

    private var usageOption: UsageStatusOption? {
        UsageStatusOption.resolve(file.usageStatus)
    }

    private func folderChip(_ text: String) -> some View {
        HStack(spacing: 3) {
            Image(systemName: "folder.fill")
                .font(.system(size: 8.5))
            Text(text)
                .font(Self.metaFont)
                .lineLimit(1)
                .truncationMode(.tail)
        }
        .foregroundStyle(folderTint)
        .padding(.horizontal, 5)
        .padding(.vertical, 1.5)
        .background(folderTint.opacity(0.13), in: Capsule())
        .help(file.folderNames.joined(separator: ", "))
    }

    private func tagLabel(_ tag: String) -> String {
        let trimmed = tag.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.hasPrefix("#") ? trimmed : "#\(trimmed)"
    }

    private func tagChip(_ text: String) -> some View {
        Text(text)
            .font(Self.metaFont)
            .lineLimit(1)
            .truncationMode(.tail)
            .foregroundStyle(Color.secondary)
            .padding(.horizontal, 5)
            .padding(.vertical, 1.5)
            .background(Color.primary.opacity(0.07), in: Capsule())
    }
}

private struct UsageStatusOption: Identifiable {
    let dbValue: String
    let title: String
    let symbolName: String
    let color: Color

    var id: String { dbValue }

    // Hierarchical workflow palette — distinct hues that read as a progression:
    // idle(gray) → prepared(blue) → integrated(green) → reused(purple) →
    // closed(bronze). Designed so an applied status pops in the row chip and
    // the detail Status tile.
    static let all: [UsageStatusOption] = [
        UsageStatusOption(
            dbValue: "unused",
            title: "Not Used",
            symbolName: "circle.dotted",
            color: Color.secondary
        ),
        UsageStatusOption(
            dbValue: "metadata-ready",
            title: "Metadata Ready",
            symbolName: "wand.and.sparkles",
            color: Color(red: 0.36, green: 0.56, blue: 0.95)  // blue — prepared
        ),
        UsageStatusOption(
            dbValue: "vault-linked",
            title: "Vault Linked",
            symbolName: "link.circle.fill",
            color: Color(red: 0.24, green: 0.74, blue: 0.49)  // green — integrated
        ),
        UsageStatusOption(
            dbValue: "used-elsewhere",
            title: "Used Elsewhere",
            symbolName: "arrow.up.forward.app.fill",
            color: Color(red: 0.60, green: 0.45, blue: 0.90)  // purple — reused
        ),
        UsageStatusOption(
            dbValue: "archived",
            title: "Archived",
            symbolName: "archivebox.fill",
            color: Color(red: 0.66, green: 0.55, blue: 0.42)  // bronze — closed
        ),
    ]

    static func resolve(_ raw: String?) -> UsageStatusOption? {
        guard let raw = raw?.trimmingCharacters(in: .whitespacesAndNewlines),
              !raw.isEmpty else { return nil }
        if let option = all.first(where: { $0.dbValue == raw }) {
            return option
        }
        return UsageStatusOption(
            dbValue: raw,
            title: raw.replacingOccurrences(of: "-", with: " ").capitalized,
            symbolName: "questionmark.circle",
            color: .secondary
        )
    }
}

private enum AnuPalette {
    static let red = Color(red: 0.953, green: 0.545, blue: 0.659)
    static let teal = Color(red: 0.580, green: 0.886, blue: 0.835)
    static let sky = Color(red: 0.537, green: 0.863, blue: 0.922)
    static let flamingo = Color(red: 0.949, green: 0.804, blue: 0.804)
    static let rosewater = Color(red: 0.961, green: 0.878, blue: 0.863)
    static let peach = Color(red: 0.980, green: 0.702, blue: 0.529)
    static let yellow = Color(red: 0.976, green: 0.886, blue: 0.686)
    static let green = Color(red: 0.651, green: 0.890, blue: 0.631)
    static let lavender = Color(red: 0.706, green: 0.745, blue: 0.996)
    static let mauve = Color(red: 0.796, green: 0.651, blue: 0.969)

    static let speakerColors: [Color] = [
        sky, lavender, peach, flamingo, yellow, mauve, green, red,
    ]
}

private struct ViewModeSegment: View {
    @Binding var rawValue: String
    let width: CGFloat
    /// Shared across every raw pane — flipping it once applies everywhere,
    /// which is what you want when a wide markdown table is the reason you
    /// reached for the toggle.
    @AppStorage("rawWrapLines") private var wrapLines: Bool = true

    init(rawValue: Binding<String>, width: CGFloat = 142) {
        self._rawValue = rawValue
        self.width = width
    }

    var body: some View {
        HStack(spacing: 6) {
            HStack(spacing: 2) {
                ForEach(ContentViewMode.allCases) { mode in
                    segmentButton(mode.title, isSelected: rawValue == mode.rawValue) {
                        rawValue = mode.rawValue
                    }
                }
            }
            .padding(3)
            .background(AppUI.subtleFill, in: RoundedRectangle(cornerRadius: AppUI.radius))
            .frame(width: width)

            // Only meaningful in Raw mode — Rendered has its own layout rules.
            if rawValue == ContentViewMode.raw.rawValue {
                Button {
                    wrapLines.toggle()
                } label: {
                    Image(systemName: wrapLines
                          ? "text.alignleft"
                          : "arrow.left.and.right")
                        .font(.system(size: 11, weight: .semibold))
                        .foregroundStyle(wrapLines ? .secondary : AnuPalette.teal)
                }
                .buttonStyle(.plain)
                .help(wrapLines
                      ? "줄바꿈 켜짐 — 클릭하면 가로 스크롤 (표·코드용)"
                      : "가로 스크롤 켜짐 — 클릭하면 줄바꿈")
            }
        }
    }

    private func segmentButton(
        _ title: String,
        isSelected: Bool,
        action: @escaping () -> Void
    ) -> some View {
        Button(action: action) {
            Text(title)
                .font(AppUI.controlFont)
                .foregroundStyle(isSelected ? .primary : .secondary)
                .lineLimit(1)
                .frame(maxWidth: .infinity)
                .padding(.vertical, 4)
                .background(
                    isSelected ? AppUI.selectedFill : Color.clear,
                    in: RoundedRectangle(cornerRadius: AppUI.tightRadius)
                )
        }
        .buttonStyle(.plain)
    }
}

/// Monospaced raw view for transcripts / markdown source.
///
/// Raw mode is for reading structure — long timestamped lines, markdown
/// tables, code fences. Soft-wrapping those at the (narrow) middle-pane width
/// shreds the alignment that makes them readable, so lines keep their real
/// length and the pane scrolls sideways instead. `wrap` flips back to the old
/// behavior for prose-heavy transcripts.
private struct RawTextView: View {
    /// Bound to the shared `rawWrapLines` preference the Raw/Rendered
    /// control toggles. Wrapping is the default because most raw content
    /// here is prose; horizontal scroll is for tables, code, and long
    /// timestamped lines whose alignment matters.
    var wrapLines: Bool = true
    let text: String

    var body: some View {
        if wrapLines {
            Text(text)
                .font(.system(.body, design: .monospaced))
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
        } else {
            ScrollView(.horizontal, showsIndicators: true) {
                Text(text)
                    .font(.system(.body, design: .monospaced))
                    .textSelection(.enabled)
                    .fixedSize(horizontal: true, vertical: true)
                    .padding(.trailing, 12)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}

/// Consistent centered placeholder for loading / empty states, matching the
/// no-selection `ContentUnavailableView` style: a centered message with
/// padding and a subtle background, optionally fronted by a spinner.
private struct CenteredStateView: View {
    let message: String
    var loading: Bool = false
    var systemImage: String? = nil
    var tint: Color = .secondary

    var body: some View {
        HStack(spacing: AppUI.spacingS) {
            if loading {
                ProgressView().controlSize(.small)
            } else if let systemImage {
                Image(systemName: systemImage)
                    .symbolRenderingMode(.hierarchical)
                    .foregroundStyle(tint)
            }
            Text(message)
                .font(AppUI.bodyFont)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
        }
        .padding(AppUI.spacingL)
        .frame(maxWidth: .infinity)
        .background(AppUI.subtleFill, in: RoundedRectangle(cornerRadius: AppUI.radius))
        .padding(.vertical, AppUI.spacingM)
    }
}

private struct MarkdownDocumentView: View {
    let text: String

    private enum Kind {
        case heading(Int, String)
        case bullet(String)
        case quote(String)
        case paragraph(String)
        case code(String)
        case rule
        case blank
    }

    private struct Block: Identifiable {
        let id: Int
        let kind: Kind
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            ForEach(Self.blocks(from: text)) { block in
                blockView(block.kind)
            }
        }
        .textSelection(.enabled)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    @ViewBuilder
    private func blockView(_ kind: Kind) -> some View {
        switch kind {
        case .heading(let level, let body):
            Text(inline(body))
                .font(headingFont(level))
                .foregroundStyle(headingColor(level))
                .padding(.top, level <= 2 ? 8 : 4)
                .padding(.bottom, level == 2 ? 4 : 0)
            if level == 2 {
                Rectangle()
                    .fill(AnuPalette.teal.opacity(0.3))
                    .frame(height: 1)
                    .padding(.bottom, 4)
            }
        case .bullet(let body):
            HStack(alignment: .top, spacing: 8) {
                Text("•")
                    .font(.body.weight(.semibold))
                    .foregroundStyle(AnuPalette.teal)
                Text(inline(body))
                    .font(.body)
                    .foregroundStyle(.primary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        case .quote(let body):
            HStack(alignment: .top, spacing: 8) {
                Rectangle()
                    .fill(AnuPalette.rosewater.opacity(0.65))
                    .frame(width: 3)
                Text(inline(body))
                    .font(.body)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(.vertical, 2)
        case .paragraph(let body):
            Text(inline(body))
                .font(.body)
                .foregroundStyle(.primary)
                .fixedSize(horizontal: false, vertical: true)
        case .code(let body):
            Text(body)
                .font(.system(.body, design: .monospaced))
                .foregroundStyle(AnuPalette.rosewater)
                .padding(8)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Color(NSColor.controlBackgroundColor).opacity(0.75),
                            in: RoundedRectangle(cornerRadius: AppUI.tightRadius))
        case .rule:
            Divider()
                .padding(.vertical, 6)
        case .blank:
            Spacer().frame(height: 4)
        }
    }

    private func headingFont(_ level: Int) -> Font {
        switch level {
        case 1: return .title2.weight(.bold)
        case 2: return .title3.weight(.bold)
        case 3: return .headline.weight(.semibold)
        case 4: return .subheadline.weight(.semibold)
        default: return .body.weight(.semibold)
        }
    }

    private func headingColor(_ level: Int) -> Color {
        // Headings are large surfaces — keep them neutral and let the type
        // scale carry the hierarchy; pastel accents stay on small details
        // (bullets, the H2 underline, quote bars).
        switch level {
        case 1, 2, 3: return .primary
        default: return .secondary
        }
    }

    private func inline(_ raw: String) -> AttributedString {
        let cleaned = escapeSingleTildes(raw)
        return (try? AttributedString(
            markdown: cleaned,
            options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        )) ?? AttributedString(raw)
    }

    private func escapeSingleTildes(_ raw: String) -> String {
        var output = ""
        var index = raw.startIndex
        while index < raw.endIndex {
            if raw[index] == "~" {
                let next = raw.index(after: index)
                if next < raw.endIndex, raw[next] == "~" {
                    output.append("~~")
                    index = raw.index(after: next)
                } else {
                    output.append("\\~")
                    index = next
                }
            } else {
                output.append(raw[index])
                index = raw.index(after: index)
            }
        }
        return output
    }

    private static func blocks(from text: String) -> [Block] {
        var blocks: [Block] = []
        var codeLines: [String] = []
        var inCode = false

        func append(_ kind: Kind) {
            blocks.append(Block(id: blocks.count, kind: kind))
        }

        for rawLine in text.components(separatedBy: .newlines) {
            let trimmed = rawLine.trimmingCharacters(in: .whitespaces)
            if trimmed.hasPrefix("```") {
                if inCode {
                    append(.code(codeLines.joined(separator: "\n")))
                    codeLines.removeAll()
                }
                inCode.toggle()
                continue
            }
            if inCode {
                codeLines.append(rawLine)
                continue
            }
            if trimmed.isEmpty {
                append(.blank)
            } else if trimmed == "---" || trimmed == "***" || trimmed == "___" {
                append(.rule)
            } else if let heading = parseHeading(trimmed) {
                append(.heading(heading.level, heading.text))
            } else if trimmed.hasPrefix("- ") || trimmed.hasPrefix("* ") {
                append(.bullet(String(trimmed.dropFirst(2))))
            } else if trimmed.hasPrefix(">") {
                append(.quote(
                    String(trimmed.dropFirst()).trimmingCharacters(in: .whitespaces)
                ))
            } else {
                append(.paragraph(rawLine))
            }
        }
        if !codeLines.isEmpty {
            append(.code(codeLines.joined(separator: "\n")))
        }
        return blocks
    }

    private static func parseHeading(_ line: String) -> (level: Int, text: String)? {
        let hashes = line.prefix { $0 == "#" }.count
        guard hashes > 0, hashes <= 6,
              line.dropFirst(hashes).first == " " else { return nil }
        return (
            hashes,
            String(line.dropFirst(hashes + 1))
                .trimmingCharacters(in: .whitespaces)
        )
    }
}

private struct TranscriptBubbleList: View {
    let text: String
    /// Lowercased `is_self` speaker names, resolved once by `FileStore`.
    /// Previously each bubble ran its own `SELECT … FROM speakers` during
    /// layout — ~1,400 locked SQLite reads per render on a long recording.
    var selfNames: [String] = []

    private struct Message: Identifiable {
        let id: Int
        let timestamp: String
        let speaker: String
        var body: String
        /// Resolved at parse time so the render path stays pure layout.
        let isSelf: Bool
        let color: Color
    }

    /// Parsing 166KB of transcript is done once per distinct `text` and
    /// cached, because SwiftUI re-evaluates `body` on every ancestor publish
    /// (hover, sync tick, search keystroke) and the old code re-split and
    /// re-parsed the whole string each time.
    private var messages: [Message] {
        Self.cache.messages(for: text, selfNames: selfNames)
    }

    var body: some View {
        // Lazy: only the bubbles near the viewport get built and laid out.
        // A plain VStack materialized all ~1,400 of them up front, which is
        // what made opening a long recording feel like a stall.
        LazyVStack(alignment: .leading, spacing: 9) {
            ForEach(messages) { message in
                bubble(message)
            }
        }
        .textSelection(.enabled)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// Single-entry memo — the detail pane shows one transcript at a time, so
    /// a one-slot cache keyed by (text identity, self-names) hits ~100%.
    private final class MessageCache: @unchecked Sendable {
        private let lock = NSLock()
        private var key: (text: String, names: [String])?
        private var value: [Message] = []

        func messages(for text: String, selfNames: [String]) -> [Message] {
            lock.lock(); defer { lock.unlock() }
            if let key, key.text == text, key.names == selfNames { return value }
            let parsed = TranscriptBubbleList.parse(text, selfNames: selfNames)
            key = (text, selfNames)
            value = parsed
            return parsed
        }
    }

    private static let cache = MessageCache()

    private func bubble(_ message: Message) -> some View {
        let selfSpeaker = message.isSelf
        let color = message.color
        return HStack(alignment: .bottom) {
            if selfSpeaker { Spacer(minLength: 44) }
            VStack(alignment: selfSpeaker ? .trailing : .leading, spacing: 4) {
                HStack(spacing: 6) {
                    if !selfSpeaker {
                        Circle()
                            .fill(color)
                            .frame(width: 7, height: 7)
                    }
                    Text(displayName(message.speaker, isSelf: selfSpeaker))
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(color)
                    Text(message.timestamp)
                        .font(.caption2.monospacedDigit())
                        .foregroundStyle(.tertiary)
                }
                Text(message.body)
                    .font(.body)
                    .foregroundStyle(.primary)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.horizontal, 12)
                    .padding(.vertical, 8)
                    .background(
                        color.opacity(selfSpeaker ? 0.22 : 0.18),
                        in: RoundedRectangle(cornerRadius: 16)
                    )
                    .overlay(
                        RoundedRectangle(cornerRadius: 16)
                            .stroke(color.opacity(0.38), lineWidth: 1)
                    )
            }
            .frame(maxWidth: 620, alignment: selfSpeaker ? .trailing : .leading)
            if !selfSpeaker { Spacer(minLength: 44) }
        }
        .frame(maxWidth: .infinity)
    }

    /// Parse the flat `[mm:ss] speaker: body` transcript into bubbles,
    /// resolving self-flag and color per *distinct speaker* (memoized in
    /// `styles`) rather than per line.
    private static func parse(_ text: String, selfNames: [String]) -> [Message] {
        var out: [Message] = []
        var styles: [String: (isSelf: Bool, color: Color)] = [:]

        // Palette slots already taken by a speaker in THIS transcript, so two
        // people in the same conversation never share a color.
        var usedSlots = Set<Int>()

        func style(_ speaker: String) -> (isSelf: Bool, color: Color) {
            if let cached = styles[speaker] { return cached }
            let isSelf = isSelfSpeaker(speaker, selfNames: selfNames)
            let color: Color
            if isSelf {
                color = AnuPalette.teal
            } else {
                let slot = claimSlot(for: speaker, used: &usedSlots)
                color = AnuPalette.speakerColors[slot]
            }
            let resolved = (isSelf, color)
            styles[speaker] = resolved
            return resolved
        }

        func append(timestamp: String, speaker: String, body: String) {
            let s = style(speaker)
            out.append(Message(id: out.count, timestamp: timestamp, speaker: speaker,
                               body: body, isSelf: s.isSelf, color: s.color))
        }

        for line in text.components(separatedBy: .newlines) {
            guard !line.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            else { continue }
            if let parsed = parseLine(line) {
                if var last = out.last,
                   last.speaker == parsed.speaker,
                   last.timestamp == parsed.timestamp {
                    last.body += "\n" + parsed.body
                    out[out.count - 1] = last
                } else {
                    append(timestamp: parsed.timestamp,
                           speaker: parsed.speaker,
                           body: parsed.body)
                }
            } else if var last = out.last {
                last.body += "\n" + line
                out[out.count - 1] = last
            } else {
                append(timestamp: "", speaker: "Unknown", body: line)
            }
        }
        return out
    }

    private static func parseLine(_ line: String)
        -> (timestamp: String, speaker: String, body: String)? {
        guard line.first == "[",
              let close = line.firstIndex(of: "]") else { return nil }
        let timestamp = String(line[line.index(after: line.startIndex)..<close])
        let rest = line[line.index(after: close)...]
            .trimmingCharacters(in: .whitespaces)
        guard let colon = rest.firstIndex(of: ":") else {
            return (timestamp, "Unknown", rest)
        }
        let speaker = String(rest[..<colon])
            .trimmingCharacters(in: .whitespacesAndNewlines)
        let body = String(rest[rest.index(after: colon)...])
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return (timestamp, speaker.isEmpty ? "Unknown" : speaker, body)
    }

    private func displayName(_ speaker: String, isSelf: Bool) -> String {
        if isSelf { return speaker.isEmpty ? "Me" : "Me · \(speaker)" }
        return speaker.isEmpty ? "Unknown" : speaker
    }

    /// No configured self-speaker → no self-styling. Match the transcript's
    /// speaker label against any saved self speaker (case-insensitive,
    /// substring either direction to tolerate "Me · Name" style labels).
    /// `selfNames` arrives already trimmed + lowercased from `FileStore`.
    private static func isSelfSpeaker(_ speaker: String, selfNames: [String]) -> Bool {
        let lower = speaker.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard !lower.isEmpty else { return false }
        return selfNames.contains { name in
            lower == name || lower.contains(name) || name.contains(lower)
        }
    }

    /// Pick a palette slot for `speaker`: their stable preferred slot when it
    /// is free, otherwise the next free one.
    ///
    /// Stability matters (the same person keeps their color across
    /// recordings), but *distinctness within one conversation* matters more —
    /// the whole point of the color is telling two speakers apart on screen.
    /// Probing gives both: collisions only shift the later arrival.
    private static func claimSlot(for speaker: String, used: inout Set<Int>) -> Int {
        let count = AnuPalette.speakerColors.count
        let preferred = stableHash(speaker) % count
        if used.count >= count {
            // More speakers than colors — reuse is unavoidable; stay stable.
            return preferred
        }
        var slot = preferred
        while used.contains(slot) { slot = (slot + 1) % count }
        used.insert(slot)
        return slot
    }

    /// FNV-1a over the UTF-8 bytes.
    ///
    /// The previous hash summed code points, which collides constantly for
    /// Korean names: 전창대 and 안창현 both summed to a multiple-of-8 offset
    /// and landed on the same slot, so every non-self speaker looked alike.
    /// FNV-1a mixes each byte into the whole word, so single-character
    /// differences move the result.
    private static func stableHash(_ text: String) -> Int {
        var hash: UInt64 = 0xcbf2_9ce4_8422_2325
        for byte in text.utf8 {
            hash ^= UInt64(byte)
            hash = hash &* 0x1000_0000_01b3
        }
        return Int(hash % UInt64(Int.max))
    }
}

// MARK: - Detail

private enum SourceTab: String, CaseIterable, Identifiable {
    case plaud = "Plaud"
    case cmds = "CMDS"
    case final = "Final"
    var id: String { rawValue }

    var title: String {
        if DistributionProfile.isCommunity, self == .cmds { return "ElevenLabs" }
        return rawValue
    }
}

private struct SidebarTogglePill: View {
    let title: String
    let systemName: String
    var active: Bool = false
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 7) {
                Image(systemName: systemName)
                    .font(.system(size: 15, weight: .semibold))
                    .symbolRenderingMode(.hierarchical)
                    .frame(width: 18, height: 18)
                Text(title)
                    .font(AppUI.controlFont)
            }
            .foregroundStyle(active ? .white : .primary)
            .padding(.horizontal, 10)
            .padding(.vertical, 7)
            .background(
                active ? AnuPalette.red : AppUI.subtleFill,
                in: RoundedRectangle(cornerRadius: AppUI.radius)
            )
            .overlay(
                RoundedRectangle(cornerRadius: AppUI.radius)
                    .stroke(Color(NSColor.separatorColor).opacity(active ? 0 : 0.45),
                            lineWidth: 1)
            )
        }
        .buttonStyle(.plain)
    }
}

/// Derived pipeline stage for the Progress tile. Stage = highest reached:
/// Integrated (any integrated .md on disk) > Transcribed (cmds_transcripts
/// row) > Cached (file_content row) > New. Computed for the *selected* file
/// only — one directory scan + one EXISTS query — never per list row.
enum PipelineStage {
    case integrated, transcribed, cached, new

    /// Called from `FileStore.loadContent` on a background task — it touches
    /// the filesystem (`data/integrated/<id>/`) and SQLite, neither of which
    /// belongs in a view body.
    static func derive(fileID: String, hasContent: Bool) -> PipelineStage {
        if Database.shared.integratedAnyExists(fileID: fileID) { return .integrated }
        if Database.shared.cmdsTranscriptExists(for: fileID) { return .transcribed }
        if hasContent { return .cached }
        return .new
    }

    var title: String {
        switch self {
        case .integrated: return "Integrated"
        case .transcribed: return "Transcribed"
        case .cached: return "Cached"
        case .new: return "New"
        }
    }

    var symbolName: String {
        switch self {
        case .integrated: return "checkmark.seal"
        case .transcribed: return "waveform"
        case .cached: return "internaldrive"
        case .new: return "circle.dashed"
        }
    }

    var color: Color {
        switch self {
        case .integrated: return AnuPalette.green
        case .transcribed: return AnuPalette.teal
        case .cached: return AnuPalette.sky
        case .new: return Color.secondary
        }
    }
}

private struct DetailMetricStrip: View {
    let file: PlaudFileVM
    let stage: PipelineStage

    var body: some View {
        HStack(spacing: 7) {
            MiniMetricCard(
                label: "Recorded",
                value: formatRecordingStart(file.createdAt),
                systemName: "calendar",
                color: AnuPalette.flamingo
            )
            .help(recordedHelp)
            MiniMetricCard(
                label: "Duration",
                value: formatRecordingDurationFull(file.durationMs),
                systemName: "timer",
                color: AnuPalette.sky
            )
            MiniMetricCard(
                label: "Folder",
                value: folderValue,
                systemName: "folder.fill",
                color: file.folderColor.flatMap { Color(hex: $0) } ?? AppUI.brandGreen
            )
            MiniMetricCard(
                label: "Progress",
                value: stage.title,
                systemName: stage.symbolName,
                color: stage.color
            )
            MiniMetricCard(
                label: "Status",
                value: statusValue,
                systemName: statusSymbol,
                color: statusColor,
                // Tint the tile once the user sets a real status (not the
                // default unused / not-set) so it's unmistakable.
                tinted: statusIsMeaningful
            )
        }
    }

    private var statusIsMeaningful: Bool {
        guard let o = statusOption else { return false }
        return o.dbValue != "unused"
    }

    private var recordedHelp: String {
        formatRecordingStartLong(file.createdAt) ?? "Recording start time unknown"
    }

    private var folderValue: String {
        guard let first = file.folderNames.first else { return "Unfiled" }
        let extra = file.folderNames.count - 1
        return extra > 0 ? "\(first) +\(extra)" : first
    }

    private var statusOption: UsageStatusOption? {
        UsageStatusOption.resolve(file.usageStatus)
    }

    private var statusValue: String {
        statusOption?.title ?? "Not Set"
    }

    private var statusSymbol: String {
        statusOption?.symbolName ?? "circle"
    }

    private var statusColor: Color {
        statusOption?.color ?? .secondary
    }
}

private struct DetailView: View {
    @ObservedObject var store: FileStore
    @State private var player: AVPlayer?
    /// KVO handle on the current player item's `status`. Held so we can surface
    /// a friendly error when streaming fails (e.g. an expired signed URL).
    @State private var statusObserver: NSKeyValueObservation?
    @State private var sourceTab: SourceTab = .plaud
    @AppStorage("showRightWorkSidebar") private var showRightWorkSidebar: Bool =
        !DistributionProfile.isCommunity
    /// Persisted Work Sidebar width. HSplitView never saves its divider, so
    /// without this the sidebar snaps back to its ideal width every launch.
    /// Default 430 keeps the transcript readable beside the source pane —
    /// slot cards fully readable without crowding the detail pane.
    @AppStorage("workSidebarWidth") private var workSidebarWidth: Double = 430
    /// Inline title editing (detail header). Click the title (or the hover
    /// pencil) to edit; Enter commits, Esc/blur cancels.
    @State private var editingTitle = false
    @State private var titleDraft = ""
    @State private var titleHovered = false
    @FocusState private var titleFieldFocused: Bool

    var body: some View {
        if let file = store.selectedFile {
            Group {
                if !DistributionProfile.isCommunity, showRightWorkSidebar {
                    HSplitView {
                        mainContent(file: file)
                            .frame(minWidth: 420)
                            .frame(
                                maxWidth: .infinity,
                                maxHeight: .infinity,
                                alignment: .topLeading
                            )

                        AIInspectorPanel(store: store) {
                            showRightWorkSidebar = false
                        }
                        // Min keeps slot cards from squishing; ideal restores
                        // the user's last dragged width (default = minimized).
                        .frame(
                            minWidth: 360,
                            idealWidth: max(360, min(520, workSidebarWidth)),
                            maxWidth: 520
                        )
                        .background(
                            GeometryReader { proxy in
                                Color.clear.onChange(of: proxy.size.width) { _, width in
                                    workSidebarWidth = Double(width)
                                }
                            }
                        )
                    }
                } else {
                    mainContent(file: file)
                        .frame(
                            maxWidth: .infinity,
                            maxHeight: .infinity,
                            alignment: .topLeading
                        )
                }
            }
            .toolbar { toolbar(file: file) }
            .onChange(of: store.selectedID) { _, _ in
                statusObserver = nil
                player?.pause()
                player = nil
                store.audioURL = nil
                editingTitle = false
            }
            .onChange(of: store.audioURL) { _, url in
                statusObserver = nil
                guard let url else { player = nil; return }
                let item = AVPlayerItem(url: url)
                statusObserver = item.observe(\.status) { item, _ in
                    if item.status == .failed {
                        Task { @MainActor in
                            store.lastCommandError =
                                "Could not stream audio (link may have expired — click Reload)."
                        }
                    }
                }
                let newPlayer = AVPlayer(playerItem: item)
                player = newPlayer
                newPlayer.play()
            }
        } else {
            // Nicer empty state for the detail pane – matches the modern dark/glass aesthetic
            VStack(spacing: 16) {
                Image(systemName: "waveform")
                    .font(.system(size: 48, weight: .light))
                    .foregroundStyle(.secondary)
                    .symbolRenderingMode(.hierarchical)

                VStack(spacing: 6) {
                    Text("No file selected")
                        .font(.title3.weight(.semibold))
                        .foregroundStyle(.primary)

                    Text(
                        DistributionProfile.isCommunity
                            ? "Pick a recording to see its transcript, Plaud summary, and local organization."
                            : "Pick a recording from the list to see details,\ntranscripts, and AI summaries."
                    )
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                }
            }
            .frame(maxWidth: 320)
            .padding(40)
            .background(
                RoundedRectangle(cornerRadius: 16)
                    .fill(Color(NSColor.controlBackgroundColor).opacity(0.6))
            )
        }
    }

    private func mainContent(file: PlaudFileVM) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            header(file: file)
            audioBar(file: file)
            if !availableSourceTabs.isEmpty {
                sourceSwitcher
                    .padding(.horizontal, 12)
                    .padding(.bottom, 6)
                Divider()
            }

            switch sourceTab {
            case .plaud: PlaudPanel(store: store)
            case .cmds: CmdsPanel(store: store)
            case .final: FinalPanel(store: store)
            }
        }
    }

    private var availableSourceTabs: [SourceTab] {
        DistributionProfile.isCommunity ? [.plaud, .cmds] : SourceTab.allCases
    }

    private var sourceSwitcher: some View {
        HStack(spacing: 8) {
            Text("Source")
                .font(AppUI.sectionFont)
                .foregroundStyle(.secondary)
            HStack(spacing: 2) {
                ForEach(availableSourceTabs) { source in
                    Button {
                        sourceTab = source
                    } label: {
                        Text(source.title)
                            .font(AppUI.controlFont)
                            .foregroundStyle(sourceTab == source ? .primary : .secondary)
                            .frame(width: 78)
                            .padding(.vertical, 6)
                            .background(
                                sourceTab == source
                                    ? AppUI.selectedFill
                                    : Color.clear,
                                in: RoundedRectangle(cornerRadius: AppUI.tightRadius)
                            )
                    }
                    .buttonStyle(.plain)
                }
            }
            .padding(4)
            // Liquid Glass segmented for 2026
            .background(
                RoundedRectangle(cornerRadius: AppUI.glassRadius)
                    .fill(.regularMaterial)
            )
            // Liquid Glass style via material + stroke (glassEffect for macOS 26+)
            .background(
                RoundedRectangle(cornerRadius: AppUI.glassRadius)
                    .fill(.regularMaterial)
            )
            .overlay(
                RoundedRectangle(cornerRadius: AppUI.glassRadius)
                    .stroke(AppUI.glassStroke, lineWidth: 0.5)
            )
            Spacer(minLength: 0)
        }
    }

    @ViewBuilder
    private func header(file: PlaudFileVM) -> some View {
        // Prefer the *live* filename from the metadata sync over the cached
        // file_content.title — Plaud renames files post-transcription and the
        // cache lags behind. We also let the user click the title to force
        // a detail re-fetch if they want fresh keywords.
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .top, spacing: 12) {
                VStack(alignment: .leading, spacing: 5) {
                    Text("Recording Workspace")
                        .font(.system(size: 10, weight: .heavy))
                        .foregroundStyle(AppUI.brandGreen)
                    HStack(spacing: 8) {
                        if editingTitle {
                            TextField("Recording title", text: $titleDraft)
                                .textFieldStyle(.plain)
                                .font(.system(size: 21, weight: .bold))
                                .focused($titleFieldFocused)
                                .onSubmit { commitTitleEdit(file: file) }
                                .onExitCommand { cancelTitleEdit() }
                                .onChange(of: titleFieldFocused) { _, focused in
                                    // Losing focus without Enter cancels.
                                    if !focused && editingTitle { cancelTitleEdit() }
                                }
                                .onAppear { titleFieldFocused = true }
                                .frame(maxWidth: 480)
                        } else {
                            Text(file.filename ?? store.content?.title ?? "(untitled)")
                                .font(.system(size: 21, weight: .bold))
                                .lineLimit(2)
                                .onTapGesture { beginTitleEdit(file: file) }
                                .help("Click to rename")
                            Button {
                                beginTitleEdit(file: file)
                            } label: {
                                Image(systemName: "pencil")
                                    .font(.system(size: 12, weight: .semibold))
                                    .foregroundStyle(.tertiary)
                            }
                            .buttonStyle(.plain)
                            .opacity(titleHovered ? 1 : 0)
                            .help("Rename recording")
                        }
                        // Grouped secondary actions for cleaner header
                        HStack(spacing: 2) {
                            Button {
                                if let id = store.selectedID {
                                    Task { await store.refetchDetail(id) }
                                }
                            } label: {
                                Image(systemName: "arrow.clockwise.circle")
                                    .font(.system(size: 13, weight: .semibold))
                                    .foregroundStyle(.tertiary)
                            }
                            .buttonStyle(.plain)
                            .help("Re-fetch detail (transcript + summary) from Plaud")

                            Button {
                                store.openInPlaudWeb(file.id)
                            } label: {
                                Image(systemName: "safari")
                                    .font(.system(size: 13, weight: .semibold))
                                    .foregroundStyle(.tertiary)
                            }
                            .buttonStyle(.plain)
                            .help("Open in Plaud Web (web.plaud.ai/file/\(file.id))")
                        }
                    }
                    .onHover { titleHovered = $0 }
                    // Recording start time — the most important context for a
                    // session, shown prominently right under the title.
                    if let recorded = formatRecordingStartLong(file.createdAt) {
                        HStack(spacing: 5) {
                            Image(systemName: "calendar.badge.clock")
                                .font(.system(size: 11, weight: .semibold))
                                .foregroundStyle(AnuPalette.flamingo)
                            Text(recorded)
                                .font(.system(size: 12, weight: .semibold))
                                .foregroundStyle(.secondary)
                                .textSelection(.enabled)
                        }
                        .help("녹음 시작 시각")
                    }
                    HStack(spacing: 8) {
                        Text(file.id).font(.caption2).foregroundStyle(.tertiary)
                            .textSelection(.enabled)
                        if let kw = store.content?.keywords, !kw.isEmpty {
                            HStack(spacing: 4) {
                                ForEach(Array(kw.prefix(5)), id: \.self) { keyword in
                                    keywordChip(keyword)
                                }
                            }
                        }
                    }
                }
                Spacer(minLength: 8)
                if !DistributionProfile.isCommunity, !showRightWorkSidebar {
                    SidebarTogglePill(
                        title: "Work Sidebar",
                        systemName: "sidebar.trailing"
                    ) {
                        showRightWorkSidebar = true
                    }
                    .help("Show AI summaries and Obsidian actions")
                }
            }
            // Stage comes from the store, refreshed off-main whenever the
            // selection or the library changes (transcription / integration
            // finishing both land through `reload()` -> `loadContent`).
            DetailMetricStrip(file: file, stage: store.selectedStage)
            MetadataBar(store: store, file: file)
        }
        .padding(14)
        // Modern floating header: glass material with subtle brand tint (Liquid Glass 2026 style)
        .background(
            RoundedRectangle(cornerRadius: AppUI.glassRadius)
                .fill(.regularMaterial)
        )
        .overlay(
            RoundedRectangle(cornerRadius: AppUI.glassRadius)
                .stroke(AppUI.glassStroke, lineWidth: 0.5)
        )
    }


    /// Clickable keyword chip: clicking filters the file list by routing the
    /// keyword through the same search field a user would type into.
    private func keywordChip(_ keyword: String) -> some View {
        Button {
            store.search = keyword
        } label: {
            Text(keyword)
                .font(AppUI.metaFont)
                .foregroundStyle(.secondary)
                .lineLimit(1)
                .padding(.horizontal, 7)
                .padding(.vertical, 2.5)
                .background(Color.primary.opacity(0.07), in: Capsule())
        }
        .buttonStyle(.plain)
        .help("이 키워드로 검색")
    }

    // MARK: Inline title editing

    private func beginTitleEdit(file: PlaudFileVM) {
        titleDraft = file.filename ?? store.content?.title ?? ""
        editingTitle = true
    }

    private func commitTitleEdit(file: PlaudFileVM) {
        guard editingTitle else { return }
        editingTitle = false
        let trimmed = titleDraft.trimmingCharacters(in: .whitespacesAndNewlines)
        let current = file.filename ?? store.content?.title ?? ""
        guard !trimmed.isEmpty, trimmed != current else { return }
        // Optimistic local rename + background `plaud rename`.
        store.renameFile(file.id, to: trimmed)
    }

    private func cancelTitleEdit() {
        editingTitle = false
    }

    @ViewBuilder
    private func audioBar(file: PlaudFileVM) -> some View {
        HStack(spacing: 8) {
            Button {
                Task { await store.loadAudioURL(file.id) }
            } label: {
                Label(player == nil ? "Load audio" : "Reload",
                      systemImage: "waveform.circle")
            }
            if let player {
                AudioPlayerView(player: player)
                    .frame(height: 50)
                    .frame(maxWidth: .infinity)
            } else {
                Text("Audio not loaded — click to stream from Plaud.")
                    .font(.caption).foregroundStyle(.secondary)
                Spacer()
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 6)
        .font(AppUI.bodyFont)
    }

    @ToolbarContentBuilder
    private func toolbar(file: PlaudFileVM) -> some ToolbarContent {
        if !DistributionProfile.isCommunity {
            ToolbarItem {
                VaultSendMenu(store: store, fileID: file.id, compact: true)
                    .menuStyle(.borderlessButton)
                    .menuIndicator(.hidden)
                    .help("Send to Obsidian vault")
            }
        }
        ToolbarItem {
            Menu {
                Button("Export audio") { Task { await store.download(file.id) } }
                Button("Export transcript") {
                    Task { await store.exportContent(file.id, kind: "transcript") }
                }
                Button("Export notes") {
                    Task { await store.exportContent(file.id, kind: "notes") }
                }
                Button("Export outline") {
                    Task { await store.exportContent(file.id, kind: "outline") }
                }
            } label: {
                ToolbarIconLabel(systemName: "ellipsis")
            }
            .menuStyle(.borderlessButton)
            .help("More")
        }
    }
}

/// Speaker real-name confirmation — the dual pipeline's one required human
/// checkpoint. Shows the LLM proposal (name + confidence + evidence) with
/// editable fields; Apply resumes the pipeline with the confirmed map.
private struct DualSpeakerSheet: View {
    @ObservedObject var store: FileStore
    let fileID: String
    let proposal: [SpeakerProposalVM]
    let onDone: () -> Void

    @State private var names: [String: String] = [:]

    var body: some View {
        VStack(alignment: .leading, spacing: AppUI.spacingM) {
            HStack(spacing: 8) {
                Image(systemName: "person.wave.2")
                    .font(.system(size: 18, weight: .semibold))
                    .foregroundStyle(AppUI.accentPink)
                VStack(alignment: .leading, spacing: 2) {
                    Text("화자 실명 확인")
                        .font(.title3.weight(.semibold))
                    Text("제안된 이름을 수정하거나 비워두세요 (빈 칸은 speaker 번호 유지). 확정한 이름은 다음 녹음의 제안 정확도를 높입니다.")
                        .font(AppUI.metaFont)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }

            VStack(spacing: 8) {
                ForEach(proposal) { p in
                    HStack(spacing: AppUI.spacingS) {
                        Text(p.speaker)
                            .font(.system(size: 12, design: .monospaced))
                            .foregroundStyle(.secondary)
                            .frame(width: 92, alignment: .leading)
                        TextField("이름", text: binding(for: p))
                            .textFieldStyle(.roundedBorder)
                            .frame(width: 140)
                        if p.confidence > 0, !p.name.isEmpty {
                            Text("\(Int(p.confidence * 100))%")
                                .font(.system(size: 10.5, weight: .semibold))
                                .foregroundStyle(p.confidence >= 0.7 ? Color.green : .orange)
                        }
                        Text(p.evidence)
                            .font(AppUI.metaFont)
                            .foregroundStyle(.tertiary)
                            .lineLimit(1)
                        Spacer(minLength: 0)
                    }
                }
            }

            HStack {
                Button("Cancel") { onDone() }
                Spacer()
                Button {
                    let map = names.filter {
                        !$0.value.trimmingCharacters(in: .whitespaces).isEmpty
                    }
                    onDone()
                    Task { await store.runDual(fileID, speakerMap: map) }
                } label: {
                    Label("Apply & Continue", systemImage: "bolt.badge.waveform")
                }
                .buttonStyle(.borderedProminent)
                .disabled(store.dualRunningIDs.contains(fileID))
            }
        }
        .padding(20)
        .frame(width: 560)
        .onAppear {
            for p in proposal where names[p.speaker] == nil {
                names[p.speaker] = p.name
            }
        }
    }

    private func binding(for p: SpeakerProposalVM) -> Binding<String> {
        Binding(
            get: { names[p.speaker] ?? p.name },
            set: { names[p.speaker] = $0 }
        )
    }
}

private struct MetadataBar: View {
    @ObservedObject var store: FileStore
    let file: PlaudFileVM
    @State private var newTag: String = ""
    @State private var showStatusPicker: Bool = false
    @State private var showSpeakerSheet: Bool = false

    private var metadata: NoteMetadataVM? {
        guard store.noteMetadata?.fileID == file.id else { return nil }
        return store.noteMetadata
    }

    private var tags: [NoteTagVM] {
        let all = metadata?.tags ?? []
        return DistributionProfile.isCommunity
            ? all.filter { $0.source == "manual" }
            : all
    }
    private var isGenerating: Bool { store.metadataGeneratingIDs.contains(file.id) }
    private var isWritingMeeting: Bool { store.meetingNoteGeneratingIDs.contains(file.id) }
    /// Provider + configured model id for meeting-note generation. Metadata
    /// generation deliberately passes no model — the CLI resolves the
    /// configured classify model (`plaud config-classify`) itself.
    private var aiModelChoice: (provider: String, modelID: String) {
        defaultAIModelChoice()
    }
    private var usageOption: UsageStatusOption? {
        UsageStatusOption.resolve(metadata?.usageStatus)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            if !tags.isEmpty {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 6) {
                        ForEach(tags) { tag in
                            tagChip(tag)
                        }
                    }
                    .padding(.vertical, 1)
                }
            }

            // Plaud-synced data: note type (read-only) + folder. The folder
            // is an interactive radio menu — picking a folder replaces the
            // single assignment, picking the current one (or Unfiled) clears.
            HStack(spacing: AppUI.spacingS) {
                if !DistributionProfile.isCommunity,
                   let type = metadata?.noteType, !type.isEmpty {
                    Label(type, systemImage: "doc.text")
                        .font(AppUI.metaFont)
                        .foregroundStyle(.tertiary)
                }
                folderMenu
                Spacer(minLength: 0)
            }

            // Locally-owned editable controls: usage status and manual tags.
            // Private-edition AI/Obsidian actions remain in a separate gated
            // branch below so Community never exposes them.
            HStack(spacing: AppUI.spacingS) {
                if let usageOption {
                    statusLabel(usageOption)
                }

                Button {
                    showStatusPicker.toggle()
                } label: {
                    Image(systemName: "checklist")
                        .font(.system(size: 13, weight: .semibold))
                }
                .buttonStyle(.plain)
                .help("Set usage status")
                .popover(isPresented: $showStatusPicker, arrowEdge: .bottom) {
                    statusPicker
                }

                HStack(spacing: 2) {
                    TextField("tag", text: $newTag)
                        .textFieldStyle(.roundedBorder)
                        .font(AppUI.metaFont)
                        .frame(width: 116)
                        .onSubmit { addTag() }
                    // Autocomplete: a menu of matching existing tags. Non-
                    // intrusive (doesn't steal focus while typing); appears
                    // only when the input matches known tags.
                    if !tagSuggestions.isEmpty {
                        Menu {
                            ForEach(tagSuggestions, id: \.self) { suggestion in
                                Button("#\(suggestion)") {
                                    newTag = ""
                                    Task { await store.addTag(suggestion, to: file.id) }
                                }
                            }
                        } label: {
                            Image(systemName: "chevron.down.circle")
                                .font(.system(size: 11))
                        }
                        .menuStyle(.borderlessButton)
                        .menuIndicator(.hidden)
                        .frame(width: 16)
                        .help("Matching tags")
                    }
                }

                Button { addTag() } label: {
                    Image(systemName: "plus.circle.fill")
                }
                .buttonStyle(.plain)
                .disabled(newTag.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                .help("Add local tag")

                if !DistributionProfile.isCommunity {
                    Button {
                        // No explicit model: the CLI resolves the configured
                        // classify model (Settings > Auto-classify).
                        Task { await store.generateMetadata(file.id) }
                    } label: {
                        if isGenerating {
                            ProgressView().controlSize(.small)
                        } else {
                            Image(systemName: "sparkles")
                        }
                    }
                    .buttonStyle(.plain)
                    .disabled(isGenerating)
                    .help("Generate metadata and auto tags")

                    Button {
                        let choice = aiModelChoice
                        Task {
                            await store.writeMeetingNote(
                                file.id, model: choice.provider, modelID: choice.modelID
                            )
                        }
                    } label: {
                        if isWritingMeeting {
                            ProgressView().controlSize(.small)
                        } else {
                            Image(systemName: "doc.badge.plus")
                        }
                    }
                    .buttonStyle(.plain)
                    .disabled(isWritingMeeting)
                    .help("Write CMDS meeting note in Obsidian")

                    if let path = metadata?.finalNotePath, !path.isEmpty {
                        Button {
                            store.openPath(path)
                        } label: {
                            Image(systemName: "arrow.up.forward.app")
                        }
                        .buttonStyle(.plain)
                        .help("Open generated Obsidian note")
                    }
                }

                Spacer(minLength: 0)
            }

            if !DistributionProfile.isCommunity {
                dualReuseRow
            }

            if !DistributionProfile.isCommunity,
               let description = metadata?.description, !description.isEmpty {
                Text(description)
                    .font(AppUI.metaFont)
                    .foregroundStyle(.tertiary)
                    .lineLimit(2)
            }
        }
        .sheet(isPresented: $showSpeakerSheet) {
            if let dual = dualForFile {
                DualSpeakerSheet(store: store, fileID: file.id, proposal: dual.proposal) {
                    showSpeakerSheet = false
                }
            }
        }
        .onChange(of: file.id) { _, _ in newTag = "" }
        .onChange(of: store.dualState?.status) { _, status in
            // Auto-open the confirmation sheet the moment names are ready.
            if !DistributionProfile.isCommunity,
               status == "relabel-pending", store.selectedID == file.id {
                showSpeakerSheet = true
            }
        }
    }

    /// Name of the file's single assigned folder, preferring the live folder
    /// sync over the (lagging) note_metadata copy.
    private var currentFolderName: String {
        if let name = file.folderNames.first, !name.isEmpty { return name }
        if let name = metadata?.folderName, !name.isEmpty { return name }
        return "Unfiled"
    }

    private var folderMenu: some View {
        Menu {
            FolderRadioMenuItems(store: store, fileID: file.id)
        } label: {
            Label(currentFolderName, systemImage: "folder")
                .font(AppUI.metaFont)
        }
        .menuStyle(.borderlessButton)
        .fixedSize()
        .foregroundStyle(.secondary)
        .help("Assign Plaud folder (one folder per file)")
    }

    /// Up to 5 existing tags matching the current input (case-insensitive
    /// substring), excluding tags already attached to this file.
    private var tagSuggestions: [String] {
        let needle = newTag.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard needle.count >= 1 else { return [] }
        let attached = Set(tags.map { $0.tag.lowercased() })
        return store.tagCounts
            .map { $0.tag }
            .filter { tag in
                let lower = tag.lowercased()
                return lower.contains(needle) && lower != needle && !attached.contains(lower)
            }
            .prefix(5)
            .map { $0 }
    }

    private func addTag() {
        let tag = newTag.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !tag.isEmpty else { return }
        newTag = ""
        Task { await store.addTag(tag, to: file.id) }
    }

    private func statusLabel(_ option: UsageStatusOption) -> some View {
        HStack(spacing: 4) {
            Image(systemName: option.symbolName)
                .font(.system(size: 11, weight: .semibold))
                .symbolRenderingMode(.hierarchical)
            Text(option.title)
                .font(AppUI.metaFont)
        }
        .foregroundStyle(option.color)
    }

    private var statusPicker: some View {
        VStack(alignment: .leading, spacing: 4) {
            ForEach(UsageStatusOption.all) { option in
                Button {
                    showStatusPicker = false
                    Task { await store.setUsageStatus(file.id, status: option.dbValue) }
                } label: {
                    HStack(spacing: 8) {
                        Image(systemName: option.symbolName)
                            .font(.system(size: 13, weight: .semibold))
                            .symbolRenderingMode(.hierarchical)
                            .foregroundStyle(option.color)
                            .frame(width: 18)
                        Text(option.title)
                            .font(AppUI.controlFont)
                            .foregroundStyle(.primary)
                        Spacer(minLength: 8)
                        if option.dbValue == metadata?.usageStatus {
                            Image(systemName: "checkmark")
                                .font(.system(size: 11, weight: .bold))
                                .foregroundStyle(.secondary)
                        }
                    }
                    .padding(.horizontal, 10)
                    .padding(.vertical, 7)
                    .contentShape(RoundedRectangle(cornerRadius: AppUI.tightRadius))
                }
                .buttonStyle(.plain)
            }
        }
        .padding(6)
        .frame(width: 190)
    }

    // MARK: Dual pipeline + reuse marks row

    private var dualForFile: DualStateVM? {
        guard store.dualState?.fileID == file.id else { return nil }
        return store.dualState
    }

    private var isDualRunning: Bool { store.dualRunningIDs.contains(file.id) }

    private static let reuseChannels = [
        "newsletter", "lecture", "shorts", "sns", "consulting", "research",
    ]

    private var dualReuseRow: some View {
        HStack(spacing: AppUI.spacingS) {
            dualControl
            Divider().frame(height: 14)
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 5) {
                    ForEach(Self.reuseChannels, id: \.self) { channel in
                        reuseChip(channel)
                    }
                }
                .padding(.vertical, 1)
            }
        }
    }

    @ViewBuilder
    private var dualControl: some View {
        if isDualRunning {
            HStack(spacing: 5) {
                ProgressView().controlSize(.small)
                Text(dualForFile.map { Self.dualStageLabel($0.status) } ?? "Dual…")
                    .font(AppUI.metaFont)
                    .foregroundStyle(.secondary)
            }
        } else if let dual = dualForFile {
            HStack(spacing: 5) {
                dualBadge(dual)
                switch dual.status {
                case "relabel-pending":
                    Button("Confirm Speakers…") { showSpeakerSheet = true }
                        .font(AppUI.metaFont)
                        .buttonStyle(.link)
                case "vault-ready":
                    Button("Send to Vault") {
                        Task { await store.dualSend(file.id) }
                    }
                    .font(AppUI.metaFont)
                    .buttonStyle(.link)
                case "vault-sent":
                    if let path = dual.vaultPath, !path.isEmpty {
                        Button {
                            store.openPath(path)
                        } label: {
                            Image(systemName: "arrow.up.forward.app")
                                .font(.system(size: 11))
                        }
                        .buttonStyle(.plain)
                        .help("Open the vault transcript note")
                    }
                default:
                    // marked / parked-with-error: resume from where it stopped.
                    Button {
                        Task { await store.runDual(file.id) }
                    } label: {
                        Image(systemName: "play.circle")
                            .font(.system(size: 12, weight: .semibold))
                    }
                    .buttonStyle(.plain)
                    .help("Resume the dual pipeline")
                }
            }
            .contextMenu {
                Button("Re-run stage") { Task { await store.runDual(file.id) } }
                Button("Remove from pipeline") { Task { await store.dualUnmark(file.id) } }
            }
        } else {
            // Never run: an unmistakable call-to-action pill (vs the colored
            // stage badge shown once the pipeline has state).
            Button {
                Task { await store.runDual(file.id) }
            } label: {
                HStack(spacing: 4) {
                    Image(systemName: "bolt.badge.waveform")
                        .font(.system(size: 10, weight: .semibold))
                    Text("Dual Transcribe")
                        .font(.system(size: 10.5, weight: .semibold))
                }
                .padding(.horizontal, 8)
                .padding(.vertical, 3)
                .overlay(Capsule().stroke(AppUI.accentPink.opacity(0.55), lineWidth: 1))
            }
            .buttonStyle(.plain)
            .foregroundStyle(AppUI.accentPink)
            .help("아직 실행 전 — ElevenLabs 전사 → 실명 매핑 → Plaud×CMDS 교차분석 → 볼트")
        }
    }

    private func dualBadge(_ dual: DualStateVM) -> some View {
        HStack(spacing: 4) {
            Image(systemName: "bolt.badge.waveform")
                .font(.system(size: 10, weight: .semibold))
            Text(Self.dualStageLabel(dual.status))
                .font(.system(size: 10.5, weight: .semibold))
        }
        .padding(.horizontal, 7)
        .padding(.vertical, 3)
        .background(Self.dualStageColor(dual.status, hasError: dual.error != nil).opacity(0.16),
                    in: Capsule())
        .foregroundStyle(Self.dualStageColor(dual.status, hasError: dual.error != nil))
        .help(dual.error ?? Self.dualStageLabel(dual.status))
    }

    private static func dualStageLabel(_ status: String) -> String {
        switch status {
        case "marked": return "Dual: marked"
        case "transcribing": return "Transcribing…"
        case "relabel-pending": return "Speakers?"
        case "integrating": return "Integrating…"
        case "vault-ready": return "Vault Ready"
        case "vault-sent": return "In Vault"
        default: return status
        }
    }

    private static func dualStageColor(_ status: String, hasError: Bool) -> Color {
        if hasError { return .red }
        switch status {
        case "relabel-pending": return .orange
        case "vault-ready": return .blue
        case "vault-sent": return .green
        default: return .secondary
        }
    }

    /// Reuse chips: tap cycles none → flagged → drafted → published → none.
    private func reuseChip(_ channel: String) -> some View {
        let mark = store.reuseMarks.first { $0.channel == channel }
        let color: Color = switch mark?.status {
        case "flagged": .orange
        case "drafted": .blue
        case "published": .green
        default: .secondary
        }
        return Button {
            Task { await store.cycleReuse(file.id, channel: channel) }
        } label: {
            Text(channel)
                .font(.system(size: 10.5, weight: mark == nil ? .regular : .semibold))
                .padding(.horizontal, 7)
                .padding(.vertical, 3)
                .background(
                    (mark == nil ? Color.secondary.opacity(0.08) : color.opacity(0.16)),
                    in: Capsule()
                )
                .foregroundStyle(mark == nil ? Color.secondary : color)
        }
        .buttonStyle(.plain)
        .help(mark.map { "\(channel): \($0.status)\($0.note.map { n in " — \(n)" } ?? "")" }
              ?? "Mark as \(channel) material")
    }

    private func tagChip(_ tag: NoteTagVM) -> some View {
        HStack(spacing: 4) {
            Text("#\(tag.tag)")
                .font(AppUI.metaFont)
                .foregroundStyle(.primary)
            Button {
                Task { await store.removeTag(tag.tag, from: file.id) }
            } label: {
                Image(systemName: "xmark.circle.fill")
                    .font(AppUI.metaFont)
                    .foregroundStyle(.tertiary)
            }
            .buttonStyle(.plain)
            .help("Remove tag")
        }
        .padding(.horizontal, 7)
        .padding(.vertical, 3)
        .background(.quaternary, in: RoundedRectangle(cornerRadius: AppUI.tightRadius))
    }
}

// MARK: - Plaud Panel

/// Source › Final: the dual pipeline's cross-analyzed final transcript +
/// comprehensive summary, with one-click clipboard copy for downstream
/// content work (newsletter, lecture, …).
private struct FinalPanel: View {
    /// Shared with `ViewModeSegment`'s wrap toggle: wrap prose, or let
    /// column-structured raw content scroll sideways.
    @AppStorage("rawWrapLines") private var wrapLines: Bool = true

    @ObservedObject var store: FileStore
    @State private var tab: Tab = .transcript
    @State private var copied: String?
    @AppStorage("defaultContentViewMode") private var viewModeRaw: String =
        ContentViewMode.rendered.rawValue

    enum Tab: Hashable { case transcript, summary }

    private var integrated: IntegratedContentVM? {
        guard store.integratedContent?.fileID == store.selectedID else { return nil }
        return store.integratedContent
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            Divider()
            if let integrated {
                ScrollView {
                    content(integrated)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(12)
                }
            } else {
                emptyState
            }
        }
        .onChange(of: store.selectedID) { _, _ in tab = .transcript; copied = nil }
    }

    private var header: some View {
        HStack(spacing: AppUI.spacingS) {
            Label("최종본 · Dual (Plaud × ElevenLabs 교차분석)",
                  systemImage: "bolt.badge.waveform")
                .font(AppUI.sectionFont)
                .foregroundStyle(.secondary)
            if let label = integrated?.label {
                Text(label)
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.tertiary)
            }
            Spacer()
            if integrated != nil {
                Picker("", selection: $tab) {
                    Text("Transcript").tag(Tab.transcript)
                    Text("Summary").tag(Tab.summary)
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .frame(width: 190)
                copyMenu
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
    }

    /// Copy for downstream context use: current tab, or both stitched.
    private var copyMenu: some View {
        Menu {
            Button("Copy Transcript") { copy(integrated?.transcript ?? "", tag: "transcript") }
            Button("Copy Summary") { copy(integrated?.summary ?? "", tag: "summary") }
            Button("Copy Both (context block)") {
                guard let i = integrated else { return }
                copy("## Summary\n\n\(i.summary)\n\n## Final Transcript\n\n\(i.transcript)",
                     tag: "both")
            }
        } label: {
            Label(copied == nil ? "Copy" : "Copied ✓",
                  systemImage: copied == nil ? "doc.on.doc" : "checkmark.circle.fill")
                .font(AppUI.metaFont)
                .foregroundStyle(copied == nil ? Color.primary : Color.green)
        }
        .menuStyle(.borderlessButton)
        .fixedSize()
        .help("최종본을 클립보드로 — 다른 콘텐츠 작업의 컨텍스트로 붙여넣기")
    }

    private func copy(_ text: String, tag: String) {
        guard !text.isEmpty else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
        copied = tag
        Task {
            try? await Task.sleep(nanoseconds: 1_600_000_000)
            copied = nil
        }
    }

    @ViewBuilder
    private func content(_ integrated: IntegratedContentVM) -> some View {
        let mode = ContentViewMode(rawValue: viewModeRaw) ?? .rendered
        switch tab {
        case .transcript:
            if integrated.transcript.isEmpty {
                CenteredStateView(message: "최종 전사본 섹션이 비어 있습니다.",
                                  systemImage: "bolt.badge.waveform")
            } else if mode == .raw {
                RawTextView(wrapLines: wrapLines, text: integrated.transcript)
            } else {
                TranscriptBubbleList(text: integrated.transcript,
                                     selfNames: store.selfSpeakerNames)
            }
        case .summary:
            if integrated.summary.isEmpty {
                CenteredStateView(message: "종합 요약 섹션이 비어 있습니다.",
                                  systemImage: "text.badge.xmark")
            } else if mode == .raw {
                RawTextView(wrapLines: wrapLines, text: integrated.summary)
            } else {
                MarkdownDocumentView(text: integrated.summary)
            }
        }
    }

    private var emptyState: some View {
        VStack(spacing: AppUI.spacingM) {
            CenteredStateView(
                message: "아직 듀얼 최종본이 없습니다.\n메타데이터 줄의 ⚡ Dual Transcribe로 생성하세요\n(ElevenLabs 전사 → 실명 확인 → Plaud×CMDS 교차분석).",
                systemImage: "bolt.badge.waveform"
            )
            if let id = store.selectedID {
                Button {
                    Task { await store.runDual(id) }
                } label: {
                    Label("Dual Transcribe 시작", systemImage: "bolt.badge.waveform")
                }
                .disabled(store.dualRunningIDs.contains(id))
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

private struct PlaudPanel: View {
    /// Shared with `ViewModeSegment`'s wrap toggle: wrap prose, or let
    /// column-structured raw content scroll sideways.
    @AppStorage("rawWrapLines") private var wrapLines: Bool = true

    @ObservedObject var store: FileStore
    @State private var tab: Tab = .summary
    @State private var summaryIndex: Int = 0
    @State private var renamingSpeakers: Bool = false
    @AppStorage("defaultContentViewMode") private var viewModeRaw: String =
        ContentViewMode.rendered.rawValue

    enum Tab: Hashable { case transcript, summary, outline }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            tabBar
            Divider()
            ScrollView {
                content
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(12)
            }
        }
        .onChange(of: store.selectedID) { _, _ in
            tab = .summary; summaryIndex = 0
        }
        .onChange(of: store.content?.summaries.count) { _, _ in
            // Summaries can be appended/replaced after generation; reset the
            // selected tab index so it never points past the new array bounds.
            summaryIndex = 0
        }
        .sheet(isPresented: $renamingSpeakers) {
            if let fid = store.selectedID {
                PlaudSpeakerRenameSheet(
                    store: store,
                    fileID: fid,
                    speakers: Self.distinctSpeakers(
                        in: store.content?.transcript ?? ""
                    )
                ) { renamingSpeakers = false }
            }
        }
    }

    private var tabBar: some View {
        HStack(spacing: 4) {
            Label("Plaud", systemImage: "waveform.badge.mic")
                .font(AppUI.sectionFont)
                .foregroundStyle(.secondary)
            Divider().frame(height: 14)

            tabButton("Transcript", .transcript)
            ForEach(Array((store.content?.summaries ?? []).enumerated()),
                    id: \.offset) { idx, s in
                Button {
                    tab = .summary; summaryIndex = idx
                } label: {
                    Text(s.title)
                        .font(AppUI.controlFont)
                        .foregroundStyle(tab == .summary && summaryIndex == idx
                                         ? .primary : .secondary)
                        .fontWeight(tab == .summary && summaryIndex == idx
                                    ? .semibold : .regular)
                        .lineLimit(1)
                        .fixedSize()
                }
                .buttonStyle(.plain)
                .padding(.horizontal, 6)
            }
            tabButton("Outline", .outline)

            Spacer()
            ViewModeSegment(rawValue: $viewModeRaw)
            Button {
                renamingSpeakers = true
            } label: {
                Image(systemName: "person.2")
                    .help("Rename speakers in the Plaud transcript (applies on the Plaud server)")
            }
            .buttonStyle(.borderless)
            .frame(width: 28)
            .disabled(store.selectedID == nil
                      || (store.content?.transcript.isEmpty ?? true))
            copyMenu
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 6)
    }

    /// Distinct speaker labels in the formatted Plaud transcript
    /// (`[ts] Speaker: content` lines), in first-seen order.
    private static func distinctSpeakers(in transcript: String) -> [String] {
        var seen: [String] = []
        for line in transcript.components(separatedBy: .newlines) {
            guard line.first == "[",
                  let close = line.firstIndex(of: "]") else { continue }
            let rest = line[line.index(after: close)...]
                .trimmingCharacters(in: .whitespaces)
            guard let colon = rest.firstIndex(of: ":") else { continue }
            let speaker = String(rest[..<colon])
                .trimmingCharacters(in: .whitespacesAndNewlines)
            if !speaker.isEmpty, !seen.contains(speaker) {
                seen.append(speaker)
            }
        }
        return seen
    }

    private func tabButton(_ title: String, _ k: Tab) -> some View {
        Button { tab = k } label: {
            Text(title)
                .font(AppUI.controlFont)
                .foregroundStyle(tab == k ? .primary : .secondary)
                .fontWeight(tab == k ? .semibold : .regular)
                // Segment labels never wrap mid-word when the pane narrows.
                .lineLimit(1)
                .fixedSize()
        }
        .buttonStyle(.plain)
        .padding(.horizontal, 6)
    }

    private var copyMenu: some View {
        Menu {
            Button("Copy all") { store.copyToClipboard(buildAll()) }
            Divider()
            Button("Copy Transcript") {
                store.copyToClipboard(store.content?.transcript ?? "")
            }
            ForEach(Array((store.content?.summaries ?? []).enumerated()),
                    id: \.offset) { _, s in
                Button("Copy \(s.title)") {
                    store.copyToClipboard(s.body)
                }
            }
            Button("Copy Outline") {
                store.copyToClipboard(store.content?.outline ?? "")
            }
        } label: {
            Image(systemName: "doc.on.clipboard")
                .help("Copy Plaud package to clipboard")
        }
        .menuStyle(.borderlessButton)
        .frame(width: 32)
    }

    private func buildAll() -> String {
        guard let c = store.content else { return "" }
        var parts: [String] = []
        if let t = c.title { parts.append("# \(t)") }
        if !c.keywords.isEmpty {
            parts.append("Keywords: " + c.keywords.joined(separator: ", "))
        }
        for s in c.summaries {
            parts.append("\n## \(s.title)\n\n\(s.body)")
        }
        if !c.outline.isEmpty {
            parts.append("\n## Outline\n\n\(c.outline)")
        }
        if !c.transcript.isEmpty {
            parts.append("\n## Transcript\n\n\(c.transcript)")
        }
        return parts.joined(separator: "\n")
    }

    @ViewBuilder
    private var content: some View {
        let mode = ContentViewMode(rawValue: viewModeRaw) ?? .rendered
        if let plaudContent = store.content {
            switch tab {
            case .transcript:
                let transcript = plaudContent.transcript
                if mode == .raw {
                    RawTextView(wrapLines: wrapLines, text: transcript)
                } else {
                    TranscriptBubbleList(text: transcript,
                                         selfNames: store.selfSpeakerNames)
                }
            case .summary:
                if !plaudContent.summaries.isEmpty,
                   summaryIndex < plaudContent.summaries.count {
                    let body = plaudContent.summaries[summaryIndex].body
                    if mode == .raw {
                        RawTextView(wrapLines: wrapLines, text: body)
                    } else {
                        MarkdownDocumentView(text: body)
                    }
                } else {
                    VStack(spacing: AppUI.spacingS) {
                        CenteredStateView(
                            message: "이 녹음의 Plaud 요약이 아직 없습니다.\nPlaud에서 처리 중이거나 받아오지 못한 상태일 수 있어요.",
                            systemImage: "doc.text.magnifyingglass"
                        )
                        if let id = store.selectedID {
                            Button {
                                Task { await store.refetchDetail(id) }
                            } label: {
                                Label("Plaud에서 다시 받아오기", systemImage: "arrow.clockwise")
                            }
                        }
                    }
                }
            case .outline:
                let outline = plaudContent.outline
                if mode == .raw {
                    RawTextView(wrapLines: wrapLines, text: outline)
                } else {
                    MarkdownDocumentView(text: outline)
                }
            }
        } else {
            missingContentState
        }
    }

    @ViewBuilder
    private var missingContentState: some View {
        if store.auth == nil {
            CenteredStateView(message: "Loading content…", loading: true)
        } else {
            switch AuthStateStyle(store.auth?.state) {
            case .expired:
                CenteredStateView(
                    message: store.auth?.autoRefreshReady == true
                        ? "Plaud auth expired. Use auth > Refresh Token Now (renews without a browser), then sync again."
                        : "Plaud auth expired. Use auth > Authenticate with Plaud, then sync again.",
                    systemImage: "xmark.shield.fill",
                    tint: .red
                )
            case .unconfigured:
                CenteredStateView(
                    message: "Plaud auth is not configured. Use auth > Authenticate with Plaud.",
                    systemImage: "shield.slash.fill",
                    tint: .red
                )
            case .unknown:
                CenteredStateView(
                    message: "Plaud content is not cached yet. Check auth, then sync or select the recording again.",
                    systemImage: "questionmark.folder.fill"
                )
            case .valid, .expiring:
                // Only spin while a detail fetch is actually in flight;
                // otherwise the fetch already failed and a retry is needed.
                if store.selectedID.map({ store.pendingDetailFetch.contains($0) }) == true {
                    CenteredStateView(message: "Loading content…", loading: true)
                } else {
                    VStack(spacing: AppUI.spacingS) {
                        CenteredStateView(
                            message: "Couldn't load content for this recording.",
                            systemImage: "exclamationmark.triangle.fill",
                            tint: .orange
                        )
                        Button {
                            store.retryContentFetch()
                        } label: {
                            Label("Retry", systemImage: "arrow.clockwise")
                        }
                    }
                }
            }
        }
    }
}

// MARK: - Plaud Speaker Rename Sheet

/// Rename speakers in the **Plaud server** transcript. Lists the distinct
/// speaker labels of the currently displayed transcript, each with a field
/// prefilled with the current name. Apply runs
/// `plaud plaud-relabel <id> OLD=NEW …` and then re-fetches detail so the
/// UI reflects the server-side rename.
private struct PlaudSpeakerRenameSheet: View {
    @ObservedObject var store: FileStore
    let fileID: String
    let speakers: [String]
    let dismiss: () -> Void

    /// Target name per raw speaker label, prefilled with the current name.
    @State private var names: [String: String] = [:]

    private var isRunning: Bool { store.plaudRelabelingIDs.contains(fileID) }

    /// Mapping of only the names the user actually changed.
    private var changedMapping: [String: String] {
        var mapping: [String: String] = [:]
        for raw in speakers {
            let new = (names[raw] ?? raw)
                .trimmingCharacters(in: .whitespacesAndNewlines)
            if !new.isEmpty, new != raw {
                mapping[raw] = new
            }
        }
        return mapping
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Rename speakers").font(.headline)
            Text("Renames the speakers on the Plaud server transcript, then refreshes the local copy.")
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)

            if speakers.isEmpty {
                Text("No speaker labels found in this transcript.")
                    .font(AppUI.bodyFont)
                    .foregroundStyle(.secondary)
            } else {
                ForEach(speakers, id: \.self) { raw in
                    HStack(spacing: 8) {
                        Text(raw)
                            .font(AppUI.bodyFont)
                            .lineLimit(1)
                            .truncationMode(.tail)
                            .frame(width: 140, alignment: .leading)
                        Image(systemName: "arrow.right")
                            .font(.caption)
                            .foregroundStyle(.tertiary)
                        TextField("New name", text: binding(for: raw))
                            .textFieldStyle(.roundedBorder)
                            .disabled(isRunning)
                    }
                }
            }

            HStack {
                if isRunning {
                    ProgressView().controlSize(.small)
                    Text("Renaming on Plaud…")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Button("Cancel") { dismiss() }
                    .disabled(isRunning)
                Button("Apply") { apply() }
                    .keyboardShortcut(.defaultAction)
                    .disabled(isRunning || changedMapping.isEmpty)
            }
        }
        .padding(20)
        .frame(minWidth: 440)
        .onAppear {
            for raw in speakers where names[raw] == nil {
                names[raw] = raw
            }
        }
    }

    private func binding(for raw: String) -> Binding<String> {
        Binding(
            get: { names[raw] ?? raw },
            set: { names[raw] = $0 }
        )
    }

    private func apply() {
        let mapping = changedMapping
        guard !mapping.isEmpty else { return }
        Task {
            // Failure surfaces via store.lastCommandError (alert in
            // ContentView); the sheet closes either way after the run.
            await store.relabelPlaudSpeakers(fileID, mapping: mapping)
            dismiss()
        }
    }
}

// MARK: - CMDS Panel

private struct CmdsPanel: View {
    /// Shared with `ViewModeSegment`'s wrap toggle: wrap prose, or let
    /// column-structured raw content scroll sideways.
    @AppStorage("rawWrapLines") private var wrapLines: Bool = true

    @ObservedObject var store: FileStore

    @State private var numSpeakers: Int = 0
    @State private var sections: [Database.CmdsSection] = []
    /// Per-section, per-raw-speaker target name. Keyed by "<sectionId>::<raw>".
    @State private var sectionMap: [String: String] = [:]
    @State private var savedSpeakers: [Database.Speaker] = []
    @State private var managingSpeakers: Bool = false
    @State private var gapSeconds: Double = 10
    @AppStorage("defaultContentViewMode") private var viewModeRaw: String =
        ContentViewMode.rendered.rawValue

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            headerBar
            Divider()
            ScrollView {
                VStack(alignment: .leading, spacing: 10) {
                    controlBar
                    Divider()
                    if let cmds = store.cmdsTranscript, !cmds.isEmpty {
                        if !DistributionProfile.isCommunity {
                            if sections.count > 1 {
                                sectionRelabelBars
                            } else {
                                flatRelabelBar
                            }
                        }
                        if (ContentViewMode(rawValue: viewModeRaw) ?? .rendered) == .raw {
                            RawTextView(wrapLines: wrapLines, text: cmds)
                        } else {
                            TranscriptBubbleList(text: cmds,
                                                 selfNames: store.selfSpeakerNames)
                        }
                    } else if let fid = store.selectedID,
                              store.transcribingIDs.contains(fid) {
                        CenteredStateView(
                            message: "Transcribing with ElevenLabs Scribe…",
                            loading: true
                        )
                    } else {
                        CenteredStateView(
                            message: DistributionProfile.isCommunity
                                ? "No ElevenLabs transcript yet — set speaker count above and click Transcribe."
                                : "No CMDS transcript yet — set speaker count above and click Transcribe."
                        )
                    }
                }
                .padding(12)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .onAppear { refresh() }
        .onChange(of: store.selectedID) { _, _ in refresh() }
        .onChange(of: store.cmdsTranscript) { _, _ in refresh() }
        .onChange(of: gapSeconds) { _, _ in refresh() }
        .sheet(isPresented: $managingSpeakers) {
            SpeakersManagerSheet(store: store) {
                managingSpeakers = false
                savedSpeakers = Database.shared.savedSpeakers()
            }
        }
    }

    private var headerBar: some View {
        HStack(spacing: 8) {
            Label(
                DistributionProfile.isCommunity
                    ? "ElevenLabs Scribe transcript" : "CMDS · ElevenLabs Scribe",
                systemImage: "person.wave.2"
            )
                .font(AppUI.sectionFont)
                .foregroundStyle(.secondary)
            Spacer()
            ViewModeSegment(rawValue: $viewModeRaw)
            copyMenu
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 6)
    }

    private var copyMenu: some View {
        Menu {
            Button("Copy all sections") {
                store.copyToClipboard(buildAllSections())
            }
            Divider()
            Button("Copy full transcript") {
                store.copyToClipboard(store.cmdsTranscript ?? "")
            }
            if sections.count > 1 {
                Divider()
                ForEach(sections) { sec in
                    Button("Copy [\(formatTime(sec.startSec))–\(formatTime(sec.endSec))]") {
                        store.copyToClipboard(sectionPackage(sec))
                    }
                }
            }
        } label: {
            Image(systemName: "doc.on.clipboard")
                .help(
                    DistributionProfile.isCommunity
                        ? "Copy ElevenLabs transcript package to clipboard"
                        : "Copy CMDS package to clipboard"
                )
        }
        .menuStyle(.borderlessButton)
        .frame(width: 32)
    }

    private func sectionPackage(_ sec: Database.CmdsSection) -> String {
        let header = "[\(formatTime(sec.startSec))–\(formatTime(sec.endSec))] " +
                     "speakers: \(sec.speakers.joined(separator: ", "))"
        return header + "\n\n" + sec.body
    }

    private func buildAllSections() -> String {
        if sections.isEmpty {
            return store.cmdsTranscript ?? ""
        }
        return sections.map { sectionPackage($0) }
                       .joined(separator: "\n\n---\n\n")
    }

    private func refresh() {
        savedSpeakers = Database.shared.savedSpeakers()
        if let id = store.selectedID {
            sections = Database.shared.cmdsSections(for: id, gapSec: gapSeconds)
        } else {
            sections = []
        }
    }

    private var controlBar: some View {
        HStack(spacing: 8) {
            Text("Speakers:")
            Stepper(value: $numSpeakers, in: 0...10) {
                Text(numSpeakers == 0 ? "Auto" : "\(numSpeakers)")
                    .font(.body.monospacedDigit())
            }
            .frame(width: 110)
            .help("0 = Auto (ElevenLabs decides). 1-10 = pin to that count.")
            Button("Transcribe with ElevenLabs…") {
                if let fid = store.selectedID {
                    Task { await store.transcribeWithElevenLabs(fid,
                                                                numSpeakers: numSpeakers) }
                }
            }
            .disabled(store.selectedID.map { store.transcribingIDs.contains($0) } ?? true)

            if !DistributionProfile.isCommunity {
                Divider().frame(height: 18)
                Text("Conv. gap:")
                Stepper(value: $gapSeconds, in: 3...60, step: 1) {
                    Text("\(Int(gapSeconds))s")
                }
                .frame(width: 100)
                .help("Silence gap (seconds) that splits one recording into multiple "
                      + "conversations. Each conversation gets its own speaker mapping.")
            }

            Spacer()
            if !DistributionProfile.isCommunity {
                Button("Manage saved speakers") { managingSpeakers = true }
                    .buttonStyle(.borderless)
            } else {
                Text("Audio is uploaded only after confirmation; ElevenLabs credits may be used.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
    }

    @ViewBuilder
    private var flatRelabelBar: some View {
        if let only = sections.first, !only.speakers.isEmpty {
            sectionRelabelRow(section: only, label: "Speakers")
        }
    }

    @ViewBuilder
    private var sectionRelabelBars: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("\(sections.count) conversations detected (gap ≥ \(Int(gapSeconds))s)")
                .font(.caption).foregroundStyle(.secondary)
            ForEach(sections) { section in
                sectionRelabelRow(
                    section: section,
                    label: "\(formatTime(section.startSec))–\(formatTime(section.endSec))"
                )
                .padding(8)
                .background(Color(NSColor.controlBackgroundColor))
                .cornerRadius(AppUI.tightRadius)
            }
            Button("Apply all") {
                applyAllSections()
            }
            .keyboardShortcut(.defaultAction)
        }
    }

    private func sectionRelabelRow(section: Database.CmdsSection,
                                   label: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(label).font(.caption.bold())
                Text("· \(section.preview.trimmingCharacters(in: .whitespaces))")
                    .font(.caption2).foregroundStyle(.tertiary)
                    .lineLimit(1)
            }
            // A conversation with 6+ speakers needs ~900pt of pickers, which
            // the middle pane never has — they used to clip and collide with
            // the Apply button. Scroll the pickers horizontally and keep
            // Apply pinned outside the scroll area so it stays reachable.
            HStack(spacing: 8) {
                ScrollView(.horizontal, showsIndicators: true) {
                    HStack(alignment: .bottom, spacing: 8) {
                        ForEach(section.speakers, id: \.self) { raw in
                            speakerPicker(sectionId: section.id, raw: raw)
                        }
                    }
                    .padding(.bottom, 4)
                }
                Button("Apply") {
                    applySection(section)
                }
                .fixedSize()
            }
        }
    }

    private func speakerPicker(sectionId: Int, raw: String) -> some View {
        let key = "\(sectionId)::\(raw)"
        return VStack(alignment: .leading, spacing: 2) {
            Text(raw).font(.caption2).foregroundStyle(.tertiary)
            Picker("", selection: Binding(
                get: { sectionMap[key] ?? "" },
                set: { sectionMap[key] = $0 }
            )) {
                Text("(keep)").tag("")
                ForEach(savedSpeakers) { sp in
                    Text(sp.isSelf ? "★ \(sp.name)" : sp.name).tag(sp.name)
                }
            }
            .frame(width: 140)
            .labelsHidden()
        }
    }

    /// Relabel a single section, clear its map, and refresh — inline (awaitable)
    /// so callers can serialize multiple sections without overlapping writes.
    private func applySectionAsync(_ section: Database.CmdsSection) async {
        guard let fid = store.selectedID else { return }
        var map: [String: String] = [:]
        for raw in section.speakers {
            let key = "\(section.id)::\(raw)"
            if let v = sectionMap[key], !v.isEmpty { map[raw] = v }
        }
        guard !map.isEmpty else { return }
        // pad endSec slightly so the last segment is included
        await store.relabelCmdsSpeakers(
            fid, mapping: map,
            startSec: section.startSec,
            endSec: section.endSec + 0.5
        )
        // clear that section's map after apply
        for raw in section.speakers {
            sectionMap["\(section.id)::\(raw)"] = ""
        }
        refresh()
    }

    private func applySection(_ section: Database.CmdsSection) {
        Task { await applySectionAsync(section) }
    }

    private func applyAllSections() {
        // Serialize: relabel writes share the same SQLite rows + sectionMap.
        // Firing overlapping Tasks let later relabels race the earlier ones.
        // A single Task that awaits each section in turn keeps them ordered.
        let snapshot = sections
        Task {
            for section in snapshot {
                await applySectionAsync(section)
            }
        }
    }

    /// Section labels share the app-wide M:SS convention (H:MM:SS ≥ 1 hour)
    /// via `Database.formatMinSec` instead of a hand-rolled format.
    private func formatTime(_ sec: Double) -> String {
        Database.formatMinSec(Int(sec))
    }
}

// MARK: - AI Inspector Panel (right side)

/// Right-side inspector hosting AI Summary slots. Always visible regardless
/// of the source tab (Plaud / CMDS) shown in the main pane.
///
/// Each slot can run either:
///   - `summarize` (CMDS transcript only → one summary file), or
///   - `integrate` (CMDS + Plaud transcripts + Plaud summaries → final
///     transcript **and** comprehensive summary, both saved separately).
///
/// Destination/mode menu for sending a recording's generated content into an
/// Obsidian vault. Integrated-first: every entry defaults to the fused output
/// (with summary/plaud fallback in the CLI). Used in the Work Sidebar Actions
/// row, the detail toolbar, and the command palette.
struct VaultSendMenu: View {
    @ObservedObject var store: FileStore
    let fileID: String
    /// Compact = toolbar icon; full = labeled button for the Actions row.
    var compact: Bool = false

    private var isSending: Bool { store.vaultSendingIDs.contains(fileID) }

    var body: some View {
        Menu {
            Section("Main Vault") {
                Button {
                    send()
                } label: {
                    Label("Inbox로 보내기 (통합본 · 즉시)", systemImage: "tray.and.arrow.down")
                }
                Button {
                    send(withTranscript: true)
                } label: {
                    Label("Inbox로 + 전사본 포함", systemImage: "doc.plaintext")
                }
                Button {
                    send(dest: "meetings")
                } label: {
                    Label("Meetings 폴더로 (63. Meetings)", systemImage: "person.2")
                }
            }
            Section("Knowledge-base folder") {
                Button {
                    send(to: "wiki")
                } label: {
                    Label("Wiki Inbox로 보내기", systemImage: "books.vertical")
                }
            }
            Section("AI로 다듬어서") {
                Button {
                    send(via: "claude")
                } label: {
                    Label("Claude가 정형화 후 저장 (headless, 수 분)", systemImage: "wand.and.stars")
                }
                Button {
                    send(via: "claude-window")
                } label: {
                    Label("Claude Terminal에서 직접 파일링…", systemImage: "terminal")
                }
            }
        } label: {
            if compact {
                if isSending {
                    ProgressView().controlSize(.small)
                } else {
                    ToolbarIconLabel(systemName: "paperplane")
                }
            } else {
                HStack(spacing: 4) {
                    if isSending {
                        ProgressView().controlSize(.small)
                        Text("Sending…")
                    } else {
                        Label("Send to Vault", systemImage: "paperplane.fill")
                    }
                }
            }
        }
        .disabled(fileID.isEmpty || isSending)
        .help("생성된 요약/통합본을 옵시디언 볼트로 보내기")
    }

    private func send(
        to: String = "main",
        dest: String = "",
        via: String = "direct",
        withTranscript: Bool = false
    ) {
        Task {
            await store.vaultSend(
                fileID, to: to, dest: dest, via: via, withTranscript: withTranscript
            )
        }
    }
}

/// The slot row exposes a mode picker (Summary / Integrated) so the user can
/// flip between both kinds of output for the same file.
private struct AIInspectorPanel: View {
    /// Shared with `ViewModeSegment`'s wrap toggle: wrap prose, or let
    /// column-structured raw content scroll sideways.
    @AppStorage("rawWrapLines") private var wrapLines: Bool = true

    @ObservedObject var store: FileStore
    let collapse: () -> Void

    @State private var slots: [Database.Slot] = []
    @State private var templates: [String] = []
    @State private var addingSlot: Bool = false
    /// Per-slot mode (.summary or .integrated). Defaults to .integrated when
    /// the slot's template is "integrated", else .summary.
    @State private var modeMap: [String: SlotMode] = [:]
    @State private var expanded: Set<String> = []
    /// Slot whose output is shown in the large expand sheet (nil = closed).
    @State private var expandedSlot: Database.Slot?
    /// Per-slot view: which integrated subsection to show (all / transcript / summary).
    @State private var viewMap: [String: Database.IntegratedKind] = [:]
    @State private var refreshTick: Int = 0
    /// "Ask about this recording…" prompt sheet (Claude Code bridge).
    @State private var askingClaude: Bool = false
    @State private var claudeQuestion: String = ""
    @AppStorage("defaultContentViewMode") private var viewModeRaw: String =
        ContentViewMode.rendered.rawValue

    enum SlotMode: String, CaseIterable, Identifiable {
        case summary, integrated
        var id: String { rawValue }
        var label: String { self == .summary ? "Summary" : "Integrated" }
        /// Segment icon: plain doc for the single-summary path, merge arrows
        /// for the integrated (Plaud + CMDS) pipeline.
        var icon: String {
            self == .summary ? "doc.text" : "arrow.triangle.merge"
        }
        /// Accent tint so the two modes are distinct at a glance:
        /// Summary = blue, Integrated = green.
        var tint: Color { self == .summary ? .blue : .green }
        /// One-line explanation of what generating in this mode produces.
        var caption: String {
            self == .summary
                ? "Plaud 전사·요약만 사용해 단일 요약 생성"
                : "Plaud + CMDS 전사를 통합해 최종 전사본 + 요약 생성"
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            Divider()
            ScrollView {
                VStack(alignment: .leading, spacing: 8) {
                    quickActions
                    if slots.isEmpty {
                        Text("No summary slots configured.")
                            .foregroundStyle(.secondary)
                            .padding(12)
                    } else {
                        ForEach(slots) { slot in
                            slotCard(slot)
                        }
                    }
                }
                .padding(12)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .frame(maxHeight: .infinity, alignment: .topLeading)
        .background(Color(NSColor.windowBackgroundColor))
        .id(refreshTick)
        .onAppear { reload() }
        .onChange(of: store.selectedID) { _, _ in
            reload()
            expanded.removeAll()
            expandedSlot = nil
            modeMap.removeAll()  // re-derive integrated-first defaults per file
            store.lastVaultSend = nil
        }
        .onChange(of: store.summarizingKeys.count) { _, _ in
            refreshTick &+= 1
        }
        .sheet(isPresented: $addingSlot) {
            AddSlotSheet(templates: templates, store: store) {
                addingSlot = false
                reload()
            }
        }
        .sheet(isPresented: $askingClaude) {
            VStack(alignment: .leading, spacing: AppUI.spacingM) {
                Label("Ask Claude about this recording", systemImage: "asterisk.circle")
                    .font(.title3.weight(.semibold))
                Text("Terminal에서 Claude Code 세션이 열립니다 — plaud 스킬·MCP·CLI가 모두 사용 가능한 환경에서 이 녹음의 전사/요약을 근거로 답합니다.")
                    .font(AppUI.metaFont)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                TextEditor(text: $claudeQuestion)
                    .font(.system(size: 13))
                    .frame(height: 90)
                    .overlay(RoundedRectangle(cornerRadius: 6)
                        .stroke(Color(NSColor.separatorColor), lineWidth: 1))
                HStack {
                    Button("Cancel") { askingClaude = false }
                    Spacer()
                    Button("Open Claude Code") {
                        let question = claudeQuestion
                        askingClaude = false
                        claudeQuestion = ""
                        if let fid = store.selectedID {
                            Task { await store.launchClaude(fid, task: "ask", prompt: question) }
                        }
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(claudeQuestion.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
            }
            .padding(20)
            .frame(width: 460)
        }
        .sheet(item: $expandedSlot) { slot in
            slotOutputSheet(slot)
        }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Label("Work Sidebar", systemImage: "sparkles")
                    .font(AppUI.sectionFont)
                Spacer()
                SidebarTogglePill(
                    title: "Hide Sidebar",
                    systemName: "sidebar.trailing",
                    active: true
                ) {
                    collapse()
                }
                .help("Collapse sidebar")
            }
            HStack(spacing: 6) {
                ViewModeSegment(rawValue: $viewModeRaw, width: 120)
                Spacer()
                Button {
                    store.openProjectFolder("data/integrated/\(store.selectedID ?? "")")
                } label: {
                    ToolbarIconLabel(systemName: "folder")
                }
                .help("Reveal integrated outputs folder")
                .buttonStyle(.borderless)
                Button {
                    store.openProjectFolder("templates")
                } label: {
                    ToolbarIconLabel(systemName: "doc.text")
                }
                .help("Open templates folder")
                .buttonStyle(.borderless)
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
    }

    private var quickActions: some View {
        let fid = store.selectedID ?? ""
        let metadataRunning = store.metadataGeneratingIDs.contains(fid)
        let meetingRunning = store.meetingNoteGeneratingIDs.contains(fid)

        return VStack(alignment: .leading, spacing: 8) {
            Text("Actions")
                .font(AppUI.sectionFont)
                .foregroundStyle(.secondary)
            HStack(spacing: 6) {
                VaultSendMenu(store: store, fileID: fid)
                    .menuStyle(.borderlessButton)
                    .fixedSize()

                Button {
                    // No explicit model: the CLI resolves the configured
                    // classify model (Settings > Auto-classify).
                    Task { await store.generateMetadata(fid) }
                } label: {
                    if metadataRunning {
                        HStack(spacing: 4) {
                            ProgressView().controlSize(.small)
                            Text("Metadata")
                        }
                    } else {
                        Label("Metadata", systemImage: "sparkles")
                    }
                }
                .disabled(fid.isEmpty || metadataRunning)
            }
            HStack(spacing: 6) {
                Button {
                    let choice = defaultAIModelChoice()
                    Task {
                        await store.writeMeetingNote(
                            fid, model: choice.provider, modelID: choice.modelID
                        )
                    }
                } label: {
                    if meetingRunning {
                        HStack(spacing: 4) {
                            ProgressView().controlSize(.small)
                            Text("Meeting Note")
                        }
                    } else {
                        Label("Meeting Note", systemImage: "doc.badge.plus")
                    }
                }
                .disabled(fid.isEmpty || meetingRunning)

                Button {
                    addingSlot = true
                } label: {
                    Label("Summary Slot", systemImage: "plus")
                }
                .disabled(fid.isEmpty)

                Button {
                    // Kick every slot's Integrated generation concurrently —
                    // each runs as its own CLI process; per-card spinners come
                    // from summarizingKeys.
                    for slot in slots {
                        let s = slot
                        Task {
                            await store.integrate(
                                fileID: fid, model: s.model,
                                modelID: s.modelID ?? "", template: s.template
                            )
                            refreshTick &+= 1
                        }
                    }
                } label: {
                    Label("Generate All", systemImage: "arrow.triangle.merge")
                }
                .disabled(fid.isEmpty || slots.isEmpty || !store.summarizingKeys.isEmpty)
                .help("모든 슬롯의 Integrated 결과를 한 번에 생성")

                // CLI/MCP bridge: open a Claude Code session preloaded with
                // this recording's context — plaud-* skills + Plaud MCP +
                // this repo's CLI all available in that session.
                Menu {
                    Button("Ask about this recording…") { askingClaude = true }
                    Button("Follow-up draft (plaud-followup)") {
                        Task { await store.launchClaude(fid, task: "followup") }
                    }
                    Button("Recent digest (plaud-digest)") {
                        Task { await store.launchClaude("", task: "digest") }
                    }
                } label: {
                    Label("Claude", systemImage: "asterisk.circle")
                }
                .fixedSize()
                .disabled(fid.isEmpty)
                .help("Claude Code 세션을 이 녹음 컨텍스트로 열기 (Terminal)")
            }
            vaultSendFeedback(fid)
        }
        .buttonStyle(.borderless)
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .font(AppUI.controlFont)
        // Liquid Glass quick actions area
        .background(
            RoundedRectangle(cornerRadius: AppUI.glassRadius)
                .fill(.regularMaterial)
        )
        // Liquid Glass style via material + stroke (use .glassEffect on macOS 26+)
        .background(
            RoundedRectangle(cornerRadius: AppUI.glassRadius)
                .fill(.regularMaterial)
        )
        .overlay(
            RoundedRectangle(cornerRadius: AppUI.glassRadius)
                .stroke(AppUI.glassStroke, lineWidth: 0.6)
        )
    }

    private func reload() {
        slots = Database.shared.loadSlots()
        templates = Database.shared.listTemplates()
    }

    /// Vault-send progress/result strip under the action buttons: spinner
    /// while a send runs, then the written note's filename + Open button.
    @ViewBuilder
    private func vaultSendFeedback(_ fid: String) -> some View {
        if store.vaultSendingIDs.contains(fid) {
            HStack(spacing: 6) {
                ProgressView().controlSize(.small)
                Text("볼트로 보내는 중…")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        } else if let sent = store.lastVaultSend, sent.status == "ok", !sent.noteFilename.isEmpty {
            HStack(spacing: 6) {
                Image(systemName: "checkmark.circle.fill")
                    .foregroundStyle(.green)
                    .font(.system(size: 11))
                Text(sent.noteFilename)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
                Spacer(minLength: 4)
                Button("Open") {
                    store.openVaultNote(sent)
                }
                .controlSize(.small)
                .font(.caption)
            }
        }
    }

    private func mode(for slot: Database.Slot) -> SlotMode {
        if let m = modeMap[slot.id] { return m }
        if slot.template == "integrated" { return .integrated }
        // Integrated-first: when a fused output already exists for this slot,
        // surface it by default — it is the artifact the user actually uses.
        let fid = store.selectedID ?? ""
        if !fid.isEmpty,
           Database.shared.integratedExists(
               fileID: fid, model: slot.outputModel, template: slot.template
           ) {
            return .integrated
        }
        return .summary
    }

    private func viewKind(for slot: Database.Slot) -> Database.IntegratedKind {
        viewMap[slot.id] ?? .all
    }

    @ViewBuilder
    private func slotCard(_ slot: Database.Slot) -> some View {
        let fid = store.selectedID ?? ""
        let key = "\(fid)::\(slot.model)::\(slot.outputModel)::\(slot.template)"
        let isRunning = store.summarizingKeys.contains(key)
        let isOpen = expanded.contains(slot.id)
        let m = mode(for: slot)
        let hasOutput: Bool = {
            switch m {
            case .summary:
                return Database.shared.summaryBody(
                    fileID: fid, model: slot.outputModel, template: slot.template) != nil
            case .integrated:
                return Database.shared.integratedExists(
                    fileID: fid, model: slot.outputModel, template: slot.template)
            }
        }()

        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Image(systemName: hasOutput ? "checkmark.circle.fill" : "circle.dashed")
                    .foregroundColor(hasOutput ? .green : .secondary)
                VStack(alignment: .leading, spacing: 3) {
                    Text(slot.name).font(AppUI.rowTitleFont)
                    HStack(spacing: 4) {
                        slotChip("\(slot.model) · \(slot.outputModel)",
                                 icon: "cpu")
                        slotChip(slot.template, icon: "doc.plaintext")
                    }
                }
                Spacer()
                Button(role: .destructive) {
                    Task { await store.deleteSlot(slot.name); reload() }
                } label: { Image(systemName: "trash") }
                .buttonStyle(.plain)
                .foregroundStyle(.tertiary)
            }

            slotModeControl(slot)

            // Action row (Generate + Copy + Show)
            HStack(spacing: 6) {
                if isRunning {
                    ProgressView().controlSize(.small)
                    Text("Running…").font(.caption).foregroundStyle(.secondary)
                } else {
                    Button(hasOutput ? "Re-generate" : "Generate") {
                        Task {
                            switch m {
                            case .summary:
                                await store.summarize(
                                    fileID: fid, model: slot.model,
                                    modelID: slot.modelID ?? "",
                                    template: slot.template
                                )
                            case .integrated:
                                await store.integrate(
                                    fileID: fid, model: slot.model,
                                    modelID: slot.modelID ?? "",
                                    template: slot.template
                                )
                            }
                            refreshTick &+= 1
                        }
                    }
                    .controlSize(.small)
                    .disabled(fid.isEmpty)
                }
                Spacer()
                if hasOutput {
                    Button {
                        Task {
                            await store.vaultSend(
                                fid,
                                content: m == .integrated ? "integrated" : "summary",
                                model: slot.outputModel,
                                template: slot.template
                            )
                        }
                    } label: {
                        Image(systemName: "paperplane")
                            .font(.system(size: 10.5, weight: .semibold))
                    }
                    .buttonStyle(.borderless)
                    .disabled(store.vaultSendingIDs.contains(fid))
                    .help("이 결과물을 메인 볼트 Inbox로 보내기")
                    copyMenu(slot: slot, mode: m)
                    Button {
                        expandedSlot = slot
                    } label: {
                        Image(systemName: "arrow.up.left.and.arrow.down.right")
                            .font(.system(size: 10.5, weight: .semibold))
                    }
                    .buttonStyle(.borderless)
                    .help("Expand output in a larger window")
                    Button(isOpen ? "Hide" : "Show") {
                        if isOpen { expanded.remove(slot.id) }
                        else { expanded.insert(slot.id) }
                    }
                    .buttonStyle(.borderless)
                    .font(AppUI.controlFont)
                }
            }

            if isOpen, hasOutput {
                outputBody(slot: slot, mode: m)
            }
        }
        .padding(10)
        // Liquid Glass card (2026): regular material base + subtle glass stroke
        .background(
            RoundedRectangle(cornerRadius: AppUI.glassRadius)
                .fill(.regularMaterial)
        )
        // Liquid Glass style via material + stroke (use .glassEffect on macOS 26+)
        .background(
            RoundedRectangle(cornerRadius: AppUI.glassRadius)
                .fill(.regularMaterial)
        )
        .overlay(
            RoundedRectangle(cornerRadius: AppUI.glassRadius)
                .stroke(AppUI.glassStroke, lineWidth: 0.6)
        )
    }

    /// Small rounded chip for slot metadata (model id, template).
    private func slotChip(_ text: String, icon: String) -> some View {
        HStack(spacing: 3) {
            Image(systemName: icon)
                .font(.system(size: 9, weight: .semibold))
            Text(text)
        }
        .font(.caption2)
        .foregroundStyle(.secondary)
        .lineLimit(1)
        .padding(.horizontal, 6)
        .padding(.vertical, 2.5)
        .background(Color.primary.opacity(0.07), in: Capsule())
    }

    /// Mode picker: icon + label segments, tinted per mode (Summary = blue,
    /// Integrated = green), with a one-line caption explaining what the
    /// selected mode generates.
    private func slotModeControl(_ slot: Database.Slot) -> some View {
        let current = mode(for: slot)
        return VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 2) {
                ForEach(SlotMode.allCases) { option in
                    let selected = current == option
                    Button {
                        modeMap[slot.id] = option
                    } label: {
                        HStack(spacing: 4) {
                            Image(systemName: option.icon)
                                .font(.system(size: 10.5, weight: .semibold))
                                .symbolRenderingMode(.hierarchical)
                            Text(option.label)
                        }
                        .font(AppUI.controlFont)
                        .foregroundStyle(selected ? option.tint : .secondary)
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 5)
                        .background(
                            selected ? option.tint.opacity(0.14) : Color.clear,
                            in: RoundedRectangle(cornerRadius: AppUI.tightRadius)
                        )
                        .overlay(
                            RoundedRectangle(cornerRadius: AppUI.tightRadius)
                                .stroke(selected ? option.tint.opacity(0.45)
                                                 : Color.clear,
                                        lineWidth: 1)
                        )
                    }
                    .buttonStyle(.plain)
                }
            }
            .padding(3)
            .background(AppUI.subtleFill,
                        in: RoundedRectangle(cornerRadius: AppUI.radius))

            Text(current.caption)
                .font(.caption2)
                .foregroundStyle(current.tint)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    @ViewBuilder
    private func outputBody(slot: Database.Slot, mode m: SlotMode) -> some View {
        let fid = store.selectedID ?? ""
        switch m {
        case .summary:
            if let body = Database.shared.summaryBody(
                fileID: fid, model: slot.outputModel, template: slot.template) {
                renderMarkdown(body)
            }
        case .integrated:
            VStack(alignment: .leading, spacing: 6) {
                integratedKindControl(slot)
                if let body = Database.shared.integratedBody(
                    fileID: fid, model: slot.outputModel, template: slot.template,
                    kind: viewKind(for: slot)) {
                    renderMarkdown(body)
                }
            }
        }
    }

    /// Large resizable sheet showing a slot's output. Reuses the exact inline
    /// pieces: `outputBody` (same rendered markdown view, and for integrated
    /// mode the same All / Transcript / Summary picker backed by the shared
    /// `viewMap`, so the card and sheet stay in sync) and `copyMenu` (same raw
    /// markdown copy logic).
    @ViewBuilder
    private func slotOutputSheet(_ slot: Database.Slot) -> some View {
        let m = mode(for: slot)
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                Text("\(slot.name) — \(m.label)")
                    .font(.headline)
                Spacer()
                copyMenu(slot: slot, mode: m)
                Button("Done") { expandedSlot = nil }
                    .keyboardShortcut(.defaultAction)
            }
            Divider()
            ScrollView {
                outputBody(slot: slot, mode: m)
                    .padding(.bottom, 8)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .padding(16)
        .frame(minWidth: 720, minHeight: 560)
    }

    private func integratedKindControl(_ slot: Database.Slot) -> some View {
        let options: [(String, Database.IntegratedKind)] = [
            ("All", .all),
            ("Transcript", .transcript),
            ("Summary", .summary),
        ]
        return HStack(spacing: 2) {
            ForEach(options, id: \.0) { title, kind in
                let selected = viewKind(for: slot) == kind
                Button {
                    viewMap[slot.id] = kind
                } label: {
                    Text(title)
                        .font(AppUI.controlFont)
                        .foregroundStyle(selected ? .primary : .secondary)
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 5)
                        .background(
                            selected ? AppUI.selectedFill : Color.clear,
                            in: RoundedRectangle(cornerRadius: AppUI.tightRadius)
                        )
                }
                .buttonStyle(.plain)
            }
        }
        .padding(3)
        .background(AppUI.subtleFill, in: RoundedRectangle(cornerRadius: AppUI.radius))
    }

    @ViewBuilder
    private func renderMarkdown(_ body: String) -> some View {
        Group {
            if (ContentViewMode(rawValue: viewModeRaw) ?? .rendered) == .raw {
                RawTextView(wrapLines: wrapLines, text: body)
            } else {
                MarkdownDocumentView(text: body)
            }
        }
        .padding(8)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color(NSColor.windowBackgroundColor))
        .cornerRadius(6)
    }

    /// Copy menu: Summary mode → just "Copy". Integrated mode → All / Transcript / Summary.
    @ViewBuilder
    private func copyMenu(slot: Database.Slot, mode m: SlotMode) -> some View {
        let fid = store.selectedID ?? ""
        switch m {
        case .summary:
            Button("Copy") {
                store.copyToClipboard(
                    Database.shared.summaryBody(
                        fileID: fid, model: slot.outputModel, template: slot.template) ?? ""
                )
            }
            .buttonStyle(.borderless)
        case .integrated:
            Menu("Copy") {
                Button("Copy all") {
                    store.copyToClipboard(
                        Database.shared.integratedBody(
                            fileID: fid, model: slot.outputModel,
                            template: slot.template, kind: .all) ?? ""
                    )
                }
                Button("Copy transcript") {
                    store.copyToClipboard(
                        Database.shared.integratedBody(
                            fileID: fid, model: slot.outputModel,
                            template: slot.template, kind: .transcript) ?? ""
                    )
                }
                Button("Copy summary") {
                    store.copyToClipboard(
                        Database.shared.integratedBody(
                            fileID: fid, model: slot.outputModel,
                            template: slot.template, kind: .summary) ?? ""
                    )
                }
            }
            .menuStyle(.borderlessButton)
            .fixedSize()
        }
    }
}

private struct AddSlotSheet: View {
    let templates: [String]
    @ObservedObject var store: FileStore
    let dismiss: () -> Void

    @State private var name: String = ""
    @State private var model: String = "claude"
    @State private var modelID: String = Database.fallbackModelIDs["claude"] ?? "claude-fable-5"
    @State private var template: String = "integrated"
    @State private var presets: [ModelPresetVM] = Database.shared.loadModelPresets()

    private let models = ["claude", "codex", "gemini", "grok"]

    private var providerPresets: [ModelPresetVM] {
        presets.filter { $0.provider == model }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("New summary slot").font(.headline)
            HStack {
                Text("Name").frame(width: 80, alignment: .leading)
                TextField("e.g. Meeting (Claude)", text: $name)
                    .textFieldStyle(.roundedBorder)
            }
            HStack {
                Text("Model").frame(width: 80, alignment: .leading)
                Picker("", selection: $model) {
                    ForEach(models, id: \.self) { Text($0).tag($0) }
                }
                .labelsHidden()
                .onChange(of: model) { _, newValue in
                    if let preset = presets.first(where: { $0.provider == newValue }) {
                        modelID = preset.apiName
                    }
                }
            }
            HStack {
                Text("Model ID").frame(width: 80, alignment: .leading)
                TextField("api model id", text: $modelID)
                    .textFieldStyle(.roundedBorder)
                Menu {
                    ForEach(providerPresets) { preset in
                        Button(preset.apiName) {
                            modelID = preset.apiName
                        }
                    }
                } label: {
                    Image(systemName: "list.bullet.rectangle")
                }
                .menuStyle(.borderlessButton)
                .help("Pick from CMDS API Information presets")
            }
            HStack {
                Text("Template").frame(width: 80, alignment: .leading)
                Picker("", selection: $template) {
                    ForEach(templates.isEmpty ? ["default"] : templates,
                            id: \.self) { Text($0).tag($0) }
                }.labelsHidden()
            }
            HStack {
                Spacer()
                Button("Cancel") { dismiss() }
                Button("Add") {
                    Task {
                        await store.addSlot(
                            name: name, model: model,
                            modelID: modelID.trimmingCharacters(in: .whitespacesAndNewlines),
                            template: template
                        )
                        dismiss()
                    }
                }
                .keyboardShortcut(.defaultAction)
                .disabled(name.isEmpty)
            }
        }
        .padding(20)
        .frame(minWidth: 460)
        .onAppear {
            let loaded = Database.shared.loadModelPresets()
            presets = loaded
            let currentProviderPresets = loaded.filter { $0.provider == model }
            if let preset = currentProviderPresets.first,
               modelID.isEmpty || !currentProviderPresets.contains(where: { $0.apiName == modelID }) {
                modelID = preset.apiName
            }
        }
    }
}

// MARK: - Speakers Manager Sheet

private struct SpeakersManagerSheet: View {
    @ObservedObject var store: FileStore
    let dismiss: () -> Void

    @State private var speakers: [Database.Speaker] = []
    @State private var newName: String = ""
    @State private var newIsSelf: Bool = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Saved speakers").font(.headline)

            if speakers.isEmpty {
                Text("No saved speakers yet.")
                    .foregroundStyle(.secondary)
            } else {
                List {
                    ForEach(speakers) { sp in
                        HStack {
                            Image(systemName: sp.isSelf ? "person.fill.checkmark"
                                                        : "person")
                                .foregroundStyle(sp.isSelf ? .blue : .secondary)
                            Text(sp.name)
                            if sp.isSelf {
                                Text("self").font(.caption2).foregroundStyle(.secondary)
                            }
                            Spacer()
                            Button(role: .destructive) {
                                Task {
                                    await store.deleteSpeaker(sp.id)
                                    speakers = Database.shared.savedSpeakers()
                                }
                            } label: {
                                Image(systemName: "trash")
                            }
                            .buttonStyle(.plain)
                        }
                    }
                }
                .frame(minHeight: 160, maxHeight: 240)
            }

            Divider()
            HStack {
                TextField("New speaker name", text: $newName)
                    .textFieldStyle(.roundedBorder)
                Toggle("Self", isOn: $newIsSelf)
                Button("Add") {
                    Task {
                        await store.addSpeaker(name: newName, isSelf: newIsSelf)
                        newName = ""
                        newIsSelf = false
                        speakers = Database.shared.savedSpeakers()
                    }
                }.disabled(newName.isEmpty)
            }

            HStack {
                Spacer()
                Button("Done") { dismiss() }
                    .keyboardShortcut(.defaultAction)
            }
        }
        .padding(20)
        .frame(minWidth: 420)
        .onAppear { speakers = Database.shared.savedSpeakers() }
    }
}

// MARK: - Edit Folder Sheet

private struct EditFolderSheet: View {
    let folder: FolderVM
    @ObservedObject var store: FileStore
    let dismiss: () -> Void

    @State private var name: String
    @State private var color: String
    @State private var icon: String

    init(folder: FolderVM, store: FileStore, dismiss: @escaping () -> Void) {
        self.folder = folder
        self.store = store
        self.dismiss = dismiss
        _name = State(initialValue: folder.name)
        _color = State(initialValue: folder.color ?? PLAUD_COLORS[0])
        _icon = State(initialValue: "")
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Edit folder").font(.headline)

            HStack {
                Text("Name").frame(width: 60, alignment: .leading)
                TextField("Folder name", text: $name)
                    .textFieldStyle(.roundedBorder)
            }

            VStack(alignment: .leading, spacing: 6) {
                Text("Color").foregroundStyle(.secondary).font(.caption)
                HStack(spacing: 10) {
                    ForEach(PLAUD_COLORS, id: \.self) { hex in
                        ColorSwatch(hex: hex, selected: hex == color) {
                            color = hex
                        }
                    }
                }
            }

            HStack {
                Text("Icon").frame(width: 60, alignment: .leading)
                TextField("iconfont_folder_speech (optional)", text: $icon)
                    .textFieldStyle(.roundedBorder)
                    .help("Plaud icon identifier; leave blank to keep current.")
            }

            HStack {
                Spacer()
                Button("Cancel") { dismiss() }
                Button("Save") {
                    Task {
                        await store.updateFolder(
                            folder.id,
                            name: name == folder.name ? nil : name,
                            color: color == folder.color ? nil : color,
                            icon: icon.isEmpty ? nil : icon
                        )
                        dismiss()
                    }
                }
                .keyboardShortcut(.defaultAction)
                .disabled(name.isEmpty)
            }
        }
        .padding(20)
        .frame(minWidth: 360)
    }
}

private struct ColorSwatch: View {
    let hex: String
    let selected: Bool
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            ZStack {
                RoundedRectangle(cornerRadius: 6)
                    .fill(Color(hex: hex) ?? .gray)
                    .frame(width: 30, height: 30)
                if selected {
                    Image(systemName: "checkmark")
                        .foregroundStyle(.white)
                        .font(.caption.bold())
                }
            }
            .overlay(
                RoundedRectangle(cornerRadius: 6)
                    .stroke(.white.opacity(selected ? 0.6 : 0), lineWidth: 1)
            )
        }
        .buttonStyle(.plain)
    }
}

// MARK: - Audio Player

/// NSViewRepresentable wrapper around AVPlayerView so we can host AVKit
/// playback inside SwiftUI without using the unbundled SwiftPM-incompatible
/// `VideoPlayer` (which crashes with a demangling error in executable targets).
struct AudioPlayerView: NSViewRepresentable {
    let player: AVPlayer

    func makeNSView(context: Context) -> AVPlayerView {
        let view = AVPlayerView()
        view.player = player
        view.controlsStyle = .inline
        view.showsFullScreenToggleButton = false
        view.showsFrameSteppingButtons = false
        return view
    }

    func updateNSView(_ view: AVPlayerView, context: Context) {
        if view.player !== player {
            view.player = player
        }
    }
}

// MARK: - Color helper
//
// `Color(hex:)` now lives in `ColorHex.swift` as an internal extension so it is
// shared across the module instead of being fileprivate to this file.

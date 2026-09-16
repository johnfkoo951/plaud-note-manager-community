import AppKit
import Combine
import Darwin
import Foundation

/// Snapshot of Plaud credential health, mirroring the JSON emitted by
/// `plaud auth --json`. All fields are optional/tolerant so a partial or
/// malformed payload still decodes into *something* the UI can render.
struct AuthStatus: Codable, Equatable {
    var configured: Bool
    var state: String  // valid | expiring | expired | unconfigured | unknown
    var workspaceID: String?
    var memberID: String?
    var role: String?
    var issuedAt: Int?
    var expiresAt: Int?
    var secondsRemaining: Int?
    var remainingHuman: String?
    var liveOK: Bool?
    /// Renewal readiness: ready | expiring | expired | disabled |
    /// not_bootstrapped | store_unavailable.
    var autoRefresh: String?
    /// When the *refresh* token itself dies (epoch s) — the re-bootstrap horizon.
    var refreshExpiresAt: Int?
    var detail: String

    enum CodingKeys: String, CodingKey {
        case configured
        case state
        case workspaceID = "workspace_id"
        case memberID = "member_id"
        case role
        case issuedAt = "issued_at"
        case expiresAt = "expires_at"
        case secondsRemaining = "seconds_remaining"
        case remainingHuman = "remaining_human"
        case liveOK = "live_ok"
        case autoRefresh = "auto_refresh"
        case refreshExpiresAt = "refresh_expires_at"
        case detail
    }

    /// True when the CLI can renew the token headlessly (no browser needed).
    var autoRefreshReady: Bool {
        autoRefresh == "ready" || autoRefresh == "expiring"
    }

    /// Fallback used when the CLI output can't be decoded — keeps the indicator
    /// alive (gray "unknown") instead of crashing or vanishing.
    static func unknown(detail: String) -> AuthStatus {
        AuthStatus(
            configured: false,
            state: "unknown",
            workspaceID: nil,
            memberID: nil,
            role: nil,
            issuedAt: nil,
            expiresAt: nil,
            secondsRemaining: nil,
            remainingHuman: nil,
            liveOK: nil,
            autoRefresh: nil,
            refreshExpiresAt: nil,
            detail: detail
        )
    }
}

/// Single-flight auth repair state.  WebKit is mounted only during
/// ``webSession`` so it cannot compete with Keychain during normal operation.
enum AuthRecoveryPhase: Equatable {
    case idle
    case webSession
    case verifying
    case needsInteractive
}

@MainActor
final class FileStore: ObservableObject {
    /// The full library (trash included) loaded once from SQLite. The visible
    /// `files` list is derived from this synchronously, so folder-clicks and
    /// search keystrokes never hit the DB or take an async hop.
    private var masterFiles: [Database.MasterFile] = []
    @Published var files: [PlaudFileVM] = []
    @Published var folders: [FolderVM] = []
    /// Distinct tags with file counts, refreshed in `reload()`. Drives the
    /// sidebar Tags section.
    @Published var tagCounts: [(tag: String, count: Int)] = []
    /// Tags pinned to the top of the Tags section (config.json `pinned_tags`).
    @Published var pinnedTags: [String] = []
    @Published var categoryCounts: (all: Int, unfiled: Int, starred: Int, trash: Int) = (0, 0, 0, 0)
    @Published var cacheStatus: (total: Int, cached: Int) = (0, 0)
    @Published var sidebar: SidebarItem = .allFiles
    @Published var search: String = ""
    /// Search scope: filename substring (fast, local) vs. full content
    /// (FTS5 via `plaud search`). Persisted by the UI in
    /// @AppStorage("searchScope") and mirrored here on change.
    @Published var contentSearchScope: Bool = false
    /// FTS hit file ids in RELEVANCE order (best match first). Empty when not
    /// in content-search mode or the query is blank. Drives both the filter
    /// (which rows are visible) and the order (rank, not date).
    @Published var contentSearchHits: [String] = []
    /// file_id -> snippet (with « » around the matched span) for the matched
    /// rows. Only populated in content-search mode.
    @Published var contentSnippets: [String: String] = [:]
    /// True while a content search subprocess is in flight (drives the field
    /// spinner).
    @Published var contentSearchRunning: Bool = false
    /// The query the current `contentSearchHits` correspond to — used by the
    /// empty-state copy so it shows the term that returned nothing.
    @Published var contentSearchQuery: String = ""
    @Published var selectedID: String?
    @Published var content: FileContentVM?
    @Published var noteMetadata: NoteMetadataVM?
    /// Dual-transcribe pipeline state of the selected recording (nil = unmarked).
    @Published var dualState: DualStateVM?
    /// Content-reuse marks of the selected recording.
    @Published var reuseMarks: [ReuseMarkVM] = []
    /// Dual final artifacts (cross-analyzed transcript + summary) of the
    /// selected recording — drives the Source › Final tab.
    @Published var integratedContent: IntegratedContentVM?
    /// Recordings with a `plaud dual` stage running (drives the ⚡ spinner).
    @Published var dualRunningIDs: Set<String> = []

    /// ElevenLabs STT credit balance for the library-overview indicator.
    struct ElevenLabsStatus: Decodable, Equatable {
        let status: String
        var tier: String?
        var remaining: Int?
        var limit: Int?
        var resetAt: Int?

        enum CodingKeys: String, CodingKey {
            case status, tier, remaining, limit
            case resetAt = "reset_at"
        }
    }

    @Published var elevenLabs: ElevenLabsStatus?
    private var elevenLabsFetchedAt: Date?

    enum CommunityElevenLabsRetryState: Equatable {
        case clear
        case outcomeUnknown
    }

    private struct CommunityElevenLabsAttemptPayload: Decodable {
        let status: String
        let retryMayBillTwice: Bool

        enum CodingKeys: String, CodingKey {
            case status
            case retryMayBillTwice = "retry_may_bill_twice"
        }
    }

    nonisolated static func communityElevenLabsRetryState(
        from output: String
    ) -> CommunityElevenLabsRetryState? {
        guard let data = output.data(using: .utf8),
              let payload = try? JSONDecoder().decode(
                  CommunityElevenLabsAttemptPayload.self,
                  from: data
              )
        else { return nil }
        switch (payload.status, payload.retryMayBillTwice) {
        case ("clear", false):
            return .clear
        case ("outcome_unknown", true):
            return .outcomeUnknown
        default:
            return nil
        }
    }

    nonisolated static func communityElevenLabsArguments(
        fileID: String,
        numSpeakers: Int,
        replacingExisting: Bool,
        retryOutcomeUnknown: Bool
    ) -> [String] {
        var args = ["elevenlabs-transcribe", fileID, "--confirm-upload", "--json"]
        if replacingExisting || retryOutcomeUnknown {
            args.append("--force")
        }
        if numSpeakers > 0 {
            args += ["--num-speakers", String(numSpeakers)]
        }
        return args
    }

    /// Fetch the ElevenLabs balance, throttled to every 30 min — it's a
    /// passive indicator, not something worth an API call per sync. Pass
    /// force after transcription runs, which actually spend credits.
    func refreshElevenLabs(force: Bool = false) async {
        if !force, let at = elevenLabsFetchedAt,
           Date().timeIntervalSince(at) < 1800 { return }
        elevenLabsFetchedAt = Date()
        let output = await runPlaudOutput(
            args: ["elevenlabs-status", "--json"], showError: false
        )
        let trimmed = output.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let data = trimmed.data(using: .utf8), !data.isEmpty,
              let decoded = try? JSONDecoder().decode(ElevenLabsStatus.self, from: data)
        else { return }
        elevenLabs = decoded
    }
    @Published var isSyncing: Bool = false
    @Published var lastSyncedAt: Date?
    @Published var deepSyncRunning: Bool = false
    @Published var lastCommandError: String?
    /// Latest Plaud credential health, refreshed at launch, after each sync,
    /// and on app activation. Drives the toolbar auth-status indicator.
    @Published var auth: AuthStatus?
    /// True while `plaud ws-refresh` mints a fresh token headlessly (drives
    /// the popover's Refresh Token button spinner).
    @Published var refreshingWorkspaceToken: Bool = false
    /// True while `refreshAuthCredentials()` parses the copied Plaud cURL and
    /// replaces the Keychain credential bundle. Drives auth UI spinners and disabled
    /// state.
    @Published var refreshingAuth = false
    /// Outcome metadata for the most recent manual cURL import. A valid access
    /// token and durable automatic renewal are separate facts; the auth sheet
    /// uses these fields to avoid describing cURL-only setup as permanent.
    @Published private(set) var lastCurlImportAutoRefreshArmed: Bool?
    @Published private(set) var lastCurlImportDetail: String?
    /// Password-free fallback through the app's persistent Plaud Web session.
    @Published var authRecoveryPhase: AuthRecoveryPhase = .idle
    @Published var authRecoveryRequestID: Int = 0
    @Published var authRecoveryStatus: String?
    /// One automatic attempt per rejected access-token generation. A healthy
    /// replacement clears this in `selfHealAuthIfNeeded`.
    var selfHealCredentialIssuedAt: Int?

    private var cloudSyncTimer: Timer?
    private var cloudSyncInterval: TimeInterval = 30
    private var cancellables: Set<AnyCancellable> = []
    private var lifecycleObservers: [NSObjectProtocol] = []
    /// File IDs with an in-flight `detail` fetch. Published so the detail
    /// pane can distinguish "loading" from "fetch finished with nothing".
    @Published private(set) var pendingDetailFetch: Set<String> = []
    /// Generation counter to coalesce bursts of reload requests: only the
    /// latest off-main fetch is allowed to publish its results.
    private var reloadGeneration: UInt64 = 0
    /// Same coalescing trick for detail loads: a fast arrow-key walk down the
    /// list fires one load per row, and only the newest may publish.
    private var contentGeneration: UInt64 = 0

    /// Lowercased names of speakers flagged `is_self`, refreshed once per
    /// `reload()`. Read by the transcript renderer instead of hitting SQLite
    /// per bubble.
    @Published var selfSpeakerNames: [String] = []

    /// Pipeline stage of the current selection (Integrated > Transcribed >
    /// Cached > New). Derived off-main in `loadContent` — deriving it inside
    /// the detail view's body meant a `data/integrated/` directory scan plus
    /// two queries on every publish.
    @Published var selectedStage: PipelineStage = .new
    /// Generation token guarding content search: a slow earlier query can't
    /// overwrite a newer one's results.
    private var contentSearchGeneration: UInt64 = 0
    /// Pending debounce task for content search (cancelled on each keystroke).
    private var contentSearchDebounce: Task<Void, Never>?

    var selectedFile: PlaudFileVM? { files.first { $0.id == selectedID } }

    struct CommandResult {
        let exitCode: Int32
        let stdout: String
        let stderr: String
        let forceKilled: Bool

        init(
            exitCode: Int32,
            stdout: String,
            stderr: String,
            forceKilled: Bool = false
        ) {
            self.exitCode = exitCode
            self.stdout = stdout
            self.stderr = stderr
            self.forceKilled = forceKilled
        }

        var ok: Bool { exitCode == 0 }

        var failureMessage: String {
            let rawBody = [stderr, stdout]
                .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
                .first { !$0.isEmpty } ?? "No output"
            let body = Self.conciseBody(from: rawBody)
            return "Exit \(exitCode): \(body)"
        }

        private static func conciseBody(from raw: String) -> String {
            let cleaned = redactSensitive(stripANSI(raw))
            let lines = cleaned
                .components(separatedBy: .newlines)
                .map { trimTraceLine($0) }
                .filter { !$0.isEmpty }
            if cleaned.localizedCaseInsensitiveContains("workspace token expired") {
                // Static context — can't check autoRefreshReady here, so point at
                // the popover, which offers whichever renewal path is available.
                return "Plaud auth expired. Use the toolbar auth button to renew, then retry."
            }

            if cleaned.localizedCaseInsensitiveContains("traceback")
                || cleaned.localizedCaseInsensitiveContains("most recent call last") {
                if let explicit = lines.reversed().first(where: isUsefulErrorLine) {
                    return explicit
                }
                if cleaned.contains("httpx") || cleaned.contains("httpcore") {
                    return "Plaud network request failed. Check your internet connection or Plaud session, then retry."
                }
                return lines.suffix(4).joined(separator: "\n")
            }

            let body = lines.joined(separator: "\n")
            if body.count <= 1200 { return body }
            return String(body.prefix(1200)) + "\n…"
        }

        private static func isUsefulErrorLine(_ line: String) -> Bool {
            let lower = line.lowercased()
            if lower.contains("plaud network error")
                || lower.contains("plaud content network error")
                || lower.contains("plaud audio network error")
                || lower.contains("plaud http")
                || lower.contains("plaud api error")
                || lower.contains("missing plaud credentials") {
                return true
            }
            return line.range(
                of: #"([A-Za-z_][A-Za-z0-9_.]*(Error|Exception|Timeout)):"#,
                options: .regularExpression
            ) != nil
        }

        private static func trimTraceLine(_ line: String) -> String {
            line
                .replacingOccurrences(of: "│", with: " ")
                .replacingOccurrences(of: "┃", with: " ")
                .trimmingCharacters(in: .whitespacesAndNewlines)
        }

        private static func stripANSI(_ text: String) -> String {
            let pattern = #"\u{001B}\[[0-?]*[ -/]*[@-~]"#
            guard let regex = try? NSRegularExpression(pattern: pattern) else {
                return text
            }
            let range = NSRange(text.startIndex..<text.endIndex, in: text)
            return regex.stringByReplacingMatches(
                in: text,
                options: [],
                range: range,
                withTemplate: ""
            )
        }

        private static func redactSensitive(_ text: String) -> String {
            let rules: [(String, String)] = [
                (#"(?i)(authorization|cookie|x-device-id)\s*[:=]\s*[^\s,;]+"#,
                 "$1: [REDACTED]"),
                (#"(?i)bearer\s+[A-Za-z0-9._~+/=-]+"#, "Bearer [REDACTED]"),
                (#"(?i)([?&](?:token|signature|x-amz-signature)=)[^&\s]+"#,
                 "$1[REDACTED]"),
                (#"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"#,
                 "[REDACTED_JWT]"),
                (#"/Users/[^/\s]+"#, "~"),
            ]
            return rules.reduce(text) { value, rule in
                guard let regex = try? NSRegularExpression(pattern: rule.0) else {
                    return value
                }
                let range = NSRange(value.startIndex..<value.endIndex, in: value)
                return regex.stringByReplacingMatches(
                    in: value,
                    options: [],
                    range: range,
                    withTemplate: rule.1
                )
            }
        }
    }

    /// Thread-safe storage used by the stdout/stderr drain workers. Keeping the
    /// bytes in memory avoids putting command output (or piped secrets) in a
    /// temporary file while still letting both pipes drain as the child runs.
    private final class ProcessCaptureBuffer: @unchecked Sendable {
        private let lock = NSLock()
        private var data = Data()

        func replace(with value: Data) {
            lock.lock()
            data = value
            lock.unlock()
        }

        func snapshot() -> Data {
            lock.lock()
            defer { lock.unlock() }
            return data
        }
    }

    private final class ProcessTimeoutState: @unchecked Sendable {
        private let lock = NSLock()
        private var timedOutValue = false
        private var forceKilledValue = false
        private var completedValue = false

        func beginTimeout() -> Bool {
            lock.lock()
            defer { lock.unlock() }
            guard !completedValue else { return false }
            timedOutValue = true
            return true
        }

        func markCompleted() {
            lock.lock()
            completedValue = true
            lock.unlock()
        }

        func markForceKilled() {
            lock.lock()
            forceKilledValue = true
            lock.unlock()
        }

        var timedOut: Bool {
            lock.lock()
            defer { lock.unlock() }
            return timedOutValue
        }

        var forceKilled: Bool {
            lock.lock()
            defer { lock.unlock() }
            return forceKilledValue
        }
    }

    init() {
        DatabaseWatcher.shared.start()
        reload()

        // Debounce DB-change ticks so a burst of rapid CLI writes (sync,
        // relabel, classify…) coalesces into a single reload instead of one
        // full re-query per WAL touch.
        NotificationCenter.default.publisher(for: .plaudDBChanged)
            .debounce(for: .milliseconds(300), scheduler: RunLoop.main)
            .sink { [weak self] _ in self?.reload() }
            .store(in: &cancellables)

        NotificationCenter.default.publisher(for: NSApplication.didBecomeActiveNotification)
            .sink { [weak self] _ in
                Task { @MainActor in
                    await self?.refreshCommunityUndoStatus()
                    guard self?.auth?.configured == true else {
                        await self?.refreshAuth()
                        return
                    }
                    await self?.sync(showError: false)
                }
            }
            .store(in: &cancellables)

        // Folder selection and search filter the already-loaded master list
        // synchronously — no DB hop, no async frame. A `@Published` sink fires
        // during `willSet`, so `self.sidebar`/`self.search` still hold the OLD
        // value here; we must use the value the publisher emits.
        $sidebar
            .sink { [weak self] newSidebar in
                self?.applyFilter(sidebar: newSidebar, search: self?.search ?? "")
            }
            .store(in: &cancellables)

        $search
            .removeDuplicates()
            .sink { [weak self] newSearch in
                guard let self else { return }
                self.applyFilter(sidebar: self.sidebar, search: newSearch)
                self.onSearchInputsChanged(search: newSearch, scope: self.contentSearchScope)
            }
            .store(in: &cancellables)

        // Flipping the scope toggle re-runs (or tears down) content search and
        // re-derives the visible list.
        $contentSearchScope
            .removeDuplicates()
            .sink { [weak self] newScope in
                guard let self else { return }
                self.onSearchInputsChanged(search: self.search, scope: newScope)
                self.applyFilter(sidebar: self.sidebar, search: self.search)
            }
            .store(in: &cancellables)

        $selectedID
            .removeDuplicates()
            .sink { [weak self] id in
                self?.loadContent(for: id)
                // Opening a recording marks it seen (read-state dot flips
                // green -> gray without waiting for a DB-watcher reload).
                if let id { self?.markSeen(id) }
            }
            .store(in: &cancellables)

        registerLifecycleObservers()
        // Show the auth indicator immediately at launch with a cheap offline
        // check, before the (slower, network-bound) initial sync finishes.
        Task {
            await refreshCommunityUndoStatus()
            await refreshAuth()
            if auth?.configured == true {
                await sync(showError: false)
            }
        }
        startCloudPolling(every: 30)
    }

    deinit {
        cloudSyncTimer?.invalidate()
        let center = NotificationCenter.default
        for observer in lifecycleObservers {
            center.removeObserver(observer)
        }
    }

    /// Suspend the cloud-sync poll while the app is in the background and
    /// resume it on activation, so the 30s network poll doesn't run forever.
    private func registerLifecycleObservers() {
        guard lifecycleObservers.isEmpty else { return }
        let center = NotificationCenter.default
        lifecycleObservers.append(
            center.addObserver(forName: NSApplication.didResignActiveNotification,
                               object: nil, queue: .main) { [weak self] _ in
                MainActor.assumeIsolated { self?.stopCloudPolling() }
            }
        )
        lifecycleObservers.append(
            center.addObserver(forName: NSApplication.didBecomeActiveNotification,
                               object: nil, queue: .main) { [weak self] _ in
                MainActor.assumeIsolated { self?.startCloudPolling(every: self?.cloudSyncInterval ?? 30) }
            }
        )
    }

    func startCloudPolling(every seconds: TimeInterval) {
        cloudSyncInterval = seconds
        cloudSyncTimer?.invalidate()
        let t = Timer(timeInterval: seconds, repeats: true) { [weak self] _ in
            Task { @MainActor in await self?.sync(showError: false) }
        }
        RunLoop.main.add(t, forMode: .common)
        cloudSyncTimer = t
    }

    func stopCloudPolling() {
        cloudSyncTimer?.invalidate()
        cloudSyncTimer = nil
    }

    /// Refresh all list/count state from SQLite.
    ///
    /// The blocking SQLite queries run on a detached background task; only the
    /// resulting value-type snapshots are published back on the main actor, so
    /// SwiftUI updates stay safe and the main thread never stalls on a full
    /// query. Bursts of reload requests are coalesced via `reloadGeneration` —
    /// only the most recent fetch is allowed to publish, collapsing a flurry of
    /// `.plaudDBChanged` ticks into one visible update.
    ///
    /// Ordering note: callers that mutate SQLite first (e.g. `assignFolder` ->
    /// `writeFileFolders`) complete their synchronous write before invoking
    /// `reload()`, so the off-main read launched here always observes that
    /// committed write — the optimistic update is never raced away.
    func reload() {
        reloadGeneration &+= 1
        let generation = reloadGeneration
        let selectedID = self.selectedID

        Task.detached(priority: .userInitiated) { [weak self] in
            let master = Database.shared.masterFiles()
            let folders = Database.shared.folders()
            let tagCounts = Database.shared.tagCounts()
            let pinnedTags = Database.shared.loadAppConfig().pinnedTags
            let categoryCounts = Database.shared.categoryCounts()
            let cacheStatus = Database.shared.contentCacheStatus()
            // Self-speaker names are hoisted out of the transcript render
            // path: `TranscriptBubbleList` used to re-query `speakers` once
            // per bubble (~1,400 locked SQLite reads for a 1h20m recording,
            // every single body evaluation).
            let selfNames = Database.shared.savedSpeakers()
                .filter(\.isSelf)
                .map { $0.name.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() }
                .filter { !$0.isEmpty }

            guard let strongSelf = self else { return }
            await MainActor.run {
                guard generation == strongSelf.reloadGeneration else { return }
                strongSelf.masterFiles = master
                strongSelf.folders = folders
                strongSelf.tagCounts = tagCounts
                strongSelf.pinnedTags = pinnedTags
                strongSelf.categoryCounts = categoryCounts
                strongSelf.cacheStatus = cacheStatus
                // Re-derive the visible list against the freshly-loaded master
                // set using the *current* sidebar/search.
                strongSelf.applyFilter(sidebar: strongSelf.sidebar,
                                       search: strongSelf.search)
                strongSelf.selfSpeakerNames = selfNames
                if let id = selectedID, id == strongSelf.selectedID {
                    strongSelf.loadContent(for: id)
                    // Covers the launch race where a file is selected before
                    // the first master load lands (no-op once seen).
                    strongSelf.markSeen(id)
                }
            }
        }
    }

    /// Derive the visible `files` list from the in-memory `masterFiles` master
    /// set. Reproduces the four sidebar cases exactly as `Database.files(for:)`
    /// did in SQL, plus a case-insensitive filename `contains(search)`.
    /// Runs synchronously on the main actor — no DB hop, same render pass.
    func applyFilter(sidebar: SidebarItem, search: String) {
        let trimmed = search.trimmingCharacters(in: .whitespacesAndNewlines)
        let needle = trimmed.lowercased()

        // Content-search mode: when scope = 전체내용 and there's a non-empty
        // query, the visible set + ORDER come from the FTS hit list (relevance
        // ranked) — NOT the filename substring + date sort. The sidebar
        // selection still narrows the result (search-within-selection).
        let contentMode = contentSearchScope && !trimmed.isEmpty
        if contentMode {
            // Order by FTS relevance (hit order), narrowing within the current
            // sidebar selection. Files not in the local master set (uncached /
            // unindexed) are skipped — they can't be shown.
            let byID = Dictionary(masterFiles.map { ($0.id, $0) },
                                  uniquingKeysWith: { a, _ in a })
            files = contentSearchHits.compactMap { id -> PlaudFileVM? in
                guard let item = byID[id],
                      matchesSidebar(item.file, folderIDs: item.folderIDs,
                                     sidebar: sidebar)
                else { return nil }
                return item.file
            }
            return
        }

        let result = masterFiles.compactMap { item -> PlaudFileVM? in
            guard matchesSidebar(item.file, folderIDs: item.folderIDs,
                                 sidebar: sidebar) else { return nil }
            if !needle.isEmpty {
                guard (item.file.filename ?? "").lowercased().contains(needle)
                else { return nil }
            }
            return item.file
        }
        files = result
    }

    /// Whether a file matches the sidebar selection. Shared by the normal and
    /// content-search filter paths.
    private func matchesSidebar(_ file: PlaudFileVM, folderIDs: Set<String>,
                                sidebar: SidebarItem) -> Bool {
        switch sidebar {
        case .allFiles:
            return !file.isTrash
        case .unfiled:
            return !file.isTrash && folderIDs.isEmpty
        case .starred:
            return !file.isTrash && file.starred
        case .trash:
            return file.isTrash
        case .folder(let id):
            return !file.isTrash && folderIDs.contains(id)
        case .tag(let tag):
            return !file.isTrash && file.allTags.contains(tag)
        case .tagPrefix(let prefix):
            // Parent node: equal to the prefix OR a `prefix/...` descendant.
            return !file.isTrash && file.allTags.contains {
                $0 == prefix || $0.hasPrefix(prefix + "/")
            }
        }
    }

    /// React to a change in the search text OR the scope toggle. Filename mode
    /// (or an empty query) tears down any content-search state and restores the
    /// normal list immediately. Content mode debounces, then runs `plaud
    /// search` off-main with a generation guard so stale responses are dropped.
    private func onSearchInputsChanged(search: String, scope: Bool) {
        contentSearchDebounce?.cancel()
        let trimmed = search.trimmingCharacters(in: .whitespacesAndNewlines)

        guard scope, !trimmed.isEmpty else {
            // Filename mode or empty query: drop content state and re-derive.
            contentSearchGeneration &+= 1  // invalidate any in-flight query
            if !contentSearchHits.isEmpty || !contentSnippets.isEmpty
                || contentSearchRunning || !contentSearchQuery.isEmpty {
                contentSearchHits = []
                contentSnippets = [:]
                contentSearchQuery = ""
                contentSearchRunning = false
            }
            applyFilter(sidebar: sidebar, search: search)
            return
        }

        contentSearchRunning = true
        contentSearchGeneration &+= 1
        let generation = contentSearchGeneration
        contentSearchDebounce = Task { [weak self] in
            // 80ms is enough to collapse a fast typist's keystrokes now that
            // the query is a local FTS read (~2ms) rather than a `uv run`
            // subprocess (~300ms) — it used to need 250ms just to amortize
            // Python startup.
            try? await Task.sleep(nanoseconds: 80_000_000)
            if Task.isCancelled { return }
            await self?.runContentSearch(query: trimmed, generation: generation)
        }
    }

    /// Run the full-content search against local SQLite (FTS5 trigram, with
    /// a LIKE fallback for short terms) and — if this is still the newest
    /// query — publish the ranked hits.
    ///
    /// This used to shell out to `plaud search --json`. Same SQL, same
    /// ranking, minus a process spawn, a `uv` lock resolution, and a Python
    /// interpreter boot on every query.
    private func runContentSearch(query: String, generation: UInt64) async {
        let hits = await Task.detached(priority: .userInitiated) {
            Database.shared.searchContent(query)
        }.value
        // Drop stale responses: a newer keystroke/scope-change superseded us.
        guard generation == contentSearchGeneration else { return }

        var ids: [String] = []
        var snippets: [String: String] = [:]
        for hit in hits where !hit.fileID.isEmpty {
            ids.append(hit.fileID)
            if !hit.snippet.isEmpty { snippets[hit.fileID] = hit.snippet }
        }

        contentSearchHits = ids
        contentSnippets = snippets
        contentSearchQuery = query
        contentSearchRunning = false
        applyFilter(sidebar: sidebar, search: search)
    }

    func sync(showError: Bool = true) async {
        guard !isSyncing else { return }
        guard auth?.configured == true else {
            await refreshAuth()
            return
        }
        isSyncing = true
        defer { isSyncing = false }
        var synced = await runPlaud(args: ["sync"], showError: showError)

        // A sync that died on auth is exactly the case the self-heal exists
        // for, so heal and retry instead of leaving a dead-end "use the
        // toolbar auth button" alert on screen. The CLI has by now recorded
        // the server's rejection, so `refreshAuth` reports `rejected` and
        // `selfHealAuthIfNeeded` escalates to the browser re-harvest.
        if lastCommandErrorMentionsAuth {
            lastCommandError = nil
            await refreshAuth()
            if auth?.state == "valid" {
                synced = await runPlaud(args: ["sync"], showError: showError)
            }
        }

        guard synced else {
            reload()
            await refreshAuth()
            return
        }

        lastSyncedAt = Date()
        reload()
        // Cheap offline auth check after every sync — also covers launch and
        // app-activation, both of which route through `sync()`.
        await refreshAuth()
        // Passive credit indicator — throttled internally to 30 min.
        if !DistributionProfile.isCommunity {
            Task { await self.refreshElevenLabs() }
        }
        // Keep new recordings flowing to "Metadata Ready" without a manual
        // Backfill click: fetch content for uncached files, which also fires
        // the CLI's auto-metadata hook (config `auto_metadata`, on by
        // default). No-ops fast when everything is cached; skipped while a
        // manual deep sync is already running.
        if !DistributionProfile.isCommunity, !deepSyncRunning {
            Task { await self.deepSync() }
        }
    }

    /// Refresh the cached Plaud auth status via `plaud auth --json`.
    ///
    /// Offline (`live == false`) is instant — it only decodes the local JWT, no
    /// network — so it's cheap enough to fire at launch, after each sync, and on
    /// app activation. Pass `live: true` for the "Verify now" button to also ping
    /// the API. Decode failures fall back to a `state:"unknown"` value rather
    /// than crashing or clearing the indicator.
    /// Whether the last command failed for an auth reason. Matches both the
    /// raw CLI wording and the friendlier text `conciseBody` rewrites it to.
    private var lastCommandErrorMentionsAuth: Bool {
        guard let err = lastCommandError?.lowercased() else { return false }
        return err.contains("workspace token expired")
            || err.contains("plaud auth expired")
            || err.contains("-419")
            || err.contains("-420")
    }

    func refreshAuth(live: Bool = false) async {
        var args = ["auth", "--json"]
        if live { args.append("--live") }
        let output = await runPlaudOutput(args: args, showError: false)
        let trimmed = output.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let data = trimmed.data(using: .utf8), !data.isEmpty else {
            auth = AuthStatus.unknown(detail: "No output from plaud auth.")
            return
        }
        if let decoded = try? JSONDecoder().decode(AuthStatus.self, from: data) {
            auth = decoded
            await selfHealAuthIfNeeded(decoded)
        } else {
            auth = AuthStatus.unknown(
                detail: "Could not read auth status from Plaud CLI."
            )
        }
    }

    /// Minimal shape of `plaud refresh-auth --json`'s output. The credential
    /// itself is *never* in this payload — the command writes it straight to
    /// macOS Keychain after parsing the copied Plaud cURL.
    private struct RefreshAuthResult: Decodable {
        let status: String
        let detail: String?
        let autoRefreshArmed: Bool?
        let autoRefreshDetail: String?

        enum CodingKeys: String, CodingKey {
            case status
            case detail
            case autoRefreshArmed = "auto_refresh_armed"
            case autoRefreshDetail = "auto_refresh_detail"
        }
    }

    /// Refresh Plaud credentials from a Plaud API cURL. When `curlText` is nil,
    /// the CLI falls back to the macOS pasteboard for the lightweight toolbar
    /// flow. The full in-app auth sheet passes pasted text directly via stdin,
    /// so the user never needs to run terminal commands.
    ///
    /// On success we re-run a *live* `refreshAuth()` so the toolbar indicator
    /// reflects the new token, and a `sync()` so the library picks up anything
    /// that was previously blocked on auth. The token/cookie are never surfaced
    /// — we only read the `status`/`detail` fields the command prints.
    @discardableResult
    func refreshAuthCredentials(curlText: String? = nil) async -> Bool {
        guard !refreshingAuth else { return false }
        refreshingAuth = true
        lastCurlImportAutoRefreshArmed = nil
        lastCurlImportDetail = nil
        defer { refreshingAuth = false }

        let cleanCurl = curlText?.trimmingCharacters(in: .whitespacesAndNewlines)
        var args = ["refresh-auth", "--json", "--validate-live"]
        let stdinText: String?
        if let cleanCurl, !cleanCurl.isEmpty {
            args.append("--stdin")
            stdinText = cleanCurl
        } else {
            stdinText = nil
        }

        let output = await runPlaudOutput(
            args: args,
            stdin: stdinText,
            timeout: 20,
            showError: false
        )
        let trimmed = output.trimmingCharacters(in: .whitespacesAndNewlines)

        guard let data = trimmed.data(using: .utf8), !data.isEmpty,
              let result = try? JSONDecoder().decode(RefreshAuthResult.self, from: data)
        else {
            lastCommandError = trimmed.isEmpty
                ? "인증 갱신에 실패했습니다 — Plaud CLI에서 응답이 없습니다."
                : "인증 갱신 응답을 해석하지 못했습니다: \(trimmed.prefix(200))"
            return false
        }

        let detail = result.detail?.trimmingCharacters(in: .whitespacesAndNewlines)
        let detailOrNil = (detail?.isEmpty ?? true) ? nil : detail
        lastCurlImportAutoRefreshArmed = result.autoRefreshArmed
        let refreshDetail = result.autoRefreshDetail?
            .trimmingCharacters(in: .whitespacesAndNewlines)
        lastCurlImportDetail = (refreshDetail?.isEmpty == false) ? refreshDetail : nil

        switch result.status {
        case "ok":
            lastCommandError = nil
            // Update the indicator with a live check, then reload the library.
            await refreshAuth(live: true)
            await sync(showError: false)
            return true
        case "live_check_unavailable":
            // Validate-before-write keeps the previous Keychain generation
            // intact when the network cannot prove the pasted candidate.
            await refreshAuth(live: false)
            lastCommandError = detailOrNil
                ?? "Plaud 연결을 검증하지 못해 새 자격증명을 저장하지 않았습니다. 네트워크를 확인해주세요."
            return false
        case "live_auth_failed":
            // Validate-before-write leaves the previous Keychain item unchanged.
            lastCommandError = detailOrNil
                ?? "Plaud가 복사한 인증 정보를 거부했습니다. 최신 cURL을 다시 복사해주세요."
            return false
        case "clipboard_empty":
            lastCommandError = detailOrNil
                ?? "Plaud API 요청을 cURL로 복사한 뒤 다시 눌러주세요."
            return false
        case "pbpaste_missing":
            lastCommandError = detailOrNil
                ?? "macOS 클립보드를 읽을 수 없습니다. 인증 창에 Plaud cURL을 직접 붙여넣어 주세요."
            return false
        default:
            // invalid_curl | anything else.
            let suffix = detailOrNil.map { " — \($0)" } ?? ""
            lastCommandError = "인증 갱신에 실패했습니다 (\(result.status))\(suffix)"
            return false
        }
    }

    @Published var classifyRunning: Bool = false
    @Published var classifyResult: String?
    /// Decoded dry-run plans driving the preview sheet (nil = sheet closed).
    @Published var classifyPlans: [ClassifyPlan]?
    /// Exact nonce emitted by the persisted Community preview. Apply must send
    /// this value back so a newer preview cannot be approved through an older
    /// sheet.
    @Published private(set) var classifyPlanID: String?
    /// Set after a successful apply so the file list can offer an undo banner.
    /// `count` = how many recordings were actually moved.
    @Published private(set) var lastClassifyApply: (count: Int, at: Date)?
    @Published private(set) var classifyUndoNeedsRetry = false
    /// A crash-interrupted apply must first be stabilized. The next explicit
    /// action only performs that recovery; a second action performs the inverse.
    @Published private(set) var classifyApplyRecoveryRequired = false

    /// One planned classification, decoded from the active folder router.
    /// In a dry run (`moved_to == ""`) this is a proposal; the preview lets the
    /// user uncheck wrong matches before applying.
    struct ClassifyPlan: Identifiable, Hashable, Decodable {
        let fileID: String
        let title: String
        let folderName: String
        let confidence: Double
        let reason: String
        let source: String
        let error: String
        let applied: Bool
        let movedTo: String
        let planID: String

        var id: String { fileID }

        enum CodingKeys: String, CodingKey {
            case fileID = "file_id"
            case title
            case folderName = "folder_name"
            case confidence
            case reason
            case source
            case error
            case applied
            case movedTo = "moved_to"
            case planID = "plan_id"
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            self.fileID = (try? c.decode(String.self, forKey: .fileID)) ?? ""
            self.title = (try? c.decode(String.self, forKey: .title)) ?? "(untitled)"
            self.folderName = (try? c.decode(String.self, forKey: .folderName)) ?? ""
            self.confidence = (try? c.decode(Double.self, forKey: .confidence)) ?? 0
            self.reason = (try? c.decode(String.self, forKey: .reason)) ?? ""
            self.source = (try? c.decode(String.self, forKey: .source)) ?? ""
            self.error = (try? c.decode(String.self, forKey: .error)) ?? ""
            self.applied = (try? c.decode(Bool.self, forKey: .applied)) ?? false
            self.movedTo = (try? c.decode(String.self, forKey: .movedTo)) ?? ""
            self.planID = (try? c.decode(String.self, forKey: .planID)) ?? ""
        }
    }

    struct ClassifyApplySummary: Equatable {
        let movedCount: Int
        let failedCount: Int
        let warningCount: Int
        let message: String?
    }

    struct ClassifyUndoSummary: Equatable {
        let revertedCount: Int
        let retryCount: Int
        let completed: Bool
        let message: String
    }

    private struct ClassifyUndoPayload: Decodable {
        struct Failure: Decodable {
            let fileID: String
            let error: String

            enum CodingKeys: String, CodingKey {
                case fileID = "file_id"
                case error
            }
        }

        let status: String
        let detail: String?
        let reverted: Int
        let failed: [Failure]

        enum CodingKeys: String, CodingKey {
            case status, detail, reverted, failed
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            status = try c.decode(String.self, forKey: .status)
            detail = try c.decodeIfPresent(String.self, forKey: .detail)
            reverted = try c.decodeIfPresent(Int.self, forKey: .reverted) ?? 0
            failed = try c.decodeIfPresent([Failure].self, forKey: .failed) ?? []
        }
    }

    struct CommunityUndoAvailability: Equatable {
        let status: String
        let count: Int
        let detail: String
    }

    private struct CommunityUndoStatusPayload: Decodable {
        let status: String
        let count: Int
        let detail: String
    }

    nonisolated static func communityUndoAvailability(
        from output: String
    ) -> CommunityUndoAvailability? {
        guard let data = output.data(using: .utf8),
              let payload = try? JSONDecoder().decode(
                  CommunityUndoStatusPayload.self,
                  from: data
              ),
              !payload.detail.isEmpty
        else { return nil }
        switch payload.status {
        case "none" where payload.count == 0:
            break
        case "undo_available" where payload.count > 0:
            break
        case "apply_recovery_required" where payload.count > 0:
            break
        default:
            return nil
        }
        return CommunityUndoAvailability(
            status: payload.status,
            count: payload.count,
            detail: payload.detail
        )
    }

    nonisolated static func normalizedCommunityBackend(
        provider: String,
        storedBackend: String?
    ) -> String {
        if provider == "gemini" || provider == "grok" { return "api" }
        return storedBackend == "api" ? "api" : "cli"
    }

    nonisolated static func validatedCommunityPlanID(in plans: [ClassifyPlan]) -> String? {
        guard let planID = plans.first?.planID,
              isCommunityPlanID(planID),
              plans.allSatisfy({ $0.planID == planID })
        else { return nil }
        return planID
    }

    nonisolated static func communityApplyArguments(
        fileIDs: [String],
        planID: String
    ) -> [String]? {
        guard isCommunityPlanID(planID), !fileIDs.isEmpty else { return nil }
        var seen: Set<String> = []
        let selected = fileIDs.filter { !$0.isEmpty && seen.insert($0).inserted }
        guard !selected.isEmpty else { return nil }
        var args = [
            "auto-folder", "--apply", "--plan-id", planID,
            "--min-confidence", "0.6",
        ]
        args += selected.flatMap { ["--only", $0] }
        args.append("--json")
        return args
    }

    nonisolated static func communityApplySummary(
        selectedFileIDs: [String],
        expectedPlanID: String,
        results: [ClassifyPlan]
    ) -> ClassifyApplySummary? {
        var selectedSeen: Set<String> = []
        let selected = selectedFileIDs.filter {
            !$0.isEmpty && selectedSeen.insert($0).inserted
        }
        let selectedSet = Set(selected)
        let resultIDs = results.map(\.fileID)
        guard isCommunityPlanID(expectedPlanID),
              !selectedSet.isEmpty,
              results.count == selectedSet.count,
              Set(resultIDs) == selectedSet,
              Set(resultIDs).count == results.count,
              results.allSatisfy({
                  $0.planID == expectedPlanID
                      && isCommunityPlanID($0.planID)
                      && $0.applied == !$0.movedTo.isEmpty
              })
        else { return nil }

        let moved = results.filter(\.applied)
        let failed = results.filter { !$0.applied }
        let warned = moved.filter { !$0.error.isEmpty }
        var messages: [String] = []
        if !failed.isEmpty {
            let headline = moved.isEmpty
                ? "선택한 \(failed.count)개 모두 이동하지 못했습니다."
                : "\(moved.count)개는 이동했고 \(failed.count)개는 이동하지 못했습니다."
            let detailLines: [String] = failed.prefix(3).map { row -> String in
                let error: String = row.error.isEmpty ? "오류 상세 없음" : row.error
                return "\(row.title): \(error)"
            }
            let details: String = detailLines.joined(separator: "\n")
            var failureMessage = headline
            if !details.isEmpty {
                failureMessage += "\n"
                failureMessage += details
            }
            messages.append(failureMessage)
        }
        if !warned.isEmpty {
            let detailLines: [String] = warned.prefix(3).map { row -> String in
                "\(row.title): \(row.error)"
            }
            let details: String = detailLines.joined(separator: "\n")
            var warningMessage = "이동된 \(warned.count)개의 로컬 캐시 갱신에 경고가 있습니다."
            if !details.isEmpty {
                warningMessage += "\n"
                warningMessage += details
            }
            messages.append(warningMessage)
        }
        return ClassifyApplySummary(
            movedCount: moved.count,
            failedCount: failed.count,
            warningCount: warned.count,
            message: messages.isEmpty ? nil : messages.joined(separator: "\n\n")
        )
    }

    nonisolated static func communityUndoSummary(
        from output: String,
        currentCount: Int
    ) -> ClassifyUndoSummary? {
        guard let data = output.data(using: .utf8),
              let payload = try? JSONDecoder().decode(ClassifyUndoPayload.self, from: data),
              payload.reverted >= 0,
              payload.failed.allSatisfy({ !$0.fileID.isEmpty && !$0.error.isEmpty })
        else { return nil }

        switch payload.status {
        case "ok":
            guard payload.failed.isEmpty else { return nil }
            return ClassifyUndoSummary(
                revertedCount: payload.reverted,
                retryCount: 0,
                completed: true,
                message: "자동 분류 \(payload.reverted)개를 되돌렸습니다."
            )
        case "nothing":
            guard payload.reverted == 0, payload.failed.isEmpty else { return nil }
            return ClassifyUndoSummary(
                revertedCount: 0,
                retryCount: 0,
                completed: true,
                message: payload.detail ?? "되돌릴 자동 분류 기록이 없습니다."
            )
        case "partial":
            guard !payload.failed.isEmpty else { return nil }
            let retryable = payload.failed.filter {
                !$0.error.hasPrefix("local cache update failed:")
            }
            let warnings = payload.failed.count - retryable.count
            var message = "자동 분류 \(payload.reverted)개를 되돌렸습니다."
            if !retryable.isEmpty {
                message += " \(retryable.count)개는 실패해 다시 시도할 수 있습니다."
            }
            if warnings > 0 {
                message += " \(warnings)개는 로컬 캐시 갱신 경고가 있어 동기화합니다."
            }
            return ClassifyUndoSummary(
                revertedCount: payload.reverted,
                retryCount: retryable.count,
                completed: retryable.isEmpty,
                message: message
            )
        case "apply_recovery_required":
            guard payload.reverted == 0, payload.failed.isEmpty else { return nil }
            return ClassifyUndoSummary(
                revertedCount: 0,
                retryCount: max(currentCount, 1),
                completed: false,
                message: payload.detail
                    ?? "중단된 폴더 적용을 안정화했습니다. 상태를 확인한 뒤 되돌리기를 다시 누르세요."
            )
        case "error":
            guard payload.reverted == 0, payload.failed.isEmpty else { return nil }
            return ClassifyUndoSummary(
                revertedCount: 0,
                retryCount: max(currentCount, 1),
                completed: false,
                message: payload.detail ?? "자동 분류 되돌리기에 실패했습니다. 다시 시도할 수 있습니다."
            )
        default:
            return nil
        }
    }

    nonisolated private static func isCommunityPlanID(_ value: String) -> Bool {
        value.utf8.count == 32 && value.utf8.allSatisfy { byte in
            (48...57).contains(byte) || (97...102).contains(byte)
        }
    }

    func dismissClassifyPreview() {
        classifyPlans = nil
        classifyPlanID = nil
    }

    func dismissClassifyUndo() {
        lastClassifyApply = nil
        classifyUndoNeedsRetry = false
        classifyApplyRecoveryRequired = false
    }

    /// Restore the durable Community undo/recovery affordance without making a
    /// Plaud request. A malformed or unavailable status never erases a visible
    /// action; the mutating CLI remains the final fail-closed authority.
    func refreshCommunityUndoStatus() async {
        guard DistributionProfile.isCommunity else { return }
        let output = await runPlaudOutput(
            args: ["classify-undo-status", "--json"],
            showError: false
        )
        guard let availability = Self.communityUndoAvailability(
            from: output.trimmingCharacters(in: .whitespacesAndNewlines)
        ) else { return }
        switch availability.status {
        case "none":
            dismissClassifyUndo()
        case "undo_available":
            lastClassifyApply = (count: availability.count, at: Date())
            classifyUndoNeedsRetry = false
            classifyApplyRecoveryRequired = false
        case "apply_recovery_required":
            lastClassifyApply = (count: availability.count, at: Date())
            classifyUndoNeedsRetry = true
            classifyApplyRecoveryRequired = true
        default:
            break
        }
    }

    /// Run a DRY-RUN classification and
    /// publish the decoded plans into `classifyPlans` to open the preview
    /// sheet. The App's unique capability — the web client cannot do this.
    /// Nothing is moved here; the user reviews + confirms in the sheet.
    func classifyPreview() async {
        guard !classifyRunning else { return }
        if DistributionProfile.isCommunity, classifyApplyRecoveryRequired {
            classifyResult =
                "중단된 폴더 적용을 먼저 안정화하세요. 되돌리기 배너의 ‘상태 안정화’를 누르세요."
            return
        }
        classifyRunning = true
        defer { classifyRunning = false }
        dismissClassifyPreview()
        lastCommandError = nil
        let command = DistributionProfile.isCommunity ? "auto-folder" : "classify"
        var args = [command, "--json"]
        if DistributionProfile.isCommunity {
            args += ["--limit", "200", "--min-confidence", "0.6"]
            let config = Database.shared.loadAppConfig()
            let provider = config.classifyModel
            let backend = Self.normalizedCommunityBackend(
                provider: provider,
                storedBackend: config.backends[provider]
            )
            let labels = [
                "claude": "Anthropic / Claude",
                "codex": "OpenAI / Codex",
                "gemini": "Google / Gemini",
                "grok": "xAI / Grok",
            ]
            let alert = NSAlert()
            alert.messageText = "How should this folder preview run?"
            alert.informativeText =
                "Use \(labels[provider] ?? provider) (\(backend)) to arbitrate weak matches? "
                + "This may send your existing folder names plus cached recording titles, "
                + "keywords, summaries, and transcripts to that provider. Local-only mode "
                + "uses no external AI. At most 20 provider requests run per preview, and "
                + "nothing moves until you approve preview rows."
            alert.alertStyle = .informational
            alert.addButton(withTitle: "Use selected AI")
            alert.addButton(withTitle: "Local only")
            alert.addButton(withTitle: "Cancel")
            switch alert.runModal() {
            case .alertFirstButtonReturn:
                args += [
                    "--llm", "--provider", provider, "--backend", backend,
                    "--confirm-external",
                ]
            case .alertSecondButtonReturn:
                break
            default:
                return
            }
        }
        let output = await runPlaudOutput(args: args)
        let trimmed = output.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let data = trimmed.data(using: .utf8), !data.isEmpty,
              let plans = try? JSONDecoder().decode([ClassifyPlan].self, from: data)
        else {
            // Only surface an error if the command itself didn't already report
            // one (runPlaudOutput sets lastCommandError on non-zero exit).
            if lastCommandError == nil {
                classifyResult = "No recordings to classify, or the plan could not be read."
            }
            return
        }
        if DistributionProfile.isCommunity {
            guard let planID = Self.validatedCommunityPlanID(in: plans) else {
                classifyResult = plans.isEmpty
                    ? "No recordings matched a folder."
                    : "The saved folder preview identifier is missing or inconsistent. Run preview again."
                return
            }
            classifyPlanID = planID
        }
        // Only files that resolved to a folder are actionable proposals.
        let actionable = plans.filter { !$0.folderName.isEmpty }
        if actionable.isEmpty {
            classifyPlanID = nil
            classifyResult = "No recordings matched a folder."
            return
        }
        classifyPlans = actionable.sorted { $0.confidence > $1.confidence }
    }

    /// Apply classification for ONLY the given file ids (`classify --apply`
    /// with one `--only <id>` each), then sync + reload. Sets
    /// `lastClassifyApply` so the UI can offer an Undo banner.
    func applyClassify(fileIDs: [String], planID: String? = nil) async {
        guard !classifyRunning, !fileIDs.isEmpty else { return }
        classifyRunning = true
        defer { classifyRunning = false }
        let command = DistributionProfile.isCommunity ? "auto-folder" : "classify"
        let args: [String]
        if DistributionProfile.isCommunity {
            guard let planID,
                  let communityArgs = Self.communityApplyArguments(
                      fileIDs: fileIDs,
                      planID: planID
                  )
            else {
                classifyResult = "The folder preview identifier is invalid. Run preview again."
                return
            }
            args = communityArgs
        } else {
            args = [command, "--apply", "--json"]
                + fileIDs.flatMap { ["--only", $0] }
        }
        lastCommandError = nil
        let output = await runPlaudOutput(args: args)
        if DistributionProfile.isCommunity {
            // A failed/ambiguous apply may have created a durable recovery WAL
            // even when no result rows can be decoded. Restore that action now
            // instead of waiting for the next app activation.
            await refreshCommunityUndoStatus()
        }
        let trimmed = output.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let data = trimmed.data(using: .utf8), !data.isEmpty,
              let results = try? JSONDecoder().decode([ClassifyPlan].self, from: data)
        else {
            if lastCommandError == nil {
                classifyResult = "Folder move finished, but its result could not be verified."
            }
            return
        }

        let moved: Int
        if DistributionProfile.isCommunity {
            guard let planID,
                  let summary = Self.communityApplySummary(
                      selectedFileIDs: fileIDs,
                      expectedPlanID: planID,
                      results: results
                  )
            else {
                classifyResult = "Folder move returned an inconsistent result. Sync before retrying."
                return
            }
            moved = summary.movedCount
            classifyResult = summary.message
        } else {
            moved = results.filter { $0.applied || !$0.movedTo.isEmpty }.count
        }
        if moved > 0 {
            await sync(showError: false)
            lastClassifyApply = (count: moved, at: Date())
            classifyUndoNeedsRetry = false
            classifyApplyRecoveryRequired = false
        } else if lastCommandError == nil && classifyResult == nil {
            classifyResult = "Folder move finished, but its result could not be verified."
        }
    }

    /// Revert the last applied classification and retain the retry affordance
    /// whenever the CLI reports a partial or preflight failure.
    func classifyUndo() async {
        guard !classifyRunning else { return }
        classifyRunning = true
        defer { classifyRunning = false }
        lastCommandError = nil
        let currentCount = lastClassifyApply?.count ?? 0
        let output = await runPlaudOutput(args: ["classify-undo", "--json"])
        await refreshCommunityUndoStatus()
        guard let summary = Self.communityUndoSummary(
            from: output.trimmingCharacters(in: .whitespacesAndNewlines),
            currentCount: currentCount
        ) else {
            if lastClassifyApply != nil {
                classifyUndoNeedsRetry = true
            }
            if lastCommandError == nil {
                classifyResult = "되돌리기 결과를 확인할 수 없습니다. 재시도 항목은 유지됩니다."
            }
            return
        }

        // A valid structured error is more useful than the generic nonzero-exit
        // alert produced by the process wrapper.
        lastCommandError = nil
        classifyResult = summary.message
        if summary.completed {
            dismissClassifyUndo()
        } else {
            lastClassifyApply = (count: summary.retryCount, at: Date())
            classifyUndoNeedsRetry = true
        }
        if summary.revertedCount > 0 {
            await sync(showError: false)
        }
        await refreshCommunityUndoStatus()
    }

    /// Background backfill of transcript/summary cache for every file.
    /// Long-running (15+ min for 1k files); UI stays responsive.
    func deepSync() async {
        guard !deepSyncRunning else { return }
        deepSyncRunning = true
        defer { deepSyncRunning = false }
        await runPlaud(args: ["sync-content"])
        reload()
    }

    /// Export (download) the audio file. Like `exportContent`, we let the user
    /// choose the destination so they get clear feedback on where the file
    /// landed. The CLI names the file itself inside the chosen directory, so we
    /// pick a directory, capture the saved path it prints, and reveal it in
    /// Finder.
    func download(_ fileID: String) async {
        let panel = NSOpenPanel()
        panel.title = "Choose where to save the audio"
        panel.prompt = "Save Here"
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.canCreateDirectories = true
        guard panel.runModal() == .OK, let dir = panel.url else { return }

        let output = await runPlaudOutput(args: ["download", fileID, dir.path])
        reload()

        // CLI prints `ok <path>`. Reveal the saved file (or the directory) so
        // the user knows exactly where it landed.
        let savedPath = output
            .components(separatedBy: .newlines)
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .compactMap { line -> String? in
                guard let range = line.range(of: "ok ") else { return nil }
                return String(line[range.upperBound...])
                    .trimmingCharacters(in: .whitespacesAndNewlines)
            }
            .last
        let revealURL = savedPath.flatMap { $0.isEmpty ? nil : URL(fileURLWithPath: $0) } ?? dir
        NSWorkspace.shared.activateFileViewerSelecting([revealURL])
    }

    func sendToObsidian(_ fileID: String) async {
        await runPlaud(args: ["obsidian", fileID])
    }

    func addTag(_ rawTag: String, to fileID: String) async {
        let tag = rawTag.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !tag.isEmpty else { return }
        await runPlaud(args: PlaudCommandArguments.tagAdd(fileID: fileID, tag: tag))
        noteMetadata = Database.shared.noteMetadata(for: fileID)
        // Refresh the sidebar tag counts + the file's chip set.
        reload()
    }

    func removeTag(_ tag: String, from fileID: String) async {
        await runPlaud(args: PlaudCommandArguments.tagRemove(fileID: fileID, tag: tag))
        noteMetadata = Database.shared.noteMetadata(for: fileID)
        reload()
    }

    /// Pin a tag to the top of the sidebar Tags section (`plaud tag-pin`),
    /// then reload so the pinned list and counts converge.
    func pinTag(_ tag: String) async {
        await runPlaud(args: ["tag-pin", tag])
        pinnedTags = Database.shared.loadAppConfig().pinnedTags
        reload()
    }

    /// Unpin a tag (`plaud tag-pin <tag> --off`).
    func unpinTag(_ tag: String) async {
        await runPlaud(args: ["tag-pin", tag, "--off"])
        pinnedTags = Database.shared.loadAppConfig().pinnedTags
        reload()
    }

    @Published var metadataGeneratingIDs: Set<String> = []
    @Published var meetingNoteGeneratingIDs: Set<String> = []
    /// Recordings with a `plaud vault-send` in flight (drives send spinners).
    @Published var vaultSendingIDs: Set<String> = []
    /// Last successful vault-send (path + obsidian:// URL) for the feedback
    /// row in the Work Sidebar. Cleared when the selection changes.
    @Published var lastVaultSend: VaultSendOutcome?

    /// Generate metadata + auto tags. By default no `--model` is passed so the
    /// CLI resolves the configured classify model (`plaud config-classify`) —
    /// single source of truth. Pass `model` only for a deliberate UI override.
    func generateMetadata(_ fileID: String, model: String = "",
                          modelID: String = "") async {
        metadataGeneratingIDs.insert(fileID)
        defer { metadataGeneratingIDs.remove(fileID) }
        var args = ["metadata-generate", fileID]
        if !model.isEmpty {
            args += ["--model", model]
        }
        if !modelID.isEmpty {
            args += ["--model-id", modelID]
        }
        await runPlaud(args: args)
        noteMetadata = Database.shared.noteMetadata(for: fileID)
        reload()
    }

    func writeMeetingNote(_ fileID: String, model: String = "claude",
                          modelID: String = "") async {
        meetingNoteGeneratingIDs.insert(fileID)
        defer { meetingNoteGeneratingIDs.remove(fileID) }
        var args = ["meeting-note", fileID, "--model", model]
        if !modelID.isEmpty {
            args += ["--model-id", modelID]
        }
        await runPlaud(args: args)
        noteMetadata = Database.shared.noteMetadata(for: fileID)
    }

    func setUsageStatus(_ fileID: String, status: String) async {
        await runPlaud(args: ["usage-status", fileID, status])
        noteMetadata = Database.shared.noteMetadata(for: fileID)
    }

    // MARK: - Dual-transcribe pipeline

    /// Run (or resume) the dual pipeline. Long-running: ElevenLabs transcription
    /// plus one or two LLM calls — the ⚡ spinner is driven by dualRunningIDs and
    /// the stage badge refreshes from the DB row the CLI keeps updated.
    /// `speakerMap` carries names confirmed in the app's speaker sheet.
    func runDual(_ fileID: String, speakerMap: [String: String] = [:],
                 toVault: Bool = false) async {
        guard !dualRunningIDs.contains(fileID) else { return }
        dualRunningIDs.insert(fileID)
        defer { dualRunningIDs.remove(fileID) }
        var args = ["dual", fileID]
        for (speaker, name) in speakerMap.sorted(by: { $0.key < $1.key })
        where !name.trimmingCharacters(in: .whitespaces).isEmpty {
            args += ["--map", "\(speaker)=\(name)"]
        }
        if toVault { args += ["--to-vault"] }
        await runPlaud(args: args)
        refreshDual(fileID)
        reload()
        // The transcribe stage spends ElevenLabs credits.
        await refreshElevenLabs(force: true)
    }

    /// Land the vault-ready note in the vault transcript inbox lane.
    func dualSend(_ fileID: String) async {
        guard !dualRunningIDs.contains(fileID) else { return }
        dualRunningIDs.insert(fileID)
        defer { dualRunningIDs.remove(fileID) }
        await runPlaud(args: ["dual-send", fileID])
        refreshDual(fileID)
        noteMetadata = Database.shared.noteMetadata(for: fileID)
    }

    func dualUnmark(_ fileID: String) async {
        await runPlaud(args: ["dual-unmark", fileID], showError: false)
        refreshDual(fileID)
    }

    private func refreshDual(_ fileID: String) {
        guard fileID == selectedID else { return }
        dualState = Database.shared.dualState(for: fileID)
        // The pipeline may have just produced/refreshed the final artifacts.
        integratedContent = Database.shared.integratedContent(for: fileID)
    }

    /// Open a Claude Code Terminal session preloaded with this recording's
    /// context (`plaud claude`) — the CLI/MCP bridge. Empty fileID is valid
    /// for library-wide tasks like digest.
    func launchClaude(_ fileID: String, task: String, prompt: String = "") async {
        var args = ["claude"]
        if !fileID.isEmpty { args.append(fileID) }
        args += ["--task", task]
        if !prompt.isEmpty { args += ["--prompt", prompt] }
        await runPlaud(args: args)
    }

    // MARK: - Content-reuse marks

    /// Chip tap: none → flagged → drafted → published → cleared.
    func cycleReuse(_ fileID: String, channel: String) async {
        let current = reuseMarks.first { $0.channel == channel }?.status
        switch current {
        case nil:
            await runPlaud(args: ["reuse", fileID, channel])
        case "flagged":
            await runPlaud(args: ["reuse", fileID, channel, "--status", "drafted"])
        case "drafted":
            await runPlaud(args: ["reuse", fileID, channel, "--status", "published"])
        default:
            await runPlaud(args: ["reuse", fileID, channel, "--clear"])
        }
        if fileID == selectedID {
            reuseMarks = Database.shared.reuseMarks(for: fileID)
        }
    }

    func setReuseNote(_ fileID: String, channel: String, note: String) async {
        let status = reuseMarks.first { $0.channel == channel }?.status ?? "flagged"
        await runPlaud(args: ["reuse", fileID, channel, "--status", status, "--note", note])
        if fileID == selectedID {
            reuseMarks = Database.shared.reuseMarks(for: fileID)
        }
    }

    /// Force a re-fetch of file detail (transcript + summary + folder ids)
    /// from Plaud, bypassing the local cache. Used by the title refresh button.
    func refetchDetail(_ fileID: String) async {
        await runPlaud(args: ["detail", fileID])
        if selectedID == fileID {
            content = Database.shared.content(for: fileID)
        }
    }

    func deleteFolder(_ folderID: String) async {
        await runPlaud(args: ["folder-delete", folderID])
        await sync(showError: false)
    }

    func createFolder(name: String) async {
        await runPlaud(args: ["folder-create", name])
        await sync(showError: false)
    }

    func updateFolder(_ folderID: String, name: String?, color: String?, icon: String?) async {
        var args = ["folder-rename", folderID]
        if let n = name { args += ["--name", n] }
        if let c = color { args += ["--color", c] }
        if let i = icon { args += ["--icon", i] }
        await runPlaud(args: args)
        await sync(showError: false)
    }

    /// Assign a file to at most one folder, replacing any existing assignment
    /// (`nil` = clear → Unfiled). Plaud Web supports only a single folder per
    /// file — multiple assignments break their UI — so the UI exposes radio
    /// semantics and this method is the only write path.
    /// Optimistic: writes locally + reloads UI immediately, then fires the
    /// API call in the background so the user never waits on the network.
    func assignFolder(_ fileID: String, folderID: String?) {
        let folderIDs = folderID.map { [$0] } ?? []
        if let error = Database.shared.writeFileFolders(fileID, folderIDs: folderIDs) {
            // Local write failed — surface it and reconcile the UI back to the
            // last persisted state instead of showing a phantom optimistic move.
            lastCommandError = "Could not update folder locally: \(error.localizedDescription)"
            reload()
            return
        }
        reload()
        Task.detached(priority: .userInitiated) { [weak self] in
            var args = ["move", fileID]
            if let folderID {
                args.append(folderID)
            }
            await self?.runPlaud(args: args)
        }
    }

    /// Folder ids the file currently belongs to, from the in-memory master
    /// list. Used by the "Move to folder" menus to place the radio checkmark.
    /// Files carry at most one folder going forward, but old data may briefly
    /// still hold several — hence a set.
    func folderIDs(for fileID: String) -> Set<String> {
        masterFiles.first { $0.id == fileID }?.folderIDs ?? []
    }

    /// Rename a recording. Optimistic: writes the new name locally + reloads
    /// the UI immediately, then fires `plaud rename` in the background.
    func renameFile(_ fileID: String, to name: String) {
        let trimmed = name.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        if let error = Database.shared.writeFileName(fileID, name: trimmed) {
            lastCommandError = "Could not rename locally: \(error.localizedDescription)"
            reload()
            return
        }
        reload()
        Task.detached(priority: .userInitiated) { [weak self] in
            await self?.runPlaud(args: ["rename", fileID, trimmed])
        }
    }

    /// Mark a recording as seen on open. Optimistic: writes `seen_at` locally
    /// (LOCAL-ONLY column, no server call) and flips the in-memory row so the
    /// unread dot goes green -> gray in the same render pass; the DB-watcher
    /// reload then converges on the same state.
    func markSeen(_ fileID: String) {
        guard let idx = masterFiles.firstIndex(where: { $0.id == fileID }),
              masterFiles[idx].file.seenAt == nil else { return }
        guard Database.shared.markSeen(fileID: fileID) == nil else { return }
        masterFiles[idx].file.seenAt = Int64(Date().timeIntervalSince1970)
        applyFilter(sidebar: sidebar, search: search)
    }

    /// Toggle the LOCAL-ONLY star flag. Optimistic: writes locally and flips
    /// the in-memory row + starred count immediately; no server call needed
    /// (mirrors `plaud star <id> [--off]` on the same SQLite rows).
    func toggleStar(_ fileID: String) {
        guard let idx = masterFiles.firstIndex(where: { $0.id == fileID }) else { return }
        let newValue = !masterFiles[idx].file.starred
        if let error = Database.shared.setStarred(fileID: fileID, starred: newValue) {
            lastCommandError = "Could not update star locally: \(error.localizedDescription)"
            reload()
            return
        }
        masterFiles[idx].file.starred = newValue
        if !masterFiles[idx].file.isTrash {
            categoryCounts.starred = max(0, categoryCounts.starred + (newValue ? 1 : -1))
        }
        applyFilter(sidebar: sidebar, search: search)
    }

    func plaudWebURL(_ fileID: String) -> URL? {
        URL(string: "https://web.plaud.ai/file/\(fileID)")
    }

    func openInPlaudWeb(_ fileID: String) {
        guard let url = plaudWebURL(fileID) else { return }
        NSWorkspace.shared.open(url)
    }

    func copyPlaudWebURL(_ fileID: String) {
        guard let url = plaudWebURL(fileID) else { return }
        let pb = NSPasteboard.general
        pb.clearContents()
        pb.setString(url.absoluteString, forType: .string)
    }

    @Published var cmdsTranscript: String?
    @Published var transcribingIDs: Set<String> = []
    @Published var audioURL: URL?

    nonisolated static func reserveTranscription(
        _ fileID: String,
        in activeFileIDs: inout Set<String>
    ) -> Bool {
        activeFileIDs.insert(fileID).inserted
    }

    func reloadCmdsTranscript() {
        cmdsTranscript = selectedID.flatMap { Database.shared.cmdsTranscript(for: $0) }
    }

    func transcribeWithElevenLabs(_ fileID: String, numSpeakers: Int = 0) async {
        guard Self.reserveTranscription(fileID, in: &transcribingIDs) else { return }
        defer { transcribingIDs.remove(fileID) }
        let replacingExisting = Database.shared.cmdsTranscriptExists(for: fileID)
        var retryOutcomeUnknown = false
        if DistributionProfile.isCommunity {
            let statusOutput = await runPlaudOutput(
                args: ["elevenlabs-attempt-status", fileID, "--json"],
                showError: false
            )
            guard let retryState = Self.communityElevenLabsRetryState(
                from: statusOutput.trimmingCharacters(in: .whitespacesAndNewlines)
            ) else {
                lastCommandError =
                    "이전 ElevenLabs 업로드 상태를 안전하게 확인하지 못했습니다. 오디오는 전송하지 않았습니다."
                return
            }
            retryOutcomeUnknown = retryState == .outcomeUnknown
            let alert = NSAlert()
            if retryOutcomeUnknown {
                alert.messageText = "이전 ElevenLabs 업로드 결과를 확인할 수 없습니다"
                alert.informativeText =
                    "이전 요청이 ElevenLabs에 도달했을 수 있습니다. 다시 업로드하면 같은 오디오가 "
                    + "두 번 처리되어 비용이 중복 청구될 수 있습니다. 이 위험을 이해한 경우에만 "
                    + "명시적으로 재시도하세요. 결과는 이 앱에 로컬로 저장됩니다."
            } else {
                alert.messageText = replacingExisting
                    ? "Upload and replace the ElevenLabs transcript?"
                    : "Upload this recording to ElevenLabs?"
                alert.informativeText =
                    "ElevenLabs transcription sends this recording's audio to ElevenLabs "
                    + "and may use paid credits. "
                    + (replacingExisting ? "This uploads and bills again. " : "")
                    + "The local result stays in this app."
            }
            alert.alertStyle = .warning
            alert.addButton(
                withTitle: retryOutcomeUnknown
                    ? "위험을 이해하고 다시 업로드"
                    : "Upload and transcribe"
            )
            alert.addButton(withTitle: "Cancel")
            guard alert.runModal() == .alertFirstButtonReturn else { return }
        }
        var args = DistributionProfile.isCommunity
            ? Self.communityElevenLabsArguments(
                fileID: fileID,
                numSpeakers: numSpeakers,
                replacingExisting: replacingExisting,
                retryOutcomeUnknown: retryOutcomeUnknown
            )
            : ["cmds-transcribe", fileID]
        if !DistributionProfile.isCommunity, numSpeakers > 0 {
            args += ["--num-speakers", String(numSpeakers)]
        }
        _ = await runPlaudOutput(args: args)
        reloadCmdsTranscript()
        reload()
        // The private edition retains its passive credit indicator; Community
        // only contacts ElevenLabs for an explicitly confirmed transcription.
        if !DistributionProfile.isCommunity {
            await refreshElevenLabs(force: true)
        }
    }

    func relabelCmdsSpeakers(_ fileID: String, mapping: [String: String],
                             startSec: Double? = nil, endSec: Double? = nil) async {
        var args = ["cmds-relabel", fileID]
        for (k, v) in mapping where !v.isEmpty {
            args.append("\(k)=\(v)")
        }
        if let s = startSec { args += ["--start", String(s)] }
        if let e = endSec { args += ["--end", String(e)] }
        await runPlaud(args: args)
        reloadCmdsTranscript()
    }

    /// File IDs with an in-flight Plaud *server* speaker relabel. Drives the
    /// progress state in the rename-speakers sheet.
    @Published var plaudRelabelingIDs: Set<String> = []

    /// Rename speakers in the Plaud SERVER transcript via
    /// `plaud plaud-relabel <id> OLD=NEW …` (the CLI refreshes the local
    /// cache), then re-fetch detail so the UI shows the renamed labels.
    func relabelPlaudSpeakers(_ fileID: String, mapping: [String: String]) async {
        let pairs = mapping.compactMap { old, new -> String? in
            let trimmed = new.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !trimmed.isEmpty, trimmed != old else { return nil }
            return "\(old)=\(trimmed)"
        }
        guard !pairs.isEmpty else { return }
        plaudRelabelingIDs.insert(fileID)
        defer { plaudRelabelingIDs.remove(fileID) }
        await runPlaud(args: ["plaud-relabel", fileID] + pairs.sorted())
        await refetchDetail(fileID)
    }

    func addSpeaker(name: String, isSelf: Bool) async {
        var args = ["speaker-add", name]
        if isSelf { args.append("--self") }
        await runPlaud(args: args)
    }

    func deleteSpeaker(_ id: Int64) async {
        await runPlaud(args: ["speaker-delete", String(id)])
    }

    @Published var summarizingKeys: Set<String> = []

    func summarize(fileID: String, model: String, modelID: String = "", template: String) async {
        let outputModel = modelID.isEmpty ? model : modelID
        let key = "\(fileID)::\(model)::\(outputModel)::\(template)"
        summarizingKeys.insert(key)
        defer { summarizingKeys.remove(key) }
        var args = [
            "cmds-summarize", fileID,
            "--model", model,
            "--template", template,
        ]
        if !modelID.isEmpty {
            args += ["--model-id", modelID]
        }
        await runPlaud(args: args)
    }

    /// Run the integrated pipeline (final transcript + summary) for one slot.
    func integrate(fileID: String, model: String, modelID: String = "", template: String) async {
        let outputModel = modelID.isEmpty ? model : modelID
        let key = "\(fileID)::\(model)::\(outputModel)::\(template)"
        summarizingKeys.insert(key)
        defer { summarizingKeys.remove(key) }
        var args = [
            "cmds-integrate", fileID,
            "--model", model,
            "--template", template,
        ]
        if !modelID.isEmpty {
            args += ["--model-id", modelID]
        }
        await runPlaud(args: args)
    }

    func addSlot(name: String, model: String, modelID: String = "", template: String) async {
        var args = ["slot-add", name, model, template]
        if !modelID.isEmpty {
            args += ["--model-id", modelID]
        }
        await runPlaud(args: args)
    }

    func deleteSlot(_ name: String) async {
        await runPlaud(args: ["slot-delete", name])
    }

    func openProjectFolder(_ subpath: String) {
        NSWorkspace.shared.open(RuntimePaths.resolve(subpath: subpath))
    }

    func openPath(_ path: String) {
        NSWorkspace.shared.open(URL(fileURLWithPath: path))
    }

    func setBackend(_ model: String, _ backend: String) async {
        await runPlaud(args: ["config-backend", model, backend])
    }

    func setModelId(_ model: String, _ id: String) async {
        await runPlaud(args: ["config-model", model, id])
    }

    func setPath(_ kind: String, _ path: String) async {
        await runPlaud(args: ["config-path", kind, path])
    }

    /// Set the default auto-classify / metadata-generate model. Routed through
    /// the CLI (`plaud config-classify`) so `data/config.json` is rewritten by
    /// the Python side, preserving unknown keys.
    func setClassifyModel(_ model: String) async {
        await runPlaud(args: ["config-classify", model])
    }

    private struct SecretStatusPayload: Decodable {
        let configured: Bool?
        let status: String?
    }

    /// Query only whether a provider key exists. The CLI never returns the
    /// secret itself, and each provider has its own OS-protected entry.
    func providerKeyConfigured(_ provider: String) async -> Bool? {
        let output = await runPlaudOutput(
            args: ["provider-key-status", secretProviderName(provider), "--json"],
            showError: false
        )
        guard let data = output.data(using: .utf8),
              let payload = try? JSONDecoder().decode(SecretStatusPayload.self, from: data)
        else { return nil }
        return payload.configured
    }

    @discardableResult
    func saveProviderKey(_ provider: String, value: String) async -> Bool {
        let key = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !key.isEmpty else {
            lastCommandError = "Enter an API key before saving."
            return false
        }
        lastCommandError = nil
        let output = await runPlaudOutput(
            args: ["provider-key-set", secretProviderName(provider)],
            stdin: key + "\n",
            timeout: 20
        )
        return commandReportedSuccess(output)
    }

    @discardableResult
    func deleteProviderKey(_ provider: String) async -> Bool {
        lastCommandError = nil
        let output = await runPlaudOutput(
            args: ["provider-key-delete", secretProviderName(provider)], timeout: 20
        )
        return commandReportedSuccess(output)
    }

    func elevenLabsKeyConfigured() async -> Bool? {
        let output = await runPlaudOutput(
            args: ["provider-key-status", "elevenlabs", "--json"], showError: false
        )
        guard let data = output.data(using: .utf8),
              let payload = try? JSONDecoder().decode(SecretStatusPayload.self, from: data)
        else { return nil }
        return payload.configured
    }

    @discardableResult
    func saveElevenLabsKey(_ value: String) async -> Bool {
        let key = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !key.isEmpty else {
            lastCommandError = "Enter an ElevenLabs API key before saving."
            return false
        }
        lastCommandError = nil
        let output = await runPlaudOutput(
            args: ["provider-key-set", "elevenlabs"],
            stdin: key + "\n",
            timeout: 20
        )
        return commandReportedSuccess(output)
    }

    @discardableResult
    func deleteElevenLabsKey() async -> Bool {
        lastCommandError = nil
        let output = await runPlaudOutput(
            args: ["provider-key-delete", "elevenlabs"], timeout: 20
        )
        return commandReportedSuccess(output)
    }

    private func commandReportedSuccess(_ output: String) -> Bool {
        guard let data = output.data(using: .utf8),
              let payload = try? JSONDecoder().decode(SecretStatusPayload.self, from: data)
        else { return lastCommandError == nil && !output.isEmpty }
        return payload.status.map { ["ok", "saved", "deleted"].contains($0) }
            ?? (payload.configured != nil)
    }

    private func secretProviderName(_ provider: String) -> String {
        ["claude": "anthropic", "codex": "openai"][provider] ?? provider
    }

    /// Set the default metadata-generate model (codex = GPT via the Codex CLI
    /// subscription login). Same CLI-routed write as setClassifyModel.
    func setMetadataModel(_ model: String) async {
        await runPlaud(args: ["config-metadata-model", model])
    }

    /// Toggle automatic metadata generation after sync.
    func setAutoMetadata(_ enabled: Bool) async {
        await runPlaud(args: ["config-auto-metadata", enabled ? "on" : "off"])
    }

    /// Resolve a fresh signed audio URL via the CLI and stash it for AVPlayer.
    /// FileStore is already `@MainActor`, so we assign directly (no extra hop),
    /// and validate the CLI output before trusting it as a streamable URL.
    func loadAudioURL(_ fileID: String) async {
        let output = await runPlaudOutput(args: ["audio-url", fileID])
        let trimmed = output.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty,
              let url = URL(string: trimmed),
              let scheme = url.scheme?.lowercased(),
              scheme == "http" || scheme == "https"
        else {
            lastCommandError = "Could not load audio URL from Plaud."
            return
        }
        audioURL = url
    }

    nonisolated private static func posixError(_ code: Int32) -> NSError {
        NSError(domain: NSPOSIXErrorDomain, code: Int(code))
    }

    nonisolated private static func requirePOSIXSuccess(_ code: Int32) throws {
        if code != 0 { throw posixError(code) }
    }

    nonisolated private static func withCStringArray<Result>(
        _ strings: [String],
        body: (UnsafeMutablePointer<UnsafeMutablePointer<CChar>?>) throws -> Result
    ) throws -> Result {
        guard strings.allSatisfy({ !$0.utf8.contains(0) }) else {
            throw posixError(EINVAL)
        }
        var pointers: [UnsafeMutablePointer<CChar>?] = []
        defer { pointers.compactMap { $0 }.forEach { free($0) } }
        for string in strings {
            guard let pointer = strdup(string) else { throw posixError(ENOMEM) }
            pointers.append(pointer)
        }
        pointers.append(nil)
        return try pointers.withUnsafeMutableBufferPointer { buffer in
            guard let baseAddress = buffer.baseAddress else { throw posixError(ENOMEM) }
            return try body(baseAddress)
        }
    }

    /// Spawn the configured executable as leader of a new process group. A
    /// timeout can therefore terminate provider helpers that inherited its
    /// stdout/stderr pipes, not just the immediate Python/uv child.
    nonisolated private static func spawnOwnedProcessGroup(
        _ process: Process,
        stdout: Pipe,
        stderr: Pipe,
        input: Pipe?
    ) throws -> pid_t {
        guard let executable = process.executableURL?.path, !executable.isEmpty else {
            throw posixError(EINVAL)
        }

        var actions: posix_spawn_file_actions_t?
        try requirePOSIXSuccess(posix_spawn_file_actions_init(&actions))
        defer { posix_spawn_file_actions_destroy(&actions) }

        let stdoutRead = stdout.fileHandleForReading.fileDescriptor
        let stdoutWrite = stdout.fileHandleForWriting.fileDescriptor
        let stderrRead = stderr.fileHandleForReading.fileDescriptor
        let stderrWrite = stderr.fileHandleForWriting.fileDescriptor
        let nullInput: Int32?
        let inputRead: Int32
        if let input {
            nullInput = nil
            inputRead = input.fileHandleForReading.fileDescriptor
        } else {
            let descriptor = Darwin.open("/dev/null", O_RDONLY | O_CLOEXEC)
            if descriptor == -1 { throw posixError(errno) }
            nullInput = descriptor
            inputRead = descriptor
        }
        defer {
            if let nullInput { _ = Darwin.close(nullInput) }
        }
        let inputWrite = input?.fileHandleForWriting.fileDescriptor

        // Duplicate every mapping source above the standard-descriptor range.
        // If the parent was launched with fd 0/1/2 closed, Pipe may reuse those
        // numbers; direct dup2/addclose actions would then overwrite or close a
        // different standard stream depending on action order.
        var safeSources: [Int32] = []
        defer { safeSources.forEach { _ = Darwin.close($0) } }
        for descriptor in [inputRead, stdoutWrite, stderrWrite] {
            let duplicate = Darwin.fcntl(descriptor, F_DUPFD_CLOEXEC, STDERR_FILENO + 1)
            if duplicate == -1 { throw posixError(errno) }
            safeSources.append(duplicate)
        }
        // CLOEXEC_DEFAULT is applied before the ordered file actions.  Mark
        // the temporary dup2 sources as explicit inheritance inputs so they
        // still exist when the dup2 actions run; the close actions below then
        // remove the temporary descriptors from the final child image.
        for descriptor in safeSources {
            try requirePOSIXSuccess(
                posix_spawn_file_actions_addinherit_np(&actions, descriptor)
            )
        }
        try requirePOSIXSuccess(
            posix_spawn_file_actions_adddup2(&actions, safeSources[0], STDIN_FILENO)
        )
        try requirePOSIXSuccess(
            posix_spawn_file_actions_adddup2(&actions, safeSources[1], STDOUT_FILENO)
        )
        try requirePOSIXSuccess(
            posix_spawn_file_actions_adddup2(&actions, safeSources[2], STDERR_FILENO)
        )

        let originalDescriptors = Set(
            [stdoutRead, stdoutWrite, stderrRead, stderrWrite, inputRead, inputWrite]
                .compactMap { $0 }
        )
        for descriptor in originalDescriptors where descriptor > STDERR_FILENO {
            try requirePOSIXSuccess(posix_spawn_file_actions_addclose(&actions, descriptor))
        }
        for descriptor in safeSources {
            try requirePOSIXSuccess(posix_spawn_file_actions_addclose(&actions, descriptor))
        }
        if let directory = process.currentDirectoryURL {
            let status = directory.path.withCString { path in
                if #available(macOS 26.0, *) {
                    posix_spawn_file_actions_addchdir(&actions, path)
                } else {
                    posix_spawn_file_actions_addchdir_np(&actions, path)
                }
            }
            try requirePOSIXSuccess(status)
        }

        var attributes: posix_spawnattr_t?
        try requirePOSIXSuccess(posix_spawnattr_init(&attributes))
        defer { posix_spawnattr_destroy(&attributes) }
        // Close every descriptor except mappings explicitly installed by the
        // file actions above. The app may hold SQLite, audio, or credential
        // descriptors that must never leak into a provider CLI.
        let flags = Int16(POSIX_SPAWN_SETPGROUP | POSIX_SPAWN_CLOEXEC_DEFAULT)
        try requirePOSIXSuccess(posix_spawnattr_setflags(&attributes, flags))
        // A zero pgroup makes the spawned pid the new group id.
        try requirePOSIXSuccess(posix_spawnattr_setpgroup(&attributes, 0))

        let arguments = [executable] + (process.arguments ?? [])
        let environment = (process.environment ?? ProcessInfo.processInfo.environment)
            .map { "\($0.key)=\($0.value)" }
            .sorted()
        var childPID: pid_t = 0
        let spawnStatus = try executable.withCString { executablePointer in
            try withCStringArray(arguments) { argumentPointers in
                try withCStringArray(environment) { environmentPointers in
                    posix_spawn(
                        &childPID,
                        executablePointer,
                        &actions,
                        &attributes,
                        argumentPointers,
                        environmentPointers
                    )
                }
            }
        }
        try requirePOSIXSuccess(spawnStatus)
        return childPID
    }

    nonisolated private static func waitForChild(_ pid: pid_t) throws -> Int32 {
        var status: Int32 = 0
        while true {
            let result = Darwin.waitpid(pid, &status, 0)
            if result == pid { return status }
            if result == -1, errno == EINTR { continue }
            throw posixError(errno)
        }
    }

    nonisolated private static func exitCode(fromWaitStatus status: Int32) -> Int32 {
        let signal = status & 0x7f
        return signal == 0 ? (status >> 8) & 0xff : signal
    }

    nonisolated private static func ownedProcessGroupExists(_ processGroup: pid_t) -> Bool {
        if Darwin.kill(-processGroup, 0) == 0 { return true }
        return errno == EPERM
    }

    /// Terminate only the process group created by `spawnOwnedProcessGroup`.
    /// The zero-signal probe avoids sending a later signal once that group no
    /// longer exists; while descendants remain, the group id remains owned by
    /// this invocation even after its leader has exited.
    nonisolated private static func terminateOwnedProcessGroup(
        _ processGroup: pid_t,
        grace: TimeInterval,
        timeoutState: ProcessTimeoutState
    ) {
        guard ownedProcessGroupExists(processGroup) else { return }
        _ = Darwin.kill(-processGroup, SIGTERM)
        if grace > 0 {
            Thread.sleep(forTimeInterval: grace)
        }
        guard ownedProcessGroupExists(processGroup) else { return }
        if Darwin.kill(-processGroup, SIGKILL) == 0 {
            timeoutState.markForceKilled()
        }
    }

    /// Run a configured process while draining stdout and stderr concurrently.
    /// Reading only after child exit can deadlock once either pipe fills. The
    /// process group and bounded drain fallback also prevent descendants from
    /// holding the app open after a timeout.
    nonisolated static func executeAndCapture(
        _ process: Process,
        stdin: String? = nil,
        timeout: TimeInterval? = nil,
        terminationGrace: TimeInterval = 0.5,
        drainGrace: TimeInterval = 1.0
    ) throws -> CommandResult {
        let boundedTerminationGrace = min(max(terminationGrace, 0), 2)
        let boundedDrainGrace = min(max(drainGrace, 0), 2)
        let stdout = Pipe()
        let stderr = Pipe()
        let input = stdin.map { _ in Pipe() }
        let childPID: pid_t
        do {
            childPID = try spawnOwnedProcessGroup(
                process,
                stdout: stdout,
                stderr: stderr,
                input: input
            )
        } catch {
            try? stdout.fileHandleForReading.close()
            try? stdout.fileHandleForWriting.close()
            try? stderr.fileHandleForReading.close()
            try? stderr.fileHandleForWriting.close()
            try? input?.fileHandleForReading.close()
            try? input?.fileHandleForWriting.close()
            throw error
        }

        let stdoutCapture = ProcessCaptureBuffer()
        let stderrCapture = ProcessCaptureBuffer()
        let drainGroup = DispatchGroup()
        drainGroup.enter()
        DispatchQueue.global(qos: .userInitiated).async {
            defer { drainGroup.leave() }
            stdoutCapture.replace(with: stdout.fileHandleForReading.readDataToEndOfFile())
        }
        drainGroup.enter()
        DispatchQueue.global(qos: .userInitiated).async {
            defer { drainGroup.leave() }
            stderrCapture.replace(with: stderr.fileHandleForReading.readDataToEndOfFile())
        }
        try? stdout.fileHandleForWriting.close()
        try? stderr.fileHandleForWriting.close()
        try? input?.fileHandleForReading.close()

        let timeoutState = ProcessTimeoutState()
        let timeoutCompletion = DispatchSemaphore(value: 0)
        let watchdog: DispatchWorkItem?
        if let timeout {
            let work = DispatchWorkItem {
                defer { timeoutCompletion.signal() }
                guard timeoutState.beginTimeout() else { return }
                terminateOwnedProcessGroup(
                    childPID,
                    grace: boundedTerminationGrace,
                    timeoutState: timeoutState
                )
            }
            DispatchQueue.global().asyncAfter(deadline: .now() + timeout, execute: work)
            watchdog = work
        } else {
            watchdog = nil
        }

        var inputError: Error?
        if let stdin, let input {
            do {
                try input.fileHandleForWriting.write(contentsOf: Data(stdin.utf8))
            } catch {
                inputError = error
                _ = Darwin.kill(-childPID, SIGKILL)
            }
            try? input.fileHandleForWriting.close()
        }

        let waitStatus = try waitForChild(childPID)
        timeoutState.markCompleted()
        watchdog?.cancel()
        if timeoutState.timedOut {
            _ = timeoutCompletion.wait(
                timeout: .now() + boundedTerminationGrace + 0.25
            )
        }
        let drainTimedOut = drainGroup.wait(timeout: .now() + boundedDrainGrace) == .timedOut
        if drainTimedOut {
            // A successful leader can still daemonize a descendant that keeps
            // these pipes open. Clean the group synchronously while it is
            // still attributable to this invocation, then fail closed instead
            // of accepting truncated output as a successful command.
            terminateOwnedProcessGroup(
                childPID,
                grace: boundedTerminationGrace,
                timeoutState: timeoutState
            )
            try? stdout.fileHandleForReading.close()
            try? stderr.fileHandleForReading.close()
            _ = drainGroup.wait(timeout: .now() + 0.25)
        }
        let outData = stdoutCapture.snapshot()
        let errData = stderrCapture.snapshot()
        if timeoutState.timedOut {
            return CommandResult(
                exitCode: -1,
                stdout: String(data: outData, encoding: .utf8) ?? "",
                stderr: "plaud command timed out after \(Int(timeout ?? 0))s",
                forceKilled: timeoutState.forceKilled
            )
        }
        if drainTimedOut {
            return CommandResult(
                exitCode: -1,
                stdout: String(data: outData, encoding: .utf8) ?? "",
                stderr: "plaud command left a helper process running",
                forceKilled: timeoutState.forceKilled
            )
        }
        if let inputError {
            throw inputError
        }
        return CommandResult(
            exitCode: exitCode(fromWaitStatus: waitStatus),
            stdout: String(data: outData, encoding: .utf8) ?? "",
            stderr: String(data: errData, encoding: .utf8) ?? ""
        )
    }

    /// Shell out to `uv run plaud …` and return stdout. `stdin` is streamed
    /// through an anonymous pipe and is never added to argv or a temporary file.
    func runPlaudOutput(
        args: [String],
        stdin: String? = nil,
        timeout: TimeInterval? = nil,
        showError: Bool = true
    ) async -> String {
        let result = await Task.detached(priority: .userInitiated) { () -> CommandResult in
            do {
                let process = try RuntimePaths.makePlaudProcess(args: args)
                return try Self.executeAndCapture(process, stdin: stdin, timeout: timeout)
            } catch {
                return CommandResult(
                    exitCode: -1,
                    stdout: "",
                    stderr: "plaud CLI failed: \(error.localizedDescription)"
                )
            }
        }.value
        if !result.ok && showError {
            lastCommandError = result.failureMessage
        }
        return result.stdout
    }

    func copyToClipboard(_ text: String) {
        let pb = NSPasteboard.general
        pb.clearContents()
        pb.setString(text, forType: .string)
    }

    func exportContent(_ fileID: String, kind: String) async {
        let panel = NSSavePanel()
        panel.nameFieldStringValue = "\(fileID)-\(kind).md"
        guard panel.runModal() == .OK, let url = panel.url else { return }
        await runPlaud(args: ["export", fileID, kind, "--out", url.path])
    }

    /// Re-trigger the content load for the current selection — used by the
    /// detail pane's Retry button after a background `detail` fetch failed.
    func retryContentFetch() {
        loadContent(for: selectedID)
    }

    /// Everything the detail pane needs for one recording, fetched together
    /// off the main actor. Bundling avoids five separate hops and lets one
    /// generation check drop the whole stale set at once.
    private struct DetailBundle {
        let metadata: NoteMetadataVM?
        let dual: DualStateVM?
        let reuse: [ReuseMarkVM]
        let integrated: IntegratedContentVM?
        let content: FileContentVM?
        let transcript: String?
        let stage: PipelineStage
    }

    /// Load the detail pane for `id`.
    ///
    /// Everything here — five SQLite reads, a ~166KB transcript JSON decode,
    /// and a `data/integrated/` directory scan for the pipeline stage — used
    /// to run synchronously on the main actor on every selection change AND
    /// on every `reload()` (which the 1s DB watcher fires throughout a sync).
    /// It now runs on a detached task; only the decoded value types are
    /// published back. `contentGeneration` drops results from a selection the
    /// user has already moved off of.
    private func loadContent(for id: String?) {
        contentGeneration &+= 1
        let generation = contentGeneration
        guard let id else {
            content = nil
            noteMetadata = nil
            dualState = nil
            reuseMarks = []
            integratedContent = nil
            cmdsTranscript = nil
            selectedStage = .new
            return
        }
        let hasContent = masterFiles.first { $0.id == id }?.file.hasContent ?? false

        Task.detached(priority: .userInitiated) { [weak self] in
            let bundle = DetailBundle(
                metadata: Database.shared.noteMetadata(for: id),
                dual: Database.shared.dualState(for: id),
                reuse: Database.shared.reuseMarks(for: id),
                integrated: Database.shared.integratedContent(for: id),
                content: Database.shared.content(for: id),
                transcript: Database.shared.cmdsTranscript(for: id),
                stage: PipelineStage.derive(fileID: id, hasContent: hasContent)
            )
            guard let self else { return }
            await MainActor.run {
                guard generation == self.contentGeneration else { return }
                self.noteMetadata = bundle.metadata
                self.dualState = bundle.dual
                self.reuseMarks = bundle.reuse
                self.integratedContent = bundle.integrated
                self.content = bundle.content
                self.cmdsTranscript = bundle.transcript
                self.selectedStage = bundle.stage
                if bundle.content == nil && !self.pendingDetailFetch.contains(id) {
                    self.pendingDetailFetch.insert(id)
                    Task {
                        await self.runPlaud(args: ["detail", id], showError: false)
                        self.pendingDetailFetch.remove(id)
                        guard generation == self.contentGeneration else { return }
                        self.content = Database.shared.content(for: id)
                    }
                }
            }
        }
    }

    @discardableResult
    private func runPlaud(args: [String], showError: Bool = true) async -> Bool {
        let result = await Task.detached(priority: .userInitiated) { () -> CommandResult in
            do {
                let process = try RuntimePaths.makePlaudProcess(args: args)
                return try Self.executeAndCapture(process)
            } catch {
                return CommandResult(
                    exitCode: -1,
                    stdout: "",
                    stderr: "plaud CLI failed: \(error.localizedDescription)"
                )
            }
        }.value
        if !result.ok && showError {
            lastCommandError = result.failureMessage
        }
        return result.ok
    }
}

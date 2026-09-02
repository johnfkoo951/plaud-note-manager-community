import AppKit
import Combine
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

    private struct CommandResult {
        let exitCode: Int32
        let stdout: String
        let stderr: String

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

        switch result.status {
        case "ok":
            lastCommandError = nil
            // Update the indicator with a live check, then reload the library.
            await refreshAuth(live: true)
            await sync(showError: false)
            return true
        case "live_check_unavailable":
            // The capture was saved, but an outage is not proof of a working
            // connection. Keep the sheet open and let the user retry.
            await refreshAuth(live: false)
            lastCommandError = detailOrNil
                ?? "자격증명은 저장했지만 Plaud 연결을 검증하지 못했습니다. 네트워크를 확인해주세요."
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
    /// Set after a successful apply so the file list can offer an undo banner.
    /// `count` = how many recordings were actually moved.
    @Published var lastClassifyApply: (count: Int, at: Date)?

    /// One planned classification, decoded from `plaud classify --json`.
    /// In a dry run (`moved_to == ""`) this is a proposal; the preview lets the
    /// user uncheck wrong matches before applying.
    struct ClassifyPlan: Identifiable, Hashable, Decodable {
        let fileID: String
        let title: String
        let folderName: String
        let confidence: Double
        let reason: String

        var id: String { fileID }

        enum CodingKeys: String, CodingKey {
            case fileID = "file_id"
            case title
            case folderName = "folder_name"
            case confidence
            case reason
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            self.fileID = (try? c.decode(String.self, forKey: .fileID)) ?? ""
            self.title = (try? c.decode(String.self, forKey: .title)) ?? "(untitled)"
            self.folderName = (try? c.decode(String.self, forKey: .folderName)) ?? ""
            self.confidence = (try? c.decode(Double.self, forKey: .confidence)) ?? 0
            self.reason = (try? c.decode(String.self, forKey: .reason)) ?? ""
        }
    }

    /// Run a DRY-RUN classification (`classify --json`, no `--apply`) and
    /// publish the decoded plans into `classifyPlans` to open the preview
    /// sheet. The App's unique capability — the web client cannot do this.
    /// Nothing is moved here; the user reviews + confirms in the sheet.
    func classifyPreview() async {
        guard !classifyRunning else { return }
        classifyRunning = true
        defer { classifyRunning = false }
        let output = await runPlaudOutput(args: ["classify", "--json"])
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
        // Only files that resolved to a folder are actionable proposals.
        let actionable = plans.filter { !$0.folderName.isEmpty }
        if actionable.isEmpty {
            classifyResult = "No recordings matched a folder."
            return
        }
        classifyPlans = actionable.sorted { $0.confidence > $1.confidence }
    }

    /// Apply classification for ONLY the given file ids (`classify --apply`
    /// with one `--only <id>` each), then sync + reload. Sets
    /// `lastClassifyApply` so the UI can offer an Undo banner.
    func applyClassify(fileIDs: [String]) async {
        guard !classifyRunning, !fileIDs.isEmpty else { return }
        classifyRunning = true
        defer { classifyRunning = false }
        let args = ["classify", "--apply"] + fileIDs.flatMap { ["--only", $0] }
        let output = await runPlaudOutput(args: args)
        await sync(showError: false)
        // The JSON array has `moved_to` set for files that were actually moved.
        let trimmed = output.trimmingCharacters(in: .whitespacesAndNewlines)
        var moved = fileIDs.count
        if let data = trimmed.data(using: .utf8), !data.isEmpty,
           let results = try? JSONSerialization.jsonObject(with: data) as? [[String: Any]] {
            let movedCount = results.filter {
                let to = ($0["moved_to"] as? String) ?? ""
                return !to.isEmpty
            }.count
            if movedCount > 0 { moved = movedCount }
        }
        lastClassifyApply = (count: moved, at: Date())
    }

    /// Revert the last applied classification (`classify-undo --json`), then
    /// sync + reload and clear the undo banner.
    func classifyUndo() async {
        guard !classifyRunning else { return }
        classifyRunning = true
        defer { classifyRunning = false }
        _ = await runPlaudOutput(args: ["classify-undo", "--json"])
        lastClassifyApply = nil
        await sync(showError: false)
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
        await runPlaud(args: ["tag-add", fileID, tag])
        noteMetadata = Database.shared.noteMetadata(for: fileID)
        // Refresh the sidebar tag counts + the file's chip set.
        reload()
    }

    func removeTag(_ tag: String, from fileID: String) async {
        await runPlaud(args: ["tag-remove", fileID, tag])
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

    func reloadCmdsTranscript() {
        cmdsTranscript = selectedID.flatMap { Database.shared.cmdsTranscript(for: $0) }
    }

    func transcribeWithElevenLabs(_ fileID: String, numSpeakers: Int = 0) async {
        transcribingIDs.insert(fileID)
        defer { transcribingIDs.remove(fileID) }
        var args = ["cmds-transcribe", fileID]
        if numSpeakers > 0 {
            args += ["--num-speakers", String(numSpeakers)]
        }
        await runPlaud(args: args)
        reloadCmdsTranscript()
        // Transcription spends ElevenLabs credits — update the indicator.
        await refreshElevenLabs(force: true)
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

    /// Shell out to `uv run plaud …` and return stdout.
    ///
    /// `timeout` (seconds) is an optional watchdog: when set, the process is
    /// terminated if it overruns. When `nil` (the default), behavior is
    /// unchanged — it waits indefinitely via `waitUntilExit()`, matching every
    /// existing caller.
    func runPlaudOutput(
        args: [String],
        stdin: String? = nil,
        timeout: TimeInterval? = nil,
        showError: Bool = true
    ) async -> String {
        let result = await Task.detached(priority: .userInitiated) { () -> CommandResult in
            let stdout = Pipe()
            let stderr = Pipe()
            let input = stdin.map { _ in Pipe() }
            do {
                let process = try RuntimePaths.makePlaudProcess(args: args)
                process.standardOutput = stdout
                process.standardError = stderr
                if let input {
                    process.standardInput = input
                }
                try process.run()
                if let stdin,
                   let input,
                   let data = stdin.data(using: .utf8) {
                    input.fileHandleForWriting.write(data)
                    input.fileHandleForWriting.closeFile()
                }

                var timedOut = false
                if let timeout {
                    // Arm a watchdog that kills the process if it overruns, then
                    // wait. Reading the pipes *after* the process exits (or is
                    // killed) avoids a deadlock on a full pipe buffer for these
                    // small-output commands.
                    let watchdog = DispatchWorkItem {
                        if process.isRunning {
                            timedOut = true
                            process.terminate()
                        }
                    }
                    DispatchQueue.global().asyncAfter(
                        deadline: .now() + timeout, execute: watchdog
                    )
                    process.waitUntilExit()
                    watchdog.cancel()
                } else {
                    process.waitUntilExit()
                }

                let outData = stdout.fileHandleForReading.readDataToEndOfFile()
                let errData = stderr.fileHandleForReading.readDataToEndOfFile()
                if timedOut {
                    return CommandResult(
                        exitCode: -1,
                        stdout: String(data: outData, encoding: .utf8) ?? "",
                        stderr: "plaud command timed out after \(Int(timeout ?? 0))s"
                    )
                }
                return CommandResult(
                    exitCode: process.terminationStatus,
                    stdout: String(data: outData, encoding: .utf8) ?? "",
                    stderr: String(data: errData, encoding: .utf8) ?? ""
                )
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
            let stdout = Pipe()
            let stderr = Pipe()
            do {
                let process = try RuntimePaths.makePlaudProcess(args: args)
                process.standardOutput = stdout
                process.standardError = stderr
                try process.run()
                process.waitUntilExit()
                let outData = stdout.fileHandleForReading.readDataToEndOfFile()
                let errData = stderr.fileHandleForReading.readDataToEndOfFile()
                return CommandResult(
                    exitCode: process.terminationStatus,
                    stdout: String(data: outData, encoding: .utf8) ?? "",
                    stderr: String(data: errData, encoding: .utf8) ?? ""
                )
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

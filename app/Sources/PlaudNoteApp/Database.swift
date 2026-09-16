import AppKit
import Foundation
import GRDB

extension Notification.Name {
    static let plaudDBChanged = Notification.Name("plaud.dbChanged")
    static let openPlaudSettings = Notification.Name("plaud.openSettings")
}

struct PlaudFileVM: Identifiable, Hashable, FetchableRecord {
    let id: String
    let filename: String?
    let status: String
    /// Duration in milliseconds (Plaud API native unit).
    let durationMs: Double?
    /// Edit timestamp in **seconds** since epoch (Plaud quirk).
    let editTime: Int64?
    /// Recording start timestamp in **milliseconds** (Plaud quirk).
    let startTime: Int64?
    let isTrash: Bool
    let hasContent: Bool
    let folderNames: [String]
    let folderColor: String?
    let primaryTags: [String]
    /// Every tag on the file (not just the top-3 `primaryTags`) — used by the
    /// Tags sidebar filter so a file matches even when the tag isn't in the
    /// truncated display set. Empty for rows from the older `files(for:)` path.
    let allTags: [String]
    let usageStatus: String?
    /// True when the server-side AI note (`file_content.summary_md`) exists —
    /// drives the green "Generated" badge (Plaud-Web parity).
    let hasSummary: Bool
    /// LOCAL-ONLY read state (epoch seconds when the user first opened the
    /// recording); nil = never opened. `var` so optimistic in-memory updates
    /// can flip it without a full reload.
    var seenAt: Int64?
    /// LOCAL-ONLY star flag.
    var starred: Bool
    /// Dual-transcribe pipeline stage (nil = never marked). Drives the ⚡
    /// indicator in list rows. Only populated by the masterFiles() query;
    /// the older files(for:) path leaves it nil.
    let dualStatus: String?

    init(row: Row) {
        self.id = row["id"]
        self.filename = row["filename"]
        self.status = row["status"] ?? "new"
        self.durationMs = row["duration"]
        self.editTime = row["edit_time"]
        self.startTime = row["start_time"]
        self.isTrash = (row["is_trash"] as Int64? ?? 0) != 0
        self.hasContent = (row["has_content"] as Int64? ?? 0) != 0
        self.folderNames = Self.splitList(row["folder_names"])
        self.folderColor = row["folder_color"]
        self.primaryTags = Self.splitList(row["primary_tags"])
        self.allTags = Self.splitList(row["all_tags"])
        self.usageStatus = row["usage_status"]
        self.hasSummary = (row["has_summary"] as Int64? ?? 0) != 0
        // Safe defaults when the columns don't exist yet (pre-migration DB).
        self.seenAt = row["seen_at"]
        self.starred = (row["starred"] as Int64? ?? 0) != 0
        self.dualStatus = row["dual_status"]
    }

    private static func splitList(_ raw: String?) -> [String] {
        guard let raw, !raw.isEmpty else { return [] }
        return raw
            .split(separator: "\u{1f}")
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
    }

    /// Recording start time as a real Date — handles the ms unit.
    var createdAt: Date? {
        if let st = startTime {
            return Date(timeIntervalSince1970: TimeInterval(st) / 1000.0)
        }
        if let et = editTime {
            return Date(timeIntervalSince1970: TimeInterval(et))
        }
        return nil
    }
}

struct FolderVM: Identifiable, Hashable, FetchableRecord {
    let id: String
    let name: String
    let color: String?
    let icon: String?
    let count: Int

    init(row: Row) {
        self.id = row["id"]
        self.name = row["name"]
        self.color = row["color"]
        self.icon = row["icon"]
        self.count = row["count"] ?? 0
    }

    /// Map Plaud's iconfont identifiers to SF Symbols. Unknown → folder.
    var sfSymbol: String {
        guard let raw = icon else { return "folder.fill" }
        let key = raw.replacingOccurrences(of: "iconfont_folder_", with: "")
                     .replacingOccurrences(of: "iconfont_", with: "")
        let map: [String: String] = [
            "speech": "waveform",
            "voice": "waveform",
            "mic": "mic.fill",
            "meeting": "person.2.fill",
            "people": "person.2.fill",
            "person": "person.fill",
            "interview": "person.wave.2.fill",
            "book": "book.fill",
            "note": "note.text",
            "lecture": "book.fill",
            "study": "graduationcap.fill",
            "school": "graduationcap.fill",
            "idea": "lightbulb.fill",
            "lightbulb": "lightbulb.fill",
            "heart": "heart.fill",
            "love": "heart.fill",
            "star": "star.fill",
            "flag": "flag.fill",
            "tag": "tag.fill",
            "music": "music.note",
            "jazz": "music.note",
            "important": "exclamationmark.triangle.fill",
            "biohacking": "stethoscope",
            "medical": "stethoscope",
            "health": "heart.text.square.fill",
            "money": "dollarsign.circle.fill",
            "finance": "dollarsign.circle.fill",
            "family": "house.fill",
            "parents": "house.fill",
            "spirit": "sparkles",
            "spirituality": "sparkles",
            "contract": "doc.text.fill",
            "document": "doc.text.fill",
            "camera": "camera.fill",
            "video": "video.fill",
        ]
        return map[key] ?? "folder.fill"
    }
}

struct FileContentVM {
    let title: String?
    let transcript: String
    let outline: String
    let summaries: [SummaryVM]
    let keywords: [String]
}

struct SummaryVM: Identifiable, Hashable {
    let id: String
    let title: String
    let body: String
}

struct NoteTagVM: Identifiable, Hashable, FetchableRecord {
    let tag: String
    let source: String
    var id: String { tag }

    init(row: Row) {
        self.tag = row["tag"]
        self.source = row["source"] ?? "manual"
    }
}

struct NoteReferenceVM: Identifiable, Hashable, FetchableRecord {
    let path: String
    let kind: String
    let title: String?
    var id: String { path }

    init(row: Row) {
        self.path = row["path"]
        self.kind = row["kind"] ?? "note"
        self.title = row["title"]
    }
}

/// One speaker → real-name candidate from the dual pipeline's proposal step.
struct SpeakerProposalVM: Identifiable, Hashable, Decodable {
    let speaker: String
    var name: String
    let confidence: Double
    let evidence: String
    var id: String { speaker }
}

/// Dual-transcribe pipeline state for one recording (`dual_pipeline` row).
struct DualStateVM: Hashable {
    let fileID: String
    let status: String  // marked | transcribing | relabel-pending | integrating | vault-ready | vault-sent
    let proposal: [SpeakerProposalVM]
    let speakerMap: [String: String]
    let vaultPath: String?
    let error: String?
}

/// The dual pipeline's final artifact pair (cross-analyzed transcript +
/// comprehensive summary) resolved from data/integrated/.
struct IntegratedContentVM: Hashable {
    let fileID: String
    let label: String  // e.g. "gpt-5.6-sol__integrated"
    let transcript: String
    let summary: String
}

/// Content-reuse mark (`note_reuse` row): channel × status × note.
struct ReuseMarkVM: Identifiable, Hashable, FetchableRecord {
    let channel: String
    let status: String  // flagged | drafted | published
    let note: String?
    var id: String { channel }

    init(row: Row) {
        self.channel = row["channel"]
        self.status = row["status"] ?? "flagged"
        self.note = row["note"]
    }
}

struct NoteMetadataVM: Hashable {
    let fileID: String
    let title: String?
    let description: String?
    let noteType: String?
    let status: String?
    let usageStatus: String?
    let category: String?
    let folderName: String?
    let vaultPath: String?
    let draftPath: String?
    let finalNotePath: String?
    let tags: [NoteTagVM]
    let references: [NoteReferenceVM]
}

struct ModelPresetVM: Identifiable, Hashable {
    let provider: String
    let providerLabel: String
    let apiName: String
    let title: String
    let description: String
    let status: String
    let isSOTA: Bool
    let sourcePath: String

    var id: String { "\(provider)::\(apiName)" }
    var displayName: String { "\(providerLabel) · \(apiName)" }
}

enum SidebarItem: Hashable {
    case allFiles
    case unfiled
    case starred
    case trash
    case folder(String)
    case tag(String)
    /// Nested-tag PARENT node: matches every recording whose tag equals the
    /// prefix OR starts with `prefix/` (e.g. selecting `AI` matches `AI`,
    /// `AI/agent`, `AI/agent/tools`). Leaf clicks still use `.tag` for an
    /// exact match.
    case tagPrefix(String)
}

final class Database: @unchecked Sendable {
    static let shared = Database()

    let path: String = RuntimePaths.databaseURL.path
    private var pool: DatabasePool?
    private var metadataSchemaReady = false
    private let lock = NSLock()

    private init() { tryOpen() }

    private func tryOpen() {
        guard pool == nil,
              FileManager.default.fileExists(atPath: path)
        else { return }
        // Single read/write pool. Read-only mode disables WAL snapshot
        // updates from other writers (Python CLI), so we go fully open.
        // The Python CLI writes concurrently; an explicit busy timeout lets
        // SQLite wait out a held lock instead of failing immediately with
        // SQLITE_BUSY.
        var config = Configuration()
        config.busyMode = .timeout(5)
        // Read tuning for a ~300MB library on a laptop SSD. `mmap_size` lets
        // SQLite page the file in via the VM system instead of read(2) per
        // page; `cache_size` is negative = KiB, so -60000 pins a 60MB page
        // cache (the whole hot working set: files + note_tags + metadata).
        // Applied per-connection, which is what `prepareDatabase` is for.
        config.prepareDatabase { db in
            try db.execute(sql: "PRAGMA cache_size = -60000")
            try db.execute(sql: "PRAGMA mmap_size = 536870912")
            try db.execute(sql: "PRAGMA temp_store = MEMORY")
            try db.execute(sql: "PRAGMA synchronous = NORMAL")
        }
        self.pool = try? DatabasePool(path: path, configuration: config)
        ensureMetadataSchema()
    }

    private func ensureMetadataSchema() {
        guard !metadataSchemaReady, let pool else { return }
        try? pool.write { db in
            // Defensive guard for the LOCAL-ONLY read/star columns. The app's
            // first reload can run before any `plaud` CLI command has had a
            // chance to migrate, so mirror the Python migration here (same
            // idempotent ALTERs; the version-gated Python migration skips
            // columns that already exist). Deliberately does NOT touch
            // PRAGMA user_version — that stays owned by the Python side.
            let fileRows = try Row.fetchAll(db, sql: "PRAGMA table_info(files)")
            let fileColumns = Set(fileRows.compactMap { row -> String? in row["name"] })
            if !fileColumns.isEmpty, !fileColumns.contains("seen_at") {
                try db.execute(sql: "ALTER TABLE files ADD COLUMN seen_at INTEGER")
                // The pre-existing library counts as "seen" — only recordings
                // that arrive after this migration light up as unread.
                try db.execute(sql: """
                    UPDATE files SET seen_at = strftime('%s','now')
                     WHERE seen_at IS NULL
                """)
            }
            if !fileColumns.isEmpty, !fileColumns.contains("starred") {
                try db.execute(sql: """
                    ALTER TABLE files ADD COLUMN starred INTEGER NOT NULL DEFAULT 0
                """)
            }
            try db.execute(sql: """
                CREATE TABLE IF NOT EXISTS note_metadata (
                    file_id TEXT PRIMARY KEY,
                    title TEXT,
                    description TEXT,
                    note_type TEXT,
                    status TEXT NOT NULL DEFAULT 'unread',
                    usage_status TEXT NOT NULL DEFAULT 'unused',
                    category TEXT,
                    folder_id TEXT,
                    folder_name TEXT,
                    vault_path TEXT,
                    draft_path TEXT,
                    final_note_path TEXT,
                    metadata_json TEXT,
                    generated_at INTEGER,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS note_tags (
                    file_id TEXT NOT NULL,
                    tag TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'manual',
                    created_at INTEGER NOT NULL,
                    PRIMARY KEY (file_id, tag)
                );
                CREATE TABLE IF NOT EXISTS note_references (
                    file_id TEXT NOT NULL,
                    path TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    title TEXT,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (file_id, path)
                );
                CREATE TABLE IF NOT EXISTS note_reuse (
                    file_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'flagged',
                    note TEXT,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (file_id, channel)
                );
                CREATE TABLE IF NOT EXISTS dual_pipeline (
                    file_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL DEFAULT 'marked',
                    speaker_proposal TEXT,
                    speaker_map TEXT,
                    vault_path TEXT,
                    error TEXT,
                    updated_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS note_tags_tag_idx ON note_tags(tag);
                CREATE INDEX IF NOT EXISTS note_refs_file_idx ON note_references(file_id);
                CREATE INDEX IF NOT EXISTS note_reuse_channel_idx ON note_reuse(channel, status);
            """)
            let rows = try Row.fetchAll(db, sql: "PRAGMA table_info(note_metadata)")
            let columns = Set(rows.compactMap { row -> String? in row["name"] })
            if !columns.contains("usage_status") {
                try db.execute(sql: """
                    ALTER TABLE note_metadata
                    ADD COLUMN usage_status TEXT NOT NULL DEFAULT 'unused'
                """)
            }
            if !columns.contains("category") {
                try db.execute(sql: "ALTER TABLE note_metadata ADD COLUMN category TEXT")
            }
            if !columns.contains("folder_id") {
                try db.execute(sql: "ALTER TABLE note_metadata ADD COLUMN folder_id TEXT")
            }
            if !columns.contains("folder_name") {
                try db.execute(sql: "ALTER TABLE note_metadata ADD COLUMN folder_name TEXT")
            }
        }
        metadataSchemaReady = true
    }

    /// Optimistic write path — Swift writes folder assignments directly so
    /// the UI updates instantly. Python CLI then does the server PATCH in
    /// the background; both paths converge on the same SQLite rows.
    ///
    /// Returns the thrown error (if any) instead of silently discarding it so
    /// the optimistic UI can reconcile (e.g. revert + re-read) on failure.
    @discardableResult
    func writeFileFolders(_ fileID: String, folderIDs: [String]) -> Error? {
        lock.lock(); defer { lock.unlock() }
        tryOpen()
        guard let pool else {
            return DatabaseError(message: "Database pool unavailable")
        }
        do {
            try pool.write { db in
                try db.execute(sql: "DELETE FROM file_folders WHERE file_id = ?",
                               arguments: [fileID])
                for fid in folderIDs {
                    try db.execute(
                        sql: "INSERT OR IGNORE INTO file_folders (file_id, folder_id) VALUES (?, ?)",
                        arguments: [fileID, fid]
                    )
                }
            }
            return nil
        } catch {
            return error
        }
    }

    /// Optimistic rename — Swift writes the new filename directly so the UI
    /// updates instantly, then the Python CLI PATCHes the server in the
    /// background; both paths converge on the same SQLite row.
    @discardableResult
    func writeFileName(_ fileID: String, name: String) -> Error? {
        lock.lock(); defer { lock.unlock() }
        tryOpen()
        guard let pool else {
            return DatabaseError(message: "Database pool unavailable")
        }
        do {
            try pool.write { db in
                try db.execute(
                    sql: """
                    UPDATE files
                       SET filename = ?, updated_at = strftime('%s','now')
                     WHERE id = ?
                    """,
                    arguments: [name, fileID]
                )
            }
            return nil
        } catch {
            return error
        }
    }

    /// Mark a recording as seen (read). LOCAL-ONLY column; no server call.
    /// The `seen_at IS NULL` guard keeps the first-open timestamp stable.
    @discardableResult
    func markSeen(fileID: String) -> Error? {
        lock.lock(); defer { lock.unlock() }
        tryOpen()
        guard let pool else {
            return DatabaseError(message: "Database pool unavailable")
        }
        do {
            try pool.write { db in
                try db.execute(
                    sql: """
                    UPDATE files SET seen_at = strftime('%s','now')
                     WHERE id = ? AND seen_at IS NULL
                    """,
                    arguments: [fileID]
                )
            }
            return nil
        } catch {
            return error
        }
    }

    /// Toggle the LOCAL-ONLY star flag. Mirrors `plaud star <id> [--off]`.
    @discardableResult
    func setStarred(fileID: String, starred: Bool) -> Error? {
        lock.lock(); defer { lock.unlock() }
        tryOpen()
        guard let pool else {
            return DatabaseError(message: "Database pool unavailable")
        }
        do {
            try pool.write { db in
                try db.execute(
                    sql: "UPDATE files SET starred = ? WHERE id = ?",
                    arguments: [starred ? 1 : 0, fileID]
                )
            }
            return nil
        } catch {
            return error
        }
    }

    private func read<T>(_ block: (GRDB.Database) throws -> T) -> T? {
        lock.lock(); defer { lock.unlock() }
        tryOpen()
        guard let pool else { return nil }
        return try? pool.read(block)
    }

    /// One row of the in-memory master list: a fully-hydrated `PlaudFileVM`
    /// plus everything the sidebar predicate needs to filter *synchronously*
    /// in Swift (trash flag + the file's folder-id membership). Loading this
    /// once lets folder-clicks and search keystrokes filter without a DB hop.
    struct MasterFile: Identifiable, Hashable {
        /// `var` so FileStore can flip seen/star state optimistically.
        var file: PlaudFileVM
        let folderIDs: Set<String>
        var id: String { file.id }
    }

    /// Load the entire library (trash included) in one pass, each row carrying
    /// its folder membership so the four sidebar cases can be reproduced
    /// in-memory exactly as `files(for:)` does in SQL.
    ///
    /// Folder names/colors/ids come from two grouped LEFT JOINs instead of
    /// per-row correlated subqueries (one scan of `file_folders` each instead
    /// of N). `fids` deliberately skips the `folders` join so dangling
    /// folder ids still surface in `folder_ids`, matching the old subquery.
    func masterFiles() -> [MasterFile] {
        let sql = """
        SELECT f.*,
               EXISTS(SELECT 1 FROM file_content fc WHERE fc.file_id = f.id) AS has_content,
               EXISTS(
                   SELECT 1 FROM file_content fc
                    WHERE fc.file_id = f.id
                      AND fc.summary_md IS NOT NULL
                      AND fc.summary_md != ''
               ) AS has_summary,
               COALESCE(
                   NULLIF(fnames.agg_folder_names, ''),
                   nm.folder_name
               ) AS folder_names,
               COALESCE(
                   fnames.agg_folder_color,
                   (
                       SELECT fo.color
                         FROM folders fo
                        WHERE fo.name = nm.folder_name
                        LIMIT 1
                   )
               ) AS folder_color,
               nm.usage_status AS usage_status,
               fids.agg_folder_ids AS folder_ids,
               (
                   SELECT GROUP_CONCAT(tag, char(31))
                     FROM (
                         SELECT nt.tag AS tag
                           FROM note_tags nt
                          WHERE nt.file_id = f.id
                          ORDER BY CASE nt.source
                                     WHEN 'manual' THEN 0
                                     WHEN 'ai' THEN 1
                                     WHEN 'auto' THEN 2
                                     ELSE 3
                                   END,
                                   nt.tag COLLATE NOCASE
                          LIMIT 3
                     )
               ) AS primary_tags,
               (
                   SELECT GROUP_CONCAT(nt.tag, char(31))
                     FROM note_tags nt
                    WHERE nt.file_id = f.id
               ) AS all_tags,
               dp.status AS dual_status
          FROM files f
          LEFT JOIN note_metadata nm ON nm.file_id = f.id
          LEFT JOIN dual_pipeline dp ON dp.file_id = f.id
          LEFT JOIN (
              SELECT file_id,
                     GROUP_CONCAT(folder_id, char(31)) AS agg_folder_ids
                FROM file_folders
               GROUP BY file_id
          ) fids ON fids.file_id = f.id
          LEFT JOIN (
              SELECT ff.file_id,
                     GROUP_CONCAT(fo.name, char(31)) AS agg_folder_names,
                     MIN(fo.color) AS agg_folder_color
                FROM file_folders ff
                JOIN folders fo ON fo.id = ff.folder_id
               GROUP BY ff.file_id
          ) fnames ON fnames.file_id = f.id
         ORDER BY COALESCE(f.start_time, f.edit_time * 1000) DESC LIMIT 5000
        """
        return read { db in
            try Row.fetchAll(db, sql: sql).map { row -> MasterFile in
                let ids = (row["folder_ids"] as String?)?
                    .split(separator: "\u{1f}")
                    .map { String($0) }
                    .filter { !$0.isEmpty } ?? []
                return MasterFile(file: PlaudFileVM(row: row), folderIDs: Set(ids))
            }
        } ?? []
    }

    func files(for sidebar: SidebarItem, search: String = "") -> [PlaudFileVM] {
        var sql = """
        SELECT f.*,
               EXISTS(SELECT 1 FROM file_content fc WHERE fc.file_id = f.id) AS has_content,
               COALESCE(
                   NULLIF((
                       SELECT GROUP_CONCAT(fo.name, char(31))
                         FROM file_folders ff
                         JOIN folders fo ON fo.id = ff.folder_id
                        WHERE ff.file_id = f.id
                   ), ''),
                   nm.folder_name
               ) AS folder_names,
               COALESCE(
                   (
                       SELECT fo.color
                         FROM file_folders ff
                         JOIN folders fo ON fo.id = ff.folder_id
                        WHERE ff.file_id = f.id
                        LIMIT 1
                   ),
                   (
                       SELECT fo.color
                         FROM folders fo
                        WHERE fo.name = nm.folder_name
                        LIMIT 1
                   )
               ) AS folder_color,
               nm.usage_status AS usage_status,
               (
                   SELECT GROUP_CONCAT(tag, char(31))
                     FROM (
                         SELECT nt.tag AS tag
                           FROM note_tags nt
                          WHERE nt.file_id = f.id
                          ORDER BY CASE nt.source
                                     WHEN 'manual' THEN 0
                                     WHEN 'ai' THEN 1
                                     WHEN 'auto' THEN 2
                                     ELSE 3
                                   END,
                                   nt.tag COLLATE NOCASE
                          LIMIT 3
                     )
               ) AS primary_tags
          FROM files f
          LEFT JOIN note_metadata nm ON nm.file_id = f.id
         WHERE 1=1
        """
        var args: [DatabaseValueConvertible] = []
        switch sidebar {
        case .allFiles:
            sql += " AND f.is_trash = 0"
        case .unfiled:
            sql += " AND f.is_trash = 0 AND f.id NOT IN (SELECT file_id FROM file_folders)"
        case .starred:
            sql += " AND f.is_trash = 0 AND f.starred = 1"
        case .trash:
            sql += " AND f.is_trash = 1"
        case .folder(let id):
            sql += " AND f.is_trash = 0 AND f.id IN (SELECT file_id FROM file_folders WHERE folder_id = ?)"
            args.append(id)
        case .tag(let tag):
            sql += " AND f.is_trash = 0 AND f.id IN (SELECT file_id FROM note_tags WHERE tag = ?)"
            args.append(tag)
        case .tagPrefix(let prefix):
            // Parent node: the bare tag OR any `prefix/...` descendant.
            sql += " AND f.is_trash = 0 AND f.id IN (SELECT file_id FROM note_tags WHERE tag = ? OR tag LIKE ?)"
            args.append(prefix)
            args.append("\(prefix)/%")
        }
        if !search.isEmpty {
            sql += " AND f.filename LIKE ?"
            args.append("%\(search)%")
        }
        // Sort by recording-creation time. start_time is ms; edit_time is sec
        // — normalize both to ms before COALESCE so the comparison is honest.
        sql += " ORDER BY COALESCE(f.start_time, f.edit_time * 1000) DESC LIMIT 5000"
        return read { db in
            try Row.fetchAll(db, sql: sql, arguments: StatementArguments(args))
                .map(PlaudFileVM.init(row:))
        } ?? []
    }

    func folders() -> [FolderVM] {
        // One grouped scan of file_folders instead of a correlated COUNT(*)
        // re-executed per folder row.
        read { db in
            try Row.fetchAll(db, sql: """
                SELECT f.id, f.name, f.color, COALESCE(c.cnt, 0) AS count
                  FROM folders f
                  LEFT JOIN (
                      SELECT ff.folder_id AS folder_id, COUNT(*) AS cnt
                        FROM file_folders ff
                        JOIN files fi ON fi.id = ff.file_id AND fi.is_trash = 0
                       GROUP BY ff.folder_id
                  ) c ON c.folder_id = f.id
                 ORDER BY f.name COLLATE NOCASE
            """).map(FolderVM.init(row:))
        } ?? []
    }

    func categoryCounts() -> (all: Int, unfiled: Int, starred: Int, trash: Int) {
        // Single pass over `files` instead of four separate COUNT queries.
        // NOT EXISTS (not NOT IN) so a NULL file_id row can never poison the
        // unfiled predicate.
        read { db in
            guard let row = try Row.fetchOne(db, sql: """
                SELECT COALESCE(SUM(is_trash = 0), 0) AS all_count,
                       COALESCE(SUM(is_trash = 0 AND NOT EXISTS(
                           SELECT 1 FROM file_folders ff
                            WHERE ff.file_id = files.id
                       )), 0) AS unfiled_count,
                       COALESCE(SUM(is_trash = 0 AND starred = 1), 0) AS starred_count,
                       COALESCE(SUM(is_trash = 1), 0) AS trash_count
                  FROM files
            """) else { return (0, 0, 0, 0) }
            let all: Int = row["all_count"] ?? 0
            let unfiled: Int = row["unfiled_count"] ?? 0
            let starred: Int = row["starred_count"] ?? 0
            let trash: Int = row["trash_count"] ?? 0
            return (all, unfiled, starred, trash)
        } ?? (0, 0, 0, 0)
    }

    /// Distinct tags across non-trash files with their file counts, ordered by
    /// count desc then name. Drives the Tags section in the sidebar.
    func tagCounts() -> [(tag: String, count: Int)] {
        read { db in
            try Row.fetchAll(db, sql: """
                SELECT nt.tag AS tag, COUNT(DISTINCT nt.file_id) AS cnt
                  FROM note_tags nt
                  JOIN files f ON f.id = nt.file_id AND f.is_trash = 0
                 GROUP BY nt.tag
                 ORDER BY cnt DESC, nt.tag COLLATE NOCASE
            """).map { row -> (tag: String, count: Int) in
                (tag: row["tag"], count: row["cnt"] ?? 0)
            }
        } ?? []
    }

    /// Set of non-trash file ids carrying the given tag. Used by the sidebar
    /// `.tag` filter (kept as a fallback / direct lookup path).
    func fileIDsWithTag(_ tag: String) -> Set<String> {
        let ids: [String] = read { db in
            try String.fetchAll(db, sql: """
                SELECT DISTINCT nt.file_id
                  FROM note_tags nt
                  JOIN files f ON f.id = nt.file_id AND f.is_trash = 0
                 WHERE nt.tag = ?
            """, arguments: [tag])
        } ?? []
        return Set(ids)
    }

    /// Count of files that don't yet have cached transcripts/summary.
    /// Used by the toolbar to show backfill progress.
    func contentCacheStatus() -> (total: Int, cached: Int) {
        read { db in
            let total = try Int.fetchOne(db,
                sql: "SELECT COUNT(*) FROM files WHERE is_trash = 0") ?? 0
            let cached = try Int.fetchOne(db,
                sql: "SELECT COUNT(*) FROM file_content") ?? 0
            return (total, cached)
        } ?? (0, 0)
    }

    struct Speaker: Identifiable, Hashable, FetchableRecord {
        let id: Int64
        let name: String
        let isSelf: Bool
        init(row: Row) {
            self.id = row["id"]
            self.name = row["name"]
            self.isSelf = (row["is_self"] as Int64? ?? 0) != 0
        }
    }

    /// One configured summary slot. Read from `data/slots.json` directly so
    /// Swift and Python share the same source of truth.
    struct Slot: Identifiable, Hashable, Decodable {
        let name: String
        let model: String
        let template: String
        let modelID: String?
        var outputModel: String { (modelID?.isEmpty == false ? modelID : nil) ?? model }
        var id: String { "\(name)__\(model)__\(outputModel)__\(template)" }

        init(name: String, model: String, template: String, modelID: String? = nil) {
            self.name = name
            self.model = model
            self.template = template
            self.modelID = modelID
        }

        enum CodingKeys: String, CodingKey {
            case name
            case model
            case template
            case modelID = "model_id"
        }
    }

    static let projectRoot: String = RuntimePaths.projectRoot.path

    /// Single source of truth for the offline-fallback model ids, keyed by
    /// provider. Used by `loadAppConfig` defaults, `fallbackModelPresets`, and
    /// the AddSlotSheet initial state — so they never drift apart. Do **not**
    /// derive these from `loadModelPresets()` (that would be circular).
    static let fallbackModelIDs: [String: String] = [
        "claude": "claude-fable-5",
        "codex": "gpt-5.6-sol",
        "gemini": "gemini-3.1-pro-preview",
        "grok": "grok-4.6",
    ]

    /// App config shared with the Python CLI. Direct read of `data/config.json`.
    /// Writes go through the `plaud config-*` CLI commands so unknown keys are
    /// always preserved by the Python side.
    struct AppConfig {
        var backends: [String: String]
        var models: [String: String]
        var paths: [String: String]
        /// Which model runs auto-classify by default
        /// (top-level `classify_model` key; `plaud config-classify`).
        var classifyModel: String
        /// Which model runs metadata-generate by default — codex = GPT via
        /// the Codex CLI subscription login
        /// (top-level `metadata_model` key; `plaud config-metadata-model`).
        var metadataModel: String
        /// Auto-generate metadata for fresh files after sync
        /// (top-level `auto_metadata` key; `plaud config-auto-metadata`).
        var autoMetadata: Bool
        /// Tags the user pinned to the top of the sidebar Tags section
        /// (top-level `pinned_tags` key; `plaud tag-pin`).
        var pinnedTags: [String]
    }

    /// `config.json` parsed once and reused until the file's mtime/size
    /// changes. `outputBaseDir(kind:)` calls this, and that in turn is called
    /// from view bodies (`integratedExists`, `summaryBody`, the slot cards) —
    /// so an uncached read meant a disk read + JSON parse several times per
    /// render pass. The CLI writes this file, hence the stat-based check
    /// rather than a load-once cache.
    private struct ConfigCache {
        let signature: String
        let config: AppConfig
    }
    private var configCache: ConfigCache?
    private let configLock = NSLock()

    private static func fileSignature(_ path: String) -> String {
        guard let attrs = try? FileManager.default.attributesOfItem(atPath: path)
        else { return "" }
        let mtime = (attrs[.modificationDate] as? Date)?.timeIntervalSince1970 ?? 0
        let size = (attrs[.size] as? UInt64) ?? 0
        return "\(mtime):\(size)"
    }

    func loadAppConfig() -> AppConfig {
        let path = RuntimePaths.dataDirectory.appendingPathComponent("config.json").path
        let signature = Self.fileSignature(path)
        configLock.lock()
        if let cached = configCache, cached.signature == signature {
            configLock.unlock()
            return cached.config
        }
        configLock.unlock()
        let parsed = parseAppConfig(path: path)
        configLock.lock()
        configCache = ConfigCache(signature: signature, config: parsed)
        configLock.unlock()
        return parsed
    }

    private func parseAppConfig(path: String) -> AppConfig {
        let defaults = AppConfig(
            backends: ["claude": "cli", "codex": "cli", "gemini": "api",
                       "grok": "api"],
            models: Self.fallbackModelIDs,
            paths: ["transcripts": "", "summaries": "", "integrated": ""],
            classifyModel: "claude",
            metadataModel: "codex",
            autoMetadata: true,
            pinnedTags: []
        )
        guard let data = try? Data(contentsOf: URL(fileURLWithPath: path)),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return defaults }
        var cfg = defaults
        if let b = obj["backends"] as? [String: String] { cfg.backends.merge(b) { _, new in new } }
        if let m = obj["models"] as? [String: String] { cfg.models.merge(m) { _, new in new } }
        if let p = obj["paths"] as? [String: String] { cfg.paths.merge(p) { _, new in new } }
        if let c = obj["classify_model"] as? String, !c.isEmpty { cfg.classifyModel = c }
        if let m = obj["metadata_model"] as? String, !m.isEmpty { cfg.metadataModel = m }
        if let a = obj["auto_metadata"] as? Bool { cfg.autoMetadata = a }
        if let t = obj["pinned_tags"] as? [String] {
            cfg.pinnedTags = t
                .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
                .filter { !$0.isEmpty }
        }
        return cfg
    }

    func loadSlots() -> [Slot] {
        let path = RuntimePaths.dataDirectory.appendingPathComponent("slots.json").path
        if let data = try? Data(contentsOf: URL(fileURLWithPath: path)),
           let arr = try? JSONDecoder().decode([Slot].self, from: data) {
            return arr
        }
        return [Slot(name: "Default (Claude)", model: "claude", template: "default")]
    }

    /// Model presets come from the Python CLI (`plaud models --json`), which is
    /// the single config-driven source of truth — the preset source directory
    /// is resolved entirely on the Python side, so no document/vault path lives
    /// in Swift. On any failure (CLI missing, non-zero exit, bad JSON) we fall
    /// back to the built-in offline presets.
    func loadModelPresets() -> [ModelPresetVM] {
        var presets = Self.fetchModelPresetsFromCLI() ?? Self.fallbackModelPresets()

        if presets.isEmpty {
            presets = Self.fallbackModelPresets()
        }
        if presets.contains(where: { $0.isSOTA }) {
            presets = presets.filter { $0.isSOTA }
        }
        return presets.sorted {
            let providerOrder = ["claude": 0, "codex": 1, "gemini": 2, "grok": 3]
            let lp = providerOrder[$0.provider] ?? 99
            let rp = providerOrder[$1.provider] ?? 99
            if lp != rp { return lp < rp }
            let ls = $0.isSOTA ? 0 : 1
            let rs = $1.isSOTA ? 0 : 1
            if ls != rs { return ls < rs }
            return $0.apiName < $1.apiName
        }
    }

    /// JSON shape emitted by `plaud models --json` (snake_case keys).
    private struct ModelPresetDTO: Decodable {
        let provider: String
        let providerLabel: String
        let apiName: String
        let title: String
        let description: String
        let status: String
        let isSOTA: Bool
        let sourcePath: String

        enum CodingKeys: String, CodingKey {
            case provider
            case providerLabel = "provider_label"
            case apiName = "api_name"
            case title
            case description
            case status
            case isSOTA = "is_sota"
            case sourcePath = "source_path"
        }

        var viewModel: ModelPresetVM {
            ModelPresetVM(
                provider: provider,
                providerLabel: providerLabel,
                apiName: apiName,
                title: title,
                description: description,
                status: status,
                isSOTA: isSOTA,
                sourcePath: sourcePath
            )
        }
    }

    /// Run `uv run plaud models --json` synchronously and decode the result.
    /// Returns `nil` (so the caller falls back) when the CLI is unavailable,
    /// exits non-zero, or emits output we can't decode.
    private static func fetchModelPresetsFromCLI() -> [ModelPresetVM]? {
        if DistributionProfile.isCommunity { return nil }
        guard let process = try? RuntimePaths.makePlaudProcess(args: ["models", "--json"])
        else { return nil }

        let stdout = Pipe()
        process.standardOutput = stdout
        process.standardError = Pipe()
        do {
            try process.run()
        } catch {
            return nil
        }
        let data = stdout.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        guard process.terminationStatus == 0, !data.isEmpty,
              let dtos = try? JSONDecoder().decode([ModelPresetDTO].self, from: data)
        else { return nil }
        return dtos.map(\.viewModel)
    }

    private static func fallbackModelPresets() -> [ModelPresetVM] {
        [
            ModelPresetVM(provider: "claude", providerLabel: "Anthropic",
                          apiName: fallbackModelIDs["claude"] ?? "claude-fable-5",
                          title: "Claude Opus 4.7",
                          description: "Anthropic flagship", status: "fallback",
                          isSOTA: true, sourcePath: ""),
            ModelPresetVM(provider: "codex", providerLabel: "OpenAI",
                          apiName: fallbackModelIDs["codex"] ?? "gpt-5.6-sol",
                          title: "GPT-5.5",
                          description: "OpenAI flagship", status: "fallback",
                          isSOTA: true, sourcePath: ""),
            ModelPresetVM(provider: "gemini", providerLabel: "Google",
                          apiName: fallbackModelIDs["gemini"] ?? "gemini-3.1-pro-preview",
                          title: "Gemini 3.1 Pro",
                          description: "Google frontier", status: "fallback",
                          isSOTA: true, sourcePath: ""),
            ModelPresetVM(provider: "grok", providerLabel: "xAI",
                          apiName: fallbackModelIDs["grok"] ?? "grok-4.6",
                          title: "Grok 4.20",
                          description: "xAI frontier", status: "fallback",
                          isSOTA: true, sourcePath: ""),
        ]
    }

    /// Templates living under `templates/`. Each `.md` filename is a template id.
    func listTemplates() -> [String] {
        let dir = RuntimePaths.templatesDirectory.path
        let url = URL(fileURLWithPath: dir)
        guard let items = try? FileManager.default.contentsOfDirectory(
            at: url, includingPropertiesForKeys: nil
        ) else { return [] }
        return items.filter { $0.pathExtension == "md" }
                    .map { $0.deletingPathExtension().lastPathComponent }
                    .sorted()
    }

    /// Resolve the base directory for a configured output kind
    /// (`transcripts` / `summaries` / `integrated`). Honors the user-set path
    /// from `data/config.json`: when non-empty it is expanded (`~` → home) and
    /// used directly; otherwise we fall back to the project `data/<kind>/`
    /// default so Swift matches the Python CLI's resolution.
    func outputBaseDir(kind: String) -> String {
        let configured = (loadAppConfig().paths[kind] ?? "")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        if !configured.isEmpty {
            return NSString(string: configured).expandingTildeInPath
        }
        return RuntimePaths.dataDirectory.appendingPathComponent(kind).path
    }

    /// Read a previously-generated summary markdown if it exists.
    func summaryFile(fileID: String, model: String, template: String) -> URL? {
        let safeModel = sanitize(model)
        let safeTpl = sanitize(template)
        let base = outputBaseDir(kind: "summaries")
        let path = "\(base)/\(fileID)/\(safeModel)__\(safeTpl).md"
        let url = URL(fileURLWithPath: path)
        return FileManager.default.fileExists(atPath: path) ? url : nil
    }

    func summaryBody(fileID: String, model: String, template: String) -> String? {
        guard let url = summaryFile(fileID: fileID, model: model, template: template),
              let raw = try? String(contentsOf: url, encoding: .utf8) else { return nil }
        return Self.stripFrontmatter(raw)
    }

    // MARK: - Integrated outputs (transcript + summary)

    /// Three-file output of `cmds-integrate`:
    /// - `<stem>.md`            → combined raw model output
    /// - `<stem>.transcript.md` → final transcript
    /// - `<stem>.summary.md`    → comprehensive summary
    enum IntegratedKind: String {
        case all
        case transcript
        case summary
    }

    func integratedFile(fileID: String, model: String, template: String,
                        kind: IntegratedKind) -> URL? {
        let safeModel = sanitize(model)
        let safeTpl = sanitize(template)
        let stem = "\(safeModel)__\(safeTpl)"
        let suffix: String = {
            switch kind {
            case .all: return ".md"
            case .transcript: return ".transcript.md"
            case .summary: return ".summary.md"
            }
        }()
        let base = outputBaseDir(kind: "integrated")
        let path = "\(base)/\(fileID)/\(stem)\(suffix)"
        let url = URL(fileURLWithPath: path)
        return FileManager.default.fileExists(atPath: path) ? url : nil
    }

    func integratedBody(fileID: String, model: String, template: String,
                        kind: IntegratedKind) -> String? {
        guard let url = integratedFile(fileID: fileID, model: model,
                                       template: template, kind: kind),
              let raw = try? String(contentsOf: url, encoding: .utf8)
        else { return nil }
        return Self.stripFrontmatter(raw)
    }

    /// True if any integrated output exists for this slot.
    func integratedExists(fileID: String, model: String, template: String) -> Bool {
        integratedFile(fileID: fileID, model: model,
                       template: template, kind: .all) != nil
    }

    /// True if *any* integrated output markdown exists for this file,
    /// regardless of slot (model/template). One directory scan of
    /// `data/integrated/<fileID>/` — used by the Progress tile.
    func integratedAnyExists(fileID: String) -> Bool {
        guard !fileID.isEmpty else { return false }
        let dir = "\(outputBaseDir(kind: "integrated"))/\(fileID)"
        guard let items = try? FileManager.default.contentsOfDirectory(atPath: dir)
        else { return false }
        return items.contains { $0.hasSuffix(".md") }
    }

    /// Compact MM:SS for short recordings, H:MM:SS only when ≥ 1 hour.
    /// Drops the noisy seconds resolution baseline (`00:00:00`) the user
    /// found cluttered.
    static func formatMinSec(_ totalSeconds: Int) -> String {
        let s = max(0, totalSeconds)
        if s >= 3600 {
            return String(format: "%d:%02d:%02d",
                          s / 3600, (s % 3600) / 60, s % 60)
        }
        return String(format: "%d:%02d", s / 60, s % 60)
    }

    private static func stripFrontmatter(_ raw: String) -> String {
        if raw.hasPrefix("---\n"),
           let end = raw.range(of: "\n---\n", options: [],
                               range: raw.index(raw.startIndex, offsetBy: 4)..<raw.endIndex) {
            return String(raw[end.upperBound...])
                .trimmingCharacters(in: .whitespacesAndNewlines)
        }
        return raw
    }

    private func sanitize(_ s: String) -> String {
        s.map { c in c.isLetter || c.isNumber || c == "-" || c == "_" ? String(c) : "_" }
         .joined()
    }

    /// One full-content search hit: the recording id plus an FTS snippet
    /// with the matched span wrapped in « ».
    struct SearchHit {
        let fileID: String
        let snippet: String
    }

    /// Full-content search over title + transcript + summary, run natively
    /// against the local SQLite instead of shelling out to `plaud search`
    /// (which cost ~300ms of `uv run` + Python import per keystroke burst).
    ///
    /// Mirrors `core/storage.py::search_recordings` exactly so both surfaces
    /// rank identically: trigram FTS5 when every term is >= 3 chars, else a
    /// LIKE scan so 2-char Korean terms still match.
    func searchContent(_ query: String, limit: Int = 200) -> [SearchHit] {
        let terms = query.split(whereSeparator: \.isWhitespace).map(String.init)
        guard !terms.isEmpty else { return [] }
        if terms.allSatisfy({ $0.count >= 3 }) {
            let hits = searchFTS(terms: terms, limit: limit)
            if !hits.isEmpty { return hits }
        }
        return searchLike(terms: terms, limit: limit)
    }

    private func searchFTS(terms: [String], limit: Int) -> [SearchHit] {
        let match = terms
            .map { "\"" + $0.replacingOccurrences(of: "\"", with: "\"\"") + "\"" }
            .joined(separator: " AND ")
        return read { db in
            try Row.fetchAll(db, sql: """
                SELECT file_id,
                       snippet(recording_fts, 2, '«', '»', '…', 12) AS snippet
                  FROM recording_fts
                 WHERE recording_fts MATCH ?
                 ORDER BY bm25(recording_fts, 10.0, 1.0)
                 LIMIT ?
            """, arguments: [match, limit]).map { row in
                SearchHit(fileID: row["file_id"] ?? "",
                          snippet: row["snippet"] ?? "")
            }
        } ?? []
    }

    private func searchLike(terms: [String], limit: Int) -> [SearchHit] {
        let clause = terms
            .map { _ in "(title LIKE ? OR summary_md LIKE ? OR transcript LIKE ?)" }
            .joined(separator: " AND ")
        var args: [DatabaseValueConvertible] = []
        for t in terms {
            let like = "%\(t)%"
            args.append(contentsOf: [like, like, like])
        }
        args.append(limit)
        return read { db in
            try Row.fetchAll(db, sql: """
                SELECT file_id, title, summary_md
                  FROM file_content
                 WHERE \(clause)
                 LIMIT ?
            """, arguments: StatementArguments(args)).map { row in
                let text: String = (row["summary_md"] as String?)
                    ?? (row["title"] as String?) ?? ""
                return SearchHit(fileID: row["file_id"] ?? "",
                                 snippet: String(text.prefix(120)))
            }
        } ?? []
    }

    func savedSpeakers() -> [Speaker] {
        read { db in
            try Row.fetchAll(db, sql: """
                SELECT id, name, is_self FROM speakers
                ORDER BY is_self DESC, name COLLATE NOCASE
            """).map(Speaker.init(row:))
        } ?? []
    }

    /// One conversation block detected by silence-gap heuristic.
    /// `startSec`/`endSec` are real-time seconds. `speakers` is the list of
    /// raw speaker labels that appear within this block, in first-seen order.
    /// `body` is the pre-formatted multi-line transcript for this block,
    /// ready for display or clipboard copy.
    struct CmdsSection: Identifiable, Hashable {
        let id: Int
        let startSec: Double
        let endSec: Double
        let speakers: [String]
        let preview: String
        let body: String
    }

    /// Split the CMDS transcript into conversation sections at gaps ≥ `gapSec`.
    /// Each section is independently relabel-able and copy-able.
    func cmdsSections(for fileID: String, gapSec: Double = 10.0) -> [CmdsSection] {
        let result: [CmdsSection]?? = read { db -> [CmdsSection]? in
            guard let row = try Row.fetchOne(db, sql:
                "SELECT segments FROM cmds_transcripts WHERE file_id = ?",
                arguments: [fileID]) else { return nil }
            let json: String = row["segments"] ?? "[]"
            struct Seg: Decodable {
                let speaker: String?
                let start_ms: Int
                let end_ms: Int
                let content: String
            }
            guard let arr = try? JSONDecoder().decode([Seg].self,
                from: Data(json.utf8)), !arr.isEmpty else { return [] }

            var sections: [CmdsSection] = []
            var bufStart = arr[0].start_ms
            var bufEnd = arr[0].end_ms
            var bufSpeakers: [String] = []
            var bufPreview = ""
            var bufLines: [String] = []

            func flush(id: Int) {
                sections.append(CmdsSection(
                    id: id,
                    startSec: Double(bufStart) / 1000.0,
                    endSec: Double(bufEnd) / 1000.0,
                    speakers: bufSpeakers,
                    preview: String(bufPreview.prefix(80)),
                    body: bufLines.joined(separator: "\n")
                ))
            }

            for seg in arr {
                let prevEnd = bufEnd
                if seg.start_ms - prevEnd > Int(gapSec * 1000) && !bufLines.isEmpty {
                    flush(id: sections.count)
                    bufStart = seg.start_ms
                    bufSpeakers = []
                    bufPreview = ""
                    bufLines = []
                }
                bufEnd = max(bufEnd, seg.end_ms)
                if let sp = seg.speaker, !bufSpeakers.contains(sp) {
                    bufSpeakers.append(sp)
                }
                if bufPreview.count < 80 {
                    bufPreview += " " + seg.content
                }
                let ts = Self.formatMinSec(seg.start_ms / 1000)
                bufLines.append("[\(ts)] \(seg.speaker ?? ""): \(seg.content)")
            }
            flush(id: sections.count)
            return sections
        }
        return (result ?? nil) ?? []
    }

    /// Distinct speaker labels currently used in this file's CMDS transcript,
    /// in chronological order. Used to populate the relabel sheet.
    func cmdsSpeakers(for fileID: String) -> [String] {
        let result: [String]?? = read { db -> [String]? in
            guard let row = try Row.fetchOne(db, sql:
                "SELECT segments FROM cmds_transcripts WHERE file_id = ?",
                arguments: [fileID]) else { return nil }
            let json: String = row["segments"] ?? "[]"
            struct Seg: Decodable { let speaker: String? }
            guard let arr = try? JSONDecoder().decode([Seg].self,
                from: Data(json.utf8)) else { return [] }
            var seen: [String] = []
            for s in arr {
                if let sp = s.speaker, !seen.contains(sp) { seen.append(sp) }
            }
            return seen
        }
        return (result ?? nil) ?? []
    }

    /// Cheap EXISTS probe for a cached CMDS transcript — avoids decoding the
    /// full `segments` JSON just to learn whether a row exists.
    func cmdsTranscriptExists(for fileID: String) -> Bool {
        read { db in
            try Bool.fetchOne(db, sql:
                "SELECT EXISTS(SELECT 1 FROM cmds_transcripts WHERE file_id = ?)",
                arguments: [fileID]) ?? false
        } ?? false
    }

    /// Returns the CMDS-side ElevenLabs transcript if cached, formatted with
    /// timestamps + speaker labels.
    func cmdsTranscript(for fileID: String) -> String? {
        let result: String?? = read { db -> String? in
            guard let row = try Row.fetchOne(db, sql: """
                SELECT segments FROM cmds_transcripts
                 WHERE file_id = ?
                 ORDER BY fetched_at DESC LIMIT 1
            """, arguments: [fileID]) else { return nil }
            let json: String = row["segments"] ?? "[]"
            struct Seg: Decodable {
                let speaker: String?; let start_ms: Int; let content: String
            }
            guard let arr = try? JSONDecoder().decode([Seg].self,
                from: Data(json.utf8)) else { return nil }
            return arr.map { s in
                let ts = Self.formatMinSec(s.start_ms / 1000)
                return "[\(ts)] \(s.speaker ?? ""): \(s.content)"
            }.joined(separator: "\n")
        }
        return result ?? nil
    }

    /// Newest integrated (Plaud × CMDS cross-analysis) artifact pair for a
    /// recording, read straight from `data/integrated/{id}/` — same
    /// resolution as the CLI's integrated-first pickers (newest mtime wins).
    func integratedContent(for fileID: String) -> IntegratedContentVM? {
        let dir = URL(fileURLWithPath: "\(Self.projectRoot)/data/integrated/\(fileID)")
        guard let files = try? FileManager.default.contentsOfDirectory(
            at: dir, includingPropertiesForKeys: [.contentModificationDateKey]
        ) else { return nil }
        let summaries = files.filter { $0.lastPathComponent.hasSuffix(".summary.md") }
        guard let newest = summaries.max(by: {
            let a = (try? $0.resourceValues(forKeys: [.contentModificationDateKey])
                .contentModificationDate) ?? .distantPast
            let b = (try? $1.resourceValues(forKeys: [.contentModificationDateKey])
                .contentModificationDate) ?? .distantPast
            return a < b
        }) else { return nil }
        let transcriptURL = URL(fileURLWithPath: newest.path
            .replacingOccurrences(of: ".summary.md", with: ".transcript.md"))
        let summary = Self.stripFrontmatter(
            (try? String(contentsOf: newest, encoding: .utf8)) ?? "")
        let transcript = Self.stripFrontmatter(
            (try? String(contentsOf: transcriptURL, encoding: .utf8)) ?? "")
        guard !summary.isEmpty || !transcript.isEmpty else { return nil }
        return IntegratedContentVM(
            fileID: fileID,
            label: newest.lastPathComponent.replacingOccurrences(of: ".summary.md", with: ""),
            transcript: transcript,
            summary: summary
        )
    }

    /// Dual-transcribe pipeline state, or nil when the recording isn't marked.
    func dualState(for fileID: String) -> DualStateVM? {
        let result: DualStateVM?? = read { db -> DualStateVM? in
            guard let row = try Row.fetchOne(
                db, sql: "SELECT * FROM dual_pipeline WHERE file_id = ?",
                arguments: [fileID]
            ) else { return nil }
            var proposal: [SpeakerProposalVM] = []
            if let json: String = row["speaker_proposal"],
               let decoded = try? JSONDecoder().decode(
                   [SpeakerProposalVM].self, from: Data(json.utf8)
               ) {
                proposal = decoded
            }
            var map: [String: String] = [:]
            if let json: String = row["speaker_map"],
               let decoded = try? JSONDecoder().decode(
                   [String: String].self, from: Data(json.utf8)
               ) {
                map = decoded
            }
            return DualStateVM(
                fileID: fileID,
                status: row["status"] ?? "marked",
                proposal: proposal,
                speakerMap: map,
                vaultPath: row["vault_path"],
                error: row["error"]
            )
        }
        return result ?? nil
    }

    func reuseMarks(for fileID: String) -> [ReuseMarkVM] {
        read { db in
            try Row.fetchAll(
                db,
                sql: "SELECT * FROM note_reuse WHERE file_id = ? ORDER BY channel",
                arguments: [fileID]
            ).map(ReuseMarkVM.init(row:))
        } ?? []
    }

    func noteMetadata(for fileID: String) -> NoteMetadataVM {
        let result: NoteMetadataVM?? = read { db -> NoteMetadataVM? in
            let meta = try Row.fetchOne(db,
                sql: "SELECT * FROM note_metadata WHERE file_id = ?",
                arguments: [fileID])
            let tags = try Row.fetchAll(db, sql: """
                SELECT tag, source
                  FROM note_tags
                 WHERE file_id = ?
                 ORDER BY CASE source
                            WHEN 'manual' THEN 0
                            WHEN 'ai' THEN 1
                            WHEN 'auto' THEN 2
                            ELSE 3
                          END,
                          tag COLLATE NOCASE
            """, arguments: [fileID]).map(NoteTagVM.init(row:))
            let refs = try Row.fetchAll(db, sql: """
                SELECT path, kind, title
                  FROM note_references
                 WHERE file_id = ?
                 ORDER BY updated_at DESC
            """, arguments: [fileID]).map(NoteReferenceVM.init(row:))
            return NoteMetadataVM(
                fileID: fileID,
                title: meta?["title"],
                description: meta?["description"],
                noteType: meta?["note_type"],
                status: meta?["status"],
                usageStatus: meta?["usage_status"],
                category: meta?["category"],
                folderName: meta?["folder_name"],
                vaultPath: meta?["vault_path"],
                draftPath: meta?["draft_path"],
                finalNotePath: meta?["final_note_path"],
                tags: tags,
                references: refs
            )
        }
        return (result ?? nil) ?? NoteMetadataVM(
            fileID: fileID,
            title: nil,
            description: nil,
            noteType: nil,
            status: nil,
            usageStatus: nil,
            category: nil,
            folderName: nil,
            vaultPath: nil,
            draftPath: nil,
            finalNotePath: nil,
            tags: [],
            references: []
        )
    }

    func content(for fileID: String) -> FileContentVM? {
        let result: FileContentVM?? = read { db -> FileContentVM? in
            guard let row = try Row.fetchOne(db,
                sql: "SELECT * FROM file_content WHERE file_id = ?",
                arguments: [fileID]) else { return nil }
            let transcriptJSON: String = row["transcript"] ?? "[]"
            let outlineJSON: String = row["outline"] ?? "[]"
            let keywordsJSON: String = row["keywords"] ?? "[]"
            let summaryMD: String? = row["summary_md"]
            let extraJSON: String = row["summary_extra"] ?? "[]"
            return FileContentVM(
                title: row["title"],
                transcript: Self.formatTranscript(transcriptJSON),
                outline: Self.formatOutline(outlineJSON),
                summaries: Self.assembleSummaries(primary: summaryMD, extraJSON: extraJSON),
                keywords: (try? JSONDecoder().decode([String].self,
                    from: Data(keywordsJSON.utf8))) ?? []
            )
        }
        return result ?? nil
    }

    private static func assembleSummaries(primary: String?, extraJSON: String) -> [SummaryVM] {
        var out: [SummaryVM] = []
        if let p = primary, !p.isEmpty {
            out.append(SummaryVM(id: "summary", title: "Summary", body: p))
        }
        let extras = (try? JSONDecoder().decode([String].self,
            from: Data(extraJSON.utf8))) ?? []
        for (i, body) in extras.enumerated() {
            out.append(SummaryVM(id: "extra-\(i)", title: "Template \(i + 1)", body: body))
        }
        return out
    }

    private static func formatTranscript(_ json: String) -> String {
        struct Seg: Decodable { let start_time: Int; let content: String; let speaker: String? }
        guard let arr = try? JSONDecoder().decode([Seg].self,
            from: Data(json.utf8)) else { return "" }
        return arr.map { seg in
            let s = max(0, seg.start_time / 1000)
            let ts = formatMinSec(s)
            return "[\(ts)] \(seg.speaker ?? ""): \(seg.content)"
        }.joined(separator: "\n")
    }

    private static func formatOutline(_ json: String) -> String {
        struct Item: Decodable { let start_time: Int; let topic: String }
        guard let arr = try? JSONDecoder().decode([Item].self,
            from: Data(json.utf8)) else { return "" }
        return arr.map { item in
            let s = max(0, item.start_time / 1000)
            let ts = formatMinSec(s)
            return "- [\(ts)] \(item.topic)"
        }.joined(separator: "\n")
    }
}

/// Watches the SQLite database files for changes and posts `.plaudDBChanged`.
///
/// The 1-second polling timer is paused while the app is inactive
/// (`didResignActive`) and resumed on `didBecomeActive`, so it does not burn
/// the main run loop in the background. A `.plaudDBChanged` post only happens
/// when the on-disk signature (mtime + size of `.db`/`-wal`/`-shm`) actually
/// changes, so a quiescent WAL does not generate work every tick.
@MainActor
final class DatabaseWatcher {
    static let shared = DatabaseWatcher()
    private var timer: Timer?
    private var lastSignature: String = ""
    private var lifecycleObservers: [NSObjectProtocol] = []
    private let dbPath: String = Database.shared.path

    /// In WAL mode SQLite writes the journal to `<db>-wal` and the main `.db`
    /// only gets touched on checkpoints. Watching both files (mtime + size)
    /// catches every write within ~1 second.
    private var watchedPaths: [String] {
        [dbPath, dbPath + "-wal", dbPath + "-shm"]
    }

    /// Begin watching. Registers app lifecycle observers (once) so the poll
    /// timer suspends in the background and resumes on activation.
    func start() {
        registerLifecycleObservers()
        startTimer()
    }

    private func registerLifecycleObservers() {
        guard lifecycleObservers.isEmpty else { return }
        let center = NotificationCenter.default
        lifecycleObservers.append(
            center.addObserver(forName: NSApplication.didResignActiveNotification,
                               object: nil, queue: .main) { [weak self] _ in
                MainActor.assumeIsolated { self?.stopTimer() }
            }
        )
        lifecycleObservers.append(
            center.addObserver(forName: NSApplication.didBecomeActiveNotification,
                               object: nil, queue: .main) { [weak self] _ in
                MainActor.assumeIsolated { self?.startTimer() }
            }
        )
    }

    private func startTimer() {
        guard timer == nil else { return }
        let t = Timer(timeInterval: 1.0, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.tick() }
        }
        RunLoop.main.add(t, forMode: .common)
        timer = t
        tick()
    }

    /// Suspend polling without losing the last-seen signature, so we don't
    /// fire a spurious change notification when we resume.
    private func stopTimer() {
        timer?.invalidate()
        timer = nil
    }

    private func tick() {
        let signature = watchedPaths.map { path -> String in
            guard let attrs = try? FileManager.default.attributesOfItem(atPath: path)
            else { return "" }
            let mtime = (attrs[.modificationDate] as? Date)?.timeIntervalSince1970 ?? 0
            let size = (attrs[.size] as? UInt64) ?? 0
            return "\(mtime):\(size)"
        }.joined(separator: "|")
        guard signature != lastSignature else { return }
        lastSignature = signature
        NotificationCenter.default.post(name: .plaudDBChanged, object: nil)
    }
}

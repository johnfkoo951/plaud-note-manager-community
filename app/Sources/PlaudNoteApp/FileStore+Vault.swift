import AppKit
import Foundation

/// Decoded `plaud vault-send --json` result. Note bodies never travel through
/// this payload — the CLI writes the file and reports only where it landed.
struct VaultSendOutcome: Decodable, Equatable {
    var status: String
    var detail: String?
    var path: String?
    var vault: String?
    var dest: String?
    var content: String?
    var via: String?
    var obsidianURL: String?

    enum CodingKeys: String, CodingKey {
        case status, detail, path, vault, dest, content, via
        case obsidianURL = "obsidian_url"
    }

    var noteFilename: String {
        (path as NSString?)?.lastPathComponent ?? ""
    }
}

extension FileStore {
    /// Send a recording's generated content into an Obsidian vault via
    /// `plaud vault-send`. Content defaults to the integrated output (with
    /// summary/plaud fallback in the CLI). `via`:
    ///   direct        instant, no AI
    ///   claude        headless `claude -p` reformat (slow — minutes)
    ///   claude-window interactive Terminal filing (returns immediately)
    @discardableResult
    func vaultSend(
        _ fileID: String,
        to: String = "main",
        dest: String = "",
        content: String = "integrated",
        via: String = "direct",
        model: String = "",
        template: String = "",
        withTranscript: Bool = false
    ) async -> Bool {
        guard !fileID.isEmpty, !vaultSendingIDs.contains(fileID) else { return false }
        vaultSendingIDs.insert(fileID)
        defer { vaultSendingIDs.remove(fileID) }

        var args = ["vault-send", fileID, "--to", to, "--content", content, "--via", via, "--json"]
        if !dest.isEmpty { args += ["--dest", dest] }
        if !model.isEmpty { args += ["--model", model] }
        if !template.isEmpty { args += ["--template", template] }
        if withTranscript { args.append("--with-transcript") }

        // Headless AI reformatting legitimately takes minutes; direct writes
        // are near-instant and claude-window returns as soon as Terminal opens.
        let timeout: TimeInterval = via == "claude" ? 960 : 60
        let output = await runPlaudOutput(args: args, timeout: timeout, showError: false)
        let trimmed = output.trimmingCharacters(in: .whitespacesAndNewlines)

        guard let data = trimmed.data(using: .utf8), !data.isEmpty,
              let outcome = try? JSONDecoder().decode(VaultSendOutcome.self, from: data)
        else {
            lastCommandError = trimmed.isEmpty
                ? "볼트 전송에 실패했습니다 — Plaud CLI에서 응답이 없습니다."
                : "볼트 전송 응답을 해석하지 못했습니다: \(trimmed.prefix(200))"
            return false
        }

        switch outcome.status {
        case "ok":
            lastCommandError = nil
            if via != "claude-window" {
                lastVaultSend = outcome
            }
            noteMetadata = Database.shared.noteMetadata(for: fileID)
            return true
        case "no_content":
            lastCommandError =
                "보낼 결과물이 없습니다 — Work Sidebar에서 Summary/Integrated를 먼저 Generate 해주세요."
            return false
        case "no_vault":
            lastCommandError =
                "볼트가 설정되지 않았습니다 — Settings 또는 `plaud config-vault`로 경로를 지정해 주세요."
            return false
        default:
            let detail = outcome.detail?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            let suffix = detail.isEmpty ? "" : " — \(detail)"
            lastCommandError = "볼트 전송에 실패했습니다 (\(outcome.status))\(suffix)"
            return false
        }
    }

    /// Open a sent note in Obsidian via its obsidian:// URL.
    func openVaultNote(_ outcome: VaultSendOutcome) {
        if let raw = outcome.obsidianURL, let url = URL(string: raw) {
            NSWorkspace.shared.open(url)
        } else if let path = outcome.path {
            NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: path)])
        }
    }
}

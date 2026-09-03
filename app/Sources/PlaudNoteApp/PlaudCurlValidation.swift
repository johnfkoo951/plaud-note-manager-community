import Foundation

/// Fast, secret-free checks for text pasted into the authentication sheet.
///
/// Python remains authoritative and performs the live Plaud probe. This layer
/// gives obvious clipboard mistakes a specific explanation without launching a
/// subprocess or reflecting any credential value back into the UI.
enum PlaudCurlValidation: Equatable {
    case empty
    case tooLarge
    case invalidCharacters
    case urlOnly
    case notCurl
    case wrongTarget
    case missingAuthorization
    case invalidAuthorization
    case missingDeviceID
    case ready

    var canImport: Bool { self == .ready }

    var message: String {
        switch self {
        case .empty:
            return "Paste a request copied with DevTools > Copy as cURL."
        case .tooLarge:
            return "The clipboard is unexpectedly large. Copy one Plaud Network request."
        case .invalidCharacters:
            return "The clipboard contains an invalid character. Copy the Plaud request again."
        case .urlOnly:
            return "A URL alone is not enough. Use Copy > Copy as cURL in DevTools."
        case .notCurl:
            return "The clipboard is not a cURL command copied from Plaud DevTools."
        case .wrongTarget:
            return "The cURL must target an HTTPS Plaud API host such as api-apne1.plaud.ai."
        case .missingAuthorization:
            return "The copied request is missing its authorization header."
        case .invalidAuthorization:
            return "The authorization header must contain a non-empty Bearer token."
        case .missingDeviceID:
            return "The copied request is missing its x-device-id header."
        case .ready:
            return "Ready. Plaud will be checked before anything replaces Keychain."
        }
    }
}

enum PlaudCurlValidator {
    private static let maximumUTF8Bytes = 256 * 1024

    static func inspect(_ raw: String) -> PlaudCurlValidation {
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return .empty }
        guard trimmed.lengthOfBytes(using: .utf8) <= maximumUTF8Bytes else {
            return .tooLarge
        }
        guard !trimmed.contains("\0") else { return .invalidCharacters }

        let text = normalizeWindowsCmd(trimmed)
        if URL(string: text)?.scheme?.lowercased() == "https" {
            return .urlOnly
        }
        guard isCurlCommand(text) else { return .notCurl }
        guard containsPlaudAPITarget(text) else { return .wrongTarget }
        guard containsHeader(named: "authorization", in: text) else {
            return .missingAuthorization
        }
        guard containsBearerAuthorization(in: text) else {
            return .invalidAuthorization
        }
        guard containsHeader(named: "x-device-id", in: text) else {
            return .missingDeviceID
        }
        return .ready
    }

    private static func normalizeWindowsCmd(_ text: String) -> String {
        text
            .replacingOccurrences(of: "^\r\n", with: " ")
            .replacingOccurrences(of: "^\n", with: " ")
            .replacingOccurrences(
                of: #"\^([\"&|<>^])"#,
                with: "$1",
                options: .regularExpression
            )
    }

    private static func isCurlCommand(_ text: String) -> Bool {
        guard let first = text.split(whereSeparator: { $0.isWhitespace }).first else {
            return false
        }
        let unquoted = first.trimmingCharacters(in: CharacterSet(charactersIn: "'\""))
        let executable = unquoted
            .replacingOccurrences(of: "\\", with: "/")
            .split(separator: "/")
            .last?
            .lowercased()
        return executable == "curl" || executable == "curl.exe"
    }

    private static func containsPlaudAPITarget(_ text: String) -> Bool {
        guard let detector = try? NSDataDetector(
            types: NSTextCheckingResult.CheckingType.link.rawValue
        ) else { return false }
        let range = NSRange(text.startIndex..<text.endIndex, in: text)
        return detector.matches(in: text, options: [], range: range).contains { match in
            guard let url = match.url,
                  url.scheme?.lowercased() == "https",
                  url.user == nil,
                  url.password == nil,
                  url.port == nil || url.port == 443,
                  let host = url.host?.lowercased()
            else { return false }
            return host == "api.plaud.ai"
                || (host.hasPrefix("api-") && host.hasSuffix(".plaud.ai"))
        }
    }

    private static func containsHeader(named name: String, in text: String) -> Bool {
        let escaped = NSRegularExpression.escapedPattern(for: name)
        let pattern = #"(?i)(?:^|\s)(?:-H|--header(?:\s*=)?)(?:\s*=?\s*)(?:\$?['\"])?"#
            + escaped
            + #"\s*:\s*[^\s'\"]+"#
        return text.range(of: pattern, options: .regularExpression) != nil
    }

    private static func containsBearerAuthorization(in text: String) -> Bool {
        let pattern = #"(?i)(?:^|\s)(?:-H|--header(?:\s*=)?)(?:\s*=?\s*)(?:\$?['\"])?authorization\s*:\s*bearer\s+[^\s'\"]+"#
        return text.range(of: pattern, options: .regularExpression) != nil
    }
}

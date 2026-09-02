import Foundation

enum DistributionProfile {
    static let isCommunity = true
    static let appName = "Plaud Note Manager Community"
    static let bundleIdentifier = "com.cmdspace.PlaudNoteManagerCommunity"
    static let keychainService = "com.cmdspace.PlaudNoteManagerCommunity.auth"
    static let appSupportID = "com.cmdspace.PlaudNoteManagerCommunity"
}

enum PlaudRuntimeError: LocalizedError {
    case missingExecutable([String])
    case runtimeUnavailable(String)

    var errorDescription: String? {
        switch self {
        case .missingExecutable(let candidates):
            return "Plaud runtime not found. Checked: \(candidates.joined(separator: ", "))"
        case .runtimeUnavailable(let detail):
            return detail
        }
    }
}

/// Resolves all paths used by the packaged community build.
///
/// A release bundle is self-contained: Python and the CLI live below
/// `Contents/Resources`, while every user-created file lives below the user's
/// Application Support directory. Development builds use the checkout's own
/// `data/` directory, but still use the community Keychain namespace.
enum RuntimePaths {
    static let isPackagedApp = Bundle.main.bundleURL.pathExtension == "app"

    static let projectRoot: URL = {
        if isPackagedApp, let resources = Bundle.main.resourceURL {
            return resources.appendingPathComponent("runtime", isDirectory: true)
        }
        if let raw = ProcessInfo.processInfo.environment["PLAUD_PROJECT_ROOT"], !raw.isEmpty {
            return URL(fileURLWithPath: NSString(string: raw).expandingTildeInPath,
                       isDirectory: true)
        }
        return URL(fileURLWithPath: FileManager.default.currentDirectoryPath,
                   isDirectory: true)
    }()

    static let appSupportRoot: URL = {
        let base = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support", isDirectory: true)
        return base.appendingPathComponent(DistributionProfile.appSupportID, isDirectory: true)
    }()

    static let dataDirectory: URL = {
        if isPackagedApp {
            return appSupportRoot.appendingPathComponent("data", isDirectory: true)
        }
        if let raw = ProcessInfo.processInfo.environment["PLAUD_DATA_DIR"], !raw.isEmpty {
            return URL(fileURLWithPath: NSString(string: raw).expandingTildeInPath,
                       isDirectory: true)
        }
        return projectRoot.appendingPathComponent("data", isDirectory: true)
    }()

    static let templatesDirectory: URL = projectRoot
        .appendingPathComponent("templates", isDirectory: true)

    static let databaseURL: URL = dataDirectory.appendingPathComponent("plaud.db")
    static let envURL: URL = appSupportRoot.appendingPathComponent("settings.env")

    private static let uvCandidates = [
        "\(NSHomeDirectory())/.local/bin/uv",
        "/opt/homebrew/bin/uv",
        "/usr/local/bin/uv",
    ]

    private static let cliPath = "/usr/bin:/bin:/usr/sbin:/sbin"

    static func makePlaudProcess(args: [String]) throws -> Process {
        try FileManager.default.createDirectory(
            at: dataDirectory, withIntermediateDirectories: true
        )
        try? FileManager.default.setAttributes(
            [.posixPermissions: 0o700], ofItemAtPath: appSupportRoot.path
        )
        try? FileManager.default.setAttributes(
            [.posixPermissions: 0o700], ofItemAtPath: dataDirectory.path
        )

        let process = Process()
        process.currentDirectoryURL = isPackagedApp ? appSupportRoot : projectRoot
        process.environment = isolatedEnvironment()

        if isPackagedApp {
            guard let resources = Bundle.main.resourceURL else {
                throw PlaudRuntimeError.runtimeUnavailable("App resources are unavailable.")
            }
            let python = resources
                .appendingPathComponent("python", isDirectory: true)
                .appendingPathComponent("bin/python3")
            guard FileManager.default.isExecutableFile(atPath: python.path) else {
                throw PlaudRuntimeError.missingExecutable([python.path])
            }
            process.executableURL = python
            process.arguments = ["-I", "-B", "-m", "cli.main"] + args
            return process
        }

        guard let uvPath = uvCandidates.first(where: {
            FileManager.default.isExecutableFile(atPath: $0)
        }) else {
            throw PlaudRuntimeError.missingExecutable(uvCandidates)
        }
        process.executableURL = URL(fileURLWithPath: uvPath)
        process.arguments = ["run", "plaud"] + args
        return process
    }

    /// Do not inherit API keys, Plaud credentials, shell hooks, or another
    /// checkout's path overrides. Only ordinary OS context plus explicit
    /// community-runtime values cross the Swift -> Python boundary.
    private static func isolatedEnvironment() -> [String: String] {
        let host = ProcessInfo.processInfo.environment
        var environment: [String: String] = [:]
        for key in ["HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL", "TZ"] {
            if let value = host[key], !value.isEmpty { environment[key] = value }
        }
        environment["PATH"] = cliPath
        environment["PYTHONUNBUFFERED"] = "1"
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PYTHONNOUSERSITE"] = "1"
        environment["PLAUD_DISTRIBUTION_PROFILE"] = "community"
        environment["PLAUD_DATA_DIR"] = dataDirectory.path
        environment["PLAUD_RESOURCE_ROOT"] = projectRoot.path
        environment["PLAUD_TEMPLATES_DIR"] = templatesDirectory.path
        environment["PLAUD_ENV_FILE"] = envURL.path
        environment["PLAUD_KEYCHAIN_SERVICE"] = DistributionProfile.keychainService
        environment["PLAUD_APP_SUPPORT_ID"] = DistributionProfile.appSupportID
        environment["PLAUD_AUTO_REFRESH"] = "1"

        if isPackagedApp, let resources = Bundle.main.resourceURL {
            environment["SSL_CERT_FILE"] = resources
                .appendingPathComponent("python/lib/python3.12/site-packages/certifi/cacert.pem")
                .path
        }
        return environment
    }

    static func resolve(subpath: String) -> URL {
        if subpath == "data" { return dataDirectory }
        if subpath.hasPrefix("data/") {
            return dataDirectory.appendingPathComponent(String(subpath.dropFirst(5)))
        }
        if subpath == "templates" { return templatesDirectory }
        if subpath.hasPrefix("templates/") {
            return templatesDirectory.appendingPathComponent(String(subpath.dropFirst(10)))
        }
        return projectRoot.appendingPathComponent(subpath)
    }
}

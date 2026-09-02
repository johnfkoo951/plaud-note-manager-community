import SwiftUI
import WebKit

private let plaudAuthMessageName = "plaudAuthCapture"

enum PlaudWebSession {
    static func clear(completion: @escaping () -> Void) {
        // Wipe everything, not just *.plaud.ai — Google SSO cookies otherwise
        // survive and the live page silently stays logged in. Safe because
        // this is the app's only WKWebView.
        let store = WKWebsiteDataStore.default()
        store.removeData(
            ofTypes: WKWebsiteDataStore.allWebsiteDataTypes(),
            modifiedSince: .distantPast
        ) {
            DispatchQueue.main.async { completion() }
        }
    }
}

struct PlaudWebLoginView: NSViewRepresentable {
    var onCapture: (PlaudWebAuthCapture) -> Void
    var onStatus: (String) -> Void
    /// When true, use Plaud's persistent HttpOnly account session to mint a
    /// brand-new workspace credential pair. Interactive login leaves this off.
    var recoveryMode: Bool = false
    /// Bumped by the owner after a failed capture or a session clear. Each
    /// change resets the coordinator's one-shot capture latch and reloads the
    /// login page so a fresh attempt is possible.
    var captureGeneration: Int = 0

    func makeCoordinator() -> Coordinator {
        Coordinator(
            onCapture: onCapture,
            onStatus: onStatus,
            recoveryMode: recoveryMode
        )
    }

    func makeNSView(context: Context) -> WKWebView {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .default()
        let script = WKUserScript(
            source: plaudAuthCaptureScript,
            injectionTime: .atDocumentStart,
            forMainFrameOnly: false
        )
        configuration.userContentController.addUserScript(script)
        configuration.userContentController.add(
            context.coordinator,
            name: plaudAuthMessageName
        )

        let webView = WKWebView(frame: .zero, configuration: configuration)
        webView.navigationDelegate = context.coordinator
        webView.uiDelegate = context.coordinator
        context.coordinator.webView = webView
        context.coordinator.lastSeenGeneration = captureGeneration
        webView.load(URLRequest(url: URL(string: "https://web.plaud.ai/")!))
        return webView
    }

    func updateNSView(_ webView: WKWebView, context: Context) {
        guard context.coordinator.lastSeenGeneration != captureGeneration else { return }
        context.coordinator.lastSeenGeneration = captureGeneration
        context.coordinator.resetCaptureState()
        webView.load(URLRequest(url: URL(string: "https://web.plaud.ai/")!))
    }

    static func dismantleNSView(_ webView: WKWebView, coordinator: Coordinator) {
        webView.stopLoading()
        webView.navigationDelegate = nil
        webView.uiDelegate = nil
        webView.configuration.userContentController.removeScriptMessageHandler(
            forName: plaudAuthMessageName
        )
        webView.configuration.userContentController.removeAllUserScripts()
        coordinator.webView = nil
    }

    final class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler {
        var latestHeaders: [String: String] = [:]
        var latestURL: URL?
        var latestWorkspaceList: String?
        var isEmitting = false
        var didCapture = false
        var didStartAccountRecovery = false
        var cookieReadAttempts = 0
        var workspaceReadAttempts = 0
        var lastSeenGeneration = 0
        weak var webView: WKWebView?

        private let onCapture: (PlaudWebAuthCapture) -> Void
        private let onStatus: (String) -> Void
        private let recoveryMode: Bool

        init(
            onCapture: @escaping (PlaudWebAuthCapture) -> Void,
            onStatus: @escaping (String) -> Void,
            recoveryMode: Bool
        ) {
            self.onCapture = onCapture
            self.onStatus = onStatus
            self.recoveryMode = recoveryMode
        }

        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            if recoveryMode {
                let path = webView.url?.path.lowercased() ?? ""
                if path.contains("login") || path.contains("sign-in") {
                    onStatus("Plaud account sign-in required.")
                    return
                }
                runAccountSessionRecovery(in: webView)
            } else {
                onStatus("Embedded login waiting for Plaud API traffic.")
            }
            emitIfComplete()
        }

        private func runAccountSessionRecovery(in webView: WKWebView) {
            guard !didStartAccountRecovery, !didCapture else { return }
            didStartAccountRecovery = true
            onStatus("Recovering from the saved Plaud account session…")
            webView.evaluateJavaScript(plaudAccountSessionRecoveryScript) { value, error in
                DispatchQueue.main.async {
                    if let error {
                        self.onStatus(
                            "Plaud account-session recovery failed: "
                                + error.localizedDescription
                        )
                        return
                    }
                    if let result = value as? [String: Any],
                       let status = result["status"] as? String,
                       status == "needsLogin" {
                        self.onStatus("Plaud account sign-in required.")
                    }
                }
            }
        }

        func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
            onStatus("Plaud Web failed to load: \(error.localizedDescription)")
        }

        func webView(
            _ webView: WKWebView,
            didFailProvisionalNavigation navigation: WKNavigation!,
            withError error: Error
        ) {
            let nsError = error as NSError
            if nsError.domain == NSURLErrorDomain, nsError.code == NSURLErrorCancelled { return }
            onStatus("Plaud Web failed to load: \(error.localizedDescription)")
        }

        /// Clears the one-shot capture latch so the embedded login can emit
        /// again after a rejected capture or a session reset.
        func resetCaptureState() {
            didCapture = false
            isEmitting = false
            cookieReadAttempts = 0
            workspaceReadAttempts = 0
            latestWorkspaceList = nil
            latestHeaders.removeAll()
            latestURL = nil
            didStartAccountRecovery = false
        }

        func webView(
            _ webView: WKWebView,
            createWebViewWith configuration: WKWebViewConfiguration,
            for navigationAction: WKNavigationAction,
            windowFeatures: WKWindowFeatures
        ) -> WKWebView? {
            if navigationAction.targetFrame == nil {
                webView.load(navigationAction.request)
            }
            return nil
        }

        func userContentController(
            _ userContentController: WKUserContentController,
            didReceive message: WKScriptMessage
        ) {
            // Only trust messages posted from Plaud's own origin — the user
            // script runs in every frame (Google SSO included).
            let host = message.frameInfo.securityOrigin.host
            guard host == "web.plaud.ai" || host.hasSuffix(".plaud.ai") else { return }
            guard message.name == plaudAuthMessageName,
                  let body = message.body as? [String: Any]
            else {
                return
            }
            if body["kind"] as? String == "recoveryStatus" {
                let state = body["status"] as? String ?? "failed"
                let detail = body["detail"] as? String ?? ""
                if state == "needsLogin" {
                    onStatus("Plaud account sign-in required.")
                } else if state == "failed" {
                    onStatus("Plaud silent recovery failed: \(detail)")
                } else if state == "starting" {
                    onStatus("Recovering from the saved Plaud account session…")
                }
                return
            }
            guard let headers = body["headers"] as? [String: Any] else { return }
            var requestHeaders: [String: String] = [:]
            for (key, value) in headers {
                requestHeaders[key.lowercased()] = String(describing: value)
            }
            // Keep one request intact. Merging headers from unrelated API
            // requests can pair a token with the wrong device/base URL.
            guard requestHeaders["authorization"]?.isEmpty == false,
                  requestHeaders["x-device-id"]?.isEmpty == false
            else { return }
            latestHeaders = requestHeaders
            if let urlString = body["url"] as? String {
                latestURL = URL(string: urlString)
            }
            if let workspaceList = body["workspaceList"] as? String,
               !workspaceList.isEmpty {
                latestWorkspaceList = workspaceList
            }
            cookieReadAttempts = 0
            workspaceReadAttempts = 0
            emitIfComplete()
        }

        private func header(_ key: String) -> String? {
            latestHeaders[key.lowercased()]?
                .trimmingCharacters(in: .whitespacesAndNewlines)
        }

        private func emitIfComplete() {
            guard !didCapture, !isEmitting, let webView else { return }
            guard let authorization = header("authorization"),
                  let deviceID = header("x-device-id"),
                  !authorization.isEmpty,
                  !deviceID.isEmpty
            else {
                onStatus("Embedded login waiting for Plaud auth headers.")
                return
            }
            let user = header("x-pld-user")

            isEmitting = true
            webView.configuration.websiteDataStore.httpCookieStore.getAllCookies { cookies in
                let cookieLine = cookies
                    .filter {
                        let name = $0.name.lowercased()
                        return $0.domain.lowercased().contains("plaud.ai")
                            && name != "pld_ut"
                            && name != "pld_urt"
                    }
                    .sorted { $0.name < $1.name }
                    .map { "\($0.name)=\($0.value)" }
                    .joined(separator: "; ")

                DispatchQueue.main.async {
                    if cookieLine.isEmpty && self.cookieReadAttempts < 6 {
                        self.cookieReadAttempts += 1
                        self.isEmitting = false
                        self.onStatus("Captured headers; waiting for Plaud cookies.")
                        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
                            self.emitIfComplete()
                        }
                        return
                    }
                    self.finishCapture(
                        authorization: authorization,
                        deviceID: deviceID,
                        user: user,
                        cookieLine: cookieLine
                    )
                }
            }
        }

        private func finishCapture(
            authorization: String,
            deviceID: String,
            user: String?,
            cookieLine: String
        ) {
            // Plaud 3.x stores this under `pld_<JWT sub>:workspaceList`, not
            // the old raw `workspaceList` key.  Select only the current JWT's
            // account + workspace; never scan another signed-in account's
            // namespaced value.
            guard let webView else {
                emitCapture(
                    authorization: authorization, deviceID: deviceID, user: user,
                    cookieLine: cookieLine, workspaceList: nil
                )
                return
            }
            if let workspaceList = latestWorkspaceList, !workspaceList.isEmpty {
                emitCapture(
                    authorization: authorization,
                    deviceID: deviceID,
                    user: user,
                    cookieLine: cookieLine,
                    workspaceList: workspaceList
                )
                return
            }
            guard let claims = workspaceClaims(authorization: authorization) else {
                isEmitting = false
                onStatus("Waiting for a Plaud workspace session.")
                return
            }
            let script = workspaceListScript(sub: claims.sub, wid: claims.wid)
            webView.evaluateJavaScript(script) { value, _ in
                DispatchQueue.main.async {
                    if let workspaceList = value as? String, !workspaceList.isEmpty {
                        self.emitCapture(
                            authorization: authorization, deviceID: deviceID, user: user,
                            cookieLine: cookieLine, workspaceList: workspaceList
                        )
                        return
                    }
                    // The request hook fires before Plaud's response stores the
                    // rotating token. Poll for up to 12 seconds instead of
                    // completing a false-success access-token-only login.
                    if self.workspaceReadAttempts < 30 {
                        self.workspaceReadAttempts += 1
                        self.onStatus("Connected; waiting for automatic-renewal token…")
                        DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) {
                            self.finishCapture(
                                authorization: authorization,
                                deviceID: deviceID,
                                user: user,
                                cookieLine: cookieLine
                            )
                        }
                    } else {
                        self.emitCapture(
                            authorization: authorization, deviceID: deviceID, user: user,
                            cookieLine: cookieLine, workspaceList: nil
                        )
                    }
                }
            }
        }

        private func workspaceClaims(authorization: String) -> (sub: String, wid: String)? {
            let token = authorization
                .replacingOccurrences(of: "^Bearer\\s+", with: "", options: [
                    .regularExpression, .caseInsensitive
                ])
                .trimmingCharacters(in: .whitespacesAndNewlines)
            let parts = token.split(separator: ".", omittingEmptySubsequences: false)
            guard parts.count == 3 else { return nil }
            var encoded = String(parts[1])
                .replacingOccurrences(of: "-", with: "+")
                .replacingOccurrences(of: "_", with: "/")
            encoded += String(repeating: "=", count: (4 - encoded.count % 4) % 4)
            guard let data = Data(base64Encoded: encoded),
                  let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let sub = object["sub"] as? String, !sub.isEmpty,
                  let wid = object["wid"] as? String, !wid.isEmpty
            else { return nil }
            return (sub, wid)
        }

        private func javascriptLiteral(_ value: String) -> String {
            guard let data = try? JSONEncoder().encode(value),
                  let literal = String(data: data, encoding: .utf8)
            else { return "\"\"" }
            return literal
        }

        private func workspaceListScript(sub: String, wid: String) -> String {
            let subLiteral = javascriptLiteral(sub)
            let widLiteral = javascriptLiteral(wid)
            return """
            (() => {
              const expectedSub = \(subLiteral);
              const expectedWid = \(widLiteral);
              const preferredKey = `pld_${expectedSub}:workspaceList`;
              const namespacedKeys = [];
              for (let i = 0; i < localStorage.length; i += 1) {
                const key = localStorage.key(i);
                if (key && key.endsWith(':workspaceList')) namespacedKeys.push(key);
              }
              const preferredRaw = localStorage.getItem(preferredKey);
              const rawValues = [];
              if (preferredRaw !== null) {
                rawValues.push(preferredRaw);
              } else if (namespacedKeys.length === 0) {
                for (const key of ['workspaceList', 'pld_workspaceList']) {
                  const raw = localStorage.getItem(key);
                  if (raw !== null) rawValues.push(raw);
                }
              }
              const decode = (raw) => {
                let value = raw;
                for (let i = 0; i < 2 && typeof value === 'string'; i += 1) {
                  try { value = JSON.parse(value); } catch (_) { return []; }
                }
                if (Array.isArray(value)) return value;
                return value && typeof value === 'object' ? [value] : [];
              };
              const candidates = [];
              for (const raw of rawValues) {
                for (const entry of decode(raw)) {
                  if (!entry || typeof entry !== 'object') continue;
                  const workspaceId = String(entry.workspaceId ?? entry.workspace_id ?? '');
                  const refreshToken = entry.refreshToken ?? entry.refresh_token;
                  const rawExpiry = entry.refreshExpiresAt ?? entry.refresh_expires_at ?? null;
                  let expiresAt = Number(rawExpiry ?? 0);
                  if (expiresAt > 0 && expiresAt < 1e12) expiresAt *= 1000;
                  if (workspaceId !== expectedWid) continue;
                  if (typeof refreshToken !== 'string' || !refreshToken.trim()) continue;
                  if (Number.isFinite(expiresAt) && expiresAt > 0 && expiresAt <= Date.now()) continue;
                  candidates.push({
                    workspaceId,
                    refreshToken: refreshToken.trim(),
                    refreshExpiresAt: rawExpiry,
                    domain: typeof entry.domain === 'string' ? entry.domain : null,
                    expiresAt: Number.isFinite(expiresAt) ? expiresAt : 0
                  });
                }
              }
              candidates.sort((a, b) => b.expiresAt - a.expiresAt);
              if (!candidates.length) return null;
              const selected = candidates[0];
              return JSON.stringify([{
                workspaceId: selected.workspaceId,
                refreshToken: selected.refreshToken,
                refreshExpiresAt: selected.refreshExpiresAt,
                domain: selected.domain
              }]);
            })()
            """
        }

        private func emitCapture(
            authorization: String,
            deviceID: String,
            user: String?,
            cookieLine: String,
            workspaceList: String?
        ) {
            guard !didCapture else { return }
            didCapture = true
            isEmitting = false
            onStatus(
                cookieLine.isEmpty
                    ? "Captured headers. Saving without cookies."
                    : "Captured Plaud session. Saving locally."
            )
            onCapture(
                PlaudWebAuthCapture(
                    authorization: authorization,
                    xDeviceID: deviceID,
                    xPldUser: user,
                    cookie: cookieLine.isEmpty ? nil : cookieLine,
                    xPldTag: header("x-pld-tag"),
                    baseURL: baseURLString(),
                    appLanguage: header("app-language"),
                    appPlatform: header("app-platform"),
                    editFrom: header("edit-from"),
                    origin: header("origin"),
                    referer: header("referer"),
                    timezone: header("timezone"),
                    workspaceList: workspaceList?.isEmpty == false ? workspaceList : nil
                )
            )
        }

        private func baseURLString() -> String? {
            guard let latestURL, let scheme = latestURL.scheme, let host = latestURL.host else {
                return nil
            }
            if let port = latestURL.port {
                return "\(scheme)://\(host):\(port)"
            }
            return "\(scheme)://\(host)"
        }
    }
}

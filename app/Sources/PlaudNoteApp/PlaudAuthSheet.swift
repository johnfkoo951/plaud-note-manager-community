import AppKit
import SwiftUI

struct PlaudAuthSheet: View {
    @ObservedObject var store: FileStore
    let onDone: () -> Void

    @State private var curlText: String = ""
    @State private var authenticating = false
    @State private var clipboardWatching = false
    @State private var showAdvancedCurl = false
    @State private var showEmbeddedLogin = false
    @State private var webStatus = "Sign in once; automatic renewal will be verified and saved."
    @State private var clearingSession = false
    /// Failure surfaced inline in the sheet. The root ContentView alert is
    /// queued behind this sheet on macOS, so errors must be shown here.
    @State private var importError: String?
    /// A copied cURL can prove and store current access without carrying the
    /// rotating workspace refresh token. Keep that partial success visible so
    /// Community never implies that durable renewal was configured.
    @State private var importNotice: String?
    @State private var accessCredentialSaved = false
    /// Bumped after every failed capture or session clear so the embedded
    /// web view resets its one-shot capture latch and reloads.
    @State private var captureGeneration = 0
    @State private var recovering = false
    @State private var recoverStatus: String?

    private var trimmedCurl: String {
        curlText.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private var curlValidation: PlaudCurlValidation {
        PlaudCurlValidator.inspect(trimmedCurl)
    }

    private var isBusy: Bool {
        authenticating || store.refreshingAuth || recovering || store.refreshingWorkspaceToken
    }

    var body: some View {
        VStack(alignment: .leading, spacing: AppUI.spacingL) {
            header
            browserImportCard
            advancedCurl
            if !DistributionProfile.isCommunity {
                autoRecoverCard
            }
            embeddedLoginFallback
            footer
        }
        .padding(22)
        .frame(width: 820)
        .task(id: clipboardWatching) {
            guard clipboardWatching else { return }
            await watchClipboardForPlaudCurl()
        }
        .onDisappear {
            clipboardWatching = false
        }
    }

    private var header: some View {
        HStack(spacing: 10) {
            Image(systemName: "key.viewfinder")
                .font(.system(size: 22, weight: .semibold))
                .symbolRenderingMode(.hierarchical)
                .foregroundStyle(AppUI.accentPink)
            VStack(alignment: .leading, spacing: 3) {
                Text("Authenticate with Plaud")
                    .font(.title3.weight(.semibold))
                Text("A verified cURL stores current access in macOS Keychain. It preserves durable automatic renewal only when matching renewal information already exists; the optional Web Login below can set that up.")
                    .font(AppUI.metaFont)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer()
        }
    }

    /// Explicit external-browser fallback. Normal automatic recovery uses the
    /// app-owned persistent WebKit session before this sheet is shown.
    private var autoRecoverCard: some View {
        VStack(alignment: .leading, spacing: AppUI.spacingS) {
            HStack(alignment: .center, spacing: AppUI.spacingM) {
                Label("External browser fallback", systemImage: "arrow.triangle.2.circlepath")
                    .font(AppUI.sectionFont)
                Spacer()
                Button {
                    runAutoRecover()
                } label: {
                    HStack(spacing: 6) {
                        if recovering {
                            ProgressView().controlSize(.small)
                        }
                        Text("Recover Now")
                    }
                }
                .disabled(recovering || isBusy)
            }
            Text(
                recoverStatus
                    ?? "Tries an existing Chrome Plaud session. The app-owned session is already retried automatically before this screen appears."
            )
            .font(AppUI.metaFont)
            .foregroundStyle(recoverStatus == nil ? .secondary : Color.primary)
            .fixedSize(horizontal: false, vertical: true)
        }
    }

    private func runAutoRecover() {
        recovering = true
        recoverStatus = "Checking an existing external Plaud browser session…"
        Task {
            let ok = await store.recoverAuthViaBrowser()
            recovering = false
            if ok {
                recoverStatus = "✅ Recovered — automatic renewal re-armed."
            } else {
                recoverStatus = store.lastCommandError
                    ?? "Recovery failed — use Web Login below."
                store.lastCommandError = nil  // keep the error inline, not behind the sheet
            }
        }
    }

    private var browserImportCard: some View {
        VStack(alignment: .leading, spacing: AppUI.spacingM) {
            HStack(alignment: .center, spacing: AppUI.spacingM) {
                Label("Manual cURL fallback", systemImage: "safari")
                    .font(AppUI.sectionFont)
                Spacer()
                Button {
                    startBrowserLogin()
                } label: {
                    Label("Open Plaud", systemImage: "safari")
                }
                Button {
                    importClipboardCurl()
                } label: {
                    HStack(spacing: 6) {
                        if isBusy {
                            ProgressView().controlSize(.small)
                        }
                        Label("Import Copied cURL", systemImage: "doc.on.clipboard")
                    }
                }
                .buttonStyle(.borderedProminent)
                .disabled(isBusy)
            }

            if let importError {
                Label(importError, systemImage: "exclamationmark.triangle.fill")
                    .font(AppUI.metaFont)
                    .foregroundStyle(.red)
                    .fixedSize(horizontal: false, vertical: true)
            }

            if let importNotice {
                Label(importNotice, systemImage: "checkmark.shield.fill")
                    .font(AppUI.metaFont)
                    .foregroundStyle(.orange)
                    .fixedSize(horizontal: false, vertical: true)
            }

            if authenticating {
                Label(
                    "Checking the copied credentials with Plaud before saving to Keychain…",
                    systemImage: "checkmark.shield"
                )
                .font(AppUI.metaFont)
                .foregroundStyle(AppUI.accentPink)
                .fixedSize(horizontal: false, vertical: true)
            }

            Text("A URL alone is not enough. In DevTools > Network, find any authenticated `api-*.plaud.ai` request (for example `weekly_recommend` or `file/simple/web`), right-click it, then Copy > Copy as cURL. The copied text must include authorization and x-device-id headers.")
                .font(AppUI.metaFont)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)

            HStack(spacing: AppUI.spacingS) {
                Image(systemName: clipboardWatching ? "dot.radiowaves.left.and.right" : "doc.on.clipboard")
                    .font(.system(size: 13, weight: .semibold))
                    .symbolRenderingMode(.hierarchical)
                    .foregroundStyle(clipboardWatching ? AppUI.accentPink : .secondary)
                Text(
                    clipboardWatching
                        ? "Watching clipboard for a Plaud cURL..."
                        : "Chrome path: View > Developer > Developer Tools > Network > filter plaud.ai."
                )
                .font(AppUI.metaFont)
                .foregroundStyle(clipboardWatching ? AppUI.accentPink : .secondary)
                Spacer()
                if clipboardWatching {
                    Button("Stop Watching") {
                        clipboardWatching = false
                    }
                    .controlSize(.small)
                }
            }
        }
        .padding(AppUI.spacingL)
        .background(
            LinearGradient(
                colors: [
                    AppUI.brandGreen.opacity(0.10),
                    AppUI.accentPink.opacity(0.08),
                    AppUI.cardFill
                ],
                startPoint: .topLeading,
                endPoint: .bottomTrailing
            ),
            in: RoundedRectangle(cornerRadius: AppUI.radius)
        )
        .overlay(
            RoundedRectangle(cornerRadius: AppUI.radius)
                .stroke(AppUI.cardStroke, lineWidth: 1)
        )
    }

    private var embeddedLoginFallback: some View {
        DisclosureGroup(
            "Plaud Web Login — optional renewal setup",
            isExpanded: $showEmbeddedLogin
        ) {
            VStack(alignment: .leading, spacing: AppUI.spacingS) {
                HStack {
                    Label(webStatus, systemImage: authenticating ? "arrow.triangle.2.circlepath" : "globe")
                        .font(AppUI.metaFont)
                        .foregroundStyle(.secondary)
                    Spacer()
                    Button {
                        clearingSession = true
                        PlaudWebSession.clear {
                            clearingSession = false
                            captureGeneration += 1
                            webStatus = "Plaud Web session cleared. Sign in again."
                        }
                    } label: {
                        HStack(spacing: 6) {
                            if clearingSession {
                                ProgressView().controlSize(.small)
                            }
                            Text("Clear Web Session")
                        }
                    }
                    .disabled(clearingSession || isBusy)
                }

                Text("If Google shows a Bluetooth or passkey error here, use Try another way or the browser import above.")
                    .font(AppUI.metaFont)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)

                webLoginCard
            }
            .padding(.top, AppUI.spacingS)
        }
    }

    private var webLoginCard: some View {
        PlaudWebLoginView(
            onCapture: { capture in
                authenticateWithWebCapture(capture)
            },
            onStatus: { status in
                webStatus = status
            },
            captureGeneration: captureGeneration
        )
        .frame(minHeight: 420)
        .clipShape(RoundedRectangle(cornerRadius: AppUI.radius))
        .overlay(
            RoundedRectangle(cornerRadius: AppUI.radius)
                .stroke(AppUI.cardStroke)
        )
    }

    private var advancedCurl: some View {
        DisclosureGroup("Paste cURL manually", isExpanded: $showAdvancedCurl) {
            VStack(alignment: .leading, spacing: AppUI.spacingS) {
                HStack(spacing: AppUI.spacingS) {
                    Button {
                        pasteClipboardIntoEditor()
                    } label: {
                        Label("Paste Clipboard", systemImage: "doc.on.clipboard")
                    }
                    Spacer()
                    Button {
                        authenticateWithCurl()
                    } label: {
                        HStack(spacing: 6) {
                            if isBusy {
                                ProgressView().controlSize(.small)
                            }
                            Text("Use cURL")
                        }
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(!curlValidation.canImport || isBusy)
                }

                TextEditor(text: $curlText)
                    .font(.system(size: 11.5, design: .monospaced))
                    .frame(height: 110)
                    .padding(6)
                    .background(AppUI.subtleFill, in: RoundedRectangle(cornerRadius: AppUI.radius))
                    .overlay(
                        RoundedRectangle(cornerRadius: AppUI.radius)
                            .stroke(AppUI.cardStroke)
                    )

                if !trimmedCurl.isEmpty {
                    Label(
                        curlValidation.message,
                        systemImage: curlValidation.canImport
                            ? "checkmark.circle.fill"
                            : "exclamationmark.circle.fill"
                    )
                    .font(AppUI.metaFont)
                    .foregroundStyle(curlValidation.canImport ? AppUI.brandGreen : .red)
                    .fixedSize(horizontal: false, vertical: true)
                }
            }
            .padding(.top, AppUI.spacingS)
        }
    }

    private var footer: some View {
        HStack {
            Text("Authentication is stored in macOS Keychain. The project .env contains no Plaud tokens or cookies.")
                .font(AppUI.metaFont)
                .foregroundStyle(.secondary)
            Spacer()
            Button(accessCredentialSaved ? "Done" : "Cancel") { onDone() }
                .keyboardShortcut(.cancelAction)
        }
    }

    private func startBrowserLogin() {
        NSWorkspace.shared.open(URL(string: "https://web.plaud.ai/")!)
        clipboardWatching = true
        webStatus = "Browser opened. Copy a Plaud API request as cURL."
    }

    private func importClipboardCurl() {
        importError = nil
        importNotice = nil
        accessCredentialSaved = false
        let text = NSPasteboard.general.string(forType: .string) ?? ""
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        let validation = PlaudCurlValidator.inspect(trimmed)
        guard validation.canImport else {
            curlText = trimmed
            showAdvancedCurl = true
            importError = validation.message
            return
        }
        curlText = trimmed
        authenticateWithCurl(trimmed)
    }

    private func pasteClipboardIntoEditor() {
        importError = nil
        importNotice = nil
        accessCredentialSaved = false
        let text = NSPasteboard.general.string(forType: .string) ?? ""
        if text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            importError = "클립보드에 Plaud cURL 텍스트가 없습니다."
        } else {
            curlText = text
        }
    }

    @MainActor
    private func watchClipboardForPlaudCurl() async {
        var lastChangeCount = NSPasteboard.general.changeCount
        while clipboardWatching && !Task.isCancelled {
            if NSPasteboard.general.changeCount != lastChangeCount {
                lastChangeCount = NSPasteboard.general.changeCount
                let text = NSPasteboard.general.string(forType: .string) ?? ""
                let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
                if PlaudCurlValidator.inspect(trimmed).canImport {
                    curlText = trimmed
                    webStatus = "Plaud cURL found on clipboard. Importing..."
                    authenticateWithCurl(trimmed)
                    return
                }
            }
            try? await Task.sleep(nanoseconds: 800_000_000)
        }
    }

    private func authenticateWithWebCapture(_ capture: PlaudWebAuthCapture) {
        guard !isBusy else {
            // Dropped capture — re-arm the web view so the next attempt fires.
            captureGeneration += 1
            return
        }
        clipboardWatching = false
        importError = nil
        authenticating = true
        Task {
            let ok = await store.refreshAuthFromWebLogin(capture)
            await MainActor.run {
                authenticating = false
                if ok {
                    onDone()
                } else {
                    let message = store.lastCommandError
                        ?? "Capture received, but Plaud rejected it. Try browser import."
                    store.lastCommandError = nil
                    importError = message
                    webStatus = message
                    captureGeneration += 1
                }
            }
        }
    }

    private func authenticateWithCurl(_ curlOverride: String? = nil) {
        let curl = (curlOverride ?? trimmedCurl).trimmingCharacters(in: .whitespacesAndNewlines)
        let validation = PlaudCurlValidator.inspect(curl)
        guard validation.canImport, !isBusy else {
            importError = validation.message
            showAdvancedCurl = true
            return
        }
        clipboardWatching = false
        importError = nil
        importNotice = nil
        accessCredentialSaved = false
        authenticating = true
        Task {
            let ok = await store.refreshAuthCredentials(curlText: curl)
            await MainActor.run {
                authenticating = false
                if ok {
                    curlText = ""
                    if store.lastCurlImportAutoRefreshArmed == true {
                        onDone()
                    } else {
                        accessCredentialSaved = true
                        let detailSuffix = store.lastCurlImportDetail.map { " (" + $0 + ")" } ?? ""
                        importNotice = "Plaud accepted the cURL and current access is stored in Keychain. Durable automatic renewal is not armed"
                            + detailSuffix
                            + ". You can click Done now, or use Web Login below to set up renewal information."
                    }
                } else {
                    let message = store.lastCommandError
                        ?? "인증 갱신에 실패했습니다. Plaud cURL을 다시 복사해 주세요."
                    store.lastCommandError = nil
                    importError = message
                }
            }
        }
    }
}

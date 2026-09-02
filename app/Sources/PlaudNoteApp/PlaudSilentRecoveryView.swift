import SwiftUI

/// Transient, non-interactive WebKit owner for password-free auth repair.
///
/// It exists only while `authRecoveryPhase == .webSession`. The moment a fresh
/// workspace pair is captured we switch to `.verifying`, which dismantles the
/// WebView before Python writes Keychain. This keeps normal token rotation
/// single-owner and prevents the browser/CLI race that caused recurring -420s.
struct PlaudSilentRecoveryView: View {
    @ObservedObject var store: FileStore

    var body: some View {
        PlaudWebLoginView(
            onCapture: { capture in
                handleCapture(capture)
            },
            onStatus: { status in
                store.authRecoveryStatus = status
                if status == "Plaud account sign-in required." {
                    store.requireInteractiveAuth(status)
                }
            },
            recoveryMode: true,
            captureGeneration: store.authRecoveryRequestID
        )
        // Keep a real drawable surface so WebKit executes the SPA and its
        // credentialed fetches, while remaining invisible/non-interactive.
        .frame(width: 2, height: 2)
        .opacity(0.001)
        .allowsHitTesting(false)
        .accessibilityHidden(true)
        .task(id: store.authRecoveryRequestID) {
            do {
                try await Task.sleep(nanoseconds: 20_000_000_000)
            } catch {
                return  // view was dismantled after capture
            }
            guard store.authRecoveryPhase == .webSession else { return }
            store.requireInteractiveAuth(
                store.authRecoveryStatus
                    ?? "Saved Plaud account session did not respond."
            )
        }
    }

    private func handleCapture(_ capture: PlaudWebAuthCapture) {
        guard store.claimSilentWebCapture() else { return }
        Task {
            let ok = await store.refreshAuthFromWebLogin(capture)
            if ok {
                store.authRecoveryStatus = "Plaud authentication recovered automatically."
                store.selfHealCredentialIssuedAt = nil
                store.authRecoveryPhase = .idle
            } else {
                let detail = store.lastCommandError
                    ?? "Saved Plaud account session could not be verified."
                store.lastCommandError = nil
                store.requireInteractiveAuth(detail)
            }
        }
    }
}

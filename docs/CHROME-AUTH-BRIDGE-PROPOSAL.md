# Chrome authentication bridge proposal

## Decision

Prototype an optional Manifest V3 extension, but do not ship a general-purpose
network recorder or a “copy cURL” extension. The extension should run only after
the user clicks it on `https://web.plaud.ai`, capture the minimum Plaud
authentication bundle needed by the installed app, send it directly to a
registered native messaging host, and then discard it.

This is a follow-up project, not part of Community 0.3.0. The manual cURL import
remains the emergency path until the native bridge passes the acceptance gates
below.

## Intended flow

1. The user signs in on `web.plaud.ai` in Chrome.
2. The user clicks **Connect Plaud Note Manager** in that tab.
3. The extension reads only the current workspace's access headers,
   `workspaceList` refresh entry, API base URL, and workspace identifier.
4. A native messaging host validates the extension origin, payload size,
   Plaud host, workspace match, and credential shape.
5. The app live-checks the access credential, matches the workspace, and first
   stores the complete captured generation atomically in Keychain or DPAPI.
   A later normal refresh rotates and replaces the stored generation atomically;
   the import path must never consume a refresh token before it is durable.
6. The app reports two results separately: **current access connected** and
   **automatic renewal ready**.

No credential should pass through the clipboard, command-line arguments,
browser storage owned by the extension, logs, crash reports, or analytics.

## Minimum permissions

- `activeTab`: temporary access only after an explicit user gesture.
- `scripting`: run the bounded capture function on the active Plaud tab.
- `nativeMessaging`: send the one-time structured payload to the installed app.

Avoid `<all_urls>`, persistent broad host access, `debugger`, `cookies`, and
`webRequest` unless a future prototype proves a strictly necessary capability
that cannot be implemented with the smaller permission set.

Chrome references:

- <https://developer.chrome.com/docs/extensions/develop/concepts/activeTab>
- <https://developer.chrome.com/docs/extensions/develop/concepts/native-messaging>
- <https://developer.chrome.com/docs/extensions/develop/concepts/declare-permissions>
- <https://developer.chrome.com/docs/webstore/program-policies/user-data-faq>

## Security boundaries

- Accept messages only from the exact published extension ID.
- Require the active tab origin to be exactly `https://web.plaud.ai`.
- Allow only HTTPS `api*.plaud.ai` API hosts and reject redirects.
- Cap the message size and reject missing, duplicate, blank, or malformed fields.
- Match the refresh entry to the access token's workspace before storing.
- Keep the previous credential generation when capture, validation, or refresh fails.
- Do not scan Chrome profiles in the Community app. The extension is an explicit,
  user-invoked transfer, not background browser discovery.

## Promise to users

Use: “Connect once; the app renews access while Plaud's refresh chain remains
valid.” Do not promise permanent access. Account logout, password/security
changes, Plaud revocation, or server-side token-policy changes can still require
another user action.

## Acceptance gates

- Fresh-profile install and permission review on macOS and Windows 11.
- Wrong origin, wrong extension ID, lookalike API host, wrong workspace, replay,
  oversized payload, malformed value, and native-host absence tests.
- Live access validation at import plus at least one successful subsequent
  refresh rotation from the durably stored generation.
- Expired access token recovery without reopening DevTools.
- Revoked refresh token fails closed and preserves the last usable generation.
- Keychain and DPAPI inspection confirms no plaintext fallback.
- Extension storage, clipboard, stdout/stderr, crash logs, and analytics remain
  credential-free.
- Chrome Web Store privacy disclosure and minimum-permission review.

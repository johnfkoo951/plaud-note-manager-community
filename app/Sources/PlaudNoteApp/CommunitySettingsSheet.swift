import AppKit
import SwiftUI

struct CommunitySettingsSheet: View {
    @ObservedObject var store: FileStore
    let dismiss: () -> Void

    @State private var config: Database.AppConfig = Database.shared.loadAppConfig()
    @State private var providerKey = ""
    @State private var providerKeyConfigured: Bool?
    @State private var elevenLabsKey = ""
    @State private var elevenLabsKeyConfigured: Bool?
    @State private var secretOperationRunning = false
    @State private var statusMessage: String?

    private let providers = ["claude", "codex", "gemini", "grok"]
    private let providerLabels = [
        "claude": "Anthropic / Claude",
        "codex": "OpenAI / Codex",
        "gemini": "Google / Gemini",
        "grok": "xAI / Grok",
    ]
    private let loginCommands = [
        "claude": "claude auth login",
        "codex": "codex login",
    ]
    private let apiOnlyProviders: Set<String> = ["gemini", "grok"]

    private var selectedProvider: String { config.classifyModel }
    private var selectedBackend: String {
        apiOnlyProviders.contains(selectedProvider)
            ? "api" : (config.backends[selectedProvider] ?? "cli")
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                VStack(alignment: .leading, spacing: 3) {
                    Text("Community Edition")
                        .font(.title2.bold())
                    Text("내 Plaud 폴더 자동 정리와 선택형 ElevenLabs 전사")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Spacer()
            }
            .padding(20)

            Divider()

            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    settingsCard("Included") {
                        Label(
                            "Plaud Web Login; automatic renewal when refresh credentials are captured",
                            systemImage: "key.fill"
                        )
                        Label("Recording sync, local search, playback, folders, tags, and export",
                              systemImage: "waveform")
                        Label("Preview-first routing into folders already in your Plaud account",
                              systemImage: "wand.and.stars")
                    }

                    settingsCard("Automatic folder routing") {
                        settingsRow("AI provider") {
                            Picker("", selection: Binding(
                                get: { config.classifyModel },
                                set: { provider in
                                    config.classifyModel = provider
                                    if apiOnlyProviders.contains(provider) {
                                        config.backends[provider] = "api"
                                    }
                                    providerKey = ""
                                    providerKeyConfigured = nil
                                    Task {
                                        await store.setClassifyModel(provider)
                                        if apiOnlyProviders.contains(provider) {
                                            await store.setBackend(provider, "api")
                                        }
                                        await refreshProviderKeyStatus()
                                    }
                                }
                            )) {
                                ForEach(providers, id: \.self) { provider in
                                    Text(providerLabels[provider] ?? provider).tag(provider)
                                }
                            }
                            .labelsHidden()
                            .frame(width: 240)
                        }

                        settingsRow("Authentication") {
                            Picker("", selection: Binding(
                                get: { selectedBackend },
                                set: { backend in
                                    config.backends[selectedProvider] = backend
                                    Task { await store.setBackend(selectedProvider, backend) }
                                }
                            )) {
                                if !apiOnlyProviders.contains(selectedProvider) {
                                    Text("App OAuth / CLI").tag("cli")
                                }
                                Text("API key").tag("api")
                            }
                            .pickerStyle(.segmented)
                            .labelsHidden()
                            .frame(width: 250)
                        }

                        if selectedBackend == "api" {
                            settingsRow("Model ID") {
                                TextField("Provider model ID", text: modelIDBinding)
                                    .textFieldStyle(.roundedBorder)
                                Button("Save") {
                                    Task {
                                        await store.setModelId(
                                            selectedProvider,
                                            config.models[selectedProvider] ?? ""
                                        )
                                        statusMessage = "Model setting saved."
                                    }
                                }
                            }
                            settingsRow("API key") {
                                SecureField("Stored in Keychain", text: $providerKey)
                                    .textFieldStyle(.roundedBorder)
                                Button("Save") { Task { await saveProviderKey() } }
                                    .disabled(providerKey.isEmpty || secretOperationRunning)
                                Button("Delete", role: .destructive) {
                                    Task { await deleteProviderKey() }
                                }
                                .disabled(providerKeyConfigured != true || secretOperationRunning)
                            }
                            Label(secretStatus(providerKeyConfigured),
                                  systemImage: providerKeyConfigured == true
                                    ? "checkmark.shield.fill" : "key")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        } else {
                            HStack(alignment: .firstTextBaseline, spacing: 8) {
                                Text("먼저 해당 앱/CLI에서 로그인하세요. OAuth 토큰은 이 앱이 저장하지 않습니다.")
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                                Spacer()
                                Button("Copy login command") {
                                    copy(loginCommands[selectedProvider] ?? "")
                                    statusMessage = "Login command copied."
                                }
                            }
                        }

                        if apiOnlyProviders.contains(selectedProvider) {
                            Text("Gemini와 Grok은 Community판에서 API key 방식만 지원합니다. 분류 텍스트를 무도구 private-stdin 경계로 넘기는 안전한 앱 로그인 경로가 확인된 Claude와 Codex만 CLI 로그인을 지원합니다.")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                                .fixedSize(horizontal: false, vertical: true)
                        }

                        Text("분류 미리보기 실행 시 녹음 제목과 로컬에 캐시된 전사/요약이 선택한 AI 제공자에게 전송될 수 있습니다. 폴더 이동은 미리보기에서 선택해 적용하기 전에는 실행되지 않습니다.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }

                    settingsCard("ElevenLabs transcription") {
                        settingsRow("API key") {
                            SecureField("ElevenLabs API key", text: $elevenLabsKey)
                                .textFieldStyle(.roundedBorder)
                            Button("Save") { Task { await saveElevenLabsKey() } }
                                .disabled(elevenLabsKey.isEmpty || secretOperationRunning)
                            Button("Delete", role: .destructive) {
                                Task { await deleteElevenLabsKey() }
                            }
                            .disabled(elevenLabsKeyConfigured != true || secretOperationRunning)
                        }
                        Label(secretStatus(elevenLabsKeyConfigured),
                              systemImage: elevenLabsKeyConfigured == true
                                ? "checkmark.shield.fill" : "key")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        Text("전사 버튼을 누를 때마다 확인을 받은 뒤 오디오를 ElevenLabs로 업로드합니다. 계정의 유료 크레딧이 사용될 수 있으며, 결과는 Community 데이터 폴더에 로컬 저장됩니다. 이전 전송 결과가 불명확하면 중복 청구 경고를 다시 확인하기 전에는 재업로드하지 않습니다.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }

                    settingsCard("Privacy boundary") {
                        Label("Plaud and provider credentials use separate macOS Keychain items",
                              systemImage: "lock.shield")
                        Label("Recordings, metadata, and cache stay in this edition's Application Support folder",
                              systemImage: "externaldrive")
                        Label("No Obsidian vault, shell startup file, or browser profile is scanned",
                              systemImage: "hand.raised.fill")
                    }

                    settingsCard("Local data") {
                        Text(RuntimePaths.appSupportRoot.path)
                            .font(.system(.caption, design: .monospaced))
                            .foregroundStyle(.secondary)
                            .textSelection(.enabled)
                        Button {
                            try? FileManager.default.createDirectory(
                                at: RuntimePaths.appSupportRoot,
                                withIntermediateDirectories: true
                            )
                            NSWorkspace.shared.activateFileViewerSelecting(
                                [RuntimePaths.appSupportRoot]
                            )
                        } label: {
                            Label("Show in Finder", systemImage: "folder")
                        }
                    }

                    if let statusMessage {
                        Text(statusMessage)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }

                    Text("Plaud Note Manager Community uses Plaud's web API and is not an official Plaud application. Only use it with your own account and recordings.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .padding(20)
            }

            Divider()

            HStack {
                Text(AppVersion.longLine)
                    .font(.caption)
                    .foregroundStyle(.tertiary)
                Spacer()
                Button("Done") { dismiss() }
                    .keyboardShortcut(.defaultAction)
            }
            .padding(.horizontal, 20)
            .padding(.vertical, 14)
        }
        .frame(width: 760, height: 700)
        .task {
            // Older config files could retain unsupported CLI backends for
            // Gemini/Grok. Normalize both entries, not just the visible one,
            // so reopening a legacy profile can never launch that route.
            for provider in ["gemini", "grok"] where config.backends[provider] != "api" {
                config.backends[provider] = "api"
                await store.setBackend(provider, "api")
            }
            await refreshProviderKeyStatus()
            elevenLabsKeyConfigured = await store.elevenLabsKeyConfigured()
        }
    }

    private var modelIDBinding: Binding<String> {
        Binding(
            get: { config.models[selectedProvider] ?? "" },
            set: { config.models[selectedProvider] = $0 }
        )
    }

    private func settingsCard<Content: View>(
        _ title: String, @ViewBuilder content: () -> Content
    ) -> some View {
        VStack(alignment: .leading, spacing: 9) {
            Text(title).font(.headline)
            content()
                .font(.callout)
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(AppUI.subtleFill, in: RoundedRectangle(cornerRadius: AppUI.radius))
    }

    private func settingsRow<Content: View>(
        _ label: String, @ViewBuilder content: () -> Content
    ) -> some View {
        HStack(spacing: 10) {
            Text(label)
                .frame(width: 105, alignment: .leading)
                .foregroundStyle(.secondary)
            content()
        }
    }

    private func secretStatus(_ configured: Bool?) -> String {
        switch configured {
        case true: return "A protected key is configured. Its value is never displayed."
        case false: return "No protected key is configured."
        case nil: return "Checking protected key status…"
        }
    }

    private func refreshProviderKeyStatus() async {
        providerKeyConfigured = await store.providerKeyConfigured(selectedProvider)
    }

    private func saveProviderKey() async {
        secretOperationRunning = true
        defer { secretOperationRunning = false }
        let provider = selectedProvider
        if await store.saveProviderKey(provider, value: providerKey) {
            providerKey = ""
            providerKeyConfigured = true
            statusMessage = "\(providerLabels[provider] ?? provider) API key saved securely."
        }
    }

    private func deleteProviderKey() async {
        secretOperationRunning = true
        defer { secretOperationRunning = false }
        let provider = selectedProvider
        if await store.deleteProviderKey(provider) {
            providerKey = ""
            providerKeyConfigured = false
            statusMessage = "\(providerLabels[provider] ?? provider) API key deleted."
        }
    }

    private func saveElevenLabsKey() async {
        secretOperationRunning = true
        defer { secretOperationRunning = false }
        if await store.saveElevenLabsKey(elevenLabsKey) {
            elevenLabsKey = ""
            elevenLabsKeyConfigured = true
            statusMessage = "ElevenLabs API key saved securely."
        }
    }

    private func deleteElevenLabsKey() async {
        secretOperationRunning = true
        defer { secretOperationRunning = false }
        if await store.deleteElevenLabsKey() {
            elevenLabsKey = ""
            elevenLabsKeyConfigured = false
            statusMessage = "ElevenLabs API key deleted."
        }
    }

    private func copy(_ text: String) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
    }
}

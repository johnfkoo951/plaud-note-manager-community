from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_macos_community_surfaces_local_metadata_and_opt_in_integrations() -> None:
    source = (ROOT / "app" / "Sources" / "PlaudNoteApp" / "ContentView.swift").read_text(
        encoding="utf-8"
    )

    header = source.split("private func header(file:", 1)[1].split(
        "// MARK: Inline title editing", 1
    )[0]
    assert "MetadataBar(store: store, file: file)" in header
    assert (
        "if !DistributionProfile.isCommunity {\n"
        "                MetadataBar(store: store, file: file)"
    ) not in header

    metadata_bar = source.split("private struct MetadataBar", 1)[1].split(
        "// MARK: - Plaud Panel", 1
    )[0]
    assert 'TextField("tag"' in metadata_bar
    assert "store.setUsageStatus" in metadata_bar
    assert "store.addTag" in metadata_bar
    assert "store.removeTag" in metadata_bar
    assert metadata_bar.index("if !DistributionProfile.isCommunity") < metadata_bar.index(
        "store.generateMetadata"
    )
    assert "if !DistributionProfile.isCommunity {\n                dualReuseRow" in metadata_bar

    context_menu = source.split(".contextMenu {", 1)[1].split(".swipeActions(edge: .leading", 1)[0]
    private_gate = context_menu.index("if !DistributionProfile.isCommunity")
    assert private_gate < context_menu.index("store.sendToObsidian")
    assert context_menu.index("store.sendToObsidian") < context_menu.index(
        "store.transcribeWithElevenLabs"
    )
    assert 'Button("Transcribe with ElevenLabs…")' in context_menu

    file_store = (ROOT / "app" / "Sources" / "PlaudNoteApp" / "FileStore.swift").read_text(
        encoding="utf-8"
    )
    assert 'DistributionProfile.isCommunity ? "auto-folder" : "classify"' in file_store
    assert '["--limit", "200", "--min-confidence", "0.6"]' in file_store
    assert '["elevenlabs-transcribe", fileID, "--confirm-upload", "--json"]' in file_store


def test_windows_ui_exposes_local_metadata_and_consent_gated_integrations() -> None:
    html = (ROOT / "windows_app" / "static" / "index.html").read_text(encoding="utf-8")
    javascript = (ROOT / "windows_app" / "static" / "app.js").read_text(encoding="utf-8")

    for usage_status in (
        "unused",
        "metadata-ready",
        "vault-linked",
        "used-elsewhere",
        "archived",
    ):
        assert f'value="{usage_status}"' in html
    assert "Plaud Cloud에는 보내지 않고 이 PC에만 저장합니다." in html
    assert 'id="tagForm"' in html
    assert 'id="tagList"' in html

    assert 'api("/api/usage-status"' in javascript
    assert 'api("/api/tag-add"' in javascript
    assert 'api("/api/tag-remove"' in javascript
    assert 'api("/api/folder-preview"' in javascript
    assert 'api("/api/folder-apply"' in javascript
    assert 'api("/api/elevenlabs-transcribe"' in javascript
    assert "confirm_external: useAI" in javascript
    assert "confirm_apply: true" in javascript
    assert "confirm_upload: true" in javascript
    assert 'provider === "gemini" || provider === "grok"' in javascript
    for unavailable_route in (
        "/api/metadata-generate",
        "/api/auto-classify",
        "/api/obsidian",
        "/api/dual",
    ):
        assert unavailable_route not in javascript

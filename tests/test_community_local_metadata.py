from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_macos_community_surfaces_local_metadata_but_gates_private_actions() -> None:
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
    assert private_gate < context_menu.index("store.transcribeWithElevenLabs")


def test_windows_ui_exposes_only_local_status_and_manual_tag_routes() -> None:
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
    for unavailable_route in (
        "/api/metadata-generate",
        "/api/auto-classify",
        "/api/obsidian",
        "/api/dual",
        "/api/folder-move",
    ):
        assert unavailable_route not in javascript

"use strict";

const fragment = new URLSearchParams(window.location.hash.slice(1));
const sessionToken = fragment.get("session") || "";
window.history.replaceState(null, "", window.location.pathname);
const HEARTBEAT_INTERVAL_MS = 15000;
const STATUS_POLL_INTERVAL_MS = 1500;

const $ = (id) => document.getElementById(id);
const state = {
  selectedId: null,
  operation: "idle",
  operationName: "none",
  sessionToken,
  pollTimer: null,
  heartbeatTimer: null,
  usageStatus: "unused",
  tags: [],
  settings: null,
  elevenTranscriptExists: false,
  elevenRetryOutcomeUnknown: false,
  routePlans: [],
  routePlanId: "",
  routePhase: "none",
  undoAvailable: false,
  applyRecoveryRequired: false,
};

function showMessage(text, danger = false) {
  const node = $("message");
  node.textContent = text;
  node.classList.toggle("danger", danger);
  node.hidden = !text;
}

async function api(path, options = {}) {
  if (!state.sessionToken) throw new Error("session");
  const headers = new Headers(options.headers || {});
  headers.set("X-Plaud-Session", state.sessionToken);
  if (options.body) headers.set("Content-Type", "application/json");
  const response = await fetch(path, {
    ...options,
    headers,
    cache: "no-store",
    credentials: "omit",
    redirect: "error",
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.message || "요청을 처리하지 못했습니다.");
  return payload;
}

function setButtonsBusy(busy) {
  for (const id of [
    "syncButton", "backfillButton", "importButton", "disconnectButton", "shutdownButton",
    "autoRouteButton", "applyRouteButton", "undoRouteButton", "saveRoutingSettingsButton",
    "saveRoutingKeyButton", "deleteRoutingKeyButton", "saveElevenLabsKeyButton",
    "deleteElevenLabsKeyButton", "elevenTranscribeButton",
  ]) {
    $(id).disabled = busy;
  }
}

function formatDate(value) {
  if (!value) return "날짜 없음";
  const milliseconds = Number(value) > 100000000000 ? Number(value) : Number(value) * 1000;
  return new Date(milliseconds).toLocaleString("ko-KR", { dateStyle: "medium", timeStyle: "short" });
}

function formatDuration(value) {
  const total = Math.max(0, Math.round(Number(value) || 0));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  return hours ? `${hours}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}` : `${minutes}:${String(seconds).padStart(2, "0")}`;
}

function renderStatus(payload) {
  const labels = { valid: "연결됨", expiring: "곧 만료", expired: "만료됨", unconfigured: "연결 필요", unavailable: "확인 불가", unknown: "연결됨" };
  $("authState").textContent = labels[payload.auth.state] || "확인 필요";
  $("libraryTotal").textContent = String(payload.library.total || 0);
  $("cachedTotal").textContent = String(payload.library.cached || 0);
  const operation = payload.operation || {};
  state.operation = operation.state || "idle";
  state.operationName = operation.name || "none";
  $("operationState").textContent = operation.message || "대기 중";
  $("operationProgress").textContent = operation.total ? `${operation.done || 0} / ${operation.total} · 실패 ${operation.failed || 0}` : "";
  setButtonsBusy(state.operation === "running");
}

function recordingButton(item) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "recording-item" + (item.id === state.selectedId ? " active" : "");
  const title = document.createElement("strong");
  title.textContent = item.title || "제목 없는 녹음";
  const meta = document.createElement("span");
  meta.textContent = `${formatDate(item.edit_time || item.start_time)} · ${formatDuration(item.duration)} · ${item.cached ? "내용 저장됨" : "목록만"}`;
  button.append(title, meta);
  button.addEventListener("click", () => loadRecording(item.id));
  return button;
}

function renderList(items, title) {
  $("listTitle").textContent = title;
  const list = $("recordingList");
  list.replaceChildren();
  if (!items.length) {
    const empty = document.createElement("p");
    empty.className = "empty-list";
    empty.textContent = "표시할 녹음이 없습니다.";
    list.append(empty);
    return;
  }
  for (const item of items) list.append(recordingButton(item));
}

async function loadLibrary() {
  try {
    const payload = await api("/api/library?limit=200&offset=0");
    renderList(payload.items || [], "최근 녹음");
  } catch (error) {
    showMessage(error.message, true);
  }
}

function segmentNode(segment) {
  const node = document.createElement("div");
  node.className = "segment";
  const meta = document.createElement("small");
  const seconds = Math.max(0, Math.floor((Number(segment.start_time) || 0) / 1000));
  meta.textContent = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}${segment.speaker ? ` · ${segment.speaker}` : ""}`;
  const text = document.createElement("div");
  text.textContent = segment.content || "";
  node.append(meta, text);
  return node;
}

function renderLocalMetadata(metadata = {}) {
  const allowed = new Set(Array.isArray(metadata.usage_statuses) ? metadata.usage_statuses : []);
  const usageStatus = allowed.has(metadata.usage_status) ? metadata.usage_status : "unused";
  state.usageStatus = usageStatus;
  state.tags = Array.isArray(metadata.tags) ? metadata.tags.filter((tag) => typeof tag === "string") : [];
  $("usageStatus").value = usageStatus;

  const list = $("tagList");
  list.replaceChildren();
  if (!state.tags.length) {
    const empty = document.createElement("span");
    empty.className = "muted tag-empty";
    empty.textContent = "아직 로컬 태그가 없습니다.";
    list.append(empty);
    return;
  }
  for (const tag of state.tags) {
    const chip = document.createElement("span");
    chip.className = "tag-chip";
    const label = document.createElement("span");
    label.textContent = `#${tag}`;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "tag-remove";
    remove.textContent = "×";
    remove.setAttribute("aria-label", `#${tag} 태그 제거`);
    remove.addEventListener("click", () => removeTag(tag, remove));
    chip.append(label, remove);
    list.append(chip);
  }
}

function renderElevenLabsTranscript(transcript) {
  state.elevenTranscriptExists = Boolean(transcript);
  const section = $("elevenTranscriptSection");
  const list = $("elevenTranscriptText");
  list.replaceChildren();
  section.hidden = !transcript;
  if (!transcript) return;
  const segments = Array.isArray(transcript.segments) ? transcript.segments : [];
  if (segments.length) {
    for (const segment of segments) {
      list.append(segmentNode({
        start_time: Number(segment.start_ms || 0),
        speaker: segment.speaker,
        content: segment.content,
      }));
    }
  } else {
    const text = document.createElement("pre");
    text.textContent = transcript.text || "저장된 전사가 비어 있습니다.";
    list.append(text);
  }
}

async function loadRecording(fileId) {
  try {
    const payload = await api(`/api/recording?id=${encodeURIComponent(fileId)}`);
    state.selectedId = payload.id;
    $("emptyDetail").hidden = true;
    $("recordingDetail").hidden = false;
    $("detailTitle").textContent = payload.title || "제목 없는 녹음";
    $("detailMeta").textContent = `${formatDate(payload.edit_time || payload.start_time)} · ${formatDuration(payload.duration)}`;
    renderLocalMetadata(payload.local_metadata);
    state.elevenRetryOutcomeUnknown = payload.elevenlabs_retry_outcome_unknown === true;
    renderElevenLabsTranscript(payload.elevenlabs_transcript);
    const content = payload.content;
    $("summaryText").textContent = content?.summary || (content ? "요약 없음" : "백필하지 않은 녹음입니다.");
    const transcript = $("transcriptText");
    transcript.replaceChildren();
    if (content?.transcript?.length) {
      for (const segment of content.transcript) transcript.append(segmentNode(segment));
    } else {
      const empty = document.createElement("p");
      empty.className = "muted";
      empty.textContent = content ? "전사 없음" : "백필 후 전사를 볼 수 있습니다.";
      transcript.append(empty);
    }
    await loadLibrary();
  } catch (error) {
    showMessage(error.message, true);
  }
}

async function removeTag(tag, button) {
  if (!state.selectedId) return;
  button.disabled = true;
  try {
    const payload = await api("/api/tag-remove", {
      method: "POST",
      body: JSON.stringify({ file_id: state.selectedId, tag }),
    });
    renderLocalMetadata(payload);
    showMessage("로컬 태그를 제거했습니다.");
  } catch (error) {
    button.disabled = false;
    showMessage(error.message, true);
  }
}

async function refreshStatus() {
  try {
    const payload = await api("/api/status");
    const previous = state.operation;
    const previousName = state.operationName;
    renderStatus(payload);
    if (previous === "running" && state.operation !== "running") {
      if (state.operation === "failed") showMessage(payload.operation?.message || "작업에 실패했습니다.", true);
      if (state.operation === "succeeded" && previousName === "folder-preview") {
        await loadRoutePlans();
        showMessage(payload.operation?.message || "폴더 분류 미리보기를 만들었습니다.");
      } else if (state.operation === "succeeded" && previousName === "folder-apply") {
        await loadRoutePlans();
        await loadLibrary();
        showMessage(payload.operation?.message || "선택한 폴더 이동을 적용했습니다.");
      } else if (state.operation === "succeeded" && previousName === "folder-undo") {
        await loadRoutePlans();
        await loadLibrary();
        showMessage(payload.operation?.message || "최근 폴더 이동을 되돌렸습니다.");
      }
      if (state.selectedId) await loadRecording(state.selectedId);
      else await loadLibrary();
    }
  } catch (error) {
    showMessage(error.message, true);
  }
}

function providerSetting(provider) {
  return state.settings?.routing?.providers?.find((item) => item.provider === provider) || null;
}

function renderProviderSettings(provider) {
  const item = providerSetting(provider);
  if (!item) return;
  const apiOnly = provider === "gemini" || provider === "grok";
  const cliOption = $("routingBackend").querySelector('option[value="cli"]');
  cliOption.disabled = apiOnly;
  $("routingProvider").value = provider;
  $("routingBackend").value = apiOnly ? "api" : (item.backend || "cli");
  $("routingModel").value = item.model_id || "";
  const apiMode = $("routingBackend").value === "api";
  $("routingKeyControls").hidden = !apiMode;
  $("oauthHelp").hidden = apiMode && !apiOnly;
  $("routingKeyStatus").textContent = item.api_key_set
    ? "Windows 보안 저장소에 key가 설정되어 있습니다. 값은 표시하지 않습니다."
    : "설정된 API key가 없습니다.";
  const commands = {
    claude: "claude auth login",
    codex: "codex login",
  };
  $("oauthHelp").textContent = apiOnly
    ? "Gemini와 Grok은 분류 텍스트를 무도구 private-stdin 경계로 넘기는 안전한 앱 로그인 경로가 확인되지 않아 이 앱에서는 API key 방식만 지원합니다."
    : `먼저 공급자 CLI에서 로그인하세요 (${commands[provider]}). 이 앱은 OAuth 토큰을 읽거나 복사하거나 저장하지 않습니다.`;
}

function renderRoutePlans(payload) {
  const items = Array.isArray(payload?.items) ? payload.items : [];
  state.routePlans = items;
  state.routePlanId = typeof payload?.plan_id === "string" ? payload.plan_id : "";
  state.routePhase = payload?.phase || "none";
  state.undoAvailable = payload?.undo_available === true;
  state.applyRecoveryRequired = payload?.apply_recovery_required === true;
  $("undoRouteButton").hidden = !state.undoAvailable && !state.applyRecoveryRequired;
  $("undoRouteButton").textContent = state.applyRecoveryRequired
    ? "중단된 폴더 적용 복구"
    : "최근 폴더 이동 되돌리기";
  const panel = $("routePreview");
  const list = $("routePlanList");
  list.replaceChildren();
  panel.hidden = !items.length && !["preview", "applied", "error"].includes(state.routePhase);

  if (!items.length) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = "이동을 제안할 미분류 녹음이 없습니다.";
    list.append(empty);
    $("applyRouteButton").hidden = true;
    return;
  }

  let actionable = 0;
  for (const item of items) {
    const eligible = state.routePhase === "preview"
      && Boolean(item.folder_id)
      && Number(item.confidence || 0) >= 0.6;
    if (eligible) actionable += 1;
    const row = document.createElement("label");
    row.className = "route-plan";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.value = String(item.file_id || "");
    checkbox.checked = eligible;
    checkbox.disabled = !eligible;

    const copy = document.createElement("span");
    const title = document.createElement("strong");
    title.textContent = item.title || "제목 없는 녹음";
    const detail = document.createElement("small");
    const source = item.source === "llm" ? "AI" : "로컬";
    const target = item.folder_name || "일치 폴더 없음";
    const error = item.error ? ` · ${item.error}` : "";
    detail.textContent = `${target} · ${source} · ${Math.round(Number(item.confidence || 0) * 100)}% · ${item.reason || "근거 없음"}${error}`;
    copy.append(title, document.createElement("br"), detail);

    const status = document.createElement("small");
    status.textContent = item.applied ? "적용됨" : (eligible ? "선택됨" : "검토 제외");
    row.append(checkbox, copy, status);
    list.append(row);
  }
  $("applyRouteButton").hidden = state.routePhase !== "preview" || actionable === 0;
}

async function loadRoutePlans() {
  try {
    renderRoutePlans(await api("/api/folder-preview"));
  } catch (error) {
    showMessage(error.message, true);
  }
}

function renderSettings(payload) {
  state.settings = payload;
  const provider = payload.routing?.selected_provider || "claude";
  renderProviderSettings(provider);
  $("elevenLabsKeyStatus").textContent = payload.elevenlabs?.api_key_set
    ? "Windows 보안 저장소에 ElevenLabs key가 설정되어 있습니다."
    : "설정된 ElevenLabs API key가 없습니다.";
}

async function loadSettings() {
  try {
    renderSettings(await api("/api/settings"));
  } catch (error) {
    showMessage(error.message, true);
  }
}

async function renewBrowserLease() {
  try {
    await api("/api/heartbeat", { method: "POST", body: "{}" });
  } catch (error) {
    stopVisibleSession();
    showMessage("앱 서버 연결이 끊겼습니다. 앱을 다시 실행하세요.", true);
  }
}

function stopVisibleSession() {
  if (state.pollTimer !== null) window.clearInterval(state.pollTimer);
  if (state.heartbeatTimer !== null) window.clearInterval(state.heartbeatTimer);
  state.pollTimer = null;
  state.heartbeatTimer = null;
}

function startVisibleSession() {
  if (document.visibilityState !== "visible" || !state.sessionToken) return;
  if (state.heartbeatTimer === null) {
    void renewBrowserLease();
    state.heartbeatTimer = window.setInterval(renewBrowserLease, HEARTBEAT_INTERVAL_MS);
  }
  if (state.pollTimer === null) {
    void refreshStatus();
    state.pollTimer = window.setInterval(refreshStatus, STATUS_POLL_INTERVAL_MS);
  }
}

async function startJob(path, confirmation) {
  if (confirmation && !window.confirm(confirmation)) return;
  try {
    showMessage("");
    await api(path, { method: "POST", body: "{}" });
    await refreshStatus();
  } catch (error) {
    showMessage(error.message, true);
  }
}

$("searchForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const query = $("searchInput").value.trim();
  if (!query) return;
  try {
    const payload = await api(`/api/search?q=${encodeURIComponent(query)}&limit=100`);
    renderList(payload.items || [], `검색 결과 · ${payload.items?.length || 0}`);
  } catch (error) {
    showMessage(error.message, true);
  }
});

$("clearSearchButton").addEventListener("click", () => {
  $("searchInput").value = "";
  loadLibrary();
});
$("refreshButton").addEventListener("click", loadLibrary);
$("syncButton").addEventListener("click", () => startJob("/api/sync"));
$("backfillButton").addEventListener("click", () => startJob("/api/backfill", "아직 저장하지 않은 전사와 요약을 이 PC로 내려받습니다. 본인 PC에서 계속할까요?"));

$("autoRouteButton").addEventListener("click", async () => {
  const useAI = $("routeMode").value === "ai";
  const provider = $("routingProvider").selectedOptions[0]?.textContent || $("routingProvider").value;
  const backend = $("routingBackend").value === "api" ? "API key" : "로그인된 CLI";
  if (useAI && !window.confirm(
    `${provider} (${backend})에 기존 Plaud 폴더 이름과 약한 매칭의 캐시된 녹음 제목·키워드·요약·전사를 보낼 수 있으며 제공자 비용이 발생할 수 있습니다. 아직 폴더는 이동하지 않습니다. 계속할까요?`
  )) return;
  try {
    $("routePreview").hidden = true;
    state.routePlans = [];
    state.routePlanId = "";
    await api("/api/folder-preview", {
      method: "POST",
      body: JSON.stringify({ use_ai: useAI, confirm_external: useAI }),
    });
    await refreshStatus();
    showMessage(useAI ? "선택한 AI를 포함한 폴더 미리보기를 시작했습니다." : "외부 전송 없이 로컬 폴더 미리보기를 시작했습니다.");
  } catch (error) {
    showMessage(error.message, true);
  }
});

$("applyRouteButton").addEventListener("click", async () => {
  const fileIds = Array.from($("routePlanList").querySelectorAll("input[type=checkbox]:checked"))
    .map((input) => input.value);
  if (!fileIds.length) return showMessage("적용할 이동안을 하나 이상 선택하세요.", true);
  if (!/^[0-9a-f]{32}$/.test(state.routePlanId)) {
    return showMessage("미리보기 식별값이 없습니다. 미리보기를 다시 실행하세요.", true);
  }
  if (!window.confirm(`선택한 ${fileIds.length}개 녹음의 폴더를 Plaud Cloud에서 변경할까요? 미리보기에 표시된 정확한 폴더로만 이동합니다.`)) return;
  try {
    await api("/api/folder-apply", {
      method: "POST",
      body: JSON.stringify({ file_ids: fileIds, plan_id: state.routePlanId, confirm_apply: true }),
    });
    await refreshStatus();
    showMessage("선택한 Plaud Cloud 폴더 이동을 시작했습니다.");
  } catch (error) {
    showMessage(error.message, true);
  }
});

$("undoRouteButton").addEventListener("click", async () => {
  if (!state.undoAvailable && !state.applyRecoveryRequired) return;
  const recovering = state.applyRecoveryRequired;
  const confirmation = recovering
    ? "중단된 폴더 적용의 원격 상태를 확인하고 같은 대상 상태로 안정화할까요? 이 단계에서는 이전 폴더로 되돌리지 않습니다. 완료 뒤 상태를 확인하고 되돌리기를 다시 눌러야 합니다."
    : "최근 자동 폴더 이동을 적용 전 상태로 되돌릴까요? 적용 뒤 Plaud 웹·모바일 또는 이 PC에서 폴더가 달라졌다면 안전을 위해 아무 항목도 변경하지 않습니다.";
  if (!window.confirm(confirmation)) return;
  try {
    await api("/api/folder-undo", {
      method: "POST",
      body: JSON.stringify({ confirm_undo: true }),
    });
    await refreshStatus();
    showMessage(recovering
      ? "중단된 폴더 적용의 안전 복구를 시작했습니다. 완료 뒤 되돌리기를 다시 확인하세요."
      : "최근 폴더 이동의 안전 검증과 되돌리기를 시작했습니다.");
  } catch (error) {
    showMessage(error.message, true);
  }
});

$("cancelRouteButton").addEventListener("click", () => {
  $("routePreview").hidden = true;
});

$("routingProvider").addEventListener("change", () => {
  $("routingApiKey").value = "";
  renderProviderSettings($("routingProvider").value);
});

$("routingBackend").addEventListener("change", () => {
  const provider = $("routingProvider").value;
  const apiOnly = provider === "gemini" || provider === "grok";
  if (apiOnly) $("routingBackend").value = "api";
  const apiMode = $("routingBackend").value === "api";
  $("routingKeyControls").hidden = !apiMode;
  $("oauthHelp").hidden = apiMode && !apiOnly;
});

$("saveRoutingSettingsButton").addEventListener("click", async () => {
  try {
    const payload = await api("/api/settings-routing", {
      method: "POST",
      body: JSON.stringify({
        provider: $("routingProvider").value,
        backend: $("routingBackend").value,
        model_id: $("routingModel").value.trim(),
      }),
    });
    renderSettings(payload);
    showMessage("자동 폴더 분류 설정을 저장했습니다.");
  } catch (error) {
    showMessage(error.message, true);
  }
});

$("saveRoutingKeyButton").addEventListener("click", async () => {
  const apiKey = $("routingApiKey").value;
  if (!apiKey) return showMessage("API key를 입력하세요.", true);
  try {
    await api("/api/provider-key-set", {
      method: "POST",
      body: JSON.stringify({ provider: $("routingProvider").value, api_key: apiKey }),
    });
    $("routingApiKey").value = "";
    await loadSettings();
    showMessage("API key를 Windows 보안 저장소에 저장했습니다.");
  } catch (error) {
    $("routingApiKey").value = "";
    showMessage(error.message, true);
  }
});

$("deleteRoutingKeyButton").addEventListener("click", async () => {
  if (!window.confirm("선택한 AI 제공자의 API key를 이 PC에서 삭제할까요?")) return;
  try {
    await api("/api/provider-key-delete", {
      method: "POST",
      body: JSON.stringify({ provider: $("routingProvider").value }),
    });
    $("routingApiKey").value = "";
    await loadSettings();
    showMessage("선택한 AI 제공자의 API key를 삭제했습니다.");
  } catch (error) {
    showMessage(error.message, true);
  }
});

$("saveElevenLabsKeyButton").addEventListener("click", async () => {
  const apiKey = $("elevenLabsApiKey").value;
  if (!apiKey) return showMessage("ElevenLabs API key를 입력하세요.", true);
  try {
    await api("/api/provider-key-set", {
      method: "POST",
      body: JSON.stringify({ provider: "elevenlabs", api_key: apiKey }),
    });
    $("elevenLabsApiKey").value = "";
    await loadSettings();
    showMessage("ElevenLabs API key를 Windows 보안 저장소에 저장했습니다.");
  } catch (error) {
    $("elevenLabsApiKey").value = "";
    showMessage(error.message, true);
  }
});

$("deleteElevenLabsKeyButton").addEventListener("click", async () => {
  if (!window.confirm("ElevenLabs API key를 이 PC에서 삭제할까요?")) return;
  try {
    await api("/api/provider-key-delete", {
      method: "POST",
      body: JSON.stringify({ provider: "elevenlabs" }),
    });
    $("elevenLabsApiKey").value = "";
    await loadSettings();
    showMessage("ElevenLabs API key를 삭제했습니다.");
  } catch (error) {
    showMessage(error.message, true);
  }
});

$("elevenTranscribeButton").addEventListener("click", async () => {
  if (!state.selectedId) return;
  const again = state.elevenTranscriptExists;
  const uncertain = state.elevenRetryOutcomeUnknown;
  const detail = uncertain
    ? "이전 ElevenLabs 업로드가 공급자에게 접수됐는지 확인하지 못했습니다. 다시 업로드하면 같은 오디오 비용이 중복 청구될 수 있습니다. 그래도 다시 시도할까요?"
    : again
    ? "이미 로컬 전사가 있습니다. 같은 오디오를 다시 업로드하고 크레딧을 다시 사용할까요?"
    : "이 녹음의 오디오를 ElevenLabs에 업로드합니다. 유료 크레딧이 사용될 수 있습니다. 계속할까요?";
  if (!window.confirm(detail)) return;
  try {
    await api("/api/elevenlabs-transcribe", {
      method: "POST",
      body: JSON.stringify({
        file_id: state.selectedId,
        confirm_upload: true,
        force: again || uncertain,
        language: $("elevenLanguage").value.trim(),
        num_speakers: Number($("elevenSpeakers").value),
      }),
    });
    await refreshStatus();
    showMessage("ElevenLabs 전사를 시작했습니다. 탭을 닫아도 작업 완료 후 서버가 안전하게 종료됩니다.");
  } catch (error) {
    showMessage(error.message, true);
  }
});

$("usageStatus").addEventListener("change", async () => {
  if (!state.selectedId) return;
  const previous = state.usageStatus;
  $("usageStatus").disabled = true;
  try {
    const payload = await api("/api/usage-status", {
      method: "POST",
      body: JSON.stringify({
        file_id: state.selectedId,
        usage_status: $("usageStatus").value,
      }),
    });
    renderLocalMetadata(payload);
    showMessage("사용 상태를 이 PC에 저장했습니다.");
  } catch (error) {
    $("usageStatus").value = previous;
    showMessage(error.message, true);
  } finally {
    $("usageStatus").disabled = false;
  }
});

$("tagForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.selectedId) return;
  const tag = $("tagInput").value.trim();
  if (!tag) return showMessage("태그를 하나 입력하세요.", true);
  $("tagInput").disabled = true;
  $("tagAddButton").disabled = true;
  try {
    const payload = await api("/api/tag-add", {
      method: "POST",
      body: JSON.stringify({ file_id: state.selectedId, tag }),
    });
    $("tagInput").value = "";
    renderLocalMetadata(payload);
    showMessage("로컬 태그를 저장했습니다.");
  } catch (error) {
    showMessage(error.message, true);
  } finally {
    $("tagInput").disabled = false;
    $("tagAddButton").disabled = false;
    $("tagInput").focus();
  }
});

$("importButton").addEventListener("click", async () => {
  const curl = $("curlInput").value;
  if (!curl.trim()) return showMessage("Plaud cURL을 입력하세요.", true);
  try {
    const payload = await api("/api/import-curl", { method: "POST", body: JSON.stringify({ curl }) });
    $("curlInput").value = "";
    if (payload.verification === "unreachable") {
      showMessage("연결 정보는 저장했지만 네트워크 문제로 확인하지 못했습니다. 인터넷 연결 후 목록 동기화를 눌러 확인하세요.", true);
    } else {
      showMessage("Plaud가 확인한 연결 정보를 Windows 보안 저장소에 저장했습니다.");
    }
    await refreshStatus();
  } catch (error) {
    showMessage(error.message, true);
  }
});

$("disconnectButton").addEventListener("click", async () => {
  if (!window.confirm("로컬 녹음과 전사는 유지하고 연결 정보만 삭제할까요?")) return;
  try {
    await api("/api/disconnect", { method: "POST", body: "{}" });
    showMessage("연결 정보를 삭제했습니다. 로컬 기록은 유지됩니다.");
    await refreshStatus();
  } catch (error) {
    showMessage(error.message, true);
  }
});

$("exportButton").addEventListener("click", async () => {
  if (!state.selectedId) return;
  try {
    const payload = await api("/api/export", {
      method: "POST",
      body: JSON.stringify({ file_id: state.selectedId, kind: $("exportKind").value }),
    });
    showMessage(`Markdown을 저장했습니다: ${payload.directory}\\${payload.file}`);
  } catch (error) {
    showMessage(error.message, true);
  }
});

$("shutdownButton").addEventListener("click", async () => {
  try {
    await api("/api/shutdown", { method: "POST", body: "{}" });
    stopVisibleSession();
    document.body.replaceChildren(Object.assign(document.createElement("p"), { className: "empty-state", textContent: "앱을 종료했습니다. 이 창을 닫아도 됩니다." }));
  } catch (error) {
    showMessage(error.message, true);
  }
});

if (!state.sessionToken) {
  $("sessionWarning").hidden = false;
  document.querySelectorAll("button, input, textarea, select").forEach((element) => { element.disabled = true; });
} else {
  void Promise.all([loadLibrary(), loadSettings(), loadRoutePlans()]);
  startVisibleSession();
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") startVisibleSession();
    else stopVisibleSession();
  });
  window.addEventListener("pagehide", stopVisibleSession);
}

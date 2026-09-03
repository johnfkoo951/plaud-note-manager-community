"use strict";

const fragment = new URLSearchParams(window.location.hash.slice(1));
const sessionToken = fragment.get("session") || "";
window.history.replaceState(null, "", window.location.pathname);

const $ = (id) => document.getElementById(id);
const state = { selectedId: null, operation: "idle", sessionToken, pollTimer: null };

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
  for (const id of ["syncButton", "backfillButton", "importButton", "disconnectButton", "shutdownButton"]) {
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

async function loadRecording(fileId) {
  try {
    const payload = await api(`/api/recording?id=${encodeURIComponent(fileId)}`);
    state.selectedId = payload.id;
    $("emptyDetail").hidden = true;
    $("recordingDetail").hidden = false;
    $("detailTitle").textContent = payload.title || "제목 없는 녹음";
    $("detailMeta").textContent = `${formatDate(payload.edit_time || payload.start_time)} · ${formatDuration(payload.duration)}`;
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

async function refreshStatus() {
  try {
    const payload = await api("/api/status");
    const previous = state.operation;
    renderStatus(payload);
    if (previous === "running" && state.operation === "succeeded") await loadLibrary();
  } catch (error) {
    showMessage(error.message, true);
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
    if (state.pollTimer !== null) window.clearInterval(state.pollTimer);
    document.body.replaceChildren(Object.assign(document.createElement("p"), { className: "empty-state", textContent: "앱을 종료했습니다. 이 창을 닫아도 됩니다." }));
  } catch (error) {
    showMessage(error.message, true);
  }
});

if (!state.sessionToken) {
  $("sessionWarning").hidden = false;
  document.querySelectorAll("button, input, textarea, select").forEach((element) => { element.disabled = true; });
} else {
  Promise.all([refreshStatus(), loadLibrary()]);
  state.pollTimer = window.setInterval(refreshStatus, 1500);
}

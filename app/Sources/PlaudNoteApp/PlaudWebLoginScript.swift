import Foundation

let plaudAuthCaptureScript = """
(() => {
  if (window.__plaudAuthCaptureInstalled) { return; }
  window.__plaudAuthCaptureInstalled = true;
  const isPlaudAPI = (rawURL) => {
    try {
      const host = new URL(String(rawURL || ""), window.location.href).hostname.toLowerCase();
      return host.startsWith("api") && host.endsWith(".plaud.ai");
    } catch (_) {
      return false;
    }
  };
  const post = (url, headers) => {
    try {
      const rawURL = String(url || "");
      if (!isPlaudAPI(rawURL)) { return; }
      window.webkit.messageHandlers.plaudAuthCapture.postMessage({
        url: rawURL,
        headers: headers || {}
      });
    } catch (_) {}
  };
  const headersObject = (headers) => {
    const out = {};
    if (!headers) { return out; }
    try {
      if (headers instanceof Headers) {
        headers.forEach((value, key) => { out[String(key).toLowerCase()] = String(value); });
        return out;
      }
      if (Array.isArray(headers)) {
        headers.forEach((pair) => {
          if (pair && pair.length >= 2) { out[String(pair[0]).toLowerCase()] = String(pair[1]); }
        });
        return out;
      }
      Object.keys(headers).forEach((key) => { out[String(key).toLowerCase()] = String(headers[key]); });
    } catch (_) {}
    return out;
  };
  const originalFetch = window.fetch;
  window.fetch = function(input, init) {
    const inputHeaders = input && input.headers ? headersObject(input.headers) : {};
    const initHeaders = init && init.headers ? headersObject(init.headers) : {};
    const url = typeof input === "string" ? input : input && input.url;
    post(url, Object.assign({}, inputHeaders, initHeaders));
    return originalFetch.apply(this, arguments);
  };
  const originalOpen = XMLHttpRequest.prototype.open;
  const originalSetRequestHeader = XMLHttpRequest.prototype.setRequestHeader;
  const originalSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function(method, url) {
    this.__plaudAuthURL = url;
    this.__plaudAuthHeaders = {};
    return originalOpen.apply(this, arguments);
  };
  XMLHttpRequest.prototype.setRequestHeader = function(key, value) {
    this.__plaudAuthHeaders[String(key).toLowerCase()] = String(value);
    return originalSetRequestHeader.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function() {
    post(this.__plaudAuthURL, this.__plaudAuthHeaders);
    return originalSend.apply(this, arguments);
  };
})();
"""

/// Runs only for the transient, hidden recovery WebView.
///
/// This mirrors Plaud Web's own account-session fallback: a rejected rotating
/// workspace refresh token is replaced through the still-live HttpOnly account
/// session.  The account cookies never cross the WebKit boundary; only the new
/// workspace access/refresh pair is posted to native code and mirrored into the
/// exact namespaced localStorage entry.
let plaudAccountSessionRecoveryScript = """
(async () => {
  const bridge = window.webkit && window.webkit.messageHandlers
    && window.webkit.messageHandlers.plaudAuthCapture;
  const status = (state, detail) => {
    try { bridge.postMessage({ kind: "recoveryStatus", status: state, detail: detail || "" }); }
    catch (_) {}
  };
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const parseStored = (raw) => {
    if (raw === null || raw === undefined) return null;
    let value = raw;
    for (let i = 0; i < 3 && typeof value === "string"; i += 1) {
      const text = value.trim();
      if (!text) return "";
      try { value = JSON.parse(text); } catch (_) { break; }
    }
    return value;
  };
  const scalar = (value) => {
    if (typeof value === "string") return value.trim();
    if (!value || typeof value !== "object") return "";
    const candidate = value.value ?? value.data ?? value.id
      ?? value.deviceId ?? value.device_id ?? value.userId ?? value.user_id;
    return typeof candidate === "string" ? candidate.trim() : "";
  };
  const normalizeDomain = (raw) => {
    const value = String(raw || "https://api.plaud.ai").trim().replace(/\\/$/, "");
    const candidate = /^https?:\\/\\//i.test(value) ? value : `https://${value}`;
    const parsed = new URL(candidate);
    const host = parsed.hostname.toLowerCase();
    const trusted = host === "api.plaud.ai"
      || (host.startsWith("api") && host.endsWith(".plaud.ai"));
    if (parsed.protocol !== "https:" || !trusted || parsed.port
        || parsed.username || parsed.password
        || (parsed.pathname && parsed.pathname !== "/")
        || parsed.search || parsed.hash) {
      throw new Error("untrusted_api_domain");
    }
    return `https://${host}`;
  };
  const readBody = async (response) => {
    try { return await response.json(); } catch (_) { return {}; }
  };
  const businessOK = (response, body) => response.ok
    && (body.status === 0 || body.status === "0" || body.status === undefined);

  try {
    status("starting", "Refreshing the saved Plaud account session…");
    const userId = scalar(parseStored(localStorage.getItem("pld_userId")));
    if (!userId) throw new Error("account_session_missing");

    const listKey = `pld_${userId}:workspaceList`;
    const rawList = parseStored(localStorage.getItem(listKey));
    const workspaceList = Array.isArray(rawList)
      ? rawList
      : (rawList && typeof rawList === "object" ? [rawList] : []);
    let workspaceId = scalar(
      parseStored(localStorage.getItem(`pld_${userId}:currentWorkspaceId`))
    );
    if (!workspaceId && workspaceList.length === 1) {
      workspaceId = String(
        workspaceList[0].workspaceId ?? workspaceList[0].workspace_id ?? ""
      );
    }
    if (!workspaceId) throw new Error("workspace_missing");

    let index = workspaceList.findIndex((entry) => String(
      entry && (entry.workspaceId ?? entry.workspace_id ?? "")
    ) === workspaceId);
    if (index < 0) throw new Error("workspace_missing");
    const current = workspaceList[index] || {};
    const deviceId = scalar(parseStored(localStorage.getItem("pld_USER_TAG")));
    if (!deviceId) throw new Error("device_id_missing");

    let domain = normalizeDomain(
      current.domain ?? current.apiDomain ?? current.api_domain
      ?? scalar(parseStored(localStorage.getItem("pld_plaud_user_api_domain")))
    );
    const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || "Asia/Seoul";
    const commonHeaders = {
      "accept": "application/json, text/plain, */*",
      "content-type": "application/json",
      "app-language": "en",
      "app-platform": "web",
      "edit-from": "web",
      "timezone": timezone,
      "x-device-id": deviceId
    };

    const requestWorkspacePair = async () => {
      const url = `${domain}/user-app/auth/workspace/token/${encodeURIComponent(workspaceId)}`;
      const response = await fetch(url, {
        method: "POST",
        headers: commonHeaders,
        credentials: "include",
        body: "{}"
      });
      const body = await readBody(response);
      return { response, body, url };
    };

    const refreshAccount = async () => {
      for (let attempt = 0; attempt < 3; attempt += 1) {
        const response = await fetch("https://api.plaud.ai/auth/refresh-user-token", {
          method: "POST",
          headers: commonHeaders,
          credentials: "include",
          body: "{}"
        });
        const body = await readBody(response);
        if (businessOK(response, body)) return true;
        const code = Number(body.status ?? 0);
        if (response.status !== 409 && code !== -4302) return false;
        await sleep(1000);
      }
      return false;
    };

    let result = await requestWorkspacePair();
    if (Number(result.body.status) === -302) {
      const moved = result.body.data && result.body.data.domains
        && result.body.data.domains.api;
      if (moved) {
        domain = normalizeDomain(moved);
        result = await requestWorkspacePair();
      }
    }
    if (!businessOK(result.response, result.body)) {
      const refreshed = await refreshAccount();
      if (!refreshed) throw new Error("account_session_expired");
      result = await requestWorkspacePair();
      if (Number(result.body.status) === -302) {
        const moved = result.body.data && result.body.data.domains
          && result.body.data.domains.api;
        if (moved) {
          domain = normalizeDomain(moved);
          result = await requestWorkspacePair();
        }
      }
    }
    if (!businessOK(result.response, result.body)) {
      throw new Error(`workspace_token_${String(result.body.status ?? result.response.status)}`);
    }

    const data = result.body.data || {};
    const accessToken = data.workspace_token ?? data.access_token ?? data.workspaceToken;
    const refreshToken = data.refresh_token ?? data.refreshToken;
    if (typeof accessToken !== "string" || !accessToken.trim()
        || typeof refreshToken !== "string" || !refreshToken.trim()) {
      throw new Error("workspace_pair_missing");
    }
    const now = Date.now();
    const accessSeconds = Number(data.expires_in ?? data.expiresIn ?? 86400);
    const refreshSeconds = Number(data.refresh_expires_in ?? data.refreshExpiresIn ?? 0);
    const next = Object.assign({}, current, {
      workspaceId,
      workspaceToken: accessToken.trim(),
      expiresAt: now + Math.max(accessSeconds, 1) * 1000,
      refreshToken: refreshToken.trim(),
      refreshExpiresAt: refreshSeconds > 0
        ? now + refreshSeconds * 1000
        : (current.refreshExpiresAt ?? current.refresh_expires_at ?? null),
      domain
    });
    workspaceList[index] = next;
    localStorage.setItem(listKey, JSON.stringify(workspaceList));

    bridge.postMessage({
      url: result.url,
      headers: {
        "authorization": `bearer ${accessToken.trim()}`,
        "x-device-id": deviceId,
        "app-language": "en",
        "app-platform": "web",
        "edit-from": "web",
        "timezone": timezone
      },
      workspaceList: JSON.stringify([next])
    });
    status("ok", "Plaud account session issued a fresh workspace credential.");
    return { status: "ok" };
  } catch (error) {
    const code = error && error.message ? String(error.message) : "recovery_failed";
    const needsLogin = code === "account_session_missing" || code === "account_session_expired";
    status(needsLogin ? "needsLogin" : "failed", code);
    return { status: needsLogin ? "needsLogin" : "failed", detail: code };
  }
})()
"""

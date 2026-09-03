"""HTTP client for the Plaud Cloud private API."""

from __future__ import annotations

import json
import logging
import random
import re
import time
from pathlib import Path
from typing import Any

import httpx
from pydantic import ValidationError

from .config import PlaudConfig
from .models import (
    FileContent,
    FileListPage,
    Folder,
    OutlineItem,
    PlaudFile,
    SummaryBlock,
    TranscriptSegment,
)


logger = logging.getLogger(__name__)


class PlaudAPIError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        api_status: int | str | None = None,
    ) -> None:
        super().__init__(message)
        # HTTP status when the server answered (e.g. 401/403); None for
        # network-level failures so callers can tell rejection from outage.
        self.status_code = status_code
        # Plaud often returns HTTP 200 with a non-zero business status. In
        # particular -419 means the workspace token is expired.
        try:
            self.api_status = int(api_status) if api_status is not None else None
        except (TypeError, ValueError):
            self.api_status = None

    @property
    def is_auth_rejection(self) -> bool:
        # -419: workspace token expired. -420: the workspace *refresh* token
        # was rejected too (observed 2026-08-19 after a web.plaud.ai sign-in
        # rotated the chain). Both mean "this credential is dead", not
        # "transient" — classifying -420 as transient is what kept the
        # recovery ladder from escalating to a browser re-harvest.
        return self.status_code in (401, 403) or self.api_status in (-419, 419, -420, 420)


def _safe_json(resp: httpx.Response, label: str) -> dict[str, Any]:
    """Decode a JSON body, surfacing a clear error on non-JSON/empty responses."""
    try:
        return resp.json()
    except ValueError:
        raise PlaudAPIError(f"Plaud returned a non-JSON response for {label}") from None


def _note_rejection(exc: PlaudAPIError, *, record_auth_rejections: bool = True) -> PlaudAPIError:
    """Persist the server's auth verdict, then hand the error back unchanged.

    Offline JWT expiry math cannot see a server-side invalidation, so without
    this memo `auth_status` keeps reporting a dead token as "valid" and the
    self-heal / recovery paths never fire.
    """
    if record_auth_rejections and exc.is_auth_rejection:
        from .auth_status import record_auth_rejection

        record_auth_rejection(status=exc.api_status or exc.status_code)
    return exc


class PlaudClient:
    def __init__(
        self,
        config: PlaudConfig,
        *,
        timeout: float = 30.0,
        record_auth_rejections: bool = True,
    ) -> None:
        self._config = config
        # Candidate credential probes must not mark the currently stored
        # credential generation as rejected. Normal application clients keep
        # the historical default and persist genuine server rejection memos.
        self._record_auth_rejections = record_auth_rejections
        self._client = httpx.Client(
            base_url=config.base_url,
            headers=config.headers(),
            timeout=timeout,
            # Credentials must never follow a server-controlled redirect to a
            # different origin. Plaud API calls are expected to answer on the
            # validated regional API host directly.
            follow_redirects=False,
            # Do not inherit host proxy credentials or routing from a shell.
            # A restricted workshop build talks directly to Plaud endpoints.
            trust_env=False,
        )

    def __enter__(self) -> "PlaudClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # ---------- core requests ----------

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            resp = self._client.request(method, path, **kwargs)
            resp.raise_for_status()
            return resp
        except httpx.HTTPStatusError as exc:
            label = _request_label(exc.request)
            status = exc.response.status_code
            raise _note_rejection(
                PlaudAPIError(f"Plaud HTTP {status} for {label}", status_code=status),
                record_auth_rejections=self._record_auth_rejections,
            ) from None
        except httpx.RequestError as exc:
            label = _request_label(exc.request)
            detail = str(exc) or exc.__class__.__name__
            raise PlaudAPIError(
                f"Plaud network error for {label}: {detail}. "
                "Check your internet connection or Plaud session, then retry."
            ) from None

    def _external_request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            resp = httpx.request(method, url, trust_env=False, **kwargs)
            resp.raise_for_status()
            return resp
        except httpx.HTTPStatusError as exc:
            label = _request_label(exc.request)
            status = exc.response.status_code
            raise PlaudAPIError(
                f"Plaud content HTTP {status} for {label}", status_code=status
            ) from None
        except httpx.RequestError as exc:
            label = _request_label(exc.request)
            detail = str(exc) or exc.__class__.__name__
            raise PlaudAPIError(
                f"Plaud content network error for {label}: {detail}. "
                "Check your internet connection or Plaud session, then retry."
            ) from None

    def _get_json(self, path: str, **params: Any) -> dict[str, Any]:
        resp = self._request("GET", path, params=params)
        data = _safe_json(resp, f"GET {path}")
        status = data.get("status")
        if status not in (0, "0", None):
            msg = data.get("msg") or data.get("error") or "unknown Plaud error"
            raise _note_rejection(
                PlaudAPIError(f"Plaud API error ({status}): {msg}", api_status=status),
                record_auth_rejections=self._record_auth_rejections,
            )
        return data

    def _patch_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        # Retry transient network errors only; HTTP errors (incl. 5xx) are NOT
        # retried — PATCH targets here are idempotent field-sets, so retry-on-
        # timeout is safe, but we surface server errors immediately.
        last_exc: httpx.RequestError | None = None
        for attempt in range(3):
            try:
                resp = self._client.patch(path, json=payload)
                resp.raise_for_status()
                break
            except httpx.HTTPStatusError as exc:
                label = _request_label(exc.request)
                status = exc.response.status_code
                raise PlaudAPIError(
                    f"Plaud HTTP {status} for {label}", status_code=status
                ) from None
            except httpx.RequestError as exc:
                last_exc = exc
                if attempt == 2:
                    label = _request_label(exc.request)
                    detail = str(exc) or exc.__class__.__name__
                    raise PlaudAPIError(
                        f"Plaud network error for {label}: {detail}. "
                        "Check your internet connection or Plaud session, then retry."
                    ) from None
                time.sleep(1.5 * (attempt + 1) + random.uniform(0, 0.5))
        else:
            raise last_exc or PlaudAPIError("PATCH request failed")
        data = _safe_json(resp, f"PATCH {path}")
        status = data.get("status")
        if status not in (0, "0", None):
            msg = data.get("msg") or data.get("error") or "unknown Plaud error"
            raise PlaudAPIError(f"Plaud API error: {msg}")
        return data

    # ---------- file list ----------

    def list_files(
        self,
        *,
        limit: int = 20,
        skip: int = 0,
        is_trash: int = 0,
        sort_by: str = "edit_time",
    ) -> FileListPage:
        data = self._get_json(
            "/file/simple/web",
            skip=skip,
            limit=limit,
            is_trash=is_trash,
            sort_by=sort_by,
            is_desc="true",
        )
        # Validate the response shape instead of masking a changed API with [].
        if "data_file_list" not in data:
            raise PlaudAPIError("Plaud list response missing 'data_file_list'")
        items = [PlaudFile(**f) for f in data["data_file_list"] or []]
        # Don't rely on the server's is_desc magic param for the default sort.
        if sort_by == "edit_time":
            items.sort(key=lambda f: f.edit_time or 0, reverse=True)
        return FileListPage(total=data.get("data_file_total") or 0, items=items)

    # ---------- file detail / content ----------

    def file_detail(self, file_id: str) -> dict[str, Any]:
        return self._get_json(f"/file/detail/{file_id}")

    def temp_url(self, file_id: str) -> str:
        data = self._get_json(f"/file/temp-url/{file_id}")
        # Prefer the regular (mp3) URL for AVPlayer/codec compatibility, matching
        # cli.audio_url and core.transcribe; fall back to opus when absent.
        url = data.get("temp_url") or data.get("temp_url_opus")
        if not url:
            raise PlaudAPIError(f"missing temp_url for {file_id}")
        return url

    def file_content(self, file_id: str) -> FileContent:
        """Fetch detail + dereference S3 content links into structured content."""
        detail = self.file_detail(file_id).get("data") or {}
        header = (detail.get("extra_data") or {}).get("aiContentHeader") or {}
        title = header.get("headline")
        keywords = header.get("keywords", [])
        folder_ids = detail.get("filetag_id_list") or []

        transcript: list[TranscriptSegment] = []
        outline: list[OutlineItem] = []
        summaries: list[SummaryBlock] = []

        for item in detail.get("content_list") or []:
            link = item.get("data_link")
            if not link:
                continue
            data_type = item.get("data_type")
            # Only swallow content-shape errors (malformed JSON / schema mismatch).
            # Let PlaudAPIError (network/HTTP from the S3 fetch) propagate so the
            # file is NOT cached as empty and stays queued for retry.
            try:
                if data_type == "transaction":
                    payload = self._fetch_link_json(link)
                    transcript = [TranscriptSegment(**s) for s in payload]
                elif data_type == "outline":
                    payload = self._fetch_link_json(link)
                    outline = [OutlineItem(**s) for s in payload]
                elif data_type in ("auto_sum_note", "sum_multi_note"):
                    summaries.append(
                        SummaryBlock(
                            kind=data_type,
                            title=item.get("data_title") or item.get("data_tab_name"),
                            body_md=self._fetch_link_text(link),
                        )
                    )
            except (json.JSONDecodeError, ValidationError) as exc:
                logger.warning("skipping malformed %s content for %s: %s", data_type, file_id, exc)
                continue

        return FileContent(
            file_id=file_id,
            title=title or detail.get("file_name"),
            transcript=transcript,
            outline=outline,
            summaries=summaries,
            keywords=keywords,
            folder_ids=folder_ids,
        )

    def _fetch_link_json(self, url: str) -> Any:
        r = self._external_request("GET", url, timeout=30, follow_redirects=True)
        return r.json()

    def _fetch_link_text(self, url: str) -> str:
        r = self._external_request("GET", url, timeout=30, follow_redirects=True)
        return r.text

    # ---------- folders ----------

    def list_folders(self) -> list[Folder]:
        data = self._get_json("/filetag/")
        return [Folder(**f) for f in data.get("data_filetag_list") or []]

    def create_folder(
        self, name: str, *, color: str | None = None, icon: str | None = None
    ) -> Folder:
        payload: dict[str, Any] = {"name": name}
        if color:
            payload["color"] = color
        if icon:
            payload["icon"] = icon
        resp = self._request("POST", "/filetag/", json=payload)
        data = _safe_json(resp, "POST /filetag/")
        if data.get("status") not in (0, "0", None):
            raise PlaudAPIError(data.get("msg") or "create_folder failed")
        payload = data.get("data") or data
        if isinstance(payload, dict) and "data_filetag" in payload:
            payload = payload["data_filetag"]
        return Folder(**payload)

    def rename_folder(
        self,
        folder_id: str,
        *,
        name: str | None = None,
        color: str | None = None,
        icon: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {}
        if name:
            payload["name"] = name
        if color:
            payload["color"] = color
        if icon:
            payload["icon"] = icon
        self._patch_json(f"/filetag/{folder_id}", payload)

    def delete_folder(self, folder_id: str) -> None:
        resp = self._request("DELETE", f"/filetag/{folder_id}")
        if not resp.content:  # 204 / empty body == success
            return
        data = _safe_json(resp, f"DELETE /filetag/{folder_id}")
        if data.get("status") not in (0, "0", None):
            raise PlaudAPIError(data.get("msg") or "delete_folder failed")

    def set_file_folders(self, file_id: str, folder_ids: list[str]) -> None:
        """Assign / unassign folders for a file via PATCH /file/{id}.

        Plaud web renders at most ONE folder per file — pushing multiple
        filetag ids corrupts the web UI, so we hard-reject it here.
        """
        if len(folder_ids) > 1:
            raise ValueError(
                "Plaud supports a single folder per file; got "
                f"{len(folder_ids)}: {', '.join(folder_ids)}"
            )
        self._patch_json(f"/file/{file_id}", {"filetag_id_list": folder_ids})

    def rename_file(self, file_id: str, name: str) -> None:
        self._patch_json(f"/file/{file_id}", {"filename": name})

    # ---------- server transcript speaker edits (web parity) ----------
    # web.plaud.ai renames transcript speakers by PATCHing the FULL segment
    # list back as `trans_result` (captured 2026-06-11). Segments carry extra
    # fields we must round-trip verbatim (original_speaker, embeddingKey, …),
    # so these helpers work on the raw dicts, not TranscriptSegment models.

    def raw_transcript(self, file_id: str) -> list[dict[str, Any]]:
        """The server transcript as raw segment dicts (all fields preserved)."""
        detail = self.file_detail(file_id).get("data") or {}
        for item in detail.get("content_list") or []:
            if item.get("data_type") != "transaction" or not item.get("data_link"):
                continue
            payload = self._fetch_link_json(item["data_link"])
            if isinstance(payload, list):
                return payload
        return []

    def update_transcript(self, file_id: str, segments: list[dict[str, Any]]) -> None:
        """Replace the server transcript via PATCH /file/{id} trans_result."""
        self._patch_json(
            f"/file/{file_id}",
            {"trans_result": segments, "support_mul_summ": True},
        )

    def rename_transcript_speakers(self, file_id: str, mapping: dict[str, str]) -> int:
        """Rename speakers in the Plaud SERVER transcript of one file.

        `mapping` maps the currently displayed speaker name to the new name.
        The STT label is preserved in `original_speaker` (set once, never
        overwritten) so renames stay reversible on the web. Returns the
        number of segments changed; 0 means nothing was pushed.
        """
        segments = self.raw_transcript(file_id)
        if not segments:
            raise PlaudAPIError(f"no server transcript for {file_id}")
        changed = 0
        for seg in segments:
            old = seg.get("speaker")
            new = mapping.get(old) if isinstance(old, str) else None
            if not new or new == old:
                continue
            seg.setdefault("original_speaker", old)
            seg["speaker"] = new
            changed += 1
        if changed:
            self.update_transcript(file_id, segments)
        return changed

    # ---------- server-side note + speaker edits (web parity) ----------
    # Reverse-engineered from web.plaud.ai (captured 2026-05-31).

    def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        resp = self._request("POST", path, json=payload)
        data = _safe_json(resp, f"POST {path}")
        status = data.get("status")
        if status not in (0, "0", None):
            msg = data.get("msg") or data.get("error") or "unknown Plaud error"
            raise PlaudAPIError(f"Plaud API error: {msg}")
        return data

    def update_note_info(
        self,
        *,
        file_id: str,
        note_id: str,
        note_type: str,
        note_content: str,
        note_tab_name: str,
        note_title: str,
    ) -> dict[str, Any]:
        """Edit a note's content/title on the Plaud server (e.g. the AI summary).

        POST /ai/update_note_info. note_id / note_type / note_tab_name come from
        the file detail's note_list (e.g. note_type='auto_sum_note',
        note_tab_name='Summary', note_id='auto_sum:<hash>:<file_id>').
        """
        return self._post_json(
            "/ai/update_note_info",
            {
                "file_id": file_id,
                "note_id": note_id,
                "note_type": note_type,
                "note_content": note_content,
                "note_tab_name": note_tab_name,
                "note_title": note_title,
            },
        )

    def list_server_speakers(self) -> list[dict[str, Any]]:
        """Server-side speaker roster (voiceprint profiles) via GET /speaker/list.

        The response wrapper is not yet verified against a live capture, so we
        probe the common key shapes and fall back to a bare list.
        """
        data = self._get_json("/speaker/list")
        for key in ("data_speaker_list", "speaker_list", "speakers", "data"):
            val = data.get(key)
            if isinstance(val, list):
                return val
            if isinstance(val, dict) and isinstance(val.get("speakers"), list):
                return val["speakers"]
        return []

    def sync_speakers(self, speakers: list[dict[str, Any]]) -> dict[str, Any]:
        """Create/update server speaker profiles via POST /speaker/sync.

        Pass full speaker records (as returned by list_server_speakers) with the
        fields you want changed (e.g. speaker_name). Renaming a profile renames
        that voiceprint everywhere it appears (per-voiceprint, not per-file).
        """
        return self._post_json("/speaker/sync", {"speakers": speakers})

    # ---------- audio download ----------

    def download(self, file_id: str, output_dir: Path, *, force: bool = False) -> Path:
        detail = self.file_detail(file_id)
        url = self.temp_url(file_id)
        display_name = (detail.get("data") or {}).get("file_name") or ""
        ext = _extract_extension(url)
        filename = f"{_sanitize(display_name)}__{file_id}.{ext}"
        output_dir.mkdir(parents=True, exist_ok=True)
        target = output_dir / filename
        if target.exists() and not force:
            return target
        try:
            # Stream the presigned S3 URL with a bare client — NOT self._client,
            # which carries Plaud auth headers (authorization / x-pld-*). The S3
            # link is self-authenticating via its query signature, so sending
            # Plaud credentials to the third-party storage host would leak them.
            with httpx.stream(
                "GET", url, follow_redirects=True, timeout=self._client.timeout
            ) as resp:
                resp.raise_for_status()
                with target.open("wb") as f:
                    for chunk in resp.iter_bytes():
                        f.write(chunk)
        except httpx.HTTPStatusError as exc:
            label = _request_label(exc.request)
            status = exc.response.status_code
            raise PlaudAPIError(
                f"Plaud audio HTTP {status} for {label}", status_code=status
            ) from None
        except httpx.RequestError as exc:
            label = _request_label(exc.request)
            detail = str(exc) or exc.__class__.__name__
            raise PlaudAPIError(
                f"Plaud audio network error for {label}: {detail}. "
                "Check your internet connection or Plaud session, then retry."
            ) from None
        return target


def _request_label(request: httpx.Request | None) -> str:
    if request is None:
        return "request"
    url = request.url
    host = url.host or ""
    path = url.path or "/"
    return f"{request.method} {host}{path}"


def _extract_extension(url: str) -> str:
    path = url.split("?", 1)[0]
    if "." not in path.rsplit("/", 1)[-1]:
        return "bin"
    return path.rsplit(".", 1)[-1] or "bin"


def _sanitize(value: str) -> str:
    value = re.sub(r'[\\/:*?"<>|]+', "_", value)
    value = re.sub(r"\s+", " ", value).strip()
    value = re.sub(r"_+", "_", value).strip(" ._")
    return value or "plaud-file"

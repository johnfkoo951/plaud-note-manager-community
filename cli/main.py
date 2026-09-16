"""`plaud` CLI — typer wrapper over core."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any, Callable, TypeVar

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table

from core import PlaudClient, app_config, load_config
from core.client import PlaudAPIError
from core.community_models import (
    PROVIDER_LABELS,
    ModelUnavailable,
    validate_route,
)
from core.config import ConfigError
from core.distribution import COMMUNITY_EDITION
from core.storage import DEFAULT_DB, Storage
from core.tags import normalize_tags

# Private-edition taxonomy modules are intentionally absent from this source
# tree. Community routing uses only folders fetched from the current user's
# Plaud workspace.
FOLDER_TAXONOMY: tuple[Any, ...] = ()
MODEL_HELP = "provider: " + " | ".join(PROVIDER_LABELS)


def classify_snapshot(*_args: Any, **_kwargs: Any) -> None:
    raise RuntimeError("classification is unavailable in the Community edition")


if COMMUNITY_EDITION:
    os.umask(0o077)

app = typer.Typer(no_args_is_help=True, add_completion=False)
console = Console()
F = TypeVar("F", bound=Callable[..., Any])

COMMUNITY_ALLOWED_COMMANDS = frozenset(
    {
        "peek",
        "brief",
        "outline-of",
        "deep",
        "query",
        "resources",
        "show",
        "list",
        "sync",
        "sync-content",
        "folders",
        "folder-create",
        "folder-rename",
        "folder-delete",
        "move",
        "auto-folder",
        "classify-undo",
        "classify-undo-status",
        "rename",
        "detail",
        "transcript",
        "summary",
        "outline",
        "download",
        "status",
        "auth",
        "disconnect",
        "refresh-auth",
        "web-auth",
        "ws-refresh",
        "ws-bootstrap",
        "metadata",
        "usage-status",
        "tags",
        "tag-add",
        "tags-all",
        "tag-pin",
        "tag-remove",
        "search",
        "search-reindex",
        "prune-empty-cache",
        "export",
        "audio-url",
        "web",
        "provider-key-set",
        "provider-key-status",
        "provider-key-delete",
        "elevenlabs-attempt-status",
        "elevenlabs-transcribe",
        "server-speakers",
        "speaker-rename-server",
        "plaud-relabel",
        "note-edit",
        "star",
        "config",
        "config-classify",
        "config-backend",
        "config-model",
        "config-folder-threshold",
        "config-path",
        "contents",
        "paths",
        "onboard",
    }
)


def _require_vault(vault: Path | None) -> Path:
    """Resolve the Obsidian vault or exit with a friendly hint."""
    resolved = vault or app_config.obsidian_vault()
    if resolved is None:
        console.print(
            "[red]Obsidian vault not configured[/red] — set PLAUD_OBSIDIAN_VAULT "
            "or run: [bold]uv run plaud config-vault <path>[/bold]"
        )
        raise typer.Exit(1)
    return resolved


def _handle_cli_errors(fn: F) -> F:
    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except ConfigError:
            console.print(
                "[red]not configured[/red] — run: [bold]pbpaste | uv run plaud onboard[/bold]"
            )
            raise typer.Exit(1) from None
        except PlaudAPIError as exc:
            console.print(f"[red]Plaud command failed[/red]: {exc}")
            raise typer.Exit(1) from None

    return wrapper  # type: ignore[return-value]


def safe_command(*args: Any, **kwargs: Any) -> Callable[[F], F]:
    def decorator(fn: F) -> F:
        command_name = str(kwargs.get("name") or fn.__name__.removesuffix("_cmd")).replace("_", "-")
        handled = _handle_cli_errors(fn)

        @wraps(fn)
        def community_guard(*fn_args: Any, **fn_kwargs: Any) -> Any:
            if COMMUNITY_EDITION and command_name not in COMMUNITY_ALLOWED_COMMANDS:
                console.print(
                    "[red]Unavailable in the Community edition[/red] — "
                    "this command can access external AI, shell credentials, or a private vault."
                )
                raise typer.Exit(2)
            return handled(*fn_args, **fn_kwargs)

        return app.command(*args, **kwargs)(community_guard)  # type: ignore[return-value]

    return decorator


def _emit_json(obj: Any) -> None:
    """Write a JSON document to stdout for --json command output."""
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, indent=2))
    sys.stdout.write("\n")


def _peek_date(start_time_ms: int | None, edit_time_s: int | None) -> str | None:
    """Format a recording date (start_time is epoch-ms, edit_time epoch-s)."""
    for ts, divisor in ((start_time_ms, 1000), (edit_time_s, 1)):
        if not ts:
            continue
        try:
            dt = datetime.fromtimestamp(int(ts) / divisor)
        except (ValueError, OSError, OverflowError):
            continue
        if 2000 <= dt.year <= 2100:
            return dt.strftime("%Y-%m-%d")
    return None


def ensure_content_cached(storage: Storage, file_id: str) -> None:
    """Fetch Plaud detail once when local transcript/summary cache is empty."""
    if storage.get_content_row(file_id):
        return
    cfg = load_config()
    with PlaudClient(cfg) as client:
        content = client.file_content(file_id)
    storage.save_content(content, now=int(time.time()))


@safe_command(name="peek")
def peek_cmd(file_id: str, json_out: bool = typer.Option(False, "--json")) -> None:
    """L0 — fastest. filename · date · duration · folders · cache status."""
    from core.disclosure import peek as do_peek

    r = do_peek(file_id)
    if not r:
        console.print(f"[red]not found[/red] {file_id}")
        raise typer.Exit(1)
    if json_out:
        _emit_json(asdict(r))
        return
    console.print(f"[bold]{r.filename}[/bold]  [dim]{r.file_id}[/dim]")
    when = _peek_date(r.start_time, r.edit_time)
    if when:
        console.print(f"  date: {when}")
    console.print(f"  folders: {', '.join(r.folders) or '(unfiled)'}")
    if r.duration_ms:
        secs = int(r.duration_ms / 1000)
        console.print(f"  duration: {secs // 60}m {secs % 60}s")
    console.print(
        f"  cache: content={'✓' if r.has_content_cache else '·'} "
        f"cmds={'✓' if r.has_cmds_transcript else '·'} "
        f"integrated={r.integrated_count}"
    )


@safe_command(name="brief")
def brief_cmd(file_id: str, json_out: bool = typer.Option(False, "--json")) -> None:
    """L1 — + title · keywords · tags · vault link counts · speakers."""
    from core.disclosure import brief as do_brief

    r = do_brief(file_id)
    if not r:
        console.print(f"[red]not found[/red] {file_id}")
        raise typer.Exit(1)
    if json_out:
        _emit_json(asdict(r))
        return
    console.print(f"[bold]{r.title or r.filename}[/bold]  [dim]{r.file_id}[/dim]")
    console.print(f"  folders : {', '.join(r.folders) or '(unfiled)'}")
    if r.keywords:
        console.print(
            f"  keywords: {', '.join(r.keywords[:12])}{' …' if len(r.keywords) > 12 else ''}"
        )
    if r.tags:
        console.print(f"  tags    : {', '.join(r.tags)}")
    if r.speakers:
        console.print(f"  speakers: {', '.join(r.speakers)}")
    if r.vault_links:
        parts = [f"{k}={v}" for k, v in r.vault_links.items()]
        console.print(f"  vault   : {' '.join(parts)}")


@safe_command(name="outline-of")
def outline_of_cmd(file_id: str, json_out: bool = typer.Option(False, "--json")) -> None:
    """L2 — + Plaud auto-summary preview · outline · integrated preview."""
    from core.disclosure import outline as do_outline

    r = do_outline(file_id)
    if not r:
        console.print(f"[red]not found[/red] {file_id}")
        raise typer.Exit(1)
    if json_out:
        _emit_json(asdict(r))
        return
    console.print(f"[bold]{r.title or r.filename}[/bold]\n")
    if r.plaud_summary_preview:
        console.print("[bold]Plaud summary preview[/bold]")
        console.print(r.plaud_summary_preview, "\n")
    if r.plaud_outline_preview:
        console.print("[bold]Outline preview[/bold]")
        console.print(r.plaud_outline_preview, "\n")
    if r.integrated_summary_preview:
        console.print("[bold]Integrated summary preview[/bold]")
        console.print(r.integrated_summary_preview)


@safe_command(name="deep")
def deep_cmd(
    file_id: str,
    json_out: bool = typer.Option(False, "--json"),
    section: str = typer.Option(
        "", help="just one section: transcript|cmds|integrated-summary|integrated-transcript|vault"
    ),
) -> None:
    """L3 — full content. Heavy; use --section to narrow."""
    from core.disclosure import deep as do_deep

    valid_sections = {
        "transcript",
        "cmds",
        "integrated-summary",
        "integrated-transcript",
        "vault",
    }
    if section and section not in valid_sections:
        console.print(
            f"[red]invalid --section '{section}'[/red] — choose one of: "
            f"{', '.join(sorted(valid_sections))}"
        )
        raise typer.Exit(1)
    r = do_deep(file_id)
    if not r:
        console.print(f"[red]not found[/red] {file_id}")
        raise typer.Exit(1)
    if json_out:
        _emit_json(asdict(r))
        return
    if section == "transcript":
        console.print(r.plaud_transcript or "(no Plaud transcript)")
        return
    if section == "cmds":
        console.print(r.cmds_transcript or "(no CMDS transcript)")
        return
    if section == "integrated-summary":
        console.print(r.integrated_summary or "(no integrated summary)")
        return
    if section == "integrated-transcript":
        console.print(r.integrated_transcript or "(no integrated transcript)")
        return
    if section == "vault":
        for link in r.vault_link_details:
            console.print(
                f"[{link['confidence']:.1f}] {link['vault']}/"
                f"{link['rel_path']}  [dim]({link['match_kind']}: {link['keyword']})[/dim]"
            )
        return
    # default summary
    console.print(f"[bold]{r.title or r.filename}[/bold]\n")
    console.print(f"keywords: {', '.join(r.keywords[:15])}")
    console.print(f"vault links: {len(r.vault_link_details)}")
    console.print(
        f"transcript: {'✓' if r.plaud_transcript else '·'} "
        f"cmds: {'✓' if r.cmds_transcript else '·'} "
        f"integrated: {'✓' if r.integrated_summary else '·'}"
    )


@safe_command(name="query")
def query_cmd(
    keyword: str = typer.Option("", "--keyword", "-k"),
    tag: str = typer.Option("", "--tag", "-t"),
    folder: str = typer.Option("", "--folder", "-f"),
    vault_note: str = typer.Option(
        "", "--vault-note", "-v", help="title of a vault note linked to the file"
    ),
    limit: int = typer.Option(30, "--limit", "-n"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Search files by keyword / tag / folder / vault note. Returns L1 briefs."""
    from core.disclosure import search

    results = search(
        keyword=keyword or None,
        tag=tag or None,
        folder=folder or None,
        vault_note_title=vault_note or None,
        limit=limit,
    )
    if json_out:
        _emit_json([asdict(r) for r in results])
        return
    if not results:
        console.print("[yellow]no matches[/yellow]")
        return
    table = Table(title=f"{len(results)} match(es)")
    table.add_column("file_id", overflow="fold")
    table.add_column("title")
    table.add_column("folders")
    table.add_column("keywords (preview)")
    for r in results:
        table.add_row(
            r.file_id,
            (r.title or r.filename or "-")[:60],
            ", ".join(r.folders) or "(unfiled)",
            ", ".join(r.keywords[:5]),
        )
    console.print(table)


@safe_command(name="resources")
def resources_cmd(
    file_id: str = typer.Argument(""),
    since: float = typer.Option(0.0, help="only resources modified after this unix mtime"),
    json_out: bool = typer.Option(False, "--json", help="emit JSON for piping"),
    manifest: bool = typer.Option(False, "--manifest", help="write data/manifest.json"),
) -> None:
    """List local resources (transcripts/summaries/integrated) as URIs + paths.

    Examples:
      plaud resources                          # everything
      plaud resources <file_id>                # one recording
      plaud resources --since 1714000000       # incremental for embeddings
      plaud resources --json | jq              # pipe to embedder
      plaud resources --manifest               # write data/manifest.json
    """
    import json as _json
    from core.locator import (
        DATA_DIR,
        build_manifest,
        iter_resources,
        resources_for,
    )

    if manifest:
        out = build_manifest()
        path = DATA_DIR / "manifest.json"
        path.write_text(_json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        console.print(f"[green]wrote[/green] {path}  ({out['count']} items)")
        return

    if file_id:
        items = resources_for(file_id)
    else:
        items = [r for r in iter_resources(since_mtime=since)]

    if json_out:
        # Plain stdout (not rich) so downstream `jq` pipelines parse cleanly.
        import sys

        sys.stdout.write(_json.dumps([r.to_dict() for r in items], ensure_ascii=False, indent=2))
        sys.stdout.write("\n")
        return

    table = Table(title=f"{len(items)} local resource(s)")
    table.add_column("uri", overflow="fold")
    table.add_column("size", justify="right")
    table.add_column("path", overflow="fold")
    for r in items:
        table.add_row(r.uri, f"{r.size}", str(r.path))
    console.print(table)


@safe_command(name="show")
def show_cmd(uri: str) -> None:
    """Print one local resource by its plaud:// uri or absolute path."""
    from core.locator import parse_uri, resources_for

    parsed = parse_uri(uri)
    if parsed:
        _, file_id, _ = parsed
        for r in resources_for(file_id):
            if r.uri == uri:
                console.print(r.path.read_text(encoding="utf-8"))
                return
        console.print(f"[red]not found[/red] {uri}")
        raise typer.Exit(1)
    path = Path(uri)
    if not path.exists():
        console.print(f"[red]not found[/red] {uri}")
        raise typer.Exit(1)
    console.print(path.read_text(encoding="utf-8"))


@safe_command(name="list")
def list_cmd(limit: int = 20, skip: int = 0, trash: bool = False) -> None:
    """List recent files from Plaud Cloud."""
    cfg = load_config()
    with PlaudClient(cfg) as client:
        page = client.list_files(limit=limit, skip=skip, is_trash=1 if trash else 0)

    table = Table(title=f"Plaud files ({len(page.items)} of {page.total})")
    table.add_column("id", overflow="fold")
    table.add_column("filename")
    table.add_column("dur", justify="right")
    table.add_column("edit_time", justify="right")
    for f in page.items:
        table.add_row(
            f.id, f.filename or f.fullname or "-", f"{f.duration or 0:.0f}", str(f.edit_time or "")
        )
    console.print(table)


@safe_command()
def sync() -> None:
    """Pull file list + folders into local SQLite (fast: metadata only)."""
    cfg = load_config()
    storage = Storage()
    now = int(time.time())
    with PlaudClient(cfg) as client:
        folders = client.list_folders()
        storage.replace_folders(folders, now=now)

        for is_trash in (0, 1):
            page = client.list_files(limit=2000, is_trash=is_trash)
            for f in page.items:
                storage.upsert_file(f, now=now, is_trash=is_trash)

    console.print(
        f"[green]synced[/green] folders={len(folders)} -> {DEFAULT_DB}\n"
        "Run [bold]plaud sync-content[/bold] (or click Backfill in the app) "
        "to populate transcripts/summaries + folder assignments."
    )


@safe_command(name="sync-content")
def sync_content(parallel: int = 6) -> None:
    """Background backfill: fetch transcript/summary for every file lacking cache."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    cfg = load_config()
    storage = Storage()
    pending = storage.files_without_content()
    if not pending:
        console.print("[green]all files already cached[/green]")
        _maybe_auto_metadata(storage)
        return
    console.print(f"backfilling {len(pending)} files with parallel={parallel}")
    done = 0

    def fetch(file_id: str) -> str:
        with PlaudClient(cfg) as client:
            content = client.file_content(file_id)
            storage.save_content(content, now=int(time.time()))
        return file_id

    with ThreadPoolExecutor(max_workers=parallel) as ex:
        futures = {ex.submit(fetch, f["id"]): f["id"] for f in pending}
        for fut in as_completed(futures):
            done += 1
            try:
                fut.result()
            except Exception as e:
                console.print(f"[red]err[/red] {futures[fut]}: {e}")
            if done % 10 == 0:
                console.print(f"  {done}/{len(pending)}")
    console.print(f"[green]done[/green] {done}/{len(pending)}")
    _maybe_auto_metadata(storage)


def _maybe_auto_metadata(storage: Storage) -> None:
    """Post-sync hook. The Community edition never generates metadata with an
    external model, so this is a deliberate no-op (the full edition's
    auto_metadata module is not shipped)."""
    return None


@safe_command()
def folders() -> None:
    """List folders (filetags)."""
    cfg = load_config()
    with PlaudClient(cfg) as client:
        for f in client.list_folders():
            console.print(f"[{f.color or '-'}] {f.name}  [dim]{f.id}[/dim]")


@safe_command(name="folder-create")
def folder_create(name: str, color: str = "", icon: str = "") -> None:
    """Create a new folder (filetag)."""
    cfg = load_config()
    with PlaudClient(cfg) as client:
        folder = client.create_folder(name, color=color or None, icon=icon or None)
    console.print(f"[green]created[/green] {folder.id}  {folder.name}")


@safe_command(name="folder-rename")
def folder_rename(folder_id: str, name: str = "", color: str = "", icon: str = "") -> None:
    """Rename / restyle a folder. Pass empty string to leave a field unchanged."""
    cfg = load_config()
    with PlaudClient(cfg) as client:
        client.rename_folder(
            folder_id,
            name=name or None,
            color=color or None,
            icon=icon or None,
        )
    console.print(f"[green]updated[/green] {folder_id}")


@safe_command(name="move")
def move(file_id: str, folder_id: str = typer.Argument(None)) -> None:
    """Assign a file to ONE folder (replacing the old one). Omit folder to clear.

    Plaud web supports a single folder per file — multiple assignments corrupt
    the web UI, so this command no longer accepts more than one folder.
    """
    folder_ids = [folder_id] if folder_id else []
    cfg = load_config()
    with PlaudClient(cfg) as client:
        client.set_file_folders(file_id, folder_ids)
    storage = Storage()
    storage.set_file_folders(file_id, folder_ids)
    console.print(f"[green]moved[/green] {file_id} -> {folder_id or '(Unfiled)'}")


@safe_command(name="auto-folder")
def auto_folder(
    apply: bool = typer.Option(
        False,
        "--apply",
        help="Apply selected preview rows. Without this flag Cloud is never mutated.",
    ),
    only: list[str] = typer.Option(
        None,
        "--only",
        help="Explicit file id to apply/preview (repeatable; required with --apply).",
    ),
    plan_id: str = typer.Option(
        "",
        "--plan-id",
        help="Exact plan id returned by the reviewed preview; required with --apply.",
    ),
    include_filed: bool = typer.Option(
        False,
        "--include-filed",
        help="Also preview recordings already assigned to a folder.",
    ),
    limit: int = typer.Option(0, "--limit", min=0, help="Maximum rows; 0 means all."),
    min_confidence: float = typer.Option(
        0.6,
        "--min-confidence",
        min=0.0,
        max=1.0,
        help="Selected rows below this local/model score are not applied.",
    ),
    llm: bool = typer.Option(
        False,
        "--llm",
        help="Allow the explicitly selected external model to arbitrate weak local matches.",
    ),
    provider: str = typer.Option(
        "",
        "--provider",
        help=MODEL_HELP + ". Required with --llm; never inferred as consent.",
    ),
    backend: str = typer.Option(
        "",
        "--backend",
        help="cli (vendor app/OAuth) or api (protected API key). Required with --llm.",
    ),
    confirm_external: bool = typer.Option(
        False,
        "--confirm-external",
        help=(
            "Confirm this preview may send folder names plus recording title, keywords, "
            "summary, and transcript to the selected provider."
        ),
    ),
    max_ai_requests: int = typer.Option(
        20,
        "--max-ai-requests",
        min=0,
        max=50,
        help="Per-preview external model request cap (0 disables arbitration calls).",
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit preview rows as a JSON array."),
) -> None:
    """Preview automatic routes into existing Plaud folders; apply selected rows only."""
    from core.community_router import (
        PreviewPlanError,
        UndoManifestError,
        apply_saved_plan,
        route_recordings,
        write_preview_plan,
    )
    from core.paths import DATA_DIR

    if apply and not only:
        raise typer.BadParameter("--apply requires at least one --only FILE_ID")
    if apply and not plan_id:
        raise typer.BadParameter("--apply requires --plan-id from the reviewed preview")
    if not apply and plan_id:
        raise typer.BadParameter("--plan-id is only valid with --apply")
    if apply and (llm or provider or backend or confirm_external or max_ai_requests != 20):
        raise typer.BadParameter(
            "--apply reuses the exact saved preview; model flags belong on the preview command"
        )
    if llm:
        try:
            provider, backend = validate_route(provider, backend)
        except ModelUnavailable as exc:
            raise typer.BadParameter(str(exc)) from exc

    storage = Storage()
    cfg = load_config()
    with PlaudClient(cfg) as client:
        try:
            if apply:
                undo_path = DATA_DIR / "last_classify.json"
                report = apply_saved_plan(
                    storage,
                    client,
                    selected_ids=only or (),
                    plan_path=DATA_DIR / "auto_folder_preview.json",
                    expected_plan_id=plan_id,
                    undo_path=undo_path,
                    min_confidence=min_confidence,
                )
            else:
                report = route_recordings(
                    storage,
                    client,
                    selected_ids=only,
                    include_filed=include_filed,
                    limit=limit or None,
                    use_llm=llm,
                    provider=provider,
                    backend=backend,
                    confirmed_external=confirm_external,
                    max_llm_calls=max_ai_requests,
                )
                if not report.error:
                    write_preview_plan(report, DATA_DIR / "auto_folder_preview.json")
        except (PreviewPlanError, UndoManifestError, ValueError) as exc:
            raise typer.BadParameter(str(exc)) from exc

    if json_out:
        _emit_json(report.public_rows())
        return
    if report.error:
        console.print(f"[yellow]{report.error}[/yellow]")
        return
    if not apply:
        console.print(f"[dim]plan id: {report.plan_id}[/dim]")
    for decision in report.decisions:
        target = decision.folder_name or "(no confident match)"
        applied = " [green]applied[/green]" if decision.applied else ""
        error = f" [yellow]{decision.error}[/yellow]" if decision.error else ""
        console.print(
            f"{decision.confidence:.2f}  [bold]{target}[/bold]  "
            f"[dim]{decision.title} · {decision.reason} · {decision.source}[/dim]"
            f"{applied}{error}"
        )
    if apply:
        console.print(
            f"[green]moved {report.applied_count}[/green] explicitly selected recording(s) — "
            "undo with `plaud classify-undo`"
        )


@safe_command(name="folder-doctor")
def folder_doctor(
    apply: bool = typer.Option(False, "--apply", help="Fix by keeping one folder per file."),
) -> None:
    """Find files whose local mapping has >1 folder and repair to a single one.

    Keeps the folder recorded in note_metadata when available, otherwise the
    first linked folder; with --apply the fix is pushed to Plaud and SQLite.
    """
    storage = Storage()
    broken = storage.files_with_multiple_folders()
    if not broken:
        console.print("[green]ok[/green] every file has at most one folder")
        return
    fixes: list[tuple[str, str]] = []
    for file_id, ids in broken:
        meta = storage.get_note_metadata(file_id)
        keep = ids[0]
        if meta and meta["folder_id"] in ids:
            keep = meta["folder_id"]
        fixes.append((file_id, keep))
        console.print(f"  {file_id}: {len(ids)} folders -> keep {keep}")
    if not apply:
        console.print(f"[yellow]{len(fixes)} files need repair[/yellow] — rerun with --apply")
        return
    cfg = load_config()
    with PlaudClient(cfg) as client:
        for file_id, keep in fixes:
            client.set_file_folders(file_id, [keep])
            storage.set_file_folders(file_id, [keep])
    console.print(f"[green]repaired[/green] {len(fixes)} files to single-folder")


@safe_command()
def rename(file_id: str, name: str) -> None:
    """Rename a Plaud session (PATCH /file/{file_id})."""
    new_name = name.strip()
    if not new_name:
        raise typer.BadParameter("name cannot be empty")
    cfg = load_config()
    with PlaudClient(cfg) as client:
        client.rename_file(file_id, new_name)
    Storage().set_file_name(file_id, new_name, now=int(time.time()))
    console.print(f"[green]renamed[/green] {file_id} -> {new_name}")


@safe_command(name="folder-delete")
def folder_delete(folder_id: str) -> None:
    """Delete a folder."""
    cfg = load_config()
    with PlaudClient(cfg) as client:
        client.delete_folder(folder_id)
    console.print(f"[green]deleted[/green] {folder_id}")


@safe_command()
def detail(file_id: str) -> None:
    """Fetch full content for a file and cache it locally."""
    cfg = load_config()
    storage = Storage()
    with PlaudClient(cfg) as client:
        content = client.file_content(file_id)
    storage.save_content(content, now=int(time.time()))
    console.print(f"[bold]{content.title}[/bold]")
    console.print(f"folders: {content.folder_ids}")
    console.print(f"keywords: {', '.join(content.keywords[:10])}")
    console.print(f"transcript segments: {len(content.transcript)}")


@safe_command()
def transcript(file_id: str) -> None:
    """Print transcript text."""
    cfg = load_config()
    with PlaudClient(cfg) as client:
        content = client.file_content(file_id)
    console.print(content.transcript_text())


@safe_command()
def summary(file_id: str) -> None:
    """Print AI summary (auto_sum_note)."""
    cfg = load_config()
    with PlaudClient(cfg) as client:
        content = client.file_content(file_id)
    if content.summary_md:
        console.print(Markdown(content.summary_md))
    else:
        console.print("[yellow]no summary[/yellow]")


@safe_command()
def outline(file_id: str) -> None:
    """Print topic outline."""
    cfg = load_config()
    with PlaudClient(cfg) as client:
        content = client.file_content(file_id)
    console.print(content.outline_text())


@safe_command()
def download(file_id: str, output_dir: Path = Path("downloads"), force: bool = False) -> None:
    """Download audio for one file."""
    cfg = load_config()
    storage = Storage()
    with PlaudClient(cfg) as client:
        path = client.download(file_id, output_dir, force=force)
    storage.mark_downloaded(file_id, path, now=int(time.time()))
    console.print(f"[green]ok[/green] {path}")


@safe_command()
def status(
    json_out: bool = typer.Option(False, "--json"),
    stage: str = typer.Option(
        None, "--stage", help="list files in one stage (new/cached/transcribed/integrated)"
    ),
    limit: int = typer.Option(20, "--limit", help="max files listed with --stage"),
) -> None:
    """Library progress — derived from cached/transcribed/integrated artifacts.

    Stages: new (metadata only) → cached (Plaud detail cached) → transcribed
    (CMDS STT exists) → integrated (integrated output on disk). Derived live
    from artifacts, so it cannot go stale like the old files.status column.
    """
    from core.progress import STAGES, derive_progress

    if stage is not None and stage not in STAGES:
        raise typer.BadParameter(f"stage must be one of: {', '.join(STAGES)}")

    storage = Storage()
    prog = derive_progress(storage)
    total = len(prog.stages)

    def stage_files(name: str) -> list[str]:
        ids = [fid for fid, s in prog.stages.items() if s == name]
        rows = [storage.get_file_row(fid) for fid in ids]
        rows = [r for r in rows if r is not None]
        rows.sort(key=lambda r: r["edit_time"] or 0, reverse=True)
        return [r["id"] for r in rows]

    if json_out:
        payload: dict = {"counts": prog.counts, "total": total}
        if stage:
            payload["stage"] = stage
            payload["files"] = stage_files(stage)[:limit]
        _emit_json(payload)
        return

    console.print("[bold]Library progress[/bold] (derived)")
    width = 24
    for s in reversed(STAGES):
        n = prog.counts.get(s, 0)
        bar = "█" * (round(width * n / total) if total else 0)
        console.print(f"  {s:>11}: {n:>5}  [dim]{bar}[/dim]")
    console.print(f"  {'total':>11}: {total:>5}")
    if stage:
        ids = stage_files(stage)
        console.print(f"\n[bold]{stage}[/bold] ({len(ids)} files, showing {min(limit, len(ids))})")
        for fid in ids[:limit]:
            row = storage.get_file_row(fid)
            console.print(f"  {fid}  {(row['filename'] or '') if row else ''}")


_AUTH_ICON = {
    "valid": "[green]✅ valid[/green]",
    "expiring": "[yellow]⚠️ expiring soon[/yellow]",
    "expired": "[red]❌ expired[/red]",
    "unconfigured": "[red]❌ not configured[/red]",
    "unknown": "[yellow]? unknown[/yellow]",
}


@safe_command(name="auth")
def auth_cmd(
    json_out: bool = typer.Option(False, "--json"),
    live: bool = typer.Option(False, "--live", help="also ping the API to confirm the token works"),
) -> None:
    """Plaud credential status — token validity + expiry countdown."""
    from dataclasses import asdict

    from core.auth_status import auth_status as get_auth

    st = get_auth(live=live)
    if json_out:
        _emit_json(asdict(st))
        return
    console.print(f"Plaud auth: {_AUTH_ICON.get(st.state, st.state)}")
    if st.detail:
        console.print(f"  {st.detail}")
    if st.workspace_id:
        console.print(f"  workspace: {st.workspace_id}   member: {st.member_id}   role: {st.role}")
    if st.expires_at:
        exp = datetime.fromtimestamp(st.expires_at).strftime("%Y-%m-%d %H:%M")
        left = "expired" if st.remaining_human == "expired" else f"{st.remaining_human} left"
        console.print(f"  expires:   {exp}  ({left})")
    if st.live_state is not None:
        live_label = {
            "ok": "[green]reachable[/green]",
            "rejected": "[red]rejected[/red]",
            "unreachable": "[yellow]could not reach Plaud (network)[/yellow]",
        }.get(st.live_state, st.live_state)
        console.print(f"  live ping: {live_label}")
    if st.auto_refresh:
        auto_label = {
            "ready": "[green]on[/green] — token renews headlessly",
            "expiring": "[yellow]on — refresh token expiring soon, re-bootstrap recommended[/yellow]",
            "expired": "[red]off — refresh token expired, re-bootstrap needed[/red]",
            "not_bootstrapped": "[dim]off — not bootstrapped[/dim]",
        }.get(st.auto_refresh, st.auto_refresh)
        console.print(f"  auto-refresh: {auto_label}")
        if st.refresh_expires_at:
            rexp = datetime.fromtimestamp(st.refresh_expires_at).strftime("%Y-%m-%d %H:%M")
            console.print(f"  refresh token expires: {rexp}")
    if st.state in ("expired", "expiring", "unconfigured"):
        if st.auto_refresh == "ready":
            console.print(
                "  [dim]headless refresh is armed — any command renews it, "
                "or force one: uv run plaud ws-refresh[/dim]"
            )
        else:
            console.print(
                "  [dim]refresh: use the app Auth button > Authenticate with Plaud. "
                "Advanced fallback: uv run plaud refresh-auth[/dim]"
            )
    if st.auto_refresh == "not_bootstrapped":
        console.print(
            "  [dim]enable headless renewal (one-time): log in once via the app's "
            "Embedded Web Login, or run: uv run plaud ws-bootstrap[/dim]"
        )


@safe_command(name="disconnect")
def disconnect_cmd(
    json_out: bool = typer.Option(False, "--json", help="Emit a machine-readable result."),
) -> None:
    """Remove Community Plaud credentials while preserving all local recordings."""
    if not COMMUNITY_EDITION:
        console.print("[red]disconnect is available only in the Community edition[/red]")
        raise typer.Exit(2)

    from core.config import resolve_env_path
    from core.secret_store import CredentialStoreError, disconnect_community_credentials

    try:
        disconnect_community_credentials(resolve_env_path())
    except CredentialStoreError as exc:
        if json_out:
            _emit_json({"status": "error", "detail": str(exc)})
        else:
            console.print(f"[red]disconnect failed[/red] — {exc}")
        raise typer.Exit(1) from None

    result = {
        "status": "ok",
        "credentials_removed": True,
        "local_data_preserved": True,
    }
    if json_out:
        _emit_json(result)
        return
    console.print("[green]disconnected[/green] — Community credentials removed")
    console.print("  [dim]recordings, transcripts, and the local database were preserved[/dim]")


@safe_command(name="refresh-auth")
def refresh_auth_cmd(
    json_out: bool = typer.Option(
        False,
        "--json",
        help="Emit JSON and always exit 0; callers must check the status field.",
    ),
    stdin: bool = typer.Option(
        False,
        "--stdin",
        help="Read the Plaud cURL from stdin instead of the macOS clipboard.",
    ),
    validate_live: bool = typer.Option(
        False,
        "--validate-live",
        help="Verify recording-list access before replacing protected credentials.",
    ),
) -> None:
    """Refresh OS-protected credentials from a fresh Plaud API cURL."""
    from core.refresh_auth import refresh_auth

    curl_text = sys.stdin.read() if stdin else None
    result = refresh_auth(curl_text=curl_text, validate_live=validate_live)
    if json_out:
        _emit_json(
            {
                "status": result.status,
                "detail": result.detail,
                "cookie_captured": result.cookie_captured,
                "auto_refresh_armed": result.auto_refresh_armed,
                "auto_refresh_detail": result.auto_refresh_detail,
            }
        )
        return
    if result.status == "ok":
        cookie = "yes" if result.cookie_captured else "no"
        console.print("[green]✅ credentials refreshed from copied cURL[/green]")
        console.print(f"  cookie captured: {cookie}")
        if result.auto_refresh_armed:
            console.print("  [green]automatic renewal: armed[/green]")
        else:
            console.print(
                "  [yellow]automatic renewal: not armed[/yellow] — "
                "the copied cURL stores current access only"
            )
        from core.auth_status import auth_status as get_auth

        st = get_auth()
        if st.expires_at:
            exp = datetime.fromtimestamp(st.expires_at).strftime("%Y-%m-%d %H:%M")
            console.print(f"  valid until {exp}")
    elif result.status == "clipboard_empty":
        console.print(f"[yellow]clipboard empty[/yellow] — {result.detail}")
        raise typer.Exit(2)
    elif result.status == "pbpaste_missing":
        console.print(f"[red]pasteboard unavailable[/red] — {result.detail}")
        raise typer.Exit(3)
    else:
        console.print(f"[red]refresh failed[/red] ({result.status}) — {result.detail}")
        raise typer.Exit(1)


@safe_command(name="web-auth")
def web_auth_cmd(
    json_out: bool = typer.Option(
        False,
        "--json",
        help="Emit JSON and always exit 0; callers must check the status field.",
    ),
    stdin: bool = typer.Option(
        False,
        "--stdin",
        help="Read the structured Plaud Web Login capture JSON from stdin.",
    ),
    skip_live: bool = typer.Option(
        False,
        "--skip-live",
        help="Write captured credentials without the live API validation step "
        "(validation runs by default).",
    ),
) -> None:
    """Import a Plaud Web Login capture into macOS Keychain (app bridge)."""
    from core.web_auth import import_web_auth

    if not stdin:
        result = {
            "status": "stdin_required",
            "detail": "send Plaud Web Login capture JSON on stdin",
            "cookie_captured": False,
            "auto_refresh_armed": False,
        }
    else:
        raw = sys.stdin.read()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            result = {
                "status": "invalid_payload",
                "detail": f"invalid JSON: {exc.msg}",
                "cookie_captured": False,
                "auto_refresh_armed": False,
            }
        else:
            imported = import_web_auth(payload, validate_live=not skip_live)
            result = {
                "status": imported.status,
                "detail": imported.detail,
                "cookie_captured": imported.cookie_captured,
                "auto_refresh_armed": imported.auto_refresh_armed,
            }

    if json_out:
        _emit_json(result)
        return
    if result["status"] == "ok":
        console.print("[green]credentials refreshed from Plaud Web Login[/green]")
        return
    if result["status"] == "live_check_unavailable":
        # Credentials were saved — only the live verification could not run.
        console.print(f"[yellow]credentials saved but unverified[/yellow] — {result['detail']}")
        return
    console.print(f"[red]web auth failed[/red] ({result['status']}) — {result['detail']}")
    raise typer.Exit(1)


_WS_BOOTSTRAP_SNIPPET = (
    "copy(localStorage.getItem(Object.keys(localStorage).find(k => "
    'k.startsWith("pld_") && k.endsWith(":workspaceList"))))'
)


def _print_ws_outcome(outcome: Any) -> None:
    """Shared pretty-printer for ws-refresh / ws-bootstrap outcomes."""
    if outcome.status in ("ok", "fresh"):
        icon = "✅ refreshed" if outcome.status == "ok" else "✅ already fresh"
        console.print(f"[green]{icon}[/green] — {outcome.detail}")
        if outcome.access_expires_at:
            exp = datetime.fromtimestamp(outcome.access_expires_at).strftime("%Y-%m-%d %H:%M")
            console.print(f"  token valid until {exp}")
        if outcome.refresh_expires_at:
            rexp = datetime.fromtimestamp(outcome.refresh_expires_at).strftime("%Y-%m-%d %H:%M")
            note = (
                "  [yellow](expiring soon — re-bootstrap recommended)[/yellow]"
                if outcome.refresh_expiring_soon
                else ""
            )
            console.print(f"  headless refresh armed until {rexp}{note}")
        return
    color = "yellow" if outcome.status in ("not_bootstrapped", "unreachable") else "red"
    console.print(f"[{color}]{outcome.status}[/{color}] — {outcome.detail}")


@safe_command(name="ws-refresh")
def ws_refresh_cmd(
    json_out: bool = typer.Option(
        False,
        "--json",
        help="Emit JSON and always exit 0; callers must check the status field.",
    ),
    only_if_needed: bool = typer.Option(
        False,
        "--only-if-needed",
        help="Skip the network call while the current token still has >6h left.",
    ),
) -> None:
    """Mint a fresh 24h Plaud token headlessly — no browser, no cURL."""
    from core.ws_refresh import refresh_workspace_token

    outcome = refresh_workspace_token(only_if_needed=only_if_needed)
    if json_out:
        _emit_json(asdict(outcome))
        return
    _print_ws_outcome(outcome)
    if outcome.status in ("ok", "fresh"):
        return
    raise typer.Exit(2 if outcome.status == "not_bootstrapped" else 1)


@safe_command(name="ws-bootstrap")
def ws_bootstrap_cmd(
    json_out: bool = typer.Option(
        False,
        "--json",
        help="Emit JSON and always exit 0; callers must check the status field.",
    ),
    stdin: bool = typer.Option(
        False,
        "--stdin",
        help="Read the workspaceList JSON from stdin instead of the macOS clipboard.",
    ),
) -> None:
    """One-time arm of headless token refresh from web.plaud.ai's workspaceList.

    In the web.plaud.ai devtools Console run
    copy the current `pld_<account>:workspaceList` value, then run this command.
    It stores the workspace refresh token in Keychain and validates it with one refresh.
    (App users don't need this: the Embedded Web Login bootstraps automatically.)
    """
    from core.refresh_auth import _read_pasteboard
    from core.ws_refresh import bootstrap_workspace

    if stdin:
        text = sys.stdin.read()
    else:
        try:
            text = _read_pasteboard()
        except RuntimeError as exc:
            text = ""
            if not json_out:
                console.print(f"[red]could not read clipboard[/red] — {exc}")

    if not text.strip():
        if json_out:
            _emit_json({"status": "invalid_payload", "detail": "no workspaceList JSON provided"})
            return
        console.print(
            "[yellow]nothing to import[/yellow] — in the web.plaud.ai devtools Console run:"
        )
        console.print(f"  [bold]{_WS_BOOTSTRAP_SNIPPET}[/bold]")
        console.print("then re-run this command (the JSON lands on your clipboard).")
        raise typer.Exit(2)

    outcome = bootstrap_workspace(text)
    if json_out:
        _emit_json(asdict(outcome))
        return
    _print_ws_outcome(outcome)
    if outcome.status != "ok":
        raise typer.Exit(1)


def _render_dashboard_md(st: Any, counts: dict[str, Any], now: int) -> str:
    gen = datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M")
    icon = {
        "valid": "✅ valid",
        "expiring": "⚠️ expiring soon",
        "expired": "❌ expired",
        "unconfigured": "❌ not configured",
        "unknown": "? unknown",
    }.get(st.state, st.state)
    exp_line = ""
    fm_exp = ""
    if st.expires_at:
        exp = datetime.fromtimestamp(st.expires_at).strftime("%Y-%m-%d %H:%M")
        exp_line = f"- **Expires**: {exp}  ({st.remaining_human} left)\n"
        fm_exp = datetime.fromtimestamp(st.expires_at).isoformat(timespec="minutes")
    live_line = ""
    if st.live_state is not None:
        live_label = {
            "ok": "reachable ✅",
            "rejected": "rejected ❌ — re-onboard",
            "unreachable": "could not reach Plaud (network) ⚠️",
        }.get(st.live_state, st.live_state)
        live_line = f"- **Live ping**: {live_label}\n"
    usage = counts.get("usage_status", {})
    usage_rows = "\n".join(f"| {k} | {v} |" for k, v in sorted(usage.items())) or "| (none) | 0 |"
    callout = (
        "> [!warning] 토큰 만료/임박 — 앱 Auth 버튼 → `Authenticate with Plaud`로 재인증"
        if st.state in ("expired", "expiring", "unconfigured")
        else "> [!tip] 토큰 정상"
    )
    self_name = app_config.author()
    author_block = f'author:\n  - "[[{self_name}]]"\n' if self_name else "author:\n"
    return f"""---
type: dashboard
aliases:
  - Plaud Status
  - Plaud 인증 상태
description: "Plaud Cloud auth health + library metrics. Auto-generated by `plaud dashboard --vault`; authState/expiresAt/libraryCount are Dataview-queryable."
{author_block}date created: {gen}
date modified: {gen}
tags:
  - plaud
  - dashboard
  - auth
  - system
project: "[[Plaud Note Manager]]"
authState: {st.state}
expiresAt: {fm_exp}
libraryCount: {counts.get("total", 0)}
generatedAt: {gen}
status: completed
---

# 🔌 Plaud 상태

> 자동 생성: {gen} · `uv run plaud dashboard --vault` 로 갱신

## 인증 (Auth)

- **상태**: {icon}
{exp_line}- **Workspace**: {st.workspace_id or "—"}  ·  **Member**: {st.member_id or "—"}  ·  **Role**: {st.role or "—"}
{live_line}
{callout}

## 라이브러리 (Library)

- **전체 녹음**: {counts.get("total", 0)}  ·  **휴지통**: {counts.get("trash", 0)}  ·  **미분류(Unfiled)**: {counts.get("unfiled", 0)}
- **폴더**: {counts.get("folders", 0)}  ·  **콘텐츠 캐시됨**: {counts.get("cached", 0)}

### usage_status 분포

| status | count |
|---|---|
{usage_rows}

---
관련: [[Plaud Note Manager]] (프로젝트 MOC) · 채널 매뉴얼은 같은 폴더 `Manuals/`
"""


@safe_command(name="dashboard")
def dashboard_cmd(
    vault: bool = typer.Option(
        False, "--vault", help="write the dashboard note into the Obsidian vault"
    ),
    out: str = typer.Option("", "--out", help="override output path"),
    live: bool = typer.Option(True, "--live/--no-live", help="ping the API for liveness"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Plaud status dashboard — auth health + library metrics; optionally write to the vault."""
    from dataclasses import asdict

    from core.auth_status import auth_status as get_auth

    now = int(time.time())
    st = get_auth(live=live, now=now)
    counts = Storage().counts()
    if json_out:
        _emit_json({"auth": asdict(st), "library": counts, "generated_at": now})
        return
    md = _render_dashboard_md(st, counts, now)
    if vault or out:
        if out:
            target = Path(out)
        else:
            vault_root = _require_vault(None)
            target = vault_root / "70. Outputs/74. Projects/Plaud Note Manager/🔌 Plaud Status.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(md, encoding="utf-8")
        console.print(f"[green]wrote[/green] {target}")
    else:
        console.print(md)


@safe_command(name="metadata")
def metadata_show(file_id: str) -> None:
    """Show local note metadata, tags, and vault references for one Plaud file."""
    import json as _json

    storage = Storage()
    row = storage.get_note_metadata(file_id)
    metadata = dict(row) if row else {"file_id": file_id}
    if metadata.get("metadata_json"):
        try:
            metadata["metadata"] = _json.loads(metadata.pop("metadata_json") or "{}")
        except Exception:
            pass
    payload = {
        "file_id": file_id,
        "metadata": metadata,
        "tags": [dict(r) for r in storage.list_note_tags(file_id)],
        "references": [dict(r) for r in storage.list_note_references(file_id)],
    }
    console.print_json(json=_json.dumps(payload, ensure_ascii=False))


@safe_command(name="usage-status")
def usage_status_set(
    file_id: str,
    usage_status: str = typer.Argument(
        ..., help="unused | metadata-ready | vault-linked | used-elsewhere | archived"
    ),
) -> None:
    """Set whether a recording has been used in Obsidian or another context."""
    allowed = {"unused", "metadata-ready", "vault-linked", "used-elsewhere", "archived"}
    if usage_status not in allowed:
        console.print(f"[red]usage_status must be one of: {', '.join(sorted(allowed))}[/red]")
        raise typer.Exit(1)
    Storage().update_usage_status(file_id, usage_status, now=int(time.time()))
    console.print(f"[green]ok[/green] {file_id} usage_status -> {usage_status}")


@safe_command(name="folder-plan")
def folder_plan() -> None:
    """Show the canonical Plaud recording folder taxonomy."""
    for rule in FOLDER_TAXONOMY:
        console.print(
            f"[bold]{rule.folder_name}[/bold]  [dim]{rule.note_type} · {rule.cmds_category}[/dim]"
        )


@safe_command(name="tags")
def tags_list(file_id: str) -> None:
    """List Obsidian-style local tags for one Plaud file."""
    storage = Storage()
    rows = storage.list_note_tags(file_id)
    if not rows:
        console.print("[yellow]no tags[/yellow]")
        return
    for row in rows:
        console.print(f"#{row['tag']}  [dim]{row['source']}[/dim]")


@safe_command(name="tag-add")
def tag_add(
    file_id: str,
    tags: list[str] = typer.Argument(..., help="One or more tags, no # needed."),
) -> None:
    """Add manual Obsidian-style tags. Spaces are normalized to hyphens."""
    storage = Storage()
    added = storage.add_note_tags(file_id, tags, source="manual", now=int(time.time()))
    if not added:
        console.print("[yellow]no valid tags[/yellow]")
        return
    console.print("[green]added[/green] " + ", ".join(f"#{tag}" for tag in added))


@safe_command(name="tags-all")
def tags_all(json_out: bool = typer.Option(False, "--json")) -> None:
    """List every tag with its file count, busiest first. Pinned tags marked."""
    storage = Storage()
    counts = storage.tag_counts()
    pinned = set(app_config.pinned_tags())
    if json_out:
        _emit_json(
            {
                "pinned": app_config.pinned_tags(),
                "tags": [{"tag": t, "count": n, "pinned": t in pinned} for t, n in counts],
            }
        )
        return
    if not counts:
        console.print("[yellow]no tags yet[/yellow]")
        return
    for tag, n in counts:
        mark = "📌 " if tag in pinned else "   "
        console.print(f"{mark}#{tag}  [dim]{n}[/dim]")


@safe_command(name="tag-pin")
def tag_pin(
    tag: str,
    off: bool = typer.Option(False, "--off", help="Unpin instead of pin."),
) -> None:
    """Pin (or --off to unpin) a tag to the top of the app's Tags sidebar."""
    normalized = normalize_tags([tag])
    if not normalized:
        raise typer.BadParameter(f"invalid tag: {tag}")
    clean = normalized[0]
    current = app_config.pinned_tags()
    if off:
        app_config.set_pinned_tags([t for t in current if t != clean])
        console.print(f"[green]unpinned[/green] #{clean}")
    elif clean not in current:
        app_config.set_pinned_tags([*current, clean])
        console.print(f"[green]pinned[/green] #{clean}")
    else:
        console.print(f"[dim]already pinned[/dim] #{clean}")


@safe_command(name="tag-remove")
def tag_remove(
    file_id: str,
    tags: list[str] = typer.Argument(..., help="One or more tags, no # needed."),
) -> None:
    """Remove local tags from one Plaud file."""
    storage = Storage()
    removed = storage.remove_note_tags(file_id, tags)
    if not removed:
        console.print("[yellow]no valid tags[/yellow]")
        return
    console.print("[green]removed[/green] " + ", ".join(f"#{tag}" for tag in removed))


@safe_command(name="dual-status")
def dual_status_cmd(json_out: bool = typer.Option(False, "--json")) -> None:
    """List recordings in the dual-transcribe pipeline and their stage."""
    rows = Storage().list_dual()
    if json_out:
        payload = [
            {
                "file_id": r["file_id"],
                "status": r["status"],
                "error": r["error"] or "",
                "vault_path": r["vault_path"] or "",
                "updated_at": r["updated_at"],
            }
            for r in rows
        ]
        _emit_json({"items": payload})
        return
    if not rows:
        console.print("[dim]no recordings in the dual pipeline[/dim]")
        return
    for r in rows:
        err = f"  [red]{r['error']}[/red]" if r["error"] else ""
        console.print(f"  {r['status']:>15}  {r['file_id']}{err}")


@safe_command(name="reuse")
def reuse_mark(
    file_id: str,
    channel: str = typer.Argument(
        "",
        help="newsletter | lecture | shorts | sns | consulting | research | other. Empty = list.",
    ),
    status: str = typer.Option("flagged", help="flagged | drafted | published"),
    note: str = typer.Option("", help="Free-text context (e.g. 게임 지식확장 사례로)."),
    clear: bool = typer.Option(False, "--clear", help="Remove the mark for this channel."),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Mark a recording as material for an output channel (content reuse check)."""
    import json as _json

    from core import reuse as reuse_mod

    storage = Storage()
    if not channel:
        summary = reuse_mod.reuse_summary(storage, file_id)
        if json_output:
            console.print_json(json=_json.dumps(summary, ensure_ascii=False))
            return
        if not summary["targets"]:
            console.print("[dim]no reuse marks[/dim]")
            return
        for t in summary["targets"]:
            note_part = f"  — {t['note']}" if t["note"] else ""
            console.print(f"  {t['channel']:>10}: {t['status']}{note_part}")
        return

    try:
        channel = reuse_mod.validate_channel(channel)
        status = reuse_mod.validate_status(status)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    if clear:
        removed = storage.clear_reuse(file_id, channel)
        console.print(
            f"[green]cleared[/green] {channel} ({removed})"
            if removed
            else f"[yellow]no mark for {channel}[/yellow]"
        )
        return
    storage.set_reuse(file_id, channel, status=status, note=note or None, now=int(time.time()))
    note_part = f" — {note}" if note else ""
    console.print(f"[green]marked[/green] {channel}: {status}{note_part}")


@safe_command(name="reuse-query")
def reuse_query(
    channel: str = typer.Argument("", help="Filter by channel. Empty = all marks."),
    status: str = typer.Option("", help="Filter: flagged | drafted | published."),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """What material do I have for a channel? (e.g. plaud reuse-query newsletter)"""
    import json as _json

    from core import reuse as reuse_mod

    if channel:
        channel = reuse_mod.validate_channel(channel)
    if status:
        status = reuse_mod.validate_status(status)
    rows = Storage().query_reuse(channel=channel or None, status=status or None)
    if json_output:
        payload = [
            {
                "file_id": r["file_id"],
                "filename": r["filename"],
                "channel": r["channel"],
                "status": r["status"],
                "note": r["note"] or "",
                "updated_at": r["updated_at"],
            }
            for r in rows
        ]
        console.print_json(json=_json.dumps(payload, ensure_ascii=False))
        return
    if not rows:
        console.print("[dim]no reuse marks[/dim]")
        return
    table = Table(box=None)
    table.add_column("channel")
    table.add_column("status")
    table.add_column("recording")
    table.add_column("note")
    for r in rows:
        table.add_row(
            r["channel"], r["status"], (r["filename"] or r["file_id"])[:48], r["note"] or ""
        )
    console.print(table)


@safe_command(name="search")
def search_cmd(
    query: str = typer.Argument(..., help="Free text — matches title, transcript, summary."),
    limit: int = typer.Option(50, "--limit"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Full-content search across cached recordings (FTS5, Korean substring OK)."""
    storage = Storage()
    hits = storage.search_recordings(query, limit=limit)
    if json_out:
        out = []
        for h in hits:
            row = storage.get_file_row(h["file_id"])
            out.append(
                {
                    "file_id": h["file_id"],
                    "filename": (row["filename"] if row else None),
                    "snippet": h["snippet"],
                }
            )
        _emit_json({"query": query, "count": len(out), "hits": out})
        return
    if not hits:
        console.print(f"[yellow]no matches[/yellow] for {query!r}")
        return
    for h in hits:
        row = storage.get_file_row(h["file_id"])
        name = (row["filename"] if row else None) or h["file_id"]
        console.print(f"[bold]{name}[/bold]  [dim]{h['file_id']}[/dim]")
        if h["snippet"]:
            console.print(f"  {h['snippet']}")


@safe_command(name="search-reindex")
def search_reindex() -> None:
    """Rebuild the full-content search index from cached recordings."""
    n = Storage().rebuild_search_index()
    console.print(f"[green]indexed {n}[/green] recordings for search")


@safe_command(name="prune-empty-cache")
def prune_empty_cache(
    refetch: bool = typer.Option(
        False, "--refetch", help="Immediately re-fetch each cleared file from Plaud."
    ),
) -> None:
    """Clear stale empty caches (recordings stored before Plaud finished
    processing) so they show as uncached and re-fetch with real content."""
    storage = Storage()
    ids = storage.delete_empty_content()
    if not ids:
        console.print("[green]ok[/green] no empty caches to prune")
        return
    console.print(f"[green]pruned {len(ids)}[/green] empty caches")
    if not refetch:
        console.print("  re-fetch via the app Backfill button, by clicking each, or --refetch")
        return
    cfg = load_config()
    ok = 0
    with PlaudClient(cfg) as client:
        for fid in ids:
            try:
                content = client.file_content(fid)
                storage.save_content(content, now=int(time.time()))
                if not content.is_empty:
                    ok += 1
            except Exception as exc:  # noqa: BLE001 — report, keep going
                console.print(f"[yellow]skip[/yellow] {fid}: {exc}")
    console.print(f"[green]re-fetched {ok}[/green] / {len(ids)} (still-empty ones stay uncached)")


@safe_command(name="classify-undo")
def classify_undo(
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Restore the exact folders saved before the most recent auto-folder apply."""
    from core.community_router import UndoManifestError, undo_saved_manifest
    from core.paths import DATA_DIR

    manifest_path = DATA_DIR / "last_classify.json"
    storage = Storage()
    try:
        with PlaudClient(load_config()) as client:
            report = undo_saved_manifest(storage, client, manifest_path)
    except UndoManifestError as exc:
        msg = f"{exc}; no additional Cloud changes were made"
        if json_out:
            _emit_json({"status": "error", "detail": msg, "reverted": 0})
        else:
            console.print(f"[red]{msg}[/red]")
        raise typer.Exit(1)
    except Exception:  # noqa: BLE001 - redact provider diagnostics
        msg = "could not verify Plaud folders; no additional Cloud changes were made"
        if json_out:
            _emit_json({"status": "error", "detail": msg, "reverted": 0})
        else:
            console.print(f"[red]{msg}[/red]")
        raise typer.Exit(1)

    payload = report.public_dict()
    if json_out:
        _emit_json(payload)
        return
    if payload["status"] == "nothing":
        console.print(f"[yellow]{payload['detail']}[/yellow]")
        return
    if payload["status"] == "apply_recovery_required":
        console.print(f"[yellow]{payload['detail']}[/yellow]")
        return
    console.print(
        f"[green]restored {report.reverted_count}[/green] file(s) to their previous folder"
    )
    for failure in report.failures:
        console.print(f"[yellow]skip[/yellow] {failure['file_id']}: {failure['error']}")


@safe_command(name="classify-undo-status")
def classify_undo_status_command(
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Read local folder undo/recovery availability without network access."""
    from core.community_router import UndoManifestError, classify_undo_status
    from core.paths import DATA_DIR

    try:
        status = classify_undo_status(DATA_DIR / "last_classify.json")
    except UndoManifestError:
        payload = {
            "status": "error",
            "count": 0,
            "detail": "folder undo artifacts are invalid or inconsistent",
        }
        if json_out:
            _emit_json(payload)
        else:
            console.print(f"[red]{payload['detail']}[/red]")
        raise typer.Exit(1)

    payload = status.public_dict()
    if json_out:
        _emit_json(payload)
        return
    console.print(f"{payload['status']}: {payload['count']} · {payload['detail']}")


def move_to_named_folder(storage: Storage, file_id: str, folder_name: str) -> str:
    """Ensure a Plaud folder exists, then assign a file to it both remotely and locally."""
    cfg = load_config()
    now = int(time.time())
    with PlaudClient(cfg) as client:
        folder = storage.folder_by_name(folder_name)
        if folder:
            folder_id = folder["id"]
        else:
            created = client.create_folder(folder_name)
            folder_id = created.id
            storage.replace_folders(client.list_folders(), now=now)
        client.set_file_folders(file_id, [folder_id])
    storage.set_file_folders(file_id, [folder_id])
    return folder_id


@safe_command()
def export(
    file_id: str,
    kind: str = typer.Argument(..., help="transcript|summary|outline|notes"),
    out: Path | None = None,
) -> None:
    """Export a content block to a file (or stdout)."""
    cfg = load_config()
    with PlaudClient(cfg) as client:
        content = client.file_content(file_id)

    if kind == "transcript":
        text = content.transcript_text()
    elif kind == "summary":
        text = content.summary_md or ""
    elif kind == "outline":
        text = content.outline_text()
    elif kind == "notes":
        text = "\n\n---\n\n".join(filter(None, [content.summary_md, *content.summary_extra_md]))
    else:
        raise typer.BadParameter(f"unknown kind: {kind}")

    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        console.print(f"[green]wrote[/green] {out}")
    else:
        console.print(text)


@safe_command()
def obsidian(
    file_id: str,
    vault: Path | None = None,
    folder: str = typer.Option("00. Inbox", help="Subfolder inside vault"),
    dry_run: bool = False,
) -> None:
    """Send a Plaud file to Claude Code with a prompt that imports it into Obsidian.

    Builds a structured prompt containing transcript + summary, then launches
    `claude` in a new Terminal window so the user can watch the assistant
    file the note into the vault using existing skills (obsidian-markdown,
    cmds-format, etc.).
    """
    vault_path = _require_vault(vault)
    cfg = load_config()
    with PlaudClient(cfg) as client:
        content = client.file_content(file_id)

    prompt = build_obsidian_prompt(content=content, vault=vault_path, folder=folder)

    if dry_run:
        console.print(prompt)
        return

    launch_claude(prompt, cwd=vault_path)
    console.print(f"[green]launched Claude Code[/green] for {file_id}")


@safe_command(name="audio-url")
def audio_url(file_id: str) -> None:
    """Print the (short-lived) signed URL for streaming a file's audio."""
    cfg = load_config()
    with PlaudClient(cfg) as client:
        # temp_url() is mp3-first (AVPlayer-compatible) and raises if absent.
        url = client.temp_url(file_id)
    console.print(url)


@safe_command()
def web(
    file_id: str,
    open_browser: bool = typer.Option(False, "--open", "-o", help="Open in default browser"),
    copy: bool = typer.Option(False, "--copy", "-c", help="Copy URL to clipboard (macOS)"),
) -> None:
    """Print (and optionally open / copy) the web.plaud.ai URL for a session."""
    url = f"https://web.plaud.ai/file/{file_id}"
    console.print(url)
    if open_browser:
        subprocess.run(["open", url], check=False)
    if copy:
        subprocess.run(["pbcopy"], input=url.encode(), check=False)


@safe_command(name="provider-key-set")
def provider_key_set(provider: str) -> None:
    """Store a provider API key read only from stdin (never an argv value)."""
    from core.provider_secrets import set_api_key
    from core.secret_store import CredentialStoreError

    if sys.stdin.isatty():
        console.print(
            "[red]API key required on stdin[/red] — pipe it to this command; "
            "the key must never be a command-line argument."
        )
        raise typer.Exit(2)
    raw = sys.stdin.read(4098)
    if len(raw) > 4097:
        console.print("[red]API key input is too long[/red]")
        raise typer.Exit(2)
    try:
        set_api_key(provider, raw)
    except (ValueError, CredentialStoreError) as exc:
        console.print(f"[red]Could not store API key[/red]: {exc}")
        raise typer.Exit(1) from None
    console.print(
        f"[green]{provider.lower()} API key stored[/green] in OS-protected storage; "
        "no key fragment was printed."
    )


@safe_command(name="provider-key-status")
def provider_key_status(
    provider: str,
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Show set/unset only; never reveal a masked key or fingerprint."""
    from core.provider_secrets import api_key_status
    from core.secret_store import CredentialStoreError

    try:
        status_result = api_key_status(provider)
    except (ValueError, CredentialStoreError) as exc:
        console.print(f"[red]Could not read API key status[/red]: {exc}")
        raise typer.Exit(1) from None
    if json_out:
        _emit_json(status_result)
        return
    console.print(f"{status_result['provider']}: [bold]{status_result['status']}[/bold]")


@safe_command(name="provider-key-delete")
def provider_key_delete(provider: str) -> None:
    """Remove one provider key without touching Plaud or other providers."""
    from core.provider_secrets import delete_api_key
    from core.secret_store import CredentialStoreError

    try:
        existed = delete_api_key(provider)
    except (ValueError, CredentialStoreError) as exc:
        console.print(f"[red]Could not remove API key[/red]: {exc}")
        raise typer.Exit(1) from None
    state = "removed" if existed else "already unset"
    console.print(f"[green]{provider.lower()} API key {state}[/green]")


@safe_command(name="elevenlabs-transcribe")
def elevenlabs_transcribe(
    file_id: str,
    confirm_upload: bool = typer.Option(
        False,
        "--confirm-upload",
        help="Required: confirm audio leaves this device and provider usage may incur cost.",
    ),
    diarize: bool = typer.Option(True, help="Ask ElevenLabs to identify speakers."),
    language: str = typer.Option("", help="Optional ISO-639-1/3 hint, e.g. ko or kor."),
    num_speakers: int = typer.Option(0, help="Expected speakers: 1-32; 0 lets Scribe decide."),
    force: bool = typer.Option(
        False,
        help=(
            "Re-upload when a local transcript exists or a prior upload outcome is unknown; "
            "this can bill again."
        ),
    ),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Upload one recording to ElevenLabs Scribe v2 and save its transcript locally."""
    from core.secret_store import CredentialStoreError
    from core.transcribe import (
        TranscriptionError,
        transcription_retry_outcome_unknown,
        transcribe_and_store,
    )

    if not confirm_upload:
        console.print(
            "[red]Upload confirmation required[/red] — this sends the recording audio to "
            "ElevenLabs and may consume paid credits. Re-run with --confirm-upload."
        )
        raise typer.Exit(2)

    storage = Storage()
    try:
        retry_outcome_unknown = transcription_retry_outcome_unknown(Path(storage._db_path), file_id)
    except (AttributeError, TypeError, ValueError):
        console.print("[red]Could not verify the local ElevenLabs retry state.[/red]")
        raise typer.Exit(1) from None
    if retry_outcome_unknown and not force:
        console.print(
            "[yellow]A prior ElevenLabs upload outcome is unknown.[/yellow] "
            "Use --force only after confirming a retry may bill twice."
        )
        raise typer.Exit(2)
    if storage.get_cmds_transcript(file_id) is not None and not force:
        console.print(
            "[yellow]A local external transcript already exists.[/yellow] "
            "Use --force only if you intend to upload and pay again."
        )
        raise typer.Exit(2)

    if not json_out:
        console.print(
            "[yellow]Uploading audio to ElevenLabs Scribe v2; provider processing and "
            "account charges may apply.[/yellow]"
        )
    try:
        result = transcribe_and_store(
            load_config(),
            file_id,
            storage=storage,
            confirm_upload=True,
            force=force,
            diarize=diarize,
            model_id="scribe_v2",
            language_code=language or None,
            num_speakers=num_speakers or None,
        )
    except (ValueError, CredentialStoreError, TranscriptionError) as exc:
        console.print(f"[red]ElevenLabs transcription failed[/red]: {exc}")
        raise typer.Exit(1) from None

    summary = {
        "file_id": result["file_id"],
        "provider": result["provider"],
        "model": result["model"],
        "language": result.get("language"),
        "segments": len(result["segments"]),
        "audio_bytes_uploaded": result["audio_bytes_uploaded"],
        "stored_locally": True,
    }
    if json_out:
        _emit_json(summary)
        return
    console.print(
        f"[green]saved locally[/green] {summary['segments']} segments · "
        f"language={summary['language'] or 'auto'}"
    )


@safe_command(name="elevenlabs-attempt-status")
def elevenlabs_attempt_status(
    file_id: str,
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Read the local paid-upload retry marker without network access."""
    from core.transcribe import transcription_retry_outcome_unknown

    storage = Storage()
    try:
        outcome_unknown = transcription_retry_outcome_unknown(Path(storage._db_path), file_id)
    except (AttributeError, TypeError, ValueError):
        payload = {
            "status": "error",
            "retry_may_bill_twice": True,
            "detail": "could not verify the local ElevenLabs retry state",
        }
        if json_out:
            _emit_json(payload)
        else:
            console.print(f"[red]{payload['detail']}[/red]")
        raise typer.Exit(1) from None

    payload = {
        "status": "outcome_unknown" if outcome_unknown else "clear",
        "retry_may_bill_twice": outcome_unknown,
    }
    if json_out:
        _emit_json(payload)
        return
    console.print(payload["status"])


@safe_command(name="cmds-relabel")
def cmds_relabel(
    file_id: str,
    mappings: list[str] = typer.Argument(
        ...,
        help="Pairs like speaker_0=Alice speaker_1=Bob",
    ),
    start: float = typer.Option(0.0, help="Only relabel from this second onward"),
    end: float = typer.Option(0.0, help="Only relabel up to this second (0 = end)"),
) -> None:
    """Rewrite speaker labels in an existing CMDS transcript.

    Range mode: pass --start / --end (in seconds) to only relabel segments
    inside that window. Useful when a single recording contains multiple
    conversations — each gets its own mapping.
    """
    import json as _json

    storage = Storage()
    row = storage.get_cmds_transcript(file_id)
    if not row:
        console.print(f"[red]no CMDS transcript[/red] for {file_id}")
        raise typer.Exit(1)
    name_map = {}
    for m in mappings:
        if "=" not in m:
            raise typer.BadParameter(f"expected key=value, got {m}")
        k, v = m.split("=", 1)
        name_map[k.strip()] = v.strip()
    start_ms = int(start * 1000)
    end_ms = int(end * 1000) if end > 0 else None
    segs = _json.loads(row["segments"] or "[]")
    touched = 0
    for s in segs:
        sm = int(s.get("start_ms") or 0)
        if sm < start_ms:
            continue
        if end_ms is not None and sm > end_ms:
            continue
        if s.get("speaker") in name_map:
            s["speaker"] = name_map[s["speaker"]]
            touched += 1
    storage.update_cmds_segments(
        file_id,
        row["model"],
        _json.dumps(segs, ensure_ascii=False),
        now=int(time.time()),
    )
    console.print(
        f"[green]relabeled[/green] {touched}/{len(segs)} segments "
        f"in window [{start:.1f}s, {(end if end > 0 else float('inf')):.1f}s] "
        f"with {name_map}"
    )


@safe_command(name="speakers")
def speakers_list() -> None:
    """List saved speakers."""
    storage = Storage()
    for s in storage.list_speakers():
        marker = " (self)" if s["is_self"] else ""
        console.print(f"{s['id']:>3}  {s['name']}{marker}")


@safe_command(name="speaker-add")
def speaker_add(
    name: str, is_self: bool = typer.Option(False, "--self", help="Mark this speaker as you")
) -> None:
    """Add a speaker to the saved list."""
    storage = Storage()
    sid = storage.add_speaker(name=name, is_self=is_self, now=int(time.time()))
    console.print(f"[green]added[/green] {sid}  {name}{' (self)' if is_self else ''}")


@safe_command(name="speaker-delete")
def speaker_delete(speaker_id: int) -> None:
    storage = Storage()
    storage.delete_speaker(speaker_id)
    console.print(f"[green]deleted[/green] {speaker_id}")


# ---------- Plaud server-side edits (web parity, captured 2026-05-31) ----------


@safe_command(name="server-speakers")
def server_speakers(json_out: bool = typer.Option(False, "--json")) -> None:
    """List the Plaud SERVER speaker roster (voiceprint profiles) via /speaker/list."""
    cfg = load_config()
    with PlaudClient(cfg) as client:
        speakers = client.list_server_speakers()
    if json_out:
        _emit_json(speakers)
        return
    if not speakers:
        console.print("[yellow]no server speakers (or unrecognized response shape)[/yellow]")
        return
    for s in speakers:
        name = s.get("speaker_name") or s.get("name") or "(unnamed)"
        sid = s.get("speaker_id") or s.get("id") or "?"
        console.print(f"{name}  [dim]{sid}[/dim]")


@safe_command(name="speaker-rename-server")
def speaker_rename_server(old_name: str, new_name: str) -> None:
    """Rename a Plaud SERVER speaker profile (renames that voiceprint everywhere it appears)."""
    cfg = load_config()
    with PlaudClient(cfg) as client:
        roster = client.list_server_speakers()
        targets = [s for s in roster if (s.get("speaker_name") or s.get("name")) == old_name]
        if not targets:
            console.print(f"[red]no server speaker named[/red] {old_name}")
            raise typer.Exit(1)
        for s in targets:
            if "speaker_name" in s:
                s["speaker_name"] = new_name
            else:
                s["name"] = new_name
            s["need_sync"] = True
        client.sync_speakers(targets)
    console.print(f"[green]renamed[/green] {old_name} → {new_name} ({len(targets)} profile)")


@safe_command(name="plaud-relabel")
def plaud_relabel(
    file_id: str,
    mapping: list[str] = typer.Argument(..., help="OLD=NEW pairs, e.g. 'Speaker 1=진행자'"),
) -> None:
    """Rename speakers in the Plaud SERVER transcript of ONE file (web parity).

    PATCHes the full trans_result back like web.plaud.ai does, preserving
    original_speaker, then refreshes the local content cache.
    """
    pairs: dict[str, str] = {}
    for raw in mapping:
        old, sep, new = raw.partition("=")
        if not sep or not old.strip() or not new.strip():
            raise typer.BadParameter(f"expected OLD=NEW, got: {raw}")
        pairs[old.strip()] = new.strip()
    cfg = load_config()
    with PlaudClient(cfg) as client:
        changed = client.rename_transcript_speakers(file_id, pairs)
        if not changed:
            console.print("[yellow]no segments matched[/yellow] — nothing pushed")
            return
        # Refresh the local cache so the app reflects the server transcript.
        content = client.file_content(file_id)
    Storage().save_content(content, now=int(time.time()))
    console.print(f"[green]relabeled[/green] {changed} segments in {file_id}")


@safe_command(name="note-edit")
def note_edit(
    file_id: str,
    note_id: str = typer.Option(..., "--note-id", help="from file detail note_list"),
    title: str = typer.Option(..., "--title"),
    content_file: str = typer.Option(..., "--content-file", help="path to markdown body"),
    note_type: str = typer.Option("auto_sum_note", "--note-type"),
    tab: str = typer.Option("Summary", "--tab"),
) -> None:
    """Edit a note's content + title on the Plaud SERVER (e.g. the AI summary)."""
    body = Path(content_file).read_text(encoding="utf-8")
    cfg = load_config()
    with PlaudClient(cfg) as client:
        client.update_note_info(
            file_id=file_id,
            note_id=note_id,
            note_type=note_type,
            note_content=body,
            note_tab_name=tab,
            note_title=title,
        )
    console.print(f"[green]updated note[/green] {note_id}")


# ---------- summarize / templates / slots ----------


@safe_command(name="templates")
def templates_list() -> None:
    """List available prompt templates."""
    from core.templates import list_templates

    for t in list_templates():
        console.print(f"  [bold]{t.name}[/bold]  [dim]{t.description}[/dim]")


@safe_command(name="template-show")
def template_show(name: str) -> None:
    from core.templates import load_template

    t = load_template(name)
    console.print(t.body)


@safe_command(name="template-save")
def template_save(
    name: str,
    description: str = typer.Option("", help="Short description"),
    body_file: Path = typer.Option(None, help="Read body from file (else stdin)"),
) -> None:
    from core.templates import save_template

    if body_file:
        body = body_file.read_text(encoding="utf-8")
    else:
        import sys

        body = sys.stdin.read()
    if not body.strip():
        console.print("[red]empty body[/red]")
        raise typer.Exit(1)
    path = save_template(name, body, description=description)
    console.print(f"[green]saved[/green] {path}")


@safe_command(name="template-delete")
def template_delete(name: str) -> None:
    from core.templates import delete_template

    delete_template(name)
    console.print(f"[green]deleted[/green] {name}")


@safe_command(name="slots")
def slots_list() -> None:
    """List configured summary slots."""
    from core.slots import load_slots

    for s in load_slots():
        output_model = s.model_id or s.model
        console.print(f"  [bold]{s.name}[/bold]  {s.model} · {output_model} · {s.template}")


@safe_command(name="slot-add")
def slot_add(
    name: str,
    model: str,
    template: str,
    model_id: str = typer.Option("", help="Optional provider API model id override."),
) -> None:
    from core.slots import load_slots, save_slots, Slot

    slots = load_slots()
    slots = [s for s in slots if s.name != name]
    slots.append(Slot(name=name, model=model, template=template, model_id=model_id))
    save_slots(slots)
    model_label = f"{model}:{model_id}" if model_id else model
    console.print(f"[green]added[/green] {name}  ({model_label} · {template})")


@safe_command(name="slot-delete")
def slot_delete(name: str) -> None:
    from core.slots import load_slots, save_slots

    slots = [s for s in load_slots() if s.name != name]
    save_slots(slots)
    console.print(f"[green]deleted[/green] {name}")


@safe_command(name="config")
def config_show() -> None:
    """Show the current app config (backends + path overrides)."""
    from core import app_config

    cfg = app_config.load()
    console.print("[bold]Backends[/bold]")
    for k, v in cfg["backends"].items():
        console.print(f"  {k:>8}: {v}")
    console.print("\n[bold]API model ids[/bold] (used when backend=api)")
    for k, v in cfg["models"].items():
        console.print(f"  {k:>8}: {v}")
    console.print(f"\n[bold]Auto-folder provider preference[/bold]: {app_config.classify_model()}")
    console.print(
        "[bold]External arbitration threshold[/bold]: "
        f"{app_config.folder_llm_threshold():.2f} (preference only; not consent)"
    )
    console.print(
        f"[bold]Metadata model[/bold]: {app_config.metadata_model()}"
        f" (backend: {app_config.backend_for(app_config.metadata_model())})"
    )
    auto_meta = "on" if app_config.auto_metadata_enabled() else "off"
    console.print(
        f"[bold]Auto metadata[/bold]: {auto_meta} (limit {app_config.auto_metadata_limit()}/batch)"
    )
    console.print("\n[bold]Path overrides[/bold] (empty = default)")
    for k, v in cfg["paths"].items():
        console.print(f"  {k:>11}: {v or '(default)'}")
    console.print("\n[bold]Environment[/bold] (env > config; empty = unset)")
    vault = app_config.obsidian_vault()
    api_info = app_config.api_info_dir()
    console.print(f"  {'obsidian_vault':>14}: {vault or '(unset)'}")
    console.print(f"  {'author':>14}: {app_config.author() or '(unset)'}")
    console.print(f"  {'api_info_dir':>14}: {api_info or '(unset)'}")


@safe_command(name="config-classify")
def config_classify(model: str) -> None:
    """Set the UI's folder-routing provider preference (not external consent)."""
    valid = ("claude", "codex", "gemini", "grok")
    if model not in valid:
        raise typer.BadParameter(f"model must be one of: {', '.join(valid)}")
    app_config.set_classify_model(model)
    backend = app_config.backend_for(model)
    console.print(f"[green]ok[/green] folder provider -> {model} (backend: {backend})")


@safe_command(name="config-metadata-model")
def config_metadata_model(
    model: str,
    backend: str = typer.Option("", help="Optionally also set the provider backend: cli | api."),
    model_id: str = typer.Option("", "--id", help="Optionally pin the API model id."),
) -> None:
    """Set which model metadata-generate uses by default (e.g. codex = GPT via subscription)."""
    valid = ("claude", "codex", "gemini", "grok")
    if model not in valid:
        raise typer.BadParameter(f"model must be one of: {', '.join(valid)}")
    if backend:
        try:
            model, backend = validate_route(model, backend)
        except ModelUnavailable as exc:
            raise typer.BadParameter(str(exc)) from exc
        app_config.set_backend(model, backend)
    if model_id:
        app_config.set_model_id(model, model_id)
    app_config.set_metadata_model(model)
    console.print(
        f"[green]ok[/green] metadata model -> {model} (backend: {app_config.backend_for(model)})"
    )


@safe_command(name="config-auto-metadata")
def config_auto_metadata(
    state: str = typer.Argument(..., help="on | off"),
    limit: int = typer.Option(0, help="Max files per auto batch (0 = keep current)."),
) -> None:
    """Enable/disable automatic metadata generation after sync."""
    if state not in ("on", "off"):
        raise typer.BadParameter("state must be 'on' or 'off'")
    app_config.set_auto_metadata(state == "on")
    if limit > 0:
        cfg = app_config.load()
        cfg["auto_metadata_limit"] = limit
        app_config.save(cfg)
    console.print(
        f"[green]ok[/green] auto metadata -> {state}"
        f" (limit {app_config.auto_metadata_limit()}/batch)"
    )


@safe_command(name="star")
def star_cmd(
    file_id: str,
    off: bool = typer.Option(False, "--off", help="Remove the star."),
) -> None:
    """Star / unstar a recording (local-only flag, shown in the app list)."""
    Storage().set_starred(file_id, not off)
    console.print(f"[green]{'unstarred' if off else 'starred'}[/green] {file_id}")


@safe_command(name="config-vault")
def config_vault(path: str = "") -> None:
    """Set the Obsidian vault path. Pass empty to clear."""
    app_config.set_obsidian_vault(path)
    console.print(f"[green]ok[/green] obsidian_vault -> {path or '(unset)'}")


@safe_command(name="config-author")
def config_author(name: str = "") -> None:
    """Set the author name used in generated notes. Pass empty to clear."""
    app_config.set_author(name)
    console.print(f"[green]ok[/green] author -> {name or '(unset)'}")


@safe_command(name="config-backend")
def config_backend(model: str, backend: str) -> None:
    """Set a model's backend: cli or api."""
    from core import app_config

    try:
        model, backend = validate_route(model, backend)
    except ModelUnavailable as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    app_config.set_backend(model, backend)
    console.print(f"[green]ok[/green] {model} -> {backend}")


@safe_command(name="config-model")
def config_model(model: str, model_id: str) -> None:
    """Pin the model id used in API mode (e.g. claude-opus-4-5, gpt-5)."""
    from core import app_config

    app_config.set_model_id(model, model_id)
    console.print(f"[green]ok[/green] {model} api model id -> {model_id}")


@safe_command(name="config-folder-threshold")
def config_folder_threshold(
    value: float = typer.Argument(..., min=0.0, max=1.0),
) -> None:
    """Set when an explicitly approved model may arbitrate a weak local match."""
    app_config.set_folder_llm_threshold(value)
    console.print(f"[green]ok[/green] folder model threshold -> {value:.2f}")


@safe_command(name="config-path")
def config_path(kind: str, path: str = "") -> None:
    """Set output path override. kind = transcripts | summaries | integrated.

    Pass empty path to clear the override and fall back to default.
    """
    from core import app_config

    if kind not in ("transcripts", "summaries", "integrated"):
        console.print("[red]kind must be transcripts / summaries / integrated[/red]")
        raise typer.Exit(1)
    app_config.set_path(kind, path)
    console.print(f"[green]ok[/green] {kind} -> {path or '(default)'}")


@safe_command()
def contents(file_id: str) -> None:
    """plfetch-style: fetch transcript + summary together and write to disk."""
    cfg = load_config()
    storage = Storage()
    with PlaudClient(cfg) as client:
        content = client.file_content(file_id)
    storage.save_content(content, now=int(time.time()))

    from core.paths import transcripts_dir

    tdir = transcripts_dir(file_id)
    (tdir / "plaud.transcript.md").write_text(content.transcript_text(), encoding="utf-8")
    if content.summary_md:
        (tdir / "plaud.summary.md").write_text(content.summary_md, encoding="utf-8")
    if content.outline:
        (tdir / "plaud.outline.md").write_text(content.outline_text(), encoding="utf-8")
    console.print(f"[green]wrote[/green] {tdir}")


@safe_command(name="paths")
def paths_show() -> None:
    """Print the canonical project paths (for CLI/reference scripts)."""
    from core.paths import (
        DATA_DIR,
        TRANSCRIPTS_DIR,
        SUMMARIES_DIR,
        TEMPLATES_DIR,
        SLOTS_FILE,
    )

    for name, p in [
        ("data", DATA_DIR),
        ("transcripts", TRANSCRIPTS_DIR),
        ("summaries", SUMMARIES_DIR),
        ("templates", TEMPLATES_DIR),
        ("slots.json", SLOTS_FILE),
    ]:
        console.print(f"{name:12s}  {p}")


@safe_command()
def onboard(env_path: Path = Path(".env")) -> None:
    """Pipe a Plaud cURL on stdin to populate Keychain (headers + cookies)."""
    from cli.onboard import parse_curl, write_env
    import sys

    curl_text = sys.stdin.read()
    if not curl_text.strip():
        console.print("[red]nothing on stdin[/red] — usage: pbpaste | uv run plaud onboard")
        raise typer.Exit(1)
    values = parse_curl(curl_text)
    write_env(values, env_path)
    console.print(f"[green]done[/green]  cookie captured: {'PLAUD_COOKIE' in values}")


def build_obsidian_prompt(*, content, vault: Path, folder: str) -> str:
    """Compose the Claude Code prompt for filing one Plaud note into Obsidian.

    Includes every summary variant Plaud produced (auto_sum_note + each
    template-based sum_multi_note such as Adaptive Summary, Meeting Minutes,
    Lecture Summary, etc.) so the assistant can pick the most relevant or
    merge them.
    """
    title = content.title or content.file_id
    keywords = ", ".join(content.keywords[:15])
    transcript = content.transcript_text()

    summary_sections = []
    for s in content.summaries:
        label = s.title or s.kind
        summary_sections.append(f"### {label}\n\n{s.body_md.strip()}")
    summaries_block = "\n\n".join(summary_sections) or "(no AI summary)"

    return f"""아래 Plaud 녹음을 옵시디언 볼트로 정리해서 보내줘.

## 메타데이터
- file_id: {content.file_id}
- 제목: {title}
- 키워드: {keywords}
- 볼트 경로: {vault}
- 대상 폴더: {folder}

## 작업
1. obsidian-markdown / cmds-format 스킬을 사용해 노트 1개를 만들어.
2. 파일명은 `YYYYMMDD_제목.md` 형태로 (제목은 한글 그대로 OK).
3. 프론트매터에 source: plaud, plaud_id, keywords, created 포함.
4. 본문은 [요약(가장 적절한 템플릿 선택 또는 병합)] → [핵심 인사이트(불릿)] → [토픽 개요(타임스탬프)] → [전체 트랜스크립트] 순으로 구성.
5. 여러 요약 템플릿이 있으면 가장 적합한 것을 선택하거나 병합해. 사용자가 Adaptive Summary나 Meeting Minutes 같은 특정 템플릿을 만들었다면 그걸 우선시해.
6. 저장 후 파일 경로를 출력하고 종료.

## AI Summary 모음 (Plaud의 모든 템플릿 변형)
{summaries_block}

## 토픽 개요
{content.outline_text() or "(없음)"}

## 전체 트랜스크립트
{transcript[:50000]}
"""


_CLAUDE_TASKS = {
    "ask": (
        "Plaud 녹음 {file_id} (제목: {title}) 에 대해 사용자가 질문한다.\n"
        "plaud-shared 스킬 규약대로 진행하되, 이 저장소의 CLI가 가장 빠른 경로다:\n"
        "  uv run plaud brief {file_id}        # L1 개요\n"
        "  uv run plaud contents {file_id}     # 요약/하이라이트\n"
        "  uv run plaud transcript {file_id}   # 전체 전사\n"
        "듀얼 최종본이 있으면 data/integrated/{file_id}/ 의 *.transcript.md 를 우선 사용.\n\n"
        "질문: {prompt}"
    ),
    "followup": (
        "plaud-followup 스킬을 사용해 Plaud 녹음 {file_id} (제목: {title}) 의 "
        "후속 조치를 진행해줘 — 액션 아이템 추출과 필요한 후속 이메일/메시지 초안까지.\n"
        "전사·요약은 이 저장소 CLI(uv run plaud contents/transcript {file_id}) 또는 "
        "data/integrated/{file_id}/ 의 듀얼 최종본을 사용.\n"
        "{prompt}"
    ),
    "digest": (
        "plaud-digest 스킬로 최근 Plaud 녹음들을 롤업 다이제스트해줘. "
        "로컬 캐시가 최신이니 uv run plaud query -n 20 으로 시작하면 빠르다.\n"
        "{prompt}"
    ),
    "custom": "{prompt}",
}


@safe_command(name="claude")
def claude_cmd(
    file_id: str = typer.Argument("", help="Recording id (digest/custom may omit)."),
    task: str = typer.Option(
        "ask", help="ask | followup | digest | custom — which skill chain to drive."
    ),
    prompt: str = typer.Option("", help="Your question / extra instructions."),
) -> None:
    """Open Claude Code preloaded with this recording's context (plaud-* skill chain).

    Bridges the app to the CLI/MCP layer: the launched session has the plaud
    skills, the Plaud MCP server, and this repo's CLI all available.
    """
    if task not in _CLAUDE_TASKS:
        raise typer.BadParameter(f"task must be one of: {', '.join(_CLAUDE_TASKS)}")
    if task in ("ask", "followup") and not file_id:
        raise typer.BadParameter(f"task '{task}' needs a file id")
    if task in ("ask", "custom") and not prompt.strip():
        raise typer.BadParameter(f"task '{task}' needs --prompt")

    title = ""
    if file_id:
        row = Storage().get_file_row(file_id)
        title = (row["filename"] if row else "") or file_id
    text = _CLAUDE_TASKS[task].format(file_id=file_id, title=title, prompt=prompt.strip()).strip()
    from core.config import PROJECT_ROOT

    launch_claude(text, cwd=PROJECT_ROOT)
    console.print(f"[green]launched Claude Code[/green] ({task}) in {PROJECT_ROOT}")


def launch_claude(prompt: str, *, cwd: Path) -> None:
    """Open Terminal.app in `cwd` and run `claude` with the prompt."""
    tmp = Path("/tmp") / f"plaud-prompt-{int(time.time())}.txt"
    tmp.write_text(prompt, encoding="utf-8")

    # AppleScript opens Terminal, cd's to vault, pipes the prompt into `claude`.
    script = f"""
    tell application "Terminal"
        activate
        do script "cd {shlex.quote(str(cwd))} && claude < {shlex.quote(str(tmp))}"
    end tell
    """
    subprocess.run(["osascript", "-e", script], check=False)


if __name__ == "__main__":
    app()

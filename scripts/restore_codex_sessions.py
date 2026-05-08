from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import shutil
import sqlite3
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_SOURCE_DIRS = ("sessions", "sessions.bak", "archived_sessions.bak")
SKIP_TEXT_PREFIXES = (
    "<permissions instructions>",
    "<app-context>",
    "<environment_context>",
    "<collaboration_mode>",
    "<skills_instructions>",
    "# AGENTS.md instructions",
    "The following is the Codex agent history",
    "We need continue from summary",
)


@dataclass(slots=True)
class RestoreCandidate:
    session_id: str
    source_path: Path
    target_path: Path
    title: str
    cwd: str
    created_at: int
    updated_at: int
    created_at_ms: int | None
    updated_at_ms: int | None
    source: str
    model_provider: str
    sandbox_policy: str
    approval_mode: str
    tokens_used: int
    has_user_event: int
    archived: int
    archived_at: int | None
    git_sha: str | None
    git_branch: str | None
    git_origin_url: str | None
    cli_version: str
    first_user_message: str
    agent_nickname: str | None
    agent_role: str | None
    memory_mode: str
    model: str | None
    reasoning_effort: str | None
    agent_path: str | None
    thread_source: str | None


@dataclass(slots=True)
class ApiCopyRepair:
    original_id: str
    copy_id: str
    rollout_path: Path
    title: str
    cwd: str
    update_cwd: bool
    update_title: bool
    update_thread_source: bool
    update_jsonl: bool
    create_jsonl: bool
    source_path: Path | None


def default_codex_home() -> Path:
    env_home = os.environ.get("CODEX_HOME")
    if env_home:
        return Path(env_home).expanduser()
    return Path.home() / ".codex"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Merge missing Codex Desktop session records into the current state_5.sqlite "
            "without overwriting existing API-mode conversations."
        ),
    )
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=default_codex_home(),
        help="Codex home directory. Defaults to CODEX_HOME or ~/.codex.",
    )
    parser.add_argument(
        "--source",
        type=Path,
        action="append",
        default=[],
        help="Extra source JSONL file or directory. Can be repeated.",
    )
    parser.add_argument(
        "--backup-db",
        type=Path,
        default=None,
        help="Optional old state_5.sqlite backup to read metadata from.",
    )
    parser.add_argument(
        "--project-filter",
        default="",
        help="Only restore sessions whose cwd contains this text.",
    )
    parser.add_argument(
        "--project-exact",
        action="append",
        default=[],
        help="Only operate on sessions whose plain cwd exactly matches this project path. Can be repeated.",
    )
    parser.add_argument(
        "--title-filter",
        default="",
        help="Only restore sessions whose title contains this text.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually modify current state/index and copy missing JSONL files. Default is dry-run.",
    )
    parser.add_argument(
        "--backup-dir",
        type=Path,
        default=None,
        help="Directory for pre-apply backups. Defaults to ~/.codex/restore_backups/<timestamp>.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Restore at most N missing sessions. Useful for testing.",
    )
    parser.add_argument(
        "--api-visible-copy",
        action="store_true",
        help=(
            "Create custom-provider visible copies for existing non-custom sessions instead of "
            "changing originals. This preserves old provider records for switching back."
        ),
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="Read-only report of current Codex threads grouped by project/provider.",
    )
    parser.add_argument(
        "--normalize-cwd",
        action="store_true",
        help="Normalize custom-provider thread cwd values from extended Windows paths to plain paths.",
    )
    parser.add_argument(
        "--repair-api-visible-copies",
        action="store_true",
        help=(
            "Repair existing custom-provider copies that were already created for openai threads. "
            "This does not modify original openai rows or unrelated custom/API conversations."
        ),
    )
    parser.add_argument(
        "--rollback",
        type=Path,
        default=None,
        help="Restore current state files from a backup directory created by this script.",
    )
    parser.add_argument(
        "--backup-only",
        action="store_true",
        help="Create a backup of current Codex state files and exit without modifying anything.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    codex_home = args.codex_home.expanduser()
    state_db = codex_home / "state_5.sqlite"
    if not state_db.exists():
        sys.stderr.write(f"Missing current database: {state_db}\n")
        return 2

    if args.rollback is not None:
        if not args.apply:
            sys.stderr.write("Rollback requires --apply and a backup directory.\n")
            return 2
        rollback_backup(codex_home, args.rollback)
        return 0

    if args.backup_only:
        backup_dir = args.backup_dir or codex_home / "restore_backups" / datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_current_files(codex_home, backup_dir)
        sys.stdout.write(f"Backup directory: {backup_dir}\n")
        return 0

    if args.report:
        write_report(codex_home, state_db)
        return 0

    if args.normalize_cwd:
        if not args.apply:
            report_cwd_normalization(state_db)
            return 0
        backup_dir = args.backup_dir or codex_home / "restore_backups" / datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_current_files(codex_home, backup_dir)
        normalize_custom_cwd(state_db, update_jsonl=True)
        sys.stdout.write(f"Backup directory: {backup_dir}\n")
        return 0

    if args.repair_api_visible_copies:
        current_columns = load_current_thread_columns(state_db)
        repairs = find_api_copy_repairs(state_db)
        repairs = filter_api_copy_repairs(repairs, args.project_exact, args.project_filter)
        print_api_copy_repair_summary(repairs, dry_run=not args.apply)
        if not args.apply:
            return 0
        backup_dir = args.backup_dir or codex_home / "restore_backups" / datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_current_files(codex_home, backup_dir)
        apply_api_copy_repairs(state_db, current_columns, repairs)
        sys.stdout.write(f"Applied API visible copy repairs for {len(repairs)} sessions.\n")
        sys.stdout.write(f"Backup directory: {backup_dir}\n")
        remaining = find_api_copy_repairs(state_db)
        if remaining:
            sys.stderr.write(
                f"WARNING: {len(remaining)} API visible copies still need repair after apply. "
                "If Codex Desktop is running, fully exit it and run this command again.\n",
            )
            return 1
        return 0

    backup_db = args.backup_db or codex_home / "state_5.sqlite.bak"
    titles = load_index_titles(codex_home)
    old_rows = load_old_thread_rows(backup_db) if backup_db.exists() else {}
    current_ids = load_current_thread_ids(state_db)
    current_columns = load_current_thread_columns(state_db)

    files = iter_session_files(codex_home, args.source)
    candidates = build_candidates(codex_home, files, titles, old_rows)
    if args.api_visible_copy:
        candidates = build_api_visible_copies(codex_home, candidates, current_ids)
    else:
        candidates = [item for item in candidates if item.session_id not in current_ids]
    candidates = filter_candidates(candidates, args.project_filter, args.title_filter)
    candidates = dedupe_candidates(candidates)
    if args.limit > 0:
        candidates = candidates[: args.limit]

    print_summary(candidates, dry_run=not args.apply)
    if not args.apply:
        return 0

    backup_dir = args.backup_dir or codex_home / "restore_backups" / datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_current_files(codex_home, backup_dir)
    copy_rollouts(candidates)
    insert_thread_rows(state_db, current_columns, candidates)
    append_session_index(codex_home / "session_index.jsonl", candidates)
    sys.stdout.write(f"Applied restore for {len(candidates)} sessions.\n")
    sys.stdout.write(f"Backup directory: {backup_dir}\n")
    return 0


def load_index_titles(codex_home: Path) -> dict[str, tuple[str, str]]:
    titles: dict[str, tuple[str, str]] = {}
    for name in ("session_index.jsonl", "session_index.jsonl.bak"):
        path = codex_home / name
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, dict):
                    continue
                session_id = as_str(row.get("id"))
                title = as_str(row.get("thread_name"))
                updated_at = as_str(row.get("updated_at"))
                if session_id and title and not is_internal_text(title):
                    titles[session_id] = (title, updated_at)
    return titles


def load_old_thread_rows(path: Path) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        table_exists = cur.execute(
            "select 1 from sqlite_master where type='table' and name='threads'",
        ).fetchone()
        if not table_exists:
            return rows
        for row in cur.execute("select * from threads"):
            item = dict(row)
            session_id = as_str(item.get("id"))
            if session_id:
                rows[session_id] = item
    finally:
        con.close()
    return rows


def load_current_thread_ids(path: Path) -> set[str]:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        cur = con.cursor()
        return {row[0] for row in cur.execute("select id from threads")}
    finally:
        con.close()


def load_current_thread_columns(path: Path) -> list[str]:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        cur = con.cursor()
        return [row[1] for row in cur.execute("pragma table_info(threads)")]
    finally:
        con.close()


def iter_session_files(codex_home: Path, sources: list[Path]) -> list[Path]:
    candidates = sources or [codex_home / name for name in DEFAULT_SOURCE_DIRS]
    found: list[Path] = []
    for source in candidates:
        path = source.expanduser()
        if path.is_file() and path.suffix.lower() == ".jsonl":
            found.append(path)
        elif path.is_dir():
            found.extend(path.rglob("*.jsonl"))
    return sorted(set(found), key=lambda item: str(item).lower())


def build_candidates(
    codex_home: Path,
    files: list[Path],
    titles: dict[str, tuple[str, str]],
    old_rows: dict[str, dict[str, Any]],
) -> list[RestoreCandidate]:
    candidates: list[RestoreCandidate] = []
    for source_path in files:
        parsed = parse_rollout(source_path)
        session_id = parsed.get("id")
        if not isinstance(session_id, str) or not session_id:
            continue
        old = old_rows.get(session_id, {})
        title, indexed_updated = titles.get(session_id, ("", ""))
        title = title or as_str(old.get("title")) or as_str(parsed.get("title")) or f"session-{session_id[:8]}"
        title = normalize_title(title, session_id)
        updated_at = int_or_none(old.get("updated_at")) or seconds_from_iso(indexed_updated) or int(parsed["updated_at"])
        created_at = int_or_none(old.get("created_at")) or int(parsed["created_at"])
        created_at_ms = int_or_none(old.get("created_at_ms")) or millis_from_seconds(created_at)
        updated_at_ms = int_or_none(old.get("updated_at_ms")) or millis_from_seconds(updated_at)
        target_path = target_rollout_path(codex_home, source_path, session_id, created_at)
        candidates.append(
            RestoreCandidate(
                session_id=session_id,
                source_path=source_path,
                target_path=target_path,
                title=title,
                cwd=normalize_cwd(as_str(old.get("cwd")) or as_str(parsed.get("cwd"))),
                created_at=created_at,
                updated_at=updated_at,
                created_at_ms=created_at_ms,
                updated_at_ms=updated_at_ms,
                source=as_str(old.get("source")) or "vscode",
                model_provider=as_str(old.get("model_provider")) or as_str(parsed.get("model_provider")) or "openai",
                sandbox_policy=as_str(old.get("sandbox_policy")) or parsed.get("sandbox_policy_json", '{"type":"danger-full-access"}'),
                approval_mode=as_str(old.get("approval_mode")) or as_str(parsed.get("approval_mode")) or "never",
                tokens_used=int_or_none(old.get("tokens_used")) or int(parsed.get("tokens_used", 0)),
                has_user_event=int_or_none(old.get("has_user_event")) or 0,
                archived=int_or_none(old.get("archived")) or 0,
                archived_at=int_or_none(old.get("archived_at")),
                git_sha=nullable_str(old.get("git_sha")),
                git_branch=nullable_str(old.get("git_branch")),
                git_origin_url=nullable_str(old.get("git_origin_url")),
                cli_version=as_str(old.get("cli_version")) or as_str(parsed.get("cli_version")),
                first_user_message=as_str(old.get("first_user_message")) or as_str(parsed.get("first_user_message")),
                agent_nickname=nullable_str(old.get("agent_nickname")),
                agent_role=nullable_str(old.get("agent_role")),
                memory_mode=as_str(old.get("memory_mode")) or "enabled",
                model=nullable_str(old.get("model")) or nullable_str(parsed.get("model")),
                reasoning_effort=nullable_str(old.get("reasoning_effort")) or nullable_str(parsed.get("reasoning_effort")),
                agent_path=nullable_str(old.get("agent_path")),
                thread_source=nullable_str(old.get("thread_source")),
            ),
        )
    return candidates


def parse_rollout(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": "",
        "created_at": int(path.stat().st_mtime),
        "updated_at": int(path.stat().st_mtime),
        "cwd": "",
        "title": "",
        "model_provider": "",
        "cli_version": "",
        "first_user_message": "",
        "model": None,
        "reasoning_effort": None,
        "approval_mode": "",
        "sandbox_policy_json": '{"type":"danger-full-access"}',
        "tokens_used": 0,
    }
    line_count = 0
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            raw = line.strip()
            if not raw:
                continue
            line_count += 1
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            timestamp = as_str(row.get("timestamp"))
            seconds = seconds_from_iso(timestamp)
            if seconds:
                result["updated_at"] = seconds
            payload = row.get("payload")
            if not isinstance(payload, dict):
                continue
            row_type = as_str(row.get("type"))
            if row_type == "session_meta":
                result["id"] = result["id"] or as_str(payload.get("id"))
                result["created_at"] = seconds_from_iso(as_str(payload.get("timestamp"))) or result["created_at"]
                result["cwd"] = result["cwd"] or as_str(payload.get("cwd"))
                result["model_provider"] = result["model_provider"] or as_str(payload.get("model_provider"))
                result["cli_version"] = result["cli_version"] or as_str(payload.get("cli_version"))
            elif row_type == "turn_context":
                result["cwd"] = result["cwd"] or as_str(payload.get("cwd"))
                result["model"] = result["model"] or nullable_str(payload.get("model"))
                result["reasoning_effort"] = result["reasoning_effort"] or nullable_str(payload.get("effort"))
                result["approval_mode"] = result["approval_mode"] or as_str(payload.get("approval_policy"))
                sandbox_policy = payload.get("sandbox_policy")
                if isinstance(sandbox_policy, dict):
                    result["sandbox_policy_json"] = json.dumps(sandbox_policy, ensure_ascii=False, separators=(",", ":"))
            elif row_type == "event_msg" and payload.get("type") == "user_message":
                text = as_str(payload.get("message")).strip()
                if text and not is_internal_text(text):
                    result["first_user_message"] = result["first_user_message"] or text
                    result["title"] = result["title"] or infer_title(text)
            elif row_type == "response_item":
                item_type = as_str(payload.get("type"))
                if item_type == "message" and payload.get("role") == "user":
                    text = extract_content_text(payload.get("content")).strip()
                    if text and not is_internal_text(text):
                        result["first_user_message"] = result["first_user_message"] or text
                        result["title"] = result["title"] or infer_title(text)
    result["tokens_used"] = line_count
    if not result["id"]:
        result["id"] = session_id_from_filename(path)
    return result


def filter_candidates(
    candidates: list[RestoreCandidate],
    project_filter: str,
    title_filter: str,
) -> list[RestoreCandidate]:
    result = candidates
    if project_filter:
        needle = project_filter.casefold()
        result = [item for item in result if needle in item.cwd.casefold()]
    if title_filter:
        needle = title_filter.casefold()
        result = [item for item in result if needle in item.title.casefold()]
    return result


def dedupe_candidates(candidates: list[RestoreCandidate]) -> list[RestoreCandidate]:
    best: dict[str, RestoreCandidate] = {}
    for item in candidates:
        current = best.get(item.session_id)
        if current is None or candidate_score(item) > candidate_score(current):
            best[item.session_id] = item
    return sorted(best.values(), key=lambda item: item.updated_at, reverse=True)


def build_api_visible_copies(
    codex_home: Path,
    candidates: list[RestoreCandidate],
    current_ids: set[str],
) -> list[RestoreCandidate]:
    copies: list[RestoreCandidate] = []
    for item in candidates:
        if item.model_provider == "custom":
            continue
        copy_id = api_copy_id(item.session_id)
        if copy_id in current_ids:
            continue
        target_path = target_api_copy_path(codex_home, item.target_path, copy_id, item.created_at)
        copies.append(
            dataclasses.replace(
                item,
                session_id=copy_id,
                target_path=target_path,
                cwd=plain_cwd(item.cwd),
                model_provider="custom",
                title=f"{item.title} [API可见副本]",
                thread_source=f"api-visible-copy:{item.session_id}",
            ),
        )
    return copies


def find_api_copy_repairs(state_db: Path) -> list[ApiCopyRepair]:
    con = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    try:
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        originals = [dict(row) for row in cur.execute("select * from threads where model_provider='openai'")]
        custom_rows = {
            as_str(row["id"]): dict(row)
            for row in cur.execute("select * from threads where model_provider='custom'")
        }
    finally:
        con.close()

    repairs: list[ApiCopyRepair] = []
    for original in originals:
        original_id = as_str(original.get("id"))
        if not original_id:
            continue
        copy_id = api_copy_id(original_id)
        copy = custom_rows.get(copy_id)
        if copy is None:
            continue

        expected_cwd = plain_cwd(as_str(original.get("cwd")))
        expected_title = api_copy_title(as_str(original.get("title")), copy_id)
        expected_source = f"api-visible-copy:{original_id}"
        rollout_path = rollout_path_to_path(as_str(copy.get("rollout_path")))
        source_path = rollout_path_to_path(as_str(original.get("rollout_path")))

        update_cwd = as_str(copy.get("cwd")) != expected_cwd
        update_title = as_str(copy.get("title")) != expected_title
        update_thread_source = as_str(copy.get("thread_source")) != expected_source
        create_jsonl = not rollout_path.exists() and source_path.exists()
        update_jsonl = create_jsonl or (
            rollout_path.exists()
            and api_copy_rollout_needs_repair(
                rollout_path,
                original_id,
                copy_id,
                expected_title,
                expected_cwd,
            )
        )

        if update_cwd or update_title or update_thread_source or update_jsonl:
            repairs.append(
                ApiCopyRepair(
                    original_id=original_id,
                    copy_id=copy_id,
                    rollout_path=rollout_path,
                    title=expected_title,
                    cwd=expected_cwd,
                    update_cwd=update_cwd,
                    update_title=update_title,
                    update_thread_source=update_thread_source,
                    update_jsonl=update_jsonl,
                    create_jsonl=create_jsonl,
                    source_path=source_path if source_path.exists() else None,
                ),
            )
    return sorted(repairs, key=lambda item: item.copy_id)


def api_copy_title(title: str, copy_id: str) -> str:
    suffix = " [API可见副本]"
    base = normalize_title(title, copy_id)
    if base.endswith(suffix):
        return base
    return f"{base}{suffix}"


def api_copy_rollout_needs_repair(
    path: Path,
    original_id: str,
    copy_id: str,
    title: str,
    cwd: str,
) -> bool:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                raw = line.strip()
                if not raw:
                    continue
                try:
                    row = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if json.dumps(row, ensure_ascii=False, separators=(",", ":")) != json.dumps(
                    normalize_api_copy_row(row, original_id, copy_id, title, cwd),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ):
                    return True
    except OSError:
        return True
    return False


def normalize_api_copy_row(
    row: Any,
    original_id: str,
    copy_id: str,
    title: str,
    cwd: str,
) -> Any:
    rewritten = replace_thread_id(row, original_id, copy_id)
    return replace_api_copy_metadata(rewritten, copy_id, title, cwd)


def replace_api_copy_metadata(value: Any, copy_id: str, title: str, cwd: str) -> Any:
    if isinstance(value, list):
        return [replace_api_copy_metadata(item, copy_id, title, cwd) for item in value]
    if isinstance(value, dict):
        result = {key: replace_api_copy_metadata(item, copy_id, title, cwd) for key, item in value.items()}
        if "cwd" in result:
            result["cwd"] = cwd
        payload = result.get("payload")
        if result.get("type") == "session_meta" and isinstance(payload, dict):
            payload["id"] = copy_id
            payload["model_provider"] = "custom"
            payload["cwd"] = cwd
        if isinstance(payload, dict):
            if "cwd" in payload:
                payload["cwd"] = cwd
            if payload.get("type") == "thread_name_updated":
                payload["thread_name"] = title
            if payload.get("thread_name") and payload.get("thread_id") == copy_id:
                payload["thread_name"] = title
        return result
    return value


def print_api_copy_repair_summary(repairs: list[ApiCopyRepair], dry_run: bool) -> None:
    mode = "DRY-RUN" if dry_run else "APPLY"
    sys.stdout.write(f"{mode}: {len(repairs)} existing API visible copies need repair.\n")
    sys.stdout.write(f"  cwd updates: {sum(1 for item in repairs if item.update_cwd)}\n")
    sys.stdout.write(f"  title updates: {sum(1 for item in repairs if item.update_title)}\n")
    sys.stdout.write(f"  thread_source updates: {sum(1 for item in repairs if item.update_thread_source)}\n")
    sys.stdout.write(f"  JSONL repairs: {sum(1 for item in repairs if item.update_jsonl)}\n")
    sys.stdout.write(f"  JSONL creates: {sum(1 for item in repairs if item.create_jsonl)}\n")
    by_project: dict[str, int] = {}
    for item in repairs:
        by_project[item.cwd] = by_project.get(item.cwd, 0) + 1
    for cwd, count in sorted(by_project.items(), key=lambda pair: pair[0].lower()):
        sys.stdout.write(f"{count:4d}  {cwd}\n")
    for item in repairs[:10]:
        flags = []
        if item.update_cwd:
            flags.append("cwd")
        if item.update_title:
            flags.append("title")
        if item.update_thread_source:
            flags.append("thread_source")
        if item.update_jsonl:
            flags.append("jsonl")
        sys.stdout.write(f"  - {item.copy_id} <- {item.original_id} | {', '.join(flags)} | {item.cwd}\n")
    if len(repairs) > 10:
        sys.stdout.write(f"  ... {len(repairs) - 10} more\n")


def filter_api_copy_repairs(
    repairs: list[ApiCopyRepair],
    project_exact: list[str],
    project_filter: str,
) -> list[ApiCopyRepair]:
    result = repairs
    exact = {plain_cwd(item) for item in project_exact if item}
    if exact:
        result = [item for item in result if plain_cwd(item.cwd) in exact]
    if project_filter:
        needle = project_filter.casefold()
        result = [item for item in result if needle in item.cwd.casefold()]
    return result


def apply_api_copy_repairs(state_db: Path, columns: list[str], repairs: list[ApiCopyRepair]) -> None:
    if not repairs:
        return
    allowed_columns = set(columns)
    con = sqlite3.connect(state_db)
    try:
        cur = con.cursor()
        for item in repairs:
            assignments: list[str] = []
            values: list[Any] = []
            if item.update_cwd and "cwd" in allowed_columns:
                assignments.append("cwd=?")
                values.append(item.cwd)
            if item.update_title and "title" in allowed_columns:
                assignments.append("title=?")
                values.append(item.title)
            if item.update_thread_source and "thread_source" in allowed_columns:
                assignments.append("thread_source=?")
                values.append(f"api-visible-copy:{item.original_id}")
            if assignments:
                values.append(item.copy_id)
                cur.execute(f"update threads set {', '.join(assignments)} where id=?", values)

            if item.update_jsonl:
                if item.create_jsonl:
                    if item.source_path is None:
                        continue
                    item.rollout_path.parent.mkdir(parents=True, exist_ok=True)
                    rewrite_rollout_copy(
                        item.source_path,
                        item.rollout_path,
                        item.original_id,
                        item.copy_id,
                        item.title,
                        item.cwd,
                    )
                elif item.rollout_path.exists():
                    repair_existing_api_copy_rollout(
                        item.rollout_path,
                        item.original_id,
                        item.copy_id,
                        item.title,
                        item.cwd,
                    )
        con.commit()
    finally:
        con.close()


def repair_existing_api_copy_rollout(
    path: Path,
    original_id: str,
    copy_id: str,
    title: str,
    cwd: str,
) -> None:
    lines: list[str] = []
    changed = False
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            raw = line.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                lines.append(line.rstrip("\n"))
                continue
            repaired = normalize_api_copy_row(row, original_id, copy_id, title, cwd)
            if repaired != row:
                changed = True
            lines.append(json.dumps(repaired, ensure_ascii=False, separators=(",", ":")))
    if changed:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def candidate_score(item: RestoreCandidate) -> tuple[int, int, float]:
    source_weight = {"sessions": 3, "sessions.bak": 2, "archived_sessions.bak": 1}.get(source_group(item.source_path), 0)
    return (source_weight, len(item.first_user_message), item.source_path.stat().st_mtime)


def target_rollout_path(codex_home: Path, source_path: Path, session_id: str, created_at: int) -> Path:
    try:
        relative = source_path.relative_to(codex_home)
        if relative.parts and relative.parts[0] == "sessions":
            return source_path
    except ValueError:
        pass
    created = datetime.fromtimestamp(created_at)
    filename = source_path.name
    if session_id not in filename:
        filename = f"rollout-{created.strftime('%Y-%m-%dT%H-%M-%S')}-{session_id}.jsonl"
    return codex_home / "sessions" / f"{created.year:04d}" / f"{created.month:02d}" / f"{created.day:02d}" / filename


def target_api_copy_path(codex_home: Path, original_target: Path, copy_id: str, created_at: int) -> Path:
    created = datetime.fromtimestamp(created_at)
    original_name = original_target.name
    filename = re.sub(r"019[a-z0-9-]{20,}", copy_id, original_name)
    if filename == original_name:
        filename = f"rollout-{created.strftime('%Y-%m-%dT%H-%M-%S')}-{copy_id}.jsonl"
    return codex_home / "sessions" / f"{created.year:04d}" / f"{created.month:02d}" / f"{created.day:02d}" / filename


def backup_current_files(codex_home: Path, backup_dir: Path) -> None:
    backup_dir.mkdir(parents=True, exist_ok=True)
    names = [
        "state_5.sqlite",
        "state_5.sqlite-shm",
        "state_5.sqlite-wal",
        "session_index.jsonl",
        "session_index.jsonl.bak",
    ]
    for name in names:
        source = codex_home / name
        if source.exists():
            shutil.copy2(source, backup_dir / name)


def copy_rollouts(candidates: list[RestoreCandidate]) -> None:
    for item in candidates:
        item.target_path.parent.mkdir(parents=True, exist_ok=True)
        if not item.target_path.exists():
            if item.thread_source and item.thread_source.startswith("api-visible-copy:"):
                original_id = item.thread_source.split(":", 1)[1]
                rewrite_rollout_copy(
                    item.source_path,
                    item.target_path,
                    original_id,
                    item.session_id,
                    item.title,
                    item.cwd,
                )
            else:
                shutil.copy2(item.source_path, item.target_path)


def api_copy_id(session_id: str) -> str:
    digest = uuid.uuid5(uuid.NAMESPACE_URL, f"codex-api-visible-copy:{session_id}")
    return f"{session_id[:8]}-{str(digest)[9:]}"


def rewrite_rollout_copy(
    source_path: Path,
    target_path: Path,
    original_id: str,
    copy_id: str,
    title: str,
    cwd: str,
) -> None:
    with source_path.open("r", encoding="utf-8", errors="replace") as source, target_path.open(
        "w",
        encoding="utf-8",
    ) as target:
        for line in source:
            raw = line.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                target.write(line)
                continue
            rewritten = replace_thread_id(row, original_id, copy_id)
            if isinstance(rewritten, dict):
                payload = rewritten.get("payload")
                if rewritten.get("type") == "session_meta" and isinstance(payload, dict):
                    payload["id"] = copy_id
                    payload["model_provider"] = "custom"
                    payload["cwd"] = cwd
                if isinstance(payload, dict):
                    if "cwd" in payload:
                        payload["cwd"] = cwd
                    if payload.get("type") == "thread_name_updated":
                        payload["thread_name"] = title
                    if payload.get("thread_name") and payload.get("thread_id") == copy_id:
                        payload["thread_name"] = title
            target.write(json.dumps(rewritten, ensure_ascii=False, separators=(",", ":")) + "\n")


def replace_thread_id(value: Any, original_id: str, copy_id: str) -> Any:
    if isinstance(value, str):
        return value.replace(original_id, copy_id)
    if isinstance(value, list):
        return [replace_thread_id(item, original_id, copy_id) for item in value]
    if isinstance(value, dict):
        return {key: replace_thread_id(item, original_id, copy_id) for key, item in value.items()}
    return value


def insert_thread_rows(db_path: Path, columns: list[str], candidates: list[RestoreCandidate]) -> None:
    con = sqlite3.connect(db_path)
    try:
        cur = con.cursor()
        placeholders = ", ".join("?" for _ in columns)
        quoted_columns = ", ".join(f'"{column}"' for column in columns)
        sql = f'insert or ignore into threads ({quoted_columns}) values ({placeholders})'
        rows = [tuple(thread_value(item, column) for column in columns) for item in candidates]
        cur.executemany(sql, rows)
        con.commit()
    finally:
        con.close()


def append_session_index(path: Path, candidates: list[RestoreCandidate]) -> None:
    existing: set[str] = set()
    if path.exists():
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    session_id = as_str(row.get("id"))
                    if session_id:
                        existing.add(session_id)
    with path.open("a", encoding="utf-8") as handle:
        for item in candidates:
            if item.session_id in existing:
                continue
            row = {
                "id": item.session_id,
                "thread_name": item.title,
                "updated_at": iso_from_seconds(item.updated_at),
            }
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def rollback_backup(codex_home: Path, backup_dir: Path) -> None:
    backup_dir = backup_dir.expanduser()
    if not backup_dir.exists() or not backup_dir.is_dir():
        raise FileNotFoundError(f"Backup directory does not exist: {backup_dir}")
    restored = 0
    for name in (
        "state_5.sqlite",
        "state_5.sqlite-shm",
        "state_5.sqlite-wal",
        "session_index.jsonl",
        "session_index.jsonl.bak",
    ):
        source = backup_dir / name
        if source.exists():
            shutil.copy2(source, codex_home / name)
            restored += 1
    sys.stdout.write(f"Restored {restored} files from {backup_dir}\n")


def write_report(codex_home: Path, state_db: Path) -> None:
    projects = load_project_roots(codex_home)
    con = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    try:
        cur = con.cursor()
        sys.stdout.write("Provider counts:\n")
        for provider, count in cur.execute("select model_provider,count(*) from threads group by model_provider"):
            sys.stdout.write(f"  {provider or 'NULL'}: {count}\n")
        sys.stdout.write("\nConfigured projects:\n")
        project_stats = collect_project_stats(cur, projects)
        for stat in project_stats:
            if stat["configured"]:
                print_project_stat(stat)
        sys.stdout.write("\nAll recognized projects:\n")
        for stat in project_stats:
            print_project_stat(stat)
        suspicious = [stat for stat in project_stats if project_stat_is_suspicious(stat)]
        sys.stdout.write("\nUI suspicious projects:\n")
        if suspicious:
            for stat in suspicious:
                print_project_stat(stat)
        else:
            sys.stdout.write("  none\n")
        extended = cur.execute(
            "select count(*) from threads where model_provider='custom' and substr(cwd,1,4)='\\\\?\\'",
        ).fetchone()[0]
        marked = cur.execute(
            "select count(*) from threads where model_provider='custom' and thread_source like 'api-visible-copy:%'",
        ).fetchone()[0]
        sys.stdout.write(f"\ncustom extended cwd rows: {extended}\n")
        sys.stdout.write(f"marked api-visible copies: {marked}\n")
    finally:
        con.close()


def load_project_roots(codex_home: Path) -> list[str]:
    path = codex_home / ".codex-global-state.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    atom = data.get("electron-persisted-atom-state", {})
    roots: list[str] = []
    for key in ("project-order", "electron-saved-workspace-roots", "active-workspace-roots"):
        extend_unique_strings(roots, data.get(key))
        extend_unique_strings(roots, atom.get(key))
    hints = data.get("thread-workspace-root-hints") or atom.get("thread-workspace-root-hints")
    if isinstance(hints, dict):
        extend_unique_strings(roots, hints.values())
    return roots


def extend_unique_strings(target: list[str], values: Any) -> None:
    if not isinstance(values, list) and not isinstance(values, tuple) and not isinstance(values, dict_values_type()):
        return
    for item in values:
        if isinstance(item, str) and item not in target:
            target.append(item)


def dict_values_type() -> type:
    return type({}.values())


def collect_project_stats(cur: sqlite3.Cursor, configured_projects: list[str]) -> list[dict[str, Any]]:
    projects: list[str] = []
    for project in configured_projects:
        add_project_root(projects, project)
    for (cwd,) in cur.execute("select distinct cwd from threads where coalesce(cwd,'') <> ''"):
        add_project_root(projects, plain_cwd(as_str(cwd)))
    stats = [project_stat(cur, project, project in configured_projects) for project in projects]
    return sorted(
        stats,
        key=lambda item: (not bool(item["configured"]), as_str(item["project"]).lower()),
    )


def add_project_root(projects: list[str], project: str) -> None:
    if not project:
        return
    plain = plain_cwd(project)
    if plain not in projects:
        projects.append(plain)


def project_stat(cur: sqlite3.Cursor, project: str, configured: bool) -> dict[str, Any]:
    plain_custom = count_threads(cur, "custom", project)
    ext_custom = count_threads(cur, "custom", normalize_cwd(project))
    openai_plain = count_threads(cur, "openai", project)
    openai_ext = count_threads(cur, "openai", normalize_cwd(project))
    user_like = count_user_like_threads(cur, "custom", project) + count_user_like_threads(
        cur,
        "custom",
        normalize_cwd(project),
    )
    return {
        "project": project,
        "display_name": project_display_name(project),
        "configured": configured,
        "custom": plain_custom + ext_custom,
        "plain": plain_custom,
        "extended": ext_custom,
        "openai": openai_plain + openai_ext,
        "user_like_custom": user_like,
    }


def print_project_stat(stat: dict[str, Any]) -> None:
    marker = "configured" if stat["configured"] else "discovered"
    sys.stdout.write(
        f"  {stat['display_name']} | {stat['project']} | {marker} | custom={stat['custom']} "
        f"(plain={stat['plain']}, extended={stat['extended']}) | "
        f"openai={stat['openai']} | user_like_custom={stat['user_like_custom']}\n",
    )


def project_stat_is_suspicious(stat: dict[str, Any]) -> bool:
    custom = int(stat["custom"])
    openai = int(stat["openai"])
    plain = int(stat["plain"])
    extended = int(stat["extended"])
    user_like = int(stat["user_like_custom"])
    if custom == 0 and openai == 0:
        return False
    if extended > 0:
        return True
    if openai > 0 and custom == 0:
        return True
    return custom > 0 and plain == 0 and user_like > 0


def project_display_name(project: str) -> str:
    cleaned = plain_cwd(project).rstrip("\\/")
    if not cleaned:
        return project or "(empty)"
    parts = re.split(r"[\\/]+", cleaned)
    return parts[-1] if parts and parts[-1] else cleaned


def count_threads(cur: sqlite3.Cursor, provider: str, cwd: str) -> int:
    return int(
        cur.execute(
            "select count(*) from threads where model_provider=? and cwd=?",
            (provider, cwd),
        ).fetchone()[0],
    )


def count_user_like_threads(cur: sqlite3.Cursor, provider: str, cwd: str) -> int:
    return int(
        cur.execute(
            """
            select count(*) from threads
            where model_provider=?
              and cwd=?
              and coalesce(first_user_message,'') <> ''
              and coalesce(model,'') <> 'codex-auto-review'
            """,
            (provider, cwd),
        ).fetchone()[0],
    )


def report_cwd_normalization(state_db: Path) -> None:
    con = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    try:
        cur = con.cursor()
        count = cur.execute(
            "select count(*) from threads where model_provider='custom' and substr(cwd,1,4)='\\\\?\\'",
        ).fetchone()[0]
        sys.stdout.write(f"DRY-RUN: {count} custom rows would have cwd normalized.\n")
    finally:
        con.close()


def normalize_custom_cwd(state_db: Path, update_jsonl: bool) -> None:
    con = sqlite3.connect(state_db)
    try:
        cur = con.cursor()
        rows = cur.execute("select id,cwd,rollout_path from threads where model_provider='custom'").fetchall()
        changed_db = 0
        changed_jsonl = 0
        for session_id, cwd, rollout_path in rows:
            if not isinstance(cwd, str):
                continue
            new_cwd = plain_cwd(cwd)
            if new_cwd != cwd:
                cur.execute("update threads set cwd=? where id=?", (new_cwd, session_id))
                changed_db += cur.rowcount
            if update_jsonl and isinstance(rollout_path, str):
                path = rollout_path_to_path(rollout_path)
                if path.exists() and rewrite_rollout_cwd(path, new_cwd):
                    changed_jsonl += 1
        con.commit()
        sys.stdout.write(f"Normalized SQLite rows: {changed_db}\n")
        sys.stdout.write(f"Normalized JSONL files: {changed_jsonl}\n")
    finally:
        con.close()


def rollout_path_to_path(path_text: str) -> Path:
    if path_text.startswith("\\\\?\\UNC\\"):
        return Path("\\\\" + path_text[8:])
    if path_text.startswith("\\\\?\\"):
        return Path(path_text[4:])
    return Path(path_text)


def rewrite_rollout_cwd(path: Path, cwd: str) -> bool:
    changed = False
    lines: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            lines.append(line)
            continue
        rewritten = replace_cwd(row, cwd)
        if rewritten != row:
            changed = True
        lines.append(json.dumps(rewritten, ensure_ascii=False, separators=(",", ":")))
    if changed:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return changed


def replace_cwd(value: Any, cwd: str) -> Any:
    if isinstance(value, list):
        return [replace_cwd(item, cwd) for item in value]
    if isinstance(value, dict):
        result = {key: replace_cwd(item, cwd) for key, item in value.items()}
        if "cwd" in result:
            result["cwd"] = cwd
        payload = result.get("payload")
        if result.get("type") == "session_meta" and isinstance(payload, dict):
            payload["cwd"] = cwd
        return result
    return value


def thread_value(item: RestoreCandidate, column: str) -> Any:
    values: dict[str, Any] = {
        "id": item.session_id,
        "rollout_path": str(item.target_path),
        "created_at": item.created_at,
        "updated_at": item.updated_at,
        "source": item.source,
        "model_provider": item.model_provider,
        "cwd": item.cwd,
        "title": item.title,
        "sandbox_policy": item.sandbox_policy,
        "approval_mode": item.approval_mode,
        "tokens_used": item.tokens_used,
        "has_user_event": item.has_user_event,
        "archived": item.archived,
        "archived_at": item.archived_at,
        "git_sha": item.git_sha,
        "git_branch": item.git_branch,
        "git_origin_url": item.git_origin_url,
        "cli_version": item.cli_version,
        "first_user_message": item.first_user_message,
        "agent_nickname": item.agent_nickname,
        "agent_role": item.agent_role,
        "memory_mode": item.memory_mode,
        "model": item.model,
        "reasoning_effort": item.reasoning_effort,
        "agent_path": item.agent_path,
        "created_at_ms": item.created_at_ms,
        "updated_at_ms": item.updated_at_ms,
        "thread_source": item.thread_source,
    }
    return values.get(column)


def print_summary(candidates: list[RestoreCandidate], dry_run: bool) -> None:
    mode = "DRY-RUN" if dry_run else "APPLY"
    sys.stdout.write(f"{mode}: {len(candidates)} missing sessions selected.\n")
    by_project: dict[str, int] = {}
    for item in candidates:
        by_project[item.cwd] = by_project.get(item.cwd, 0) + 1
    for cwd, count in sorted(by_project.items(), key=lambda pair: pair[0].lower()):
        sys.stdout.write(f"{count:4d}  {cwd}\n")
    for item in candidates[:10]:
        sys.stdout.write(f"  - {item.session_id} | {item.title} | {item.cwd}\n")
    if len(candidates) > 10:
        sys.stdout.write(f"  ... {len(candidates) - 10} more\n")


def extract_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        value = item.get("text")
        if isinstance(value, str) and value:
            parts.append(value)
    return "\n\n".join(parts)


def infer_title(text: str) -> str:
    first_line = re.sub(r"\s+", " ", text).strip()
    return first_line[:80]


def normalize_title(title: str, session_id: str) -> str:
    cleaned = re.sub(r"\s+", " ", title).strip()
    if not cleaned or is_internal_text(cleaned):
        return f"session-{session_id[:8]}"
    return cleaned[:80]


def is_internal_text(text: str) -> bool:
    stripped = text.strip()
    return any(stripped.startswith(prefix) for prefix in SKIP_TEXT_PREFIXES)


def normalize_cwd(cwd: str) -> str:
    if not cwd:
        return "\\\\?\\unknown-workspace"
    if cwd.startswith("\\\\?\\"):
        return cwd
    if cwd.startswith("\\\\"):
        return "\\\\?\\UNC\\" + cwd.lstrip("\\")
    return "\\\\?\\" + cwd


def plain_cwd(cwd: str) -> str:
    if cwd.startswith("\\\\?\\UNC\\"):
        return "\\\\" + cwd[8:]
    if cwd.startswith("\\\\?\\"):
        return cwd[4:]
    return cwd


def session_id_from_filename(path: Path) -> str:
    match = re.search(r"(019[a-z0-9-]{20,})", path.stem)
    return match.group(1) if match else ""


def source_group(path: Path) -> str:
    parts = path.parts
    for name in DEFAULT_SOURCE_DIRS:
        if name in parts:
            return name
    return "custom"


def seconds_from_iso(value: str) -> int | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def iso_from_seconds(value: int) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def millis_from_seconds(value: int) -> int:
    return value * 1000


def int_or_none(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def as_str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def nullable_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


if __name__ == "__main__":
    raise SystemExit(main())

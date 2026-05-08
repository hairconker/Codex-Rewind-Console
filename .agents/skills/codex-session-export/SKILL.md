---
name: codex-session-export
description: Export hidden or missing Codex Desktop chat records from .codex sessions, sessions.bak, and archived_sessions.bak into Markdown without modifying source files, or safely reapply missing records into the current Codex Desktop state without overwriting existing API-mode conversations. Use when the user says Codex history disappeared, API mode hid records, multiple projects lost records, or asks to export/backup/restore Codex conversations.
allowed-tools: Bash, Read, Grep, Glob
---

# Codex Session Export

Use this skill to recover visibility into Codex Desktop conversations that no longer show in the UI after account, provider, API mode, or project changes.

## Guardrails

- For export tasks, never edit, delete, rename, or restore files inside the source Codex data directory.
- Treat historical sources as read-only: `sessions.bak/`, `archived_sessions.bak/`, `session_index.jsonl.bak`, and `state_5.sqlite.bak`.
- For UI restore tasks, only insert missing thread ids into the current `state_5.sqlite`; never overwrite existing rows.
- Always create a backup before `--apply` restore.
- Export to a separate output directory unless the user explicitly asks to reapply records to the UI.
- Warn the user before sharing the export because raw sessions can contain paths, command output, secrets, or tool logs.

## Preferred Tool

When this skill is checked out inside a repository, use the bundled scripts under `.agents/skills/codex-session-export/scripts/`.

For Markdown export:

```powershell
python .agents\skills\codex-session-export\scripts\export_codex_sessions.py --output codex-session-export --copy-raw
```

For restoring hidden records into the current Codex Desktop UI, dry-run first:

```powershell
python .agents\skills\codex-session-export\scripts\restore_codex_sessions.py
```

Then apply only if the dry-run looks correct:

```powershell
python .agents\skills\codex-session-export\scripts\restore_codex_sessions.py --apply
```

If the current Codex Desktop is using API/custom provider mode and old provider records still do not show, create visible copies instead of changing originals:

```powershell
python .agents\skills\codex-session-export\scripts\restore_codex_sessions.py --api-visible-copy
python .agents\skills\codex-session-export\scripts\restore_codex_sessions.py --api-visible-copy --apply
```

If `--api-visible-copy` reports `0 missing sessions selected` but some old chats still do not appear, repair already-created API visible copies:

```powershell
python .agents\skills\codex-session-export\scripts\restore_codex_sessions.py --repair-api-visible-copies
python .agents\skills\codex-session-export\scripts\restore_codex_sessions.py --repair-api-visible-copies --apply
```

This fixes copied rows and copied JSONL files whose provider, cwd, title, or `thread_source` metadata were left in a half-converted state. It does not modify the original old-provider rows.

If the skill is installed globally, use the copy under `$env:CODEX_HOME\skills\codex-session-export\scripts\` or `%USERPROFILE%\.codex\skills\codex-session-export\scripts\`.

The equivalent project-level script is also available:

```powershell
python scripts\export_codex_sessions.py --output codex-session-export --copy-raw
```

## Workflow

1. List projects first when the user is unsure what is missing:

```powershell
python .agents\skills\codex-session-export\scripts\export_codex_sessions.py --list-only
```

2. Export all visible and backup session files:

```powershell
python .agents\skills\codex-session-export\scripts\export_codex_sessions.py --output codex-session-export --copy-raw
```

3. Export only one project when requested:

```powershell
python .agents\skills\codex-session-export\scripts\export_codex_sessions.py --project-filter "E:\work\商店小程序" --output codex-session-export-shop --copy-raw
```

4. Point the user to `index.md` in the output directory.

## Restore Workflow

Use this only when the user wants missing records to reappear in Codex Desktop.

1. Run a dry-run and read the selected count/projects:

```powershell
python .agents\skills\codex-session-export\scripts\restore_codex_sessions.py
```

2. If the selection is correct, apply:

```powershell
python .agents\skills\codex-session-export\scripts\restore_codex_sessions.py --apply
```

3. Verify dry-run is empty afterward:

```powershell
python .agents\skills\codex-session-export\scripts\restore_codex_sessions.py
```

4. Tell the user to restart Codex Desktop if the sidebar does not refresh.

Restore behavior:

- Copies missing JSONL files into current `sessions/YYYY/MM/DD/`.
- Inserts only missing `threads.id` rows into current `state_5.sqlite`.
- Appends only missing ids to `session_index.jsonl`.
- Backs up current state files under `.codex/restore_backups/<timestamp>/`.
- Preserves existing API-mode conversations by skipping ids already present in the current database.

API visible copy behavior:

- Keeps original old-provider rows unchanged.
- Creates a new legal thread id for each copy.
- Rewrites copied JSONL internals from original id to copy id.
- Sets copy `model_provider` to `custom`.
- Adds `[API可见副本]` to the copy title.
- Stores `thread_source = api-visible-copy:<original-id>`.

Repair behavior:

- Scans original `openai` rows and derives their deterministic custom copy ids.
- Updates only matching custom-provider copy rows.
- Normalizes copy `cwd` values to plain Windows paths so project filters can match them.
- Repairs copied JSONL metadata and leaves original JSONL files untouched.
- Skips unrelated custom/API-mode conversations created by the user.

## Output Shape

- `index.md`: root project index.
- `index.json`: machine-readable session index.
- `projects/<project-slug>/index.md`: per-project session list.
- `projects/<project-slug>/*.md`: readable conversation exports.
- `projects/<project-slug>/raw/*.jsonl`: original JSONL copies when `--copy-raw` is used.

## Validation

Run:

```powershell
python .agents\skills\codex-session-export\scripts\export_codex_sessions.py --help
python .agents\skills\codex-session-export\scripts\export_codex_sessions.py --list-only
```

For script edits, also run:

```powershell
python -m py_compile .agents\skills\codex-session-export\scripts\export_codex_sessions.py
python -m py_compile .agents\skills\codex-session-export\scripts\restore_codex_sessions.py
```

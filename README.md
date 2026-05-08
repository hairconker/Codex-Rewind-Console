# Codex Rewind Console

Codex Rewind Console is a local recovery dashboard for Codex Desktop session history.

It helps when conversations still exist on disk but do not appear in Codex Desktop after switching accounts, providers, API/custom mode, or projects.

## Features

- Read-only export of Codex session JSONL files to Markdown.
- Safe restore helpers for missing `state_5.sqlite` thread rows.
- API/custom-provider visible copies without modifying original `openai` records.
- Repair mode for half-converted API-visible copies.
- Local web dashboard at `http://127.0.0.1:8765/`.
- Project-level selection before running repair actions.
- Backup-only action before applying changes.
- No external Python dependencies.

## Safety Model

- Source session files are treated as read-only.
- Original old-provider rows are preserved.
- Existing API/custom conversations are not overwritten.
- Apply actions create backups under:

```text
%USERPROFILE%\.codex\restore_backups\
```

- The web server binds to `127.0.0.1` by default.
- The dashboard only exposes whitelisted actions; it does not run arbitrary commands.

## Quick Start

Clone the repository:

```powershell
git clone git@github.com:hairconker/Codex-Rewind-Console.git
cd Codex-Rewind-Console
```

Start the local dashboard:

```powershell
python scripts\codex_recovery_web.py --port 8765
```

Open:

```text
http://127.0.0.1:8765/
```

## Command Line Usage

Show the current Codex history state:

```powershell
python scripts\restore_codex_sessions.py --report
```

Export all detected sessions to Markdown and raw JSONL copies:

```powershell
python scripts\export_codex_sessions.py --output codex-session-export --copy-raw
```

Create a backup only:

```powershell
python scripts\restore_codex_sessions.py --backup-only
```

Preview API-visible copy repairs:

```powershell
python scripts\restore_codex_sessions.py --repair-api-visible-copies
```

Apply repairs after fully exiting Codex Desktop:

```powershell
python scripts\restore_codex_sessions.py --repair-api-visible-copies --apply
```

Repair only selected project paths:

```powershell
python scripts\restore_codex_sessions.py --repair-api-visible-copies --project-exact "E:\biji"
python scripts\restore_codex_sessions.py --repair-api-visible-copies --project-exact "E:\biji" --apply
```

## Important Workflow

If Codex Desktop is running, it may rewrite `state_5.sqlite` from its in-memory state. For final restore actions:

1. Use the dashboard or CLI to inspect and back up.
2. Fully exit Codex Desktop.
3. Run the apply command or use the dashboard apply action.
4. Reopen Codex Desktop.

## Skill

The bundled Codex skill is in:

```text
.agents/skills/codex-session-export/
```

Install it globally by copying the folder to:

```text
%USERPROFILE%\.codex\skills\codex-session-export\
```

## Repository Layout

```text
codex_recovery_dashboard.html
scripts/
  codex_recovery_web.py
  export_codex_sessions.py
  restore_codex_sessions.py
.agents/
  skills/
    codex-session-export/
docs/
  codex_session_recovery.md
```

## What Not To Commit

Do not commit exported sessions or raw JSONL recovery output. They may contain local paths, commands, tool logs, or secrets.

The provided `.gitignore` excludes common export and backup directories.

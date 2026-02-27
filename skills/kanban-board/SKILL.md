---
name: kanban-board
description: Manages project Kanban board for tracking work across sessions. Use when the user mentions "project", "kanban", "board", "track work", "create project", "move to in progress", "link session", "link PR", "what am I working on", "project status", or when starting significant new work that should be tracked. Also use proactively when creating PRs (to link them) or when a task transitions stages.
---

# Kanban Board — Project Tracking via `oc-kanban`

Track projects through stages: **pending → in_progress → pr → done**.
Each project can have linked OpenCode sessions and GitHub PRs.

## CLI Reference

The `oc-kanban` command is available on PATH. All output is grep-friendly (`key=value` format).

### List & View

```bash
# List all projects
oc-kanban list

# List projects in a specific stage
oc-kanban list --stage in_progress

# Show full details for a project
oc-kanban show <project_id>
```

### Create & Update

```bash
# Create a new project (defaults to "pending" stage)
oc-kanban create "Project title" --desc "Brief description"

# Create directly into a stage
oc-kanban create "Hotfix auth bug" --stage in_progress

# Create with tags
oc-kanban create "Refactor API" --tag backend --tag api

# Update fields
oc-kanban update <project_id> --title "New title"
oc-kanban update <project_id> --desc "Updated description"
```

### Move Between Stages

```bash
# Move to next stage
oc-kanban move <project_id> in_progress
oc-kanban move <project_id> pr
oc-kanban move <project_id> done

# Move back if needed
oc-kanban move <project_id> pending
```

Valid stages in order: `pending`, `in_progress`, `pr`, `done`

### Link Sessions & PRs

```bash
# Link the current OpenCode session to a project
oc-kanban link-session <project_id> <session_id>

# Link a GitHub PR to a project
oc-kanban link-pr <project_id> <pr_number>

# Unlink if needed
oc-kanban unlink-session <project_id> <session_id>
oc-kanban unlink-pr <project_id> <pr_number>
```

### Delete

```bash
oc-kanban delete <project_id>
```

## When to Use (Proactive Triggers)

### Starting New Work

When the user asks you to start a significant piece of work (not a trivial one-liner), create a project:

```bash
oc-kanban create "Feature: dark mode toggle" --desc "Add theme switching to settings" --stage in_progress
```

Then link the current session. The session ID is available from the environment — look for `ses_` prefixed IDs in the OpenCode context.

### Creating a PR

When you create a PR via `gh pr create`, also:

1. Move the project to `pr` stage
2. Link the PR number

```bash
oc-kanban move <project_id> pr
oc-kanban link-pr <project_id> <pr_number>
```

### Work Completed

When a PR is merged or work is confirmed done:

```bash
oc-kanban move <project_id> done
```

### Checking Status

When the user asks "what am I working on" or "project status":

```bash
oc-kanban list
```

## Output Format

All commands output `key=value` pairs for easy parsing:

```
id=a1b2c3d4  stage=in_progress  title=Refactor auth flow
  desc=Move to JWT tokens
  sessions=ses_abc123,ses_def456
  prs=42,43
  tags=backend,auth
  created=2026-02-27T10:00:00  updated=2026-02-27T14:30:00
```

## Data Storage

Projects are stored in `~/.local/share/oc-dashboard/kanban.json`. The backend is a thin adapter — can be swapped to JIRA or Trello MCP in the future without changing usage patterns.

## Integration with oc-dashboard TUI

The user can view the board visually by pressing `b` in the oc-dashboard TUI. The board shows all 4 columns with project cards, linked sessions, and PR status. The TUI and CLI share the same JSON file — changes from either side are immediately visible to the other.

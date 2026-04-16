# oc-dashboard

> **Human:** Point your AI agent at this README and tell it to install oc-dashboard.
>
> ```
> Install oc-dashboard: https://github.com/wheeyls/oc-dashboard
> ```
>
> That's it. Your agent handles the rest.

---

## Agent Instructions

You are installing **oc-dashboard**, a TUI for monitoring OpenCode sessions with a built-in Kanban board, archive, search, and a wheel for fast context-switching between active projects.

### Prerequisites

Verify these are available before proceeding. Do NOT install them — if missing, tell the user what's needed.

- **Python 3.9+** — `python3 --version`
- **OpenCode** — `~/.local/share/opencode/opencode.db` should exist (the user is running OpenCode if they're asking you to install this)
- **gh CLI** — `gh --version` (used for PR data)
- **tmux** — `tmux -V` (used to open sessions in split panes)
- **Nerd Font** — the user's terminal must use a [Nerd Font](https://www.nerdfonts.com/) (icons will render as boxes otherwise). Don't check for this — just mention it if the dashboard looks broken.

### Step 1: Clone and Install

```bash
git clone https://github.com/wheeyls/oc-dashboard.git ~/.local/share/oc-dashboard
cd ~/.local/share/oc-dashboard
python3 -m venv .venv
.venv/bin/pip install -e .
```

### Step 2: Add to PATH

```bash
ln -sf ~/.local/share/oc-dashboard/.venv/bin/oc-dashboard ~/.local/bin/oc-dashboard
ln -sf ~/.local/share/oc-dashboard/.venv/bin/oc-kanban ~/.local/bin/oc-kanban
```

Verify: `oc-kanban list` should return without error (empty output is fine).

If `~/.local/bin` is not on the user's PATH, tell them to add it:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

### Step 3: Install the Kanban Skill

The skill teaches OpenCode agents how to use the Kanban board automatically — creating projects, linking sessions, moving stages.

```bash
ln -sf ~/.local/share/oc-dashboard/skills/kanban-board ~/.config/opencode/skills/kanban-board
```

Create the skills directory first if it doesn't exist:

```bash
mkdir -p ~/.config/opencode/skills
```

Verify: the skill directory should contain `SKILL.md`:

```bash
ls ~/.config/opencode/skills/kanban-board/SKILL.md
```

### Step 4: Launch

Tell the user to run:

```bash
oc-dashboard
```

### Post-Install: Quick Orientation for the User

After installing, tell the user:

**The dashboard is a Kanban board** with three columns: Pending, In Progress, and Done. It monitors your OpenCode sessions, PRs, costs, and system health in real-time. Projects can be archived (with stage memory) and restored later. A search screen lets you find projects by title, description, session ID, PR number, or tag. The wheel gives you fast circular rotation through your most active projects.

**Keyboard shortcuts:**

| Key                 | Action                                     |
| ------------------- | ------------------------------------------ |
| `tab` / `shift+tab` | Move between columns                       |
| `j` / `k`           | Navigate within a column                   |
| `enter`             | Open linked session (creates one if none)  |
| `a`                 | Add a new project                          |
| `m` / `M`           | Move project right / left through stages   |
| `s`                 | Link a session to the selected project     |
| `u`                 | Unlink a session                           |
| `p`                 | Link a PR number                           |
| `d`                 | Archive a project                          |
| `A`                 | Open the archive (restore with `enter`)    |
| `/`                 | Search projects and sessions               |
| `n` / `N`           | Wheel: rotate next / prev and open session |
| `w` / `W`           | Wheel: add / remove selected project       |
| `S`                 | Open the sessions browser                  |
| `r`                 | Force refresh                              |
| `q`                 | Quit                                       |

**Tmux wheel integration (optional):** Add to `~/.tmux.conf` for global rotation from any pane:

```
bind-key n run-shell "oc-kanban wheel-go next"
bind-key N run-shell "oc-kanban wheel-go prev"
```

This lets you hit `prefix + n` to rotate to the next project and open its session — even from inside an active OpenCode session.

**The Kanban skill is now active.** When you start significant work in OpenCode, your agent will automatically create and track projects on the board. You can also ask your agent things like "what am I working on?" or "move my project to done."

## Updating

```bash
cd ~/.local/share/oc-dashboard && git pull
```

The `-e` (editable) pip install means `git pull` is sufficient — no reinstall needed unless dependencies change. If dependencies changed:

```bash
cd ~/.local/share/oc-dashboard && .venv/bin/pip install -e .
```

## License

MIT

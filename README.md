# oc-dashboard

Hacker-aesthetic TUI for monitoring [OpenCode](https://github.com/sst/opencode) sessions, PRs, costs, and system health.

![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-green) ![Textual](https://img.shields.io/badge/tui-textual-blue)

## Features

- **Session tree** — Sessions grouped by repo with fork nesting (`├─ / └─`), sorted by effective recency (child activity bubbles parents up)
- **Live COMMS feed** — Real-time event stream from OpenCode logs (bus events, VCS branch changes, errors)
- **CI failure alerts** — Failed PRs sort to top in bold red, topbar badge, error flash, COMMS event
- **Next Ops** — Priority-ranked recommendations: resume stalled sessions, request reviews, fix failing CI
- **Memory watchdog** — Kills runaway `opencode` processes exceeding 8GB RSS (SIGTERM → SIGKILL)
- **Cost tracking** — Per-session cost column + cumulative $ in topbar
- **Tmux integration** — Press Enter to open a session in a 25% tmux split pane
- **PR browser** — Press `o` to open a PR in your browser
- **Responsive layout** — Adapts to narrow viewports (hides panels, stacks vertically)
- **Auto-refresh** — SQLite WAL file watching triggers re-render on DB changes

## Install

```bash
# Clone and set up
git clone https://github.com/wheeyls/oc-dashboard.git
cd oc-dashboard
python3 -m venv .venv
.venv/bin/pip install -e .

# Symlink to PATH (optional)
ln -sf "$(pwd)/.venv/bin/oc-dashboard" ~/.local/bin/oc-dashboard
```

## Usage

```bash
oc-dashboard
```

### Keybindings

| Key       | Action                          |
| --------- | ------------------------------- |
| `j` / `k` | Navigate sessions (vim-style)   |
| `Enter`   | Open session in tmux split pane |
| `o`       | Open selected PR in browser     |
| `r`       | Force refresh                   |
| `q`       | Quit                            |

## Requirements

- Python 3.9+
- [OpenCode](https://github.com/sst/opencode) installed (`~/.local/share/opencode/opencode.db`)
- [Nerd Font](https://www.nerdfonts.com/) patched terminal font (icons use FA range U+F000–F2E0)
- `gh` CLI (for PR data)
- tmux (for session opening)

## Layout

```
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃  OC//DASH  15:48:23  3 LIVE  1 STALLED  $1,300            ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃  SESSIONS                   ┃  INTEL                      ┃
┃   ue                        ┃  ● LIVE │ Session title     ┃
┃  ├─  Session A         now  ┃  mem 1.2GB                  ┃
┃  │  └─  Session A (fork)    ┃  ── Todos (3/5) ──          ┃
┃  ├─  Session B         2h   ┃                             ┃
┃  └─  Session C         1d   ┃                             ┃
┃   spanish                   ┃                             ┃
┃  └─  Session D         3d   ┃                             ┃
┣━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━┻━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃  COMMS    ┃ NEXT OPS┃  PULL REQUESTS                      ┃
┃  events   ┃ actions ┃  #123  ✓  Title          Approved   ┃
┗━━━━━━━━━━━┻━━━━━━━━━┻━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
```

At narrow widths (<100 cols), Intel and Next Ops hide automatically; panels stack vertically.

## License

MIT

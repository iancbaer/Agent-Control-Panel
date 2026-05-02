# Local AI Control

A native GNOME/GTK desktop control panel for local AI agent stacks.

Local AI Control gives you one place to watch and manage the moving parts around a desktop agent setup: service status, Ollama models, automation jobs, token usage, memory services, logs, project tasks, and local note vault graph previews.

## Features

- Agent gateway/service status and quick restart/stop controls
- Ollama active model visibility and stop controls
- Automation/cron job overview with run, pause, resume, delete, and model override controls
- Token burn summaries and simple forward-looking automation estimates
- Local process and log views for agent-related services
- Lightweight project task tracker
- Obsidian-compatible local vault graph preview
- Desktop chat pane for an installed local agent CLI

## Requirements

- Linux desktop with GNOME/GTK 3
- Python 3
- Python packages: `PyGObject`, `PyYAML`, `pycairo`
- A local Hermes-style agent install under `~/.hermes` for the built-in service and automation controls
- Ollama for local model controls

On Debian/Ubuntu-style systems, the GTK dependencies are typically:

```bash
sudo apt install python3-gi gir1.2-gtk-3.0 python3-yaml python3-cairo
```

## Run from source

```bash
./launch-local-ai-control.sh
```

## Install as a GNOME app

Clone the repo, then install the launcher and icon into your user desktop locations:

```bash
git clone https://github.com/iancbaer/local-ai-control.git
cd local-ai-control
chmod +x launch-local-ai-control.sh local_ai_control.py

mkdir -p ~/.local/share/applications ~/.local/share/icons/hicolor/scalable/apps
install -m 644 io.github.iancbaer.LocalAIControl.desktop ~/.local/share/applications/io.github.iancbaer.LocalAIControl.desktop
install -m 644 local-ai-control.svg ~/.local/share/icons/hicolor/scalable/apps/local-ai-control.svg

python3 - <<'PY'
from pathlib import Path
app_dir = Path.cwd()
desktop = Path.home() / '.local/share/applications/io.github.iancbaer.LocalAIControl.desktop'
text = desktop.read_text()
text = text.replace('Exec=launch-local-ai-control.sh', f'Exec={app_dir}/launch-local-ai-control.sh')
desktop.write_text(text)
PY

update-desktop-database ~/.local/share/applications 2>/dev/null || true
gtk-update-icon-cache -q -t -f ~/.local/share/icons/hicolor 2>/dev/null || true
```

Launch it from GNOME as “Local AI Control” or run:

```bash
gtk-launch io.github.iancbaer.LocalAIControl
```

## Configuration notes

The app reads local state from `~/.hermes` and shells out to local commands such as `systemctl --user`, `ollama`, and the configured agent Python environment. If your agent install is elsewhere, set:

```bash
export HERMES_PYTHON=/path/to/hermes-agent/venv/bin/python
```

before launching.

## Project status

This is a practical desktop utility extracted from a working local setup. Some controls assume a Hermes-style directory layout, but the code is intentionally small enough to adapt for other local agent stacks.

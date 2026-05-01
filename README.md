# Rook Control

Native GNOME control panel for the local Hermes/Rook stack.

## What it does

- Shows local Hermes gateway, Ollama, Hindsight, cron jobs, token burn, and process status.
- Provides quick controls for restarting/stopping the Hermes gateway and stopping loaded Ollama models.
- Includes a desktop chat pane, automation/job view, local project quest tracker, and Obsidian vault graph preview.

## Requirements

- Linux desktop with GNOME/GTK 3
- Python 3
- Python packages: `PyGObject`, `PyYAML`, `pycairo`
- Hermes/Rook installed locally under `~/.hermes`
- Ollama if using local model controls

## Run

```bash
./launch-hermes-control.sh
```

## Install as a GNOME app

The current launcher uses the local path this app was built from. If you clone it elsewhere, update `launch-hermes-control.sh` and `local.hermes.Control.desktop` to point at the clone path, then install:

```bash
mkdir -p ~/.local/share/applications ~/.local/share/icons/hicolor/scalable/apps
install -m 755 launch-hermes-control.sh ./launch-hermes-control.sh
install -m 644 local.hermes.Control.desktop ~/.local/share/applications/local.hermes.Control.desktop
install -m 644 hermes-control.svg ~/.local/share/icons/hicolor/scalable/apps/rook-control.svg
update-desktop-database ~/.local/share/applications 2>/dev/null || true
gtk-update-icon-cache -q -t -f ~/.local/share/icons/hicolor 2>/dev/null || true
```

Launch with:

```bash
gtk-launch local.hermes.Control
```

## Notes

This is intentionally local-first. It reads local Hermes/Rook state from `~/.hermes` and does not ship credentials or runtime state.

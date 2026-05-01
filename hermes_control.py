#!/usr/bin/env python3
import json
import math
import os
import re
import sqlite3
import subprocess
import threading
from datetime import datetime, timedelta
from pathlib import Path

import gi
import yaml
import cairo

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk, Gdk

APP_ID = "local.hermes.Control"
ICON_NAME = "rook-control"


HOME = Path.home()
HERMES = HOME / ".hermes"
HERMES_SERVICE = "hermes-gateway.service"
DESKTOP_SESSION_FILE = HOME / ".hermes/desktop-control-session.json"
APP_DIR = Path(__file__).resolve().parent
ICON_PATH = APP_DIR / "hermes-control.svg"
HERMES_PYTHON = "/home/iancbaer/.hermes/hermes-agent/venv/bin/python"
PROJECTS_FILE = HERMES / "rook-projects.json"
OBSIDIAN_GRAPH_FILE = HERMES / "rook-obsidian-graph.png"
STATE_DB = HERMES / "state.db"


def run(command, timeout=8, cwd=None):
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
            cwd=cwd,
        )
        return result.returncode == 0, result.stdout.strip(), result.stderr.strip()
    except Exception as exc:
        return False, "", str(exc)


def shell(command, timeout=8):
    return run(["/bin/bash", "-lc", command], timeout=timeout)


def read_json(path, fallback):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return fallback


def read_config():
    try:
        return yaml.safe_load((HERMES / "config.yaml").read_text()) or {}
    except Exception:
        return {}


def load_project_tasks():
    data = read_json(PROJECTS_FILE, {"tasks": []})
    tasks = data.get("tasks", []) if isinstance(data, dict) else []
    return [task for task in tasks if isinstance(task, dict)]


def save_project_tasks(tasks):
    PROJECTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    PROJECTS_FILE.write_text(json.dumps({"tasks": tasks}, indent=2) + "\n")


def discover_obsidian_vaults():
    candidates = []
    for root in [
        HOME / "Documents/Obsidian Vault",
        HOME / "Documents/99_Archives/Other/11_Desktop/Desktop-Archive/Rook-Brain",
        HOME / "Documents/01_Business/Rook",
        HOME / "wiki",
    ]:
        if root.exists() and any(root.rglob("*.md")):
            candidates.append(root)
    for base in [HOME / "Documents", HOME / "workspace", HOME / "wiki"]:
        if not base.exists():
            continue
        try:
            for obsidian_dir in base.rglob(".obsidian"):
                vault = obsidian_dir.parent
                if vault.exists():
                    candidates.append(vault)
        except Exception:
            pass
    seen = set()
    vaults = []
    for path in candidates:
        resolved = str(path.resolve())
        if resolved not in seen:
            seen.add(resolved)
            vaults.append(path)
    return vaults


def build_obsidian_graph(vault, limit=90):
    vault = Path(vault)
    files = [p for p in vault.rglob("*.md") if ".obsidian" not in p.parts]
    files = sorted(files, key=lambda p: len(str(p)))[:limit]
    names = {}
    for idx, path in enumerate(files):
        names[path.stem.lower()] = idx
        rel_no_suffix = str(path.relative_to(vault).with_suffix("")).replace("\\", "/").lower()
        names[rel_no_suffix] = idx
    nodes = []
    for p in files:
        rel = str(p.relative_to(vault))
        parts = Path(rel).parts
        nodes.append(
            {
                "name": p.stem,
                "path": rel,
                "full_path": str(p),
                "group": parts[0] if len(parts) > 1 else "Root",
                "degree": 0,
            }
        )
    edges = set()
    wiki_re = re.compile(r"\[\[([^\]#|]+)")
    md_re = re.compile(r"\[[^\]]+\]\(([^)#]+)")
    for idx, path in enumerate(files):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")[:60000]
        except Exception:
            continue
        targets = set(wiki_re.findall(text))
        targets.update(Path(match).stem for match in md_re.findall(text) if match.endswith(".md"))
        for target in targets:
            target_text = target.strip().replace("\\", "/").strip("/")
            candidates = [target_text.lower(), Path(target_text).stem.lower()]
            for candidate in candidates:
                if candidate in names and names[candidate] != idx:
                    edge = tuple(sorted((idx, names[candidate])))
                    edges.add(edge)
                    break
    for a, b in edges:
        nodes[a]["degree"] += 1
        nodes[b]["degree"] += 1
    return {"vault": str(vault), "nodes": nodes, "edges": sorted(edges)}


def merge_obsidian_graphs(graphs):
    nodes = []
    edges = set()
    index = {}
    for graph in graphs:
        vault_name = Path(graph.get("vault", "")).name or "Vault"
        for node in graph.get("nodes", []):
            key = f"{vault_name}:{node.get('path') or node.get('name')}"
            if key not in index:
                index[key] = len(nodes)
                nodes.append(
                    {
                        "name": f"{vault_name} / {node.get('name', '')}",
                        "path": node.get("path", ""),
                        "full_path": node.get("full_path", ""),
                        "group": vault_name,
                        "degree": 0,
                    }
                )
        source_nodes = graph.get("nodes", [])
        for a, b in graph.get("edges", []):
            if a >= len(source_nodes) or b >= len(source_nodes):
                continue
            ka = f"{vault_name}:{source_nodes[a].get('path') or source_nodes[a].get('name')}"
            kb = f"{vault_name}:{source_nodes[b].get('path') or source_nodes[b].get('name')}"
            if ka in index and kb in index:
                edges.add(tuple(sorted((index[ka], index[kb]))))
    for a, b in edges:
        nodes[a]["degree"] += 1
        nodes[b]["degree"] += 1
    return {"vault": "Combined", "nodes": nodes, "edges": sorted(edges)}


def tail(path, lines=80):
    if not Path(path).exists():
        return ""
    ok, out, err = run(["tail", "-n", str(lines), str(path)], timeout=5)
    return out if ok else err


def parse_systemctl_show(text):
    fields = {}
    for line in text.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            fields[key] = value
    return fields


def service_status():
    ok, out, err = run(
        [
            "systemctl",
            "--user",
            "show",
            HERMES_SERVICE,
            "--property=ActiveState,SubState,MainPID,NTasks,MemoryCurrent,CPUUsageNSec,ExecMainStartTimestamp",
            "--no-pager",
        ]
    )
    fields = parse_systemctl_show(out)
    return {
        "ok": ok,
        "active": fields.get("ActiveState", "unknown"),
        "sub": fields.get("SubState", "unknown"),
        "pid": int(fields.get("MainPID") or 0),
        "tasks": int(fields.get("NTasks") or 0),
        "memory": int(fields.get("MemoryCurrent") or 0),
        "cpu_ns": int(fields.get("CPUUsageNSec") or 0),
        "started": fields.get("ExecMainStartTimestamp", ""),
        "error": err,
    }


def parse_ollama_ps(text):
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) <= 1:
        return []
    models = []
    for line in lines[1:]:
        parts = re.split(r"\s{2,}", line.strip())
        models.append(
            {
                "name": parts[0] if len(parts) > 0 else line.strip(),
                "id": parts[1] if len(parts) > 1 else "",
                "size": parts[2] if len(parts) > 2 else "",
                "processor": parts[3] if len(parts) > 3 else "",
                "context": parts[4] if len(parts) > 4 else "",
                "until": parts[5] if len(parts) > 5 else "",
                "raw": line,
            }
        )
    return models


def ollama_state():
    ok, ps, err = run(["ollama", "ps"], timeout=6)
    ok_ver, version, _ = run(["ollama", "--version"], timeout=4)
    return {
        "ok": ok,
        "version": version if ok_ver else "",
        "active": parse_ollama_ps(ps),
        "error": err,
    }


def process_list():
    ok, out, _ = shell(
        "ps -eo pid=,ppid=,stat=,etime=,pcpu=,pmem=,comm=,args= "
        "| rg -i 'hermes-agent|hindsight-api|ollama runner|ollama serve|hindsight-embed-hermes' || true",
        timeout=5,
    )
    rows = []
    for line in out.splitlines():
        match = re.match(r"\s*(\d+)\s+(\d+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(.*)", line)
        if not match:
            continue
        pid, ppid, stat, etime, cpu, mem, comm, args = match.groups()
        if comm in {"bash", "sh", "rg"}:
            continue
        rows.append(
            {
                "pid": int(pid),
                "ppid": int(ppid),
                "stat": stat,
                "etime": etime,
                "cpu": float(cpu),
                "mem": float(mem),
                "comm": comm,
                "args": args,
            }
        )
    return rows


def cron_jobs():
    data = read_json(HERMES / "cron/jobs.json", {"jobs": []})
    jobs = []
    for job in data.get("jobs", []):
        jobs.append(
            {
                "id": job.get("id", ""),
                "name": job.get("name", ""),
                "enabled": bool(job.get("enabled")),
                "state": job.get("state", ""),
                "schedule": job.get("schedule_display") or job.get("schedule", {}).get("display", ""),
                "next": job.get("next_run_at", ""),
                "last": job.get("last_run_at", ""),
                "status": job.get("last_status", ""),
                "error": job.get("last_error", ""),
                "deliver": job.get("deliver", ""),
                "provider": job.get("provider") or "",
                "model": job.get("model") or "",
                "schedule_data": job.get("schedule") if isinstance(job.get("schedule"), dict) else {},
                "prompt": job.get("prompt") or job.get("task") or "",
            }
        )
    return jobs


def session_tokens(session):
    return sum(
        int(session.get(key) or 0)
        for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens")
    )


def fmt_tokens(value):
    value = int(value or 0)
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 10_000:
        return f"{value / 1_000:.0f}k"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return f"{value:,}"


def fmt_money(value):
    value = float(value or 0)
    if value <= 0:
        return "$0"
    if value < 1:
        return f"${value:.2f}"
    return f"${value:,.2f}"


def rough_tokens(text):
    return max(0, math.ceil(len(str(text or "")) / 4))


def parse_iso_datetime(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone()
    except Exception:
        return None


def expand_cron_field(field, minimum, maximum):
    values = set()
    field = str(field or "*")
    for part in field.split(","):
        part = part.strip()
        if not part:
            continue
        step = 1
        if "/" in part:
            part, step_text = part.split("/", 1)
            try:
                step = max(1, int(step_text))
            except ValueError:
                step = 1
        if part == "*":
            start, end = minimum, maximum
        elif "-" in part:
            try:
                start, end = [int(piece) for piece in part.split("-", 1)]
            except ValueError:
                continue
        else:
            try:
                start = end = int(part)
            except ValueError:
                continue
        for value in range(max(minimum, start), min(maximum, end) + 1, step):
            values.add(value)
    return sorted(values)


def forecast_job_runs(job, hours):
    if not job.get("enabled"):
        return 0
    now = datetime.now().astimezone()
    end = now + timedelta(hours=hours)
    schedule = job.get("schedule_data") or {}
    kind = schedule.get("kind")
    next_run = parse_iso_datetime(job.get("next"))
    if kind == "once":
        return int(bool(next_run and now < next_run <= end))
    if kind == "interval":
        minutes = int(schedule.get("minutes") or 0)
        if minutes <= 0:
            return int(bool(next_run and now < next_run <= end))
        first = next_run if next_run and next_run > now else now + timedelta(minutes=minutes)
        if first > end:
            return 0
        return 1 + int((end - first).total_seconds() // (minutes * 60))
    if kind == "cron":
        parts = str(schedule.get("expr") or "").split()
        if len(parts) < 5:
            return int(bool(next_run and now < next_run <= end))
        minutes = expand_cron_field(parts[0], 0, 59)
        hours_of_day = expand_cron_field(parts[1], 0, 23)
        if not minutes or not hours_of_day:
            return int(bool(next_run and now < next_run <= end))
        count = 0
        day = now.date()
        while datetime.combine(day, datetime.min.time()).replace(tzinfo=now.tzinfo) <= end:
            for hour in hours_of_day:
                for minute in minutes:
                    candidate = datetime.combine(day, datetime.min.time()).replace(
                        hour=hour,
                        minute=minute,
                        tzinfo=now.tzinfo,
                    )
                    if now < candidate <= end:
                        count += 1
            day += timedelta(days=1)
        return count
    return int(bool(next_run and now < next_run <= end))


def load_usage_sessions(days=30):
    if not STATE_DB.exists():
        return []
    cutoff = datetime.now().timestamp() - days * 86400
    query = """
        SELECT id, source, model, billing_provider, billing_mode, message_count,
               tool_call_count, input_tokens, output_tokens, cache_read_tokens,
               cache_write_tokens, reasoning_tokens, estimated_cost_usd,
               actual_cost_usd, cost_status, started_at, ended_at, api_call_count
        FROM sessions
        WHERE COALESCE(ended_at, started_at) >= ?
        ORDER BY COALESCE(ended_at, started_at) DESC
    """
    rows = []
    try:
        with sqlite3.connect(str(STATE_DB), timeout=1) as conn:
            conn.row_factory = sqlite3.Row
            for row in conn.execute(query, (cutoff,)):
                item = dict(row)
                item["total_tokens"] = session_tokens(item)
                match = re.match(r"cron_([A-Za-z0-9]+)_", item.get("id") or "")
                item["job_id"] = match.group(1) if match else ""
                rows.append(item)
    except Exception:
        return []
    return rows


def token_burn_state(jobs):
    sessions = load_usage_sessions(days=30)
    now = datetime.now().timestamp()
    periods = {
        "24h": now - 86400,
        "7d": now - 7 * 86400,
        "30d": now - 30 * 86400,
    }
    totals = {}
    for name, cutoff in periods.items():
        scoped = [s for s in sessions if float(s.get("ended_at") or s.get("started_at") or 0) >= cutoff]
        totals[name] = {
            "sessions": len(scoped),
            "tokens": sum(s["total_tokens"] for s in scoped),
            "input": sum(int(s.get("input_tokens") or 0) for s in scoped),
            "output": sum(int(s.get("output_tokens") or 0) for s in scoped),
            "cache": sum(int(s.get("cache_read_tokens") or 0) + int(s.get("cache_write_tokens") or 0) for s in scoped),
            "reasoning": sum(int(s.get("reasoning_tokens") or 0) for s in scoped),
            "cost": sum(float(s.get("estimated_cost_usd") or s.get("actual_cost_usd") or 0) for s in scoped),
        }
    automation_sessions = [s for s in sessions if s.get("source") == "cron" or s.get("job_id")]
    automation_ids = {s.get("id") for s in automation_sessions}
    interactive_sessions = [s for s in sessions if s.get("id") not in automation_ids]
    by_job = {}
    for session in automation_sessions:
        job_id = session.get("job_id")
        if not job_id:
            continue
        by_job.setdefault(job_id, []).append(session)
    forecasts = []
    for job in jobs:
        history = by_job.get(job["id"], [])
        totals_history = [s["total_tokens"] for s in history if s.get("total_tokens")]
        if totals_history:
            avg_tokens = int(sum(totals_history) / len(totals_history))
            basis = f"{len(totals_history)} runs"
        else:
            avg_tokens = rough_tokens(job.get("prompt")) + 2500
            basis = "prompt estimate"
        runs_24h = forecast_job_runs(job, 24)
        runs_7d = forecast_job_runs(job, 24 * 7)
        last = history[0] if history else {}
        forecasts.append(
            {
                "id": job["id"],
                "name": job["name"] or job["id"],
                "enabled": job["enabled"],
                "model": f"{job['provider']} {job['model']}".strip() or "default",
                "avg_tokens": avg_tokens,
                "basis": basis,
                "runs_24h": runs_24h,
                "runs_7d": runs_7d,
                "forecast_24h": avg_tokens * runs_24h,
                "forecast_7d": avg_tokens * runs_7d,
                "last_tokens": last.get("total_tokens", 0),
                "last_at": last.get("ended_at") or last.get("started_at"),
            }
        )
    return {
        "totals": totals,
        "sessions": sessions,
        "automation_tokens_30d": sum(s["total_tokens"] for s in automation_sessions),
        "interactive_tokens_30d": sum(s["total_tokens"] for s in interactive_sessions),
        "forecasts": sorted(forecasts, key=lambda row: row["forecast_7d"], reverse=True),
    }


def sessions_state():
    return read_json(HERMES / "sessions/sessions.json", {})


def hindsight_state():
    config = read_json(HERMES / "hindsight/config.json", {})
    log = tail(HOME / ".hindsight/profiles/hermes.log", 90)
    stats = ""
    tasks = []
    for line in log.splitlines():
        if "[WORKER_STATS]" in line:
            stats = line
        if "[WORKER_TASK]" in line:
            tasks.append(line.split("[WORKER_TASK]", 1)[-1].strip())
    return {
        "config": config,
        "log": log,
        "stats": stats,
        "tasks": tasks[-8:],
        "daemon": next((p for p in process_list() if "hindsight-api" in p["args"]), None),
    }


def collect_state():
    hermes = service_status()
    ollama = ollama_state()
    processes = process_list()
    jobs = cron_jobs()
    token_burn = token_burn_state(jobs)
    hindsight = hindsight_state()
    config = read_config()
    logs = {
        "Agent": tail(HERMES / "logs/agent.log", 100),
        "Errors": tail(HERMES / "logs/errors.log", 90),
        "Hindsight": hindsight["log"],
    }
    incidents = []
    now = datetime.now().strftime("%H:%M:%S")
    runner = next((p for p in processes if "ollama runner" in p["args"]), None)
    if runner:
        incidents.append(("High", now, "Ollama runner active", f"PID {runner['pid']} · {runner['etime']} · CPU {runner['cpu']}%"))
    active_models = [m["name"] for m in ollama["active"]]
    for model in active_models:
        if re.search(r"(7b|8b|27b|70b)", model, re.I):
            incidents.append(("High", now, "Large local model loaded", model))
    h_model = hindsight["config"].get("llm_model", "")
    if re.search(r"(7b|8b|27b|70b)", h_model, re.I):
        incidents.append(("High", now, "Hindsight large-model drift", h_model))
    if hermes["active"] != "active":
        incidents.append(("High", now, "Hermes gateway inactive", f"{hermes['active']}/{hermes['sub']}"))
    for job in [j for j in jobs if j["enabled"] and j["status"] == "error"][:6]:
        incidents.append(("Medium", job["last"] or now, f"Cron error: {job['name']}", job["error"] or "last_status=error"))
    return {
        "time": datetime.now(),
        "hermes": hermes,
        "ollama": ollama,
        "processes": processes,
        "jobs": jobs,
        "token_burn": token_burn,
        "sessions": sessions_state(),
        "hindsight": hindsight,
        "config": config,
        "logs": logs,
        "incidents": incidents,
    }


def fmt_bytes(value):
    if not value:
        return "-"
    units = ["B", "KB", "MB", "GB"]
    value = float(value)
    idx = 0
    while value >= 1024 and idx < len(units) - 1:
        value /= 1024
        idx += 1
    return f"{value:.1f} {units[idx]}" if idx else f"{value:.0f} B"


def short_time(value):
    if not value:
        return "-"
    try:
        value = value.replace("Z", "+00:00")
        return datetime.fromisoformat(value).strftime("%b %d %H:%M")
    except Exception:
        return value[:16]


def short_unix_time(value):
    if not value:
        return "-"
    try:
        return datetime.fromtimestamp(float(value)).strftime("%b %d %H:%M")
    except Exception:
        return "-"


class HermesControl(Gtk.Application):
    def __init__(self):
        GLib.set_application_name("Rook Control")
        GLib.set_prgname(APP_ID)
        try:
            Gtk.Window.set_default_icon_name(ICON_NAME)
            Gtk.Window.set_default_icon_from_file(str(ICON_PATH))
        except Exception:
            pass
        super().__init__(application_id=APP_ID)
        self.window = None
        self.state = None
        self.refreshing = False
        self.chat_proc = None
        self.job_rows = {}
        self.selected_job_id = None
        self.token_overview_card = None
        self.token_forecast_card = None
        self.token_recent_card = None
        self.token_forecast_tree = None
        self.token_recent_tree = None
        self.token_status_label = None
        self.model_provider_combo = None
        self.model_name_combo = None
        self.model_status_label = None
        self.log_selector = None
        self.log_view = None
        self.latest_logs = {}
        self.project_tree = None
        self.project_rows = {}
        self.selected_project_id = None
        self.project_selected_label = None
        self.obsidian_vault_combo = None
        self.obsidian_status_label = None
        self.obsidian_canvas = None
        self.obsidian_graph_image = None
        self.obsidian_note_tree = None
        self.obsidian_note_title = None
        self.obsidian_note_preview = None
        self.obsidian_vaults = []
        self.obsidian_graph = {"nodes": [], "edges": []}
        self.obsidian_positions = []
        self.selected_obsidian_node = None
        self.obsidian_selection_syncing = False
        self.obsidian_graph_size = (1100, 720)
        self.obsidian_zoom = 1.0
        self.obsidian_offset = (0.0, 0.0)
        self.obsidian_dragging = False
        self.obsidian_moved = False
        self.obsidian_last_drag = None

    def do_activate(self):
        if self.window:
            self.window.present()
            return
        self.window = Gtk.ApplicationWindow(application=self)
        self.window.set_title("Rook Control")
        self.window.set_default_size(1180, 820)
        self.window.set_position(Gtk.WindowPosition.CENTER)
        try:
            self.window.set_icon_name(ICON_NAME)
            self.window.set_icon_from_file(str(ICON_PATH))
        except Exception:
            pass
        self.apply_css()
        self.build_ui()
        self.window.show_all()
        self.refresh()
        GLib.timeout_add_seconds(8, self.refresh)

    def apply_css(self):
        css = b"""
        window { background: #111318; color: #d7dde5; }
        .top { background: #151820; border-bottom: 1px solid #2a303a; padding: 18px; }
        .brand-mark { background: #111318; border: 1px solid #3a4655; border-radius: 10px; padding: 6px; }
        .title { font-size: 25px; font-weight: 700; color: #eef2f6; }
        .muted { color: #8b95a3; }
        .card { background: #171b22; border: 1px solid #2d3540; border-radius: 10px; padding: 14px; }
        .conversation { background: #171b22; border: 1px solid #3a4655; border-radius: 10px; padding: 14px; }
        .chat-title { font-size: 19px; font-weight: 700; color: #eef2f6; }
        .chat-meta { color: #8b95a3; font-size: 12px; }
        .chat-shell { background: #111318; border: 1px solid #2d3540; border-radius: 10px; }
        .section { font-weight: 700; font-size: 12px; letter-spacing: 0; color: #d7dde5; }
        .metric-label { color: #8b95a3; font-size: 12px; }
        .metric-value { font-size: 22px; font-weight: 700; color: #eef2f6; }
        .good { color: #6db28a; }
        .warn { color: #c3a35b; }
        .bad { color: #c46f78; }
        button { border-radius: 8px; padding: 8px 12px; border: 1px solid #3a4655; background: #1c222b; color: #d7dde5; }
        button:hover { border-color: #58708c; background: #232b36; }
        button.primary { background: #24364a; color: #eef2f6; border-color: #58708c; }
        button.danger { color: #d68a92; border-color: #69434b; }
        button.subtle { color: #a1aab6; }
        stackswitcher button { border-radius: 8px; padding: 8px 12px; }
        textview { font-size: 12px; }
        .log-view, .log-view text { background: #101216; color: #c9d1db; font-family: monospace; }
        .chat-input, .chat-input text { background: #111318; color: #d7dde5; font-size: 14px; }
        .chat-output, .chat-output text { background: #111318; color: #d7dde5; font-size: 13px; }
        treeview { background: #111318; color: #d7dde5; }
        treeview:selected { background: #24364a; color: #eef2f6; }
        notebook header { background: #111318; border-color: #2d3540; }
        notebook tab { padding: 10px 18px; border-radius: 8px; border: 1px solid #2d3540; background: #151820; color: #8b95a3; }
        notebook tab:checked { background: #1c222b; color: #eef2f6; border-bottom-color: #58708c; }
        entry, combobox, combobox entry { background: #111318; color: #d7dde5; border-color: #2d3540; }
        """
        provider = Gtk.CssProvider()
        provider.load_from_data(css)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

    def label(self, text="", css=None, xalign=0):
        widget = Gtk.Label(label=text, xalign=xalign)
        widget.set_ellipsize(3)
        if css:
            widget.get_style_context().add_class(css)
        return widget

    def build_ui(self):
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.window.add(root)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        header.get_style_context().add_class("top")
        header.set_hexpand(True)
        root.pack_start(header, False, False, 0)

        mark_frame = Gtk.Frame()
        mark_frame.get_style_context().add_class("brand-mark")
        mark_frame.set_shadow_type(Gtk.ShadowType.NONE)
        mark = Gtk.Image.new_from_file(str(ICON_PATH))
        mark.set_pixel_size(44)
        mark_frame.add(mark)
        header.pack_start(mark_frame, False, False, 0)

        main_stack = Gtk.Stack()
        main_stack.set_transition_type(Gtk.StackTransitionType.NONE)
        switcher = Gtk.StackSwitcher()
        switcher.set_stack(main_stack)
        header.pack_start(switcher, False, False, 0)

        title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        title_box.set_hexpand(True)
        header.pack_start(title_box, True, True, 0)
        title = self.label("Rook Control", "title")
        subtitle = self.label("Local agent console", "muted")
        self.updated_label = self.label("Loading", "muted")
        title_box.pack_start(title, False, False, 0)
        title_box.pack_start(subtitle, False, False, 0)
        title_box.pack_start(self.updated_label, False, False, 0)

        root.pack_start(main_stack, True, True, 0)

        chat_content = self.page_container()
        self.speak_card = self.conversation_section()
        chat_content.content_box.pack_start(self.speak_card, True, True, 0)
        self.build_speak_card()
        main_stack.add_titled(chat_content, "chat", "Chat")

        automation_content = self.page_container()
        self.automation_card = self.card("Automation")
        automation_content.content_box.pack_start(self.automation_card, True, True, 0)
        self.jobs_tree = self.tree(["Name", "Status", "Schedule", "Next", "Model"], data_columns=1)
        self.configure_jobs_tree()
        self.automation_card.content_box.pack_start(self.automation_page(), True, True, 0)
        main_stack.add_titled(automation_content, "automation", "Automation")

        token_content = self.page_container()
        token_grid = Gtk.Grid(column_spacing=14, row_spacing=14)
        token_content.content_box.pack_start(token_grid, True, True, 0)
        self.token_overview_card = self.card("Token Burn")
        self.token_forecast_card = self.card("Automation Forecast")
        self.token_recent_card = self.card("Recent Usage")
        self.token_forecast_tree = self.tree(["Job", "Avg / Fire", "Last", "24h Runs", "24h Burn", "7d Burn", "Model"])
        self.token_recent_tree = self.tree(["When", "Source", "Tokens", "In", "Out", "Cache", "Model"])
        self.token_forecast_card.content_box.pack_start(self.wrap(self.token_forecast_tree), True, True, 0)
        self.token_recent_card.content_box.pack_start(self.wrap(self.token_recent_tree), True, True, 0)
        token_grid.attach(self.token_overview_card, 0, 0, 2, 1)
        token_grid.attach(self.token_forecast_card, 0, 1, 1, 1)
        token_grid.attach(self.token_recent_card, 1, 1, 1, 1)
        main_stack.add_titled(token_content, "tokens", "Token Burn")

        projects_content = self.page_container()
        self.projects_card = self.card("Ian's Projects")
        projects_content.content_box.pack_start(self.projects_card, True, True, 0)
        self.project_tree = self.tree(["Quest", "Status", "Area", "Priority", "Due"], data_columns=1)
        self.configure_project_tree()
        self.projects_card.content_box.pack_start(self.projects_page(), True, True, 0)
        main_stack.add_titled(projects_content, "projects", "Projects")

        obsidian_content = self.page_container()
        self.obsidian_card = self.card("Obsidian Zettelkasten")
        obsidian_content.content_box.pack_start(self.obsidian_card, True, True, 0)
        self.build_obsidian_page()
        main_stack.add_titled(obsidian_content, "obsidian", "Obsidian")

        system_content = self.page_container()
        status_grid = Gtk.Grid(column_spacing=14, row_spacing=14)
        system_content.content_box.pack_start(status_grid, False, False, 0)
        self.model_card = self.card("Default Model")
        self.agent_card = self.card("Rook Status")
        self.resources_card = self.card("Local Resources")
        self.memory_card = self.card("Memory")
        self.gateway_card = self.card("Gateway Controls")
        status_grid.attach(self.model_card, 0, 0, 1, 1)
        status_grid.attach(self.agent_card, 1, 0, 1, 1)
        status_grid.attach(self.resources_card, 2, 0, 1, 1)
        status_grid.attach(self.gateway_card, 0, 1, 1, 1)
        status_grid.attach(self.memory_card, 1, 1, 1, 1)
        self.build_model_card()
        self.build_gateway_card()

        self.incident_card = self.card("Incident Timeline")
        status_grid.attach(self.incident_card, 2, 1, 1, 1)

        system_grid = Gtk.Grid(column_spacing=14, row_spacing=14)
        system_content.content_box.pack_start(system_grid, True, True, 0)
        self.process_card = self.card("Processes")
        self.process_tree = self.tree(["PID", "Process", "Elapsed", "CPU", "Command"])
        self.process_card.content_box.pack_start(self.wrap(self.process_tree), True, True, 0)
        system_grid.attach(self.process_card, 0, 0, 1, 1)

        self.logs_card = self.card("Logs")
        log_toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        log_toolbar.pack_start(self.label("Stream", "metric-label"), False, False, 0)
        self.log_selector = Gtk.ComboBoxText()
        for name in ["Agent", "Errors", "Hindsight"]:
            self.log_selector.append_text(name)
        self.log_selector.set_active(0)
        self.log_selector.connect("changed", lambda *_: self.render_selected_log())
        log_toolbar.pack_start(self.log_selector, False, False, 0)
        self.logs_card.content_box.pack_start(log_toolbar, False, False, 0)
        self.log_view = Gtk.TextView()
        self.log_view.set_editable(False)
        self.log_view.set_monospace(True)
        self.log_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.log_view.get_style_context().add_class("log-view")
        self.logs_card.content_box.pack_start(self.wrap(self.log_view), True, True, 0)
        system_grid.attach(self.logs_card, 1, 0, 1, 1)
        main_stack.add_titled(system_content, "system", "System Control")
        self.render_project_tasks()
        self.reload_obsidian_graph()

    def page_container(self):
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        box.set_margin_top(14)
        box.set_margin_bottom(14)
        box.set_margin_start(18)
        box.set_margin_end(18)
        scroller.add(box)
        scroller.content_box = box
        return scroller

    def build_gateway_card(self):
        box = self.gateway_card.content_box
        for text, css, callback in [
            ("Refresh", "subtle", lambda *_: self.refresh()),
            ("Restart Gateway", "primary", lambda *_: self.run_action(["systemctl", "--user", "restart", HERMES_SERVICE])),
            ("Stop Gateway", "danger", lambda *_: self.run_action(["systemctl", "--user", "stop", HERMES_SERVICE])),
        ]:
            button = Gtk.Button(label=text)
            button.get_style_context().add_class(css)
            button.connect("clicked", callback)
            box.pack_start(button, False, False, 0)

    def card(self, heading):
        frame = Gtk.Frame()
        frame.get_style_context().add_class("card")
        frame.set_hexpand(True)
        frame.set_vexpand(True)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.pack_start(self.label(heading, "section"), False, False, 0)
        frame.add(box)
        frame.content_box = box
        return frame

    def conversation_section(self):
        frame = Gtk.Frame()
        frame.get_style_context().add_class("conversation")
        frame.set_hexpand(True)
        frame.set_vexpand(True)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(14)
        box.set_margin_bottom(14)
        box.set_margin_start(14)
        box.set_margin_end(14)
        frame.add(box)
        frame.content_box = box
        return frame

    def wrap(self, widget):
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_min_content_height(260)
        scroller.add(widget)
        return scroller

    def tree(self, columns, data_columns=0):
        store = Gtk.ListStore(*([str] * (len(columns) + data_columns)))
        tree = Gtk.TreeView(model=store)
        tree.store = store
        tree.set_headers_clickable(True)
        tree.set_enable_search(True)
        tree.set_search_column(0)
        tree.set_grid_lines(Gtk.TreeViewGridLines.HORIZONTAL)
        for idx, name in enumerate(columns):
            renderer = Gtk.CellRendererText()
            renderer.set_property("ellipsize", 3)
            column = Gtk.TreeViewColumn(name, renderer, text=idx)
            column.set_resizable(True)
            column.set_expand(idx in {0, len(columns) - 1})
            tree.append_column(column)
        return tree

    def configure_jobs_tree(self):
        selection = self.jobs_tree.get_selection()
        selection.set_mode(Gtk.SelectionMode.SINGLE)
        selection.connect("changed", self.on_job_selection_changed)
        self.jobs_tree.connect("button-press-event", self.on_jobs_tree_button_press)
        self.jobs_tree.connect("row-activated", self.on_jobs_tree_row_activated)

    def configure_project_tree(self):
        selection = self.project_tree.get_selection()
        selection.set_mode(Gtk.SelectionMode.SINGLE)
        selection.connect("changed", self.on_project_selection_changed)
        self.project_tree.connect("button-press-event", self.on_project_tree_button_press)

    def automation_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        page.pack_start(toolbar, False, False, 0)

        for text, css, callback in [
            ("Add Job", "primary", lambda *_: self.open_create_job_dialog()),
            ("Change Model", "subtle", lambda *_: self.open_cron_model_dialog()),
            ("Pause", "subtle", lambda *_: self.run_selected_cron_action("pause")),
            ("Restart", "subtle", lambda *_: self.run_selected_cron_action("resume")),
            ("Run Now", "subtle", lambda *_: self.run_selected_cron_action("run")),
            ("Delete", "danger", lambda *_: self.confirm_delete_selected_job()),
        ]:
            button = Gtk.Button(label=text)
            button.get_style_context().add_class(css)
            button.connect("clicked", callback)
            toolbar.pack_start(button, False, False, 0)

        self.selected_job_label = self.label("No automation job selected", "muted")
        page.pack_start(self.selected_job_label, False, False, 0)
        page.pack_start(self.wrap(self.jobs_tree), True, True, 0)
        return page

    def projects_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        page.pack_start(toolbar, False, False, 0)
        for text, css, callback in [
            ("Add Quest", "primary", lambda *_: self.open_project_dialog()),
            ("Complete", "subtle", lambda *_: self.set_selected_project_status("done")),
            ("Reopen", "subtle", lambda *_: self.set_selected_project_status("open")),
            ("Delete", "danger", lambda *_: self.delete_selected_project()),
        ]:
            button = Gtk.Button(label=text)
            button.get_style_context().add_class(css)
            button.connect("clicked", callback)
            toolbar.pack_start(button, False, False, 0)
        self.project_selected_label = self.label("No quest selected", "muted")
        page.pack_start(self.project_selected_label, False, False, 0)
        page.pack_start(self.wrap(self.project_tree), True, True, 0)
        return page

    def build_obsidian_page(self):
        box = self.obsidian_card.content_box
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        toolbar.pack_start(self.label("Vault", "metric-label"), False, False, 0)
        self.obsidian_vault_combo = Gtk.ComboBoxText()
        self.obsidian_vaults = discover_obsidian_vaults()
        if self.obsidian_vaults:
            self.obsidian_vault_combo.append_text("Combined: Ian + Rook")
        for vault in self.obsidian_vaults:
            self.obsidian_vault_combo.append_text(str(vault))
        self.obsidian_vault_combo.set_active(0)
        self.obsidian_vault_combo.connect("changed", lambda *_: self.reload_obsidian_graph())
        toolbar.pack_start(self.obsidian_vault_combo, True, True, 0)
        refresh = Gtk.Button(label="Refresh Graph")
        refresh.get_style_context().add_class("primary")
        refresh.connect("clicked", lambda *_: self.reload_obsidian_graph())
        toolbar.pack_start(refresh, False, False, 0)
        box.pack_start(toolbar, False, False, 0)
        self.obsidian_status_label = self.label("Loading graph", "muted")
        box.pack_start(self.obsidian_status_label, False, False, 0)
        graph_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        graph_row.set_hexpand(True)
        graph_row.set_vexpand(True)
        box.pack_start(graph_row, True, True, 0)

        canvas_frame = Gtk.Frame()
        canvas_frame.get_style_context().add_class("chat-shell")
        canvas_frame.set_shadow_type(Gtk.ShadowType.NONE)
        canvas_frame.set_hexpand(True)
        canvas_frame.set_vexpand(True)
        self.obsidian_canvas = Gtk.EventBox()
        self.obsidian_canvas.set_hexpand(True)
        self.obsidian_canvas.set_vexpand(True)
        self.obsidian_canvas.set_size_request(760, 580)
        self.obsidian_canvas.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
            | Gdk.EventMask.SCROLL_MASK
        )
        self.obsidian_canvas.connect("button-press-event", self.on_obsidian_button_press)
        self.obsidian_canvas.connect("button-release-event", self.on_obsidian_button_release)
        self.obsidian_canvas.connect("motion-notify-event", self.on_obsidian_motion)
        self.obsidian_canvas.connect("scroll-event", self.on_obsidian_scroll)
        self.obsidian_graph_image = Gtk.Image()
        self.obsidian_graph_image.set_hexpand(True)
        self.obsidian_graph_image.set_vexpand(True)
        self.obsidian_canvas.add(self.obsidian_graph_image)
        canvas_frame.add(self.obsidian_canvas)
        graph_row.pack_start(canvas_frame, True, True, 0)

        self.obsidian_note_tree = self.tree(["Group", "Note"], data_columns=1)
        self.obsidian_note_tree.set_search_column(1)
        self.obsidian_note_tree.get_selection().connect("changed", self.on_obsidian_note_selection_changed)
        note_wrap = self.wrap(self.obsidian_note_tree)
        note_wrap.set_min_content_width(280)
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        side.set_size_request(340, -1)
        self.obsidian_note_title = self.label("Select a note", "section")
        side.pack_start(self.obsidian_note_title, False, False, 0)
        side.pack_start(note_wrap, True, True, 0)
        preview_frame = Gtk.Frame()
        preview_frame.get_style_context().add_class("chat-shell")
        preview_frame.set_shadow_type(Gtk.ShadowType.NONE)
        self.obsidian_note_preview = Gtk.TextView()
        self.obsidian_note_preview.set_editable(False)
        self.obsidian_note_preview.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.obsidian_note_preview.set_left_margin(10)
        self.obsidian_note_preview.set_right_margin(10)
        self.obsidian_note_preview.set_top_margin(8)
        self.obsidian_note_preview.set_bottom_margin(8)
        preview_scroll = Gtk.ScrolledWindow()
        preview_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        preview_scroll.add(self.obsidian_note_preview)
        preview_frame.add(preview_scroll)
        preview_frame.set_size_request(-1, 260)
        side.pack_start(preview_frame, False, False, 0)
        graph_row.pack_start(side, False, False, 0)

    def build_model_card(self):
        box = self.model_card.content_box
        self.model_status_label = self.label("Loading model settings", "muted")
        box.pack_start(self.model_status_label, False, False, 0)

        grid = Gtk.Grid(column_spacing=10, row_spacing=10)
        box.pack_start(grid, False, False, 0)

        self.model_provider_combo = Gtk.ComboBoxText.new_with_entry()
        for provider in self.model_provider_options(read_config()):
            self.model_provider_combo.append_text(provider)
        self.model_name_combo = Gtk.ComboBoxText.new_with_entry()
        for model in self.model_name_options(read_config()):
            self.model_name_combo.append_text(model)

        for row, (label_text, widget) in enumerate(
            [
                ("Provider", self.model_provider_combo),
                ("Model", self.model_name_combo),
            ]
        ):
            grid.attach(self.label(label_text, "metric-label"), 0, row, 1, 1)
            widget.set_hexpand(True)
            grid.attach(widget, 1, row, 1, 1)

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        controls.pack_start(self.label("Changes save to ~/.hermes/config.yaml", "muted"), True, True, 0)
        apply_button = Gtk.Button(label="Set Default")
        apply_button.get_style_context().add_class("primary")
        apply_button.connect("clicked", lambda *_: self.apply_default_model())
        controls.pack_start(apply_button, False, False, 0)
        box.pack_start(controls, False, False, 0)

        self.sync_model_inputs(read_config(), force=True)

    def model_config_values(self, config):
        model_cfg = config.get("model", {})
        if isinstance(model_cfg, dict):
            return {
                "provider": str(model_cfg.get("provider") or ""),
                "model": str(model_cfg.get("default") or ""),
                "base_url": str(model_cfg.get("base_url") or ""),
            }
        if isinstance(model_cfg, str):
            return {"provider": "", "model": model_cfg, "base_url": ""}
        return {"provider": "", "model": "", "base_url": ""}

    def model_provider_options(self, config):
        current = self.model_config_values(config)["provider"]
        providers = {
            "openai-codex",
            "ollama",
            "zai",
            "anthropic",
            "openrouter",
            "gemini",
            "xai",
            "copilot",
            "copilot-acp",
        }
        providers.update((config.get("providers") or {}).keys())
        if current:
            providers.add(current)
        return sorted(p for p in providers if p)

    def model_name_options(self, config):
        current = self.model_config_values(config)["model"]
        models = {
            "gpt-5.5",
            "gpt-5.4",
            "gpt-5.4-mini",
            "gpt-5.3-codex",
            "glm-4.5-air",
            "qwen2.5:3b-instruct",
            "qwen3:4b",
            "gemma3:4b-it-qat",
            "llama3.2:3b",
        }
        if current:
            models.add(current)
        for provider in (config.get("providers") or {}).values():
            if isinstance(provider, dict):
                for key in ("default_model", "model"):
                    if provider.get(key):
                        models.add(str(provider[key]))
        for entry in config.get("fallback_providers") or []:
            if isinstance(entry, dict) and entry.get("model"):
                models.add(str(entry["model"]))
        return sorted(models)

    def combo_text(self, combo):
        child = combo.get_child()
        if isinstance(child, Gtk.Entry):
            return child.get_text().strip()
        return (combo.get_active_text() or "").strip()

    def set_combo_text(self, combo, text):
        child = combo.get_child()
        if isinstance(child, Gtk.Entry):
            child.set_text(text or "")
            return
        if text:
            combo.set_active_id(text)

    def sync_model_inputs(self, config, force=False):
        if not self.model_provider_combo or not self.model_name_combo:
            return
        values = self.model_config_values(config)
        focused = any(
            combo.get_child() and combo.get_child().has_focus()
            for combo in (self.model_provider_combo, self.model_name_combo)
        )
        if force or not focused:
            self.set_combo_text(self.model_provider_combo, values["provider"])
            self.set_combo_text(self.model_name_combo, values["model"])
        if self.model_status_label:
            provider = values["provider"] or "auto"
            model = values["model"] or "not set"
            self.model_status_label.set_text(f"Current: {provider} · {model}")

    def apply_default_model(self):
        provider = self.combo_text(self.model_provider_combo)
        model = self.combo_text(self.model_name_combo)
        if not model:
            self.show_message("Choose a model first.")
            return
        code = r"""
import sys
from hermes_cli.config import load_config, save_config

provider = sys.argv[1].strip()
model = sys.argv[2].strip()
config = load_config()
model_cfg = config.get("model")
if not isinstance(model_cfg, dict):
    model_cfg = {}
model_cfg["default"] = model
if provider:
    model_cfg["provider"] = provider

providers = config.get("providers") if isinstance(config.get("providers"), dict) else {}
base_by_provider = {
    "ollama": "http://127.0.0.1:11434/v1",
    "openai-codex": "https://chatgpt.com/backend-api/codex",
}
if provider in providers and isinstance(providers[provider], dict):
    provider_cfg = providers[provider]
    base_url = provider_cfg.get("base_url") or provider_cfg.get("api")
    api_key = provider_cfg.get("api_key")
    if base_url:
        model_cfg["base_url"] = base_url
    if api_key:
        model_cfg["api_key"] = api_key
elif provider in base_by_provider:
    model_cfg["base_url"] = base_by_provider[provider]
    if provider == "ollama":
        model_cfg["api_key"] = "ollama"
elif provider:
    model_cfg.pop("base_url", None)
    model_cfg.pop("api_key", None)

config["model"] = model_cfg
save_config(config)
print(f"Default model set to {provider or 'auto'} · {model}")
"""

        def worker():
            ok, out, err = run(
                [HERMES_PYTHON, "-c", code, provider, model],
                timeout=20,
                cwd="/home/iancbaer/.hermes/hermes-agent",
            )
            GLib.idle_add(self.show_message, out or err or ("Default model saved." if ok else "Failed to save model."))
            self.refresh()

        threading.Thread(target=worker, daemon=True).start()

    def open_cron_model_dialog(self):
        job = self.selected_job()
        if not job:
            return

        config = read_config()
        dialog = Gtk.Dialog(
            title=f"Change Model: {job.get('name') or job['id']}",
            transient_for=self.window,
            flags=0,
        )
        dialog.set_default_size(560, 260)
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Change Model", Gtk.ResponseType.OK)
        content = dialog.get_content_area()
        content.set_spacing(12)
        content.set_margin_top(12)
        content.set_margin_bottom(12)
        content.set_margin_start(12)
        content.set_margin_end(12)

        grid = Gtk.Grid(column_spacing=10, row_spacing=10)
        content.pack_start(grid, False, False, 0)

        provider_combo = Gtk.ComboBoxText.new_with_entry()
        for provider in self.model_provider_options(config):
            provider_combo.append_text(provider)
        model_combo = Gtk.ComboBoxText.new_with_entry()
        model_combo.append_text("")
        for model in self.model_name_options(config):
            model_combo.append_text(model)

        self.set_combo_text(provider_combo, job.get("provider") or self.model_config_values(config)["provider"])
        self.set_combo_text(model_combo, job.get("model") or "")

        for row, (label_text, widget) in enumerate(
            [
                ("Provider", provider_combo),
                ("Model override", model_combo),
            ]
        ):
            grid.attach(self.label(label_text, "metric-label"), 0, row, 1, 1)
            widget.set_hexpand(True)
            grid.attach(widget, 1, row, 1, 1)

        current = job.get("model") or "default model"
        content.pack_start(self.label(f"Current job model: {current}", "muted"), False, False, 0)
        content.pack_start(self.label("Leave model blank to return this cron job to the default model.", "muted"), False, False, 0)

        dialog.show_all()
        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            provider = self.combo_text(provider_combo)
            model = self.combo_text(model_combo)
            if not model:
                provider = ""
            self.update_cron_job_model(job["id"], provider, model)
        dialog.destroy()

    def update_cron_job_model(self, job_id, provider, model):
        code = r"""
import json
import sys
from tools.cronjob_tools import cronjob

job_id = sys.argv[1]
provider = sys.argv[2].strip()
model = sys.argv[3].strip()
result = json.loads(cronjob(
    action="update",
    job_id=job_id,
    provider=provider,
    model=model,
    base_url="",
))
if not result.get("success"):
    print(result.get("error", "Failed to update cron job model"), file=sys.stderr)
    raise SystemExit(1)
job = result.get("job", {})
provider_label = job.get("provider") or "default provider"
model_label = job.get("model") or "default model"
print(f"Updated {job.get('name', job_id)}: {provider_label} · {model_label}")
"""

        def worker():
            ok, out, err = run(
                [HERMES_PYTHON, "-c", code, job_id, provider, model],
                timeout=20,
                cwd="/home/iancbaer/.hermes/hermes-agent",
            )
            GLib.idle_add(self.show_message, out or err or ("Cron job model updated." if ok else "Failed to update cron job model."))
            self.refresh()

        threading.Thread(target=worker, daemon=True).start()

    def build_speak_card(self):
        box = self.speak_card.content_box
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        title_box.set_hexpand(True)
        title_box.pack_start(self.label("Speak to Rook", "chat-title"), False, False, 0)
        title_box.pack_start(
            self.label("Desktop session continuity, bounded turns, memory providers off for local safety.", "chat-meta"),
            False,
            False,
            0,
        )
        header.pack_start(title_box, True, True, 0)
        self.chat_status = self.label("Ready", "muted")
        header.pack_start(self.chat_status, False, False, 0)
        box.pack_start(header, False, False, 0)

        transcript_frame = Gtk.Frame()
        transcript_frame.get_style_context().add_class("chat-shell")
        transcript_frame.set_shadow_type(Gtk.ShadowType.NONE)
        self.chat_output = Gtk.TextView()
        self.chat_output.get_style_context().add_class("chat-output")
        self.chat_output.set_editable(False)
        self.chat_output.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.chat_output.set_left_margin(12)
        self.chat_output.set_right_margin(12)
        self.chat_output.set_top_margin(10)
        self.chat_output.set_bottom_margin(10)
        self.chat_output.set_size_request(-1, 150)
        transcript_frame.add(self.chat_output)
        box.pack_start(transcript_frame, False, False, 0)

        input_frame = Gtk.Frame()
        input_frame.get_style_context().add_class("chat-shell")
        input_frame.set_shadow_type(Gtk.ShadowType.NONE)
        self.chat_input = Gtk.TextView()
        self.chat_input.get_style_context().add_class("chat-input")
        self.chat_input.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.chat_input.set_left_margin(12)
        self.chat_input.set_right_margin(12)
        self.chat_input.set_top_margin(9)
        self.chat_input.set_bottom_margin(9)
        self.chat_input.set_size_request(-1, 72)
        self.chat_input.connect("key-press-event", self.on_chat_input_key_press)
        input_frame.add(self.chat_input)
        box.pack_start(input_frame, False, False, 0)

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        hint = self.label("Enter sends. Shift+Enter adds a line.", "chat-meta")
        hint.set_hexpand(True)
        controls.pack_start(hint, True, True, 0)
        send = Gtk.Button(label="Send")
        send.get_style_context().add_class("primary")
        send.connect("clicked", lambda *_: self.send_chat())
        stop = Gtk.Button(label="Stop Reply")
        stop.get_style_context().add_class("danger")
        stop.connect("clicked", lambda *_: self.stop_chat())
        new_session = Gtk.Button(label="New Thread")
        new_session.get_style_context().add_class("subtle")
        new_session.connect("clicked", lambda *_: self.clear_desktop_session())
        controls.pack_start(send, False, False, 0)
        controls.pack_start(stop, False, False, 0)
        controls.pack_start(new_session, False, False, 0)
        box.pack_start(controls, False, False, 0)

    def clear_box(self, box):
        for child in list(box.get_children())[1:]:
            box.remove(child)

    def metric_grid(self, items):
        grid = Gtk.Grid(column_spacing=18, row_spacing=10)
        for idx, (label, value, css) in enumerate(items):
            cell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            cell.pack_start(self.label(label, "metric-label"), False, False, 0)
            value_label = self.label(value, "metric-value")
            if css:
                value_label.get_style_context().add_class(css)
            cell.pack_start(value_label, False, False, 0)
            grid.attach(cell, idx, 0, 1, 1)
        return grid

    def refresh(self):
        if self.refreshing:
            return True
        self.refreshing = True
        threading.Thread(target=self.refresh_worker, daemon=True).start()
        return True

    def refresh_worker(self):
        state = collect_state()
        GLib.idle_add(self.render, state)

    def render(self, state):
        self.state = state
        self.refreshing = False
        self.updated_label.set_text(f"Updated {state['time'].strftime('%H:%M:%S')}")
        self.render_model(state)
        self.render_agent(state)
        self.render_resources(state)
        self.render_memory(state)
        self.render_incidents(state)
        self.render_jobs(state)
        self.render_token_burn(state)
        self.render_project_tasks()
        self.render_processes(state)
        self.render_logs(state)
        return False

    def render_model(self, state):
        self.sync_model_inputs(state.get("config") or {})

    def render_agent(self, state):
        box = self.agent_card.content_box
        self.clear_box(box)
        hermes = state["hermes"]
        status_css = "good" if hermes["active"] == "active" else "bad"
        box.pack_start(
            self.metric_grid(
                [
                    ("Status", f"{hermes['active']}/{hermes['sub']}", status_css),
                    ("PID", str(hermes["pid"] or "-"), None),
                    ("Memory", fmt_bytes(hermes["memory"]), None),
                    ("CPU", f"{hermes['cpu_ns'] / 1e9:.1f}s" if hermes["cpu_ns"] else "-", None),
                ]
            ),
            False,
            False,
            0,
        )
        sessions = list(state["sessions"].values())[:3]
        for session in sessions:
            box.pack_start(
                self.label(
                    f"{session.get('display_name', 'Session')} · {session.get('session_id', '')} · "
                    f"{'flushed' if session.get('memory_flushed') else 'open'}",
                    "muted",
                ),
                False,
                False,
                0,
            )

    def render_resources(self, state):
        box = self.resources_card.content_box
        self.clear_box(box)
        active = state["ollama"]["active"]
        if active:
            for model in active:
                row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                row.pack_start(self.label(f"{model['name']} · {model['processor']} · {model['until']}"), True, True, 0)
                button = Gtk.Button(label="Stop")
                button.connect("clicked", lambda _b, name=model["name"]: self.run_action(["ollama", "stop", name]))
                row.pack_start(button, False, False, 0)
                box.pack_start(row, False, False, 0)
        else:
            box.pack_start(self.label("No local model loaded.", "muted"), False, False, 0)
        runners = [p for p in state["processes"] if "ollama runner" in p["args"]]
        if runners:
            for proc in runners:
                box.pack_start(self.label(f"Runner PID {proc['pid']} · {proc['etime']} · CPU {proc['cpu']}%", "bad"), False, False, 0)

    def render_memory(self, state):
        box = self.memory_card.content_box
        self.clear_box(box)
        h = state["hindsight"]
        cfg = h["config"]
        daemon = h["daemon"]
        rows = [
            f"Daemon: {'pid ' + str(daemon['pid']) if daemon else 'stopped'}",
            f"Provider: {cfg.get('llm_provider', '-')}",
            f"Model: {cfg.get('llm_model', '-')}",
            f"Mode: {cfg.get('memory_mode', '-')}",
        ]
        for row in rows:
            css = "bad" if "7b" in row.lower() or "8b" in row.lower() or "27b" in row.lower() else "muted"
            box.pack_start(self.label(row, css), False, False, 0)
        if h["stats"]:
            box.pack_start(self.label(h["stats"][-180:], "muted"), False, False, 0)
        button = Gtk.Button(label="Stop Hindsight Daemon")
        button.get_style_context().add_class("danger")
        button.connect("clicked", lambda *_: self.run_action(["/bin/bash", "-lc", "pkill -TERM -f 'hindsight-api --daemon --idle-timeout 0 --port 9177' || true"]))
        box.pack_start(button, False, False, 0)

    def render_incidents(self, state):
        box = self.incident_card.content_box
        self.clear_box(box)
        if not state["incidents"]:
            box.pack_start(self.label("No active incidents.", "muted"), False, False, 0)
            return
        for severity, at, title, detail in state["incidents"]:
            css = "bad" if severity == "High" else "warn"
            box.pack_start(self.label(f"{severity} · {at} · {title}", css), False, False, 0)
            box.pack_start(self.label(detail, "muted"), False, False, 0)

    def render_jobs(self, state):
        store = self.jobs_tree.store
        store.clear()
        self.job_rows = {}
        jobs = sorted(state["jobs"], key=lambda j: (not j["enabled"], j["next"] or "z", j["name"]))[:40]
        selected_path = None
        for job in jobs:
            status = job["status"] or job["state"]
            if not job["enabled"]:
                status = "paused" if job["state"] == "paused" else status or "disabled"
            self.job_rows[job["id"]] = job
            iterator = store.append(
                [
                    job["name"],
                    status,
                    job["schedule"],
                    short_time(job["next"]),
                    f"{job['provider']} {job['model']}".strip() or "default",
                    job["id"],
                ]
            )
            if job["id"] == self.selected_job_id:
                selected_path = store.get_path(iterator)
        if selected_path is not None:
            self.jobs_tree.get_selection().select_path(selected_path)
            self.jobs_tree.scroll_to_cell(selected_path, None, False, 0, 0)
        elif self.selected_job_id and self.selected_job_id not in self.job_rows:
            self.selected_job_id = None
            self.update_selected_job_label(None)

    def render_token_burn(self, state):
        burn = state.get("token_burn") or {}
        totals = burn.get("totals") or {}
        day = totals.get("24h", {})
        week = totals.get("7d", {})
        month = totals.get("30d", {})
        forecast_rows = burn.get("forecasts") or []
        forecast_24h = sum(row.get("forecast_24h", 0) for row in forecast_rows if row.get("enabled"))
        forecast_7d = sum(row.get("forecast_7d", 0) for row in forecast_rows if row.get("enabled"))

        if self.token_overview_card:
            box = self.token_overview_card.content_box
            self.clear_box(box)
            box.pack_start(
                self.metric_grid(
                    [
                        ("Last 24h", fmt_tokens(day.get("tokens")), "good" if day.get("tokens", 0) < 250_000 else "warn"),
                        ("Last 7d", fmt_tokens(week.get("tokens")), None),
                        ("30d Automations", fmt_tokens(burn.get("automation_tokens_30d")), None),
                        ("30d Chat", fmt_tokens(burn.get("interactive_tokens_30d")), None),
                        ("Next 24h", fmt_tokens(forecast_24h), "warn" if forecast_24h > 500_000 else None),
                        ("Next 7d", fmt_tokens(forecast_7d), "warn" if forecast_7d > 2_000_000 else None),
                    ]
                ),
                False,
                False,
                0,
            )
            detail = (
                f"30d: {fmt_tokens(month.get('input'))} input · "
                f"{fmt_tokens(month.get('output'))} output · "
                f"{fmt_tokens(month.get('cache'))} cache · "
                f"{fmt_tokens(month.get('reasoning'))} reasoning · "
                f"{fmt_money(month.get('cost'))} tracked cost"
            )
            box.pack_start(self.label(detail, "muted"), False, False, 0)
            box.pack_start(
                self.label("Forecasts use each automation's recent average per firing; jobs without history use a rough prompt-size estimate.", "muted"),
                False,
                False,
                0,
            )

        if self.token_forecast_tree:
            store = self.token_forecast_tree.store
            store.clear()
            for row in forecast_rows[:50]:
                avg = f"{fmt_tokens(row.get('avg_tokens'))} · {row.get('basis')}"
                last = fmt_tokens(row.get("last_tokens")) if row.get("last_tokens") else "-"
                store.append(
                    [
                        row.get("name", ""),
                        avg,
                        last,
                        str(row.get("runs_24h") or 0),
                        fmt_tokens(row.get("forecast_24h")),
                        fmt_tokens(row.get("forecast_7d")),
                        row.get("model", ""),
                    ]
                )

        if self.token_recent_tree:
            job_names = {job["id"]: job["name"] or job["id"] for job in state.get("jobs", [])}
            store = self.token_recent_tree.store
            store.clear()
            for session in (burn.get("sessions") or [])[:80]:
                source = session.get("source") or "-"
                if session.get("job_id"):
                    source = f"cron · {job_names.get(session['job_id'], session['job_id'])}"
                cache = int(session.get("cache_read_tokens") or 0) + int(session.get("cache_write_tokens") or 0)
                store.append(
                    [
                        short_unix_time(session.get("ended_at") or session.get("started_at")),
                        source,
                        fmt_tokens(session.get("total_tokens")),
                        fmt_tokens(session.get("input_tokens")),
                        fmt_tokens(session.get("output_tokens")),
                        fmt_tokens(cache),
                        session.get("model") or "",
                    ]
                )

    def render_project_tasks(self):
        if not self.project_tree:
            return
        store = self.project_tree.store
        store.clear()
        self.project_rows = {}
        tasks = sorted(
            load_project_tasks(),
            key=lambda task: (
                task.get("status") == "done",
                task.get("priority", "medium"),
                task.get("due") or "9999",
                task.get("title", ""),
            ),
        )
        selected_path = None
        for task in tasks:
            task_id = task.get("id") or str(int(datetime.now().timestamp() * 1000))
            task["id"] = task_id
            self.project_rows[task_id] = task
            iterator = store.append(
                [
                    task.get("title", ""),
                    task.get("status", "open"),
                    task.get("area", ""),
                    task.get("priority", "medium"),
                    task.get("due", ""),
                    task_id,
                ]
            )
            if task_id == self.selected_project_id:
                selected_path = store.get_path(iterator)
        if selected_path is not None:
            self.project_tree.get_selection().select_path(selected_path)
        elif self.selected_project_id and self.selected_project_id not in self.project_rows:
            self.selected_project_id = None
            self.update_project_selected_label(None)

    def update_project_selected_label(self, task):
        if not self.project_selected_label:
            return
        if not task:
            self.project_selected_label.set_text("No quest selected")
            return
        self.project_selected_label.set_text(
            f"Selected: {task.get('title', task.get('id'))} · {task.get('status', 'open')} · {task.get('area', '-')}"
        )

    def on_project_selection_changed(self, selection):
        model, iterator = selection.get_selected()
        if iterator is None:
            return
        task_id = model.get_value(iterator, 5)
        self.selected_project_id = task_id
        self.update_project_selected_label(self.project_rows.get(task_id))

    def on_project_tree_button_press(self, tree, event):
        if event.button != 1:
            return False
        hit = tree.get_path_at_pos(int(event.x), int(event.y))
        if not hit:
            return False
        path, _column, _cell_x, _cell_y = hit
        tree.set_cursor(path, None, False)
        tree.get_selection().select_path(path)
        return False

    def selected_project_task(self):
        if self.selected_project_id and self.selected_project_id in self.project_rows:
            return self.project_rows[self.selected_project_id]
        selection = self.project_tree.get_selection()
        model, iterator = selection.get_selected()
        if iterator is None:
            self.show_message("Select a quest first.")
            return None
        task_id = model.get_value(iterator, 5)
        return self.project_rows.get(task_id)

    def open_project_dialog(self):
        dialog = Gtk.Dialog(title="Add Quest", transient_for=self.window, flags=0)
        dialog.set_default_size(540, 260)
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Add Quest", Gtk.ResponseType.OK)
        content = dialog.get_content_area()
        content.set_spacing(10)
        content.set_margin_top(12)
        content.set_margin_bottom(12)
        content.set_margin_start(12)
        content.set_margin_end(12)
        grid = Gtk.Grid(column_spacing=10, row_spacing=10)
        content.pack_start(grid, True, True, 0)
        title_entry = Gtk.Entry()
        area_entry = Gtk.Entry()
        priority_entry = Gtk.ComboBoxText()
        for priority in ["high", "medium", "low"]:
            priority_entry.append_text(priority)
        priority_entry.set_active(1)
        due_entry = Gtk.Entry()
        due_entry.set_placeholder_text("YYYY-MM-DD or blank")
        for row, (label_text, widget) in enumerate(
            [("Quest", title_entry), ("Area", area_entry), ("Priority", priority_entry), ("Due", due_entry)]
        ):
            grid.attach(self.label(label_text, "metric-label"), 0, row, 1, 1)
            widget.set_hexpand(True)
            grid.attach(widget, 1, row, 1, 1)
        dialog.show_all()
        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            title = title_entry.get_text().strip()
            if not title:
                dialog.destroy()
                self.show_message("Quest title is required.")
                return
            tasks = load_project_tasks()
            tasks.append(
                {
                    "id": datetime.now().strftime("%Y%m%d%H%M%S%f"),
                    "title": title,
                    "area": area_entry.get_text().strip(),
                    "priority": priority_entry.get_active_text() or "medium",
                    "due": due_entry.get_text().strip(),
                    "status": "open",
                    "created_at": datetime.now().isoformat(),
                }
            )
            save_project_tasks(tasks)
            self.render_project_tasks()
        dialog.destroy()

    def set_selected_project_status(self, status):
        task = self.selected_project_task()
        if not task:
            return
        tasks = load_project_tasks()
        for item in tasks:
            if item.get("id") == task.get("id"):
                item["status"] = status
                item["updated_at"] = datetime.now().isoformat()
                break
        save_project_tasks(tasks)
        self.render_project_tasks()

    def delete_selected_project(self):
        task = self.selected_project_task()
        if not task:
            return
        dialog = Gtk.MessageDialog(
            transient_for=self.window,
            flags=0,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.NONE,
            text=f"Delete quest '{task.get('title')}'?",
        )
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Delete", Gtk.ResponseType.OK)
        response = dialog.run()
        dialog.destroy()
        if response == Gtk.ResponseType.OK:
            save_project_tasks([item for item in load_project_tasks() if item.get("id") != task.get("id")])
            self.selected_project_id = None
            self.render_project_tasks()

    def reload_obsidian_graph(self):
        if not self.obsidian_vault_combo:
            return
        choice = self.obsidian_vault_combo.get_active_text()
        if not choice:
            if self.obsidian_status_label:
                self.obsidian_status_label.set_text("No vault found")
            return
        try:
            if choice.startswith("Combined:"):
                graphs = [build_obsidian_graph(vault, limit=70) for vault in self.obsidian_vaults[:4]]
                self.obsidian_graph = merge_obsidian_graphs(graphs)
            else:
                self.obsidian_graph = build_obsidian_graph(choice, limit=140)
        except Exception as exc:
            self.obsidian_graph = {"nodes": [], "edges": [], "error": str(exc)}
        self.selected_obsidian_node = None
        self.obsidian_offset = (0.0, 0.0)
        node_count = len(self.obsidian_graph.get("nodes", []))
        edge_count = len(self.obsidian_graph.get("edges", []))
        if self.obsidian_status_label:
            error = self.obsidian_graph.get("error")
            if error:
                self.obsidian_status_label.set_text(f"Graph error: {error}")
            else:
                self.obsidian_status_label.set_text(
                    f"{self.obsidian_graph.get('vault', choice)} · {node_count} notes · {edge_count} verified note links"
                )
        self.render_obsidian_note_list()
        self.render_obsidian_image()

    def render_obsidian_note_list(self):
        if not self.obsidian_note_tree:
            return
        store = self.obsidian_note_tree.store
        store.clear()
        nodes = sorted(self.obsidian_graph.get("nodes", []), key=lambda node: (node.get("group", ""), node.get("name", "")))
        for node in nodes[:160]:
            store.append([node.get("group", "Root"), node.get("name", ""), node.get("full_path", "")])
        if self.obsidian_note_title:
            self.obsidian_note_title.set_text("Select a note")
        if self.obsidian_note_preview:
            self.obsidian_note_preview.get_buffer().set_text("")

    def on_obsidian_note_selection_changed(self, selection):
        if self.obsidian_selection_syncing:
            return
        model, iterator = selection.get_selected()
        if iterator is None:
            return
        full_path = model.get_value(iterator, 2)
        for idx, node in enumerate(self.obsidian_graph.get("nodes", [])):
            if node.get("full_path") == full_path:
                if idx != self.selected_obsidian_node:
                    self.select_obsidian_node(idx)
                return

    def select_obsidian_node(self, idx):
        nodes = self.obsidian_graph.get("nodes", [])
        if idx < 0 or idx >= len(nodes):
            return
        self.selected_obsidian_node = idx
        node = nodes[idx]
        if self.obsidian_note_title:
            self.obsidian_note_title.set_text(node.get("name", "Note"))
        if self.obsidian_note_preview:
            path = Path(node.get("full_path", ""))
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except Exception as exc:
                text = f"Could not read note: {exc}"
            self.obsidian_note_preview.get_buffer().set_text(text[:12000])
        if self.obsidian_note_tree:
            model = self.obsidian_note_tree.get_model()
            iterator = model.get_iter_first()
            self.obsidian_selection_syncing = True
            try:
                while iterator:
                    if model.get_value(iterator, 2) == node.get("full_path", ""):
                        path = model.get_path(iterator)
                        self.obsidian_note_tree.get_selection().select_path(path)
                        self.obsidian_note_tree.scroll_to_cell(path, None, False, 0, 0)
                        break
                    iterator = model.iter_next(iterator)
            finally:
                self.obsidian_selection_syncing = False
        self.render_obsidian_image()

    def on_obsidian_button_press(self, _widget, event):
        if event.button == 1:
            self.obsidian_dragging = True
            self.obsidian_moved = False
            self.obsidian_last_drag = (event.x, event.y)
            return True
        return False

    def on_obsidian_button_release(self, _widget, event):
        if event.button == 1:
            if not self.obsidian_moved:
                self.select_obsidian_node_at(event.x, event.y)
            self.obsidian_dragging = False
            self.obsidian_last_drag = None
            return True
        return False

    def on_obsidian_motion(self, _widget, event):
        if not self.obsidian_dragging or not self.obsidian_last_drag:
            return False
        last_x, last_y = self.obsidian_last_drag
        dx = event.x - last_x
        dy = event.y - last_y
        if abs(dx) + abs(dy) > 4:
            self.obsidian_moved = True
        offset_x, offset_y = self.obsidian_offset
        self.obsidian_offset = (offset_x + dx, offset_y + dy)
        self.obsidian_last_drag = (event.x, event.y)
        self.render_obsidian_image()
        return True

    def select_obsidian_node_at(self, event_x, event_y):
        if not self.obsidian_positions or not self.obsidian_canvas:
            return
        alloc = self.obsidian_canvas.get_allocation()
        render_width, render_height = self.obsidian_graph_size
        width = max(1, alloc.width)
        height = max(1, alloc.height)
        x = event_x * render_width / width
        y = event_y * render_height / height
        best = None
        for idx, pos in enumerate(self.obsidian_positions):
            radius = 7 + pos.get("scale", 1) * 4
            distance = math.hypot(pos["x"] - x, pos["y"] - y)
            if distance <= max(18, radius + 8) and (best is None or distance < best[0]):
                best = (distance, idx)
        if best:
            self.select_obsidian_node(best[1])

    def on_obsidian_scroll(self, _widget, event):
        if event.direction == Gdk.ScrollDirection.UP:
            self.obsidian_zoom = min(1.8, self.obsidian_zoom * 1.08)
        elif event.direction == Gdk.ScrollDirection.DOWN:
            self.obsidian_zoom = max(0.55, self.obsidian_zoom / 1.08)
        self.render_obsidian_image()
        return True

    def obsidian_node_positions(self, width, height):
        nodes = self.obsidian_graph.get("nodes", [])
        center_x = width / 2
        center_y = height / 2
        offset_x, offset_y = self.obsidian_offset
        positions = []
        degrees = [node.get("degree", 0) for node in nodes]
        max_degree = max(degrees) if degrees else 1
        group_sizes = {}
        group_indexes = {}
        for idx, node in enumerate(nodes):
            group = node.get("group", "Root")
            group_sizes[group] = group_sizes.get(group, 0) + 1
            group_indexes.setdefault(group, []).append(idx)
        groups = sorted(group_sizes, key=lambda group: (-group_sizes[group], group.lower()))
        group_count = max(1, len(groups))
        group_centers = {}
        columns = max(1, math.ceil(math.sqrt(group_count * width / max(1, height))))
        rows = max(1, math.ceil(group_count / columns))
        usable_width = width * 0.82
        usable_height = height * 0.74
        cell_width = usable_width / columns
        cell_height = usable_height / rows
        for group_idx, group in enumerate(groups):
            row = group_idx // columns
            col = group_idx % columns
            grid_x = (col + 0.5) * cell_width - usable_width / 2
            grid_y = (row + 0.5) * cell_height - usable_height / 2
            group_centers[group] = (
                center_x + offset_x + grid_x * self.obsidian_zoom,
                center_y + offset_y + grid_y * self.obsidian_zoom,
            )
        group_rank = {}
        for group, indexes in group_indexes.items():
            ranked = sorted(
                indexes,
                key=lambda index: (-nodes[index].get("degree", 0), nodes[index].get("name", "").lower()),
            )
            for rank, index in enumerate(ranked):
                group_rank[index] = rank
        group_offset = {group: idx * 0.55 for idx, group in enumerate(groups)}
        cluster_radius = max(42, min(cell_width, cell_height) * 0.33) * self.obsidian_zoom
        for idx, node in enumerate(nodes):
            group = node.get("group", "Root")
            local_idx = group_rank.get(idx, 0)
            local_count = max(1, group_sizes[group])
            degree = node.get("degree", 0)
            hub_pull = 1 - (degree / max(1, max_degree)) * 0.45
            angle = group_offset.get(group, 0) + local_idx * math.pi * (3 - 5 ** 0.5)
            radial = cluster_radius * math.sqrt((local_idx + 0.5) / local_count) * hub_pull
            gx, gy = group_centers[group]
            positions.append(
                {
                    "x": gx + math.cos(angle) * radial,
                    "y": gy + math.sin(angle) * radial,
                    "scale": 1 + degree / max(1, max_degree),
                    "z": degree,
                    "name": node.get("name", ""),
                    "group": group,
                }
            )
        return positions

    def obsidian_group_color(self, group):
        palette = [
            (0.46, 0.62, 0.78),
            (0.56, 0.70, 0.58),
            (0.74, 0.60, 0.48),
            (0.62, 0.56, 0.74),
            (0.70, 0.70, 0.50),
            (0.48, 0.68, 0.70),
        ]
        index = sum(ord(char) for char in str(group)) % len(palette)
        return palette[index]

    def render_obsidian_image(self):
        if not self.obsidian_graph_image:
            return
        OBSIDIAN_GRAPH_FILE.parent.mkdir(parents=True, exist_ok=True)
        width, height = 1100, 720
        if self.obsidian_canvas:
            alloc = self.obsidian_canvas.get_allocation()
            width = max(760, alloc.width or width)
            height = max(540, alloc.height or height)
        self.obsidian_graph_size = (width, height)
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
        cr = cairo.Context(surface)
        self.draw_obsidian_graph_context(cr, width, height)
        surface.write_to_png(str(OBSIDIAN_GRAPH_FILE))
        self.obsidian_graph_image.set_from_file(str(OBSIDIAN_GRAPH_FILE))

    def draw_obsidian_graph(self, widget, cr):
        width = widget.get_allocated_width()
        height = widget.get_allocated_height()
        self.draw_obsidian_graph_context(cr, width, height)
        return False

    def draw_obsidian_graph_context(self, cr, width, height):
        cr.set_source_rgb(0.05, 0.06, 0.08)
        cr.rectangle(0, 0, width, height)
        cr.fill()
        nodes = self.obsidian_graph.get("nodes", [])
        if not nodes:
            cr.set_source_rgb(0.65, 0.69, 0.74)
            cr.select_font_face("Sans")
            cr.set_font_size(14)
            cr.move_to(24, 40)
            message = self.obsidian_graph.get("error") or "No notes found for this vault."
            cr.show_text(message[:100])
            return
        positions = self.obsidian_node_positions(width, height)
        self.obsidian_positions = positions
        groups = {}
        for pos in positions:
            groups.setdefault(pos.get("group", "Root"), []).append(pos)
        for group, group_positions in groups.items():
            if not group_positions:
                continue
            gx = sum(pos["x"] for pos in group_positions) / len(group_positions)
            gy = sum(pos["y"] for pos in group_positions) / len(group_positions)
            radius = max(math.hypot(pos["x"] - gx, pos["y"] - gy) for pos in group_positions) + 28
            red, green, blue = self.obsidian_group_color(group)
            cr.set_source_rgba(red, green, blue, 0.10)
            cr.arc(gx, gy, radius, 0, math.pi * 2)
            cr.fill()
            cr.set_source_rgba(red, green, blue, 0.28)
            cr.set_line_width(1.2)
            cr.arc(gx, gy, radius, 0, math.pi * 2)
            cr.stroke()
            cr.set_source_rgba(0.72, 0.78, 0.86, 0.72)
            cr.select_font_face("Sans")
            cr.set_font_size(11)
            cr.move_to(gx - radius + 12, gy - radius + 22)
            cr.show_text(str(group)[:22])
        for a, b in self.obsidian_graph.get("edges", []):
            if a >= len(positions) or b >= len(positions):
                continue
            pa = positions[a]
            pb = positions[b]
            cr.set_source_rgba(0.48, 0.56, 0.66, 0.42)
            cr.set_line_width(1.0)
            cr.move_to(pa["x"], pa["y"])
            cr.line_to(pb["x"], pb["y"])
            cr.stroke()
        ordered = sorted(enumerate(positions), key=lambda item: item[1]["scale"])
        label_stride = max(1, len(positions) // 18)
        for idx, pos in ordered:
            size = 3.5 + pos["scale"] * 4.0
            red, green, blue = self.obsidian_group_color(pos.get("group", "Root"))
            if idx == self.selected_obsidian_node:
                cr.set_source_rgba(0.93, 0.96, 1.0, 1.0)
                cr.arc(pos["x"], pos["y"], size + 6, 0, math.pi * 2)
                cr.fill()
                cr.set_source_rgba(red, green, blue, 1.0)
            else:
                cr.set_source_rgba(red, green, blue, 0.92)
            cr.arc(pos["x"], pos["y"], size, 0, math.pi * 2)
            cr.fill()
            if idx == self.selected_obsidian_node or idx % label_stride == 0 or pos["scale"] > 1.45:
                cr.set_source_rgba(0.88, 0.91, 0.95, 0.88)
                cr.select_font_face("Sans")
                cr.set_font_size(10)
                cr.move_to(pos["x"] + 8, pos["y"] + 3)
                cr.show_text(pos["name"][:28])
        cr.set_source_rgba(0.70, 0.76, 0.84, 0.72)
        cr.select_font_face("Sans")
        cr.set_font_size(12)
        cr.move_to(18, 24)
        cr.show_text(f"{len(nodes)} notes / {len(self.obsidian_graph.get('edges', []))} verified links   drag to pan / scroll to zoom")
        return

    def render_processes(self, state):
        store = self.process_tree.store
        store.clear()
        for proc in state["processes"][:16]:
            store.append(
                [
                    str(proc["pid"]),
                    proc["comm"],
                    proc["etime"],
                    f"{proc['cpu']}%",
                    proc["args"],
                ]
            )

    def render_logs(self, state):
        self.latest_logs = state.get("logs") or {}
        self.render_selected_log()

    def render_selected_log(self):
        if not self.log_view:
            return
        name = self.log_selector.get_active_text() if self.log_selector else "Agent"
        self.log_view.get_buffer().set_text(self.latest_logs.get(name or "Agent", ""))

    def selected_job(self):
        selection = self.jobs_tree.get_selection()
        model, iterator = selection.get_selected()
        if iterator is not None:
            job_id = model.get_value(iterator, 5)
            self.selected_job_id = job_id
            job = self.job_rows.get(job_id, {"id": job_id, "name": model.get_value(iterator, 0)})
            return job
        if self.selected_job_id and self.selected_job_id in self.job_rows:
            return self.job_rows[self.selected_job_id]
        if iterator is None:
            self.show_message("Select an automation job first.")
            return None

    def update_selected_job_label(self, job):
        if not hasattr(self, "selected_job_label"):
            return
        if not job:
            self.selected_job_label.set_text("No automation job selected")
            return
        status = job.get("status") or job.get("state") or "unknown"
        if not job.get("enabled", True):
            status = "paused" if job.get("state") == "paused" else status or "disabled"
        self.selected_job_label.set_text(
            f"Selected: {job.get('name') or job.get('id')} · {status} · {job.get('schedule') or '-'}"
        )

    def on_job_selection_changed(self, selection):
        model, iterator = selection.get_selected()
        if iterator is None:
            return
        job_id = model.get_value(iterator, 5)
        self.selected_job_id = job_id
        self.update_selected_job_label(self.job_rows.get(job_id, {"id": job_id, "name": model.get_value(iterator, 0)}))

    def on_jobs_tree_button_press(self, tree, event):
        if event.button != 1:
            return False
        hit = tree.get_path_at_pos(int(event.x), int(event.y))
        if not hit:
            return False
        path, _column, _cell_x, _cell_y = hit
        tree.set_cursor(path, None, False)
        tree.get_selection().select_path(path)
        return False

    def on_jobs_tree_row_activated(self, tree, path, _column):
        tree.get_selection().select_path(path)
        model = tree.get_model()
        iterator = model.get_iter(path)
        self.selected_job_id = model.get_value(iterator, 5)
        self.update_selected_job_label(self.job_rows.get(self.selected_job_id))

    def run_selected_cron_action(self, action):
        job = self.selected_job()
        if not job:
            return
        command = [
            HERMES_PYTHON,
            "-m",
            "hermes_cli.main",
            "cron",
            action,
            job["id"],
        ]
        self.run_action(command)

    def confirm_delete_selected_job(self):
        job = self.selected_job()
        if not job:
            return
        dialog = Gtk.MessageDialog(
            transient_for=self.window,
            flags=0,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.NONE,
            text=f"Delete automation job '{job.get('name') or job['id']}'?",
        )
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Delete", Gtk.ResponseType.OK)
        response = dialog.run()
        dialog.destroy()
        if response == Gtk.ResponseType.OK:
            self.run_action(
                [
                    HERMES_PYTHON,
                    "-m",
                    "hermes_cli.main",
                    "cron",
                    "remove",
                    job["id"],
                ]
            )

    def text_view_value(self, view):
        buffer = view.get_buffer()
        start, end = buffer.get_bounds()
        return buffer.get_text(start, end, True).strip()

    def open_create_job_dialog(self):
        dialog = Gtk.Dialog(
            title="Add Automation Job",
            transient_for=self.window,
            flags=0,
        )
        dialog.set_default_size(640, 460)
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Add Job", Gtk.ResponseType.OK)
        content = dialog.get_content_area()
        content.set_spacing(12)
        content.set_margin_top(12)
        content.set_margin_bottom(12)
        content.set_margin_start(12)
        content.set_margin_end(12)

        grid = Gtk.Grid(column_spacing=10, row_spacing=10)
        content.pack_start(grid, False, False, 0)

        name_entry = Gtk.Entry()
        name_entry.set_placeholder_text("Optional display name")
        schedule_entry = Gtk.Entry()
        schedule_entry.set_placeholder_text("every 2h, 30m, or 0 9 * * *")
        deliver_entry = Gtk.Entry()
        deliver_entry.set_text("local")

        for row, (label_text, widget) in enumerate(
            [
                ("Name", name_entry),
                ("Schedule", schedule_entry),
                ("Deliver", deliver_entry),
            ]
        ):
            grid.attach(self.label(label_text, "metric-label"), 0, row, 1, 1)
            grid.attach(widget, 1, row, 1, 1)
            widget.set_hexpand(True)

        prompt_label = self.label("Prompt", "metric-label")
        content.pack_start(prompt_label, False, False, 0)
        prompt_frame = Gtk.Frame()
        prompt_frame.get_style_context().add_class("chat-shell")
        prompt_frame.set_shadow_type(Gtk.ShadowType.NONE)
        prompt_view = Gtk.TextView()
        prompt_view.get_style_context().add_class("chat-input")
        prompt_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        prompt_view.set_left_margin(10)
        prompt_view.set_right_margin(10)
        prompt_view.set_top_margin(8)
        prompt_view.set_bottom_margin(8)
        prompt_frame.add(prompt_view)
        prompt_frame.set_size_request(-1, 190)
        content.pack_start(prompt_frame, True, True, 0)

        dialog.show_all()
        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            name = name_entry.get_text().strip()
            schedule = schedule_entry.get_text().strip()
            deliver = deliver_entry.get_text().strip()
            prompt = self.text_view_value(prompt_view)
            if not schedule or not prompt:
                dialog.destroy()
                self.show_message("Schedule and prompt are required.")
                return
            command = [
                HERMES_PYTHON,
                "-m",
                "hermes_cli.main",
                "cron",
                "create",
                schedule,
                prompt,
            ]
            if name:
                command.extend(["--name", name])
            if deliver:
                command.extend(["--deliver", deliver])
            self.run_action(command)
        dialog.destroy()

    def run_action(self, command):
        def worker():
            ok, out, err = run(command, timeout=20)
            message = out or err or ("Command completed." if ok else "Command failed.")
            GLib.idle_add(self.show_message, message)
            self.refresh()

        threading.Thread(target=worker, daemon=True).start()

    def get_chat_input(self):
        buffer = self.chat_input.get_buffer()
        start, end = buffer.get_bounds()
        return buffer.get_text(start, end, True).strip()

    def on_chat_input_key_press(self, _widget, event):
        if event.keyval not in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            return False
        if event.state & Gdk.ModifierType.SHIFT_MASK:
            return False
        self.send_chat()
        return True

    def set_chat_status(self, text, css="muted"):
        self.chat_status.set_text(text)
        context = self.chat_status.get_style_context()
        for klass in ("muted", "good", "warn", "bad"):
            context.remove_class(klass)
        context.add_class(css)

    def append_chat_output(self, text):
        buffer = self.chat_output.get_buffer()
        start, end = buffer.get_bounds()
        current = buffer.get_text(start, end, True)
        prefix = "\n\n" if current else ""
        buffer.set_text(current + prefix + text)

    def send_chat(self):
        prompt = self.get_chat_input()
        if not prompt:
            self.show_message("Type a message for Rook first.")
            return
        if self.chat_proc and self.chat_proc.poll() is None:
            self.show_message("Rook is already replying. Stop it first if needed.")
            return

        self.chat_input.get_buffer().set_text("")
        GLib.idle_add(self.append_chat_output, f"You:\n{prompt}\n\nRook:\nThinking...")
        GLib.idle_add(self.set_chat_status, "Rook is replying", "warn")
        threading.Thread(target=self.chat_worker, args=(prompt,), daemon=True).start()

    def chat_worker(self, prompt):
        command = [
            "/home/iancbaer/.hermes/hermes-agent/venv/bin/python",
            "-m",
            "hermes_cli.main",
            "chat",
            "-Q",
            "--ignore-rules",
            "--source",
            "desktop",
            "--max-turns",
            "12",
        ]
        session_id = self.load_desktop_session_id()
        if session_id:
            command.extend(["--resume", session_id])
        command.extend(["-q", prompt])
        try:
            self.chat_proc = subprocess.Popen(
                command,
                cwd=str(HOME),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            out, err = self.chat_proc.communicate(timeout=180)
            code = self.chat_proc.returncode
            if code != 0 and session_id and "No session found" in (err or out):
                command = [part for part in command if part not in {"--resume", session_id}]
                self.chat_proc = subprocess.Popen(
                    command,
                    cwd=str(HOME),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                out, err = self.chat_proc.communicate(timeout=180)
                code = self.chat_proc.returncode
        except subprocess.TimeoutExpired:
            if self.chat_proc:
                self.chat_proc.terminate()
            out, err, code = "", "Timed out after 3 minutes. Reply was stopped.", 124
        except Exception as exc:
            out, err, code = "", str(exc), 1
        finally:
            self.chat_proc = None

        text = (out or err or "").strip()
        text = self.capture_session_id(text)
        if not text:
            text = f"Rook exited with code {code} and no output."
        if code == 0:
            GLib.idle_add(self.replace_last_thinking, text)
            GLib.idle_add(self.set_chat_status, "Ready", "good")
        else:
            GLib.idle_add(self.replace_last_thinking, f"[Error]\n{text}")
            GLib.idle_add(self.set_chat_status, "Ready after error", "bad")
        self.refresh()

    def load_desktop_session_id(self):
        data = read_json(DESKTOP_SESSION_FILE, {})
        session_id = data.get("session_id", "")
        return session_id if isinstance(session_id, str) and session_id.strip() else ""

    def clear_desktop_session(self):
        try:
            DESKTOP_SESSION_FILE.unlink(missing_ok=True)
        except Exception as exc:
            self.show_message(f"Could not clear desktop session: {exc}")
            return
        self.set_chat_status("New thread ready", "good")
        self.show_message("Desktop Hermes thread cleared.")

    def capture_session_id(self, text):
        match = re.search(r"^session_id:\s*(\S+)\s*$", text, flags=re.MULTILINE)
        if match:
            DESKTOP_SESSION_FILE.write_text(
                json.dumps(
                    {"session_id": match.group(1), "updated_at": datetime.now().isoformat()},
                    indent=2,
                )
            )
            text = re.sub(r"^session_id:\s*\S+\s*\n*", "", text, flags=re.MULTILINE).strip()
        return text

    def replace_last_thinking(self, text):
        buffer = self.chat_output.get_buffer()
        start, end = buffer.get_bounds()
        current = buffer.get_text(start, end, True)
        marker = "Rook:\nThinking..."
        if marker in current:
            current = current.rsplit(marker, 1)[0] + f"Rook:\n{text}"
        else:
            current = current + "\n\n" + text
        buffer.set_text(current)
        return False

    def stop_chat(self):
        if self.chat_proc and self.chat_proc.poll() is None:
            self.chat_proc.terminate()
            self.set_chat_status("Stopping reply", "warn")
        else:
            self.set_chat_status("No active reply", "muted")

    def show_message(self, message):
        dialog = Gtk.MessageDialog(
            transient_for=self.window,
            flags=0,
            message_type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.OK,
            text=message[:500],
        )
        dialog.run()
        dialog.destroy()
        return False


if __name__ == "__main__":
    app = HermesControl()
    raise SystemExit(app.run(None))

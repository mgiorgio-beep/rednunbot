#!/usr/bin/env python3
"""
Red Nun Agent Bot — Telegram + Claude agent loop
Runs on the Beelink server alongside Wheelhouse.

Setup:
  1. pip install python-telegram-bot anthropic requests paramiko --break-system-packages
  2. Create a Telegram bot via @BotFather → get your BOT_TOKEN
  3. Set env vars (or use .env file)
  4. python bot.py

Env vars:
  TELEGRAM_BOT_TOKEN   — from @BotFather
  ANTHROPIC_API_KEY    — from console.anthropic.com
  ALLOWED_USERS        — comma-separated Telegram user IDs (security!)
  TEMPSTICK_API_KEY    — from your Temp Stick account (Settings → API)
"""

import os
import json
import logging
import asyncio
import subprocess
import requests
from datetime import datetime, timedelta
from typing import Any

import anthropic
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
TEMPSTICK_API_KEY = os.environ.get("TEMPSTICK_API_KEY", "")
ALLOWED_USERS = [
    int(uid.strip())
    for uid in os.environ.get("ALLOWED_USERS", "").split(",")
    if uid.strip()
]

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("rednun-agent")

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


# ===========================================================================
# SERVER MONITORING & REMEDIATION
# ===========================================================================

# Whitelist of services the agent is allowed to monitor and restart.
# Add services here as you deploy them. Anything not listed is blocked.
ALLOWED_SERVICES = {
    "wheelhouse",           # Wheelhouse fishing dashboard
    "rednun-dashboard",     # Red Nun analytics dashboard
    "rednun-agent",         # This bot itself
    "nginx",                # Web server / reverse proxy
    "cloudflared",          # Cloudflare tunnel
    "postgresql",           # Database (if used)
    "redis",                # Cache (if used)
}

# URLs to health-check (agent pings these every 5 min)
HEALTH_CHECK_URLS = {
    "wheelhouse": "https://wheelhouse.rednun.com",
    "dashboard": "https://dashboard.rednun.com",
    # Add more as needed
}

# Servers the agent can reach. For the Beelink, commands run locally.
# For DigitalOcean, use SSH. Configure SSH key auth (no passwords).
SERVERS = {
    "beelink": {"type": "local"},  # bot runs here, so just subprocess
    "digitalocean": {
        "type": "ssh",
        "host": "dashboard.rednun.com",  # or the IP
        "user": "rednun",
        "port": 22,
        "key_file": os.path.expanduser("~/.ssh/id_rsa"),  # adjust path
    },
}

# Map services to which server they run on
SERVICE_SERVER_MAP = {
    "wheelhouse": "beelink",
    "rednun-agent": "beelink",
    "cloudflared": "beelink",
    "rednun-dashboard": "digitalocean",
    "nginx": "digitalocean",
    "postgresql": "digitalocean",
    "redis": "digitalocean",
}


def _run_local_cmd(cmd, timeout=15):
    """Run a command locally on the Beelink."""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        return {
            "stdout": result.stdout.strip()[-2000:],  # cap output
            "stderr": result.stderr.strip()[-500:],
            "returncode": result.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": "Command timed out", "returncode": -1}
    except Exception as e:
        return {"stdout": "", "stderr": str(e), "returncode": -1}


def _run_ssh_cmd(server_config, cmd, timeout=15):
    """Run a command on a remote server via SSH."""
    try:
        import paramiko
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(
            hostname=server_config["host"],
            port=server_config.get("port", 22),
            username=server_config["user"],
            key_filename=server_config.get("key_file"),
            timeout=10,
        )
        stdin, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
        out = stdout.read().decode().strip()[-2000:]
        err = stderr.read().decode().strip()[-500:]
        code = stdout.channel.recv_exit_status()
        ssh.close()
        return {"stdout": out, "stderr": err, "returncode": code}
    except Exception as e:
        return {"stdout": "", "stderr": str(e), "returncode": -1}


def run_server_cmd(server_name, cmd, timeout=15):
    """Route command to the right server."""
    server = SERVERS.get(server_name)
    if not server:
        return {"error": f"Unknown server: {server_name}"}
    if server["type"] == "local":
        return _run_local_cmd(cmd, timeout)
    elif server["type"] == "ssh":
        return _run_ssh_cmd(server, cmd, timeout)
    return {"error": f"Unknown server type: {server['type']}"}


def check_service_status(service_name):
    """Check if a systemd service is running."""
    if service_name not in ALLOWED_SERVICES:
        return {"error": f"Service '{service_name}' is not in the allowed list"}
    server = SERVICE_SERVER_MAP.get(service_name, "beelink")
    result = run_server_cmd(server, f"systemctl is-active {service_name} && systemctl show {service_name} --property=ActiveEnterTimestamp,MainPID,MemoryCurrent")
    
    # Parse into something readable
    output = result.get("stdout", "")
    lines = output.split("\n")
    status = lines[0] if lines else "unknown"
    
    return {
        "service": service_name,
        "server": server,
        "status": status,
        "details": output,
        "error": result.get("stderr") if result.get("returncode") != 0 else None,
    }


def read_service_logs(service_name, lines=50):
    """Read recent logs for a service."""
    if service_name not in ALLOWED_SERVICES:
        return {"error": f"Service '{service_name}' is not in the allowed list"}
    server = SERVICE_SERVER_MAP.get(service_name, "beelink")
    result = run_server_cmd(server, f"journalctl -u {service_name} -n {lines} --no-pager")
    return {
        "service": service_name,
        "server": server,
        "logs": result.get("stdout", ""),
        "error": result.get("stderr") if result.get("returncode") != 0 else None,
    }


def restart_service(service_name):
    """Restart a systemd service."""
    if service_name not in ALLOWED_SERVICES:
        return {"error": f"Service '{service_name}' is not in the allowed list"}
    if service_name == "rednun-agent":
        return {"error": "I can't restart myself — that would kill this conversation. Do it manually: sudo systemctl restart rednun-agent"}
    server = SERVICE_SERVER_MAP.get(service_name, "beelink")
    result = run_server_cmd(server, f"sudo systemctl restart {service_name}")
    
    # Verify it came back
    import time
    time.sleep(3)
    verify = run_server_cmd(server, f"systemctl is-active {service_name}")
    
    return {
        "service": service_name,
        "server": server,
        "action": "restarted",
        "new_status": verify.get("stdout", "unknown").strip(),
        "error": result.get("stderr") if result.get("returncode") != 0 else None,
    }


def run_server_diagnostic(server_name):
    """Run a health diagnostic on a server — disk, memory, CPU, top processes."""
    cmds = {
        "disk": "df -h / | tail -1",
        "memory": "free -m | grep Mem",
        "load": "uptime",
        "top_cpu": "ps aux --sort=-%cpu | head -6",
        "top_mem": "ps aux --sort=-%mem | head -6",
    }
    results = {}
    for name, cmd in cmds.items():
        r = run_server_cmd(server_name, cmd)
        results[name] = r.get("stdout", r.get("stderr", "failed"))
    
    return {"server": server_name, "diagnostics": results}


def check_url_health(name, url):
    """Check if a URL is responding."""
    try:
        resp = requests.get(url, timeout=10, allow_redirects=True)
        return {
            "name": name,
            "url": url,
            "status_code": resp.status_code,
            "response_time_ms": round(resp.elapsed.total_seconds() * 1000),
            "ok": 200 <= resp.status_code < 400,
        }
    except requests.Timeout:
        return {"name": name, "url": url, "status_code": None, "ok": False, "error": "Timeout"}
    except Exception as e:
        return {"name": name, "url": url, "status_code": None, "ok": False, "error": str(e)[:200]}


def check_all_endpoints():
    """Health-check all configured URLs."""
    results = []
    for name, url in HEALTH_CHECK_URLS.items():
        results.append(check_url_health(name, url))
    return {"endpoints": results}


# ===========================================================================
# TEMP STICK API
# ===========================================================================
TEMPSTICK_BASE_URL = "https://tempstickapi.com/api/v1"

SENSOR_MAP = {
    # "sensor_id": {"name": "Dennis Walk-In Cooler", "location": "dennis", "target_temp": 36, "alert_high": 42, "alert_low": 30},
}

_reading_history = {}


def tempstick_get_sensors():
    if not TEMPSTICK_API_KEY:
        return {"error": "TEMPSTICK_API_KEY not set"}
    resp = requests.get(
        f"{TEMPSTICK_BASE_URL}/sensors",
        headers={"Authorization": f"Bearer {TEMPSTICK_API_KEY}"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def tempstick_get_readings(sensor_id, duration="24_hours"):
    if not TEMPSTICK_API_KEY:
        return {"error": "TEMPSTICK_API_KEY not set"}
    resp = requests.get(
        f"{TEMPSTICK_BASE_URL}/sensors/{sensor_id}/readings",
        headers={"Authorization": f"Bearer {TEMPSTICK_API_KEY}"},
        params={"duration": duration},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def get_all_current_temps():
    try:
        data = tempstick_get_sensors()
    except Exception as e:
        return {"error": str(e)}

    results = []
    sensors = data.get("data", {}).get("sensors", [])

    for sensor in sensors:
        sensor_id = sensor.get("sensor_id", sensor.get("id", "unknown"))
        temp_f = sensor.get("last_temp")
        humidity = sensor.get("last_humidity")
        last_reading = sensor.get("last_reading_at") or sensor.get("last_checkin")
        sensor_name = sensor.get("sensor_name", sensor.get("name", "Unknown"))

        meta = SENSOR_MAP.get(str(sensor_id), {})
        status = "ok"
        if meta and temp_f is not None:
            if temp_f > meta.get("alert_high", 999):
                status = "🚨 HIGH"
            elif temp_f < meta.get("alert_low", -999):
                status = "🚨 LOW"
            elif abs(temp_f - meta.get("target_temp", temp_f)) > 3:
                status = "⚠️ DRIFT"

        if temp_f is not None:
            history = _reading_history.setdefault(str(sensor_id), [])
            history.append((datetime.now().isoformat(), temp_f))
            _reading_history[str(sensor_id)] = history[-100:]

        results.append({
            "sensor_id": sensor_id,
            "name": meta.get("name", sensor_name),
            "location": meta.get("location", "unknown"),
            "temp_f": temp_f, "humidity": humidity,
            "last_reading": last_reading,
            "status": status, "target_temp": meta.get("target_temp"),
        })

    return {"sensors": results, "count": len(results)}


def get_temp_trends(sensor_id):
    try:
        data = tempstick_get_readings(sensor_id, "24_hours")
    except Exception as e:
        return {"error": str(e)}

    readings = data.get("data", {}).get("readings", [])
    if not readings:
        return {"error": "No readings available"}

    temps = [r.get("temperature") or r.get("temp_f") for r in readings
             if r.get("temperature") or r.get("temp_f")]
    if not temps:
        return {"error": "No temperature data"}

    current = temps[-1]
    avg = sum(temps) / len(temps)
    quarter = max(1, len(temps) // 4)
    recent_avg = sum(temps[-quarter:]) / quarter
    early_avg = sum(temps[:quarter]) / quarter
    diff = recent_avg - early_avg

    if diff > 2:
        trend = f"📈 RISING ({diff:+.1f}°F)"
    elif diff < -2:
        trend = f"📉 FALLING ({diff:+.1f}°F)"
    else:
        trend = "➡️ STABLE"

    meta = SENSOR_MAP.get(str(sensor_id), {})
    return {
        "sensor_id": sensor_id, "name": meta.get("name", "Unknown"),
        "current_temp_f": current, "avg_temp_f": round(avg, 1),
        "high_temp_f": max(temps), "low_temp_f": min(temps),
        "trend": trend, "reading_count": len(temps), "period": "24 hours",
    }


# ===========================================================================
# SYSTEM PROMPT
# ===========================================================================
SYSTEM_PROMPT = """You are the Red Nun Operations Agent. You help Mike Giorgio
manage his restaurant and business operations on Cape Cod.

You have access to tools. When the user asks a question, decide which tool(s)
to call, interpret the results, and respond conversationally. Keep it concise —
this goes to Telegram on a phone screen.

Current date: {date}

Locations:
- Red Nun Public House: Dennis Port, MA (Cape Cod Five acct 2757)
- Red Buoy: Chatham, MA (Cape Cod Five acct 5975)

Servers:
- beelink: Runs Wheelhouse, this agent bot, Cloudflare tunnel (local, 10.1.10.83)
- digitalocean: Runs Red Nun dashboard, nginx, database (dashboard.rednun.com)

Mike's style: direct, no fluff, casual.

For server issues: diagnose first (check status + logs), explain what you found
in plain English, then fix if possible. Always tell Mike what you did and why.
If you can't fix it, tell him exactly what needs to happen.

For refrigeration: flag anything drifting or out of range. Walk-in cooler: 34-38°F.
Walk-in freezer: -5 to 5°F.
"""


# ===========================================================================
# TOOL DEFINITIONS
# ===========================================================================
TOOLS = [
    # ---- Restaurant Ops ----
    {
        "name": "get_daily_sales",
        "description": "Get sales summary for a location and date.",
        "input_schema": {
            "type": "object",
            "properties": {
                "location": {"type": "string", "enum": ["dennis", "chatham"]},
                "date": {"type": "string", "description": "YYYY-MM-DD. Defaults to yesterday."},
            },
            "required": ["location"],
        },
    },
    {
        "name": "get_labor_summary",
        "description": "Get labor hours and cost for a location.",
        "input_schema": {
            "type": "object",
            "properties": {
                "location": {"type": "string", "enum": ["dennis", "chatham"]},
                "start_date": {"type": "string"},
                "end_date": {"type": "string"},
            },
            "required": ["location"],
        },
    },
    {
        "name": "get_food_cost",
        "description": "Get food/bev cost percentages for a location and period.",
        "input_schema": {
            "type": "object",
            "properties": {
                "location": {"type": "string", "enum": ["dennis", "chatham"]},
                "period": {"type": "string", "enum": ["yesterday", "wtd", "mtd", "last_week"]},
            },
            "required": ["location"],
        },
    },
    {
        "name": "check_sync_status",
        "description": "Check if Toast/7shifts/QuickBooks data syncs are current.",
        "input_schema": {
            "type": "object",
            "properties": {
                "system": {"type": "string", "enum": ["toast", "7shifts", "quickbooks", "all"]},
            },
            "required": ["system"],
        },
    },
    {
        "name": "get_thermostat",
        "description": "Get HVAC thermostat readings (Honeywell TCC).",
        "input_schema": {
            "type": "object",
            "properties": {"location": {"type": "string", "enum": ["dennis", "chatham"]}},
            "required": ["location"],
        },
    },
    {
        "name": "get_weather",
        "description": "Get current weather and forecast for Cape Cod.",
        "input_schema": {
            "type": "object",
            "properties": {"days": {"type": "integer", "description": "Forecast days (1-5)"}},
        },
    },
    {
        "name": "run_payroll_check",
        "description": "Check if current pay period journal entry has been generated.",
        "input_schema": {
            "type": "object",
            "properties": {"location": {"type": "string", "enum": ["dennis", "chatham"]}},
            "required": ["location"],
        },
    },

    # ---- Refrigeration (Temp Stick) ----
    {
        "name": "get_refrigeration_temps",
        "description": "Get current temperatures from all Temp Stick sensors. Shows walk-in coolers, freezers with status (ok, drift, alert).",
        "input_schema": {
            "type": "object",
            "properties": {
                "location": {"type": "string", "enum": ["dennis", "chatham", "all"], "description": "Defaults to all."},
            },
        },
    },
    {
        "name": "get_temp_trend",
        "description": "Analyze 24h temperature trend for a sensor. Use when drift or alert detected.",
        "input_schema": {
            "type": "object",
            "properties": {"sensor_id": {"type": "string"}},
            "required": ["sensor_id"],
        },
    },

    # ---- Server Monitoring & Remediation ----
    {
        "name": "check_service",
        "description": "Check if a service is running. Use when Mike reports something is down or broken. Allowed services: wheelhouse, rednun-dashboard, rednun-agent, nginx, cloudflared, postgresql, redis.",
        "input_schema": {
            "type": "object",
            "properties": {
                "service": {"type": "string", "description": "The systemd service name."},
            },
            "required": ["service"],
        },
    },
    {
        "name": "read_logs",
        "description": "Read recent log lines for a service. Use to diagnose why something crashed or is misbehaving. Returns the last N lines from journalctl.",
        "input_schema": {
            "type": "object",
            "properties": {
                "service": {"type": "string", "description": "The systemd service name."},
                "lines": {"type": "integer", "description": "Number of log lines (default 50, max 200)."},
            },
            "required": ["service"],
        },
    },
    {
        "name": "restart_service",
        "description": "Restart a service. Use ONLY after diagnosing the issue with check_service and read_logs first. Always explain what you found before restarting. Cannot restart rednun-agent (self).",
        "input_schema": {
            "type": "object",
            "properties": {
                "service": {"type": "string", "description": "The systemd service name."},
            },
            "required": ["service"],
        },
    },
    {
        "name": "server_diagnostic",
        "description": "Run a full health check on a server: disk space, memory, CPU load, top processes. Use when diagnosing performance issues or before/after a restart.",
        "input_schema": {
            "type": "object",
            "properties": {
                "server": {"type": "string", "enum": ["beelink", "digitalocean"], "description": "Which server to check."},
            },
            "required": ["server"],
        },
    },
    {
        "name": "check_endpoints",
        "description": "Health-check all web endpoints (wheelhouse.rednun.com, dashboard.rednun.com). Returns HTTP status, response time, and whether each is up.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
]


# ===========================================================================
# TOOL IMPLEMENTATIONS
# ===========================================================================
def execute_tool(name: str, args: dict) -> str:

    # ---- TEMP STICK (LIVE) ----
    if name == "get_refrigeration_temps":
        try:
            result = get_all_current_temps()
            loc = args.get("location", "all")
            if loc != "all" and "sensors" in result:
                result["sensors"] = [s for s in result["sensors"] if s.get("location") == loc or s.get("location") == "unknown"]
                result["count"] = len(result["sensors"])
            return json.dumps(result)
        except Exception as e:
            return json.dumps({"error": str(e)})

    elif name == "get_temp_trend":
        try:
            return json.dumps(get_temp_trends(args["sensor_id"]))
        except Exception as e:
            return json.dumps({"error": str(e)})

    # ---- SERVER MONITORING (LIVE) ----
    elif name == "check_service":
        return json.dumps(check_service_status(args["service"]))

    elif name == "read_logs":
        lines = min(args.get("lines", 50), 200)
        return json.dumps(read_service_logs(args["service"], lines))

    elif name == "restart_service":
        return json.dumps(restart_service(args["service"]))

    elif name == "server_diagnostic":
        return json.dumps(run_server_diagnostic(args["server"]))

    elif name == "check_endpoints":
        return json.dumps(check_all_endpoints())

    # ---- STUBS (wire up later) ----
    elif name == "get_daily_sales":
        return json.dumps({
            "location": args.get("location", "dennis"),
            "date": args.get("date", "2026-04-08"),
            "net_sales": 8432.50, "covers": 142, "avg_check": 59.38,
            "labor_pct": 28.3, "comps": 125.00,
            "note": "⚠️ STUB — wire up Toast API",
        })

    elif name == "get_labor_summary":
        return json.dumps({
            "location": args.get("location"),
            "total_hours": 312.5, "labor_cost": 5842.00, "overtime_hours": 4.5,
            "note": "⚠️ STUB — wire up 7shifts API",
        })

    elif name == "get_food_cost":
        return json.dumps({
            "location": args.get("location"),
            "period": args.get("period", "wtd"),
            "food_cost_pct": 31.2, "bev_cost_pct": 19.8,
            "note": "⚠️ STUB — wire up cost tracking",
        })

    elif name == "check_sync_status":
        return json.dumps({
            "toast": {"last_sync": "2026-04-08T23:45:00", "status": "ok"},
            "7shifts": {"last_sync": "2026-04-08T22:00:00", "status": "ok"},
            "quickbooks": {"last_sync": "2026-04-07T08:00:00", "status": "⚠️ 36hrs stale"},
            "note": "⚠️ STUB — wire up sync monitoring",
        })

    elif name == "get_thermostat":
        return json.dumps({
            "location": args.get("location"),
            "zones": {"dining": {"current": 68, "set": 70}, "kitchen": {"current": 74, "set": 72}},
            "note": "⚠️ STUB — wire up Honeywell TCC",
        })

    elif name == "get_weather":
        return json.dumps({
            "current": {"temp": 52, "condition": "Partly cloudy", "wind": "SW 12mph"},
            "forecast": "Rain likely tomorrow, clearing Friday",
            "note": "⚠️ STUB — wire up weather API",
        })

    elif name == "run_payroll_check":
        return json.dumps({
            "location": args.get("location"),
            "current_period": "03/31/26 - 04/13/26",
            "journal_entry_status": "not_generated",
            "note": "⚠️ STUB — wire up payroll pipeline",
        })

    return json.dumps({"error": f"Unknown tool: {name}"})


# ===========================================================================
# CLAUDE AGENT LOOP
# ===========================================================================
async def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    system = SYSTEM_PROMPT.format(date=datetime.now().strftime("%Y-%m-%d %A"))

    for _ in range(10):
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
            system=system,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            parts = [b.text for b in response.content if b.type == "text"]
            return "\n".join(parts) or "Done."

        assistant_content = []
        tool_results = []

        for block in response.content:
            if block.type == "text":
                assistant_content.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                assistant_content.append({
                    "type": "tool_use", "id": block.id,
                    "name": block.name, "input": block.input,
                })
                logger.info(f"Tool call: {block.name}({block.input})")
                result = execute_tool(block.name, block.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

        messages.append({"role": "assistant", "content": assistant_content})
        messages.append({"role": "user", "content": tool_results})

    return "Agent hit iteration limit."


# ===========================================================================
# TELEGRAM HANDLERS
# ===========================================================================
def is_authorized(user_id: int) -> bool:
    if not ALLOWED_USERS:
        logger.warning("No ALLOWED_USERS set — bot is open!")
        return True
    return user_id in ALLOWED_USERS


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    await update.message.reply_text(
        "🔴 Red Nun Agent online.\n\n"
        "Ask me anything — sales, labor, food cost, walk-in temps, "
        "server status, sync issues, weather, payroll.\n\n"
        "Examples:\n"
        "• How'd Dennis do last night?\n"
        "• What are the walk-in temps?\n"
        "• The dashboard is down\n"
        "• Is the Beelink running ok?\n"
        "• Any sync issues?\n\n"
        "Commands:\n"
        "/briefing — Full morning ops briefing\n"
        "/temps — Quick refrigeration check\n"
        "/status — All services + endpoints"
    )


async def cmd_briefing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    await update.message.reply_text("Building your briefing... ☕")
    result = await run_agent(
        "Full morning briefing: yesterday's sales and labor at both "
        "locations, sync issues, all refrigeration temps, thermostat, "
        "web endpoint health, today's weather. Keep it tight."
    )
    await update.message.reply_text(result)


async def cmd_temps(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    try:
        result = get_all_current_temps()
        if "error" in result:
            await update.message.reply_text(f"❌ {result['error']}")
            return
        lines = ["🌡️ Refrigeration Status\n"]
        for s in result.get("sensors", []):
            icon = {"ok": "✅", "⚠️ DRIFT": "⚠️", "🚨 HIGH": "🚨", "🚨 LOW": "🚨"}.get(s["status"], "❓")
            target = f" (target: {s['target_temp']}°F)" if s.get("target_temp") is not None else ""
            lines.append(f"{icon} {s['name']}: {s['temp_f']}°F{target}")
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        await update.message.reply_text(f"❌ {str(e)[:200]}")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Quick system status — no Claude call needed."""
    if not is_authorized(update.effective_user.id):
        return
    lines = ["🖥️ System Status\n"]

    # Check endpoints
    for name, url in HEALTH_CHECK_URLS.items():
        r = check_url_health(name, url)
        icon = "✅" if r["ok"] else "🔴"
        ms = f" ({r['response_time_ms']}ms)" if r.get("response_time_ms") else ""
        err = f" — {r['error']}" if r.get("error") else ""
        lines.append(f"{icon} {name}{ms}{err}")

    # Check key services
    lines.append("")
    for svc in ["wheelhouse", "rednun-dashboard", "nginx", "cloudflared"]:
        try:
            r = check_service_status(svc)
            icon = "✅" if r.get("status") == "active" else "🔴"
            lines.append(f"{icon} {svc}: {r.get('status', 'unknown')}")
        except Exception:
            lines.append(f"❓ {svc}: check failed")

    await update.message.reply_text("\n".join(lines))


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    user_msg = update.message.text
    logger.info(f"Message from {update.effective_user.id}: {user_msg}")
    await update.message.chat.send_action("typing")
    try:
        result = await run_agent(user_msg)
        if len(result) > 4000:
            for i in range(0, len(result), 4000):
                await update.message.reply_text(result[i : i + 4000])
        else:
            await update.message.reply_text(result)
    except Exception as e:
        logger.error(f"Agent error: {e}")
        await update.message.reply_text(f"Agent error: {str(e)[:200]}")


# ===========================================================================
# BACKGROUND MONITORS
# ===========================================================================
async def refrigeration_monitor(context: ContextTypes.DEFAULT_TYPE):
    """Every 15 min: check temps, alert on problems."""
    if not TEMPSTICK_API_KEY:
        return
    try:
        result = get_all_current_temps()
    except Exception as e:
        logger.error(f"Fridge monitor error: {e}")
        return

    alerts = [s for s in result.get("sensors", []) if s["status"] != "ok"]
    if not alerts:
        return

    lines = ["🚨 REFRIGERATION ALERT\n"]
    for a in alerts:
        target = f" (target: {a['target_temp']}°F)" if a.get("target_temp") is not None else ""
        lines.append(f"{a['status']} {a['name']}: {a['temp_f']}°F{target}")

    critical = [a for a in alerts if "🚨" in a["status"]]
    if critical:
        lines.append("")
        for sensor in critical:
            try:
                trend = get_temp_trends(str(sensor["sensor_id"]))
                if "trend" in trend:
                    lines.append(f"📊 {sensor['name']}: {trend['trend']}")
                    lines.append(f"   24h: {trend['low_temp_f']}°F – {trend['high_temp_f']}°F")
            except Exception:
                pass
        lines.append("\n💡 Check compressor, door seals, or defrost cycle.")

    msg = "\n".join(lines)
    for user_id in ALLOWED_USERS:
        try:
            await context.bot.send_message(chat_id=user_id, text=msg)
        except Exception as e:
            logger.error(f"Alert failed for {user_id}: {e}")


async def endpoint_monitor(context: ContextTypes.DEFAULT_TYPE):
    """Every 5 min: check web endpoints, alert + auto-restart on failure."""
    for name, url in HEALTH_CHECK_URLS.items():
        r = check_url_health(name, url)
        if r["ok"]:
            continue

        # Something is down — try to identify and restart the service
        # Map endpoint name to service
        svc_map = {"wheelhouse": "wheelhouse", "dashboard": "rednun-dashboard"}
        svc = svc_map.get(name)

        lines = [f"🔴 {name} is DOWN"]
        if r.get("error"):
            lines.append(f"Error: {r['error']}")
        elif r.get("status_code"):
            lines.append(f"HTTP {r['status_code']}")

        if svc:
            # Check the service
            status = check_service_status(svc)
            if status.get("status") != "active":
                lines.append(f"Service '{svc}' is {status.get('status', 'unknown')} — restarting...")
                restart_result = restart_service(svc)
                new_status = restart_result.get("new_status", "unknown")
                if new_status == "active":
                    lines.append(f"✅ Restarted successfully — {name} should be back up.")
                else:
                    lines.append(f"⚠️ Restart attempted but status is: {new_status}")
                    lines.append("Manual intervention may be needed.")
            else:
                lines.append(f"Service '{svc}' is running — could be nginx, network, or app error.")
                lines.append("Check logs with: /status or ask me to read logs.")

        msg = "\n".join(lines)
        for user_id in ALLOWED_USERS:
            try:
                await context.bot.send_message(chat_id=user_id, text=msg)
            except Exception as e:
                logger.error(f"Alert failed for {user_id}: {e}")


async def scheduled_briefing(context: ContextTypes.DEFAULT_TYPE):
    result = await run_agent(
        "Morning briefing: yesterday's sales and labor at both locations, "
        "sync status, all refrigeration temps, endpoint health, "
        "thermostat check, today's weather. Quick phone format."
    )
    for user_id in ALLOWED_USERS:
        try:
            await context.bot.send_message(chat_id=user_id, text=f"☀️ Morning Briefing\n\n{result}")
        except Exception as e:
            logger.error(f"Briefing failed for {user_id}: {e}")


# ===========================================================================
# MAIN
# ===========================================================================
def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("briefing", cmd_briefing))
    app.add_handler(CommandHandler("temps", cmd_temps))
    app.add_handler(CommandHandler("status", cmd_status))

    # All text → agent
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    from datetime import time as dt_time

    # Morning briefing at 7am ET
    app.job_queue.run_daily(
        scheduled_briefing,
        time=dt_time(hour=11, minute=0),
        name="morning_briefing",
    )

    # Refrigeration monitor every 15 min
    app.job_queue.run_repeating(
        refrigeration_monitor, interval=900, first=30,
        name="fridge_monitor",
    )

    # Endpoint monitor every 5 min
    app.job_queue.run_repeating(
        endpoint_monitor, interval=300, first=60,
        name="endpoint_monitor",
    )

    logger.info("Red Nun Agent Bot starting...")
    logger.info(f"Temp Stick: {'✅' if TEMPSTICK_API_KEY else '❌ NOT SET'}")
    logger.info(f"Health checks: {list(HEALTH_CHECK_URLS.keys())}")
    logger.info(f"Users: {ALLOWED_USERS}")
    app.run_polling()


if __name__ == "__main__":
    main()

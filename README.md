# Red Nun Agent Bot

Telegram bot + Claude agent for restaurant ops monitoring.

## What It Does
- **Ask anything** via Telegram — sales, labor, food cost, walk-in temps, sync status, weather, payroll
- **7am morning briefing** auto-sent to your phone
- **Refrigeration monitoring** every 15 min via Temp Stick API — alerts you on Telegram if anything drifts or goes critical
- **Trend analysis** — detects slow temperature creep (failing compressor, bad door seal) before it becomes an emergency
- `/temps` command for instant refrigeration status without waiting for Claude
- `/briefing` command for on-demand morning report

## Quick Start

### 1. Telegram Bot
- Open Telegram → search **@BotFather** → `/newbot` → save the token
- Search **@userinfobot** → send any message → save your user ID

### 2. API Keys
- **Anthropic**: https://console.anthropic.com → create API key
- **Temp Stick**: Log into your Temp Stick account → Settings → API → copy key

### 3. Deploy on Beelink

```bash
ssh -p 2222 rednun@ssh.rednun.com
mkdir -p ~/rednun-agent && cd ~/rednun-agent

# Copy bot.py and .env.example here

pip install python-telegram-bot anthropic requests python-dotenv --break-system-packages

cp .env.example .env
nano .env  # fill in all 4 values

# Test run
python bot.py
```

### 4. Configure Sensor Map

Once your Temp Sticks are set up, run the bot and ask it "what are the temps?" — it'll show sensor IDs. Then edit the `SENSOR_MAP` in bot.py:

```python
SENSOR_MAP = {
    "abc123": {"name": "Dennis Walk-In Cooler", "location": "dennis", "target_temp": 36, "alert_high": 42, "alert_low": 30},
    "def456": {"name": "Dennis Freezer", "location": "dennis", "target_temp": 0, "alert_high": 10, "alert_low": -10},
    "ghi789": {"name": "Chatham Walk-In Cooler", "location": "chatham", "target_temp": 36, "alert_high": 42, "alert_low": 30},
}
```

### 5. Run as Service

```bash
sudo tee /etc/systemd/system/rednun-agent.service << 'EOF'
[Unit]
Description=Red Nun Agent Bot
After=network.target

[Service]
Type=simple
User=rednun
WorkingDirectory=/home/rednun/rednun-agent
EnvironmentFile=/home/rednun/rednun-agent/.env
ExecStart=/usr/bin/python3 /home/rednun/rednun-agent/bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable rednun-agent
sudo systemctl start rednun-agent
```

## Wiring Up More Tools

The bot has stub functions for sales, labor, food cost, etc. Replace them one at a time with real API calls to Toast, 7shifts, QuickBooks. The Temp Stick integration is live out of the box.

## Cost Estimate
- Claude API: ~$10-15/month at normal usage
- Temp Stick sensors: ~$150 each, no subscription fees
- Telegram: free

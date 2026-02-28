# WSL2 Server Setup & Deploy Guide

## Overview

This guide covers running the Story RPG server on a WSL2 (Windows Subsystem for Linux) machine, accessible remotely via Tailscale or local network.

## Prerequisites

### 1. Install Python dependencies

```bash
# Download and install pip
python3 /tmp/get-pip.py --user --break-system-packages

# Install project dependencies
~/.local/bin/pip install -r requirements.txt --user --break-system-packages
```

### 2. Install Claude CLI

```bash
npm install -g @anthropic-ai/claude-code
```

Claude CLI will be at `~/.npm-global/bin/claude`.

### 3. Windows Port Forwarding (one-time setup)

Run in PowerShell (Administrator) to expose WSL2 ports to the network:

```powershell
# SSH (port 2222 → WSL2)
netsh interface portproxy add v4tov4 listenport=2222 listenaddress=0.0.0.0 connectport=2222 connectaddress=<WSL2_IP>
New-NetFirewallRule -DisplayName "WSL2 SSH" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 2222

# Flask server (port 5051 → WSL2)
netsh interface portproxy add v4tov4 listenport=5051 listenaddress=0.0.0.0 connectport=5051 connectaddress=<WSL2_IP>
New-NetFirewallRule -DisplayName "WSL2 Flask 5051" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 5051
```

Get the WSL2 IP with:
```bash
hostname -I
```

### 4. SSH Server (auto-start on WSL2 boot)

Enable systemd and let it manage SSH. Add to `/etc/wsl.conf`:

```ini
[boot]
systemd=true
```

Then enable SSH via systemd:
```bash
sudo systemctl enable ssh
sudo systemctl start ssh
```

> **Note:** Do not use `command = /usr/sbin/sshd` in `[boot]` when `systemd=true` is set — they conflict.

### 5. Auto-start WSL2 on Windows Login

WSL2 does not start automatically after a Windows reboot. Without this step, systemd never runs and SSH stays down.

Run in **PowerShell (Administrator)**:

```powershell
$action = New-ScheduledTaskAction -Execute "wsl.exe" -Argument "-d Ubuntu --exec echo init"
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Seconds 30)
Register-ScheduledTask -TaskName "WSL2 AutoStart" -Action $action -Trigger $trigger -Settings $settings -RunLevel Highest -Force
```

Replace `Ubuntu` with your distro name (check with `wsl -l`).

**Verify** after setup:
```powershell
wsl --shutdown
Start-ScheduledTask -TaskName "WSL2 AutoStart"
# wait 10 seconds
wsl -e bash -c "systemctl status ssh --no-pager"
```

Should show `Active: active (running)`.

### 6. Auto-start RPG Server via systemd

Create a systemd service so the Flask server starts automatically with WSL2:

```bash
sudo nano /etc/systemd/system/rpg-server.service
```

```ini
[Unit]
Description=Story RPG Server
After=network.target ssh.service

[Service]
Type=simple
User=eddylai
WorkingDirectory=/home/eddylai/project/agent-story
ExecStart=/usr/bin/python3 app.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable and start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable rpg-server
sudo systemctl start rpg-server
```

Verify:
```bash
sudo systemctl status rpg-server
```

### 7. Remote Access via Tailscale

Install Tailscale on both Windows and the remote machine, log in with the same account. Use the Tailscale IP (`100.x.x.x`) to connect from anywhere.

## Starting the Server

### Automatic (after setup above)

Both SSH and the RPG server start automatically when WSL2 boots. No manual action needed after a Windows reboot.

### Manual start

```bash
PYTHONPATH="$HOME/.local/lib/python3.12/site-packages" \
CLAUDE_BIN="$HOME/.npm-global/bin/claude" \
nohup /usr/bin/python3 app.py >> server.log 2>&1 &
```

### Deploy script (pull latest + restart)

```bash
./deploy_wsl2.sh
```

This will:
1. Pull latest `main` from origin
2. Kill any existing server on port 5051
3. Start the server with correct `PYTHONPATH` and `CLAUDE_BIN`
4. Verify the server is responding

## Environment Variables

| Variable | WSL2 Value | Purpose |
|----------|-----------|---------|
| `PYTHONPATH` | `~/.local/lib/python3.12/site-packages` | User-installed Python packages |
| `CLAUDE_BIN` | `~/.npm-global/bin/claude` | Claude CLI path (overrides Mac default) |
| `PORT` | `5051` (default) | Flask server port |

## Notes

- The default `CLAUDE_BIN` in `claude_bridge.py` is set to a Mac path (`/Users/eddylai/.local/bin/claude`). The `CLAUDE_BIN` env var overrides this for WSL2.
- Production on Mac uses `deploy.sh`; WSL2 uses `deploy_wsl2.sh`.
- Data files (`data/`) are gitignored and not affected by deploys.
- The `rpg-server` systemd service does not inherit shell env vars. If `PYTHONPATH` or `CLAUDE_BIN` are needed, add `Environment=` lines to the `[Service]` section.

# Fuel Forecast Bot

Automated Philippine fuel price forecast bot. Fetches live Brent crude oil prices, USD/PHP exchange rates, and DOE adjustment data — calculates the weekly forecast and sends it to a Telegram channel.

Runs fully automated via GitHub Actions triggered by [cron-job.org](https://cron-job.org).

---

## Run with Docker

No Python installation needed. Just Docker.

### Option A — Docker Desktop (recommended)
1. Download and install [Docker Desktop](https://www.docker.com/products/docker-desktop)
2. Run the command below

### Option B — Docker Portable (no admin required)
If you're on a company laptop or can't install Docker Desktop, use DockerPortable + QEMU (no admin, no WSL2, no Hyper-V required).

**Pre-requisites:** Git for Windows and 7-Zip installed. Always use Windows PowerShell 64-bit.

**Step 1 — Allow PowerShell scripts to run**
```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

**Step 2 — Download and extract QEMU**
```powershell
Invoke-WebRequest -Uri "https://qemu.weilnetz.de/w64/qemu-w64-setup-20241112.exe" -OutFile "$env:USERPROFILE\Downloads\qemu-setup.exe"
```
Then right-click `qemu-setup.exe` → 7-Zip → Extract to `qemu-setup\`, then:
```powershell
Move-Item "$env:USERPROFILE\Downloads\qemu-setup" "$env:USERPROFILE\QEMU"
```

**Step 3 — Add QEMU to PATH**
```powershell
[Environment]::SetEnvironmentVariable("Path", [Environment]::GetEnvironmentVariable("Path","User") + ";$env:USERPROFILE\QEMU", "User")
```
Verify: `qemu-system-x86_64 --version`

**Step 4 — Download DockerPortable**
```powershell
git clone https://github.com/knockshore/dockerportable.git "$env:USERPROFILE\dockerportable"
```

**Step 5 — Start the Docker VM**
```powershell
cd "$env:USERPROFILE\dockerportable"
.\boot.bat
```
Wait for the login prompt (30–90 seconds). Leave this window open.

**Step 6 — Connect and use Docker**

Open a second PowerShell window:
```powershell
cd "$env:USERPROFILE\dockerportable"
.\connect.bat
```
Log in as `root` (no password). You now have a working Docker environment.

Test it:
```bash
docker run hello-world
```

**Daily use:**
- Start Docker: `.\boot.bat` (keep open)
- Use Docker: new window → `.\connect.bat`
- Stop Docker: type `exit` in connect window → close boot window

**Troubleshooting:**
- `Port 22 already in use` — open `boot.bat`, change `hostfwd=tcp::22-:22` to `hostfwd=tcp::2222-:22`, update `connect.bat` with `-p 2222`
- `qemu-system-x86_64 not recognized` — re-run Step 3 and reopen PowerShell
- QEMU window stuck — wait up to 2 minutes, then close and rerun `boot.bat`

---

## Run the Bot

```bash
docker run \
  -e TELEGRAM_BOT_TOKEN=your_bot_token \
  -e TELEGRAM_CHAT_ID=@YourChannelName \
  wyethmontana/fuel-forecast-bot
```

### Environment Variables

| Variable | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Your Telegram bot token from BotFather |
| `TELEGRAM_CHAT_ID` | Your Telegram channel (e.g. `@YourChannel`) or chat ID |

---

## How It Works

1. Fetches live **Brent crude oil price** from Yahoo Finance
2. Fetches live **USD/PHP exchange rate** from open.er-api.com
3. Scrapes **DOE weekly adjustment** from GMA News
4. Applies DOE calculation methodology (MOPS dampener, forex factor, fuel-type multipliers)
5. Sends formatted forecast to Telegram

---

## Automated Schedule

The bot runs automatically via [cron-job.org](https://cron-job.org) triggering the GitHub Actions `workflow_dispatch` endpoint. No manual intervention needed.

---

## Docker Image

Available on Docker Hub: [wyethmontana/fuel-forecast-bot](https://hub.docker.com/r/wyethmontana/fuel-forecast-bot)

The image is automatically rebuilt and republished to Docker Hub on every push to `main`.

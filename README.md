# JeevesBot — Project Zomboid Server Management

A Discord bot that manages a Project Zomboid dedicated server. Features include RCON control, mod update detection, automatic restarts, chat relay, player tracking, rank synchronization, and integration with the Jeeves mod ecosystem.

---

## Table of Contents

1. [Requirements](#requirements)
2. [Creating a Discord Bot](#creating-a-discord-bot)
3. [Installation](#installation)
4. [Configuration](#configuration)
5. [Running the Bot](#running-the-bot)
6. [Commands](#commands)
7. [Features](#features)
8. [Jeeves Ecosystem](#jeeves-ecosystem)
9. [Building from Source](#building-from-source)
10. [Troubleshooting](#troubleshooting)

---

## Requirements

- **Windows or Linux** — The bot runs on the same machine as your PZ dedicated server. Both platforms are fully supported.
- **Project Zomboid Dedicated Server** — Installed via SteamCMD with RCON enabled.
- **Discord Account** — You need to create a Discord bot application (free).
- **SteamCMD** *(optional)* — Only required if you want to use the `/update` command to update the server from Discord.

If running from the pre-built executable, no Python installation is needed.
If running from source or building yourself, you need **Python 3.10+**.

---

## Creating a Discord Bot

Before you can use JeevesBot, you need to create a Discord bot application and invite it to your server. This is free and takes about 5 minutes.

### Step 1: Create the Application

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications)
2. Click **"New Application"** in the top right
3. Name it something like "Jeeves" and click **Create**
4. On the **General Information** page, you can optionally upload an avatar for the bot

### Step 2: Create the Bot User

1. Click **"Bot"** in the left sidebar
2. Click **"Reset Token"** and copy the token — **save this somewhere safe, you will need it for config.env**. You can only see the token once. If you lose it, you'll need to reset it again.
3. Under **Privileged Gateway Intents**, enable all three:
   - **Presence Intent** — ✅
   - **Server Members Intent** — ✅ (required for rank sync)
   - **Message Content Intent** — ✅ (required for chat relay)

### Step 3: Set Bot Permissions

1. Click **"OAuth2"** in the left sidebar
2. Under **OAuth2 URL Generator**, check the **"bot"** and **"applications.commands"** scopes
3. Under **Bot Permissions**, check:
   - Send Messages
   - Embed Links
   - Read Message History
   - Use Slash Commands
   - Manage Messages *(optional, for chat relay cleanup)*
4. Copy the generated URL at the bottom

### Step 4: Invite the Bot to Your Server

1. Paste the URL from Step 3 into your browser
2. Select your Discord server from the dropdown
3. Click **Authorize**
4. The bot should now appear in your server's member list (offline until you start it)

### Step 5: Get Your Server and Channel IDs

You need two Discord IDs for the configuration:

1. **Enable Developer Mode**: In Discord, go to User Settings → Advanced → Enable **Developer Mode**
2. **Guild (Server) ID**: Right-click your server name in the sidebar → **Copy Server ID**
3. **Channel ID**: Right-click the channel you want the bot to post in → **Copy Channel ID**
4. **Chat Relay Channel ID** *(optional)*: Right-click a second channel for in-game chat relay → **Copy Channel ID**

---

## Installation

### Option A: Pre-Built Executable (Windows — Recommended)

1. Extract the `Jeeves` folder to a location on your server machine
2. Copy `config.env.example` to `config.env`
3. Edit `config.env` with your settings (see [Configuration](#configuration))
4. Run `Jeeves.exe`

### Option B: Running from Source (Windows)

1. Install [Python 3.10+](https://www.python.org/downloads/) — check **"Add Python to PATH"** during installation
2. Open a command prompt in the JeevesBot folder
3. Install dependencies:
   ```
   pip install -r requirements.txt
   ```
4. Copy `config.env.example` to `config.env`
5. Edit `config.env` with your settings
6. Run:
   ```
   python Jeeves.py
   ```

### Option C: Running from Source (Linux — Recommended for Linux)

1. Install Python 3.10+ if not already present:
   ```bash
   # Ubuntu/Debian
   sudo apt install python3 python3-pip
   # Fedora/RHEL
   sudo dnf install python3 python3-pip
   ```
2. Copy the JeevesBot folder to your server
3. Install dependencies:
   ```bash
   chmod +x install.sh run.sh
   ./install.sh
   ```
4. Copy `config.env.example` to `config.env` and edit with your Linux paths:
   ```bash
   cp config.env.example config.env
   nano config.env
   ```
5. Run:
   ```bash
   ./run.sh
   ```

See [Running as a systemd Service](#running-as-a-systemd-service-linux) for production use on Linux.

---

## Configuration

Edit `config.env` with a text editor (Notepad works fine). Every line that says `Your...` must be replaced with your actual values.

### Required Settings

| Setting | Description |
|---------|-------------|
| `DISCORD_TOKEN` | The bot token from Step 2 of [Creating a Discord Bot](#step-2-create-the-bot-user) |
| `DISCORD_CHANNEL_ID` | The Discord channel ID where the bot posts status messages |
| `DISCORD_GUILD_ID` | Your Discord server ID |
| `RCON_PASSWORD` | Must match the RCON password in your PZ server settings |
| `RCON_PORT` | Must match the RCON port in your PZ server settings (default: 27015) |
| `SERVER_BATCH` | Full path to your server start script (`StartServer64.bat` on Windows, `start-server.sh` on Linux) |
| `SERVER_INI_PATH` | Full path to your server's `.ini` file (e.g., `C:\Users\You\Zomboid\Server\MyServer.ini` or `/home/pzuser/Zomboid/Server/MyServer.ini`) |
| `MODS_FOLDER_PATH` | Path to the Workshop content folder (usually `.../steamapps/workshop/content/108600`) |

### Optional Settings

| Setting | Description |
|---------|-------------|
| `UPDATE_LOG_PATH` | Where the bot stores mod update timestamps (default: next to the exe) |
| `STEAMCMD_PATH` | Path to SteamCMD (`steamcmd.exe` on Windows, `/usr/games/steamcmd` on Linux) — only needed for `/update` |
| `CHAT_RELAY_CHANNEL_ID` | Discord channel ID for bidirectional chat relay |
| `CHAT_LOG_PATH` | Path to your PZ server's Logs folder (for chat relay) |
| `USER_LOG_PATH` | Path to your PZ server's Logs folder (for player tracking) |
| `DEFAULT_ROLE` | Discord role name required to use admin commands (default: "Admin") |
| `RANK_1` through `RANK_6` | Discord role names that map to in-game rank colors |

### Startup Timing (Optional)

If your server is heavily modded or has large maps, it may need more time to start up before the bot begins checking RCON. These settings are optional — the defaults work for most servers.

| Setting | Default | Description |
|---------|---------|-------------|
| `STARTUP_WAIT` | `120` | Seconds to wait after launching the server before polling RCON |
| `CHECK_INTERVAL` | `30` | Seconds between RCON retry attempts during startup monitoring |
| `MONITOR_RETRIES` | `20` | Number of RCON retries before giving up (total wait = STARTUP_WAIT + CHECK_INTERVAL × MONITOR_RETRIES) |

For a very large server with many mods and 16+ GB heap, you might want `STARTUP_WAIT=180` and `MONITOR_RETRIES=30`.

### Custom Emojis (Optional)

By default, the bot uses standard Unicode emoji in its messages. If you want custom emoji (like the Project Zomboid Spiffo emotes), upload them to your Discord server, then add their IDs to config.env:

```
EMOJI_HAPPY=<:pzhappy:1234567890123456789>
EMOJI_PANIC=<:pzpanic:1234567890123456789>
```

To get an emoji ID: type `\:emojiname:` in Discord chat and send it. Discord will show the raw format with the ID.

---

## Running the Bot

### First Run

1. Make sure your PZ dedicated server is configured with RCON enabled
2. Start `Jeeves.exe` (or `python Jeeves.py`)
3. The bot will validate your configuration and print any errors
4. On successful start, the bot will:
   - Connect to Discord
   - Check if the PZ server is running via RCON
   - Start the server if it's offline
   - Register slash commands (may take up to an hour on first run for Discord to propagate)
5. Once you see "Server is Online!" in your Discord channel, the bot is fully operational

### Running as a Service

For production use, you'll want the bot to start automatically.

**Windows:**

1. Create a shortcut to `Jeeves.exe`
2. Press `Win+R`, type `shell:startup`, press Enter
3. Move the shortcut into the Startup folder

For more robust options, use [NSSM](https://nssm.cc/) to register it as a Windows service.

**Linux (systemd):**
<a name="running-as-a-systemd-service-linux"></a>

Create a service file at `/etc/systemd/system/jeevesbot.service`:

```ini
[Unit]
Description=JeevesBot — PZ Server Manager
After=network.target

[Service]
Type=simple
User=pzuser
WorkingDirectory=/home/pzuser/JeevesBot
ExecStart=/usr/bin/python3 /home/pzuser/JeevesBot/Jeeves.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Then enable and start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable jeevesbot
sudo systemctl start jeevesbot
sudo systemctl status jeevesbot   # verify it's running
sudo journalctl -u jeevesbot -f   # tail logs
```

---

## Commands

All slash commands require the role specified by `DEFAULT_ROLE` in your config unless noted otherwise.

### Server Management

| Command | Description |
|---------|-------------|
| `/online` | Check if the server is running |
| `/start` | Start the server if it's offline |
| `/stop` | Shut down the server |
| `/restart` | Restart the server with countdown warnings |
| `/skip` | Skip the next scheduled automatic restart |
| `/unskip` | Cancel a previously issued skip |
| `/update` | Update the server via SteamCMD (requires STEAMCMD_PATH) |

### Player Management

| Command | Description |
|---------|-------------|
| `/players` | List currently connected players |
| `/playerlist` | Show all players who have ever joined |
| `/teleport` | Teleport one player to another |
| `/msg` | Broadcast a server-wide message |
| `/playsound` | Trigger a JeevesIntegration sound on all clients |

### Mod Management

| Command | Description |
|---------|-------------|
| `/mod` | Manually check for mod updates |
| `/cleanmods` | Remove unused Workshop mod folders from disk |

### Rank System

| Command | Description | Permission |
|---------|-------------|------------|
| `/myrank` | Show your current rank and chat color | Everyone |
| `/linkme` | Link your Discord to your PZ username | Everyone (60s cooldown) |
| `/unlinkme` | Remove your Discord-to-PZ link | Everyone (60s cooldown) |
| `/setrank` | Manually set a player's rank | Admin |
| `/syncranks` | Rebuild all ranks from Discord roles | Admin |
| `/linkname` | Link another user's Discord to their PZ name | Admin |
| `/unlinkname` | Remove another user's link | Admin |

---

## Features

### Automatic Restarts

The bot restarts the server on a configurable UTC schedule (default: every 4 hours). Countdown warnings are broadcast in-game and to Discord at 10 minutes, 5 minutes, 1 minute, and 10 seconds before restart.

### Mod Update Detection

Every hour, the bot checks all Workshop mods listed in your server's `.ini` file against Steam's API. If any mod has been updated, it triggers a restart countdown. No Steam API key is required — the bot uses Steam's public endpoint.

### Crash Detection

A background heartbeat pings the server via RCON every 60 seconds. If the server stops responding for approximately 7.5 minutes and the process is no longer running, the bot automatically restarts it.

### Chat Relay

Bidirectional chat bridge between a Discord channel and in-game General chat. Requires the JeevesIntegration mod for Discord-to-game messages. Game-to-Discord works by tailing the server's chat log file.

### Rank Synchronization

Discord roles automatically sync to in-game chat name colors via the JeevesIntegration mod. When a player's Discord role changes, their in-game rank updates in real time. Players self-link their accounts with `/linkme`.

### Jeeves Drops & Hordes Integration

The bot communicates with JeevesDrops and JeevesHordes mods via a file-based Lua bridge, enabling Discord notifications for airdrop events and horde nights.

---

## Jeeves Ecosystem

JeevesBot is one part of the Jeeves server management suite. Each component works independently, but together they provide a complete server experience:

- **JeevesBot** — This Discord bot for server administration
- **Jeeve's Integration** — In-game mod for Discord chat relay, rank colors, and sound alerts
- **Jeeve's Hordes** — Dynamic zombie horde night events with survivor progression
- **Jeeve's Drops** — Randomized airdrop events with guarded loot crates
- **Jeeve's Claims** — Property and vehicle ownership system
- **Jeeve's Journals** — Skill recovery journals for preserving progress across deaths

---

## Building from Source

If you've made changes to the bot code and want to compile a standalone executable:

**Windows:**

1. Make sure Python 3.10+ is installed with pip
2. Open a command prompt in the JeevesBot folder
3. Run:
   ```
   build.bat
   ```
4. The compiled bot will be in `dist\Jeeves\`

**Linux:**

1. Make sure Python 3.10+ is installed
2. Run:
   ```bash
   chmod +x build.sh
   ./build.sh
   ```
3. The compiled bot will be in `dist/Jeeves/`

Most Linux server administrators will not need to build — running from source with `./run.sh` is the recommended approach. The build scripts automatically install dependencies, run PyInstaller, and copy the config example into the distribution folder.

---

## Troubleshooting

### "DISCORD_TOKEN is not set"

You forgot to copy `config.env.example` to `config.env` and fill in your bot token. See [Configuration](#configuration).

### Bot is online but slash commands don't appear

Discord can take up to an hour to propagate slash commands after the bot's first run. If commands still don't appear after an hour, try kicking and re-inviting the bot.

### "RCON connection failed"

- Make sure RCON is enabled in your PZ server settings
- Verify the RCON port and password match between `config.env` and your server
- Make sure the PZ server is actually running
- Check that no firewall is blocking the RCON port (if the bot runs on a different machine)

### Bot can't start/stop the server

**Windows:**
- `SERVER_BATCH` must point to the exact `.bat` file you use to start your server
- The bot must run with the same permissions as the server (if the server runs as Administrator, the bot needs to as well)
- Make sure your batch file includes `@cd /d "%~dp0"` as the first line — this ensures the server runs from the correct directory regardless of how it's launched

**Linux:**
- `SERVER_BATCH` must point to your `start-server.sh` script (make sure it's executable: `chmod +x start-server.sh`)
- The bot must run as the same user that owns the server files, or with sufficient permissions to start/kill the server process
- If using systemd for both the bot and server, make sure they run as the same user

### Server crashes when started by the bot but works when started manually

This is usually a timing issue. The bot may start polling RCON before the server has finished initializing Steam and loading mods, which can interfere with startup on heavily modded servers. Try increasing `STARTUP_WAIT` in config.env:

```
STARTUP_WAIT=180
MONITOR_RETRIES=30
```

If the server still crashes immediately (within seconds of launching), check:
- That no other java process is still running from a previous server instance (`tasklist | findstr java` on Windows, `pgrep -f zomboid` on Linux)
- That no other application is using your server's ports (`netstat -ano | findstr :16261` on Windows, `ss -tlnp | grep 16261` on Linux)
- That your antivirus/firewall isn't blocking a Java process spawned by another application

### Mod updates not detecting

- `SERVER_INI_PATH` must point to the correct `.ini` file that contains your `WorkshopItems` list
- Make sure `MODS_FOLDER_PATH` points to the Workshop content folder for PZ (app ID 108600)
- The Steam API endpoint doesn't require a key but it does rate-limit — if you're getting errors, the bot will retry on the next hourly check

### Chat relay not working

- Game → Discord: Make sure `CHAT_LOG_PATH` points to the folder containing your server's chat log files (they look like `2024-01-15_12-00_chat.txt`)
- Discord → Game: Requires the JeevesIntegration mod installed on the server. The bot writes to `Zomboid\Lua\jeeves_chat.lua` which the mod picks up.

### Custom emoji showing as text

If you see `<:emojiname:123456>` instead of the actual emoji, the bot doesn't have access to that emoji. Make sure the emoji is uploaded to the same Discord server the bot is in, and the emoji IDs in config.env are correct.

# JeevesBot — Project Zomboid Server Management

A Discord bot for running a Project Zomboid dedicated server: lifecycle control,
scheduled restarts, mod update detection, chat relay, player tracking, rank
colours, and event control for the Jeeves Ecosystem mods.

Runs on **Windows and Linux**. Talks to the server over RCON, and to the Jeeves
mods through a small file bridge.

> **This release is the build that actually runs the ODYN servers.** Every file
> here was verified byte-identical against the two live boxes before publishing,
> rather than assembled from a development branch. If it is in this repo, it is
> in production.

---

## Table of Contents

1. [Requirements](#requirements)
2. [Installation](#installation)
3. [Creating a Discord Bot](#creating-a-discord-bot)
4. [Configuration](#configuration)
5. [Running the Bot](#running-the-bot)
6. [Commands](#commands)
7. [Features](#features)
8. [The Jeeves Ecosystem](#the-jeeves-ecosystem)
9. [Building from Source](#building-from-source)
10. [Troubleshooting](#troubleshooting)
11. [Contributing](#contributing)

---

## Requirements

- A **Project Zomboid dedicated server** with RCON enabled
- A **Discord server** you can add a bot to
- **Windows 10+** or **Linux** (both are supported and both are in production use)
- **Python 3.10 or later** — only if you run from source. Tested on 3.12 and 3.13
- **SteamCMD** — only for the `/update` command
- **Jeeves Ecosystem mods** — only for chat relay, rank colours, airdrops and hordes

---

## Installation

**Windows** → Option A (easiest) or Option B.
**Linux** → Option C. There is no Linux binary; run it from source, which is how
one of the two reference servers runs it.

### Option A — Pre-built executable (Windows only)

1. Download `Jeeves.zip` from the [Releases page](https://github.com/StewBagger/Jeeves/releases)
2. Extract it somewhere on your server machine
3. Copy `config.env.example` to `config.env` and fill it in (see [Configuration](#configuration))
4. Run `Jeeves.exe`

The executable is **not** committed to this repository — it is published as a
release attachment so it can never drift silently behind the source.

### Option B — From source (Windows)

```bash
git clone https://github.com/StewBagger/Jeeves.git
cd Jeeves
pip install -r requirements.txt
copy config.env.example config.env
```

Edit `config.env`, then run `run_bot.bat` (or `python Jeeves.py`).

### Option C — From source (Linux)

```bash
git clone https://github.com/StewBagger/Jeeves.git
cd Jeeves
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.env.example config.env
```

Edit `config.env`, then run `.venv/bin/python Jeeves.py`.

---

## Creating a Discord Bot

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications) and click **New Application**.
2. Open **Bot** in the sidebar, click **Reset Token**, and copy the token — you only see it once.
3. Under **Privileged Gateway Intents**, enable all three: **Presence**, **Server Members**, and **Message Content**.
4. Under **OAuth2 → URL Generator**, tick the `bot` and `applications.commands` scopes, then the permissions **Send Messages**, **Embed Links**, **Read Message History** and **Use Slash Commands**.
5. Open the generated URL, pick your server, and authorise.
6. Enable **Developer Mode** (Discord Settings → Advanced), then right-click your server and channels to **Copy ID** for the values below.

---

## Configuration

Copy `config.env.example` to `config.env` and edit it. Lines starting with `#`
are comments. **Never commit `config.env`** — it holds your bot token and RCON
password, and it is gitignored for that reason.

### Required

| Setting | Description |
|---|---|
| `DISCORD_TOKEN` | Bot token from the Developer Portal |
| `DISCORD_CHANNEL_ID` | Channel where the bot posts status messages |
| `DISCORD_GUILD_ID` | Your Discord server (guild) ID |
| `RCON_HOST` | RCON host — `127.0.0.1` if the bot runs on the server machine |
| `RCON_PORT` | RCON port (default `27015`) |
| `RCON_PASSWORD` | RCON password, matching your server config |
| `SERVER_BATCH` | Full path to the server start script (`StartServer64.bat` / `start-server.sh`) |
| `SERVER_INI_PATH` | Full path to your server's `.ini` |
| `MODS_FOLDER_PATH` | Workshop content folder for app `108600` |

### Optional

| Setting | Default | Description |
|---|---|---|
| `UPDATE_LOG_PATH` | next to the bot | Where mod update timestamps are stored |
| `SERVER_PROCESS_NAME` | `java.exe` / `java` | Process name for crash detection — **the Linux default is usually wrong, see below** |
| `DEFAULT_ROLE` | `Admin` | Discord role name required for admin commands |
| `STEAMCMD_PATH` | — | Path to SteamCMD, for `/update` and `/modadd` |
| `STEAM_API_KEY` | — | Only needed for **unlisted** Workshop items — see below |
| `CHAT_RELAY_CHANNEL_ID` | — | Channel for the bidirectional chat relay |
| `CHAT_LOG_PATH` | — | Your PZ server's `Logs` folder (chat relay) |
| `USER_LOG_PATH` | — | Your PZ server's `Logs` folder (player tracking) |
| `STATUS_CHANNEL_ID` | — | Channel for the auto-updating status dashboard |
| `HORDE_LEADERBOARD_CHANNEL_ID` | — | Channel for the auto-updating horde leaderboard |
| `RANK_1` … `RANK_6` | Fuel, Spark, Cinder, Flame, Blaze, Inferno | Discord role names mapped to in-game chat colours |
| `EMOJI_*` | Unicode fallbacks | Ten optional custom emoji overrides |

### Startup timing

| Setting | Default | Description |
|---|---|---|
| `STARTUP_WAIT` | `120` | Seconds to wait after launching before polling RCON |
| `CHECK_INTERVAL` | `30` | Seconds between RCON retries |
| `MONITOR_RETRIES` | `20` | Retries before giving up |

Maximum wait is `STARTUP_WAIT + (CHECK_INTERVAL × MONITOR_RETRIES)`. For a
heavily modded server with a large heap, try `STARTUP_WAIT=180` and
`MONITOR_RETRIES=30`.

### `SERVER_PROCESS_NAME` on Linux

Crash detection looks for this name in the process table. **On Linux the default
`java` is usually wrong.** The Build 42 dedicated server runs as
`ProjectZomboid64`, not as a visible `java` process, so the default finds nothing
and the bot treats a perfectly healthy server as gone. Check what yours is
actually called:

```bash
ps aux | grep -i zomboid
```

and set `SERVER_PROCESS_NAME` to something distinctive from that line —
`ProjectZomboid64` on a stock B42 install.

Two caveats worth knowing. On Linux the match is a **substring search over the
whole process table**, not a filtered lookup, so pick a string that cannot match
something unrelated. And on a **managed or containerised host** the game server
may live in a namespace the bot cannot see at all, in which case no value works —
install Jeeve's Integration and let the bridge answer instead, as described under
[Crash detection](#crash-detection).

### `STEAM_API_KEY` and unlisted mods

The update checker uses Steam's keyless endpoint first. That endpoint **cannot
see an unlisted Workshop item at all** — Steam answers result `9` with no data.
The local `appworkshop` manifest that covers the gap is only rewritten when the
game server boots, which is the same event that installs the update. So without
a key, **an unlisted mod can never be announced before the restart that already
applied it.**

If you host unlisted items, get a key at
[steamcommunity.com/dev/apikey](https://steamcommunity.com/dev/apikey) and set
`STEAM_API_KEY`. Public items work fine without one.

---

## Running the Bot

### Windows

`run_bot.bat` launches the bot from its own folder. It prefers the `py`
launcher deliberately: a bare `python` on Windows can resolve to the Microsoft
Store stub, whose MSIX sandbox silently hangs the subprocess spawns the bot
needs to start the game server — it does not error, it simply never returns.
Set `PYTHON` to pin a specific interpreter.

For unattended running, register it as a scheduled task or use
[NSSM](https://nssm.cc/) to run it as a service. The script has no `pause` on
purpose — under a scheduled task that would block forever on a keypress.

### Linux (systemd)

```ini
[Unit]
Description=Jeeves Discord bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pzserver
WorkingDirectory=/opt/jeeves
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/jeeves/.venv/bin/python /opt/jeeves/Jeeves.py
Restart=on-failure
RestartSec=15
KillMode=process

[Install]
WantedBy=multi-user.target
```

`KillMode=process` is load-bearing. The bot owns the game server as a child
process, and the default control-group kill would take the server down with
every bot restart, dropping your players for a reason that has nothing to do
with them.

### First run

Slash commands can take up to an hour to appear the first time. That is a
Discord limitation, not a bug.

---

## Commands

43 slash commands. Unless noted, they require the Discord role named by
`DEFAULT_ROLE`.

### Server management

| Command | Description |
|---|---|
| `/online` | Check whether the server is running |
| `/start` | Start the server if it is offline |
| `/stop` | Shut the server down |
| `/restart` | Restart now, or `/restart minutes:10` for a countdown |
| `/skip` | Skip the next scheduled automatic restart |
| `/unskip` | Cancel a previously issued `/skip` |
| `/postpone` | Postpone a mod-update restart by 10 minutes |
| `/update` | Update the server via SteamCMD |
| `/hello` | Health check — the bot replies if it is alive |

### Players

| Command | Description |
|---|---|
| `/players` | List currently connected players |
| `/playerlist` | Show everyone who has ever joined |
| `/teleport` | Teleport one player to another |
| `/setaccesslevel` | Set a player's server access level |
| `/msg` | Broadcast a server-wide message |
| `/playsound` | Trigger a Jeeve's Integration sound on all clients |

### Mods

| Command | Description |
|---|---|
| `/mod` | Check whether the server's mods are current — **see the warning below** |
| `/cleanmods` | Remove unused Workshop mod folders from disk |
| `/modadd` | Add a Workshop item: download, sort, update the INI |
| `/modremove` | Remove a mod from the config and re-sort |
| `/modreorder` | Re-sort the entire mod list in the INI |
| `/modlist` | Show all mods and Workshop items in the config |
| `/modorder` | Validate the current load order |
| `/modsort` | Show the recommended order against the current one |
| `/modinfo` | Detailed load-order info for one mod |

> ⚠️ **`/mod` is not role-gated.** Any member of your Discord can run it, and if
> Steam reports a pending mod update it starts a real restart countdown. This is
> how the bot has always behaved and it is left unchanged here, but you should
> know about it. To restrict it without touching code, use Discord's own
> **Server Settings → Integrations → JeevesBot** permissions to limit the command
> by role or channel.

### Ranks

| Command | Description | Who |
|---|---|---|
| `/myrank` | Show your rank and chat colour | Everyone |
| `/linkme` | Link your Discord to your PZ username | Everyone |
| `/unlinkme` | Remove your own link | Everyone |
| `/setrank` | Set a player's in-game rank | Admin |
| `/syncranks` | Rebuild all ranks from Discord roles and push | Admin |
| `/linkname` | Link another user to their PZ name | Admin |
| `/unlinkname` | Remove another user's link | Admin |

### Horde events — requires [Jeeve's Hordes](https://steamcommunity.com/sharedfiles/filedetails/?id=3672042113)

| Command | Description |
|---|---|
| `/horde` | Trigger a casual horde (does not affect horde-night progression) |
| `/hordeoff` | Stop an active horde event |
| `/hordestatus` | Show current horde state |
| `/hordenight` | Simulate a scheduled horde night — moodle warning, then horde at nightfall |
| `/hordereset` | Reset progression: event count and all survivor multipliers |
| `/hordeclear` | Clear player horde data — buffs, immunity, cooldowns, streaks |
| `/hordechange` | Move the next scheduled horde to a specific world day |
| `/playerreset` | Reset one player's survivor multiplier and survival count |

### Airdrops — requires [Jeeve's Drops](https://steamcommunity.com/sharedfiles/filedetails/?id=3672451284)

| Command | Description |
|---|---|
| `/airdrop` | Trigger an airdrop on a random or specific player |
| `/airdropstatus` | Show current airdrop status |
| `/supplyevent` | Force a supply drop at a random map location |
| `/supplyeventstatus` | Show current supply event status |

---

## Features

### Automatic restarts
Restarts on a configurable UTC schedule, with countdown warnings broadcast
in-game and to Discord at 10 minutes, 5 minutes, 1 minute and 10 seconds.
`/skip` suppresses the next one.

### Mod update detection
Hourly checks of every Workshop mod in your server's `.ini`. When one updates,
the bot starts a restart countdown that `/postpone` can delay. Unlisted items
need `STEAM_API_KEY` — see [above](#steam_api_key-and-unlisted-mods).

### Player counts and liveness

Where Jeeve's Integration is installed, the bot takes the player roster from the
mod's status file rather than from RCON. The mod builds it from
`getOnlinePlayers()` — the engine's own list — and writes it every 30 seconds.
RCON's `players` is the same fact printed as English prose that then has to be
re-parsed, and a build that rewords that output returns zero players while
reporting no error at all. A wrong zero is not harmless: it is what lets a
restart proceed without warning anyone in the world.

Servers without the mod, or with the bridge switched off, fall back to RCON and
behave exactly as before.

### Crash detection

A heartbeat every 60 seconds and a monitor every 90. The monitor asks the mod
bridge first: **a fresh status file proves the game loop is running**, because
only `OnTick` writes it, whereas an RCON reply proves only that a socket handler
answered. Those two come apart exactly when it matters — a server thrashing hard
enough to stop ticking can still complete an RCON handshake, and a healthy
server on a managed host can be invisible in the bot's process table.

If the bridge is stale it falls back to the heartbeat, then to the process
table, and only then restarts. If the world is demonstrably running but RCON has
been silent for about 7.5 minutes, the bot **says so and restarts nothing** —
broadcasts and `/players` will be broken until RCON recovers, but that is not a
reason to drop everyone in the world.

> If you run on a managed host (AMP, Pterodactyl, or any container setup) where
> the bot cannot see the game server in its own process table, installing
> Jeeve's Integration is strongly recommended. Without it the bot's only
> evidence is RCON plus a process check that cannot succeed, which is the
> configuration most likely to restart a server that was never down.

### Chat relay
Bidirectional bridge between a Discord channel and in-game General chat.
Game-to-Discord tails the server chat log. Discord-to-game needs Jeeve's
Integration.

**Non-Latin scripts work.** Text going into the game is escaped to pure ASCII
byte escapes first, because the game's Lua engine reads bridge files back
without specifying a charset and uses the JVM platform default — which mangles
multi-byte characters on any host whose default is not UTF-8. Escaping is
charset-invariant, so Cyrillic, Greek, CJK and emoji all survive the trip.

### Rank synchronisation
Discord roles drive in-game chat name colours through Jeeve's Integration.
Players self-link with `/linkme`; role changes propagate in real time.

### Status dashboard and leaderboard
Two optional auto-updating embeds, each in its own channel: a server dashboard
(status, players, next restart, in-game time, weather, next horde night) and a
horde survivor leaderboard. Both survive brief RCON outages by holding
last-known-good data rather than blanking.

### The file bridge
The bot talks to the Jeeves mods through files in the server's `Lua` folder —
`jeeves_commands.txt`, `jeeves_chat.txt`, `jeeves_horde_status.txt`,
`jeeves_drops_status.txt`.

> **These are `.txt`, not `.lua`.** Build 42.20 restricted which extensions
> `getFileWriter` will accept, so both sides moved. This bot requires the
> current Workshop versions of the Jeeves mods; older mod versions still looking
> for `.lua` will not see anything the bot writes.

---

## The Jeeves Ecosystem

JeevesBot is one part of a suite. Each mod works alone; together they share the
bridge and the rank system.

**Directly integrated with this bot:**

- [Jeeve's Integration](https://steamcommunity.com/sharedfiles/filedetails/?id=3660924327) — chat relay, rank colours, sound alerts. *Required for Discord-to-game features.*
- [Jeeve's Hordes](https://steamcommunity.com/sharedfiles/filedetails/?id=3672042113) — horde night events and survivor progression
- [Jeeve's Drops](https://steamcommunity.com/sharedfiles/filedetails/?id=3672451284) — airdrops and guarded supply crates

**The rest of the suite:**

[Build](https://steamcommunity.com/sharedfiles/filedetails/?id=3705530591) ·
[Claims](https://steamcommunity.com/sharedfiles/filedetails/?id=3674013419) ·
[Journals](https://steamcommunity.com/sharedfiles/filedetails/?id=3674452315) ·
[PC](https://steamcommunity.com/sharedfiles/filedetails/?id=3693550188) ·
[Patches](https://steamcommunity.com/sharedfiles/filedetails/?id=3684025083) ·
[QoL](https://steamcommunity.com/sharedfiles/filedetails/?id=3687487317) ·
[Solar Arrays](https://steamcommunity.com/sharedfiles/filedetails/?id=3737412008) ·
[Zones](https://steamcommunity.com/sharedfiles/filedetails/?id=3739941052) ·
[Everything Breaks Down](https://steamcommunity.com/sharedfiles/filedetails/?id=3749242062)

---

## Building from Source

```bash
pip install -r requirements.txt
pyinstaller Jeeves.spec
```

Output lands in `dist/Jeeves/`. `build.bat` (Windows) and `build.sh` (Linux)
wrap the same call.

> **If you add, rename or remove a cog, update `Jeeves.spec` as well.** Cogs load
> via `load_extension()` on a string, which PyInstaller cannot see, so every cog
> must also be listed in `hidden_imports`. This has bitten before: a cog rename
> left the spec pointing at the old name, and every executable built afterwards
> silently shipped without horde nights or the leaderboard — the bot started
> normally and two features simply did not exist.

---

## Troubleshooting

**`DISCORD_TOKEN is not set`** — `config.env` is missing or not beside the bot. Copy `config.env.example` and fill it in.

**Slash commands don't appear** — Wait up to an hour on first run, confirm `DISCORD_GUILD_ID` is right, and check the bot was invited with the `applications.commands` scope.

**RCON connection failed** — Check `RCON_PASSWORD` and `RCON_PORT` match your server `.ini`, that RCON is enabled, and that the server has finished loading. A heavily modded server can take several minutes before RCON answers.

**The bot can't start the server** — Verify `SERVER_BATCH` is the full path to a start script that works when you run it by hand. On Windows, make sure a bare `python` is not resolving to the Microsoft Store stub (see [Running the Bot](#running-the-bot)).

**The server crashes when the bot starts it but works manually** — Usually memory. The bot's child process inherits its environment; check the heap settings in your start script.

**Mod updates aren't detected** — Confirm `MODS_FOLDER_PATH` points at `.../steamapps/workshop/content/108600` and that the mods are listed in your server `.ini`. For unlisted items you need `STEAM_API_KEY`.

**Chat relay doesn't work** — Game-to-Discord needs `CHAT_LOG_PATH` and `CHAT_RELAY_CHANNEL_ID`. Discord-to-game additionally needs Jeeve's Integration installed and current.

**Non-Latin characters arrive corrupted in game** — You are running a mod version older than the fix. Update Jeeve's Integration and this bot together.

**Custom emoji show as text** — Use the full `<:name:id>` form, and make sure the bot is in the server that owns the emoji.

### Linux specifically

**The bot restarts a server that is plainly running** — `SERVER_PROCESS_NAME` is almost certainly still `java`. See [`SERVER_PROCESS_NAME` on Linux](#server_process_name-on-linux).

**Bridge commands are written but the game never acts on them** — The bot and the game server must run as the **same user**, or at least the bot must be able to write into the server's `Zomboid/Lua/` directory. Check ownership there; a bot running as `root` against a server running as `pzserver` writes files the game cannot consume.

**The bot dies on startup with a `UnicodeEncodeError`** — Its output includes emoji, and a service with no locale set can leave Python on an ASCII codec. Add `Environment=PYTHONIOENCODING=utf-8` to the unit, or set a UTF-8 `LANG`.

**Restarting the bot service takes the game server down with it** — You are missing `KillMode=process`. See [Running the Bot](#linux-systemd).

**`systemctl status` shows the bot running but there are no logs** — With `StandardOutput=append:` the output goes to that file, not the journal. Without it, `journalctl -u <unit>` has everything. Pick one and know which.

**Slash commands work but nothing appears in game** — Confirm Jeeve's Integration is installed *server-side* and that `SERVER_INI_PATH` is right: the bot derives the bridge directory from it as `<ini's parent>/../Lua`.

---

## Contributing

Issues and pull requests are welcome.

One thing worth knowing before you touch `server_update.py`: the SteamCMD branch
is `public`, and that is deliberate. B42 was promoted to stable and Steam
**retired** the `unstable` branch for app `380870` entirely. Pointing at it fed
SteamCMD a branch that does not exist, which pinned the install to an orphaned
build and left a server running 42.19 for five hours while every update reported
success. The branch is also passed on every invocation on purpose — omitting
`-beta` inherits whatever the install's own manifest already records, so a stale
pin survives silently. Check Steam's real branch list before changing it.

---

## License

[MIT](LICENSE) © 2026 StewBagger

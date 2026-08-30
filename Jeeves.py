"""
Project Zomboid Discord Bot
Manages a PZ dedicated server via RCON with mod update checking and auto-restart.
"""

import os
import sys
import json
import asyncio
import socket
import subprocess
import datetime
import shutil
import time
from pathlib import Path
from typing import Optional, List, Dict

try:
    from dotenv import load_dotenv
    # When compiled with PyInstaller, __file__ is inside _internal/.
    # Check the exe's directory first, then fall back to the script directory.
    _script_dir = os.path.dirname(os.path.abspath(__file__))
    _exe_dir = os.path.dirname(os.path.abspath(sys.executable)) if getattr(sys, 'frozen', False) else _script_dir
    _config_path = None
    for _candidate in (
        os.path.join(_exe_dir, 'config.env'),
        os.path.join(_script_dir, 'config.env'),
    ):
        if os.path.isfile(_candidate):
            _config_path = _candidate
            break
    if _config_path:
        load_dotenv(_config_path)
        print(f"Loaded {_config_path}")
    else:
        print(f"WARNING: config.env not found (searched {_exe_dir} and {_script_dir})")
except ImportError:
    print("WARNING: python-dotenv not installed, using system env vars only.")
except Exception as e:
    print(f"WARNING: Could not load config.env: {e}")

import discord
from discord import app_commands
from discord.ext import commands

try:
    import rcon.source
    from rcon.source import Client
except ImportError:
    sys.exit("ERROR: rcon package not installed. Install with: pip install rcon")

try:
    import httpx
except ImportError:
    sys.exit("ERROR: httpx package not installed. Install with: pip install httpx")

import lua_bridge


# =============================================================================
# CONFIGURATION
# =============================================================================

def _env(key: str, default: str = '') -> str:
    return os.getenv(key, default)

def _env_int(key: str, default: int = 0) -> int:
    return int(os.getenv(key, str(default)))


class Config:
    """Server and bot configuration settings."""

    def __init__(self):
        self.RCON_HOST       = _env('RCON_HOST', '127.0.0.1')
        self.RCON_PORT       = _env_int('RCON_PORT', 27015)
        self.RCON_PASSWORD   = _env('RCON_PASSWORD')
        self.CHANNEL_ID      = _env_int('DISCORD_CHANNEL_ID')
        self.GUILD_ID        = _env_int('DISCORD_GUILD_ID')
        self.TOKEN           = _env('DISCORD_TOKEN')
        self.SERVER_BATCH    = _env('SERVER_BATCH')
        self.SERVER_INI_PATH = _env('SERVER_INI_PATH')
        self.UPDATE_LOG_PATH = _env('UPDATE_LOG_PATH')
        self.MODS_FOLDER_PATH = _env('MODS_FOLDER_PATH')
        self.SERVER_PROCESS_NAME = _env('SERVER_PROCESS_NAME', 'java.exe' if sys.platform == 'win32' else 'java')

        # Timing (configurable — heavy/modded servers may need higher values)
        self.STARTUP_WAIT = _env_int('STARTUP_WAIT', 120)
        self.CHECK_INTERVAL = _env_int('CHECK_INTERVAL', 30)
        self.MONITOR_RETRIES = _env_int('MONITOR_RETRIES', 20)

        # Roles
        self.DEFAULT_ROLE = _env('DEFAULT_ROLE', 'Admin')
        self.RANKS = {i: _env(f'RANK_{i}', f'Rank {i}') for i in range(1, 7)}

        # Steam Workshop API (no key required)
        self.STEAM_API_URL = "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/"

        # Keyed Workshop API. The keyless endpoint above CANNOT see an unlisted
        # item -- it answers result 9 with no data at all -- and the local
        # manifest that used to cover that gap is only rewritten when the game
        # server boots, which is the same event that installs the update. So an
        # unlisted mod could never be ANNOUNCED before the restart that had
        # already applied it. This endpoint answers for unlisted items and is a
        # live query, so unlisted mods are checked on the same cadence as public
        # ones. Optional: with no key set, behaviour is exactly as before.
        self.STEAM_API_KEY = _env('STEAM_API_KEY', '')
        self.STEAM_PUBLISHEDFILE_URL = "https://api.steampowered.com/IPublishedFileService/GetDetails/v1/"

        print(f"Config loaded: RCON={self.RCON_HOST}:{self.RCON_PORT} Guild={self.GUILD_ID} Channel={self.CHANNEL_ID}")

    def validate(self) -> List[str]:
        errors = []
        if not self.TOKEN or self.TOKEN == 'YOUR_DISCORD_TOKEN':
            errors.append("DISCORD_TOKEN is not set")
        if not self.CHANNEL_ID:
            errors.append("DISCORD_CHANNEL_ID is not set")
        if not self.GUILD_ID:
            errors.append("DISCORD_GUILD_ID is not set")
        if not self.RCON_PASSWORD:
            errors.append("RCON_PASSWORD is not set")
        if not self.SERVER_BATCH:
            errors.append("SERVER_BATCH is not set (path to StartServer64.bat or start-server.sh)")
        if not self.SERVER_INI_PATH:
            errors.append("SERVER_INI_PATH is not set (path to your server .ini)")
        return errors


# =============================================================================
# CUSTOM EMOJIS
# Configure custom Discord emoji IDs in config.env, or leave blank for defaults.
# Format: <:name:ID> for static, <a:name:ID> for animated
# =============================================================================

class Emojis:
    HAPPY          = _env('EMOJI_HAPPY')          or "\U0001f7e2"   # 🟢
    DIZZY          = _env('EMOJI_DIZZY')          or "\U0001f635"   # 😵
    PANIC          = _env('EMOJI_PANIC')          or "\U0001f534"   # 🔴
    ANGRY          = _env('EMOJI_ANGRY')          or "\u26d4"       # ⛔
    JEEVES         = _env('EMOJI_JEEVES')         or "\U0001f9d1"   # 🧑
    SPIFFO_POP     = _env('EMOJI_SPIFFO_POP')     or "\U0001f389"   # 🎉
    SPIFFO_WAVE    = _env('EMOJI_SPIFFO_WAVE')    or "\U0001f44b"   # 👋
    SPIFFO_EDUCATE = _env('EMOJI_SPIFFO_EDUCATE') or "\u2757"       # ❗
    SPIFFO_KATANA  = _env('EMOJI_SPIFFO_KATANA')  or "\u26a0\ufe0f" # ⚠️
    SPIFFO_STOP    = _env('EMOJI_SPIFFO_STOP')    or "\U0001f6d1"   # 🛑

# Debug: show what emoji values were loaded
print(f"Emoji check: HAPPY={Emojis.HAPPY!r}  JEEVES={Emojis.JEEVES!r}")


# =============================================================================
# SERVER STATE
# =============================================================================

class ServerState:
    def __init__(self):
        self.updated_mods: Optional[List[str]] = None
        self.first_start = True
        self.auto_restart_pending = False
        self.players_online = False
        self.player_count = 0
        self.player_names: set = set()
        self.restart_task: Optional[asyncio.Task] = None
        self.is_restarting = False
        self.is_starting = False
        self.server_ready = False
        self.skip_next_restart = False
        self.last_rcon_ok = False
        self.mod_update_running = False

        # WHEN the server was last shown to be alive, and by what. Until
        # 2026-08-30 aliveness was the bare `last_rcon_ok` boolean above and
        # nothing else, which could not answer "alive as of when". That mattered
        # in two ways. A boolean cannot distinguish a server that has been quiet
        # for eight minutes from one whose heartbeat task died and left the flag
        # frozen at its last value -- and a frozen False restarts a healthy
        # server every 7.5 minutes, for ever. It also could not record that the
        # evidence came from the mod bridge rather than from RCON, which is the
        # stronger of the two: see poll_players.
        self.last_alive_ts: float = 0.0
        self.alive_source: str = "none"

    def mark_alive(self, source: str) -> None:
        """Record that the server was observably alive just now.

        Deliberately does NOT touch last_rcon_ok. That flag means one specific
        thing -- "RCON answered" -- and the bridge answering is not evidence
        about RCON. Letting this method set it would have made the restart
        countdown log print `rcon=ok` on a run where RCON was never contacted,
        which is the same species of quiet inaccuracy as the player parse.
        """
        self.last_alive_ts = time.time()
        self.alive_source = source

    def seconds_since_alive(self) -> Optional[float]:
        """Seconds since the last positive sign of life, or None if never seen."""
        if self.last_alive_ts <= 0:
            return None
        return max(0.0, time.time() - self.last_alive_ts)


# =============================================================================
# RCON HELPER
# =============================================================================

class RCONHelper:
    def __init__(self, config: Config):
        self.host = config.RCON_HOST
        self.port = config.RCON_PORT
        self.password = config.RCON_PASSWORD

    async def send_command(self, command: str, timeout: int = 10) -> Optional[str]:
        try:
            return await asyncio.wait_for(
                rcon.source.rcon(command, host=self.host, port=self.port, passwd=self.password),
                timeout=timeout
            )
        except asyncio.TimeoutError:
            print(f"RCON timeout ({timeout}s): {command}")
            return None
        except (socket.timeout, ConnectionRefusedError, OSError) as e:
            print(f"RCON error: {e}")
            return None

    async def broadcast(self, message: str) -> None:
        """Send a red-alert servermsg + trigger alert sound via lua bridge.
        Used by /msg, restart notifications, and mod update warnings.
        The servermsg handles display (red alert text). The lua bridge
        command tells the client mod to analyze and play the alert sound
        only (soundOnly flag prevents duplicate chat panel display).
        """
        await self.send_command(f'servermsg "{message}"')
        await lua_bridge.broadcast(message, sound_only=True)

    async def save_and_quit(self) -> None:
        await self.send_command('save')
        await asyncio.sleep(8)
        await self.send_command('quit')
        await asyncio.sleep(8)

    def is_server_online(self, timeout: int = 5) -> bool:
        try:
            with Client(self.host, self.port, passwd=self.password, timeout=timeout) as client:
                client.run('players')
                return True
        except (socket.timeout, ConnectionRefusedError, OSError):
            return False

    @staticmethod
    def parse_players(response: str) -> tuple[set, int]:
        """Parse player names and count from RCON 'players' response.
        Returns (names_set, count).
        """
        names = set()
        count = 0
        if not response:
            return names, count

        for line in response.splitlines():
            stripped = line.strip()
            if stripped.startswith("-"):
                name = stripped.lstrip("-").strip()
                if name:
                    names.add(name)
            elif "Players connected (" in stripped:
                try:
                    count = int(stripped[stripped.index("(") + 1:stripped.index(")")])
                except (ValueError, IndexError):
                    pass

        return names, count


# =============================================================================
# MOD CHECKER
# =============================================================================

class ModChecker:
    def __init__(self, config: Config):
        self.config = config

    def get_workshop_ids(self) -> List[str]:
        try:
            with open(self.config.SERVER_INI_PATH, 'r') as f:
                for line in f:
                    if line.strip().startswith("WorkshopItems="):
                        return [i.strip() for i in line.split('=', 1)[1].strip().split(';') if i.strip()]
        except Exception as e:
            print(f"Error reading .ini: {e}")
        return []

    async def _fetch_workshop_state(self, workshop_ids: List[str]) -> Dict:
        """Current update times per Workshop id, from Steam's API where it can
        answer and from Steam's own local manifest where it cannot.

        The API used here is keyless, and it CANNOT SEE UNLISTED ITEMS: it
        answers result 9 with no time_updated and no title. This method used to
        ignore result entirely, take the entry anyway, and substitute defaults --
        time 0 and "Unknown Mod" -- so an unlisted mod compared 0 > 0, never
        reported an update, and then had that 0 written back over its baseline.
        Silent, permanent, and worst for exactly the mods that change most.

        So a non-success entry is now DISCARDED rather than defaulted. What the
        keyless endpoint refuses is then resolved in two steps, in this order:

          1. IPublishedFileService/GetDetails, which needs an API key but CAN
             see an unlisted item, and is a LIVE query -- so an unlisted mod is
             detected on the same cadence as a public one.
          2. steamapps/workshop/appworkshop_108600.acf, the local manifest.
             Correct but LAGGING: latest_timeupdated only advances when the game
             server boots, which is also when the update gets installed, so on
             its own this can never announce an update before it has already
             been applied. It is the no-key fallback, not the primary source.
        """
        if not workshop_ids:
            return {}

        data = {'itemcount': len(workshop_ids), 'format': 'json'}
        for i, item_id in enumerate(workshop_ids):
            data[f'publishedfileids[{i}]'] = item_id

        state: Dict = {}
        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                resp = await client.post(self.config.STEAM_API_URL, data=data)
                resp.raise_for_status()
                for item in resp.json().get('response', {}).get('publishedfiledetails', []):
                    item_id = str(item.get('publishedfileid'))
                    if item.get('result') != 1:
                        # Steam said it cannot see this. Say so and move on --
                        # inventing a timestamp here is what hid the bug before.
                        print(f"  Steam API cannot see {item_id} "
                              f"(result={item.get('result')}); trying local manifest.")
                        continue
                    state[item_id] = {
                        'time': item.get('time_updated', 0),
                        'title': item.get('title', 'Unknown Mod'),
                        'source': 'api',
                    }
            except Exception as e:
                print(f"Steam API error: {e}")

        missing = [i for i in workshop_ids if i not in state]
        if missing and self.config.STEAM_API_KEY:
            state.update(await self._fetch_state_from_keyed_api(missing))

        missing = [i for i in workshop_ids if i not in state]
        if missing:
            state.update(self._fetch_state_from_manifest(missing))

        still_missing = [i for i in workshop_ids if i not in state]
        if still_missing:
            # Named, not swallowed: an id nothing can resolve is invisible to
            # update detection, and that has to be visible to a human.
            print(f"  !! no update source for {len(still_missing)} id(s): "
                  f"{', '.join(still_missing)}")
        return state

    async def _fetch_state_from_keyed_api(self, workshop_ids: List[str]) -> Dict:
        """Live lookup for the ids the keyless endpoint refuses to answer for.

        This is the one that makes an unlisted mod behave like a public one.
        ISteamRemoteStorage answers result 9 for an unlisted item; this service
        answers properly, and because it is a live query it sees an upload the
        moment it happens -- unlike the local manifest, whose latest_timeupdated
        only advances when the game server boots and fetches the update anyway.

        This service is GET-only -- a POST is answered with HTTP 405 -- so the
        key unavoidably travels in the query string. That makes the logging
        discipline here load-bearing rather than stylistic: NOTHING in this
        method may print an exception, a URL, or a response body, because an
        httpx error stringifies the request URL and would put the key straight
        into the bot log and from there into Discord. Status codes, exception
        CLASS names, and ids only. Keep it that way.
        """
        params = {'key': self.config.STEAM_API_KEY}
        for i, item_id in enumerate(workshop_ids):
            params[f'publishedfileids[{i}]'] = item_id
        params['includetags'] = 'false'
        params['includeadditionalpreviews'] = 'false'
        params['includechildren'] = 'false'

        out: Dict = {}
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(self.config.STEAM_PUBLISHEDFILE_URL, params=params)
                if resp.status_code != 200:
                    # 401/403 means the key is missing, wrong, or not entitled to
                    # see these items. Say which, without echoing anything sent.
                    print(f"  keyed Workshop API refused the lookup "
                          f"(HTTP {resp.status_code}); falling back to the local manifest.")
                    return {}
                details = resp.json().get('response', {}).get('publishedfiledetails', [])
        except Exception as e:
            print(f"  keyed Workshop API error ({type(e).__name__}); "
                  f"falling back to the local manifest.")
            return {}

        for item in details:
            item_id = str(item.get('publishedfileid'))
            if item.get('result') != 1:
                continue
            when = item.get('time_updated') or item.get('time_created') or 0
            try:
                when = int(when)
            except (TypeError, ValueError):
                when = 0
            if when <= 0:
                # No usable timestamp is not a baseline. Leave it for the manifest
                # rather than recording a zero -- recording zeros is the original bug.
                continue
            title = item.get('title')
            if not title:
                try:
                    import workshop_acf
                    title = workshop_acf.read_item_title(self.config.MODS_FOLDER_PATH, item_id)
                except Exception:
                    title = None
            out[item_id] = {
                'time': when,
                'title': title or f"Workshop item {item_id}",
                'source': 'api-keyed',
            }

        if out:
            print(f"  keyed Workshop API resolved {len(out)} unlisted id(s): "
                  f"{', '.join(sorted(out))}")
        return out

    def _fetch_state_from_manifest(self, workshop_ids: List[str]) -> Dict:
        """Fallback source: Steam's local appworkshop manifest.

        Cross-checked against the API on ids it CAN answer for, and the values
        agree exactly, so this is an equivalent source rather than an
        approximation -- and it is the only source for an unlisted item.
        """
        try:
            import workshop_acf
        except ImportError as e:
            print(f"  manifest fallback unavailable: {e}")
            return {}

        acf = workshop_acf.acf_path_from_mods_folder(self.config.MODS_FOLDER_PATH)
        if not acf:
            print("  manifest fallback: appworkshop manifest not found "
                  "(is MODS_FOLDER_PATH the .../content/108600 dir?)")
            return {}

        times = workshop_acf.read_item_times(acf, workshop_ids)
        out: Dict = {}
        for item_id, when in times.items():
            title = workshop_acf.read_item_title(self.config.MODS_FOLDER_PATH, item_id)
            out[item_id] = {
                'time': when,
                'title': title or f"Workshop item {item_id}",
                'source': 'manifest',
            }
        if out:
            print(f"  manifest supplied {len(out)} id(s) the API could not: "
                  f"{', '.join(sorted(out))}")
        return out

    def _load_state(self) -> Dict:
        if os.path.exists(self.config.UPDATE_LOG_PATH):
            try:
                with open(self.config.UPDATE_LOG_PATH, 'r') as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                pass
        return {}

    def _save_state(self, state: Dict) -> None:
        Path(self.config.UPDATE_LOG_PATH).parent.mkdir(parents=True, exist_ok=True)
        with open(self.config.UPDATE_LOG_PATH, 'w') as f:
            json.dump(state, f, indent=4)

    async def check_for_updates(self) -> List[str]:
        """Compare current Steam timestamps against saved state. Returns updated mod names."""
        print(f"🚀 Checking mods... ({datetime.datetime.now():%H:%M:%S})")

        workshop_ids = self.get_workshop_ids()
        if not workshop_ids:
            print("No Workshop IDs found.")
            return []

        previous = self._load_state()
        current = await self._fetch_workshop_state(workshop_ids)
        if not current:
            print("Failed to fetch mod data from Steam.")
            return []

        # A stored time of 0 is not "very old", it is NO BASELINE. Unlisted mods
        # were previously recorded as 0 because the API could not see them and
        # this code defaulted the missing value; the first run after that is
        # fixed would otherwise see 0 -> a real timestamp, call it an update, and
        # restart a live server for every one of them at once. Treat 0 as
        # unknown, adopt the real value silently, and compare from next time.
        updated = []
        for mod_id, info in current.items():
            was = previous.get(mod_id)
            if not was:
                continue                       # newly tracked: adopt, do not announce
            base = was.get('time', 0)
            if base <= 0:
                print(f"  baseline for {mod_id} was unknown; adopting {info['time']} "
                      f"without reporting an update.")
                continue
            if info['time'] > base:
                updated.append(info['title'])

        self._save_state(current)
        print(f"🚨 Updates: {', '.join(updated)}" if updated else "✅ All mods current.")
        return updated

    async def seed_state(self) -> None:
        """Snapshot current timestamps as baseline (called on server start)."""
        print(f"🌱 Seeding mod state... ({datetime.datetime.now():%H:%M:%S})")
        ids = self.get_workshop_ids()
        if not ids:
            return
        state = await self._fetch_workshop_state(ids)
        if state:
            self._save_state(state)
            print(f"✅ Seeded {len(state)} mod(s).")


# =============================================================================
# CROSS-PLATFORM PROCESS HELPERS
# =============================================================================

_IS_WINDOWS = sys.platform == "win32"

# How stale the mod's world-status file may be before the bot stops believing
# it. JeevesWorldStatus.lua writes every 30 s (WRITE_INTERVAL_MS = 30000), so
# 90 s is three consecutive missed writes. One missed write is a busy tick; three
# is a game loop that has stopped, which is the thing worth reacting to.
#
# Do not raise this much past a restart's own duration. Its other job is to stop
# the bot reading a status file left behind by a server that has since died, and
# a window longer than the gap between shutdown and boot would do exactly that.
WORLD_STATUS_MAX_AGE = 90.0

def _tasklist(filter_str: str, timeout: int = 10) -> str:
    """Run tasklist (Windows) or ps (Linux) with the given filter and return output."""
    if _IS_WINDOWS:
        try:
            return subprocess.check_output(
                f'tasklist /FI "{filter_str}" /NH', shell=True, text=True, timeout=timeout
            )
        except Exception:
            return ""
    else:
        # On Linux, parse the filter string for the process name or PID
        try:
            return subprocess.check_output(
                ['ps', 'aux'], text=True, timeout=timeout
            )
        except Exception:
            return ""

def _taskkill(target: str) -> None:
    """Kill a process. On Windows uses taskkill, on Linux uses kill."""
    if _IS_WINDOWS:
        os.system(f'taskkill /f {target} 2>nul')
    else:
        # Extract PID from target string if present
        import re
        pid_match = re.search(r'/pid\s+(\d+)', target)
        im_match = re.search(r'/im\s+"?([^"]+)"?', target)
        if pid_match:
            pid = int(pid_match.group(1))
            try:
                import signal
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        elif im_match:
            proc_name = im_match.group(1)
            try:
                subprocess.run(['pkill', '-f', proc_name],
                               capture_output=True, timeout=5)
            except Exception:
                pass


# =============================================================================
# DISCORD BOT
# =============================================================================

class PZBot(commands.Bot):
    Emojis = Emojis

    def __init__(self, config: Config):
        super().__init__(command_prefix="!", intents=discord.Intents.all())
        self.config = config
        self.state = ServerState()
        self.rcon = RCONHelper(config)
        self.mod_checker = ModChecker(config)
        self.server_process: Optional[subprocess.Popen] = None
        self._batch_pid: Optional[int] = None

    async def setup_hook(self) -> None:
        guild = discord.Object(id=self.config.GUILD_ID)

        # Initialize the Lua file bridge (must happen before extensions load)
        lua_bridge.init(self)

        for ext in ('auto_restart', 'mod_check_timer', 'player_tracker', 'rank_sync', 'chat_relay', 'horde_events', 'jeeves_drops', 'jeeves_modsorter', 'jeeves_modmanager', 'server_update', 'server_status', 'horde_leaderboard'):
            try:
                await self.load_extension(ext)
                print(f"Loaded {ext}")
            except Exception as e:
                print(f"Failed to load {ext}: {e}")
                import traceback; traceback.print_exc()

        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        print(f"Commands synced to guild {guild.id}")

    def get_notification_channel(self) -> Optional[discord.TextChannel]:
        return self.get_channel(self.config.CHANNEL_ID)

    async def send_notification(self, title: str, colour: discord.Colour = discord.Colour.purple(),
                                description: Optional[str] = None) -> None:
        channel = self.get_notification_channel()
        if channel:
            await channel.send(embed=discord.Embed(title=title, colour=colour, description=description))
        else:
            print(f"Warning: notification channel {self.config.CHANNEL_ID} not found")

    # ---- Server Control ----

    async def start_server(self) -> None:
        self.state.is_starting = True
        try:
            batch_dir = os.path.dirname(os.path.abspath(self.config.SERVER_BATCH))

            # When running as a PyInstaller exe, the bundled runtime sets env
            # vars (_MEIPASS, modified PATH, etc.) that can interfere with the
            # PZ server's Java process. Build a clean environment for the child.
            env = os.environ.copy()
            if getattr(sys, 'frozen', False):
                # Remove PyInstaller-specific variables
                for key in ('_MEIPASS', '_MEIPASS2', '_PYI_SPLASH_IPC'):
                    env.pop(key, None)
                # Restore PATH: remove any temp extraction directories
                # PyInstaller temp dirs look like _MEIxxxxx
                if 'PATH' in env:
                    clean_path = os.pathsep.join(
                        p for p in env['PATH'].split(os.pathsep)
                        if '_MEI' not in p
                    )
                    env['PATH'] = clean_path
                print(f"[ServerControl] Cleaned PyInstaller env vars for server launch")

            popen_kwargs = dict(
                cwd=batch_dir,
                env=env,
            )
            if _IS_WINDOWS:
                popen_kwargs['creationflags'] = subprocess.CREATE_NEW_CONSOLE
                self.server_process = subprocess.Popen(
                    self.config.SERVER_BATCH, **popen_kwargs)
            else:
                # Linux: launch the shell script in a new session so it doesn't
                # die when the bot's terminal closes.
                self.server_process = subprocess.Popen(
                    ['bash', self.config.SERVER_BATCH],
                    start_new_session=True, **popen_kwargs)
            self._batch_pid = self.server_process.pid
            print(f"[ServerControl] Started server (PID: {self._batch_pid}, cwd: {batch_dir})")

            # Early health check — verify the process survives initial startup
            for check in range(3):
                await asyncio.sleep(5)
                if self.server_process.poll() is not None:
                    exit_code = self.server_process.returncode
                    print(f"[ServerControl] Server process died during startup (exit code: {exit_code})")
                    await self.send_notification(
                        f"{Emojis.PANIC} Server failed to start! (exit code: {exit_code})",
                        discord.Colour.red(),
                        description="The server process exited before it could finish loading. "
                                    "Check the server log for errors."
                    )
                    self.state.is_starting = False
                    return
                print(f"[ServerControl] Health check {check + 1}/3 — process alive")

            # Wait for the server to fully load (Steam init, mods, map)
            remaining_wait = max(0, self.config.STARTUP_WAIT - 15)  # subtract the 15s spent on health checks
            print(f"[ServerControl] Waiting {remaining_wait}s for server to finish loading...")
            await asyncio.sleep(remaining_wait)

            await self.mod_checker.seed_state()
            await self.monitor_until_online()
        finally:
            self.state.is_starting = False

    def discover_server_pid(self) -> None:
        """Find PID of an already-running server (bot restart scenario)."""
        self._batch_pid = None
        proc = self.config.SERVER_PROCESS_NAME

        if _IS_WINDOWS:
            try:
                output_csv = subprocess.check_output(
                    f'tasklist /FI "IMAGENAME eq {proc}" /FO CSV /NH',
                    shell=True, text=True, timeout=10
                )
                for line in output_csv.strip().splitlines():
                    if proc in line:
                        parts = line.split(',')
                        if len(parts) >= 2:
                            pid = int(parts[1].strip('"'))
                            print(f"[ServerControl] Discovered server PID: {pid}")
                            self.server_process = type('Process', (), {'pid': pid})()
                            return
            except Exception as e:
                print(f"[ServerControl] Could not discover server PID: {e}")
        else:
            # Linux: use pgrep to find the process
            try:
                output = subprocess.check_output(
                    ['pgrep', '-f', proc], text=True, timeout=10
                ).strip()
                if output:
                    pid = int(output.splitlines()[0])
                    print(f"[ServerControl] Discovered server PID: {pid}")
                    self.server_process = type('Process', (), {'pid': pid})()
                    return
            except Exception as e:
                print(f"[ServerControl] Could not discover server PID: {e}")

    async def stop_server(self) -> None:
        self.state.server_ready = False
        print("[ServerControl] Stopping server...")

        await self.rcon.save_and_quit()

        proc = self.config.SERVER_PROCESS_NAME

        if _IS_WINDOWS:
            _taskkill(f'/im "{proc}"')

            for pid in filter(None, {getattr(self, '_batch_pid', None),
                                      getattr(self.server_process, 'pid', None)}):
                _taskkill(f'/t /pid {pid}')

            batch_name = os.path.basename(self.config.SERVER_BATCH)
            for title in (batch_name, f'Administrator:  {batch_name}', self.config.SERVER_BATCH):
                _taskkill(f'/fi "WINDOWTITLE eq {title}"')
        else:
            # Linux: kill by PID, then by process name as fallback
            import signal
            for pid in filter(None, {getattr(self, '_batch_pid', None),
                                      getattr(self.server_process, 'pid', None)}):
                try:
                    os.kill(pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
            # Fallback: pkill by process name
            try:
                subprocess.run(['pkill', '-f', proc],
                               capture_output=True, timeout=5)
            except Exception:
                pass

        self._batch_pid = None
        self.server_process = None

        await asyncio.sleep(2)
        print("[ServerControl] Stop complete.")

    async def restart_server(self) -> None:
        if self.state.is_restarting:
            print("[ServerControl] Restart already in progress, ignoring duplicate request.")
            return
        # Cancel any pending mod update countdown, but only if WE are not
        # inside that task. Otherwise we'd cancel ourselves mid-restart.
        current_task = asyncio.current_task()
        if self.state.restart_task and self.state.restart_task is not current_task:
            self.cancel_restart_task()
        self.state.server_ready = False
        self.state.is_restarting = True
        print("[ServerControl] Restarting...")
        await self.stop_server()
        await asyncio.sleep(5)
        await self.start_server()
        self.state.is_restarting = False

    # ---- Monitoring ----

    async def monitor_until_online(self, max_retries: int = None) -> bool:
        if max_retries is None:
            max_retries = self.config.MONITOR_RETRIES
        for attempt in range(max_retries):
            # Check process is still alive before trying RCON
            if self.server_process and self.server_process.poll() is not None:
                print(f"[ServerControl] Server process exited during monitoring (exit code: {self.server_process.returncode})")
                await self.send_notification(
                    f"{Emojis.PANIC} Server process died while waiting for it to come online!",
                    discord.Colour.red()
                )
                return False

            if self.rcon.is_server_online():
                await self.send_notification(f"{Emojis.HAPPY} Server is Online!", discord.Colour.green())
                if not self.state.first_start:
                    try:
                        await self.reload_extension('mod_check_timer')
                    except commands.ExtensionNotLoaded:
                        await self.load_extension('mod_check_timer')
                self.state.first_start = False
                self.state.server_ready = True
                print("[ServerControl] server_ready = True")
                return True
            if attempt < max_retries - 1:
                print(f"[ServerControl] RCON check {attempt + 1}/{max_retries} failed, retrying in {self.config.CHECK_INTERVAL}s...")
                await self.send_notification(
                    f"{Emojis.PANIC} Server Offline! Retrying in {self.config.CHECK_INTERVAL}s... ({attempt + 1}/{max_retries})",
                    discord.Colour.red()
                )
                await asyncio.sleep(self.config.CHECK_INTERVAL)

        await self.send_notification(f"{Emojis.ANGRY} Retry limit exceeded!", discord.Colour.red())
        return False

    async def poll_players(self) -> Optional[str]:
        """Update player state, preferring the mod bridge and falling back to RCON.

        WHY THE BRIDGE IS ASKED FIRST
            JeevesWorldStatus.lua builds this roster from ``getOnlinePlayers()``
            -- the engine's own list -- and writes it every 30 seconds. RCON's
            `players` is the same fact rendered as English prose which we then
            re-parse by hunting for a leading "-" and the literal string
            "Players connected (". Any build that rewords that output returns
            zero players and reports no error at all.

            A wrong zero is not inert. ``players_online`` is what decides
            whether a restart broadcasts a countdown to the people in the world
            (see the restart paths and server_update), so a parse that quietly
            fails is a restart that quietly drops players without warning them.

            The bridge is also better evidence of a different kind. RCON
            answering proves a socket handler is alive. A fresh status file
            proves the GAME LOOP ran, because only OnTick writes it. Those come
            apart exactly when it matters -- a server thrashing badly enough to
            stop ticking can still complete an RCON handshake -- and conflating
            them is why this bot has restarted servers that were merely busy.

        RCON REMAINS THE FALLBACK AND IS STILL FULLY SUPPORTED
            A server without Jeeve's Integration, or with the DiscordBridge
            sandbox option off, has no status file. Those keep precisely the old
            behaviour, so this is an upgrade where the mod is present and a
            no-op where it is not.
        """
        status, age = lua_bridge.read_world_status_with_age()

        if (status is not None and age is not None
                and age <= WORLD_STATUS_MAX_AGE
                and status.get('playerCount') is not None):
            try:
                count = int(status['playerCount'])
            except (TypeError, ValueError):
                count = None

            if count is not None:
                # `players` is table.concat(names, ", ") written by the mod, and
                # is the empty string when nobody is on. Names are usernames,
                # the same identifier RCON reports, so downstream comparisons
                # against player_names are unaffected by which source won.
                raw = str(status.get('players', '') or '')
                names = {n.strip() for n in raw.split(',') if n.strip()}

                self.state.player_names = names
                self.state.player_count = count
                self.state.players_online = count > 0
                self.state.mark_alive('bridge')
                return None

        # No usable bridge data — fall back to RCON.
        response = await self.rcon.send_command('players')
        if response is not None:
            self.state.last_rcon_ok = True
            self.state.player_names, self.state.player_count = self.rcon.parse_players(response)
            self.state.players_online = self.state.player_count > 0
            self.state.mark_alive('rcon')
        else:
            self.state.last_rcon_ok = False
        return response

    # ---- Horde Night Restart Guard ----

    # How long any horde deferral waits for the event to clear before giving up
    # and restarting anyway, and how often it re-asks. They live on the bot so
    # the AutoRestart cog reaches them through self.bot rather than importing
    # this module, which is __main__ at run time. Shared so the two defer paths
    # cannot drift apart: until 2026-08-20 only the mod-update path had a cap at
    # all, and a status file stuck mid-event would have stopped every scheduled
    # restart from then on without saying so.
    HORDE_DEFER_TIMEOUT = 3 * 60 * 60   # seconds
    HORDE_DEFER_POLL = 60               # seconds

    # Phases that mean the horde itself is over but its tail is not: the lure
    # window and the boosted airdrop it hands off to. A restart here destroys
    # the drop. They block for a bounded grace period measured from the status
    # file's own timestamp, because nothing rewrites the file to say the tail
    # ended — the next write is the next boot, which may be hours away.
    _HORDE_TAIL_PHASES = ("ended", "dropped", "expired")
    _HORDE_TAIL_GRACE = 20 * 60       # seconds
    # How long a phase="active" file is believed. A horde runs about two
    # in-game hours; a file older than this is one nobody is updating, not a
    # horde that has lasted all day. Without the cap a status file stuck on
    # "active" blocks every future restart and says nothing about why.
    _HORDE_ACTIVE_MAX_AGE = 45 * 60   # seconds

    def _is_horde_blocking_restart(self) -> tuple:
        """Check if a horde event should block a server restart.
        Returns (should_block, reason_string).

        Blocks if:
          - a horde is running, including when the file was last written by the
            /horde status command rather than by the event itself
          - the lure window or its airdrop tail is still settling
          - today is horde night, from the moodle onward

        WHY THERE IS NO DAY ARITHMETIC HERE ANY MORE. This used to compare the
        horde file's nextHordeDay against the world bridge's elapsedDays, with a
        hardcoded `- 1` to reconcile them. Those two counters do not share a
        basis and the offset between them is not a constant: elapsedDays is
        computed from the in-game calendar and rolls at midnight, while
        JH_GetActualDay() floors getWorldAgeDays() and rolls at whatever
        time-of-day the world was created — in-game 07:00 on ODYN. So the offset
        is 1 for seventeen in-game hours a day and 0 for the other seven, and the
        guard was a full day wrong between midnight and 07:00: it deferred
        restarts about fifteen in-game hours before a horde that had not been
        scheduled yet, then went quiet for the twelve in-game hours after the
        moodle when every player can see it is horde night. On 2026-08-20 that
        put a mod-update restart on the server one in-game hour after the horde
        moodle went up.

        eventDay and nextHordeDay are both JH_GetActualDay() values written by
        the same module in the same file, so comparing them to each other needs
        no offset and cannot drift. Their equality is exactly the condition the
        mod itself uses to raise the moodle, which makes the bot's idea of
        "horde night" the same object as the player's. See Jeeves DR-0081.
        """
        horde = lua_bridge.read_horde_status()

        if not horde:
            return False, ""

        phase = horde.get("phase", "")
        written = horde.get("timestamp")
        age = None
        if isinstance(written, (int, float)):
            age = int(time.time()) - int(written)

        # Horde in progress. `phase` alone is not enough: JeevesCommandServer's
        # status handler rewrites this same file with phase="status", which
        # erased "active" and silently disarmed the whole guard. That write
        # carries its own `active` flag, so read it.
        if phase == "active" or horde.get("active") is True:
            if phase == "active" and age is not None and age > self._HORDE_ACTIVE_MAX_AGE:
                print(f"[HordeGuard] Ignoring stale active status ({age}s old) - not blocking")
                return False, ""
            return True, "Horde night is currently active"

        # The lure window and the boosted airdrop run on after the horde ends.
        if horde.get("lurePhase") is True:
            return True, "Horde lure window still running"
        if phase in self._HORDE_TAIL_PHASES:
            if age is None or age <= self._HORDE_TAIL_GRACE:
                return True, f"Horde aftermath still settling (phase {phase})"
            return False, ""

        # Horde night, from the moodle onward.
        if phase == "scheduled":
            event_day = horde.get("eventDay")
            next_day = horde.get("nextHordeDay")
            if event_day is not None and next_day is not None and event_day == next_day:
                return True, f"Horde night tonight (day {next_day}){self._ingame_clock_suffix()}"

        return False, ""

    def _ingame_clock_suffix(self) -> str:
        """In-game clock for the deferral message, or "" if the world bridge is
        not there. Cosmetic only — the decision above never consults it, which
        is the point. It used to, and that is what went wrong."""
        world = lua_bridge.read_world_status()
        if not world:
            return ""
        hour = world.get("hour")
        if hour is None:
            return ""
        return f", in-game {int(hour):02d}:{int(world.get('minutes') or 0):02d}"

    # ---- Mod Restart Logic ----

    async def handle_mod_updates(self) -> None:
        updated = await self.mod_checker.check_for_updates()
        if not updated:
            self.state.auto_restart_pending = False
            return
        self.state.updated_mods = updated
        if self.state.auto_restart_pending:
            print("Auto restart pending, skipping mod restart.")
            self.state.auto_restart_pending = False
            return
        self.state.restart_task = asyncio.create_task(self._mod_restart_sequence(notify=True))

    async def _mod_restart_sequence(self, notify: bool = True) -> None:
        try:
            # Check if a horde night would be interrupted.
            blocked, reason = self._is_horde_blocking_restart()
            if blocked:
                print(f"[ModRestart] Deferred: {reason}")
                await self.send_notification(
                    f"{Emojis.JEEVES} Mod update restart deferred — {reason}. Will retry after the event.",
                    discord.Colour.orange()
                )
                # Poll until the horde clears. The cap exists so a status file
                # that never leaves its phase cannot hold a mod update forever;
                # it is three hours because the guard now covers horde night
                # from the moodle, which is about 75 real minutes on its own,
                # plus the event and its airdrop tail.
                MAX_HORDE_WAIT = self.HORDE_DEFER_TIMEOUT // self.HORDE_DEFER_POLL
                for _ in range(MAX_HORDE_WAIT):
                    await asyncio.sleep(self.HORDE_DEFER_POLL)
                    blocked, reason = self._is_horde_blocking_restart()
                    if not blocked:
                        print("[ModRestart] Horde cleared, proceeding with restart.")
                        await self.send_notification(
                            f"{Emojis.JEEVES} Horde event concluded — proceeding with mod update restart.",
                            discord.Colour.purple()
                        )
                        break
                else:
                    hours = self.HORDE_DEFER_TIMEOUT // 3600
                    print(f"[ModRestart] Horde wait exceeded {hours} hours - proceeding anyway.")
                    await self.send_notification(
                        f"{Emojis.JEEVES} Horde wait exceeded {hours} hours — proceeding with mod update restart.",
                        discord.Colour.orange()
                    )

            if notify:
                await self.send_notification(
                    f"{Emojis.JEEVES} Mod update detected!", discord.Colour.purple(),
                    description=str(self.state.updated_mods)
                )
            await self.rcon.broadcast(
                "Mod update detected! Server will restart to apply changes. Disconnect now to skip the wait."
            )
            await self.poll_players()
            if self.state.players_online:
                await self._restart_countdown(reason="mod update")
            else:
                await self._immediate_restart()
        except asyncio.CancelledError:
            print("Mod update countdown cancelled.")

    async def _restart_countdown(self, reason: str = None) -> None:
        EMPTY_CHECKS_REQUIRED = 3
        consecutive_empty = 0

        stages = [
            ("10 Minutes", Emojis.SPIFFO_WAVE,    30),
            ("5 Minutes",  Emojis.SPIFFO_EDUCATE, 24),
            ("1 Minute",   Emojis.SPIFFO_KATANA,   5),
        ]

        for label, emoji, checks in stages:
            await self._announce_restart(label, emoji, reason)
            for check_num in range(checks):
                await asyncio.sleep(10)
                await self.poll_players()
                # Report WHICH source answered, not just that something did.
                # This used to print rcon=ok/FAIL unconditionally; now that the
                # roster can come from the mod bridge instead, naming the source
                # is the difference between a log that explains a surprising
                # player count and one that hides it.
                age = self.state.seconds_since_alive()
                alive = "FAIL" if age is None else f"{self.state.alive_source} {age:.0f}s ago"
                players = self.state.player_count if self.state.players_online else 0
                if (check_num + 1) % 5 == 0 or age is None:
                    print(f"[RestartCountdown] {label} — check {check_num + 1}/{checks} "
                          f"alive={alive} players={players}")
                if not self.state.players_online:
                    consecutive_empty += 1
                    if consecutive_empty >= EMPTY_CHECKS_REQUIRED:
                        print(f"[RestartCountdown] No players ({consecutive_empty}x), restarting immediately")
                        await self._immediate_restart()
                        return
                else:
                    consecutive_empty = 0
            print(f"[RestartCountdown] Stage '{label}' complete ({checks} checks)")

        await self._announce_restart("10 Seconds", Emojis.SPIFFO_STOP, reason)
        await asyncio.sleep(10)
        print("[RestartCountdown] Final countdown complete, restarting now")
        await self.restart_server()

    async def _announce_restart(self, label: str, emoji: str, reason: str = None) -> None:
        if reason == "mod update":
            msg = f"Mod update — restarting in {label}. Server restarts immediately if all players disconnect."
        else:
            msg = f"Server will automatically restart in {label}!"
        await self.send_notification(f"{emoji} {msg}", discord.Colour.yellow())
        await self.rcon.broadcast(msg)

    async def _immediate_restart(self) -> None:
        if self.state.is_restarting:
            return
        await self.send_notification(f"{Emojis.JEEVES} No players online, restarting immediately!", discord.Colour.purple())
        await self.restart_server()

    def cancel_restart_task(self) -> None:
        if self.state.restart_task and not self.state.restart_task.done():
            self.state.restart_task.cancel()


# =============================================================================
# BOT INSTANCE
# =============================================================================

print("=" * 50)
print("Project Zomboid Discord Bot Starting...")
print("=" * 50)

config = Config()
errors = config.validate()
if errors:
    for e in errors:
        print(f"  ❌ {e}")
    sys.exit(1)
print("\n✅ Configuration validated successfully!\n")

bot = PZBot(config)


# =============================================================================
# STARTUP
# =============================================================================

@bot.event
async def on_ready() -> None:
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    await bot.send_notification(f"{Emojis.JEEVES} Checking on the server...", discord.Colour.purple())

    # Try RCON, then fallback to process check
    server_online = False
    for attempt in range(3):
        if bot.rcon.is_server_online(timeout=10):
            server_online = True
            break
        print(f"[Startup] RCON check {attempt + 1}/3 failed, retrying...")
        await asyncio.sleep(5)

    if not server_online:
        proc = bot.config.SERVER_PROCESS_NAME
        if proc in _tasklist(f'IMAGENAME eq {proc}'):
            print(f"[Startup] RCON down but {proc} running — treating as online.")
            server_online = True

    if server_online:
        await bot.send_notification(f"{Emojis.HAPPY} Server is already Online!", discord.Colour.green())
        bot.state.first_start = False
        bot.state.server_ready = True
        bot.discover_server_pid()
    else:
        await bot.send_notification(f"{Emojis.PANIC} Server is Offline! Starting...", discord.Colour.red())
        await bot.start_server()


# =============================================================================
# ROLE CHECKS
# =============================================================================

def require_role(role_name: str):
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            return False
        role = discord.utils.get(interaction.guild.roles, name=role_name)
        if role is None or role not in interaction.user.roles:
            raise app_commands.MissingRole(role_name)
        return True
    return app_commands.check(predicate)


async def _send_error(interaction: discord.Interaction, embed: discord.Embed) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)
    except discord.NotFound:
        # Interaction expired (>3s) — nothing we can do
        pass
    except discord.HTTPException:
        pass


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    if isinstance(error, app_commands.MissingRole):
        embed = discord.Embed(
            title=f"{Emojis.ANGRY} Permission Denied",
            description=f"You need the **{error.missing_role}** role to use this command.",
            colour=discord.Colour.red()
        )
    elif isinstance(error, app_commands.CheckFailure):
        embed = discord.Embed(
            title=f"{Emojis.ANGRY} Permission Denied",
            description="You don't have permission to use this command.",
            colour=discord.Colour.red()
        )
    else:
        print(f"[CommandError] {type(error).__name__}: {error}")
        embed = discord.Embed(
            title=f"{Emojis.PANIC} An error occurred", description=str(error),
            colour=discord.Colour.red()
        )
    await _send_error(interaction, embed)


# =============================================================================
# HELPER: quick embed response
# =============================================================================

async def _respond(interaction: discord.Interaction, title: str,
                   colour: discord.Colour = discord.Colour.purple(),
                   ephemeral: bool = True, description: str = None) -> None:
    embed = discord.Embed(title=title, colour=colour, description=description)
    await interaction.response.send_message(embed=embed, ephemeral=ephemeral)


# =============================================================================
# SLASH COMMANDS
# =============================================================================

@bot.tree.command(name="hello", description="Returns Hello!")
@require_role(config.DEFAULT_ROLE)
async def cmd_hello(interaction: discord.Interaction) -> None:
    await _respond(interaction, "Hello!")


@bot.tree.command(name="players", description="Returns a list of players currently connected.")
@require_role(config.DEFAULT_ROLE)
async def cmd_players(interaction: discord.Interaction) -> None:
    response = await bot.rcon.send_command('players')
    await _respond(interaction, response or "Failed to get player list")


@bot.tree.command(name="online", description="Checks if game server is online.")
@require_role(config.DEFAULT_ROLE)
async def cmd_online(interaction: discord.Interaction) -> None:
    await _respond(interaction, "Checking on server...")
    if bot.rcon.is_server_online():
        await bot.send_notification(f"{Emojis.HAPPY} Server is Online!", discord.Colour.green())
    else:
        await bot.send_notification(f"{Emojis.PANIC} Server is Offline!", discord.Colour.red())


@bot.tree.command(name="restart", description="Restarts the game server. Optional countdown in minutes.")
@require_role(config.DEFAULT_ROLE)
@app_commands.describe(
    minutes="Countdown before restart (e.g. 10, 5, 1). Omit for immediate restart."
)
async def cmd_restart(interaction: discord.Interaction, minutes: int = None) -> None:
    if minutes is not None and minutes > 0:
        await interaction.response.defer()
        await interaction.followup.send(embed=discord.Embed(
            title=f"{Emojis.JEEVES} Restart scheduled",
            description=f"Server will restart in **{minutes} minute(s)**.",
            colour=discord.Colour.yellow()
        ))

        # Build countdown stages from the requested minutes
        stages = []
        remaining = minutes
        if remaining >= 10:
            stages.append(("10 Minutes", Emojis.SPIFFO_WAVE, (remaining - 5) * 60))
            remaining = 5
        if remaining >= 5:
            stages.append(("5 Minutes", Emojis.SPIFFO_EDUCATE, (remaining - 1) * 60))
            remaining = 1
        if remaining >= 1:
            stages.append(("1 Minute", Emojis.SPIFFO_KATANA, 50))
            remaining = 0

        for label, emoji, wait_seconds in stages:
            msg = f"Server will restart in {label}!"
            await bot.send_notification(f"{emoji} {msg}", discord.Colour.yellow())
            await bot.rcon.broadcast(msg)
            await asyncio.sleep(wait_seconds)

        msg = "Server restarting in 10 Seconds!"
        await bot.send_notification(f"{Emojis.SPIFFO_STOP} {msg}", discord.Colour.yellow())
        await bot.rcon.broadcast(msg)
        await asyncio.sleep(10)
        await bot.restart_server()
    else:
        await _respond(interaction, "Restarting server, this may take several minutes...", ephemeral=False)
        await bot.restart_server()


@bot.tree.command(name="start", description="Starts the game server if not running.")
@require_role(config.DEFAULT_ROLE)
async def cmd_start(interaction: discord.Interaction) -> None:
    await _respond(interaction, "Attempting to start the server...", ephemeral=False)
    if bot.rcon.is_server_online():
        await bot.send_notification(f"{Emojis.DIZZY} Server is already Online!", discord.Colour.green())
    else:
        await bot.start_server()


@bot.tree.command(name="stop", description="Stops the game server.")
@require_role(config.DEFAULT_ROLE)
async def cmd_stop(interaction: discord.Interaction) -> None:
    await _respond(interaction, "Shutting down the server...", ephemeral=False)
    await bot.stop_server()
    if bot.rcon.is_server_online():
        await bot.send_notification(f"{Emojis.HAPPY} Server is Online!", discord.Colour.green())
    else:
        await bot.send_notification(f"{Emojis.PANIC} Server is Offline!", discord.Colour.red())


@bot.tree.command(name="teleport", description="Teleports player1 to player2's location.")
@require_role(config.DEFAULT_ROLE)
async def cmd_teleport(interaction: discord.Interaction, player1: str, player2: str) -> None:
    await bot.rcon.send_command(f'teleport ("{player1}", "{player2}")')
    await _respond(interaction, f"Attempting to teleport {player1} to {player2}'s location.")


@bot.tree.command(name="setaccesslevel", description="Sets a player's access level on the server.")
@require_role(config.DEFAULT_ROLE)
async def cmd_setaccesslevel(interaction: discord.Interaction, player: str, level: str) -> None:
    valid_levels = ["admin", "moderator", "overseer", "gm", "observer", "none"]
    if level.lower() not in valid_levels:
        await _respond(interaction, f"Invalid access level: {level}. Valid: {', '.join(valid_levels)}", discord.Colour.red())
        return
    await bot.rcon.send_command(f'setaccesslevel "{player}" "{level.lower()}"')
    await _respond(interaction, f"Set {player}'s access level to **{level.lower()}**.")


@cmd_setaccesslevel.autocomplete("level")
async def _accesslevel_autocomplete(interaction: discord.Interaction, current: str):
    levels = ["admin", "moderator", "overseer", "gm", "observer", "none"]
    return [
        app_commands.Choice(name=lvl, value=lvl)
        for lvl in levels if current.lower() in lvl.lower()
    ]


@bot.tree.command(name="msg", description="Broadcasts a message to the server.")
@require_role(config.DEFAULT_ROLE)
async def cmd_msg(interaction: discord.Interaction, message: str) -> None:
    await interaction.response.defer(ephemeral=True)
    await bot.rcon.send_command(f'servermsg "{message}"')
    embed = discord.Embed(title="Message sent", colour=discord.Colour.purple())
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="skip", description="Skip the next scheduled automatic restart.")
@require_role(config.DEFAULT_ROLE)
async def cmd_skip(interaction: discord.Interaction) -> None:
    if bot.state.skip_next_restart:
        await _respond(interaction, f"{Emojis.DIZZY} The next restart is already being skipped.", discord.Colour.yellow())
    else:
        bot.state.skip_next_restart = True
        await _respond(interaction, f"{Emojis.JEEVES} Next scheduled restart will be skipped.", discord.Colour.green(), ephemeral=False)
        await bot.send_notification(
            f"{Emojis.JEEVES} Next automatic restart has been skipped by {interaction.user.display_name}.",
            discord.Colour.yellow()
        )


@bot.tree.command(name="unskip", description="Cancel a previously issued /skip.")
@require_role(config.DEFAULT_ROLE)
async def cmd_unskip(interaction: discord.Interaction) -> None:
    if not bot.state.skip_next_restart:
        await _respond(interaction, f"{Emojis.DIZZY} No restart is currently being skipped.", discord.Colour.yellow())
    else:
        bot.state.skip_next_restart = False
        await _respond(interaction, f"{Emojis.JEEVES} Skip cancelled — restarts resume as normal.", discord.Colour.green(), ephemeral=False)
        await bot.send_notification(
            f"{Emojis.JEEVES} Restart skip cancelled by {interaction.user.display_name}.",
            discord.Colour.yellow()
        )


@bot.tree.command(name="postpone", description="Postpone a mod update restart by 10 minutes.")
@require_role(config.DEFAULT_ROLE)
async def cmd_postpone(interaction: discord.Interaction) -> None:
    if not bot.state.restart_task or bot.state.restart_task.done():
        await _respond(interaction, f"{Emojis.DIZZY} No mod update restart is currently pending.", discord.Colour.yellow())
        return
    # Cancel the active countdown
    bot.cancel_restart_task()
    await _respond(
        interaction,
        f"{Emojis.JEEVES} Mod update restart postponed by 10 minutes.",
        discord.Colour.green(), ephemeral=False
    )
    await bot.send_notification(
        f"{Emojis.JEEVES} Mod update restart postponed 10 minutes by {interaction.user.display_name}.",
        discord.Colour.yellow()
    )
    await bot.rcon.broadcast("Mod update restart postponed by 10 minutes.")

    # Re-queue the restart after 10 minutes
    async def _delayed_restart():
        try:
            await asyncio.sleep(600)  # 10 minutes
            await bot.send_notification(
                f"{Emojis.JEEVES} Postponed mod update restart starting now.",
                discord.Colour.purple()
            )
            await bot._mod_restart_sequence()
        except asyncio.CancelledError:
            print("[Postpone] Delayed restart cancelled.")

    bot.state.restart_task = asyncio.create_task(_delayed_restart())


@bot.tree.command(name="mod", description="Checks if the server's mods are up to date.")
async def cmd_mod(interaction: discord.Interaction) -> None:
    await interaction.response.defer()
    updated = await bot.mod_checker.check_for_updates()
    if not updated:
        await interaction.followup.send(embed=discord.Embed(
            title=f"{Emojis.HAPPY} All mods are up to date!",
            description="No updates found.",
            colour=discord.Colour.green()
        ))
        return
    bot.state.updated_mods = updated
    bot.state.restart_task = asyncio.create_task(bot._mod_restart_sequence(notify=False))
    await interaction.followup.send(embed=discord.Embed(
        title=f"{Emojis.JEEVES} Mod update detected!",
        description=str(updated),
        colour=discord.Colour.purple()
    ))


@bot.tree.command(name="playerlist", description="Shows all players who have joined the server.")
@require_role(config.DEFAULT_ROLE)
async def cmd_playerlist(interaction: discord.Interaction) -> None:
    from player_tracker import get_all_players
    players = get_all_players()
    if not players:
        await _respond(interaction, "No players recorded yet.")
        return

    lines = [f"**{u}** — {c} session(s), first seen {f[:10]}" for u, f, _, c in players[:25]]
    desc = "\n".join(lines)
    if len(players) > 25:
        desc += f"\n\n*...and {len(players) - 25} more*"
    await _respond(interaction, f"Player Database ({len(players)} total)", description=desc)


@bot.tree.command(name="cleanmods", description="Remove unused mod folders from the server.")
@require_role(config.DEFAULT_ROLE)
async def cmd_cleanmods(interaction: discord.Interaction) -> None:
    await _respond(interaction, "Scanning for unused mods...")

    used_ids = set(bot.mod_checker.get_workshop_ids())
    if not used_ids:
        await interaction.followup.send(embed=discord.Embed(
            title=f"{Emojis.ANGRY} Could not read mod list!", colour=discord.Colour.red()
        ), ephemeral=True)
        return

    mods_folder = Path(bot.config.MODS_FOLDER_PATH)
    if not mods_folder.exists():
        await interaction.followup.send(embed=discord.Embed(
            title=f"{Emojis.ANGRY} Mods folder not found: {mods_folder}", colour=discord.Colour.red()
        ), ephemeral=True)
        return

    unused = [f for f in mods_folder.iterdir() if f.is_dir() and f.name not in used_ids]
    if not unused:
        await interaction.followup.send(embed=discord.Embed(
            title=f"{Emojis.HAPPY} No unused mods found!", colour=discord.Colour.green()
        ), ephemeral=True)
        return

    removed, failed = [], []
    for folder in unused:
        try:
            shutil.rmtree(folder)
            removed.append(folder.name)
        except Exception as e:
            print(f"Failed to remove {folder.name}: {e}")
            failed.append(folder.name)

    removed_list = "\n".join(removed[:40])
    if len(removed) > 40:
        removed_list += f"\n*...and {len(removed) - 40} more*"

    if not failed:
        embed = discord.Embed(
            title=f"{Emojis.HAPPY} Removed {len(removed)} unused mod(s)!",
            colour=discord.Colour.green(), description=f"**Removed:**\n{removed_list}"
        )
    else:
        embed = discord.Embed(
            title=f"{Emojis.DIZZY} Removed {len(removed)}, {len(failed)} failed.",
            colour=discord.Colour.yellow(),
            description=f"**Removed:**\n{removed_list}\n\n**Failed:**\n{chr(10).join(failed[:20])}"
        )
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="playsound", description="Trigger a Jeeves Alerts sound on all connected players via RCON.")
@require_role(config.DEFAULT_ROLE)
@app_commands.choices(sound=[
    app_commands.Choice(name="Alarm 1",            value="1"),
    app_commands.Choice(name="Alarm 2",            value="2"),
    app_commands.Choice(name="Alarm 3",            value="3"),
    app_commands.Choice(name="Alarm 4",            value="4"),
    app_commands.Choice(name="Alarm 5",            value="5"),
    app_commands.Choice(name="Ambient Creak",      value="6"),
    app_commands.Choice(name="Ambient Keyboard",   value="7"),
    app_commands.Choice(name="Chime Peaceful",     value="8"),
    app_commands.Choice(name="Chime Sword",        value="9"),
    app_commands.Choice(name="Chime Walkie",       value="10"),
    app_commands.Choice(name="Chime Wrong",        value="11"),
])
async def cmd_playsound(interaction: discord.Interaction, sound: app_commands.Choice[str], message: str = None) -> None:
    await interaction.response.defer(ephemeral=True)
    success = await lua_bridge.playsound(int(sound.value), message)
    desc = f"Sound: **{sound.name}**"
    if message:
        desc += f"\nMessage: *{message}*"
    if success:
        embed = discord.Embed(title="\U0001f50a Sound triggered!", description=desc, colour=discord.Colour.purple())
    else:
        embed = discord.Embed(title="Failed to trigger sound", description=desc, colour=discord.Colour.red())
    await interaction.followup.send(embed=embed, ephemeral=True)


# =============================================================================
# RANK COMMAND — shows the user their current rank based on highest role
# =============================================================================

_RANK_INFO = {
    0: ("Default",  "No color",  "\u2b1c"),
    1: ("Fuel",     "Green",     "\U0001f7e9"),
    2: ("Spark",    "Blue",      "\U0001f7e6"),
    3: ("Cinder",   "Violet",    "\U0001f7ea"),
    4: ("Flame",    "Yellow",    "\U0001f7e8"),
    5: ("Blaze",    "Orange",    "\U0001f7e7"),
    6: ("Inferno",  "Red",       "\U0001f7e5"),
}

@bot.tree.command(name="myrank", description="Shows your current in-game rank and chat color.")
async def cmd_myrank(interaction: discord.Interaction) -> None:
    highest = 0
    for role in interaction.user.roles:
        for rank_num, rank_role in config.RANKS.items():
            if role.name == rank_role and rank_num > highest:
                highest = rank_num

    name, color, emoji = _RANK_INFO.get(highest, ("Unknown", "None", "\u2753"))
    await _respond(
        interaction,
        f"{emoji} {name} — Rank {highest}",
        description=f"Your in-game chat name color: **{color}**\n"
                    f"Your rank shows in-game as your chat name color.",
    )


# =============================================================================
# ENTRY POINT
# =============================================================================

LOCK_FILE = Path(sys.executable).parent / "jeeves.lock" if getattr(sys, 'frozen', False) else Path(__file__).parent / "jeeves.lock"


def enforce_single_instance() -> None:
    my_pid = os.getpid()
    if LOCK_FILE.exists():
        try:
            old_pid = int(LOCK_FILE.read_text().strip())
        except (ValueError, OSError):
            old_pid = None
        if old_pid and old_pid != my_pid:
            if _IS_WINDOWS:
                output = _tasklist(f'PID eq {old_pid}')
                if str(old_pid) in output and "python" in output.lower():
                    print(f"[SingleInstance] Killing previous instance (PID: {old_pid})...")
                    os.system(f'taskkill /f /t /pid {old_pid} 2>nul')
                    time.sleep(2)
                else:
                    print(f"[SingleInstance] Stale lock file (PID {old_pid} gone).")
            else:
                # Linux: check if the old PID is still running
                try:
                    os.kill(old_pid, 0)  # signal 0 = check existence only
                    print(f"[SingleInstance] Killing previous instance (PID: {old_pid})...")
                    import signal
                    os.kill(old_pid, signal.SIGTERM)
                    time.sleep(2)
                except ProcessLookupError:
                    print(f"[SingleInstance] Stale lock file (PID {old_pid} gone).")
                except PermissionError:
                    print(f"[SingleInstance] Cannot kill PID {old_pid} (permission denied).")
    try:
        LOCK_FILE.write_text(str(my_pid))
    except OSError as e:
        print(f"[SingleInstance] Warning: {e}")


if __name__ == "__main__":
    enforce_single_instance()
    try:
        print("Starting bot...")
        bot.run(config.TOKEN)
    finally:
        try:
            LOCK_FILE.unlink(missing_ok=True)
        except OSError:
            pass

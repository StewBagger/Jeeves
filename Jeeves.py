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
        """Fetch current update times from Steam API (no API key required)."""
        if not workshop_ids:
            return {}

        data = {'itemcount': len(workshop_ids), 'format': 'json'}
        for i, item_id in enumerate(workshop_ids):
            data[f'publishedfileids[{i}]'] = item_id

        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                resp = await client.post(self.config.STEAM_API_URL, data=data)
                resp.raise_for_status()
                return {
                    str(item['publishedfileid']): {
                        'time': item.get('time_updated', 0),
                        'title': item.get('title', 'Unknown Mod')
                    }
                    for item in resp.json().get('response', {}).get('publishedfiledetails', [])
                }
            except Exception as e:
                print(f"Steam API error: {e}")
        return {}

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

        updated = [
            info['title'] for mod_id, info in current.items()
            if mod_id in previous and info['time'] > previous[mod_id]['time']
        ]

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

        for ext in ('auto_restart', 'mod_check_timer', 'player_tracker', 'rank_sync', 'chat_relay', 'jeeves_events', 'jeeves_drops', 'jeeves_modsorter', 'jeeves_modmanager', 'server_update', 'server_status', 'horde_leaderboard'):
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
        """Single RCON call that updates all player state. Used by all polling loops."""
        response = await self.rcon.send_command('players')
        if response is not None:
            self.state.last_rcon_ok = True
            self.state.player_names, self.state.player_count = self.rcon.parse_players(response)
            self.state.players_online = self.state.player_count > 0
        else:
            self.state.last_rcon_ok = False
        return response

    # ---- Horde Night Restart Guard ----

    def _is_horde_blocking_restart(self, real_minutes_ahead=0) -> tuple:
        """Check if a horde event should block a server restart.
        Returns (should_block, reason_string).

        real_minutes_ahead: how many real-world minutes until the restart
        would actually execute. The method projects the in-game time forward
        by that amount using the server's DayLength setting (hoursForDay).

        Conversion: 1 in-game day = hoursForDay real hours.
        So 1 in-game hour = hoursForDay/24 real hours = hoursForDay*2.5 real minutes.
        Inverse: 1 real minute = 24/(hoursForDay*60) in-game hours.

        Blocks if:
          - Horde is currently active
          - Horde is scheduled for today AND the current or projected
            in-game time falls inside or near the night window (19:00+)
          - Currently in the post-midnight tail of the window (before 7 AM)

        Day offset: The Lua horde scheduler's nextHordeDay runs 1 ahead of
        what the world status bridge reports as elapsedDays. The status page
        subtracts 1 from nextHordeDay for display; this function must apply
        the same offset when comparing against the current day.
        """
        horde = lua_bridge.read_horde_status()
        world = lua_bridge.read_world_status()

        if not horde:
            return False, ""

        phase = horde.get("phase", "")

        # Active horde — always block
        if phase == "active":
            return True, "Horde night is currently active"

        # Check if horde is scheduled for today and window is imminent
        if phase == "scheduled" and world:
            next_day = horde.get("nextHordeDay")
            if next_day is not None:
                # Use elapsedDays (accurate date-based count from bridge)
                # with fallback to worldAgeDays. Match the status page logic.
                elapsed = world.get("elapsedDays")
                if elapsed is not None:
                    current_day = int(elapsed)
                else:
                    age_raw = world.get("worldAgeDays", 0)
                    current_day = int(age_raw) if age_raw >= 1 else max(1, int(age_raw))

                hour = world.get("hour", 12)

                # Calculate in-game hours that will pass in real_minutes_ahead
                hours_for_day = world.get("hoursForDay", 2)
                if hours_for_day <= 0:
                    hours_for_day = 2
                # 1 real minute = 24 / (hoursForDay * 60) in-game hours
                ig_hours_per_real_min = 24.0 / (hours_for_day * 60)
                lookahead_ig_hours = real_minutes_ahead * ig_hours_per_real_min
                projected_hour = hour + lookahead_ig_hours

                # nextHordeDay is 1 ahead of the in-game display day.
                # Subtract 1 to align with current_day (same offset as
                # server_status._horde_fields).
                display_horde_day = next_day - 1

                if display_horde_day == current_day:
                    if hour >= 19 or projected_hour >= 19:
                        return True, (f"Horde night tonight (day {display_horde_day}), "
                                      f"in-game {hour}:00, projected {projected_hour:.0f}:00 "
                                      f"in {real_minutes_ahead}m")
                    # Post-midnight tail of tonight's window
                    if hour < 7:
                        return True, f"Horde night active (day {display_horde_day}), in-game {hour}:00 (post-midnight)"

        return False, ""

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
        self.state.restart_task = asyncio.create_task(self._mod_restart_sequence())

    async def _mod_restart_sequence(self) -> None:
        try:
            # Check if a horde night would be interrupted.
            # Mod restart countdown takes ~15 real minutes = ~3 in-game hours.
            blocked, reason = self._is_horde_blocking_restart(real_minutes_ahead=15)
            if blocked:
                print(f"[ModRestart] Deferred: {reason}")
                await self.send_notification(
                    f"{Emojis.JEEVES} Mod update restart deferred — {reason}. Will retry after the event.",
                    discord.Colour.orange()
                )
                # Wait and retry every 5 minutes until the horde clears
                while True:
                    await asyncio.sleep(300)
                    blocked, reason = self._is_horde_blocking_restart()
                    if not blocked:
                        print("[ModRestart] Horde cleared, proceeding with restart.")
                        await self.send_notification(
                            f"{Emojis.JEEVES} Horde event concluded — proceeding with mod update restart.",
                            discord.Colour.purple()
                        )
                        break

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
@require_role(config.DEFAULT_ROLE)
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
    bot.state.restart_task = asyncio.create_task(bot._mod_restart_sequence())
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
                    f"Use `/jeevesrank` in-game to verify your rank.",
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

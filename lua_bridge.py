"""
lua_bridge.py — Unified Lua file-based command bridge for Jeeves bot.

The bot writes command files to the Zomboid/Lua/ directory and the
JeevesIntegration server mod polls for them.

Two command files to avoid queueing delays:
  jeeves_commands.lua  — General commands (broadcast sounds, playsound,
                         rankpush, horde, etc.)
  jeeves_chat.lua      — Dedicated chat relay file. Discord messages are
                         written here so they never queue behind broadcasts.

Architecture:
  - Separate write points and locks for commands vs chat relay.
  - Each file has a unique incrementing ID + timestamp for dedup.
  - The mod polls both files every ~2 seconds.

Communication strategy:
  - Broadcasts (restart warnings, /msg): RCON servermsg handles the red
    alert text display. The lua bridge only triggers the alert sound via
    sendServerCommand so the client mod can analyze and play audio.
  - Chat relay: lua bridge only (injected directly into chat panel by the
    client mod, no red alert text).
  - PlaySound / Horde / RankPush: lua bridge only.
"""

import os
import time
import asyncio
from pathlib import Path
from typing import Optional

# Module-level state
_lua_dir: Optional[Path] = None
_command_id: int = 0
_chat_id: int = 0
_write_lock = asyncio.Lock()
_chat_lock = asyncio.Lock()

# Filenames the Jeeves Integration mod watches for
COMMAND_FILE = "jeeves_commands.lua"
CHAT_FILE = "jeeves_chat.lua"


def init(bot) -> Path:
    """Initialize the Lua bridge using the bot's SERVER_INI_PATH config.
    Call this once during bot startup (e.g. in setup_hook or on_ready).
    Returns the Lua directory path.
    """
    global _lua_dir

    ini_path = getattr(bot.config, 'SERVER_INI_PATH', None)
    if ini_path:
        _lua_dir = Path(ini_path).parent.parent / "Lua"
    else:
        _lua_dir = Path(os.path.expanduser('~')) / 'Zomboid' / 'Lua'
        print("[LuaBridge] WARNING: SERVER_INI_PATH not set, using fallback: " + str(_lua_dir))

    _lua_dir.mkdir(parents=True, exist_ok=True)
    print(f"[LuaBridge] Initialized — commands: {_lua_dir / COMMAND_FILE}, chat: {_lua_dir / CHAT_FILE}")
    return _lua_dir


def get_lua_dir() -> Optional[Path]:
    """Return the configured Lua directory, or None if not initialized."""
    return _lua_dir


def _escape_lua_string(s: str) -> str:
    """Escape a Python string for safe embedding in a Lua string literal."""
    return (s
            .replace('\\', '\\\\')
            .replace('"', '\\"')
            .replace('\n', '\\n')
            .replace('\r', '\\r'))


def _build_lua_table(command: str, cmd_id: int, **kwargs) -> str:
    """Build a Lua table string for a command with arbitrary key-value pairs."""
    lines = [
        'return {',
        f'    command = "{_escape_lua_string(command)}",',
        f'    id = {cmd_id},',
        f'    timestamp = {int(time.time())},',
    ]

    for key, value in kwargs.items():
        if value is None:
            continue
        if isinstance(value, bool):
            lines.append(f'    {key} = {str(value).lower()},')
        elif isinstance(value, (int, float)):
            lines.append(f'    {key} = {value},')
        elif isinstance(value, str):
            lines.append(f'    {key} = "{_escape_lua_string(value)}",')
        else:
            lines.append(f'    {key} = "{_escape_lua_string(str(value))}",')

    lines.append('}')
    return '\n'.join(lines)


async def _write_file(filepath: Path, lock: asyncio.Lock, content: str, label: str) -> bool:
    """Write content to a file with lock and retry logic."""
    async with lock:
        # Wait for the mod to consume any previous command file.
        # The PZ mod "deletes" by overwriting with empty content (PZ has no
        # os.remove), so we check for empty OR missing file as "consumed".
        # The mod polls every ~0.5s, so 10 attempts × 0.5s = 5s max wait.
        for attempt in range(10):
            if not filepath.exists():
                break
            try:
                size = filepath.stat().st_size
                if size == 0:
                    break  # Mod consumed the previous command (wrote empty file)
            except OSError:
                break
            await asyncio.sleep(0.5)
        else:
            print(f"[LuaBridge] WARNING: Previous command not consumed after 5s, overwriting for {label}")

        try:
            filepath.write_text(content, encoding='utf-8')
            print(f"[LuaBridge] Wrote {label}")
            # Brief pause after writing to give the mod time to read before
            # the lock is released and the next caller can overwrite.
            await asyncio.sleep(0.6)
            return True
        except Exception as e:
            print(f"[LuaBridge] Failed to write {label}: {e}")
            return False


async def write_command(command: str, **kwargs) -> bool:
    """Write a command to jeeves_commands.lua."""
    if _lua_dir is None:
        print("[LuaBridge] ERROR: Not initialized! Call lua_bridge.init(bot) first.")
        return False

    global _command_id
    _command_id += 1
    content = _build_lua_table(command, _command_id, **kwargs)
    return await _write_file(
        _lua_dir / COMMAND_FILE, _write_lock, content,
        f"command '{command}' (id={_command_id})"
    )


async def write_chat(author: str, message: str) -> bool:
    """Write a chat relay command to jeeves_chat.lua (dedicated file)."""
    if _lua_dir is None:
        print("[LuaBridge] ERROR: Not initialized! Call lua_bridge.init(bot) first.")
        return False

    global _chat_id
    _chat_id += 1
    content = _build_lua_table("chat", _chat_id, author=author, message=message)
    return await _write_file(
        _lua_dir / CHAT_FILE, _chat_lock, content,
        f"chat relay '[{author}] {message}' (id={_chat_id})"
    )


# =========================================================================
# Convenience wrappers
# =========================================================================

async def broadcast(message: str, sound_only: bool = False) -> bool:
    """Send a broadcast to all players via lua bridge.

    Args:
        message: The broadcast text.
        sound_only: If True, client only plays the alert sound (no chat
            panel display). Used when RCON servermsg handles the display.
            If False, client displays the message in chat AND plays sound.
            Used for welcome messages and other non-RCON broadcasts.
    """
    return await write_command("broadcast", message=message,
                               soundOnly=sound_only)


async def chat_relay(author: str, message: str) -> bool:
    """Relay a Discord chat message to the game via dedicated chat file."""
    return await write_chat(author, message)


async def playsound(sound_id: int, message: str = None) -> bool:
    """Trigger a Jeeves Alerts sound on all connected players."""
    return await write_command("playsound", sound=sound_id, message=message or "")


async def rank_push() -> bool:
    """Signal the mod to reload the jeeves_ranks.lua file."""
    return await write_command("rankpush")


async def horde(count: int) -> bool:
    """Trigger a casual zombie horde event with a specific count per player."""
    return await write_command("horde", count=count)


async def horde_stop() -> bool:
    """Stop an active horde event."""
    return await write_command("hordestop")


async def horde_status() -> bool:
    """Request horde status output."""
    return await write_command("hordestatus")


async def horde_night() -> bool:
    """Simulate a scheduled/random horde night with moodle warning."""
    return await write_command("hordenight")


async def horde_reset() -> bool:
    """Reset horde progress — event count and all survivor multipliers."""
    return await write_command("hordereset")


async def airdrop(target_player: str = None, crate_type: str = None) -> bool:
    """Trigger an air drop event on a random or specific player."""
    kwargs = {}
    if target_player:
        kwargs['targetPlayer'] = target_player
    if crate_type:
        kwargs['crateType'] = crate_type
    return await write_command("airdrop", **kwargs)


async def airdrop_status() -> bool:
    """Request air drop status output."""
    return await write_command("airdropstatus")


async def supply_event() -> bool:
    """Force-trigger a supply drop event."""
    return await write_command("supplyevent")


async def supply_event_status() -> bool:
    """Request supply event status output."""
    return await write_command("supplyeventstatus")


# =========================================================================
# Horde status file reader (server -> bot communication)
# =========================================================================

HORDE_STATUS_FILE = "jeeves_horde_status.lua"
DROPS_STATUS_FILE = "jeeves_drops_status.lua"


def read_horde_status() -> dict | None:
    """Read the horde status file written by JeevesHordesServer.
    Returns the parsed Lua table as a dict, or None if unavailable.
    Non-async because it's a simple file read."""
    if _lua_dir is None:
        return None

    filepath = _lua_dir / HORDE_STATUS_FILE
    if not filepath.exists():
        return None

    try:
        text = filepath.read_text(encoding='utf-8').strip()
        if not text:
            return None

        # Parse simple Lua table: return { key = value, ... }
        # Strip "return {" and "}"
        inner = text
        if inner.startswith("return"):
            inner = inner[6:].strip()
        if inner.startswith("{"):
            inner = inner[1:]
        if inner.endswith("}"):
            inner = inner[:-1]

        result = {}
        for line in inner.split('\n'):
            line = line.strip().rstrip(',')
            if '=' not in line:
                continue
            key, _, val = line.partition('=')
            key = key.strip()
            val = val.strip()

            # Parse value types
            if val.startswith('"') and val.endswith('"'):
                result[key] = val[1:-1]
            elif val == "true":
                result[key] = True
            elif val == "false":
                result[key] = False
            else:
                try:
                    result[key] = int(val)
                except ValueError:
                    try:
                        result[key] = float(val)
                    except ValueError:
                        result[key] = val

        return result if result else None

    except Exception as e:
        print(f"[LuaBridge] Failed to read horde status: {e}")
        return None


def read_drops_status() -> dict | None:
    """Read the drops status file written by JeevesDropsServer.
    Returns the parsed Lua table as a dict, or None if unavailable.
    Non-async because it's a simple file read."""
    if _lua_dir is None:
        return None

    filepath = _lua_dir / DROPS_STATUS_FILE
    if not filepath.exists():
        return None

    try:
        text = filepath.read_text(encoding='utf-8').strip()
        if not text:
            return None

        inner = text
        if inner.startswith("return"):
            inner = inner[6:].strip()
        if inner.startswith("{"):
            inner = inner[1:]
        if inner.endswith("}"):
            inner = inner[:-1]

        result = {}
        for line in inner.split('\n'):
            line = line.strip().rstrip(',')
            if '=' not in line:
                continue
            key, _, val = line.partition('=')
            key = key.strip()
            val = val.strip()

            if val.startswith('"') and val.endswith('"'):
                result[key] = val[1:-1]
            elif val == "true":
                result[key] = True
            elif val == "false":
                result[key] = False
            else:
                try:
                    result[key] = int(val)
                except ValueError:
                    try:
                        result[key] = float(val)
                    except ValueError:
                        result[key] = val

        return result if result else None

    except Exception as e:
        print(f"[LuaBridge] Failed to read drops status: {e}")
        return None


WORLD_STATUS_FILE = "jeeves_world_status.lua"
SUPPLY_EVENT_STATUS_FILE = "jeeves_supply_event_status.lua"


def read_world_status() -> dict | None:
    """Read the world status file written by JeevesWorldStatus.lua.
    Returns the parsed Lua table as a dict, or None if unavailable.
    Non-async because it's a simple file read."""
    if _lua_dir is None:
        return None

    filepath = _lua_dir / WORLD_STATUS_FILE
    if not filepath.exists():
        return None

    try:
        text = filepath.read_text(encoding='utf-8').strip()
        if not text:
            return None

        inner = text
        if inner.startswith("return"):
            inner = inner[6:].strip()
        if inner.startswith("{"):
            inner = inner[1:]
        if inner.endswith("}"):
            inner = inner[:-1]

        result = {}
        for line in inner.split('\n'):
            line = line.strip().rstrip(',')
            if '=' not in line:
                continue
            key, _, val = line.partition('=')
            key = key.strip()
            val = val.strip()

            if val.startswith('"') and val.endswith('"'):
                result[key] = val[1:-1]
            elif val == "true":
                result[key] = True
            elif val == "false":
                result[key] = False
            else:
                try:
                    result[key] = int(val)
                except ValueError:
                    try:
                        result[key] = float(val)
                    except ValueError:
                        result[key] = val

        return result if result else None

    except Exception as e:
        print(f"[LuaBridge] Failed to read world status: {e}")
        return None


def read_supply_event_status() -> dict | None:
    """Read the supply event status file written by JeevesDropsSupplyEvents.
    Returns the parsed Lua table as a dict, or None if unavailable.
    Non-async because it's a simple file read."""
    if _lua_dir is None:
        return None

    filepath = _lua_dir / SUPPLY_EVENT_STATUS_FILE
    if not filepath.exists():
        return None

    try:
        text = filepath.read_text(encoding='utf-8').strip()
        if not text:
            return None

        inner = text
        if inner.startswith("return"):
            inner = inner[6:].strip()
        if inner.startswith("{"):
            inner = inner[1:]
        if inner.endswith("}"):
            inner = inner[:-1]

        result = {}
        for line in inner.split('\n'):
            line = line.strip().rstrip(',')
            if '=' not in line:
                continue
            key, _, val = line.partition('=')
            key = key.strip()
            val = val.strip()

            if val.startswith('"') and val.endswith('"'):
                result[key] = val[1:-1]
            elif val == "true":
                result[key] = True
            elif val == "false":
                result[key] = False
            else:
                try:
                    result[key] = int(val)
                except ValueError:
                    try:
                        result[key] = float(val)
                    except ValueError:
                        result[key] = val

        return result if result else None

    except Exception as e:
        print(f"[LuaBridge] Failed to read supply event status: {e}")
        return None


# =========================================================================
# Horde survivor data reader (leaderboard)
# =========================================================================

SURVIVOR_FILE = "jeeves_horde_survivors.lua"


def read_survivor_data() -> dict | None:
    """Read the horde survivor data file written by JeevesHordesServer.
    Returns a dict of { "username": { "mult": float, "survived": int }, ... }
    or None if unavailable. Non-async because it's a simple file read."""
    if _lua_dir is None:
        return None

    filepath = _lua_dir / SURVIVOR_FILE
    if not filepath.exists():
        return None

    try:
        text = filepath.read_text(encoding='utf-8').strip()
        if not text:
            return None

        # Parse: return { ["name"] = { mult = 0.3, survived = 3 }, ... }
        inner = text
        if inner.startswith("return"):
            inner = inner[6:].strip()
        if inner.startswith("{"):
            inner = inner[1:]
        if inner.rstrip().endswith("}"):
            inner = inner.rstrip()[:-1]

        result = {}
        import re
        # Match each player entry: ["name"] = { key = val, key = val, ... },
        pattern = re.compile(
            r'\["([^"]+)"\]\s*=\s*\{([^}]*)\}',
            re.DOTALL
        )
        for match in pattern.finditer(inner):
            username = match.group(1)
            fields_str = match.group(2)
            entry = {}
            for field in fields_str.split(','):
                field = field.strip()
                if '=' not in field:
                    continue
                k, _, v = field.partition('=')
                k = k.strip()
                v = v.strip()
                if v == "true":
                    entry[k] = True
                elif v == "false":
                    entry[k] = False
                else:
                    try:
                        entry[k] = float(v)
                    except ValueError:
                        entry[k] = v
            result[username] = entry

        return result if result else None

    except Exception as e:
        print(f"[LuaBridge] Failed to read survivor data: {e}")
        return None


def reset_player_survivor(username: str) -> bool:
    """Reset a single player's horde survivor data by editing the bridge file.
    Sets mult=0, survived=0, clears streaks. Works while server is offline.
    Returns True on success, False on failure."""
    if _lua_dir is None:
        return False

    filepath = _lua_dir / SURVIVOR_FILE
    if not filepath.exists():
        print(f"[LuaBridge] Survivor file not found: {filepath}")
        return False

    try:
        data = read_survivor_data()
        if not data:
            print(f"[LuaBridge] No survivor data to reset")
            return False

        key = username.lower()
        if key not in data:
            # Try case-insensitive match
            for k in data:
                if k.lower() == key:
                    key = k
                    break

        if key not in data:
            print(f"[LuaBridge] Player '{username}' not found in survivor data")
            return False

        # Reset the player's entry
        data[key] = {"mult": 0.0, "survived": 0}

        # Write back the full file
        lines = ["return {"]
        for name, entry in data.items():
            parts = []
            parts.append(f"mult = {entry.get('mult', 0):.1f}")
            parts.append(f"survived = {int(entry.get('survived', 0))}")
            # Preserve other fields if present
            for field in ('immune', 'immuneDay', 'fragranceBuffDay', 'lastStewDay',
                          'lastFragranceDay', 'stewStreak', 'fragranceStreak'):
                val = entry.get(field)
                if val is not None and val is not False and val != 0:
                    if isinstance(val, bool):
                        parts.append(f"{field} = true")
                    elif isinstance(val, (int, float)):
                        parts.append(f"{field} = {int(val)}")
            lines.append(f'  ["{name}"] = {{ {", ".join(parts)} }},')
        lines.append("}")

        filepath.write_text("\n".join(lines) + "\n", encoding='utf-8')
        print(f"[LuaBridge] Reset survivor data for '{username}' (key='{key}')")
        return True

    except Exception as e:
        print(f"[LuaBridge] Failed to reset player survivor data: {e}")
        return False

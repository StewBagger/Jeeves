"""
Player Tracker Extension
Watches the PZ server _user.txt log file for join events and sends
welcome / welcome-back messages to Discord and in-game via RCON.

Log file detection:
  - Looks in USER_LOG_PATH (config) for the newest file ending in _user.txt
  - Handles log rotation automatically (new file each server session)

Trigger lines and their roles
──────────────────────────────────────────────────────────────────────────
  "<STEAMID> "Name" attempting to join."
      → Discord welcome notification (first-time players only, 10 s delay)

  "<STEAMID> "Name" fully connected (x,y,z)."
      → In-game RCON broadcast  (first-time: Welcome message, 10 s delay)
                                 (returning:  Welcome Back message, 10 s delay)
──────────────────────────────────────────────────────────────────────────

The 10-second delays are independent per player and run as background tasks
so they never block the tail loop.
"""

import asyncio
import datetime
import os
import re
import sqlite3
from pathlib import Path
from typing import Optional, Set

import discord
from discord.ext import commands, tasks

import lua_bridge

_DEFAULT_LOG_PATH = ""

# ── DB ───────────────────────────────────────────────────────────────────────

DB_PATH = Path(__file__).parent / "players.db"


def _db():
    return sqlite3.connect(DB_PATH)


def init_db():
    with _db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS players (
                username    TEXT PRIMARY KEY,
                first_seen  TEXT NOT NULL,
                last_seen   TEXT NOT NULL,
                join_count  INTEGER NOT NULL DEFAULT 1
            )
        """)


def upsert_player(username: str) -> bool:
    """Insert or update player.  Returns True if this is their first visit."""
    now = datetime.datetime.utcnow().isoformat()
    with _db() as conn:
        existing = conn.execute(
            "SELECT 1 FROM players WHERE username = ?", (username,)
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO players (username, first_seen, last_seen, join_count) VALUES (?, ?, ?, 1)",
                (username, now, now),
            )
            return True
        conn.execute(
            "UPDATE players SET last_seen = ?, join_count = join_count + 1 WHERE username = ?",
            (now, username),
        )
        return False


def get_all_players() -> list:
    with _db() as conn:
        return conn.execute(
            "SELECT username, first_seen, last_seen, join_count FROM players ORDER BY join_count DESC"
        ).fetchall()


# ── Log-line regexes ─────────────────────────────────────────────────────────

# Matches:  [17-02-26 14:29:33.432] 76561198957637531 "Plume" attempting to join.
_ATTEMPTING_RE = re.compile(r'^\[\S+\s+\S+\]\s+\d+\s+"(.+?)"\s+attempting to join\.')

# Matches:  [17-02-26 14:30:57.776] 76561198957637531 "Plume" fully connected (1720,5907,0).
_CONNECTED_RE = re.compile(r'^\[\S+\s+\S+\]\s+\d+\s+"(.+?)"\s+fully connected \(')


# ── Cog ──────────────────────────────────────────────────────────────────────

class PlayerTrackerCog(commands.Cog):

    def __init__(self, bot):
        self.bot = bot

        # Path to the Logs folder (set via USER_LOG_PATH in config.env)
        self._log_dir: str = os.getenv("USER_LOG_PATH", _DEFAULT_LOG_PATH)

        # File-tail state
        self._current_log: Optional[str] = None
        self._file_pos: int = 0

        # Track which players we've already queued a Discord notification for
        # within this join attempt, so we don't double-fire.
        self._pending_discord: Set[str] = set()

        # Dedup guard for in-game welcome messages (name -> timestamp)
        self._welcome_sent: dict = {}

        init_db()
        self._tail_user_log.start()

    def cog_unload(self):
        self._tail_user_log.cancel()

    # ── File helpers ─────────────────────────────────────────────────────────

    def _find_latest_user_log(self) -> Optional[str]:
        """Return the path of the most recent *_user.txt file in the log dir."""
        log_dir = Path(self._log_dir)
        if not log_dir.is_dir():
            return None
        candidates = sorted(log_dir.glob("*_user.txt"), reverse=True)
        return str(candidates[0]) if candidates else None

    # ── Delayed-action helpers ────────────────────────────────────────────────

    async def _delayed_discord_welcome(self, name: str, delay: float = 10.0):
        """Send a Discord embed after *delay* seconds (first-time players only)."""
        await asyncio.sleep(delay)
        await self.bot.send_notification(
            f"{self.bot.Emojis.SPIFFO_WAVE} New player **{name}** has joined for the first time!",
            discord.Colour.blue(),
        )
        print(f"[PlayerTracker] Discord welcome sent -> {name}")

    async def _delayed_rcon_broadcast(self, name: str, is_new: bool, delay: float = 0.0):
        """Send an in-game chat message."""
        await asyncio.sleep(delay)
        # Dedup: skip if we already sent a welcome for this player recently
        now = __import__('time').time()
        last = self._welcome_sent.get(name, 0)
        if now - last < 30:
            print(f"[PlayerTracker] Skipping duplicate welcome for {name}")
            return
        self._welcome_sent[name] = now

        if is_new:
            msg = f"Welcome to the server, {name}! Enjoy your stay and be safe out there!"
        else:
            msg = f"Welcome back, {name}!"
        await lua_bridge.write_command("display", message=msg)
        label = "First-time" if is_new else "Returning"
        print(f"[PlayerTracker] Lua display ({label}) -> {name}")

    # ── Main tail loop ────────────────────────────────────────────────────────

    @tasks.loop(seconds=2.0)
    async def _tail_user_log(self):
        try:
            log_file = self._find_latest_user_log()
            if not log_file:
                return

            # ── Log rotation: new file detected ──────────────────────────────
            if log_file != self._current_log:
                self._current_log = log_file
                self._pending_discord.clear()
                try:
                    # Seek to end so we don't replay history from before bot start
                    self._file_pos = os.path.getsize(log_file)
                except OSError:
                    self._file_pos = 0
                print(f"[PlayerTracker] Now tailing: {log_file}")
                return

            try:
                file_size = os.path.getsize(log_file)
            except OSError:
                return

            # File was truncated / rotated mid-session
            if file_size < self._file_pos:
                self._file_pos = 0

            if file_size <= self._file_pos:
                return

            try:
                with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(self._file_pos)
                    new_lines = f.readlines()
                    self._file_pos = f.tell()
            except (OSError, IOError):
                return

            for line in new_lines:
                line = line.strip()

                # ── "attempting to join" → Discord notification (first-time only) ──
                m = _ATTEMPTING_RE.match(line)
                if m:
                    name = m.group(1)
                    # Peek at the DB without inserting — upsert happens on "fully connected"
                    with _db() as conn:
                        already_known = conn.execute(
                            "SELECT 1 FROM players WHERE username = ?", (name,)
                        ).fetchone()
                    if already_known is None and name not in self._pending_discord:
                        self._pending_discord.add(name)
                        asyncio.ensure_future(self._delayed_discord_welcome(name))
                        print(f"[PlayerTracker] Queued Discord welcome (10 s) -> {name}")
                    continue

                # ── "fully connected" → RCON broadcast (first-time or returning) ──
                m = _CONNECTED_RE.match(line)
                if m:
                    name = m.group(1)
                    is_new = upsert_player(name)

                    # Sync ranks (unchanged from original behaviour)
                    rank_cog = self.bot.get_cog("RankSync")
                    if rank_cog:
                        rank_cog.sync_by_pz_username(name)

                    asyncio.ensure_future(self._delayed_rcon_broadcast(name, is_new))
                    label = "First-time" if is_new else "Returning"
                    print(f"[PlayerTracker] Queued RCON broadcast ({label}, 10 s) -> {name}")

                    # Clean up the pending-discord tracking set
                    self._pending_discord.discard(name)
                    continue

        except Exception as e:
            print(f"[PlayerTracker] Tail error: {e}")

    @_tail_user_log.before_loop
    async def _before_tail(self):
        await self.bot.wait_until_ready()
        print("[PlayerTracker] Started - watching for *_user.txt log")


async def setup(bot):
    await bot.add_cog(PlayerTrackerCog(bot))

"""
Chat Relay Extension for Jeeves Bot

Bridges in-game PZ chat with a Discord channel.

Game -> Discord:
  - Tails the server chat log file for player messages
  - Strips <RGB:...> color tags from author names
  - Posts as plain text with ANSI color codes for ranked players

Discord -> Game:
  - Listens for messages in the relay channel
  - Forwards them to the PZ server via RCON servermsg

Config (config.env):
  CHAT_RELAY_CHANNEL_ID=  (Discord channel ID for the chat relay)
  CHAT_LOG_PATH=          (Path to the PZ server Logs folder or chat log file)
"""

import re
import os
import asyncio
import discord
from pathlib import Path
from discord.ext import commands, tasks
from typing import Optional

# Regex to strip PZ rich-text tags from author names
RGB_TAG_RE = re.compile(r'<RGB:[^>]+>')
SIZE_TAG_RE = re.compile(r'<SIZE:[^>]+>')

# Parse player chat lines from the PZ server chat log
# Format: [timestamp][info] Got message:ChatMessage{chat=General, author='StewBag', text='hello'}.
CHAT_LINE_RE = re.compile(
    r'\[.*?\]\[info\] Got message:ChatMessage\{chat=(\w+), author=\'([^\']+)\', text=\'([^\']*)\'\}'
)



# Chat types we relay
RELAY_CHAT_TYPES = {'General'}

# Discord ANSI color codes (used inside ```ansi blocks)
# Discord supports: 30=gray, 31=red, 32=green, 33=yellow, 34=blue, 35=pink, 36=cyan, 37=white
# Bold variants (1;xx) are brighter
ANSI_COLORS = {
    0: None,        # Default - no color
    1: "1;32",      # Fuel - bold green
    2: "1;34",      # Spark - bold blue
    3: "1;35",      # Cinder - bold pink/violet
    4: "1;33",      # Flame - bold yellow
    5: "1;36",      # Blaze - bold cyan
    6: "1;31",      # Inferno - bold red
}


def strip_rgb_tags(text: str) -> str:
    """Remove all <RGB:...> and <SIZE:...> tags from a string."""
    text = RGB_TAG_RE.sub('', text)
    text = SIZE_TAG_RE.sub('', text)
    return text.strip()


class ChatRelay(commands.Cog):
    """Relays chat between PZ server and Discord."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._channel_id = int(os.getenv('CHAT_RELAY_CHANNEL_ID', '0'))
        self._log_path = os.getenv(
            'CHAT_LOG_PATH',
            r'C:\Users\ut2k3\Zomboid\Logs'
        )
        self._file_pos = 0
        self._current_log = None
        self._active = False

        if not self._channel_id:
            print("[ChatRelay] WARNING: CHAT_RELAY_CHANNEL_ID not set. Relay disabled.")
            return

        self._active = True
        self._tail_chat_log.start()
        print(f"[ChatRelay] Relay active. Channel={self._channel_id} Log={self._log_path}")

    def cog_unload(self):
        if self._active:
            self._tail_chat_log.cancel()

    def _get_channel(self) -> Optional[discord.TextChannel]:
        return self.bot.get_channel(self._channel_id)

    def _get_rank_for_author(self, author: str) -> int:
        """Look up the rank for a PZ username from the RankSync cog."""
        rank_cog = self.bot.get_cog("RankSync")
        if not rank_cog:
            return 0

        # Check the in-memory rank table (case-insensitive)
        for pz_name, rank in rank_cog._ranks.items():
            if pz_name.lower() == author.lower():
                return rank

        return 0

    def _format_message(self, chat_type: str, author: str, text: str) -> str:
        """Format a chat message for Discord with ANSI rank colors."""
        rank = self._get_rank_for_author(author)
        ansi_code = ANSI_COLORS.get(rank)

        if ansi_code:
            # Use ANSI code block for colored name
            return f"```ansi\n\u001b[{ansi_code}m[{author}]\u001b[0m: {text}\n```"
        else:
            # Plain text for unranked players
            return f"**[{author}]**: {text}"

    # ================================================================
    # Game -> Discord: tail the chat log file
    # ================================================================

    def _find_latest_chat_log(self) -> Optional[str]:
        """Find the most recent chat log file.
        PZ names them like: YYYY-MM-DD_HH-MM_chat.txt"""
        log_path = Path(self._log_path)

        if log_path.is_file():
            return str(log_path)

        search_dir = log_path if log_path.is_dir() else log_path.parent
        if not search_dir.is_dir():
            return None

        chat_logs = sorted(search_dir.glob('*chat*.txt'), reverse=True)
        if chat_logs:
            return str(chat_logs[0])
        return None

    @tasks.loop(seconds=2.0)
    async def _tail_chat_log(self):
        """Poll the chat log for new lines and relay player messages."""
        if not self._active:
            return

        try:
            log_file = self._find_latest_chat_log()
            if not log_file:
                return

            # Detect log rotation (new file)
            if log_file != self._current_log:
                self._current_log = log_file
                try:
                    self._file_pos = os.path.getsize(log_file)
                except OSError:
                    self._file_pos = 0
                print(f"[ChatRelay] Now tailing: {log_file}")
                return

            try:
                file_size = os.path.getsize(log_file)
            except OSError:
                return

            # File was truncated/rotated
            if file_size < self._file_pos:
                self._file_pos = 0

            if file_size <= self._file_pos:
                return

            try:
                with open(log_file, 'r', encoding='utf-8', errors='replace') as f:
                    f.seek(self._file_pos)
                    new_lines = f.readlines()
                    self._file_pos = f.tell()
            except (OSError, IOError):
                return

            channel = self._get_channel()
            if not channel:
                return

            for line in new_lines:
                line = line.strip()

                match = CHAT_LINE_RE.search(line)
                if not match:
                    continue

                chat_type = match.group(1)
                raw_author = match.group(2)
                message_text = match.group(3)

                if chat_type not in RELAY_CHAT_TYPES:
                    continue

                clean_author = strip_rgb_tags(raw_author)

                if not message_text.strip():
                    continue

                msg = self._format_message(chat_type, clean_author, message_text)

                try:
                    await channel.send(msg)
                except discord.HTTPException as e:
                    print(f"[ChatRelay] Discord send error: {e}")

        except Exception as e:
            print(f"[ChatRelay] Tail error: {e}")

    @_tail_chat_log.before_loop
    async def _before_tail(self):
        await self.bot.wait_until_ready()
        while not self.bot.state.server_ready:
            await asyncio.sleep(5)

        # Seek to end of current log so we don't replay history
        log_file = self._find_latest_chat_log()
        if log_file:
            try:
                self._file_pos = os.path.getsize(log_file)
                self._current_log = log_file
                print(f"[ChatRelay] Tailing {log_file} from position {self._file_pos}")
            except OSError:
                self._file_pos = 0

    # ================================================================
    # Discord -> Game: listen for messages in the relay channel
    # ================================================================

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Forward Discord messages from the relay channel to the game."""
        if not self._active:
            return

        # Ignore bot messages and other channels
        if message.author.bot:
            return
        if message.channel.id != self._channel_id:
            return

        # Skip commands
        if message.content.startswith('/') or message.content.startswith('!'):
            return

        if not message.content.strip():
            return

        # Get display name
        display_name = message.author.display_name

        # Send to game via RCON
        try:
            game_msg = f"[Discord] {display_name}: {message.content}"
            # Escape quotes for RCON
            game_msg = game_msg.replace('"', '\\"')
            await self.bot.rcon.send_command(f'servermsg "{game_msg}"')
        except Exception as e:
            print(f"[ChatRelay] RCON relay error: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(ChatRelay(bot))
    print("[ChatRelay] Extension loaded.")

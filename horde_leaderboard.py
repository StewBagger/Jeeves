"""
Horde Night Leaderboard Extension

Maintains a single auto-updating Discord embed in a dedicated channel showing
players ranked by their horde night survivor multiplier (highest first).

Refreshes once per minute by reading the jeeves_horde_survivors.lua bridge file.

Config:
  HORDE_LEADERBOARD_CHANNEL_ID=  (Discord channel ID for the leaderboard embed)
"""

import os
import datetime
import discord
from discord.ext import commands, tasks

import lua_bridge

# ============================================================================
# Constants
# ============================================================================

ICON_URL = "https://cdn.discordapp.com/attachments/1160323630773842010/1479184189835317430/jeeves_icon_128.png"

# Rank thresholds and display
RANK_TIERS = [
    (50,  "💀", "Undying"),
    (25,  "🔴", "Veteran"),
    (15,  "🟠", "Hardened"),
    (10,  "🟡", "Seasoned"),
    (5,   "🟢", "Survivor"),
    (0,   "⚪", "Rookie"),
]


def _get_rank(survived: int) -> tuple:
    """Return (emoji, title) for a player's survival count."""
    for threshold, emoji, title in RANK_TIERS:
        if survived >= threshold:
            return emoji, title
    return "⚪", "Rookie"


def _build_leaderboard_embed(survivor_data: dict | None, horde_data: dict | None) -> discord.Embed:
    """Build the leaderboard embed from survivor data."""

    embed = discord.Embed(
        color=discord.Color.from_rgb(180, 40, 40),
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    embed.set_author(
        name="🏆 HORDE NIGHT LEADERBOARD",
        icon_url=ICON_URL,
    )

    if not survivor_data:
        embed.description = "*No survivor data available.*\n\nPlayers appear here after surviving their first horde night."
        embed.set_footer(text="Jeeve's Hordes • Updates every 60s")
        return embed

    # Sort by multiplier descending, then by survived descending
    sorted_players = sorted(
        survivor_data.items(),
        key=lambda x: (x[1].get('mult', 0), x[1].get('survived', 0)),
        reverse=True,
    )

    # Filter out zeroed-out entries
    sorted_players = [(name, data) for name, data in sorted_players
                      if data.get('mult', 0) > 0 or data.get('survived', 0) > 0]

    if not sorted_players:
        embed.description = "*No survivors yet.*\n\nPlayers appear here after surviving their first horde night."
        embed.set_footer(text="Jeeve's Hordes • Updates every 60s")
        return embed

    # Build the leaderboard text
    lines = []
    for i, (username, data) in enumerate(sorted_players):
        mult = data.get('mult', 0)
        survived = int(data.get('survived', 0))
        rank_emoji, rank_title = _get_rank(survived)

        # Position medal for top 3
        if i == 0:
            pos = "🥇"
        elif i == 1:
            pos = "🥈"
        elif i == 2:
            pos = "🥉"
        else:
            pos = f"`{i + 1:>2}.`"

        # Format multiplier
        mult_str = f"{mult:.1f}x"

        lines.append(
            f"{pos} {rank_emoji} **{username}** — `{mult_str}` mult • `{survived}` survived • {rank_title}"
        )

    # Split into chunks if needed (Discord embed field limit is 1024 chars)
    leaderboard_text = "\n".join(lines)

    if len(leaderboard_text) <= 4000:
        embed.description = leaderboard_text
    else:
        # Split into multiple fields
        chunk = []
        chunk_len = 0
        field_num = 1
        for line in lines:
            if chunk_len + len(line) + 1 > 1000:
                embed.add_field(
                    name=f"Rankings {'(cont.)' if field_num > 1 else ''}",
                    value="\n".join(chunk),
                    inline=False,
                )
                chunk = []
                chunk_len = 0
                field_num += 1
            chunk.append(line)
            chunk_len += len(line) + 1
        if chunk:
            embed.add_field(
                name=f"Rankings {'(cont.)' if field_num > 1 else ''}",
                value="\n".join(chunk),
                inline=False,
            )

    # Summary stats
    total_players = len(sorted_players)
    total_survivals = sum(int(d.get('survived', 0)) for _, d in sorted_players)
    highest_mult = sorted_players[0][1].get('mult', 0) if sorted_players else 0

    stats_text = (
        f"**{total_players}** survivors • "
        f"**{total_survivals}** total hordes survived • "
        f"**{highest_mult:.1f}x** highest multiplier"
    )

    # Add horde info if available
    if horde_data:
        event_count = horde_data.get('eventCount', 0)
        next_day = horde_data.get('nextHordeDay', 0)
        if event_count:
            stats_text += f"\n🌙 **{event_count}** horde nights completed"
        if next_day:
            # nextHordeDay runs 1 ahead of the in-game display day
            # (same offset as server_status._horde_fields)
            stats_text += f" • Next horde: **Day {next_day - 1}**"

    embed.add_field(name="📊 Stats", value=stats_text, inline=False)

    embed.set_footer(text="Jeeve's Hordes • Updates every 60s")

    return embed


# ============================================================================
# Cog
# ============================================================================

class HordeLeaderboardCog(commands.Cog):

    def __init__(self, bot):
        self.bot = bot
        self._channel_id = int(os.getenv('HORDE_LEADERBOARD_CHANNEL_ID', '0'))
        self._message_id = None
        self._channel = None

        if not self._channel_id:
            print("[HordeLeaderboard] WARNING: HORDE_LEADERBOARD_CHANNEL_ID not set. Leaderboard disabled.")
        else:
            print(f"[HordeLeaderboard] Leaderboard channel: {self._channel_id}")
            self.leaderboard_loop.start()

    def cog_unload(self):
        self.leaderboard_loop.cancel()

    async def _get_channel(self):
        if self._channel:
            return self._channel
        if not self._channel_id:
            return None
        ch = self.bot.get_channel(self._channel_id)
        if not ch:
            try:
                ch = await self.bot.fetch_channel(self._channel_id)
            except (discord.NotFound, discord.Forbidden) as e:
                print(f"[HordeLeaderboard] Could not access channel {self._channel_id}: {e}")
                return None
        self._channel = ch
        return ch

    async def _send_or_edit(self, embed):
        channel = await self._get_channel()
        if not channel:
            return

        # Try to edit existing message
        if self._message_id:
            try:
                msg = await channel.fetch_message(self._message_id)
                await msg.edit(embed=embed)
                return
            except (discord.NotFound, discord.HTTPException):
                self._message_id = None

        # Search for our previous leaderboard message to reuse
        try:
            async for msg in channel.history(limit=20):
                if msg.author == self.bot.user and msg.embeds:
                    for e in msg.embeds:
                        if e.author and e.author.name and "LEADERBOARD" in e.author.name:
                            self._message_id = msg.id
                            await msg.edit(embed=embed)
                            print(f"[HordeLeaderboard] Found existing leaderboard message: {msg.id}")
                            return
        except discord.HTTPException:
            pass

        # Send new
        try:
            msg = await channel.send(embed=embed)
            self._message_id = msg.id
            print(f"[HordeLeaderboard] Created leaderboard message: {msg.id}")
        except discord.HTTPException as e:
            print(f"[HordeLeaderboard] Failed to send leaderboard: {e}")

    @tasks.loop(seconds=60)
    async def leaderboard_loop(self):
        try:
            survivor_data = lua_bridge.read_survivor_data()
            horde_data = lua_bridge.read_horde_status()
            embed = _build_leaderboard_embed(survivor_data, horde_data)
            await self._send_or_edit(embed)
        except Exception as e:
            print(f"[HordeLeaderboard] Error in leaderboard loop: {e}")

    @leaderboard_loop.before_loop
    async def _before_leaderboard(self):
        await self.bot.wait_until_ready()
        import asyncio
        await asyncio.sleep(10)  # Let other cogs initialize first


async def setup(bot):
    await bot.add_cog(HordeLeaderboardCog(bot))

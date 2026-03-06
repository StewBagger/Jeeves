"""
Server Status Dashboard Extension

Maintains a single auto-updating Discord embed in a dedicated channel showing:
  - Server status (Online/Offline)
  - Active player count
  - Next scheduled restart (countdown)
  - In-game time and date with day/night indicator
  - Server age (day count)
  - Weather conditions, wind, and temperature
  - Next horde night (days remaining)

Uses Discord embed text fields for crisp, natively-rendered data with the
Jeeves bunker artwork as branding imagery.

Config:
  STATUS_CHANNEL_ID=  (Discord channel ID for the status embed)
"""

import os
import sys
import datetime
import discord
from discord.ext import commands, tasks

import lua_bridge

# ============================================================================
# Constants
# ============================================================================

ICON_URL = "https://cdn.discordapp.com/attachments/1160323630773842010/1479184189835317430/jeeves_icon_128.png"
IMAGE_URL = "https://cdn.discordapp.com/attachments/1160323630773842010/1479186567758086164/status_bunker_banner_v2.png"

MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December"
]

RESTART_HOURS_UTC = [1, 5, 9, 13, 17, 21]

WEATHER_EMOJI = {
    "Clear": "\u2600\ufe0f",
    "Partly Cloudy": "\u26c5",
    "Overcast": "\u2601\ufe0f",
    "Light Rain": "\U0001f326\ufe0f",
    "Rain": "\U0001f327\ufe0f",
    "Heavy Rain": "\u26c8\ufe0f",
    "Foggy": "\U0001f32b\ufe0f",
    "Snowing": "\u2744\ufe0f",
}

# ============================================================================
# Data helpers
# ============================================================================

def _next_restart_str(skip_active):
    if skip_active:
        return "Skipped"
    now = datetime.datetime.now(datetime.timezone.utc)
    candidates = []
    for h in RESTART_HOURS_UTC:
        t = now.replace(hour=h, minute=0, second=0, microsecond=0)
        if t <= now:
            t += datetime.timedelta(days=1)
        candidates.append(t)
    nxt = min(candidates)
    delta = nxt - now
    total_minutes = int(delta.total_seconds() // 60)
    hours = total_minutes // 60
    minutes = total_minutes % 60
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _format_time(hour, minutes):
    period = "AM" if hour < 12 else "PM"
    display_hour = hour % 12
    if display_hour == 0:
        display_hour = 12
    return f"{display_hour}:{minutes:02d} {period}"


def _temp_f(celsius):
    return f"{celsius * 9 / 5 + 32:.0f}\u00b0F"


# ============================================================================
# Embed builder
# ============================================================================

def build_embed(server_online, world, horde, skip_active):
    """Build the status dashboard embed."""

    if server_online:
        embed = discord.Embed(colour=discord.Colour.green())
    else:
        embed = discord.Embed(colour=discord.Colour.red())

    embed.set_author(name="SERVER STATUS", icon_url=ICON_URL)
    embed.set_thumbnail(url=ICON_URL)
    embed.set_image(url=IMAGE_URL)

    if not server_online:
        embed.add_field(name="\u200b", value="\U0001f534 **Server Offline**", inline=False)
        embed.set_footer(text="Updates every 30 seconds")
        embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
        return embed

    # --- Row 1: Status | Players | Restart ---
    embed.add_field(name="\U0001f4e1 Status", value="\U0001f7e2 Online", inline=True)

    player_count = "0"
    if world and world.get("playerCount") is not None:
        player_count = str(world["playerCount"])
    embed.add_field(name="\u2b50 Players", value=player_count, inline=True)

    restart_str = _next_restart_str(skip_active)
    embed.add_field(name="\u23f0 Restart", value=restart_str, inline=True)

    # --- Row 2: Time | Date | Age ---
    if world:
        hour = world.get("hour", 0)
        mins = world.get("minutes", 0)
        is_night = world.get("isNight", False)
        time_icon = "\U0001f319" if is_night else "\u2600\ufe0f"
        embed.add_field(name=f"{time_icon} Time", value=_format_time(hour, mins), inline=True)

        month = world.get("month", 0)
        day = world.get("day", 1)
        month_name = MONTH_NAMES[month][:3] if 0 <= month < 12 else "???"
        embed.add_field(name="\U0001f4c5 Date", value=f"{month_name} {day}", inline=True)

        age_raw = world.get("worldAgeDays", 0)
        elapsed = world.get("elapsedDays", 0)
        if age_raw >= 1:
            age = int(age_raw + 0.5)
        elif elapsed and elapsed > 0:
            age = int(elapsed)
        else:
            age = max(1, int(age_raw + 0.5))
        embed.add_field(name="\U0001f4c6 Age", value=f"Day {age}", inline=True)
    else:
        embed.add_field(name="\U0001f30d World", value="Waiting...", inline=False)

    # --- Row 3: Weather | Cycle | Horde ---
    if world:
        weather = world.get("weather", "Clear")
        temp = world.get("temperature", 0)
        w_emoji = WEATHER_EMOJI.get(weather, "\u2600\ufe0f")
        embed.add_field(name=f"{w_emoji} Weather", value=f"{weather}, {_temp_f(temp)}", inline=True)

        is_night = world.get("isNight", False)
        if is_night:
            embed.add_field(name="\U0001f319 Cycle", value="Night", inline=True)
        else:
            embed.add_field(name="\u2600\ufe0f Cycle", value="Day", inline=True)
    else:
        embed.add_field(name="\u2601\ufe0f Weather", value="Waiting...", inline=True)
        embed.add_field(name="\u200b", value="\u200b", inline=True)

    # Horde (completes row 3)
    horde_val = _horde_value(horde, world)
    embed.add_field(name="\U0001f9df Horde", value=horde_val, inline=True)

    embed.set_footer(text="Updates every 30 seconds")
    embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
    return embed


def _horde_value(horde, world):
    """Determine horde display value from status file and world data."""
    if not horde:
        return "Waiting..."

    phase = horde.get("phase", "")

    # Active horde
    if phase == "active":
        return "\u26a0\ufe0f **ACTIVE**"

    # Try nextHordeDay first, then eventDay
    next_day = horde.get("nextHordeDay")
    if next_day is None:
        next_day = horde.get("eventDay")

    if next_day is not None and world:
        # Use JE_GetActualDay equivalent — the horde mod uses its own day
        # counter, so compare directly against the value it wrote.
        # The horde mod writes nextHordeDay relative to its own day system,
        # so we just display the raw value from the file.
        if phase == "ended":
            return "Completed"
        if phase == "scheduled":
            return f"Day {next_day}"

    # Fallback for other phases
    if phase == "ended":
        return "Completed"
    if phase == "scheduled":
        return "Scheduled"
    if phase:
        return phase.capitalize()
    return "Scheduled"


# ============================================================================
# Discord Cog
# ============================================================================

class ServerStatusCog(commands.Cog):

    def __init__(self, bot):
        self.bot = bot
        self._channel_id = int(os.getenv('STATUS_CHANNEL_ID', '0'))
        self._message_id = None
        self._channel = None

        if not self._channel_id:
            print("[ServerStatus] WARNING: STATUS_CHANNEL_ID not set. Dashboard disabled.")
        else:
            print(f"[ServerStatus] Dashboard channel: {self._channel_id}")
            self.status_loop.start()

    def cog_unload(self):
        self.status_loop.cancel()

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
                print(f"[ServerStatus] Could not access channel {self._channel_id}: {e}")
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

        # Search for our previous status message to reuse
        try:
            async for msg in channel.history(limit=20):
                if msg.author == self.bot.user and msg.embeds:
                    for e in msg.embeds:
                        if e.author and e.author.name and "SERVER STATUS" in e.author.name:
                            self._message_id = msg.id
                            await msg.edit(embed=embed)
                            print(f"[ServerStatus] Found existing status message: {msg.id}")
                            return
        except discord.HTTPException:
            pass

        # Send new
        try:
            msg = await channel.send(embed=embed)
            self._message_id = msg.id
            print(f"[ServerStatus] Created status message: {msg.id}")
        except discord.HTTPException as e:
            print(f"[ServerStatus] Failed to send status: {e}")

    @tasks.loop(seconds=30)
    async def status_loop(self):
        try:
            server_online = self.bot.state.server_ready and self.bot.rcon.is_server_online()
            world = lua_bridge.read_world_status()
            horde = lua_bridge.read_horde_status()
            skip_active = self.bot.state.skip_next_restart

            embed = build_embed(server_online, world, horde, skip_active)
            await self._send_or_edit(embed)

        except Exception as e:
            print(f"[ServerStatus] Error in status loop: {e}")

    @status_loop.before_loop
    async def _before_status(self):
        await self.bot.wait_until_ready()
        import asyncio
        await asyncio.sleep(5)
        print("[ServerStatus] Dashboard started")


async def setup(bot):
    await bot.add_cog(ServerStatusCog(bot))

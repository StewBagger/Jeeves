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

Resilience features:
  - Grace period: 3 consecutive RCON failures before showing offline
  - Lua file freshness: treats recently-written bridge files as a secondary
    online signal even if RCON is momentarily unresponsive
  - Last-known-good data: retains and displays cached world/horde data during
    brief outages instead of blanking the panel
  - Horde status: shows event count and current day alongside next horde info

Config:
  STATUS_CHANNEL_ID=  (Discord channel ID for the status embed)
"""

import os
import sys
import time
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

# How many consecutive RCON failures before declaring offline
OFFLINE_GRACE_COUNT = 3

# How old (seconds) a Lua bridge file can be and still count as "fresh"
# (i.e., the server was writing data recently even if RCON timed out)
BRIDGE_FRESHNESS_SECONDS = 120

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


def _bridge_file_is_fresh(data, max_age=BRIDGE_FRESHNESS_SECONDS):
    """Check if a Lua bridge dict has a recent timestamp."""
    if not data:
        return False
    ts = data.get("timestamp")
    if ts is None:
        return False
    try:
        age = time.time() - float(ts)
        return age < max_age
    except (TypeError, ValueError):
        return False


# ============================================================================
# Embed builder
# ============================================================================

def build_embed(server_online, world, horde, skip_active, stale=False):
    """Build the status dashboard embed.

    Args:
        server_online: Whether the server is confirmed online.
        world: World data dict (may be cached/stale).
        horde: Horde data dict (may be cached/stale).
        skip_active: Whether next restart is being skipped.
        stale: If True, data is cached from a previous poll (server may be
               temporarily unreachable but we're within the grace period).
    """

    if server_online and not stale:
        embed = discord.Embed(colour=discord.Colour.green())
    elif server_online and stale:
        # Within grace period — show amber/yellow to hint at instability
        embed = discord.Embed(colour=discord.Colour.orange())
    else:
        embed = discord.Embed(colour=discord.Colour.red())

    embed.set_author(name="SERVER STATUS", icon_url=ICON_URL)
    embed.set_thumbnail(url=ICON_URL)
    embed.set_image(url=IMAGE_URL)

    # Fully offline with no cached data at all
    if not server_online and not world:
        embed.add_field(name="\u200b", value="\U0001f534 **Server Offline**", inline=False)
        embed.set_footer(text="Updates every 30 seconds")
        embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
        return embed

    # --- Row 1: Status | Players | Restart ---
    if server_online:
        embed.add_field(name="\U0001f4e1 Status", value="\U0001f7e2 Online", inline=True)
    else:
        embed.add_field(name="\U0001f4e1 Status", value="\U0001f534 Offline", inline=True)

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
        if elapsed and elapsed > 0:
            age = int(elapsed)
        elif age_raw >= 1:
            age = int(age_raw)
        else:
            age = max(1, int(age_raw))
        embed.add_field(name="\U0001f4c6 Age", value=f"Day {age}", inline=True)
    else:
        embed.add_field(name="\U0001f30d World", value="Waiting...", inline=False)

    # --- Row 3: Weather | Wind | Cycle ---
    if world:
        weather = world.get("weather", "Clear")
        temp = world.get("temperature", 0)
        w_emoji = WEATHER_EMOJI.get(weather, "\u2600\ufe0f")
        embed.add_field(name=f"{w_emoji} Weather", value=f"{weather}, {_temp_f(temp)}", inline=True)

        wind_speed = world.get("windSpeed", 0)
        if wind_speed > 0.6:
            wind_desc = "Strong"
        elif wind_speed > 0.3:
            wind_desc = "Moderate"
        elif wind_speed > 0.05:
            wind_desc = "Light"
        else:
            wind_desc = "Calm"
        embed.add_field(name="\U0001f4a8 Wind", value=wind_desc, inline=True)

        is_night = world.get("isNight", False)
        if is_night:
            embed.add_field(name="\U0001f319 Cycle", value="Night", inline=True)
        else:
            embed.add_field(name="\u2600\ufe0f Cycle", value="Day", inline=True)
    else:
        embed.add_field(name="\u2601\ufe0f Weather", value="Waiting...", inline=True)
        embed.add_field(name="\u200b", value="\u200b", inline=True)
        embed.add_field(name="\u200b", value="\u200b", inline=True)

    # --- Row 4: Horde Day | Horde Status | Completed ---
    horde_day, horde_status, horde_completed = _horde_fields(horde)
    embed.add_field(name="\U0001f31a Horde", value=horde_day, inline=True)
    embed.add_field(name="\U0001f9df Status", value=horde_status, inline=True)
    embed.add_field(name="\U0001f3c6 Completed", value=horde_completed, inline=True)

    footer = "Updates every 30 seconds"
    if stale:
        footer = "Updates every 30 seconds \u2022 Data may be stale"
    embed.set_footer(text=footer)
    embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
    return embed


def _horde_fields(horde):
    """Return (horde_day, horde_status, completed) for three inline fields."""
    if not horde:
        return ("—", "Idle", "0")

    phase = horde.get("phase", "")
    event_count = horde.get("eventCount")
    next_day = horde.get("nextHordeDay")

    # Horde day — the Lua scheduler's day counter runs 1 ahead of what
    # players see in-game. Subtract 1 to match the in-game display.
    if next_day is not None:
        horde_day = f"Day {next_day - 1}"
    else:
        horde_day = "—"

    # Status
    if phase == "active":
        horde_status = "\u26a0\ufe0f **ACTIVE**"
    elif phase == "ended":
        horde_status = "Idle"
    elif phase == "scheduled":
        horde_status = "Scheduled"
    elif phase == "status":
        horde_status = "Idle"
    elif phase:
        horde_status = phase.capitalize()
    else:
        horde_status = "Idle"

    # Completed count
    horde_completed = str(event_count) if event_count is not None else "0"

    return (horde_day, horde_status, horde_completed)


# ============================================================================
# Discord Cog
# ============================================================================

class ServerStatusCog(commands.Cog):

    def __init__(self, bot):
        self.bot = bot
        self._channel_id = int(os.getenv('STATUS_CHANNEL_ID', '0'))
        self._message_id = None
        self._channel = None

        # Grace period state
        self._rcon_fail_count = 0

        # Last known good data (retained across brief outages)
        self._last_world = None
        self._last_horde = None

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
            # --- 1. Probe RCON ---
            rcon_ok = self.bot.state.server_ready and self.bot.rcon.is_server_online()

            # --- 2. Read Lua bridge files ---
            world = lua_bridge.read_world_status()
            horde = lua_bridge.read_horde_status()

            # --- 3. Determine online status with grace period ---
            world_fresh = _bridge_file_is_fresh(world)
            horde_fresh = _bridge_file_is_fresh(horde)
            bridge_fresh = world_fresh or horde_fresh

            if rcon_ok:
                # RCON succeeded — reset fail counter, update cache
                self._rcon_fail_count = 0
                if world:
                    self._last_world = world
                if horde:
                    self._last_horde = horde

                embed = build_embed(True, world or self._last_world,
                                    horde or self._last_horde,
                                    self.bot.state.skip_next_restart, stale=False)
            elif bridge_fresh:
                # RCON failed but bridge files are fresh — server is likely busy
                self._rcon_fail_count += 1
                if world:
                    self._last_world = world
                if horde:
                    self._last_horde = horde

                embed = build_embed(True, world or self._last_world,
                                    horde or self._last_horde,
                                    self.bot.state.skip_next_restart, stale=True)
            elif self._rcon_fail_count < OFFLINE_GRACE_COUNT:
                # RCON failed, bridge stale, but still within grace period
                self._rcon_fail_count += 1

                embed = build_embed(True, self._last_world, self._last_horde,
                                    self.bot.state.skip_next_restart, stale=True)
            else:
                # Fully offline: RCON failed repeatedly, bridge stale
                # Still show last known data rather than blanking
                embed = build_embed(False, self._last_world, self._last_horde,
                                    self.bot.state.skip_next_restart, stale=False)

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

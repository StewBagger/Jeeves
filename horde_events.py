"""
horde_events.py — Discord bot cog for Jeeves Hordes event notifications.

Provides /horde, /hordeoff, /hordestatus, /hordenight, /hordereset, /hordeclear, and /hordechange slash commands
that write commands via the unified lua_bridge module, which the JeevesHordes
server mod reads.

Also runs a background task that polls jeeves_horde_status.lua written by the
server mod, and sends Discord notifications at three key horde phases:
  1) "scheduled" — Horde night announced (7am in-game on the day)
  2) "active"    — Horde night has begun (zombies spawning)
  3) "ended"     — Horde night has concluded (lure phase finished)

Deduplication ensures each phase is only announced once per event day, even
across bot restarts.

All commands that call lua_bridge defer the interaction first, since
write_command may sleep up to 3s waiting for the mod to consume the
previous command file.  Discord interactions expire after 3s, so we
must acknowledge immediately and use followup.send for the actual reply.

Add to Jeeves bot:
  1. Place this file alongside Jeeves.py
  2. Add 'horde_events' to the extensions list in setup_hook()

The lua_bridge module handles all file I/O to Zomboid/Lua/.
The mod checks for the command file approximately every 2 seconds.
"""

import asyncio

import discord
from discord import app_commands
from discord.ext import commands, tasks

import lua_bridge


class JeevesHordesCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Dedup: track (eventDay, phase, timestamp) tuples we've already sent
        self._sent_phases: set[tuple] = set()
        self._last_poller_key = None

    async def cog_load(self):
        self.horde_status_poller.start()

    async def cog_unload(self):
        self.horde_status_poller.cancel()

    # ── helpers ──────────────────────────────────────────────────────────

    def _check_role(self, interaction: discord.Interaction) -> bool:
        """Return True if the user has the required role."""
        role = discord.utils.get(
            interaction.guild.roles, name=self.bot.config.DEFAULT_ROLE
        )
        return role is not None and role in interaction.user.roles

    # ── background poller ───────────────────────────────────────────────

    @tasks.loop(seconds=15)
    async def horde_status_poller(self):
        """Poll jeeves_horde_status.lua and send Discord notifications."""
        try:
            status = lua_bridge.read_horde_status()
            if not status:
                return

            phase = status.get("phase")
            event_day = status.get("eventDay", 0)
            ts = status.get("timestamp", 0)

            if not phase or not event_day:
                return

            # Log only on state changes
            poller_key = (phase, event_day, ts)
            if poller_key != self._last_poller_key:
                print(f"[JeevesHordes] Poller: phase={phase}, eventDay={event_day}, ts={ts}")
                self._last_poller_key = poller_key

            # Dedup: skip if we've already sent this (eventDay, phase, timestamp)
            key = (event_day, phase, ts)
            if key in self._sent_phases:
                return

            channel = self.bot.get_notification_channel()
            if not channel:
                return

            if phase == "scheduled":
                # Don't send Discord notifications for "scheduled" — this fires
                # on every server boot when the horde mod writes its initial status.
                # Players get the in-game moodle on horde day. Only notify Discord
                # for active (horde started) and ended (horde complete) events.
                self._sent_phases.add(key)

            elif phase == "active":
                total = status.get("totalZombies", "?")
                players = status.get("playerCount", "?")
                embed = discord.Embed(
                    title="\U0001f9df Horde Night Is Currently Active!",
                    description=(
                        f"The horde has arrived! **{total}** zombies are "
                        f"descending on **{players}** survivor(s).\n\n"
                        "Stay alive."
                    ),
                    colour=discord.Colour.red()
                )
                await channel.send(embed=embed)
                self._sent_phases.add(key)
                print(f"[JeevesHordes] Notification sent: active, eventDay={event_day}")

            elif phase == "ended":
                next_day = status.get("nextHordeDay")
                desc = "The horde has been defeated. The night is quiet once more."
                if next_day:
                    desc += f"\n\nNext horde night: **Day {next_day - 1}**"
                embed = discord.Embed(
                    title="\u2705 Horde Night Has Ended",
                    description=desc,
                    colour=discord.Colour.green()
                )
                await channel.send(embed=embed)
                self._sent_phases.add(key)
                print(f"[JeevesHordes] Notification sent: ended, eventDay={event_day}")

                # Clean up old dedup entries (keep only recent 5 events)
                if len(self._sent_phases) > 15:
                    sorted_keys = sorted(self._sent_phases, key=lambda k: k[0])
                    self._sent_phases = set(sorted_keys[-15:])

        except Exception as e:
            print(f"[JeevesHordes] Horde status poller error: {e}")

    @horde_status_poller.before_loop
    async def before_horde_poller(self):
        await self.bot.wait_until_ready()
        # Seed sent_phases with any existing status file to suppress stale
        # notifications from before the bot started.
        try:
            status = lua_bridge.read_horde_status()
            if status:
                phase = status.get("phase")
                event_day = status.get("eventDay", 0)
                ts = status.get("timestamp", 0)
                if phase and event_day and phase in ("scheduled", "active", "ended"):
                    key = (event_day, phase, ts)
                    self._sent_phases.add(key)
                    print(f"[JeevesHordes] Suppressed stale horde on startup: phase={phase}, eventDay={event_day}, ts={ts}")
        except Exception:
            pass

    # ── /horde ──────────────────────────────────────────────────────────

    @app_commands.command(
        name="horde",
        description="Trigger a casual zombie horde event (does not affect horde night progression)."
    )
    @app_commands.describe(
        count="Number of zombies per player to spawn"
    )
    async def cmd_horde(
        self,
        interaction: discord.Interaction,
        count: int
    ) -> None:
        if not self._check_role(interaction):
            await interaction.response.send_message(embed=discord.Embed(
                title="Permission Denied",
                description=f"You need the **{self.bot.config.DEFAULT_ROLE}** role to use this command.",
                colour=discord.Colour.red()
            ), ephemeral=True)
            return

        if count < 1:
            await interaction.response.send_message(embed=discord.Embed(
                title="Invalid count",
                description="Zombie count must be at least 1.",
                colour=discord.Colour.red()
            ), ephemeral=True)
            return

        await interaction.response.defer()

        success = await lua_bridge.horde(count)
        if success:
            desc = (
                f"Triggered by **{interaction.user.display_name}**\n"
                f"**{count}** zombies per player (casual — no progression)"
            )

            embed = discord.Embed(
                title="\U0001f9df Horde Event Triggered!",
                description=desc,
                colour=discord.Colour.red()
            )
            await interaction.followup.send(embed=embed)

            channel = self.bot.get_notification_channel()
            if channel and channel.id != interaction.channel_id:
                await channel.send(embed=discord.Embed(
                    title="\U0001f9df Horde Event Triggered!",
                    description=desc,
                    colour=discord.Colour.red()
                ))
        else:
            await interaction.followup.send(embed=discord.Embed(
                title="Failed to trigger horde",
                description="Could not write the command file. Check lua_bridge initialization.",
                colour=discord.Colour.red()
            ), ephemeral=True)

    # ── /hordeoff ───────────────────────────────────────────────────────

    @app_commands.command(
        name="hordeoff",
        description="Stop a currently active horde event."
    )
    async def cmd_hordeoff(self, interaction: discord.Interaction) -> None:
        if not self._check_role(interaction):
            await interaction.response.send_message(embed=discord.Embed(
                title="Permission Denied",
                description=f"You need the **{self.bot.config.DEFAULT_ROLE}** role to use this command.",
                colour=discord.Colour.red()
            ), ephemeral=True)
            return

        await interaction.response.defer()

        success = await lua_bridge.horde_stop()
        if success:
            await interaction.followup.send(embed=discord.Embed(
                title="\U0001f6d1 Horde Event Stopped",
                description=f"Stop requested by **{interaction.user.display_name}**",
                colour=discord.Colour.green()
            ))
        else:
            await interaction.followup.send(embed=discord.Embed(
                title="Failed to stop horde",
                description="Could not write the command file.",
                colour=discord.Colour.red()
            ), ephemeral=True)

    # ── /hordestatus ────────────────────────────────────────────────────

    @app_commands.command(
        name="hordestatus",
        description="Show the current horde event status."
    )
    async def cmd_hordestatus(self, interaction: discord.Interaction) -> None:
        if not self._check_role(interaction):
            await interaction.response.send_message(embed=discord.Embed(
                title="Permission Denied",
                description=f"You need the **{self.bot.config.DEFAULT_ROLE}** role to use this command.",
                colour=discord.Colour.red()
            ), ephemeral=True)
            return

        await interaction.response.defer()

        success = await lua_bridge.horde_status()
        if not success:
            await interaction.followup.send(embed=discord.Embed(
                title="Failed",
                description="Could not write the command file.",
                colour=discord.Colour.red()
            ), ephemeral=True)
            return

        # Poll for the status response (server writes it after processing)
        status = None
        for _ in range(10):
            await asyncio.sleep(1)
            status = lua_bridge.read_horde_status()
            if status and status.get("phase") == "status":
                break
        else:
            status = None

        if not status:
            await interaction.followup.send(embed=discord.Embed(
                title="\U0001f4cb Horde Status",
                description="Server did not respond in time. Check the server logs.",
                colour=discord.Colour.orange()
            ))
            return

        day = status.get("eventDay", "?")
        active = status.get("active", False)
        lure = status.get("lurePhase", False)
        remaining = status.get("remaining", 0)
        spawned = status.get("spawned", 0)
        event_count = status.get("eventCount", 0)
        next_day = status.get("nextHordeDay", 0)
        moodle = status.get("moodleActive", False)

        if active:
            state = "\U0001f534 **ACTIVE** — Horde in progress"
            if remaining > 0:
                state += f"\n\U0001f9df Spawned: **{spawned}** | Remaining: **{remaining}**"
        elif lure:
            state = "\U0001f7e0 **Lure Phase** — Zombies being drawn to players"
        elif moodle:
            state = "\U0001f7e1 **Warning Active** — Horde night approaching tonight"
        else:
            state = "\U0001f7e2 **Idle** — No active horde"

        lines = [
            state,
            "",
            f"\U0001f4c5 Current Day: **{day}**",
            f"\U0001f319 Next Horde Night: **Day {next_day - 1}**" if next_day else "\U0001f319 Next Horde Night: **Not scheduled**",
            f"\U0001f4ca Hordes Completed: **{event_count}**",
        ]

        await interaction.followup.send(embed=discord.Embed(
            title="\U0001f4cb Horde Status",
            description="\n".join(lines),
            colour=discord.Colour.red() if active else discord.Colour.blue()
        ))

    # ── /hordenight ─────────────────────────────────────────────────────

    @app_commands.command(
        name="hordenight",
        description="Simulate a scheduled horde night — shows moodle warning, then triggers horde at nightfall."
    )
    async def cmd_hordenight(self, interaction: discord.Interaction) -> None:
        if not self._check_role(interaction):
            await interaction.response.send_message(embed=discord.Embed(
                title="Permission Denied",
                description=f"You need the **{self.bot.config.DEFAULT_ROLE}** role to use this command.",
                colour=discord.Colour.red()
            ), ephemeral=True)
            return

        await interaction.response.defer()

        success = await lua_bridge.horde_night()
        if success:
            embed = discord.Embed(
                title="\U0001f319 Horde Night Scheduled!",
                description=(
                    f"Triggered by **{interaction.user.display_name}**\n\n"
                    "A \"Horde Night Approaches\" warning moodle is now visible to all players.\n"
                    "The horde will trigger automatically at a random time during tonight's night window."
                ),
                colour=discord.Colour.dark_red()
            )
            await interaction.followup.send(embed=embed)

            channel = self.bot.get_notification_channel()
            if channel and channel.id != interaction.channel_id:
                await channel.send(embed=discord.Embed(
                    title="\U0001f319 Horde Night Scheduled!",
                    description=f"Triggered by **{interaction.user.display_name}** — horde will fire tonight.",
                    colour=discord.Colour.dark_red()
                ))
        else:
            await interaction.followup.send(embed=discord.Embed(
                title="Failed to schedule horde night",
                description="Could not write the command file. Check lua_bridge initialization.",
                colour=discord.Colour.red()
            ), ephemeral=True)


    # ── /hordereset ──────────────────────────────────────────────────────

    @app_commands.command(
        name="hordereset",
        description="Reset horde night progression — event count and all player survivor multipliers."
    )
    async def cmd_hordereset(self, interaction: discord.Interaction) -> None:
        if not self._check_role(interaction):
            await interaction.response.send_message(embed=discord.Embed(
                title="Permission Denied",
                description=f"You need the **{self.bot.config.DEFAULT_ROLE}** role to use this command.",
                colour=discord.Colour.red()
            ), ephemeral=True)
            return

        await interaction.response.defer()

        success = await lua_bridge.horde_reset()
        if success:
            await interaction.followup.send(embed=discord.Embed(
                title="\U0001f504 Horde Progress Reset",
                description=(
                    f"Reset by **{interaction.user.display_name}**\n\n"
                    "Event count set to 0 (base zombie count restored).\n"
                    "All player survivor multipliers cleared.\n"
                    "Next horde night will be treated as the first."
                ),
                colour=discord.Colour.orange()
            ))
        else:
            await interaction.followup.send(embed=discord.Embed(
                title="Failed to reset horde progress",
                description="Could not write the command file. Check lua_bridge initialization.",
                colour=discord.Colour.red()
            ), ephemeral=True)

    # ── /hordeclear ─────────────────────────────────────────────────────

    @app_commands.command(
        name="hordeclear",
        description="Clear player horde data — buffs, immunity, cooldowns, streaks, progression."
    )
    @app_commands.describe(
        username="Target a specific player (leave blank to clear ALL players)"
    )
    async def cmd_hordeclear(self, interaction: discord.Interaction, username: str = None) -> None:
        if not self._check_role(interaction):
            await interaction.response.send_message(embed=discord.Embed(
                title="Permission Denied",
                description=f"You need the **{self.bot.config.DEFAULT_ROLE}** role to use this command.",
                colour=discord.Colour.red()
            ), ephemeral=True)
            return

        await interaction.response.defer()

        success = await lua_bridge.horde_clear(username)
        if success:
            if username:
                await interaction.followup.send(embed=discord.Embed(
                    title="\U0001f9f9 Player Horde Data Cleared",
                    description=(
                        f"Cleared by **{interaction.user.display_name}**\n\n"
                        f"Wiped all horde data for **{username}**:\n"
                        "\u2022 Survivor multiplier & survival count\n"
                        "\u2022 Zombie Stew immunity & cooldown\n"
                        "\u2022 Fragrance buff & cooldown\n"
                        "\u2022 Personal horde & streaks"
                    ),
                    colour=discord.Colour.orange()
                ))
            else:
                await interaction.followup.send(embed=discord.Embed(
                    title="\U0001f9f9 All Player Horde Data Cleared",
                    description=(
                        f"Cleared by **{interaction.user.display_name}**\n\n"
                        "All player data wiped:\n"
                        "\u2022 Survivor multipliers & survival counts\n"
                        "\u2022 Zombie Stew immunity & cooldowns\n"
                        "\u2022 Fragrance buffs & cooldowns\n"
                        "\u2022 Personal hordes & streaks\n\n"
                        "Event count and horde scheduling are unchanged."
                    ),
                    colour=discord.Colour.orange()
                ))
        else:
            await interaction.followup.send(embed=discord.Embed(
                title="Failed to clear player data",
                description="Could not write the command file. Check lua_bridge initialization.",
                colour=discord.Colour.red()
            ), ephemeral=True)

    # ── /hordechange ───────────────────────────────────────────────────

    @app_commands.command(
        name="hordechange",
        description="Change the next scheduled horde to a specific world day."
    )
    @app_commands.describe(
        day="The world day number to schedule the next horde on"
    )
    async def cmd_hordechange(self, interaction: discord.Interaction, day: int) -> None:
        if not self._check_role(interaction):
            await interaction.response.send_message(embed=discord.Embed(
                title="Permission Denied",
                description=f"You need the **{self.bot.config.DEFAULT_ROLE}** role to use this command.",
                colour=discord.Colour.red()
            ), ephemeral=True)
            return

        if day < 1:
            await interaction.response.send_message(embed=discord.Embed(
                title="Invalid Day",
                description="Day must be 1 or greater.",
                colour=discord.Colour.red()
            ), ephemeral=True)
            return

        await interaction.response.defer()

        success = await lua_bridge.horde_change(day + 1)
        if success:
            await interaction.followup.send(embed=discord.Embed(
                title="\U0001f4c5 Horde Day Changed",
                description=(
                    f"Changed by **{interaction.user.display_name}**\n\n"
                    f"Next horde night rescheduled to **day {day}**."
                ),
                colour=discord.Colour.blue()
            ))
        else:
            await interaction.followup.send(embed=discord.Embed(
                title="Failed to change horde day",
                description="Could not write the command file. Check lua_bridge initialization.",
                colour=discord.Colour.red()
            ), ephemeral=True)

    # ── /playerreset ────────────────────────────────────────────────────

    @app_commands.command(
        name="playerreset",
        description="Reset a specific player's horde night survivor multiplier and survival count."
    )
    @app_commands.describe(
        username="The player's in-game username (case-insensitive)"
    )
    async def cmd_playerreset(self, interaction: discord.Interaction, username: str) -> None:
        if not self._check_role(interaction):
            await interaction.response.send_message(embed=discord.Embed(
                title="Permission Denied",
                description=f"You need the **{self.bot.config.DEFAULT_ROLE}** role to use this command.",
                colour=discord.Colour.red()
            ), ephemeral=True)
            return

        await interaction.response.defer()

        # Read current data first to show what was reset
        survivor_data = lua_bridge.read_survivor_data()
        old_mult = 0.0
        old_survived = 0
        if survivor_data:
            key = username.lower()
            for k, v in survivor_data.items():
                if k.lower() == key:
                    old_mult = v.get('mult', 0)
                    old_survived = int(v.get('survived', 0))
                    break

        success = lua_bridge.reset_player_survivor(username)
        if success:
            await interaction.followup.send(embed=discord.Embed(
                title="\U0001f504 Player Progress Reset",
                description=(
                    f"Reset by **{interaction.user.display_name}**\n\n"
                    f"**Player:** {username}\n"
                    f"**Previous multiplier:** {old_mult:.1f}x\n"
                    f"**Previous survivals:** {old_survived}\n\n"
                    "Multiplier and survival count set to 0.\n"
                    "Changes take effect on next server restart or horde event."
                ),
                colour=discord.Colour.orange()
            ))
        else:
            await interaction.followup.send(embed=discord.Embed(
                title="Failed to reset player",
                description=(
                    f"Could not find or reset player **{username}**.\n"
                    "Check that the player name is correct and has participated in at least one horde night."
                ),
                colour=discord.Colour.red()
            ), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(JeevesHordesCog(bot))
    print("[JeevesHordes] Bot cog loaded")

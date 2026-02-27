"""
jeeves_drops.py — Discord bot cog for Jeeve's Drops mod integration.

Provides /airdrop and /airdropstatus slash commands.
Supports crate type selection: military, medical, materials, fooddrink, toolsmelee

Add to Jeeves bot:
  1. Place this file alongside Jeeves.py
  2. Add 'jeeves_drops' to the extensions list in setup_hook()
"""

import asyncio

import discord
from discord import app_commands
from discord.ext import commands, tasks

import lua_bridge

CRATE_TYPES = [
    app_commands.Choice(name="Military",    value="military"),
    app_commands.Choice(name="Medical",     value="medical"),
    app_commands.Choice(name="Materials",   value="materials"),
    app_commands.Choice(name="Food/Drink",  value="fooddrink"),
    app_commands.Choice(name="Tools/Melee", value="toolsmelee"),
]

CRATE_EMOJIS = {
    "Military":    "🔫",
    "Medical":     "🏥",
    "Materials":   "🧱",
    "Food/Drink":  "🍖",
    "Tools/Melee": "🔧",
}


class JeevesDropsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._sent_drops: set[tuple] = set()
        self._last_poller_key = None

    async def cog_load(self):
        self.drops_status_poller.start()

    async def cog_unload(self):
        self.drops_status_poller.cancel()

    def _check_role(self, interaction: discord.Interaction) -> bool:
        role = discord.utils.get(
            interaction.guild.roles, name=self.bot.config.DEFAULT_ROLE
        )
        return role is not None and role in interaction.user.roles

    # ── background poller ───────────────────────────────────────────────

    @tasks.loop(seconds=10)
    async def drops_status_poller(self):
        try:
            status = lua_bridge.read_drops_status()
            if not status:
                return

            phase = status.get("phase")
            if not phase:
                return

            ts = status.get("timestamp", 0)
            drop_id = status.get("dropId", "?")
            poller_key = (phase, drop_id, ts)
            if poller_key != self._last_poller_key:
                print(f"[JeevesDrops] Poller: phase={phase}, dropId={drop_id}, ts={ts}")
                self._last_poller_key = poller_key

            if phase == "dropped":
                drop_id = status.get("dropId", 0)
                drop_key = (drop_id, ts)
                if drop_key in self._sent_drops:
                    return

                target = status.get("targetPlayer", "Unknown")
                x = status.get("x", "?")
                y = status.get("y", "?")
                item_count = status.get("itemCount", "?")
                source = status.get("source", "auto")
                crate_label = status.get("crateLabel", "Supply")

                channel = self.bot.get_notification_channel()
                if not channel:
                    print("[JeevesDrops] No notification channel found, skipping drop notification")
                    return

                source_label = "🤖 Bot" if source == "bot" else "🎲 Random"
                emoji = CRATE_EMOJIS.get(crate_label, "📦")

                embed = discord.Embed(
                    title=f"{emoji} {crate_label} Crate Incoming!",
                    description=(
                        f"An air drop has landed near **{target}**!\n\n"
                        f"📍 Location: **{x}, {y}**\n"
                        f"🎁 Items: **{item_count}**\n"
                        f"📦 Type: **{crate_label}**\n"
                        f"Source: {source_label}"
                    ),
                    colour=discord.Colour.blue()
                )
                await channel.send(embed=embed)
                self._sent_drops.add(drop_key)
                print(f"[JeevesDrops] Notification sent for dropId={drop_id}, ts={ts}")

                if len(self._sent_drops) > 50:
                    sorted_keys = sorted(self._sent_drops, key=lambda k: k[1])
                    self._sent_drops = set(sorted_keys[-30:])

            elif phase == "error":
                reason = status.get("reason", "unknown")
                target = status.get("targetPlayer", "")
                crate_type = status.get("crateType", "")
                valid_types = status.get("validTypes", "")
                channel = self.bot.get_notification_channel()
                if channel:
                    desc = f"Air drop failed: **{reason}**"
                    if target:
                        desc += f" (target: {target})"
                    if crate_type:
                        desc += f"\nRequested type: `{crate_type}`"
                    if valid_types:
                        desc += f"\nValid types: `{valid_types}`"
                    await channel.send(embed=discord.Embed(
                        title="⚠️ Air Drop Error",
                        description=desc,
                        colour=discord.Colour.orange()
                    ))

        except Exception as e:
            print(f"[JeevesDrops] Drops status poller error: {e}")

    @drops_status_poller.before_loop
    async def before_drops_poller(self):
        await self.bot.wait_until_ready()
        # Seed sent_drops with any existing status file to suppress stale
        # notifications from before the bot started.
        try:
            status = lua_bridge.read_drops_status()
            if status and status.get("phase") == "dropped":
                drop_id = status.get("dropId", 0)
                ts = status.get("timestamp", 0)
                self._sent_drops.add((drop_id, ts))
                print(f"[JeevesDrops] Suppressed stale drop on startup: dropId={drop_id}, ts={ts}")
        except Exception:
            pass

    # ── /airdrop ────────────────────────────────────────────────────────

    @app_commands.command(
        name="airdrop",
        description="Trigger an air drop on a random or specific player."
    )
    @app_commands.describe(
        player="Target player name (leave empty for random)",
        type="Crate type (leave empty for random)"
    )
    @app_commands.choices(type=CRATE_TYPES)
    async def cmd_airdrop(
        self,
        interaction: discord.Interaction,
        player: str = None,
        type: app_commands.Choice[str] = None
    ) -> None:
        if not self._check_role(interaction):
            await interaction.response.send_message(embed=discord.Embed(
                title="Permission Denied",
                description=f"You need the **{self.bot.config.DEFAULT_ROLE}** role.",
                colour=discord.Colour.red()
            ), ephemeral=True)
            return

        await interaction.response.defer()

        crate_type = type.value if type else None
        crate_label = type.name if type else "Random"

        success = await lua_bridge.airdrop(player, crate_type)
        if success:
            target_str = f"**{player}**" if player else "**Random player**"
            emoji = CRATE_EMOJIS.get(crate_label, "📦")

            desc = (
                f"Triggered by **{interaction.user.display_name}**\n"
                f"🎯 Target: {target_str}\n"
                f"{emoji} Crate: **{crate_label}**"
            )

            embed = discord.Embed(
                title=f"{emoji} Air Drop Triggered!",
                description=desc,
                colour=discord.Colour.blue()
            )
            await interaction.followup.send(embed=embed)

            channel = self.bot.get_notification_channel()
            if channel and channel.id != interaction.channel_id:
                await channel.send(embed=embed)
        else:
            await interaction.followup.send(embed=discord.Embed(
                title="Failed to trigger air drop",
                description="Could not write the command file.",
                colour=discord.Colour.red()
            ), ephemeral=True)

    # ── /airdropstatus ──────────────────────────────────────────────────

    @app_commands.command(
        name="airdropstatus",
        description="Show the current air drop status."
    )
    async def cmd_airdropstatus(self, interaction: discord.Interaction) -> None:
        if not self._check_role(interaction):
            await interaction.response.send_message(embed=discord.Embed(
                title="Permission Denied",
                description=f"You need the **{self.bot.config.DEFAULT_ROLE}** role.",
                colour=discord.Colour.red()
            ), ephemeral=True)
            return

        await interaction.response.defer()

        success = await lua_bridge.airdrop_status()
        if not success:
            await interaction.followup.send(embed=discord.Embed(
                title="Failed",
                description="Could not write the command file.",
                colour=discord.Colour.red()
            ), ephemeral=True)
            return

        await asyncio.sleep(3)

        status = lua_bridge.read_drops_status()
        if not status or status.get("phase") != "status":
            await interaction.followup.send(embed=discord.Embed(
                title="📦 Air Drop Status",
                description="No status data available yet.",
                colour=discord.Colour.greyple()
            ))
            return

        active_count = status.get("activeCount", 0)
        next_hour = status.get("nextDropHour", 0)
        world_hour = status.get("worldHour", 0)
        drops_info = status.get("drops", "")

        hours_until = max(0, next_hour - world_hour) if next_hour > 0 else "?"

        desc = (
            f"**Active drops:** {active_count}\n"
            f"**Next auto-drop in:** {hours_until} hour(s)\n"
            f"**World hour:** {world_hour}"
        )

        if drops_info:
            desc += f"\n\n**Active drop details:**\n`{drops_info}`"

        embed = discord.Embed(
            title="📦 Air Drop Status",
            description=desc,
            colour=discord.Colour.blue()
        )
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(JeevesDropsCog(bot))

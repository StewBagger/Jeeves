"""
jeeves_modsorter.py — Discord bot cog for mod load order analysis.

Provides /modorder, /modsort, and /modinfo slash commands that analyze
the server's mod load order by reading mod.info files directly from the
workshop content folder. No server connection required.

Uses mod_sorter.py for all sorting and category detection logic.

Commands:
  /modorder  — Validate current load order with color-coded status per mod
  /modsort   — Show recommended sort order vs current, with move indicators
  /modinfo   — Get detailed info for a specific mod (category, deps, warnings)

Add to Jeeves bot:
  1. Place this file alongside Jeeves.py
  2. Add 'jeeves_modsorter' to the extensions list in setup_hook()
"""

from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

import mod_sorter


# =========================================================================
# INI helpers
# =========================================================================

def _get_ini_value(ini_path: Path, key: str) -> list[str]:
    """Extract semicolon-separated values for a key from an INI file.
    Strips the b42 leading backslash from Mods= values."""
    try:
        for line in ini_path.read_text(encoding='utf-8').splitlines():
            if line.strip().startswith(f"{key}="):
                raw = line.split("=", 1)[1].strip()
                values = [v.strip() for v in raw.split(";") if v.strip()]
                if key == "Mods":
                    values = [v.lstrip("\\") for v in values]
                return values
    except Exception:
        pass
    return []


def _truncate(text: str, limit: int = 4000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit - 50] + "\n\n*... truncated (too many mods to display)*"


# =========================================================================
# Cog
# =========================================================================

class JeevesModSorterCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _check_role(self, interaction: discord.Interaction) -> bool:
        role = discord.utils.get(
            interaction.guild.roles, name=self.bot.config.DEFAULT_ROLE
        )
        return role is not None and role in interaction.user.roles

    def _ini_path(self) -> Path:
        return Path(self.bot.config.SERVER_INI_PATH)

    def _mods_folder(self) -> Path:
        return Path(self.bot.config.MODS_FOLDER_PATH)

    def _load_state(self) -> tuple[list[str], list[str], dict]:
        """Read INI and build mod cache. Returns (mod_ids, workshop_ids, cache)."""
        ini_path = self._ini_path()
        mods_folder = self._mods_folder()
        mod_ids = _get_ini_value(ini_path, "Mods")
        ws_ids = _get_ini_value(ini_path, "WorkshopItems")
        # Detect map mods from the Map= line
        map_entries = _get_ini_value(ini_path, "Map")
        map_mod_ids = {m for m in mod_ids if m in map_entries}
        cache = mod_sorter.build_mod_cache(mods_folder, mod_ids, ws_ids, map_mod_ids=map_mod_ids)
        return mod_ids, ws_ids, cache

    # ── /modorder ───────────────────────────────────────────────────────

    @app_commands.command(
        name="modorder",
        description="Validate the server's current mod load order."
    )
    async def cmd_modorder(self, interaction: discord.Interaction) -> None:
        if not self._check_role(interaction):
            await interaction.response.send_message(embed=discord.Embed(
                title="Permission Denied",
                description=f"You need the **{self.bot.config.DEFAULT_ROLE}** role.",
                colour=discord.Colour.red()
            ), ephemeral=True)
            return

        await interaction.response.defer()

        if not self._ini_path().exists():
            await interaction.followup.send(embed=discord.Embed(
                title="Server INI Not Found",
                colour=discord.Colour.red()
            ), ephemeral=True)
            return

        mod_ids, ws_ids, cache = self._load_state()
        issues = mod_sorter.validate_order(cache, mod_ids)

        lines = []
        for i, mid in enumerate(mod_ids, 1):
            info = cache.get(mid, {})
            cat = info.get("category", "?")

            if mid in issues:
                mi = issues[mid]
                if mi.get("missing"):
                    emoji = "🔴"
                elif mi.get("incompatible"):
                    emoji = "🟣"
                elif mi.get("wrongOrder"):
                    emoji = "🟡"
                else:
                    emoji = "🟢"
            else:
                emoji = "🟢"

            line = f"`{i:>3}.` {emoji} **{mid}** `{cat}`"

            if mid in issues:
                mi = issues[mid]
                details = []
                if mi.get("missing"):
                    details.append(f"missing: {', '.join(mi['missing'])}")
                if mi.get("wrongOrder"):
                    details.append(f"should load after: {', '.join(mi['wrongOrder'])}")
                if mi.get("incompatible"):
                    details.append(f"incompatible with: {', '.join(mi['incompatible'])}")
                if details:
                    line += f"\n      ↳ *{'; '.join(details)}*"
            lines.append(line)

        issue_count = len(issues)
        total = len(mod_ids)

        if issue_count == 0:
            header = f"✅ **All {total} mods are in valid order!**\n\n"
            colour = discord.Colour.green()
        else:
            header = f"⚠️ **{issue_count} issue(s) found** in {total} mods\n\n"
            colour = discord.Colour.orange()

        legend = "🟢 OK  🔴 Missing dep  🟡 Wrong order  🟣 Incompatible\n\n"
        body = _truncate(header + legend + "\n".join(lines))

        await interaction.followup.send(embed=discord.Embed(
            title="📋 Mod Load Order Validation",
            description=body,
            colour=colour
        ))

    # ── /modsort ────────────────────────────────────────────────────────

    @app_commands.command(
        name="modsort",
        description="Show recommended mod sort order vs current order."
    )
    async def cmd_modsort(self, interaction: discord.Interaction) -> None:
        if not self._check_role(interaction):
            await interaction.response.send_message(embed=discord.Embed(
                title="Permission Denied",
                description=f"You need the **{self.bot.config.DEFAULT_ROLE}** role.",
                colour=discord.Colour.red()
            ), ephemeral=True)
            return

        await interaction.response.defer()

        if not self._ini_path().exists():
            await interaction.followup.send(embed=discord.Embed(
                title="Server INI Not Found",
                colour=discord.Colour.red()
            ), ephemeral=True)
            return

        mod_ids, ws_ids, cache = self._load_state()
        # Detect map mods from Map= line
        map_entries = _get_ini_value(self._ini_path(), "Map")
        map_mod_ids = {m for m in mod_ids if m in map_entries}
        sorted_ids, issues, change_count = mod_sorter.sort_ini_mods(
            self._mods_folder(), mod_ids, ws_ids, map_mod_ids=map_mod_ids
        )

        current_positions = {mid: i for i, mid in enumerate(mod_ids)}
        total = len(sorted_ids)

        if change_count == 0:
            body = f"✅ **Current order is already optimal!** ({total} mods)\n\nNo changes recommended."
            colour = discord.Colour.green()
        else:
            body = (
                f"🔄 **{change_count} position change(s)** recommended for {total} mods\n\n"
                "*Use `/modreorder` to apply this sort to the server INI.*\n\n"
            )
            lines = []
            for i, mid in enumerate(sorted_ids):
                cat = cache.get(mid, {}).get("category", "?")
                old_pos = current_positions.get(mid)
                if old_pos is not None and old_pos != i:
                    direction = "⬆️" if old_pos > i else "⬇️"
                    delta = abs(old_pos - i)
                    lines.append(f"`{i+1:>3}.` {direction} **{mid}** `{cat}` *(was #{old_pos+1}, moved {delta})*")
                else:
                    lines.append(f"`{i+1:>3}.` ▪️ **{mid}** `{cat}`")

            body += _truncate("\n".join(lines), 3800)
            colour = discord.Colour.blue()

        await interaction.followup.send(embed=discord.Embed(
            title="🔄 Recommended Mod Sort Order",
            description=body,
            colour=colour
        ))

    # ── /modinfo ────────────────────────────────────────────────────────

    @app_commands.command(
        name="modinfo",
        description="Get detailed load order info for a specific mod."
    )
    @app_commands.describe(
        mod_id="Mod ID or partial name to search for"
    )
    async def cmd_modinfo(
        self,
        interaction: discord.Interaction,
        mod_id: str
    ) -> None:
        if not self._check_role(interaction):
            await interaction.response.send_message(embed=discord.Embed(
                title="Permission Denied",
                description=f"You need the **{self.bot.config.DEFAULT_ROLE}** role.",
                colour=discord.Colour.red()
            ), ephemeral=True)
            return

        await interaction.response.defer()

        if not self._ini_path().exists():
            await interaction.followup.send(embed=discord.Embed(
                title="Server INI Not Found",
                colour=discord.Colour.red()
            ), ephemeral=True)
            return

        mod_ids, ws_ids, cache = self._load_state()

        # Find the mod — exact match, then case-insensitive, then partial name
        info = cache.get(mod_id)
        if not info:
            lower = mod_id.lower()
            for mid, mi in cache.items():
                if mid.lower() == lower:
                    info = mi
                    break
                if mi.get("name") and lower in mi["name"].lower():
                    info = mi
                    break

        if not info:
            await interaction.followup.send(embed=discord.Embed(
                title="🔍 Mod Info",
                description=f"❌ Mod not found: `{mod_id}`",
                colour=discord.Colour.red()
            ))
            return

        # Validate to get warnings
        issues = mod_sorter.validate_order(cache, mod_ids)

        actual_id = info["id"]
        name = info.get("name") or actual_id
        category = info.get("category", "unknown")
        tags = ", ".join(info.get("tags", []))
        requirements = ", ".join(info.get("require", []))
        load_after = ", ".join(info.get("loadAfter", []))
        load_before = ", ".join(info.get("loadBefore", []))
        incompatible = ", ".join(info.get("incompatibleMods", []))
        ws_id = info.get("_workshop_id", "")
        unresolved = info.get("_unresolved", False)

        lines = [f"**{name}**\n`{actual_id}`"]
        if ws_id:
            lines.append(f"🔗 Workshop: `{ws_id}`")
        if unresolved:
            lines.append("⚠️ *Not found on disk — category and dependencies unknown*")
        lines.append("")

        lines.append(f"📂 **Category:** `{category}`")
        if tags:
            lines.append(f"🏷️ **Tags:** {tags}")

        lines.append("")

        if requirements:
            lines.append(f"📦 **Requires:** {requirements}")
        if load_after:
            lines.append(f"⬇️ **Load after:** {load_after}")
        if load_before:
            lines.append(f"⬆️ **Load before:** {load_before}")
        if incompatible:
            lines.append(f"🚫 **Incompatible with:** {incompatible}")

        if info.get("loadFirst"):
            lines.append(f"⏫ **Load first:** `on`")
        if info.get("loadLast"):
            lines.append(f"⏬ **Load last:** `on`")

        # Warnings
        mod_issues = issues.get(actual_id)
        if mod_issues:
            lines.append("\n⚠️ **Current warnings:**")
            if mod_issues.get("missing"):
                lines.append(f"  🔴 Missing/misordered dependencies: `{', '.join(mod_issues['missing'])}`")
            if mod_issues.get("wrongOrder"):
                lines.append(f"  🟡 Should load after: `{', '.join(mod_issues['wrongOrder'])}`")
            if mod_issues.get("incompatible"):
                lines.append(f"  🟣 Incompatible mods loaded: `{', '.join(mod_issues['incompatible'])}`")
            colour = discord.Colour.orange()
        else:
            lines.append("\n✅ No warnings — this mod's position looks correct.")
            colour = discord.Colour.green()

        await interaction.followup.send(embed=discord.Embed(
            title="🔍 Mod Info",
            description="\n".join(lines),
            colour=colour
        ))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(JeevesModSorterCog(bot))
    print("[JeevesModSorter] Bot cog loaded — /modorder, /modsort, /modinfo")

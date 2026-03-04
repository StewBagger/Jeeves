"""
jeeves_modmanager.py — Discord bot cog for server mod list management.

Commands:
  /modadd <input>        — Add a Workshop item, download via SteamCMD, sort,
                           and update the INI (including Map= for map mods)
  /modremove <mod_id>    — Remove a mod ID (and Workshop ID if orphaned)
  /modlist               — Show all mods and Workshop items in the server config
  /modreorder            — Re-sort the entire Mods= line

Accepted /modadd input formats:
  1) Pasted from Workshop page:
     "Workshop ID: 3644794945Mod ID: MaplewoodMap Folder: Maplewood"
     "Workshop ID: 3633899582Mod ID: BetterTowingMod ID: DisableTowing"
  2) Just a numeric Workshop ID (auto-detects after download):
     "3633899582"
  3) Workshop ID + manual mod IDs:
     "3633899582 BetterTowing DisableTowing"

b42 INI format notes:
  - Mods= line uses backslash-prefixed IDs: \\ModA;\\ModB;\\ModC
  - Map= line lists map folders before the base map: MapA;MapB;Muldraugh, KY
  - Map folders for custom maps must appear BEFORE "Muldraugh, KY"
  - All lines use \\r\\n (Windows CRLF) line endings
"""

import asyncio
import os
import re
import shutil
import subprocess
import datetime
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import mod_sorter


# PZ app ID for SteamCMD workshop downloads
_PZ_APP_ID = "108600"


# =========================================================================
# INI helpers (b42 format aware)
# =========================================================================

def _read_ini(ini_path: Path) -> list[str]:
    """Read the server INI as a list of lines, preserving line endings."""
    return ini_path.read_text(encoding='utf-8').splitlines(keepends=True)


def _backup_ini(ini_path: Path) -> Path:
    """Create a timestamped backup of the INI before modifying."""
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = ini_path.with_suffix(f".backup_{ts}.ini")
    shutil.copy2(ini_path, backup)
    return backup


def _get_ini_value(lines: list[str], key: str) -> list[str]:
    """Extract semicolon-separated values for a key from INI lines.
    Strips the b42 leading backslash from Mods= values."""
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(f"{key}="):
            raw = stripped.split("=", 1)[1].strip()
            values = [v.strip() for v in raw.split(";") if v.strip()]
            if key == "Mods":
                values = [v.lstrip("\\") for v in values]
            return values
    return []


def _set_ini_value(lines: list[str], key: str, values: list[str]) -> list[str]:
    """Set a semicolon-separated value for a key in INI lines.
    Adds the b42 leading backslash to Mods= values.
    Preserves CRLF line endings."""
    if key == "Mods":
        formatted = [f"\\{v}" if not v.startswith("\\") else v for v in values]
    else:
        formatted = values
    joined = ";".join(formatted)
    found = False
    new_lines = []
    for line in lines:
        if line.strip().startswith(f"{key}="):
            new_lines.append(f"{key}={joined}\r\n")
            found = True
        else:
            new_lines.append(line)
    if not found:
        new_lines.append(f"{key}={joined}\r\n")
    return new_lines


def _write_ini(ini_path: Path, lines: list[str]) -> None:
    """Write lines back to the INI file."""
    ini_path.write_text("".join(lines), encoding='utf-8')


def _add_map_folders(lines: list[str], folders: list[str]) -> list[str]:
    """Add map folder names to the Map= line, before the base map.
    Map mods must appear before 'Muldraugh, KY' in the Map= value."""
    current_maps = _get_ini_value(lines, "Map")
    if not current_maps:
        current_maps = ["Muldraugh, KY"]

    for folder in folders:
        if folder not in current_maps:
            # Insert before the last entry (which should be "Muldraugh, KY")
            # Find the base map entry
            base_idx = None
            for i, m in enumerate(current_maps):
                if "muldraugh" in m.lower():
                    base_idx = i
                    break
            if base_idx is not None:
                current_maps.insert(base_idx, folder)
            else:
                # No Muldraugh found — just append before end
                current_maps.insert(len(current_maps), folder)

    return _set_ini_value(lines, "Map", current_maps)


def _remove_map_folders(lines: list[str], folders: list[str]) -> list[str]:
    """Remove map folder names from the Map= line."""
    current_maps = _get_ini_value(lines, "Map")
    changed = False
    for folder in folders:
        if folder in current_maps:
            current_maps.remove(folder)
            changed = True
    if changed:
        return _set_ini_value(lines, "Map", current_maps)
    return lines


# =========================================================================
# Workshop folder scanner
# =========================================================================

def _resolve_mod_ids(mods_folder: Path, workshop_id: str) -> list[dict]:
    """Scan a workshop item folder and return all mod IDs found."""
    ws_path = mods_folder / workshop_id
    if not ws_path.is_dir():
        return []

    results = []
    seen_ids = set()

    for mod_info_path in ws_path.rglob("mod.info"):
        mod_id = None
        mod_name = None
        try:
            text = mod_info_path.read_text(encoding='utf-8', errors='replace')
            for line in text.splitlines():
                line = line.strip()
                if line.lower().startswith("id="):
                    mod_id = line.split("=", 1)[1].strip()
                elif line.lower().startswith("name="):
                    mod_name = line.split("=", 1)[1].strip()
        except Exception:
            continue

        if mod_id and mod_id not in seen_ids:
            seen_ids.add(mod_id)
            results.append({"id": mod_id, "name": mod_name or mod_id})

    return results


def _resolve_map_folders(mods_folder: Path, workshop_id: str) -> list[str]:
    """Scan a workshop item folder for map folders (media/maps/*)."""
    ws_path = mods_folder / workshop_id
    if not ws_path.is_dir():
        return []

    folders = set()
    for maps_dir in ws_path.rglob("media/maps"):
        if maps_dir.is_dir():
            for child in maps_dir.iterdir():
                if child.is_dir():
                    folders.add(child.name)
    return sorted(folders)


def _find_workshop_id_for_mod(mods_folder: Path, mod_id: str) -> Optional[str]:
    """Given a mod ID, find the Workshop ID folder it lives in."""
    mod_id_lower = mod_id.lower()
    for ws_dir in mods_folder.iterdir():
        if not ws_dir.is_dir():
            continue
        for mod_info_path in ws_dir.rglob("mod.info"):
            try:
                text = mod_info_path.read_text(encoding='utf-8', errors='replace')
                for line in text.splitlines():
                    if line.strip().lower().startswith("id="):
                        found_id = line.split("=", 1)[1].strip()
                        if found_id.lower() == mod_id_lower:
                            return ws_dir.name
            except Exception:
                continue
    return None


# =========================================================================
# SteamCMD workshop downloader
# =========================================================================

async def _download_workshop_item(steamcmd_path: str, workshop_id: str, install_dir: str = "") -> tuple[bool, str]:
    """Download a single workshop item via SteamCMD.

    Uses +force_install_dir so the workshop content lands in the same
    steamapps tree the PZ server reads from (derived from MODS_FOLDER_PATH).
    """
    if not os.path.isfile(steamcmd_path):
        return False, f"SteamCMD not found at `{steamcmd_path}`"

    cmd = [steamcmd_path]

    # force_install_dir ensures workshop content downloads to the server's
    # steamapps folder, not SteamCMD's own steamapps folder.
    if install_dir:
        cmd += ["+force_install_dir", install_dir]

    cmd += [
        "+login", "anonymous",
        "+workshop_download_item", _PZ_APP_ID, workshop_id, "validate",
        "+quit",
    ]

    print(f"[ModManager] SteamCMD: downloading workshop item {workshop_id}")
    print(f"[ModManager] Install dir: {install_dir or '(default)'}")
    print(f"[ModManager] Running: {' '.join(cmd)}")

    try:
        process = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: subprocess.run(cmd, capture_output=True, text=True, timeout=300),
        )

        stdout = process.stdout or ""
        stderr = process.stderr or ""
        output = stdout + stderr

        failure_markers = ["Error!", "FAILED", "ERROR"]
        output_lower = output.lower()
        failed = any(m.lower() in output_lower for m in failure_markers)
        succeeded = process.returncode == 0 and not failed

        tail = "\n".join(stdout.strip().splitlines()[-15:])
        print(f"[ModManager] SteamCMD exited code={process.returncode}, success={succeeded}")

        return succeeded, tail

    except subprocess.TimeoutExpired:
        return False, "SteamCMD timed out after 5 minutes"
    except Exception as e:
        return False, str(e)


# =========================================================================
# Cog
# =========================================================================

class JeevesModManagerCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._steamcmd_path = getattr(bot.config, 'STEAMCMD_PATH', None) or os.getenv("STEAMCMD_PATH", "")

    def _check_role(self, interaction: discord.Interaction) -> bool:
        role = discord.utils.get(
            interaction.guild.roles, name=self.bot.config.DEFAULT_ROLE
        )
        return role is not None and role in interaction.user.roles

    def _ini_path(self) -> Path:
        return Path(self.bot.config.SERVER_INI_PATH)

    def _mods_folder(self) -> Path:
        return Path(self.bot.config.MODS_FOLDER_PATH)

    def _install_dir(self) -> str:
        """Derive the server install root from MODS_FOLDER_PATH.

        MODS_FOLDER_PATH = .../steamapps/workshop/content/108600
        Install dir      = .../  (4 levels up)

        SteamCMD uses +force_install_dir to download workshop content
        into the correct steamapps tree so it lands where the PZ server
        expects to find it.
        """
        mods = self._mods_folder()
        # Walk up: 108600 → content → workshop → steamapps → install root
        install = mods.parent.parent.parent.parent
        return str(install)

    # ── /modadd ─────────────────────────────────────────────────────────

    @app_commands.command(
        name="modadd",
        description="Add a Workshop item to the server. Downloads, sorts, and updates the INI."
    )
    @app_commands.describe(
        input="Workshop ID + Mod IDs. Paste from Workshop page or enter a Workshop ID."
    )
    async def cmd_modadd(
        self,
        interaction: discord.Interaction,
        input: str,
    ) -> None:
        if not self._check_role(interaction):
            await interaction.response.send_message(embed=discord.Embed(
                title="Permission Denied",
                description=f"You need the **{self.bot.config.DEFAULT_ROLE}** role.",
                colour=discord.Colour.red()
            ), ephemeral=True)
            return

        await interaction.response.defer()

        # ── Parse input ─────────────────────────────────────────────
        raw = input.strip()
        workshop_id = None
        explicit_mod_ids = []
        map_folders = []

        ws_match = re.search(r'Workshop\s*ID\s*:\s*(\d+)', raw, re.IGNORECASE)
        if ws_match:
            workshop_id = ws_match.group(1)
            after_ws = raw[ws_match.end():]
            # Extract Map Folder entries
            map_matches = re.findall(r'Map\s*Folder\s*:\s*(\S+)', after_ws, re.IGNORECASE)
            map_folders = [m.strip() for m in map_matches if m.strip()]
            # Remove Map Folder entries before splitting on Mod ID
            cleaned = re.sub(r'Map\s*Folder\s*:\s*\S+', '', after_ws, flags=re.IGNORECASE)
            parts = re.split(r'Mod\s*ID\s*:\s*', cleaned, flags=re.IGNORECASE)
            explicit_mod_ids = [p.strip() for p in parts if p.strip()]
        else:
            tokens = re.split(r'[;\s]+', raw)
            tokens = [t.strip() for t in tokens if t.strip()]
            if tokens and tokens[0].isdigit():
                workshop_id = tokens[0]
                explicit_mod_ids = tokens[1:]
            else:
                await interaction.followup.send(embed=discord.Embed(
                    title="Could Not Parse Input",
                    description=(
                        "Paste the mod info from the Workshop page, or enter a numeric Workshop ID.\n\n"
                        "**Accepted formats:**\n"
                        "• `Workshop ID: 123Mod ID: MyModMap Folder: MapName`\n"
                        "• `123456` *(auto-detects mod IDs after download)*\n"
                        "• `123456 MyMod OtherMod`"
                    ),
                    colour=discord.Colour.red()
                ), ephemeral=True)
                return

        if not workshop_id or not workshop_id.isdigit():
            await interaction.followup.send(embed=discord.Embed(
                title="Invalid Workshop ID",
                description="Could not find a numeric Workshop ID in the input.",
                colour=discord.Colour.red()
            ), ephemeral=True)
            return

        ini_path = self._ini_path()
        mods_folder = self._mods_folder()

        if not ini_path.exists():
            await interaction.followup.send(embed=discord.Embed(
                title="Server INI Not Found",
                description=f"Could not find `{ini_path}`",
                colour=discord.Colour.red()
            ), ephemeral=True)
            return

        # ── Step 1: Download via SteamCMD ───────────────────────────
        ws_path = mods_folder / workshop_id
        already_downloaded = ws_path.is_dir() and any(ws_path.rglob("mod.info"))

        if not already_downloaded:
            if not self._steamcmd_path:
                await interaction.followup.send(embed=discord.Embed(
                    title="⚠️ SteamCMD Not Configured",
                    description=(
                        "Workshop item is not downloaded and `STEAMCMD_PATH` is not set.\n"
                        "Either download the mod manually, or set `STEAMCMD_PATH` in `config.env`."
                    ),
                    colour=discord.Colour.orange()
                ))
                return

            await interaction.followup.send(embed=discord.Embed(
                title="📥 Downloading Workshop Item...",
                description=f"Fetching `{workshop_id}` via SteamCMD. This may take a moment.",
                colour=discord.Colour.blue()
            ))

            success, output = await _download_workshop_item(
                self._steamcmd_path, workshop_id, install_dir=self._install_dir()
            )
            if not success:
                await interaction.followup.send(embed=discord.Embed(
                    title="❌ SteamCMD Download Failed",
                    description=f"Could not download `{workshop_id}`.\n```\n{output[:1500]}\n```",
                    colour=discord.Colour.red()
                ))
                return

        # ── Step 2: Resolve mod IDs ─────────────────────────────────
        if explicit_mod_ids:
            mods_to_add = [{"id": m, "name": m} for m in explicit_mod_ids]
            folder_mods = {m["id"]: m for m in _resolve_mod_ids(mods_folder, workshop_id)}
            for m in mods_to_add:
                if m["id"] in folder_mods:
                    m["name"] = folder_mods[m["id"]]["name"]
        else:
            mods_to_add = _resolve_mod_ids(mods_folder, workshop_id)
            if not mods_to_add:
                await interaction.followup.send(embed=discord.Embed(
                    title="⚠️ No Mods Found",
                    description=(
                        f"Workshop item `{workshop_id}` has no `mod.info` files.\n"
                        f"Try specifying mod IDs manually:\n"
                        f"`Workshop ID: {workshop_id}Mod ID: YourModId`"
                    ),
                    colour=discord.Colour.orange()
                ))
                return

        # Auto-detect map folders if not explicitly provided
        if not map_folders:
            map_folders = _resolve_map_folders(mods_folder, workshop_id)

        # ── Step 3: Read current INI state ──────────────────────────
        lines = _read_ini(ini_path)
        current_mods = _get_ini_value(lines, "Mods")
        current_ws = _get_ini_value(lines, "WorkshopItems")

        already_present = [m for m in mods_to_add if m["id"] in current_mods]
        to_add = [m for m in mods_to_add if m["id"] not in current_mods]

        if not to_add and workshop_id in current_ws:
            names = ", ".join(f"**{m['name']}**" for m in already_present)
            await interaction.followup.send(embed=discord.Embed(
                title="Already Added",
                description=f"Workshop `{workshop_id}` and mod(s) ({names}) already in config.",
                colour=discord.Colour.greyple()
            ))
            return

        # ── Step 4: Add to lists ────────────────────────────────────
        for m in to_add:
            current_mods.append(m["id"])

        if workshop_id not in current_ws:
            current_ws.append(workshop_id)

        # ── Step 5: Sort the full mod list ──────────────────────────
        # Build set of known map mod IDs from:
        # 1) Mods whose Map Folder was explicitly provided or auto-detected
        # 2) Mods already in the Map= line
        known_map_mod_ids = set()
        if map_folders:
            # The mods being added with map folders are map mods
            for m in mods_to_add:
                known_map_mod_ids.add(m["id"])
        # Also check existing Map= entries — mods whose ID matches a map folder name
        existing_maps = _get_ini_value(lines, "Map")
        for mod_id in current_mods:
            if mod_id in existing_maps:
                known_map_mod_ids.add(mod_id)

        sorted_mods, issues, change_count = mod_sorter.sort_ini_mods(
            mods_folder, current_mods, current_ws, map_mod_ids=known_map_mod_ids
        )

        # ── Step 6: Write sorted order + map folders to INI ─────────
        backup = _backup_ini(ini_path)

        lines = _set_ini_value(lines, "Mods", sorted_mods)
        lines = _set_ini_value(lines, "WorkshopItems", current_ws)

        # Add map folders before Muldraugh
        map_added = []
        if map_folders:
            current_maps = _get_ini_value(lines, "Map")
            new_folders = [f for f in map_folders if f not in current_maps]
            if new_folders:
                lines = _add_map_folders(lines, new_folders)
                map_added = new_folders

        _write_ini(ini_path, lines)

        # ── Build response ──────────────────────────────────────────
        added_names = "\n".join(f"• **{m['name']}** (`{m['id']}`)" for m in to_add)
        already_names = "\n".join(f"• ~~{m['name']}~~ (`{m['id']}`) — already present" for m in already_present)

        desc = f"Workshop item `{workshop_id}` added to server config.\n\n"
        if added_names:
            desc += f"**Added mod(s):**\n{added_names}\n"
        if already_names:
            desc += f"\n{already_names}\n"
        if map_added:
            desc += f"\n🗺️ **Map folder(s) added:** {', '.join(f'`{f}`' for f in map_added)} *(before Muldraugh)*\n"

        if change_count > 0:
            desc += f"\n🔄 **Load order sorted** — {change_count} position(s) adjusted across {len(sorted_mods)} mods."
        else:
            desc += f"\n✅ **Load order** — no adjustments needed ({len(sorted_mods)} mods)."

        new_warnings = []
        for m in to_add:
            if m["id"] in issues:
                mi = issues[m["id"]]
                parts = []
                if mi.get("missing"):
                    parts.append(f"missing deps: {', '.join(mi['missing'])}")
                if mi.get("incompatible"):
                    parts.append(f"incompatible: {', '.join(mi['incompatible'])}")
                if parts:
                    new_warnings.append(f"• **{m['id']}**: {'; '.join(parts)}")
        if new_warnings:
            desc += f"\n\n⚠️ **Warnings:**\n" + "\n".join(new_warnings)

        desc += f"\n\n⚠️ **Server restart required** for changes to take effect."
        desc += f"\n📋 Backup: `{backup.name}`"

        await interaction.followup.send(embed=discord.Embed(
            title="✅ Mod Added & Sorted",
            description=desc,
            colour=discord.Colour.green()
        ))

        print(f"[ModManager] Added workshop={workshop_id}, mods={[m['id'] for m in to_add]}, "
              f"maps={map_added}, sorted={change_count} changes, by {interaction.user.display_name}")

    # ── /modremove ──────────────────────────────────────────────────────

    @app_commands.command(
        name="modremove",
        description="Remove a mod ID from the server config and re-sort."
    )
    @app_commands.describe(
        mod_id="Mod ID to remove from the server",
        keep_workshop="Keep the Workshop item even if no mods from it remain (default: False)"
    )
    async def cmd_modremove(
        self,
        interaction: discord.Interaction,
        mod_id: str,
        keep_workshop: bool = False
    ) -> None:
        if not self._check_role(interaction):
            await interaction.response.send_message(embed=discord.Embed(
                title="Permission Denied",
                description=f"You need the **{self.bot.config.DEFAULT_ROLE}** role.",
                colour=discord.Colour.red()
            ), ephemeral=True)
            return

        await interaction.response.defer()

        ini_path = self._ini_path()
        mods_folder = self._mods_folder()

        if not ini_path.exists():
            await interaction.followup.send(embed=discord.Embed(
                title="Server INI Not Found",
                colour=discord.Colour.red()
            ), ephemeral=True)
            return

        lines = _read_ini(ini_path)
        current_mods = _get_ini_value(lines, "Mods")
        current_ws = _get_ini_value(lines, "WorkshopItems")

        # Case-insensitive match
        if mod_id not in current_mods:
            match = next((m for m in current_mods if m.lower() == mod_id.lower()), None)
            if match:
                mod_id = match
            else:
                await interaction.followup.send(embed=discord.Embed(
                    title="Mod Not Found",
                    description=f"`{mod_id}` is not in the current mod list.",
                    colour=discord.Colour.red()
                ), ephemeral=True)
                return

        backup = _backup_ini(ini_path)
        current_mods.remove(mod_id)

        # Remove Workshop ID if no sibling mods remain
        ws_removed = None
        map_removed = []
        if not keep_workshop:
            workshop_id = _find_workshop_id_for_mod(mods_folder, mod_id)
            if workshop_id and workshop_id in current_ws:
                sibling_mods = _resolve_mod_ids(mods_folder, workshop_id)
                remaining = [m for m in sibling_mods if m["id"] in current_mods]
                if not remaining:
                    current_ws.remove(workshop_id)
                    ws_removed = workshop_id
                    # Also remove map folders for this workshop item
                    map_folders = _resolve_map_folders(mods_folder, workshop_id)
                    if map_folders:
                        lines = _remove_map_folders(lines, map_folders)
                        map_removed = map_folders

        # Re-sort (detect map mods from Map= line)
        existing_maps = _get_ini_value(lines, "Map")
        known_map_mod_ids = {m for m in current_mods if m in existing_maps}
        sorted_mods, issues, change_count = mod_sorter.sort_ini_mods(
            mods_folder, current_mods, current_ws, map_mod_ids=known_map_mod_ids
        )

        lines = _set_ini_value(lines, "Mods", sorted_mods)
        lines = _set_ini_value(lines, "WorkshopItems", current_ws)
        _write_ini(ini_path, lines)

        desc = f"Removed **{mod_id}** from server config.\n"
        if ws_removed:
            desc += f"Workshop item `{ws_removed}` also removed.\n"
        if map_removed:
            desc += f"🗺️ Map folder(s) removed from Map= line: {', '.join(f'`{f}`' for f in map_removed)}\n"
        if change_count > 0:
            desc += f"\n🔄 Load order re-sorted — {change_count} position(s) adjusted."
        desc += f"\n\n⚠️ **Server restart required** for changes to take effect."
        desc += f"\n📋 Backup: `{backup.name}`"

        await interaction.followup.send(embed=discord.Embed(
            title="🗑️ Mod Removed & Re-sorted",
            description=desc,
            colour=discord.Colour.orange()
        ))

        print(f"[ModManager] Removed mod={mod_id}, ws={ws_removed}, maps={map_removed}, by {interaction.user.display_name}")

    # ── /modreorder ─────────────────────────────────────────────────────

    @app_commands.command(
        name="modreorder",
        description="Re-sort the entire mod list in the server INI."
    )
    async def cmd_modreorder(self, interaction: discord.Interaction) -> None:
        if not self._check_role(interaction):
            await interaction.response.send_message(embed=discord.Embed(
                title="Permission Denied",
                description=f"You need the **{self.bot.config.DEFAULT_ROLE}** role.",
                colour=discord.Colour.red()
            ), ephemeral=True)
            return

        await interaction.response.defer()

        ini_path = self._ini_path()
        mods_folder = self._mods_folder()

        if not ini_path.exists():
            await interaction.followup.send(embed=discord.Embed(
                title="Server INI Not Found",
                colour=discord.Colour.red()
            ), ephemeral=True)
            return

        lines = _read_ini(ini_path)
        current_mods = _get_ini_value(lines, "Mods")
        current_ws = _get_ini_value(lines, "WorkshopItems")

        if not current_mods:
            await interaction.followup.send(embed=discord.Embed(
                title="📋 No Mods",
                description="The `Mods=` line is empty.",
                colour=discord.Colour.greyple()
            ))
            return

        # Detect map mods from Map= line
        existing_maps = _get_ini_value(lines, "Map")
        known_map_mod_ids = {m for m in current_mods if m in existing_maps}

        sorted_mods, issues, change_count = mod_sorter.sort_ini_mods(
            mods_folder, current_mods, current_ws, map_mod_ids=known_map_mod_ids
        )

        if change_count == 0 and not issues:
            await interaction.followup.send(embed=discord.Embed(
                title="✅ Load Order Already Optimal",
                description=f"All {len(sorted_mods)} mods are in the correct order.",
                colour=discord.Colour.green()
            ))
            return

        backup = _backup_ini(ini_path)
        lines = _set_ini_value(lines, "Mods", sorted_mods)
        _write_ini(ini_path, lines)

        desc = ""
        if change_count > 0:
            desc += f"🔄 **{change_count} position(s) adjusted** across {len(sorted_mods)} mods.\n"
        else:
            desc += f"No position changes, but warnings found.\n"

        if issues:
            issue_lines = []
            for mid, mi in list(issues.items())[:10]:
                parts = []
                if mi.get("missing"):
                    parts.append(f"missing: {', '.join(mi['missing'])}")
                if mi.get("wrongOrder"):
                    parts.append(f"order: {', '.join(mi['wrongOrder'])}")
                if mi.get("incompatible"):
                    parts.append(f"incompatible: {', '.join(mi['incompatible'])}")
                issue_lines.append(f"• **{mid}**: {'; '.join(parts)}")
            desc += f"\n⚠️ **{len(issues)} warning(s):**\n" + "\n".join(issue_lines)
            if len(issues) > 10:
                desc += f"\n*...and {len(issues) - 10} more*"

        desc += f"\n\n⚠️ **Server restart required** for changes to take effect."
        desc += f"\n📋 Backup: `{backup.name}`"

        await interaction.followup.send(embed=discord.Embed(
            title="🔄 Mod List Re-sorted",
            description=desc,
            colour=discord.Colour.blue()
        ))

        print(f"[ModManager] Reorder: {change_count} changes, {len(issues)} warnings, by {interaction.user.display_name}")

    # ── /modlist ────────────────────────────────────────────────────────

    @app_commands.command(
        name="modlist",
        description="Show all mods and Workshop items in the server config."
    )
    async def cmd_modlist(self, interaction: discord.Interaction) -> None:
        if not self._check_role(interaction):
            await interaction.response.send_message(embed=discord.Embed(
                title="Permission Denied",
                description=f"You need the **{self.bot.config.DEFAULT_ROLE}** role.",
                colour=discord.Colour.red()
            ), ephemeral=True)
            return

        await interaction.response.defer()

        ini_path = self._ini_path()
        mods_folder = self._mods_folder()

        if not ini_path.exists():
            await interaction.followup.send(embed=discord.Embed(
                title="Server INI Not Found",
                colour=discord.Colour.red()
            ), ephemeral=True)
            return

        lines = _read_ini(ini_path)
        current_mods = _get_ini_value(lines, "Mods")
        current_ws = _get_ini_value(lines, "WorkshopItems")
        current_maps = _get_ini_value(lines, "Map")

        # Build workshop → mod mapping
        mod_to_ws: dict[str, str] = {}
        for ws_id in current_ws:
            for m in _resolve_mod_ids(mods_folder, ws_id):
                mod_to_ws[m["id"]] = ws_id

        grouped_lines = []
        seen_mods = set()

        for ws_id in current_ws:
            mods_in_ws = [m for m in current_mods if mod_to_ws.get(m) == ws_id]
            if mods_in_ws:
                mod_list = ", ".join(f"`{m}`" for m in mods_in_ws)
                grouped_lines.append(f"🔗 **{ws_id}** → {mod_list}")
                seen_mods.update(mods_in_ws)
            else:
                grouped_lines.append(f"🔗 **{ws_id}** → *(no matching mods)*")

        ungrouped = [m for m in current_mods if m not in seen_mods]

        desc = f"**{len(current_mods)}** mod(s), **{len(current_ws)}** Workshop item(s)\n"
        desc += f"🗺️ **Map:** `{';'.join(current_maps)}`\n\n"

        if grouped_lines:
            body = "\n".join(grouped_lines)
            if len(body) > 3400:
                body = body[:3400] + "\n\n*... truncated*"
            desc += body

        if ungrouped:
            ug_list = ", ".join(f"`{m}`" for m in ungrouped[:50])
            desc += f"\n\n**Unlinked mods:**\n{ug_list}"
            if len(ungrouped) > 50:
                desc += f"\n*...and {len(ungrouped) - 50} more*"

        await interaction.followup.send(embed=discord.Embed(
            title="📋 Server Mod List",
            description=desc,
            colour=discord.Colour.blue()
        ))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(JeevesModManagerCog(bot))
    print("[JeevesModManager] Bot cog loaded — /modadd, /modremove, /modreorder, /modlist")

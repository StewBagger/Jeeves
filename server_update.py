"""
Server Update Extension for Jeeves Bot

Adds a /update slash command that:
  1. Warns connected players and runs a countdown (if players are online)
  2. Gracefully shuts down the PZ server via RCON save + quit
  3. Launches SteamCMD to update app 380870 (PZ Dedicated Server, unstable beta)
  4. Waits for SteamCMD to finish, relaying progress to Discord
  5. Restarts the PZ server

Config (config.env):
  STEAMCMD_PATH=  (Path to steamcmd.exe, default: C:\\SteamCMD\\steamcmd.exe)
"""

import asyncio
import os
import subprocess
import discord
from discord import app_commands
from discord.ext import commands
from typing import Optional


# SteamCMD default path
_DEFAULT_STEAMCMD = ""

# The full SteamCMD command sequence for updating the PZ dedicated server
_UPDATE_APP_ID = "380870"
_UPDATE_BETA = "unstable"


class ServerUpdateCog(commands.Cog):
    """Handles updating the PZ dedicated server via SteamCMD."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._steamcmd_path: str = os.getenv("STEAMCMD_PATH", _DEFAULT_STEAMCMD)
        self._update_in_progress = False
        print(f"[ServerUpdate] Extension loaded. SteamCMD: {self._steamcmd_path}")

    # ------------------------------------------------------------------ #
    # SteamCMD runner                                                      #
    # ------------------------------------------------------------------ #

    async def _run_steamcmd_update(self, channel: Optional[discord.TextChannel] = None) -> bool:
        """
        Run SteamCMD to update the PZ dedicated server.
        Returns True on success, False on failure.
        Sends progress updates to the given channel if provided.
        """
        if not os.path.isfile(self._steamcmd_path):
            msg = f"SteamCMD not found at `{self._steamcmd_path}`"
            print(f"[ServerUpdate] ERROR: {msg}")
            if channel:
                await channel.send(embed=discord.Embed(
                    title=f"{self.bot.Emojis.ANGRY} Update Failed",
                    description=msg,
                    colour=discord.Colour.red(),
                ))
            return False

        cmd = [
            self._steamcmd_path,
            "+login", "anonymous",
            "+app_update", _UPDATE_APP_ID, "-beta", _UPDATE_BETA, "validate",
            "+quit",
        ]

        if channel:
            await channel.send(embed=discord.Embed(
                title=f"{self.bot.Emojis.JEEVES} SteamCMD Update Started",
                description=(
                    f"Updating app `{_UPDATE_APP_ID}` (beta: `{_UPDATE_BETA}`)…\n"
                    "This may take a few minutes."
                ),
                colour=discord.Colour.blue(),
            ))

        print(f"[ServerUpdate] Running: {' '.join(cmd)}")

        try:
            # Run SteamCMD in a subprocess; use asyncio so we don't block the bot
            process = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=600,  # 10-minute timeout
                ),
            )

            stdout = process.stdout or ""
            stderr = process.stderr or ""

            # SteamCMD returns 0 on success; some versions always return 0,
            # so also check output for common success/failure strings.
            success_markers = ["Success!", "already up to date", "fully installed"]
            failure_markers = ["Error!", "FAILED", "Login Failure"]

            output_lower = stdout.lower() + stderr.lower()
            failed = any(m.lower() in output_lower for m in failure_markers)
            succeeded = process.returncode == 0 and not failed

            # Log last ~30 lines for diagnostics
            tail = "\n".join(stdout.strip().splitlines()[-30:])
            print(f"[ServerUpdate] SteamCMD exited with code {process.returncode}")
            if tail:
                print(f"[ServerUpdate] Output tail:\n{tail}")

            if channel:
                if succeeded:
                    await channel.send(embed=discord.Embed(
                        title=f"{self.bot.Emojis.HAPPY} SteamCMD Update Complete",
                        description="The server files have been updated successfully.",
                        colour=discord.Colour.green(),
                    ))
                else:
                    # Show a snippet of the output so the admin can diagnose
                    snippet = tail[-1500:] if tail else "(no output)"
                    await channel.send(embed=discord.Embed(
                        title=f"{self.bot.Emojis.ANGRY} SteamCMD Update May Have Failed",
                        description=f"Exit code: `{process.returncode}`\n```\n{snippet}\n```",
                        colour=discord.Colour.red(),
                    ))

            return succeeded

        except subprocess.TimeoutExpired:
            msg = "SteamCMD timed out after 10 minutes."
            print(f"[ServerUpdate] ERROR: {msg}")
            if channel:
                await channel.send(embed=discord.Embed(
                    title=f"{self.bot.Emojis.ANGRY} Update Timed Out",
                    description=msg,
                    colour=discord.Colour.red(),
                ))
            return False

        except Exception as e:
            msg = f"SteamCMD error: {e}"
            print(f"[ServerUpdate] ERROR: {msg}")
            if channel:
                await channel.send(embed=discord.Embed(
                    title=f"{self.bot.Emojis.ANGRY} Update Error",
                    description=msg,
                    colour=discord.Colour.red(),
                ))
            return False

    # ------------------------------------------------------------------ #
    # Player countdown (reuses the bot's existing pattern)                 #
    # ------------------------------------------------------------------ #

    async def _update_countdown(self, channel: Optional[discord.TextChannel] = None) -> None:
        """Warn players, wait for them to leave (or count down), then proceed."""
        await self.bot.rcon.broadcast(
            "Server update starting! The server will shut down shortly for a game update."
        )

        await self.bot.poll_players()

        if not self.bot.state.players_online:
            if channel:
                await channel.send(embed=discord.Embed(
                    title=f"{self.bot.Emojis.JEEVES} No players online — updating immediately.",
                    colour=discord.Colour.purple(),
                ))
            return

        # Staged countdown similar to mod_restart_sequence
        stages = [
            ("5 Minutes",  self.bot.Emojis.SPIFFO_EDUCATE, 24),   # 24 × 10s = 240s ≈ 4min
            ("1 Minute",   self.bot.Emojis.SPIFFO_KATANA,   5),   # 5 × 10s  = 50s
            ("10 Seconds", self.bot.Emojis.SPIFFO_STOP,     0),
        ]

        EMPTY_CHECKS = 3
        consecutive_empty = 0

        for label, emoji, checks in stages:
            msg = f"Server update — restarting in {label}. Please disconnect."
            if channel:
                await channel.send(embed=discord.Embed(
                    title=f"{emoji} {msg}", colour=discord.Colour.yellow(),
                ))
            await self.bot.rcon.broadcast(msg)

            for _ in range(checks):
                await asyncio.sleep(10)
                await self.bot.poll_players()
                if not self.bot.state.players_online:
                    consecutive_empty += 1
                    if consecutive_empty >= EMPTY_CHECKS:
                        if channel:
                            await channel.send(embed=discord.Embed(
                                title=f"{self.bot.Emojis.JEEVES} All players disconnected — proceeding with update.",
                                colour=discord.Colour.purple(),
                            ))
                        return
                else:
                    consecutive_empty = 0

        await asyncio.sleep(10)  # final 10-second wait

    # ------------------------------------------------------------------ #
    # Full update sequence                                                 #
    # ------------------------------------------------------------------ #

    async def _full_update_sequence(self, channel: Optional[discord.TextChannel] = None) -> None:
        """Complete update pipeline: countdown → stop → steamcmd → start."""
        self._update_in_progress = True
        try:
            # 1. Countdown / warn players
            await self._update_countdown(channel)

            # 2. Stop the server gracefully
            if channel:
                await channel.send(embed=discord.Embed(
                    title=f"{self.bot.Emojis.JEEVES} Shutting down the server…",
                    colour=discord.Colour.purple(),
                ))
            await self.bot.stop_server()
            await asyncio.sleep(5)

            # 3. Run SteamCMD update
            success = await self._run_steamcmd_update(channel)

            # 4. Start the server back up regardless (even if update "failed",
            #    the admin probably wants the server running)
            if channel:
                await channel.send(embed=discord.Embed(
                    title=f"{self.bot.Emojis.JEEVES} Starting the server…",
                    colour=discord.Colour.purple(),
                ))
            await self.bot.start_server()

            if channel and not success:
                await channel.send(embed=discord.Embed(
                    title=f"{self.bot.Emojis.PANIC} Server is back online, but the update may not have applied cleanly.",
                    description="Check the SteamCMD output above for details.",
                    colour=discord.Colour.yellow(),
                ))

        except Exception as e:
            print(f"[ServerUpdate] Update sequence error: {e}")
            import traceback
            traceback.print_exc()
            if channel:
                await channel.send(embed=discord.Embed(
                    title=f"{self.bot.Emojis.ANGRY} Update sequence failed!",
                    description=str(e),
                    colour=discord.Colour.red(),
                ))
            # Best-effort: try to start the server even after an error
            try:
                await self.bot.start_server()
            except Exception:
                pass
        finally:
            self._update_in_progress = False

    # ------------------------------------------------------------------ #
    # Slash command                                                        #
    # ------------------------------------------------------------------ #

    @app_commands.command(
        name="update",
        description="Update the PZ dedicated server via SteamCMD (stops server, updates, restarts).",
    )
    async def cmd_update(self, interaction: discord.Interaction) -> None:
        # Guard: only one update at a time
        if self._update_in_progress:
            embed = discord.Embed(
                title=f"{self.bot.Emojis.ANGRY} An update is already in progress!",
                description="Please wait for the current update to finish.",
                colour=discord.Colour.red(),
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        # Guard: don't overlap with a mod-update restart
        if self.bot.state.mod_update_running or self.bot.state.is_restarting:
            embed = discord.Embed(
                title=f"{self.bot.Emojis.ANGRY} The server is currently restarting!",
                description="Wait for the current restart to complete before updating.",
                colour=discord.Colour.red(),
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        # Acknowledge immediately
        await interaction.response.send_message(
            embed=discord.Embed(
                title=f"{self.bot.Emojis.JEEVES} Server update initiated by {interaction.user.display_name}.",
                description=(
                    "The server will be shut down, updated via SteamCMD, and restarted.\n"
                    "Progress updates will appear in this channel."
                ),
                colour=discord.Colour.purple(),
            )
        )

        # Run the full sequence as a background task so we don't block
        channel = interaction.channel
        asyncio.create_task(self._full_update_sequence(channel))


async def setup(bot: commands.Bot):
    from Jeeves import require_role, config
    cog = ServerUpdateCog(bot)
    # Apply the Admin role check to the /update command
    cog.cmd_update = require_role(config.DEFAULT_ROLE)(cog.cmd_update)
    await bot.add_cog(cog)

"""
Mod Check Timer Extension
Hourly mod update checks and crash detection.

The crash monitor relies on bot.state.last_rcon_ok, which is written by
the rcon_heartbeat loop below. Previously player_tracker kept this flag
alive by calling poll_players() every 15 s. After switching player_tracker
to file-tail mode it no longer calls poll_players(), so last_rcon_ok was
stuck at False and the crash monitor fired constant false positives.

Fix: a dedicated rcon_heartbeat loop (every 60 s) issues a lightweight
RCON 'players' command and writes last_rcon_ok. The crash_monitor only
acts after CRASH_THRESHOLD consecutive missed heartbeats.

Crash detection logic:
  - rcon_heartbeat runs every 60 s → sets last_rcon_ok True/False
  - crash_monitor runs every 90 s → reads last_rcon_ok
  - On failure: increment consecutive_failures
  - On CRASH_THRESHOLD failures (~7.5 min): check process table
    • Process alive  → heavy load, back off to CRASH_THRESHOLD // 2 and wait
    • Process gone   → confirmed crash, restart
"""

import asyncio
import discord
from discord.ext import commands, tasks


class ModCheckCog(commands.Cog):

    def __init__(self, bot):
        self.bot = bot
        self.consecutive_failures = 0
        self.CRASH_THRESHOLD = 5      # 5 × 90 s ≈ 7.5 min of sustained RCON silence
        self.hourly_mod_check.start()
        self.rcon_heartbeat.start()
        self.crash_monitor.start()

    def cog_unload(self):
        self.hourly_mod_check.cancel()
        self.rcon_heartbeat.cancel()
        self.crash_monitor.cancel()

    # ------------------------------------------------------------------ #
    # Hourly mod check                                                     #
    # ------------------------------------------------------------------ #

    @tasks.loop(hours=1.0)
    async def hourly_mod_check(self):
        if self.bot.state.server_ready:
            await self.bot.handle_mod_updates()

    @hourly_mod_check.before_loop
    async def _before_mod_check(self):
        await self.bot.wait_until_ready()
        await self._wait_for_server("mod check")

    # ------------------------------------------------------------------ #
    # RCON heartbeat — keeps bot.state.last_rcon_ok current               #
    # ------------------------------------------------------------------ #

    @tasks.loop(seconds=60.0)
    async def rcon_heartbeat(self):
        """
        Lightweight RCON ping.  Calls poll_players() which writes
        bot.state.last_rcon_ok True on success, False on failure.
        Skipped while a restart or startup is in progress so we don't
        spam errors during intentional downtime.
        """
        if not self.bot.state.server_ready or self.bot.state.is_restarting or self.bot.state.is_starting:
            return
        await self.bot.poll_players()

    @rcon_heartbeat.before_loop
    async def _before_heartbeat(self):
        await self.bot.wait_until_ready()
        await self._wait_for_server("RCON heartbeat")

    # ------------------------------------------------------------------ #
    # Crash monitor                                                        #
    # ------------------------------------------------------------------ #

    @tasks.loop(seconds=90.0)
    async def crash_monitor(self):
        # Don't run during intentional restarts, startup, or before the server is up
        if not self.bot.state.server_ready or self.bot.state.is_restarting or self.bot.state.is_starting:
            self.consecutive_failures = 0
            return

        if self.bot.state.last_rcon_ok:
            # Heartbeat landed since last check — all good
            self.consecutive_failures = 0
            return

        self.consecutive_failures += 1
        print(
            f"[CrashMonitor] RCON unresponsive "
            f"({self.consecutive_failures}/{self.CRASH_THRESHOLD})"
        )

        if self.consecutive_failures >= self.CRASH_THRESHOLD:
            await self._handle_crash()

    @crash_monitor.before_loop
    async def _before_crash(self):
        await self.bot.wait_until_ready()
        await self._wait_for_server("crash monitor")

    # ------------------------------------------------------------------ #
    # Crash handler                                                        #
    # ------------------------------------------------------------------ #

    async def _handle_crash(self):
        proc = self.bot.config.SERVER_PROCESS_NAME
        from Jeeves import _tasklist
        if proc in _tasklist(f'IMAGENAME eq {proc}'):
            # Process is alive — server is under heavy load, not crashed.
            # Back off to half-threshold so we keep watching but don't
            # immediately re-trigger.
            print(
                "[CrashMonitor] Server process alive — "
                "likely under heavy load, skipping restart."
            )
            self.consecutive_failures = self.CRASH_THRESHOLD // 2
            return

        print("[CrashMonitor] Server process gone — crash confirmed!")
        self.consecutive_failures = 0
        await self.bot.send_notification(
            f"{self.bot.Emojis.PANIC} Server crash detected! Restarting...",
            discord.Colour.red()
        )
        await self.bot.stop_server()
        await asyncio.sleep(5)
        await self.bot.start_server()

    # ------------------------------------------------------------------ #
    # Shared helper                                                        #
    # ------------------------------------------------------------------ #

    async def _wait_for_server(self, label: str):
        while not self.bot.state.server_ready:
            await asyncio.sleep(5)
        print(f"[ModCheckCog] {label} started")


async def setup(bot):
    await bot.add_cog(ModCheckCog(bot))

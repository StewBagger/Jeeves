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
  - crash_monitor runs every 90 s and asks the mod bridge FIRST
    • world status file fresh → the game loop is running, nothing to do
  - only then does it fall back to the heartbeat flag
  - On failure: increment consecutive_failures
  - On CRASH_THRESHOLD failures (~7.5 min): re-check the bridge, then the
    process table
    • Bridge recent   → not a crash, back off
    • Process alive   → heavy load, back off to CRASH_THRESHOLD // 2 and wait
    • Process gone    → confirmed crash, restart

2026-08-30: the monitor no longer depends solely on last_rcon_ok. That flag is
written by rcon_heartbeat, and a discord.py task loop stops silently on an
unhandled exception -- so a dead heartbeat froze the flag and a frozen False
restarted a healthy server every 7.5 minutes indefinitely. Both loops now carry
error handlers that say so out loud and restart themselves, and the monitor
reads the bridge itself rather than trusting another task to have done it.
"""

import asyncio
import discord
from discord.ext import commands, tasks

import lua_bridge

# Kept local rather than imported from Jeeves: that module is __main__ at run
# time, so importing it here at module scope would not resolve.
WORLD_STATUS_MAX_AGE = 90.0    # 3 missed 30 s writes — matches Jeeves.py
CRASH_BRIDGE_GRACE = 180.0     # looser window for the final pre-restart check


class ModCheckCog(commands.Cog):

    def __init__(self, bot):
        self.bot = bot
        self.consecutive_failures = 0
        self.CRASH_THRESHOLD = 5      # 5 × 90 s ≈ 7.5 min of sustained RCON silence
        # RCON dead while the world is demonstrably running: reported, never
        # acted on. Alerted once at ~7.5 min to match CRASH_THRESHOLD, so the
        # operator hears about it on the same timescale the old code would have
        # (wrongly) restarted, and once more when it recovers.
        self.rcon_silent_cycles = 0
        self.RCON_SILENT_ALERT_AT = 5
        # Strong references to in-flight loop restarters. asyncio keeps only weak
        # references to tasks, so a bare create_task() can be garbage collected
        # mid-await and the restart would simply never happen.
        self._restarters: list = []
        self.hourly_mod_check.start()
        self.rcon_heartbeat.start()
        self.crash_monitor.start()

    def cog_unload(self):
        self.hourly_mod_check.cancel()
        self.rcon_heartbeat.cancel()
        self.crash_monitor.cancel()
        # Kill any pending restarter too, or a reload leaves one behind waiting
        # to restart a loop belonging to the cog we just unloaded.
        for task in self._restarters:
            task.cancel()
        self._restarters.clear()

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

        # Ask the mod bridge DIRECTLY rather than trusting the heartbeat's flag.
        #
        # This monitor used to read bot.state.last_rcon_ok and nothing else, so
        # it was only ever as alive as the heartbeat task that wrote it. A
        # discord.py task loop stops silently on an unhandled exception, and a
        # stopped heartbeat froze that flag at its last value: stuck False meant
        # restarting a perfectly healthy server every 7.5 minutes for ever, with
        # the log line each time reporting RCON silence that nobody was
        # listening for any more. Reading the file here gives this loop its own
        # evidence, so one dead task can no longer manufacture a crash.
        #
        # A fresh status file is also the stronger signal: only OnTick writes it,
        # so its freshness proves the game loop is running, where an RCON reply
        # proves only that a socket handler answered.
        status, age = lua_bridge.read_world_status_with_age()
        if status is not None and age is not None and age <= WORLD_STATUS_MAX_AGE:
            self.bot.state.mark_alive('bridge')
            self.consecutive_failures = 0

            # The game loop is fine, so this is NOT a crash and nothing gets
            # restarted. But if RCON is also dead, half the bot is broken --
            # /players, broadcasts, teleport and the restart countdown all run
            # through it -- and the old code would at least have made noise by
            # (wrongly) restarting. Trading a false restart for silent
            # degradation is not an improvement, so this state is reported.
            if not self.bot.state.last_rcon_ok:
                self.rcon_silent_cycles += 1
                if self.rcon_silent_cycles == self.RCON_SILENT_ALERT_AT:
                    minutes = (self.RCON_SILENT_ALERT_AT * 90) // 60
                    await self.bot.send_notification(
                        f"{self.bot.Emojis.SPIFFO_KATANA} RCON is silent but the "
                        f"server is alive",
                        discord.Colour.orange(),
                        description=(
                            f"The mod bridge has been updating normally, so the "
                            f"world is running and **nothing has been restarted**. "
                            f"But RCON has not answered for about {minutes} minutes, "
                            f"so broadcasts, `/players`, `/teleport` and restart "
                            f"countdowns will not work until it recovers.\n\n"
                            f"Check the server's RCON port and password."
                        ),
                    )
            else:
                if self.rcon_silent_cycles >= self.RCON_SILENT_ALERT_AT:
                    await self.bot.send_notification(
                        f"{self.bot.Emojis.HAPPY} RCON is answering again",
                        discord.Colour.green(),
                    )
                self.rcon_silent_cycles = 0
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
        # Last line of defence before restarting a live world: if the mod bridge
        # has written at ANY point recently, the game loop is running and this is
        # not a crash however silent RCON has become.
        #
        # This is deliberately a second, looser look at the same file the monitor
        # already checks. By the time we are here the file was stale on the last
        # pass, and a server that is thrashing may write it late rather than not
        # at all. Restarting a world costs everyone on it, so the check that
        # prevents that is worth doing twice.
        status, age = lua_bridge.read_world_status_with_age()
        if status is not None and age is not None and age <= CRASH_BRIDGE_GRACE:
            print(f"[CrashMonitor] Bridge file is {age:.0f}s old — the game loop "
                  f"is running. Not restarting.")
            self.consecutive_failures = self.CRASH_THRESHOLD // 2
            return

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

    # ------------------------------------------------------------------ #
    # Loop error handlers                                                  #
    # ------------------------------------------------------------------ #
    #
    # A discord.py task loop that raises an unhandled exception STOPS, and does
    # so quietly. Before these handlers existed a dead rcon_heartbeat left
    # bot.state.last_rcon_ok frozen at whatever it last held, and nothing
    # anywhere reported that the loop had gone. Frozen True disabled crash
    # detection permanently; frozen False restarted a healthy server every 7.5
    # minutes. Both look like a bot that is running fine.
    #
    # Restarting the loop is the right response rather than merely logging: the
    # failure these guard against is transient (a dropped socket, a file being
    # rewritten as we read it), and a monitor that gives up on first contact
    # with one is not a monitor.

    async def _loop_failed(self, label: str, loop, error: BaseException) -> None:
        import traceback
        print(f"[ModCheckCog] {label} raised {type(error).__name__}: {error}")
        traceback.print_exception(type(error), error, error.__traceback__)
        try:
            await self.bot.send_notification(
                f"{self.bot.Emojis.ANGRY} {label} stopped and is being restarted",
                discord.Colour.red(),
                description=(
                    "A background monitor raised an error and would otherwise "
                    "have stayed dead silently.\n"
                    f"```{type(error).__name__}: {error}```"
                ),
            )
        except Exception:  # noqa: BLE001 — never let reporting block the restart
            pass

        # Restart from a DETACHED task, not from here.
        #
        # discord.py calls this handler from inside the failing task's own
        # `except` block and re-raises afterwards (tasks/__init__.py: `await
        # self._call_loop_function('error', exc)` then `raise exc`). The task has
        # therefore not finished yet, is_running() still answers True, and
        # calling restart() inline would be cancelling the task we are currently
        # executing inside. Waiting for it to die first, from outside, is the
        # only ordering that is not a race.
        self._restarters.append(
            asyncio.create_task(self._restart_when_dead(label, loop))
        )

    async def _restart_when_dead(self, label: str, loop) -> None:
        for _ in range(60):
            if not loop.is_running():
                break
            await asyncio.sleep(1)
        else:
            print(f"[ModCheckCog] {label} never stopped; not restarting it.")
            return
        loop.restart()
        print(f"[ModCheckCog] {label} restarted.")

    @hourly_mod_check.error
    async def _hourly_mod_check_error(self, error):
        await self._loop_failed("hourly mod check", self.hourly_mod_check, error)

    @rcon_heartbeat.error
    async def _rcon_heartbeat_error(self, error):
        await self._loop_failed("RCON heartbeat", self.rcon_heartbeat, error)

    @crash_monitor.error
    async def _crash_monitor_error(self, error):
        await self._loop_failed("crash monitor", self.crash_monitor, error)


async def setup(bot):
    await bot.add_cog(ModCheckCog(bot))

"""
Auto Restart Extension
Handles scheduled server restarts with countdown notifications.
"""

import asyncio
import datetime
from typing import List

import discord
from discord.ext import commands, tasks

UTC = datetime.timezone.utc


def _schedule(hours: List[int], minute: int, second: int = 0) -> List[datetime.time]:
    return [datetime.time(hour=h, minute=minute, second=second, tzinfo=UTC) for h in hours]


# Restart at 01:00, 05:00, 09:00, 13:00, 17:00, 21:00 UTC
RESTART_HOURS = [1, 5, 9, 13, 17, 21]
# Notifications fire 1 hour before
NOTIFY_HOURS = [0, 4, 8, 12, 16, 20]

# The deferred countdown, as (label, emoji attribute, seconds to sleep after
# announcing). It is written out rather than open-coded so the announcements and
# the sleeps cannot disagree about how long is left: they sum to ten minutes and
# a reader can check that in one glance.
DEFERRED_COUNTDOWN = [
    ("10 Minutes", "SPIFFO_WAVE", 300),
    ("5 Minutes", "SPIFFO_EDUCATE", 240),
    ("1 Minute", "SPIFFO_KATANA", 50),
    ("10 Seconds", "SPIFFO_STOP", 10),
]


class AutoRestartCog(commands.Cog):

    def __init__(self, bot):
        self.bot = bot
        self._horde_defer_active = False
        # Set while a deferred countdown is running. The 5m/1m/10s notifiers are
        # driven by the wall clock, not by the countdown, so without this they
        # would announce times belonging to a restart that has already been
        # displaced hours into the future.
        self._deferred_countdown_active = False
        for task in (self.auto_restart, self.notify_10m, self.notify_5m, self.notify_1m, self.notify_10s):
            task.start()

    def cog_unload(self):
        for task in (self.auto_restart, self.notify_10m, self.notify_5m, self.notify_1m, self.notify_10s):
            task.cancel()

    def _countdown_muted(self) -> bool:
        return (self.bot.state.skip_next_restart
                or self._horde_defer_active
                or self._deferred_countdown_active)

    # ---- Scheduled tasks ----

    @tasks.loop(time=_schedule(RESTART_HOURS, minute=0))
    async def auto_restart(self):
        if self.bot.state.skip_next_restart:
            self.bot.state.skip_next_restart = False
            self._horde_defer_active = False
            await self.bot.send_notification(
                f"{self.bot.Emojis.JEEVES} Scheduled restart was skipped. Next restart will proceed as normal.",
                discord.Colour.yellow()
            )
            print("[AutoRestart] Restart skipped.")
            return

        # Ask again at the moment of the restart, not only at the 10m mark. The
        # flag set by notify_10m is ten real minutes old by the time we get
        # here, and at the server's DayLength ten real minutes is two in-game
        # hours — long enough for a horde to begin inside the countdown.
        blocked, reason = self.bot._is_horde_blocking_restart()
        if blocked and not self._horde_defer_active:
            self._horde_defer_active = True
            print(f"[AutoRestart] Horde guard at restart time: {reason}")
            await self._announce_deferral(reason)

        if self._horde_defer_active:
            cleared = await self._wait_for_horde_to_clear()
            self._horde_defer_active = False
            # The countdown runs either way. Players were told the restart was
            # deferred, so whichever way the wait ended they are owed the ten
            # minutes' warning before the server goes down.
            await self._deferred_countdown(cleared)

        self.bot.state.auto_restart_pending = True
        await self.bot.send_notification(
            f"{self.bot.Emojis.SPIFFO_POP} Automatic Restart Initiated, this may take several minutes...",
            discord.Colour.yellow()
        )
        await self.bot.restart_server()

    @tasks.loop(time=_schedule(NOTIFY_HOURS, minute=50))
    async def notify_10m(self):
        if self.bot.state.skip_next_restart:
            return
        blocked, reason = self.bot._is_horde_blocking_restart()
        if blocked:
            self._horde_defer_active = True
            print(f"[AutoRestart] Horde guard at 10m: {reason}")
            await self._announce_deferral(reason)
            return
        self._horde_defer_active = False
        await self._announce("10 Minutes", self.bot.Emojis.SPIFFO_WAVE)

    @tasks.loop(time=_schedule(NOTIFY_HOURS, minute=55))
    async def notify_5m(self):
        if not self._countdown_muted():
            await self._announce("5 Minutes", self.bot.Emojis.SPIFFO_EDUCATE)

    @tasks.loop(time=_schedule(NOTIFY_HOURS, minute=59))
    async def notify_1m(self):
        if not self._countdown_muted():
            await self._announce("1 Minute", self.bot.Emojis.SPIFFO_KATANA)

    @tasks.loop(time=_schedule(NOTIFY_HOURS, minute=59, second=50))
    async def notify_10s(self):
        if not self._countdown_muted():
            await self._announce("10 Seconds", self.bot.Emojis.SPIFFO_STOP)

    # ---- Horde deferral ----

    async def _announce_deferral(self, reason: str) -> None:
        await self.bot.send_notification(
            f"{self.bot.Emojis.JEEVES} Scheduled restart deferred — {reason}. Will restart after the event.",
            discord.Colour.orange()
        )
        await self.bot.rcon.broadcast(
            "Scheduled restart deferred — horde night. Server will restart after the event."
        )

    async def _wait_for_horde_to_clear(self) -> bool:
        """Poll until the horde guard releases. True if it cleared, False if we
        gave up and the caller should restart anyway.

        The check comes before the first sleep: the flag may have been set ten
        minutes ago and the event may already be over, and the old loop slept
        five minutes before it would even look.

        The timeout is the point of this function. This loop used to be a bare
        `while True`, so a status file stuck mid-event would hold the restart
        task open forever — and because the task is what schedules the next
        restart, every later restart with it, silently. The mod-update path has
        always had a cap; both now read the same one off the bot.
        """
        timeout = self.bot.HORDE_DEFER_TIMEOUT
        poll = self.bot.HORDE_DEFER_POLL
        waited = 0
        while True:
            blocked, reason = self.bot._is_horde_blocking_restart()
            if not blocked:
                print(f"[AutoRestart] Horde cleared after {waited}s, running deferred countdown.")
                return True
            if waited >= timeout:
                hours = timeout // 3600
                print(f"[AutoRestart] Horde wait exceeded {hours} hours ({reason}) - proceeding anyway.")
                await self.bot.send_notification(
                    f"{self.bot.Emojis.JEEVES} Horde wait exceeded {hours} hours — restarting anyway.",
                    discord.Colour.orange()
                )
                return False
            await asyncio.sleep(poll)
            waited += poll

    async def _deferred_countdown(self, cleared: bool = True) -> None:
        """The normal ten-minute countdown, run late because the horde ran late.

        The clock-driven notifiers are muted throughout — see
        `_deferred_countdown_active`.
        """
        self._deferred_countdown_active = True
        try:
            await self.bot.rcon.broadcast(
                "Horde event concluded. Server will restart in 10 minutes!"
                if cleared else
                "Deferred restart proceeding. Server will restart in 10 minutes!"
            )
            for label, emoji_name, wait in DEFERRED_COUNTDOWN:
                await self._announce(label, getattr(self.bot.Emojis, emoji_name))
                await asyncio.sleep(wait)
        finally:
            self._deferred_countdown_active = False

    async def _announce(self, label: str, emoji: str):
        msg = f"Server will automatically restart in {label}!"
        await self.bot.send_notification(f"{emoji} {msg}", discord.Colour.yellow())
        await self.bot.rcon.broadcast(msg)

    # ---- Wait for ready ----

    @auto_restart.before_loop
    async def _wait_auto(self):
        await self.bot.wait_until_ready()

    @notify_10m.before_loop
    async def _wait_10m(self):
        await self.bot.wait_until_ready()

    @notify_5m.before_loop
    async def _wait_5m(self):
        await self.bot.wait_until_ready()

    @notify_1m.before_loop
    async def _wait_1m(self):
        await self.bot.wait_until_ready()

    @notify_10s.before_loop
    async def _wait_10s(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(AutoRestartCog(bot))

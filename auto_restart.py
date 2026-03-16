"""
Auto Restart Extension
Handles scheduled server restarts with countdown notifications.
"""

import datetime
from typing import List
from discord.ext import commands, tasks

UTC = datetime.timezone.utc


def _schedule(hours: List[int], minute: int, second: int = 0) -> List[datetime.time]:
    return [datetime.time(hour=h, minute=minute, second=second, tzinfo=UTC) for h in hours]


# Restart at 01:00, 05:00, 09:00, 13:00, 17:00, 21:00 UTC
RESTART_HOURS = [1, 5, 9, 13, 17, 21]
# Notifications fire 1 hour before
NOTIFY_HOURS = [0, 4, 8, 12, 16, 20]


class AutoRestartCog(commands.Cog):

    def __init__(self, bot):
        self.bot = bot
        self._horde_defer_active = False
        for task in (self.auto_restart, self.notify_10m, self.notify_5m, self.notify_1m, self.notify_10s):
            task.start()

    def cog_unload(self):
        for task in (self.auto_restart, self.notify_10m, self.notify_5m, self.notify_1m, self.notify_10s):
            task.cancel()

    # ---- Scheduled tasks ----

    @tasks.loop(time=_schedule(RESTART_HOURS, minute=0))
    async def auto_restart(self):
        if self.bot.state.skip_next_restart:
            import discord
            self.bot.state.skip_next_restart = False
            await self.bot.send_notification(
                f"{self.bot.Emojis.JEEVES} Scheduled restart was skipped. Next restart will proceed as normal.",
                discord.Colour.yellow()
            )
            print("[AutoRestart] Restart skipped.")
            return

        if self._horde_defer_active:
            import discord, asyncio
            # Already deferred at the 10m mark. Wait for horde to clear.
            print("[AutoRestart] Restart deferred for horde, polling until clear...")
            while True:
                await asyncio.sleep(300)
                blocked, reason = self.bot._is_horde_blocking_restart()
                if not blocked:
                    self._horde_defer_active = False
                    print("[AutoRestart] Horde cleared, running deferred countdown.")
                    await self._announce("10 Minutes", self.bot.Emojis.SPIFFO_WAVE)
                    await self.bot.rcon.broadcast(
                        "Horde event concluded. Server will restart in 10 minutes!"
                    )
                    await asyncio.sleep(300)  # 5 minutes
                    await self._announce("5 Minutes", self.bot.Emojis.SPIFFO_EDUCATE)
                    await asyncio.sleep(240)  # 4 minutes
                    await self._announce("1 Minute", self.bot.Emojis.SPIFFO_KATANA)
                    await asyncio.sleep(50)   # 50 seconds
                    await self._announce("10 Seconds", self.bot.Emojis.SPIFFO_STOP)
                    await asyncio.sleep(10)
                    break

        import discord
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
        # Horde guard: check current AND projected in-game time.
        # With 1 day = 2 hours, 10 real minutes = 2 in-game hours.
        # If a horde is active or the window will be active at restart
        # time, defer the entire countdown.
        blocked, reason = self.bot._is_horde_blocking_restart(real_minutes_ahead=10)
        if blocked:
            import discord
            self._horde_defer_active = True
            print(f"[AutoRestart] Horde guard at 10m: {reason}")
            await self.bot.send_notification(
                f"{self.bot.Emojis.JEEVES} Scheduled restart deferred — {reason}. Will restart after the event.",
                discord.Colour.orange()
            )
            await self.bot.rcon.broadcast(
                "Scheduled restart deferred — horde night approaching. Server will restart after the event."
            )
            return
        self._horde_defer_active = False
        await self._announce("10 Minutes", self.bot.Emojis.SPIFFO_WAVE)

    @tasks.loop(time=_schedule(NOTIFY_HOURS, minute=55))
    async def notify_5m(self):
        if not self.bot.state.skip_next_restart and not self._horde_defer_active:
            await self._announce("5 Minutes", self.bot.Emojis.SPIFFO_EDUCATE)

    @tasks.loop(time=_schedule(NOTIFY_HOURS, minute=59))
    async def notify_1m(self):
        if not self.bot.state.skip_next_restart and not self._horde_defer_active:
            await self._announce("1 Minute", self.bot.Emojis.SPIFFO_KATANA)

    @tasks.loop(time=_schedule(NOTIFY_HOURS, minute=59, second=50))
    async def notify_10s(self):
        if not self.bot.state.skip_next_restart and not self._horde_defer_active:
            await self._announce("10 Seconds", self.bot.Emojis.SPIFFO_STOP)

    async def _announce(self, label: str, emoji: str):
        import discord
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
import discord, datetime
from discord.ext import commands, tasks


utc = datetime.timezone.utc
scheduled_times = [
    datetime.time(hour=1, minute=0, tzinfo=utc),
    datetime.time(hour=5, minute=0, tzinfo=utc),
    datetime.time(hour=9, minute=0, tzinfo=utc),
    datetime.time(hour=13, minute=0, tzinfo=utc),
    datetime.time(hour=17, minute=0, tzinfo=utc),
    datetime.time(hour=21, minute=0, tzinfo=utc)
]
scheduled_times_10m = [
    datetime.time(hour=0, minute=50, second=0, tzinfo=utc),
    datetime.time(hour=4, minute=50, second=0, tzinfo=utc),
    datetime.time(hour=8, minute=50, second=0, tzinfo=utc),
    datetime.time(hour=12, minute=50, second=0, tzinfo=utc),
    datetime.time(hour=16, minute=50, second=0, tzinfo=utc),
    datetime.time(hour=20, minute=50, second=0, tzinfo=utc)
]
scheduled_times_5m = [
    datetime.time(hour=0, minute=55, second=0, tzinfo=utc),
    datetime.time(hour=4, minute=55, second=0, tzinfo=utc),
    datetime.time(hour=8, minute=55, second=0, tzinfo=utc),
    datetime.time(hour=12, minute=55, second=0, tzinfo=utc),
    datetime.time(hour=16, minute=55, second=0, tzinfo=utc),
    datetime.time(hour=20, minute=55, second=0, tzinfo=utc)
]
scheduled_times_1m = [
    datetime.time(hour=0, minute=59, second=0, tzinfo=utc),
    datetime.time(hour=4, minute=59, second=0, tzinfo=utc),
    datetime.time(hour=8, minute=59, second=0, tzinfo=utc),
    datetime.time(hour=12, minute=59, second=0, tzinfo=utc),
    datetime.time(hour=16, minute=59, second=0, tzinfo=utc),
    datetime.time(hour=20, minute=59, second=0, tzinfo=utc)
]
scheduled_times_10s = [
    datetime.time(hour=0, minute=59, second=50, tzinfo=utc),
    datetime.time(hour=4, minute=59, second=50, tzinfo=utc),
    datetime.time(hour=8, minute=59, second=50, tzinfo=utc),
    datetime.time(hour=12, minute=59, second=50, tzinfo=utc),
    datetime.time(hour=16, minute=59, second=50, tzinfo=utc),
    datetime.time(hour=20, minute=59, second=50, tzinfo=utc)
]

class SpecificTimeCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.auto_restart.start()
        self.restart_notification_10m.start()
        self.restart_notification_5m.start()
        self.restart_notification_1m.start()
        self.restart_notification_10s.start()
    def cog_unload(self):
        self.auto_restart.cancel()
        self.restart_notification_10m.cancel()
        self.restart_notification_5m.cancel()
        self.restart_notification_1m.cancel()
        self.restart_notification_10s.cancel()

    @tasks.loop(time=scheduled_times)
    async def auto_restart(self):
        await self.bot.auto_restart_main()
        
    @tasks.loop(time=scheduled_times_10m)
    async def restart_notification_10m(self):
        await self.bot.server_restart_10m()
        
    @tasks.loop(time=scheduled_times_5m)
    async def restart_notification_5m(self):
        await self.bot.server_restart_5m()

    @tasks.loop(time=scheduled_times_1m)
    async def restart_notification_1m(self):
        await self.bot.server_restart_1m()

    @tasks.loop(time=scheduled_times_10s)
    async def restart_notification_10s(self):
        await self.bot.server_restart_10s()        

    @auto_restart.before_loop
    async def before_my_task(self):
        await self.bot.wait_until_ready()
        
async def setup(bot):
    await bot.add_cog(SpecificTimeCog(bot))

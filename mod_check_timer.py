from discord.ext import tasks, commands

class IntervalCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.hourly_mod_check.start()
        self.player_check.start()

    def cog_unload(self):
        self.hourly_mod_check.cancel()
        self.player_check.cancel()

    @tasks.loop(hours=1.0)
    async def hourly_mod_check(self):
        await self.bot.check_mod_main()
        
    @tasks.loop(seconds=15.0)
    async def player_check(self):
        await self.bot.player_check_main()

    @hourly_mod_check.before_loop
    async def before_check(self):
        await self.bot.wait_until_ready()
        
async def setup(bot):
    await bot.add_cog(IntervalCog(bot))
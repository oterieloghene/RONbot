import discord
from discord.ext import commands

import database


class Admin(commands.Cog):
    """Server-level maintenance commands. Admins only -- the bot owner
    always passes the check, since Discord grants the owner implicit
    Administrator."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="resetdatabase")
    @commands.has_permissions(administrator=True)
    async def reset_database(self, ctx: commands.Context):
        """Full factory reset: drop the entire public schema and rebuild it
        from schema.sql + the seed files. Every player row, bank account,
        vehicle, transit card, trip, and pending request is deleted; the
        world (locations, roles, routes, state economy) comes back exactly
        as the seeds define it, so the next player to arrive starts fresh."""
        before = await database.count_players()
        status = await ctx.send("Wiping the database... this takes a few seconds.")
        try:
            await database.reset_database()
        except Exception as e:
            await status.edit(content=f"Reset failed: {e}")
            return
        await status.edit(
            content=f"Database wiped. {before} player(s) removed. "
            "The server is fresh -- the next arrival starts from zero."
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Admin(bot))
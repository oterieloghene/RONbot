import logging

import discord
from discord.ext import commands

import database

logger = logging.getLogger(__name__)


class Admin(commands.Cog):
    """Server-level maintenance commands. Admins only -- the bot owner
    always passes the check, since Discord grants the owner implicit
    Administrator."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @staticmethod
    async def _announce(ctx: commands.Context, content: str):
        """Post a status message if the bot is allowed to in ctx.channel.

        Location channels lock out @everyone (and possibly the bot's role)
        by design, so a wiped database must not be gated on chat access:
        when the send is Forbidden the outcome is logged to stdout (Render
        logs) and the command continues. Returns the message or None."""
        try:
            return await ctx.send(content)
        except discord.Forbidden:
            logger.warning(
                "resetdatabase: bot cannot send in %r (id %s); proceeding "
                "without an in-channel status message",
                ctx.channel.name,
                ctx.channel.id,
            )
            return None

    @commands.command(name="resetdatabase")
    @commands.has_permissions(administrator=True)
    async def reset_database(self, ctx: commands.Context):
        """Full factory reset: drop the entire public schema and rebuild it
        from schema.sql + the seed files. Every player row, bank account,
        vehicle, transit card, trip, and pending request is deleted; the
        world (locations, roles, routes, state economy) comes back exactly
        as the seeds define it, so the next player to arrive starts fresh."""
        before = await database.count_players()
        status = await self._announce(
            ctx, "Wiping the database... this takes a few seconds."
        )
        try:
            await database.reset_database()
        except Exception as e:
            logger.error("resetdatabase failed: %s", e)
            if status is not None:
                await status.edit(content=f"Reset failed: {e}")
            return
        if status is not None:
            await status.edit(
                content=f"Database wiped. {before} player(s) removed. "
                "The server is fresh -- the next arrival starts from zero."
            )
        else:
            logger.info(
                "resetdatabase completed: %d player(s) removed; confirmation "
                "could not be posted in %r",
                before,
                ctx.channel.name,
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(Admin(bot))
import discord
from discord.ext import commands

import config
import database
import discord_utils


class Onboarding(commands.Cog):
    """Destination selection happens through Discord's own onboarding
    Customization Question (Server Settings -> Customize -> Onboarding),
    not anything the bot posts. That question hands a member one of the
    three "{State} Arrival" roles directly.

    This cog just watches for that role showing up on a member and reacts:
    records the arrival in the database and posts the welcome announcement
    in that state's Arrival Terminal channel.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        before_role_names = {r.name for r in before.roles}
        after_role_names = {r.name for r in after.roles}
        new_role_names = after_role_names - before_role_names
        if not new_role_names:
            return

        for state, state_cfg in config.STATES.items():
            if f"{state} Arrival" in new_role_names:
                await self._handle_arrival(after, state, state_cfg)
                break  # a member only picks one destination

    async def _handle_arrival(self, member: discord.Member, state: str, state_cfg: dict):
        # Already arrived somewhere? Don't process again (e.g. role was
        # re-added by a moderator, or onboarding fired twice).
        existing = await database.get_player(member.id)
        if existing and existing["immigration_status"] != "unarrived":
            return

        await database.ensure_player_exists(member.id)
        await database.record_arrival(member.id, state)

        terminal_channel = discord_utils.get_channel(
            member.guild, state, "BORDER & ENTRY", "arrival-terminal"
        )
        if terminal_channel:
            await terminal_channel.send(
                f"{member.mention} has arrived in {state}. Welcome to {state} State."
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(Onboarding(bot))

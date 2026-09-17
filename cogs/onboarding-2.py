import discord
from discord.ext import commands

import config
import database


class Onboarding(commands.Cog):
    """Destination selection happens through Discord's own onboarding
    Customization Question (Server Settings -> Customize -> Onboarding),
    not anything the bot posts. That question hands a member one of the
    three state "arrival_role_id" roles directly.

    This cog just watches for that role showing up on a member and reacts:
    records the arrival in the database and posts the welcome announcement
    in that state's Arrival Terminal channel.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        before_role_ids = {r.id for r in before.roles}
        after_role_ids = {r.id for r in after.roles}
        new_role_ids = after_role_ids - before_role_ids
        if not new_role_ids:
            return

        for state, state_cfg in config.STATES.items():
            arrival_role_id = state_cfg["arrival_role_id"]
            if arrival_role_id and arrival_role_id in new_role_ids:
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

        terminal_channel = member.guild.get_channel(
            state_cfg["arrival_terminal_channel_id"]
        )
        if terminal_channel:
            await terminal_channel.send(
                f"{member.mention} has arrived in {state}. Welcome to {state} State."
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(Onboarding(bot))

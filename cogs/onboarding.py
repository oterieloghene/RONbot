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
    async def on_member_remove(self, member: discord.Member):
        """Discord already strips a departed member's roles for us -- this
        is the DB-side equivalent. Without it, their old players row stays
        e.g. 'immigrated', so on rejoin _handle_arrival below sees
        immigration_status != 'unarrived' and silently skips, and they
        never get a welcome message or get to go through arrival/!name
        again. Resetting here makes a rejoin behave like a new player, and
        frees their NIN number back to the pool immediately (not deferred
        until someone else needs it) along with deleting the now-orphaned
        NIN role itself.

        The departure announcement is posted first, using the state on
        their record before it gets wiped -- a player who never picked a
        destination has no state to announce from, so they're skipped.
        """
        player = await database.get_player(member.id)
        if player and player["current_state"]:
            state = player["current_state"]
            terminal_channel = discord_utils.get_channel(
                member.guild, state, "BORDER & ENTRY", "arrival-terminal"
            )
            if terminal_channel:
                await terminal_channel.send(
                    f"{member.mention} just left the Republic of Nigeria. "
                    f"Goodbye you will be missed."
                )

        old_role_id = await database.reset_player_on_leave(member.id)
        if old_role_id:
            role = member.guild.get_role(old_role_id)
            if role:
                try:
                    await role.delete(reason="Player left the server -- NIN freed for reuse")
                except discord.Forbidden:
                    pass

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

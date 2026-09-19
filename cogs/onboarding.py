import logging

import discord
from discord.ext import commands

import config
import database
import discord_utils

logger = logging.getLogger(__name__)


class Onboarding(commands.Cog):
    """Destination selection happens through Discord's own onboarding
    Customization Question (Server Settings -> Customize -> Onboarding),
    not anything the bot posts. That question hands a member one of the
    three "{State} Arrival" roles directly.

    This cog just watches for that role showing up on a member and reacts:
    records the arrival in the database, posts the welcome announcement in
    that state's Arrival Terminal channel, and grants write access to
    immigration-office and its sublocation refugee-camp (current_location_id
    is set to immigration-office by database.record_arrival -- see
    _grant_immigration_office_write_access below for the Discord-side half
    of that). arrival-terminal stays visible-only until the player travels
    there for real.
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

    @staticmethod
    def _stale_state_roles(guild: discord.Guild, state: str) -> list[discord.Role]:
        """Every state role on a member EXCEPT the fresh "{state} Arrival"
        role that triggered this arrival. That covers leftover "{State}"
        settlement roles and other states' "{State} Arrival" roles from a
        previous life -- the two role names that gate state channels
        (see location_roles_seed.sql). A rejoiner or re-onboarder must not
        keep them, or they'd still see their old state's channels."""
        stale = []
        for other_state in config.STATES:
            for name in (f"{other_state} Arrival", other_state):
                if other_state == state and name == f"{state} Arrival":
                    continue  # the new arrival role -- the flow depends on it
                role = discord_utils.get_role(guild, name)
                if role is not None:
                    stale.append(role)
        return stale

    async def _handle_arrival(self, member: discord.Member, state: str, state_cfg: dict):
        # First life: brand-new player, nothing to clean up. Second life
        # (rejoin/re-onboard): the bot may have missed the leave event,
        # leaving a stale 'named'/'immigrated' row and leftover state roles
        # behind. That stale row is exactly why _handle_arrival used to
        # skip the welcome -- reset it so the rejoiner walks the full
        # arrival/!name/!immigrate path like a new player.
        existing = await database.get_player(member.id)
        if existing and existing["immigration_status"] != "unarrived":
            old_nin_role_id = await database.reset_player_on_leave(member.id)
            if old_nin_role_id:
                nin_role = member.guild.get_role(old_nin_role_id)
                if nin_role:
                    try:
                        await nin_role.delete(
                            reason="Player re-arrived -- stale NIN role freed for reuse"
                        )
                    except discord.Forbidden:
                        pass

        # Strip any state roles still on the member before recording the
        # fresh arrival, so channel access never carries over from a
        # previous state (or a previous life in the same state).
        stale_roles = [
            role
            for role in self._stale_state_roles(member.guild, state)
            if role in member.roles
        ]
        if stale_roles:
            try:
                await member.remove_roles(
                    *stale_roles, reason="Fresh arrival -- clearing stale state roles"
                )
            except discord.Forbidden:
                # Without Manage Roles the bot can't strip them; the role
                # gate stays stale, but the DB record below is still fixed.
                pass

        await database.ensure_player_exists(member.id)
        await database.record_arrival(member.id, state)
        await self._grant_immigration_office_write_access(member, state)

        terminal_channel = discord_utils.get_channel(
            member.guild, state, "BORDER & ENTRY", "arrival-terminal"
        )
        if terminal_channel:
            try:
                await terminal_channel.send(
                    f"{member.mention} has arrived in {state}. Welcome to {state} State."
                )
            except discord.Forbidden:
                # 50013: the bot can't send in the Arrival Terminal (bot not
                # a member / @everyone denied / role order above the bot's).
                # The arrival itself is already recorded in the DB -- log
                # the permission gap to Render's logs instead of crashing
                # the whole on_member_update handler.
                logger.warning(
                    "onboarding: bot cannot send the welcome in the %s "
                    "Arrival Terminal (id %s) -- check the bot's role "
                    "there (Send Messages, not blocked by @everyone)",
                    state,
                    terminal_channel.id,
                )

    async def _grant_immigration_office_write_access(self, member: discord.Member, state: str) -> None:
        """database.record_arrival already points current_location_id at
        immigration-office -- this is the Discord-side half of that: the
        actual send_messages overwrite. Both immigration-office and its
        sublocation refugee-camp become writable together, since writability
        flows downward to sublocations (see permissions.writable_location_ids).
        arrival-terminal is left alone -- visible via the Arrival role, but
        read-only until the player actually travels there.

        Channel-not-found and Forbidden are both logged rather than raised --
        a missing/misconfigured channel shouldn't crash on_member_update, and
        the DB side (current_location_id) is already correct regardless."""
        for channel_name in ("immigration-office", "refugee-camp"):
            channel = discord_utils.get_channel(member.guild, state, "BORDER & ENTRY", channel_name)
            if channel is None:
                logger.warning(
                    "onboarding: couldn't find %s's %s channel to grant write access -- "
                    "check it exists under 'BORDER & ENTRY'",
                    state,
                    channel_name,
                )
                continue
            try:
                await channel.set_permissions(member, view_channel=True, send_messages=True)
            except discord.Forbidden:
                logger.warning(
                    "onboarding: bot lacks Manage Permissions in %s's %s (id %s) -- "
                    "couldn't grant write access on arrival",
                    state,
                    channel_name,
                    channel.id,
                )


async def setup(bot: commands.Bot):
    await bot.add_cog(Onboarding(bot))

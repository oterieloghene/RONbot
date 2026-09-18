import discord
from discord.ext import commands

import config
import database
import discord_utils


def is_immigration_officer():
    async def predicate(ctx: commands.Context) -> bool:
        role = discord_utils.get_role(ctx.guild, config.IMMIGRATION_OFFICER_ROLE_NAME)
        return role is not None and role in ctx.author.roles

    return commands.check(predicate)


class Immigration(commands.Cog):
    """Two-step process, both run by an Immigration Officer in the state's
    Front Desk channel (staff-only -- arrivals can't see it):

      !name @player Full Name
          Names the player, manufactures and grants their "NIN-0001-{code}"
          role, and grants "{State} Indigene". The arrival role is left
          alone -- they can still only see the arrival-flow channels at
          this point.

      !immigrate @player
          Grants the general "{State}" location role (this is what unlocks
          every other state-gated channel) and removes the arrival role.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _state_cfg(self, state: str):
        return config.STATES.get(state)

    def _front_desk(self, guild: discord.Guild, state: str) -> discord.abc.GuildChannel | None:
        return discord_utils.get_channel(guild, state, "BORDER & ENTRY", "front-desk")

    @commands.command(name="name")
    @is_immigration_officer()
    async def name_player(self, ctx: commands.Context, member: discord.Member, *, player_name: str):
        player = await database.get_player(member.id)

        if player is None or player["immigration_status"] == "unarrived":
            await ctx.send(f"{member.mention} hasn't chosen a destination yet -- nothing to name.")
            return

        if player["immigration_status"] in ("named", "immigrated"):
            await ctx.send(
                f"{member.mention} has already been named **{player['player_name']}** "
                f"({player['player_id']})."
            )
            return

        state = player["current_state"]
        state_cfg = self._state_cfg(state)
        if state_cfg is None:
            await ctx.send(f"Unknown state on record: {state}. Check config.py.")
            return

        desk_channel = self._front_desk(ctx.guild, state)
        if desk_channel is None:
            await ctx.send(f"Couldn't find {state}'s Front Desk channel -- check the category name in Discord matches 'BORDER & ENTRY'.")
            return
        if ctx.channel.id != desk_channel.id:
            await ctx.send(f"This has to be run in {state}'s Front Desk channel.")
            return

        nin_number = await database.allocate_nin_number()
        player_id = f"NIN-{nin_number:04d}-{state_cfg['state_code']}"

        # Defensive only: reset_player_on_leave deletes a departed player's
        # NIN role the instant they leave, so this number's old role
        # shouldn't still exist. But if it somehow does (permissions
        # hiccup, manual recreation, etc.), clear it before manufacturing
        # the fresh one so two roles never end up sharing a NIN name.
        stale_role = discord_utils.get_role(ctx.guild, player_id)
        if stale_role:
            try:
                await stale_role.delete(reason="Reassigning this NIN -- clearing stale role first")
            except discord.Forbidden:
                pass

        nin_role = await ctx.guild.create_role(
            name=player_id, reason=f"NIN assigned to {member} by Immigration Officer"
        )
        await member.add_roles(nin_role, reason="Named by Immigration Officer")

        await database.finalize_naming(member.id, player_name, player_id, nin_number, nin_role.id)

        indigene_role = discord_utils.get_role(ctx.guild, state_cfg["indigene_role_name"])
        if indigene_role:
            await member.add_roles(indigene_role, reason="Named by Immigration Officer")

        try:
            await member.edit(nick=player_name)
        except discord.Forbidden:
            pass  # bot role may be below the member's -- not fatal

        await ctx.send(
            f"{member.mention} named **{player_name}** ({player_id}) -- "
            f"now a {state} Indigene. Run !immigrate once ready to give full access."
        )

    @commands.command(name="immigrate")
    @is_immigration_officer()
    async def immigrate(self, ctx: commands.Context, member: discord.Member):
        player = await database.get_player(member.id)

        if player is None or player["immigration_status"] in ("unarrived", "arrived"):
            await ctx.send(f"{member.mention} needs to be named first -- run !name.")
            return

        if player["immigration_status"] == "immigrated":
            await ctx.send(f"{member.mention} has already been immigrated.")
            return

        state = player["current_state"]
        state_cfg = self._state_cfg(state)
        if state_cfg is None:
            await ctx.send(f"Unknown state on record: {state}. Check config.py.")
            return

        desk_channel = self._front_desk(ctx.guild, state)
        if desk_channel is None:
            await ctx.send(f"Couldn't find {state}'s Front Desk channel -- check the category name in Discord matches 'BORDER & ENTRY'.")
            return
        if ctx.channel.id != desk_channel.id:
            await ctx.send(f"This has to be run in {state}'s Front Desk channel.")
            return

        await database.complete_immigration(member.id)

        arrival_role = discord_utils.get_role(ctx.guild, f"{state} Arrival")
        state_role = discord_utils.get_role(ctx.guild, state)

        if arrival_role and arrival_role in member.roles:
            await member.remove_roles(arrival_role, reason="Immigration complete")
        if state_role:
            await member.add_roles(state_role, reason="Immigration complete")

        await ctx.send(
            f"{member.mention} ({player['player_id']}) fully immigrated -- now has full access to {state}."
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Immigration(bot))

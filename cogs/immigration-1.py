import discord
from discord.ext import commands

import config
import database


def is_immigration_officer():
    async def predicate(ctx: commands.Context) -> bool:
        role = ctx.guild.get_role(config.IMMIGRATION_OFFICER_ROLE_ID)
        return role is not None and role in ctx.author.roles

    return commands.check(predicate)


class Immigration(commands.Cog):
    """Two-step process, both run by an Immigration Officer in the state's
    Immigration Office channel:

      !name @player Full Name
          Names the player and grants "{State} Indigine". The arrival role
          is left alone -- they can still only see the arrival-flow
          channels at this point.

      !immigrate @player
          Grants the general "{State}" location role (this is what unlocks
          every other state-gated channel) and removes the arrival role.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _state_cfg(self, state: str):
        return config.STATES.get(state)

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

        if ctx.channel.id != state_cfg["immigration_office_channel_id"]:
            await ctx.send(f"This has to be run in {state}'s Immigration Office channel.")
            return

        player_id = await database.name_player(member.id, player_name)

        indigine_role = ctx.guild.get_role(state_cfg["indigine_role_id"])
        if indigine_role:
            await member.add_roles(indigine_role, reason="Named by Immigration Officer")

        try:
            await member.edit(nick=player_name)
        except discord.Forbidden:
            pass  # bot role may be below the member's -- not fatal

        await ctx.send(
            f"{member.mention} named **{player_name}** ({player_id}) -- "
            f"now a {state} Indigine. Run !immigrate once ready to give full access."
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

        if ctx.channel.id != state_cfg["immigration_office_channel_id"]:
            await ctx.send(f"This has to be run in {state}'s Immigration Office channel.")
            return

        await database.complete_immigration(member.id)

        arrival_role = ctx.guild.get_role(state_cfg["arrival_role_id"])
        state_role = ctx.guild.get_role(state_cfg["state_role_id"])

        if arrival_role and arrival_role in member.roles:
            await member.remove_roles(arrival_role, reason="Immigration complete")
        if state_role:
            await member.add_roles(state_role, reason="Immigration complete")

        await ctx.send(
            f"{member.mention} fully immigrated -- now has full access to {state}."
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Immigration(bot))

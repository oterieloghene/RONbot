import discord
from discord import app_commands
from discord.ext import commands

import config
import database


def is_immigration_officer():
    async def predicate(interaction: discord.Interaction) -> bool:
        role = interaction.guild.get_role(config.IMMIGRATION_OFFICER_ROLE_ID)
        return role is not None and role in interaction.user.roles

    return app_commands.check(predicate)


class Immigration(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="immigrate",
        description="(Immigration Officer) Process a new arrival and assign their state identity",
    )
    @app_commands.describe(
        member="The player to process",
        player_name="The RP name to give this player",
    )
    @is_immigration_officer()
    async def immigrate(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        player_name: str,
    ):
        player = await database.get_player(member.id)

        if player is None or player["immigration_status"] == "unarrived":
            await interaction.response.send_message(
                f"{member.mention} hasn't chosen a destination yet -- nothing to process.",
                ephemeral=True,
            )
            return

        if player["immigration_status"] == "immigrated":
            await interaction.response.send_message(
                f"{member.mention} has already been immigrated as "
                f"**{player['player_name']}** ({player['player_id']}).",
                ephemeral=True,
            )
            return

        state = player["current_state"]
        state_cfg = config.STATES.get(state)
        if state_cfg is None:
            await interaction.response.send_message(
                f"Unknown state on record: {state}. Check config.py.", ephemeral=True
            )
            return

        # Confirm this is happening in the right state's immigration office
        if interaction.channel_id != state_cfg["immigration_office_channel_id"]:
            await interaction.response.send_message(
                f"This has to be run in {state}'s Immigration Office channel.",
                ephemeral=True,
            )
            return

        player_id = await database.complete_immigration(member.id, player_name)

        # Swap arrival (limited-access) role for the full indigene role
        arrival_role = interaction.guild.get_role(state_cfg["arrival_role_id"])
        indigene_role = interaction.guild.get_role(state_cfg["indigene_role_id"])

        if arrival_role and arrival_role in member.roles:
            await member.remove_roles(arrival_role, reason="Immigration complete")
        if indigene_role:
            await member.add_roles(indigene_role, reason="Immigration complete")

        try:
            await member.edit(nick=player_name)
        except discord.Forbidden:
            pass  # bot role may be below the member's -- not fatal

        await interaction.response.send_message(
            f"{member.mention} processed as **{player_name}** ({player_id}) -- "
            f"now a {state} Indigene."
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Immigration(bot))

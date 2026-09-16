import discord
from discord import app_commands
from discord.ext import commands

import config
import database


class DestinationSelect(discord.ui.Select):
    """Dropdown a new member uses to choose their destination state.
    custom_id is fixed so the view survives bot restarts (registered as a
    persistent view in Onboarding.cog_load)."""

    def __init__(self):
        options = [
            discord.SelectOption(label=state, value=state)
            for state in config.STATES.keys()
        ]
        super().__init__(
            placeholder="Choose your destination...",
            options=options,
            custom_id="rp:destination_select",
        )

    async def callback(self, interaction: discord.Interaction):
        state = self.values[0]
        member = interaction.user
        guild = interaction.guild
        state_cfg = config.STATES[state]

        # Already arrived somewhere? Don't let them pick again.
        existing = await database.get_player(member.id)
        if existing and existing["immigration_status"] != "unarrived":
            await interaction.response.send_message(
                "You've already arrived somewhere -- you can't pick a destination again.",
                ephemeral=True,
            )
            return

        await database.ensure_player_exists(member.id)
        await database.record_arrival(member.id, state)

        # Swap Newcomer role for this state's arrival (limited-access) role
        newcomer_role = guild.get_role(config.NEWCOMER_ROLE_ID)
        arrival_role = guild.get_role(state_cfg["arrival_role_id"])

        if newcomer_role and newcomer_role in member.roles:
            await member.remove_roles(newcomer_role, reason="Picked a destination")
        if arrival_role:
            await member.add_roles(arrival_role, reason=f"Arrived in {state}")

        await interaction.response.send_message(
            f"Welcome to **{state}**! Head to the Refugee Camp or Immigration "
            f"Office to get processed.",
            ephemeral=True,
        )

        # Post the arrival announcement in that state's Arrival Terminal
        terminal_channel = guild.get_channel(state_cfg["arrival_terminal_channel_id"])
        if terminal_channel:
            await terminal_channel.send(
                f"{member.mention} has arrived in {state}. Welcome to {state} State."
            )


class DestinationView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)  # persistent
        self.add_item(DestinationSelect())


class Onboarding(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        # Re-register the persistent view so the dropdown keeps working
        # after a restart/redeploy on Render.
        self.bot.add_view(DestinationView())

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        await database.ensure_player_exists(member.id)
        newcomer_role = member.guild.get_role(config.NEWCOMER_ROLE_ID)
        if newcomer_role:
            await member.add_roles(newcomer_role, reason="New member joined")

    @app_commands.command(
        name="post_destination_select",
        description="(Admin) Post the destination-selection dropdown in this channel",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def post_destination_select(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="Choose Your Destination",
            description="Select the state you want to enter. This determines "
            "where you arrive and cannot be undone.",
        )
        await interaction.channel.send(embed=embed, view=DestinationView())
        await interaction.response.send_message("Posted.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Onboarding(bot))

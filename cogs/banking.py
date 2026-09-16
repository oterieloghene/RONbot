import discord
from discord.ext import commands

import database

# Abuja's government/staff roles are named after FCT (the Federal Capital
# Territory that administers it) rather than "Abuja" -- e.g.
# "Bank Staff (FCT Employee)", matching how the FCT Minister etc. are
# referred to in-universe. Everywhere else in this codebase (players.state,
# config.STATES, the `locations` table) still calls the state "Abuja", so
# this map translates between the two.
STATE_ROLE_LABEL = {
    "Abuja": "FCT",
    "Lagos": "Lagos",
    "Delta": "Delta",
}


def is_bank_staff():
    """Bank Staff role for the state whose channel the command was run in --
    e.g. only Delta's Bank Staff can run !dep in Delta's deposit channel."""

    async def predicate(ctx: commands.Context) -> bool:
        location = await database.get_location_by_channel(ctx.channel.id)
        if location is None or location["category"] != "BANK PLC":
            return False

        label = None
        for state, role_label in STATE_ROLE_LABEL.items():
            if state.upper() == location["state"]:
                label = role_label
                break
        if label is None:
            return False

        role_id = await database.get_role_id(f"Bank Staff ({label} Employee)")
        role = ctx.guild.get_role(role_id) if role_id else None
        return role is not None and role in ctx.author.roles

    return commands.check(predicate)


def in_bank_channel(channel_name: str):
    """Confirms the command was run in a specific BANK PLC channel, e.g.
    'atm' for !with and !transfer, 'deposit' for !dep, 'customer-care' for
    !create-account. Returns the location row (with its state) to the
    command via ctx -- callers re-fetch it since checks can't return values."""

    async def predicate(ctx: commands.Context) -> bool:
        location = await database.get_location_by_channel(ctx.channel.id)
        return (
            location is not None
            and location["category"] == "BANK PLC"
            and location["channel_name"] == channel_name
        )

    return commands.check(predicate)


class Banking(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _log_transaction(self, state: str, message: str) -> None:
        channel_id = await database.get_transaction_log_channel_id(state)
        channel = self.bot.get_channel(channel_id) if channel_id else None
        if channel:
            await channel.send(message)

    @commands.command(name="create-account")
    @is_bank_staff()
    @in_bank_channel("customer-care")
    async def create_account(self, ctx: commands.Context, member: discord.Member):
        existing = await database.get_personal_account(member.id)
        if existing is not None:
            await ctx.send(f"{member.mention} already has an account: `{existing['account_number']}`.")
            return

        location = await database.get_location_by_channel(ctx.channel.id)
        account_number = await database.create_personal_account(
            member.id, location["state"], created_by=ctx.author.id
        )
        await ctx.send(f"Account created for {member.mention}: `{account_number}`.")
        await self._log_transaction(
            location["state"],
            f"Account `{account_number}` opened for {member.mention} by {ctx.author.mention}.",
        )

    @commands.command(name="dep")
    @is_bank_staff()
    @in_bank_channel("deposit")
    async def deposit(self, ctx: commands.Context, member: discord.Member, amount: float):
        location = await database.get_location_by_channel(ctx.channel.id)
        try:
            result = await database.deposit(
                member.id, amount, performed_by=ctx.author.id, location_state=location["state"]
            )
        except (database.InsufficientFunds, ValueError) as e:
            await ctx.send(str(e))
            return

        account = result["account"]
        await ctx.send(
            f"Deposited {amount:,.2f} into {member.mention}'s account `{account['account_number']}`."
        )
        await self._log_transaction(
            account["state"],
            f"Deposit: {amount:,.2f} into `{account['account_number']}` "
            f"({member.mention}) by {ctx.author.mention}, run from {location['state']}.",
        )

    @commands.command(name="with")
    @in_bank_channel("atm")
    async def withdraw(self, ctx: commands.Context, amount: float):
        location = await database.get_location_by_channel(ctx.channel.id)
        try:
            result = await database.withdraw(
                ctx.author.id, amount, location_state=location["state"]
            )
        except (database.InsufficientFunds, ValueError) as e:
            await ctx.send(str(e))
            return

        account = result["account"]
        await ctx.send(f"Withdrew {amount:,.2f} from `{account['account_number']}`. Now in your pocket.")
        await self._log_transaction(
            account["state"],
            f"Withdrawal: {amount:,.2f} from `{account['account_number']}` "
            f"({ctx.author.mention}), run from {location['state']}.",
        )

    @commands.command(name="transfer")
    @in_bank_channel("atm")
    async def transfer(self, ctx: commands.Context, to_account_number: str, amount: float):
        location = await database.get_location_by_channel(ctx.channel.id)
        try:
            result = await database.transfer(
                ctx.author.id, to_account_number, amount, location_state=location["state"]
            )
        except (database.InsufficientFunds, ValueError) as e:
            await ctx.send(str(e))
            return

        from_account = result["from_account"]
        to_account = result["to_account"]
        await ctx.send(
            f"Transferred {amount:,.2f} from `{from_account['account_number']}` "
            f"to `{to_account_number}`."
        )
        await self._log_transaction(
            from_account["state"],
            f"Transfer out: {amount:,.2f} from `{from_account['account_number']}` "
            f"({ctx.author.mention}) to `{to_account_number}`, run from {location['state']}.",
        )
        if to_account["state"] != from_account["state"]:
            await self._log_transaction(
                to_account["state"],
                f"Transfer in: {amount:,.2f} into `{to_account_number}` "
                f"from `{from_account['account_number']}` ({ctx.author.mention}).",
            )

    @commands.command(name="cash-bal")
    async def cash_bal(self, ctx: commands.Context):
        balance = await database.get_cash_balance(ctx.author.id)
        await ctx.send(f"{ctx.author.mention}, you're carrying {balance:,.2f} in cash.")


async def setup(bot: commands.Bot):
    await bot.add_cog(Banking(bot))

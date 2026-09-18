"""
The player's Phone -- one ephemeral menu covering Bus, BRT Card, Taxi,
Flights, Hotel, Contacts, Mechanic, Dispatch, Map, Emergency, My Job
App, and the Bank App.

SCOPE, AS OF THIS FILE:

    RONbot currently only has real commands for three of those twelve
    screens -- transportation.py (book-bus / buy-brt / approve-brt),
    banking.py (cash-bal / with / transfer / create-account / dep), and
    petroleum.py's trailer/tanker fleet (buy-trailer / buy-tanker /
    approve-vehicle / decline-vehicle / order-trailer / order-tanker,
    wired in below as DispatchView -- see cogs/petroleum.py's module
    docstring for the full buy -> approve -> order flow). So "Bus",
    "Bank App", and "Dispatch" actually do something below; the other
    nine (BRT Card, Taxi, Flights, Hotel, Contacts, Mechanic, Map,
    Emergency, My Job App) render a "Coming Soon" screen with a Back
    button and nothing else, since there's no backend command yet for
    the phone to call into. Wiring each of those up later is just a
    matter of adding a new View + modal (if it needs typed input)
    following the same pattern as BusView / BankAppView / DispatchView
    below, and swapping its MainMenuView button over from
    open_coming_soon to it.

    Note: interstate (and Nigeria-owned intrastate) hire approvals stay
    a button card posted straight to the owning side's Ministry of
    Commerce channel (HireDecisionView in petroleum.py), same as the
    design note above about bus approvals -- there's deliberately no
    "Approve/Decline a Hire Request" button on the phone.

HOW THIS WORKS (same approach as the EkoPhone reference file):

    This cog does NOT duplicate the logic in transportation.py or
    banking.py, and does NOT modify those files. Every button/modal
    here builds a normal command Context (same as if the player had
    typed the command themselves in that channel) and calls
    ctx.invoke(...) on the REAL command. That means every existing
    check, fare/fee calculation, and treasury/account update stays
    exactly as it already is -- the phone is just a friendlier front
    door.

    IMPORTANT CONSEQUENCE: several banking commands are gated to a
    specific channel via @in_bank_channel(...) (e.g. !with and
    !transfer only work in the "atm" channel, !dep/!create-account
    only in "deposit"/"customer-care" and only for Bank Staff). Since
    ctx.invoke() runs against the channel the player typed !phone in,
    clicking "Withdraw" from the phone still requires the player to
    be sitting in the right physical channel -- exactly like typing
    !with themselves. The phone does not bypass location gating.

    Those channel/role checks are enforced with @commands.check(...)
    decorators in banking.py, which raise commands.CheckFailure
    instead of just calling ctx.send() with a friendly message (that's
    the difference from e.g. buy-brt's "Only a Commissioner of
    Commerce can request a bus." check, which is a plain `if` inside
    the command body and so already sends its own message through
    ctx.send). _invoke() below catches CheckFailure and turns it into
    a plain ephemeral "you can't do that here" message so a wrong-
    channel or wrong-role click doesn't just silently fail.

    An image asset lives at cogs/assets/phone_home.png (create that
    assets/ folder if it isn't already in your repo and drop the file
    in) and is attached once, on the initial ephemeral send in
    PhoneOpenView.open_phone -- every later screen just points its
    embed at the same "attachment://phone_home.png" reference, the
    same trick the other bot's phone.py uses, so it doesn't need to
    be re-uploaded on every button click.
"""

import os

import discord
from discord.ext import commands

import database


PHONE_TIMEOUT_SECONDS = 180
PHONE_OPEN_TIMEOUT_SECONDS = 60

PHONE_COLOR = discord.Color.from_rgb(61, 220, 132)  # Android green

PHONE_IMAGE_PATH = os.path.join(os.path.dirname(__file__), "assets", "phone_home.png")

# Every screen not yet backed by a real command. Keys match the labels
# used on the MainMenuView buttons below.
COMING_SOON_SCREENS = (
    "BRT Card",
    "Taxi",
    "Flights",
    "Hotel",
    "Contacts",
    "Mechanic",
    "Map",
    "Emergency",
    "My Job App",
)


def _phone_embed(subtitle: str = None, description: str = None) -> discord.Embed:
    embed = discord.Embed(
        title="\U0001f4f1 Phone" + (f" \u2192 {subtitle}" if subtitle else ""),
        description=description,
        color=PHONE_COLOR,
    )
    embed.set_image(url="attachment://phone_home.png")
    return embed


def _resolve_member(guild: discord.Guild, raw: str) -> discord.Member | None:
    """Accepts a raw ID, an '<@123>' / '<@!123>' mention, or a bare
    numeric string typed into a modal, and returns the matching member
    (or None if it doesn't resolve)."""
    cleaned = raw.strip().lstrip("<@!").rstrip(">")
    if not cleaned.isdigit():
        return None
    return guild.get_member(int(cleaned))


# ================================================================
# HELPER -- BUILD A USABLE ctx FROM A BUTTON/MODAL INTERACTION
# ================================================================

class _SilentMessage:
    """Stand-in for ctx.message so commands that call
    ctx.message.delete() (cleaning up a typed command) don't try to
    delete the phone panel instead."""

    def __init__(self, real_message: discord.Message):
        self.id = real_message.id
        self.channel = real_message.channel
        self.guild = real_message.guild
        self.author = real_message.author
        self.content = ""

    async def delete(self, *args, **kwargs):
        return None


async def _invoke(
    bot: commands.Bot,
    interaction: discord.Interaction,
    command_name: str,
    *args,
    **kwargs,
) -> bool:
    """Run an existing prefix command exactly as if the player had
    typed it in interaction.channel, using their real permissions and
    real location. Assumes the interaction has already been deferred
    ephemerally by the caller."""

    command = bot.get_command(command_name)

    if command is None:
        await interaction.followup.send(
            f"\u26a0\ufe0f Internal error: `{command_name}` isn't loaded. Tell an admin.",
            ephemeral=True,
        )
        return False

    ctx = await bot.get_context(interaction.message)
    ctx.author = interaction.user
    ctx.message = _SilentMessage(interaction.message)
    ctx.from_phone = True

    async def _ephemeral_send(*send_args, **send_kwargs):
        send_kwargs["ephemeral"] = True
        return await interaction.followup.send(*send_args, **send_kwargs)

    ctx.send = _ephemeral_send

    try:
        await ctx.invoke(command, *args, **kwargs)
    except commands.CheckFailure:
        # A @commands.check(...) (e.g. is_bank_staff / in_bank_channel)
        # failed silently -- give the player something instead of
        # letting the interaction just look broken.
        await interaction.followup.send(
            "\U0001f6ab You can't do that from here -- wrong channel, "
            "or you don't have the role for it.",
            ephemeral=True,
        )
        return False
    except commands.BadArgument:
        await interaction.followup.send(
            "\u26a0\ufe0f That input didn't look right. Try again.",
            ephemeral=True,
        )
        return False

    return True


# ================================================================
# SHARED BASE VIEW -- only the phone's owner can press its buttons
# ================================================================

class _OwnedView(discord.ui.View):
    def __init__(self, bot: commands.Bot, owner_id: int, timeout: int = PHONE_TIMEOUT_SECONDS):
        super().__init__(timeout=timeout)
        self.bot = bot
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "\U0001f4f5 This isn't your phone.", ephemeral=True
            )
            return False
        return True


# ================================================================
# MODALS
# ================================================================

class BookRideModal(discord.ui.Modal, title="Book a Bus"):
    destination_zone = discord.ui.TextInput(
        label="Destination zone (A, B, or C)", placeholder="B", max_length=2
    )

    def __init__(self, bot: commands.Bot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=False)
        await _invoke(self.bot, interaction, "book-bus", self.destination_zone.value)


class RequestBusModal(discord.ui.Modal, title="Request a Bus"):
    zone_a = discord.ui.TextInput(label="From zone", placeholder="A", max_length=2)
    zone_b = discord.ui.TextInput(label="To zone", placeholder="B", max_length=2)

    def __init__(self, bot: commands.Bot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=False)
        await _invoke(self.bot, interaction, "buy-brt", self.zone_a.value, self.zone_b.value)


class ApproveBusModal(discord.ui.Modal, title="Approve a Bus Request"):
    request_id = discord.ui.TextInput(label="Request ID", placeholder="12", max_length=10)

    def __init__(self, bot: commands.Bot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=False)
        if not self.request_id.value.strip().isdigit():
            await interaction.followup.send("Request ID has to be a number.", ephemeral=True)
            return
        await _invoke(self.bot, interaction, "approve-brt", int(self.request_id.value.strip()))


class DeclineBusModal(discord.ui.Modal, title="Decline a Bus Request"):
    request_id = discord.ui.TextInput(label="Request ID", placeholder="12", max_length=10)

    def __init__(self, bot: commands.Bot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=False)
        if not self.request_id.value.strip().isdigit():
            await interaction.followup.send("Request ID has to be a number.", ephemeral=True)
            return
        await _invoke(self.bot, interaction, "decline-brt", int(self.request_id.value.strip()))


class OrderTrailerModal(discord.ui.Modal, title="Order a Trailer"):
    destination = discord.ui.TextInput(
        label="Destination (state + channel name)",
        placeholder="lagos refinery",
        max_length=50,
    )
    fleet = discord.ui.TextInput(
        label="Fleet (own / nigeria / a state name)",
        placeholder="own",
        required=False,
        max_length=20,
    )

    def __init__(self, bot: commands.Bot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=False)
        fleet = self.fleet.value.strip() or "own"
        await _invoke(self.bot, interaction, "order-trailer", fleet, destination=self.destination.value.strip())


class OrderTankerModal(discord.ui.Modal, title="Order a Tanker"):
    destination = discord.ui.TextInput(
        label="Destination (state + channel name)",
        placeholder="lagos nnpc-fuel-station",
        max_length=50,
    )
    fleet = discord.ui.TextInput(
        label="Fleet (own / nigeria / a state name)",
        placeholder="own",
        required=False,
        max_length=20,
    )

    def __init__(self, bot: commands.Bot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=False)
        fleet = self.fleet.value.strip() or "own"
        await _invoke(self.bot, interaction, "order-tanker", fleet, destination=self.destination.value.strip())


class ApproveVehicleModal(discord.ui.Modal, title="Approve a Vehicle Purchase"):
    request_id = discord.ui.TextInput(label="Request ID", placeholder="12", max_length=10)

    def __init__(self, bot: commands.Bot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=False)
        if not self.request_id.value.strip().isdigit():
            await interaction.followup.send("Request ID has to be a number.", ephemeral=True)
            return
        await _invoke(self.bot, interaction, "approve-vehicle", int(self.request_id.value.strip()))


class DeclineVehicleModal(discord.ui.Modal, title="Decline a Vehicle Purchase"):
    request_id = discord.ui.TextInput(label="Request ID", placeholder="12", max_length=10)

    def __init__(self, bot: commands.Bot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=False)
        if not self.request_id.value.strip().isdigit():
            await interaction.followup.send("Request ID has to be a number.", ephemeral=True)
            return
        await _invoke(self.bot, interaction, "decline-vehicle", int(self.request_id.value.strip()))


class WithdrawModal(discord.ui.Modal, title="Withdraw"):
    amount = discord.ui.TextInput(label="Amount", placeholder="5000", max_length=15)

    def __init__(self, bot: commands.Bot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=False)
        try:
            amount = float(self.amount.value.strip())
        except ValueError:
            await interaction.followup.send("That doesn't look like a number.", ephemeral=True)
            return
        await _invoke(self.bot, interaction, "with", amount=amount)


class TransferModal(discord.ui.Modal, title="Transfer"):
    to_account_number = discord.ui.TextInput(label="To account number", max_length=30)
    amount = discord.ui.TextInput(label="Amount", placeholder="5000", max_length=15)

    def __init__(self, bot: commands.Bot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=False)
        try:
            amount = float(self.amount.value.strip())
        except ValueError:
            await interaction.followup.send("That doesn't look like a number.", ephemeral=True)
            return
        await _invoke(
            self.bot,
            interaction,
            "transfer",
            to_account_number=self.to_account_number.value.strip(),
            amount=amount,
        )


class CreateAccountModal(discord.ui.Modal, title="Open an Account (Bank Staff)"):
    member_id = discord.ui.TextInput(
        label="Customer's user ID or @mention", placeholder="123456789012345678"
    )

    def __init__(self, bot: commands.Bot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=False)
        member = _resolve_member(interaction.guild, self.member_id.value)
        if member is None:
            await interaction.followup.send("Couldn't find that member.", ephemeral=True)
            return
        await _invoke(self.bot, interaction, "create-account", member)


class DepositModal(discord.ui.Modal, title="Deposit (Bank Staff)"):
    member_id = discord.ui.TextInput(
        label="Customer's user ID or @mention", placeholder="123456789012345678"
    )
    amount = discord.ui.TextInput(label="Amount", placeholder="5000", max_length=15)

    def __init__(self, bot: commands.Bot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=False)
        member = _resolve_member(interaction.guild, self.member_id.value)
        if member is None:
            await interaction.followup.send("Couldn't find that member.", ephemeral=True)
            return
        try:
            amount = float(self.amount.value.strip())
        except ValueError:
            await interaction.followup.send("That doesn't look like a number.", ephemeral=True)
            return
        await _invoke(self.bot, interaction, "dep", member, amount=amount)


# ================================================================
# SUBMENUS
# ================================================================

class ComingSoonView(_OwnedView):
    """The screen every not-yet-built menu item lands on."""

    def __init__(self, bot: commands.Bot, owner_id: int):
        super().__init__(bot, owner_id)

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, emoji="\u2b05\ufe0f")
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=_phone_embed(), view=MainMenuView(self.bot, self.owner_id))


class BusView(_OwnedView):
    @discord.ui.button(label="Book a Ride", style=discord.ButtonStyle.primary, emoji="\U0001f68c")
    async def book_ride(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(BookRideModal(self.bot))

    @discord.ui.button(label="Check Queue", style=discord.ButtonStyle.secondary, emoji="\U0001f552")
    async def check_queue(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=False)
        await _invoke(self.bot, interaction, "queue")

    @discord.ui.button(label="Request a Bus", style=discord.ButtonStyle.secondary, emoji="\U0001f4dd", row=1)
    async def request_bus(self, interaction: discord.Interaction, button: discord.ui.Button):
        # No role check here -- buy-brt already sends its own "Only a
        # Commissioner of Commerce can request a bus." message via
        # ctx.send if the player doesn't hold the role.
        await interaction.response.send_modal(RequestBusModal(self.bot))

    @discord.ui.button(label="Approve a Bus Request", style=discord.ButtonStyle.success, emoji="\u2705", row=1)
    async def approve_bus(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ApproveBusModal(self.bot))

    @discord.ui.button(label="Decline a Bus Request", style=discord.ButtonStyle.danger, emoji="\u274c", row=1)
    async def decline_bus(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(DeclineBusModal(self.bot))

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, emoji="\u2b05\ufe0f", row=2)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=_phone_embed(), view=MainMenuView(self.bot, self.owner_id))


class DispatchView(_OwnedView):
    """The trailer/tanker fleet -- buy, get a purchase approved/declined
    by Finance, and order a pickup. Interstate (and Nigeria-owned
    intrastate) hire approvals are deliberately NOT here -- those are
    the HireDecisionView button card posted to a Ministry of Commerce
    channel, same "never a typed command" rule as bus approvals."""

    @discord.ui.button(label="Buy Trailer", style=discord.ButtonStyle.primary, emoji="\U0001f69a")
    async def buy_trailer(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=False)
        await _invoke(self.bot, interaction, "buy-trailer")

    @discord.ui.button(label="Buy Tanker", style=discord.ButtonStyle.primary, emoji="\u26fd")
    async def buy_tanker(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=False)
        await _invoke(self.bot, interaction, "buy-tanker")

    @discord.ui.button(label="Order Trailer", style=discord.ButtonStyle.secondary, emoji="\U0001f4dd", row=1)
    async def order_trailer(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(OrderTrailerModal(self.bot))

    @discord.ui.button(label="Order Tanker", style=discord.ButtonStyle.secondary, emoji="\U0001f4dd", row=1)
    async def order_tanker(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(OrderTankerModal(self.bot))

    @discord.ui.button(label="Approve a Purchase", style=discord.ButtonStyle.success, emoji="\u2705", row=2)
    async def approve_purchase(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ApproveVehicleModal(self.bot))

    @discord.ui.button(label="Decline a Purchase", style=discord.ButtonStyle.danger, emoji="\u274c", row=2)
    async def decline_purchase(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(DeclineVehicleModal(self.bot))

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, emoji="\u2b05\ufe0f", row=3)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=_phone_embed(), view=MainMenuView(self.bot, self.owner_id))


class BankAppView(_OwnedView):
    @discord.ui.button(label="Cash Balance", style=discord.ButtonStyle.primary, emoji="\U0001f4b5")
    async def cash_balance(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=False)
        await _invoke(self.bot, interaction, "cash-bal")

    @discord.ui.button(label="Withdraw", style=discord.ButtonStyle.primary, emoji="\U0001f3e7")
    async def withdraw(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(WithdrawModal(self.bot))

    @discord.ui.button(label="Transfer", style=discord.ButtonStyle.primary, emoji="\U0001f4b8")
    async def transfer(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TransferModal(self.bot))

    @discord.ui.button(label="Open Account (Staff)", style=discord.ButtonStyle.secondary, emoji="\U0001f4c7", row=1)
    async def create_account(self, interaction: discord.Interaction, button: discord.ui.Button):
        # No role/location pre-check -- is_bank_staff() + in_bank_channel()
        # on !create-account itself will CheckFailure (caught by _invoke)
        # for anyone who isn't Bank Staff standing in customer-care.
        await interaction.response.send_modal(CreateAccountModal(self.bot))

    @discord.ui.button(label="Deposit (Staff)", style=discord.ButtonStyle.secondary, emoji="\U0001f4b0", row=1)
    async def deposit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(DepositModal(self.bot))

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, emoji="\u2b05\ufe0f", row=1)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=_phone_embed(), view=MainMenuView(self.bot, self.owner_id))


# ================================================================
# MAIN MENU
# ================================================================

class MainMenuView(_OwnedView):

    async def _open_coming_soon(self, interaction: discord.Interaction, label: str):
        await interaction.response.edit_message(
            embed=_phone_embed(label, "\U0001f6a7 Coming soon."),
            view=ComingSoonView(self.bot, self.owner_id),
        )

    @discord.ui.button(label="Bus", style=discord.ButtonStyle.primary, emoji="\U0001f68c")
    async def bus(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=_phone_embed("Bus"), view=BusView(self.bot, self.owner_id))

    @discord.ui.button(label="BRT Card", style=discord.ButtonStyle.primary, emoji="\U0001f4b3")
    async def brt_card(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._open_coming_soon(interaction, "BRT Card")

    @discord.ui.button(label="Taxi", style=discord.ButtonStyle.primary, emoji="\U0001f695")
    async def taxi(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._open_coming_soon(interaction, "Taxi")

    @discord.ui.button(label="Flights", style=discord.ButtonStyle.primary, emoji="\u2708\ufe0f")
    async def flights(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._open_coming_soon(interaction, "Flights")

    @discord.ui.button(label="Hotel", style=discord.ButtonStyle.primary, emoji="\U0001f3e8", row=1)
    async def hotel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._open_coming_soon(interaction, "Hotel")

    @discord.ui.button(label="Contacts", style=discord.ButtonStyle.primary, emoji="\U0001f4d6", row=1)
    async def contacts(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._open_coming_soon(interaction, "Contacts")

    @discord.ui.button(label="Mechanic", style=discord.ButtonStyle.primary, emoji="\U0001f527", row=1)
    async def mechanic(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._open_coming_soon(interaction, "Mechanic")

    @discord.ui.button(label="Dispatch", style=discord.ButtonStyle.primary, emoji="\U0001f4e1", row=1)
    async def dispatch(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=_phone_embed("Dispatch"), view=DispatchView(self.bot, self.owner_id)
        )

    @discord.ui.button(label="Map", style=discord.ButtonStyle.primary, emoji="\U0001f5fa\ufe0f", row=2)
    async def map_(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._open_coming_soon(interaction, "Map")

    @discord.ui.button(label="Emergency", style=discord.ButtonStyle.danger, emoji="\U0001f6a8", row=2)
    async def emergency(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._open_coming_soon(interaction, "Emergency")

    @discord.ui.button(label="My Job App", style=discord.ButtonStyle.primary, emoji="\U0001f4bc", row=2)
    async def job_app(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._open_coming_soon(interaction, "My Job App")

    @discord.ui.button(label="Bank App", style=discord.ButtonStyle.success, emoji="\U0001f3e6", row=2)
    async def bank_app(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=_phone_embed("Bank App"), view=BankAppView(self.bot, self.owner_id)
        )


# ================================================================
# THE ONE-TAP GATE -- the only ever-public part of the phone
# ================================================================

class PhoneOpenView(discord.ui.View):
    """The public "Tap to open your phone" prompt !phone posts. Locked
    to whoever typed !phone; a tap from the right person opens their
    own real, ephemeral phone menu and deletes this prompt."""

    def __init__(self, bot: commands.Bot, owner_id: int):
        super().__init__(timeout=PHONE_OPEN_TIMEOUT_SECONDS)
        self.bot = bot
        self.owner_id = owner_id
        self.message: discord.Message = None

    @discord.ui.button(label="Tap to open your phone", style=discord.ButtonStyle.primary, emoji="\U0001f4f1")
    async def open_phone(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("\U0001f4f5 This isn't your phone.", ephemeral=True)
            return

        try:
            await interaction.response.send_message(
                embed=_phone_embed(),
                file=discord.File(PHONE_IMAGE_PATH, filename="phone_home.png"),
                view=MainMenuView(self.bot, self.owner_id),
                ephemeral=True,
            )
        except (discord.HTTPException, FileNotFoundError):
            await interaction.response.send_message(
                "\U0001f4f1 **Phone** (image unavailable)",
                view=MainMenuView(self.bot, self.owner_id),
                ephemeral=True,
            )

        try:
            await interaction.message.delete()
        except discord.HTTPException:
            pass

        self.stop()

    async def on_timeout(self):
        if self.message is not None:
            try:
                await self.message.delete()
            except discord.HTTPException:
                pass


# ================================================================
# COG
# ================================================================

class PhoneCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="phone")
    async def phone(self, ctx: commands.Context):
        await database.ensure_player_exists(ctx.author.id)

        view = PhoneOpenView(self.bot, ctx.author.id)
        try:
            message = await ctx.send("\U0001f4f1 **Tap to open your phone**", view=view)
            view.message = message
        except discord.HTTPException:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(PhoneCog(bot))

"""
The oil/fuel economy: Delta-only crude drilling, refining at any state's
refinery, and the state- or Nigeria-owned trailer/tanker fleet that
hauls crude from the oil-well to a refinery and refined fuel from a
refinery to an NNPC station.

FLOW, END TO END:

    !drill (Delta Commissioner of Petroleum, at the oil-well) -> pending
    treasury-cost request -> !approve-drill / !decline-drill (Delta
    Commissioner of Finance) -> 15 minutes -> +50 barrels at the well
    (capped at 250), then a 10-minute cooldown before the well can be
    drilled again.

    A Commissioner of Commerce (or the Minister of Trade and Commerce,
    for Nigeria's fleet) buys a trailer/tanker with !buy-trailer /
    !buy-tanker -- that submits a pending vehicle_purchase_requests row,
    same shape as !drill: the state's Commissioner of Finance (or, for
    Nigeria's fleet, the Minister of Finance) has to !approve-vehicle /
    !decline-vehicle it before the treasury (or national treasury) is
    actually debited and the vehicle is spawned at its home depot.

    Once owned, it's dispatched to a pickup location with !order-trailer
    / !order-tanker -- phone-only (Dispatch screen -> Order Trailer/
    Tanker), never a typed command in a channel: there's no physical
    channel to infer a destination from, so the Commissioner of Commerce
    ordering it types the destination directly (state + channel-name,
    e.g. "lagos refinery") into the modal. Using your own
    state's vehicle within your own state is free and dispatches
    immediately -- no approval needed. Every other case needs the
    owning side's sign-off: a Nigeria-owned vehicle used within a single
    state is a flat fee + km, and anything that crosses a state line is
    a (higher) flat fee + km -- both posted as an Approve/Decline button
    card in the owning side's Ministry of Commerce channel, never a
    typed command (see HireDecisionView below).

    Once a trailer arrives at the oil-well: `!load crude <qty>` pulls
    barrels from the well onto it. At a refinery: `!offload crude <qty>`
    drops them into that refinery's crude stock, and
    `!refine crude <qty>` converts crude into fuel (50L/barrel) and gas
    (60kg/barrel) after a 1-minute-per-barrel refining time, capped at
    12,500L / 15,000kg. `!load fuel <qty>` at the refinery puts refined
    fuel onto an idle tanker; `!offload fuel <qty>` at an NNPC station
    credits that state's fuel_stations.stock_litres (the same stock
    !refuel in cogs/cars.py draws down). Refined gas can't be loaded
    onto a tanker yet -- future work, same as the original design doc
    says.

DESIGN NOTES:

    Reuses cogs.transportation's STATE_ROLE_LABEL / commissioner_role_name /
    member_commissioner_state / debit_treasury / credit_ministry_of_commerce /
    InsufficientTreasuryFunds rather than duplicating them -- a
    Commissioner of Petroleum/Commerce/Finance check is identical in
    shape to the existing Commerce/Finance checks in transportation.py,
    just a different office string.

    Reuses cogs.cars's straight-line/interstate distance helpers
    (_get_distance_km, _get_interstate_leg, _get_location,
    ROAD_CHECKPOINT_CATEGORY/CHANNEL) for hire pricing's per-km
    component, so a trailer/tanker order costs the same km math a
    private car trip would for the same two points.

    "Nigeria" is not a real `locations.state` value -- it's a pseudo
    owner_state used only in state_vehicles/vehicle_hire_requests to
    mean "owned by the federal government, purchased/ordered by the
    Minister of Trade and Commerce". Its vehicles physically live in and
    move between real states like any other; only its *ownership* (and
    so its treasury and pricing tier) is federal. Its home depot and its
    Ministry of Commerce (for hire approvals) are both Abuja's.
"""

import datetime

import discord
from discord.ext import commands, tasks

import database
import discord_utils
import cogs.cars as cars_module
from cogs.transportation import (
    STATE_ROLE_LABEL,
    InsufficientTreasuryFunds,
    credit_ministry_of_commerce,
    debit_treasury,
    member_commissioner_state,
)

TICK_SECONDS = 10

OIL_STATE = "DELTA"  # only Delta has an oil-well, per the design doc

# -- drilling --------------------------------------------------------------
DRILL_COST = 2_000_000       # placeholder equipment cost -- tune freely
DRILL_YIELD_BARRELS = 50
CRUDE_CAP_BARRELS = 250       # 5 drills' worth
DRILL_DURATION = datetime.timedelta(minutes=15)
DRILL_COOLDOWN = datetime.timedelta(minutes=10)

# -- refining ----------------------------------------------------------------
REFINE_MINUTES_PER_BARREL = 1
BARREL_TO_FUEL_LITRES = 50
BARREL_TO_GAS_KG = 60
FUEL_CAP_LITRES = 12_500
GAS_CAP_KG = 15_000

# -- fleet ---------------------------------------------------------------
VEHICLE_PRICES = {"trailer": 32_000_000, "tanker": 80_000_000}
TRAILER_CAP_BARRELS = 125
TANKER_CAP_LITRES = 6_250  # fuel only for now -- see module docstring

HIRE_FLAT_INTERSTATE = {"trailer": 500_000, "tanker": 800_000}
HIRE_FLAT_NIGERIA_INTRASTATE = {"trailer": 166_667, "tanker": 266_667}
HIRE_PER_KM = {"trailer": 50_000, "tanker": 70_000}

HAUL_SPEED_KMH = 50  # flat placeholder speed for trailers/tankers

VALID_OWNER_STATES = {"DELTA", "LAGOS", "ABUJA", "NIGERIA"}

# -- locations -------------------------------------------------------------
OIL_WELL_CHANNEL = "oil-well"
REFINERY_CHANNEL = "refinery"
NNPC_CHANNEL = cars_module.NNPC_CHANNEL
DEPOT_CATEGORY = "BUSINESS & COMMERCE"
DEPOT_CHANNEL = "depot"
MINISTRY_OF_COMMERCE_CHANNEL = "ministry-of-commerce"


def _vehicle_name(owner_state: str, vehicle_type: str, unit_number: int) -> str:
    label = "Nigeria" if owner_state == "NIGERIA" else owner_state.title()
    return f"{label} {vehicle_type.capitalize()} {unit_number}"


async def _member_has_role(member: discord.Member, role_name: str) -> bool:
    role = discord_utils.get_role(member.guild, role_name)
    return role is not None and role in member.roles


async def _ordering_authority(member: discord.Member) -> str | None:
    """Who's allowed to buy/order trailers & tankers, and whose fleet
    'own' refers to: a state's Commissioner of Commerce (-> that state,
    upper-case), or the Minister of Trade and Commerce (-> 'NIGERIA')."""
    state = await member_commissioner_state(member, "Commerce")
    if state is not None:
        return state.upper()
    if await _member_has_role(member, "Minister of Trade and Commerce"):
        return "NIGERIA"
    return None


async def debit_national_treasury(amount: float) -> None:
    balance = await database.pool().fetchval("SELECT balance FROM national_treasury WHERE id = 1")
    if balance is None or float(balance) < amount:
        raise InsufficientTreasuryFunds(f"The national treasury only has {float(balance or 0):,.2f}.")
    await database.pool().execute("UPDATE national_treasury SET balance = balance - $1 WHERE id = 1", amount)


async def credit_national_treasury(amount: float) -> None:
    await database.pool().execute("UPDATE national_treasury SET balance = balance + $1 WHERE id = 1", amount)


class VehicleOrderError(Exception):
    pass


# ---------------------------------------------------------------------------
# Interstate hire approval -- button card, never a typed command
# ---------------------------------------------------------------------------

class HireDecisionView(discord.ui.View):
    """Posted to the owning state's (or Nigeria's) Ministry of Commerce
    channel. Persistent (timeout=None, fixed custom_id) so it still
    works after a bot restart -- see Petroleum.cog_load, which
    re-registers one of these for every still-pending request."""

    def __init__(self, request_id: int):
        super().__init__(timeout=None)
        self.request_id = request_id
        self.approve.custom_id = f"petro-hire-approve-{request_id}"
        self.decline.custom_id = f"petro-hire-decline-{request_id}"

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, emoji="\u2705")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        await Petroleum.instance.handle_hire_decision(interaction, self.request_id, approve=True)

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger, emoji="\u274c")
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        await Petroleum.instance.handle_hire_decision(interaction, self.request_id, approve=False)


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class Petroleum(commands.Cog):
    instance: "Petroleum" = None  # set in __init__ -- HireDecisionView calls back into it

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        Petroleum.instance = self
        self.petro_tick.start()

    def cog_unload(self):
        self.petro_tick.cancel()

    async def cog_load(self):
        # Re-attach a live view to every hire request still awaiting a
        # decision, so its buttons keep working across a bot restart.
        pending = await database.pool().fetch("SELECT id FROM vehicle_hire_requests WHERE status = 'pending'")
        for row in pending:
            self.bot.add_view(HireDecisionView(row["id"]))

    # -- small DB helpers -----------------------------------------------------

    async def _get_or_create_well(self, state: str):
        row = await database.pool().fetchrow(
            "INSERT INTO oil_well_stock (state) VALUES ($1) ON CONFLICT (state) DO NOTHING RETURNING *",
            state,
        )
        if row is None:
            row = await database.pool().fetchrow("SELECT * FROM oil_well_stock WHERE state = $1", state)
        return row

    async def _get_or_create_refinery(self, state: str):
        row = await database.pool().fetchrow(
            "INSERT INTO refinery_stock (state) VALUES ($1) ON CONFLICT (state) DO NOTHING RETURNING *",
            state,
        )
        if row is None:
            row = await database.pool().fetchrow("SELECT * FROM refinery_stock WHERE state = $1", state)
        return row

    async def _home_location(self, owner_state: str):
        state = "ABUJA" if owner_state == "NIGERIA" else owner_state
        return await cars_module._get_location(state, DEPOT_CATEGORY, DEPOT_CHANNEL)

    async def _ministry_of_commerce_location(self, owner_state: str):
        state = "ABUJA" if owner_state == "NIGERIA" else owner_state
        return await database.pool().fetchrow(
            "SELECT * FROM locations WHERE state = $1 AND channel_name = $2",
            state,
            MINISTRY_OF_COMMERCE_CHANNEL,
        )

    async def _announce(self, state: str, channel_name: str, text: str) -> None:
        location = await database.pool().fetchrow(
            "SELECT * FROM locations WHERE state = $1 AND channel_name = $2", state, channel_name
        )
        if location and location["channel_id"]:
            channel = self.bot.get_channel(location["channel_id"])
            if channel is not None:
                await channel.send(text)

    # =====================================================================
    # DRILLING
    # =====================================================================

    @commands.command(name="drill")
    async def drill(self, ctx: commands.Context):
        location = await database.get_location_by_channel(ctx.channel.id)
        if location is None or location["state"] != OIL_STATE or location["channel_name"] != OIL_WELL_CHANNEL:
            await ctx.send("Crude can only be drilled at the Delta oil-well.")
            return
        state = await member_commissioner_state(ctx.author, "Petroleum")
        if state != "Delta":
            await ctx.send("Only the Delta Commissioner of Petroleum can request a drill.")
            return

        well = await self._get_or_create_well(OIL_STATE)
        now = datetime.datetime.now(datetime.timezone.utc)
        if well["drilling_ready_at"] is not None:
            await ctx.send("A drill is already in progress.")
            return
        if well["cooldown_until"] is not None and well["cooldown_until"] > now:
            remaining = int((well["cooldown_until"] - now).total_seconds())
            await ctx.send(f"Equipment is cooling down for another {remaining}s.")
            return
        if float(well["crude_barrels"]) >= CRUDE_CAP_BARRELS:
            await ctx.send("Crude stock is already at capacity -- move some out before drilling again.")
            return

        request_id = await database.pool().fetchval(
            "INSERT INTO drill_requests (state, requested_by, cost) VALUES ($1, $2, $3) RETURNING id",
            OIL_STATE,
            ctx.author.id,
            DRILL_COST,
        )
        await ctx.send(
            f"Drill request #{request_id} submitted -- {DRILL_COST:,.2f} for equipment. "
            f"Awaiting Delta Commissioner of Finance approval."
        )

    @commands.command(name="approve-drill")
    async def approve_drill(self, ctx: commands.Context, request_id: int):
        state = await member_commissioner_state(ctx.author, "Finance")
        if state != "Delta":
            await ctx.send("Only the Delta Commissioner of Finance can approve a drill.")
            return

        request = await database.pool().fetchrow("SELECT * FROM drill_requests WHERE id = $1", request_id)
        if request is None or request["status"] != "pending":
            await ctx.send("No pending drill request with that ID.")
            return

        try:
            await debit_treasury(OIL_STATE, float(request["cost"]))
        except InsufficientTreasuryFunds as e:
            await ctx.send(str(e))
            return

        await self._get_or_create_well(OIL_STATE)
        await database.pool().execute(
            "UPDATE oil_well_stock SET drilling_ready_at = $2 WHERE state = $1",
            OIL_STATE,
            datetime.datetime.now(datetime.timezone.utc) + DRILL_DURATION,
        )
        await database.pool().execute(
            "UPDATE drill_requests SET status = 'approved', decided_by = $2, decided_at = now() WHERE id = $1",
            request_id,
            ctx.author.id,
        )
        await ctx.send(f"Approved. Drilling underway -- ready in {int(DRILL_DURATION.total_seconds() // 60)} minutes.")

    @commands.command(name="decline-drill")
    async def decline_drill(self, ctx: commands.Context, request_id: int):
        state = await member_commissioner_state(ctx.author, "Finance")
        if state != "Delta":
            await ctx.send("Only the Delta Commissioner of Finance can decline a drill.")
            return

        request = await database.pool().fetchrow("SELECT * FROM drill_requests WHERE id = $1", request_id)
        if request is None or request["status"] != "pending":
            await ctx.send("No pending drill request with that ID.")
            return

        await database.pool().execute(
            "UPDATE drill_requests SET status = 'denied', decided_by = $2, decided_at = now() WHERE id = $1",
            request_id,
            ctx.author.id,
        )
        await ctx.send(f"Declined drill request #{request_id}. No funds were moved.")

    # =====================================================================
    # LOAD / OFFLOAD / REFINE
    # =====================================================================

    @commands.command(name="load")
    async def load(self, ctx: commands.Context, item: str, qty: float):
        item = item.lower()
        if qty <= 0:
            await ctx.send("Quantity must be positive.")
            return
        if item == "crude":
            await self._load_crude(ctx, qty)
        elif item == "fuel":
            await self._load_fuel(ctx, qty)
        elif item == "gas":
            await ctx.send("Refined gas can't be loaded onto a tanker yet.")
        else:
            await ctx.send("Unknown item -- try `!load crude <qty>` or `!load fuel <qty>`.")

    @commands.command(name="offload")
    async def offload(self, ctx: commands.Context, item: str, qty: float):
        item = item.lower()
        if qty <= 0:
            await ctx.send("Quantity must be positive.")
            return
        if item == "crude":
            await self._offload_crude(ctx, qty)
        elif item == "fuel":
            await self._offload_fuel(ctx, qty)
        else:
            await ctx.send("Unknown item -- try `!offload crude <qty>` or `!offload fuel <qty>`.")

    @commands.command(name="refine")
    async def refine(self, ctx: commands.Context, item: str, qty: float):
        if item.lower() != "crude":
            await ctx.send("You can only `!refine crude <qty>`.")
            return
        if qty <= 0:
            await ctx.send("Quantity must be positive.")
            return
        await self._refine_crude(ctx, qty)

    async def _load_crude(self, ctx: commands.Context, qty: float):
        location = await database.get_location_by_channel(ctx.channel.id)
        if location is None or location["state"] != OIL_STATE or location["channel_name"] != OIL_WELL_CHANNEL:
            await ctx.send("Crude can only be loaded at the Delta oil-well.")
            return
        state = await member_commissioner_state(ctx.author, "Petroleum")
        if state != "Delta":
            await ctx.send("Only the Delta Commissioner of Petroleum can load crude.")
            return

        well = await self._get_or_create_well(OIL_STATE)
        if float(well["crude_barrels"]) < qty:
            await ctx.send(f"The oil-well only has {float(well['crude_barrels']):,.0f} barrels in stock.")
            return

        trailer = await database.pool().fetchrow(
            """
            SELECT * FROM state_vehicles
            WHERE current_location_id = $1 AND vehicle_type = 'trailer'
              AND status = 'idle' AND (cargo_type IS NULL OR cargo_type = 'crude')
            ORDER BY id LIMIT 1
            """,
            location["id"],
        )
        if trailer is None:
            await ctx.send("No idle trailer is parked here to load.")
            return

        room = TRAILER_CAP_BARRELS - float(trailer["cargo_qty"])
        if qty > room:
            await ctx.send(f"That trailer only has room for {room:,.0f} more barrels.")
            return

        await database.pool().execute(
            "UPDATE oil_well_stock SET crude_barrels = crude_barrels - $2 WHERE state = $1", OIL_STATE, qty
        )
        await database.pool().execute(
            "UPDATE state_vehicles SET cargo_type = 'crude', cargo_qty = cargo_qty + $2 WHERE id = $1",
            trailer["id"],
            qty,
        )
        name = _vehicle_name(trailer["owner_state"], "trailer", trailer["unit_number"])
        await ctx.send(f"Loaded {qty:,.0f} barrels onto {name}.")

    async def _offload_crude(self, ctx: commands.Context, qty: float):
        location = await database.get_location_by_channel(ctx.channel.id)
        if location is None or location["channel_name"] != REFINERY_CHANNEL:
            await ctx.send("Crude can only be offloaded at a refinery.")
            return
        state = await member_commissioner_state(ctx.author, "Petroleum")
        if state is None or state.upper() != location["state"]:
            await ctx.send("Only that state's Commissioner of Petroleum can offload here.")
            return

        trailer = await database.pool().fetchrow(
            """
            SELECT * FROM state_vehicles
            WHERE current_location_id = $1 AND vehicle_type = 'trailer'
              AND status = 'idle' AND cargo_type = 'crude'
            ORDER BY id LIMIT 1
            """,
            location["id"],
        )
        if trailer is None or float(trailer["cargo_qty"]) < qty:
            await ctx.send("No trailer here is carrying that much crude.")
            return

        refinery = await self._get_or_create_refinery(location["state"])
        new_qty = float(trailer["cargo_qty"]) - qty
        await database.pool().execute(
            "UPDATE state_vehicles SET cargo_qty = $2, cargo_type = CASE WHEN $2 = 0 THEN NULL ELSE cargo_type END WHERE id = $1",
            trailer["id"],
            new_qty,
        )
        added = min(qty, CRUDE_CAP_BARRELS - float(refinery["crude_barrels"]))
        await database.pool().execute(
            "UPDATE refinery_stock SET crude_barrels = crude_barrels + $2 WHERE state = $1",
            location["state"],
            added,
        )
        note = "" if added >= qty else f" ({qty - added:,.0f} barrels overflowed and were lost -- crude storage here is capped)"
        name = _vehicle_name(trailer["owner_state"], "trailer", trailer["unit_number"])
        await ctx.send(f"Offloaded {qty:,.0f} barrels from {name} into the refinery.{note}")

    async def _refine_crude(self, ctx: commands.Context, qty: float):
        location = await database.get_location_by_channel(ctx.channel.id)
        if location is None or location["channel_name"] != REFINERY_CHANNEL:
            await ctx.send("You can only refine at a refinery.")
            return
        state = await member_commissioner_state(ctx.author, "Petroleum")
        if state is None or state.upper() != location["state"]:
            await ctx.send("Only that state's Commissioner of Petroleum can refine here.")
            return

        refinery = await self._get_or_create_refinery(location["state"])
        if refinery["refining_ready_at"] is not None:
            await ctx.send("A batch is already refining here.")
            return
        if float(refinery["crude_barrels"]) < qty:
            await ctx.send(f"Only {float(refinery['crude_barrels']):,.0f} barrels of crude are in storage.")
            return

        ready_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
            minutes=qty * REFINE_MINUTES_PER_BARREL
        )
        await database.pool().execute(
            "UPDATE refinery_stock SET crude_barrels = crude_barrels - $2, refining_barrels = $2, refining_ready_at = $3 WHERE state = $1",
            location["state"],
            qty,
            ready_at,
        )
        await ctx.send(f"Refining {qty:,.0f} barrels -- ready in {int(qty * REFINE_MINUTES_PER_BARREL)} minute(s).")

    async def _load_fuel(self, ctx: commands.Context, qty: float):
        location = await database.get_location_by_channel(ctx.channel.id)
        if location is None or location["channel_name"] != REFINERY_CHANNEL:
            await ctx.send("Fuel can only be loaded at a refinery.")
            return
        state = await member_commissioner_state(ctx.author, "Petroleum")
        if state is None or state.upper() != location["state"]:
            await ctx.send("Only that state's Commissioner of Petroleum can load fuel here.")
            return

        refinery = await self._get_or_create_refinery(location["state"])
        if float(refinery["fuel_litres"]) < qty:
            await ctx.send(f"Only {float(refinery['fuel_litres']):,.0f}L of fuel is in storage.")
            return

        tanker = await database.pool().fetchrow(
            """
            SELECT * FROM state_vehicles
            WHERE current_location_id = $1 AND vehicle_type = 'tanker'
              AND status = 'idle' AND (cargo_type IS NULL OR cargo_type = 'fuel')
            ORDER BY id LIMIT 1
            """,
            location["id"],
        )
        if tanker is None:
            await ctx.send("No idle tanker is parked here to load.")
            return

        room = TANKER_CAP_LITRES - float(tanker["cargo_qty"])
        if qty > room:
            await ctx.send(f"That tanker only has room for {room:,.0f} more litres.")
            return

        await database.pool().execute(
            "UPDATE refinery_stock SET fuel_litres = fuel_litres - $2 WHERE state = $1", location["state"], qty
        )
        await database.pool().execute(
            "UPDATE state_vehicles SET cargo_type = 'fuel', cargo_qty = cargo_qty + $2 WHERE id = $1",
            tanker["id"],
            qty,
        )
        name = _vehicle_name(tanker["owner_state"], "tanker", tanker["unit_number"])
        await ctx.send(f"Loaded {qty:,.0f}L of fuel onto {name}.")

    async def _offload_fuel(self, ctx: commands.Context, qty: float):
        location = await database.get_location_by_channel(ctx.channel.id)
        if location is None or location["channel_name"] != NNPC_CHANNEL:
            await ctx.send("Fuel can only be offloaded at an NNPC station.")
            return
        state = await member_commissioner_state(ctx.author, "Petroleum")
        if state is None or state.upper() != location["state"]:
            await ctx.send("Only that state's Commissioner of Petroleum can offload here.")
            return

        tanker = await database.pool().fetchrow(
            """
            SELECT * FROM state_vehicles
            WHERE current_location_id = $1 AND vehicle_type = 'tanker'
              AND status = 'idle' AND cargo_type = 'fuel'
            ORDER BY id LIMIT 1
            """,
            location["id"],
        )
        if tanker is None or float(tanker["cargo_qty"]) < qty:
            await ctx.send("No tanker here is carrying that much fuel.")
            return

        new_qty = float(tanker["cargo_qty"]) - qty
        await database.pool().execute(
            "UPDATE state_vehicles SET cargo_qty = $2, cargo_type = CASE WHEN $2 = 0 THEN NULL ELSE cargo_type END WHERE id = $1",
            tanker["id"],
            new_qty,
        )
        await database.pool().execute(
            """
            INSERT INTO fuel_stations (state, stock_litres) VALUES ($1, $2)
            ON CONFLICT (state) DO UPDATE SET stock_litres = fuel_stations.stock_litres + EXCLUDED.stock_litres
            """,
            location["state"],
            qty,
        )
        name = _vehicle_name(tanker["owner_state"], "tanker", tanker["unit_number"])
        await ctx.send(f"Offloaded {qty:,.0f}L of fuel from {name} into the NNPC station.")

    # =====================================================================
    # PURCHASE
    # =====================================================================

    @commands.command(name="buy-trailer")
    async def buy_trailer(self, ctx: commands.Context):
        await self._buy_vehicle(ctx, "trailer")

    @commands.command(name="buy-tanker")
    async def buy_tanker(self, ctx: commands.Context):
        await self._buy_vehicle(ctx, "tanker")

    async def _buy_vehicle(self, ctx: commands.Context, vehicle_type: str):
        owner_state = await _ordering_authority(ctx.author)
        if owner_state is None:
            await ctx.send(
                "Only a Commissioner of Commerce (their own state) or the Minister of "
                "Trade and Commerce (Nigeria) can buy a trailer or tanker."
            )
            return

        home = await self._home_location(owner_state)
        if home is None:
            await ctx.send("No depot is set up to park it at yet.")
            return

        price = VEHICLE_PRICES[vehicle_type]
        request_id = await database.pool().fetchval(
            """
            INSERT INTO vehicle_purchase_requests (owner_state, vehicle_type, requested_by, cost)
            VALUES ($1, $2, $3, $4)
            RETURNING id
            """,
            owner_state,
            vehicle_type,
            ctx.author.id,
            price,
        )
        approver = (
            "the Minister of Finance"
            if owner_state == "NIGERIA"
            else f"the {owner_state.title()} Commissioner of Finance"
        )
        await ctx.send(
            f"Purchase request #{request_id} submitted for a {vehicle_type} at {price:,.2f} -- "
            f"awaiting {approver}'s approval."
        )

    async def _finance_authorized(self, member: discord.Member, owner_state: str) -> bool:
        if owner_state == "NIGERIA":
            return await _member_has_role(member, "Minister of Finance")
        state = await member_commissioner_state(member, "Finance")
        return state is not None and state.upper() == owner_state

    @commands.command(name="approve-vehicle")
    async def approve_vehicle(self, ctx: commands.Context, request_id: int):
        request = await database.pool().fetchrow(
            "SELECT * FROM vehicle_purchase_requests WHERE id = $1", request_id
        )
        if request is None or request["status"] != "pending":
            await ctx.send("No pending purchase request with that ID.")
            return

        owner_state = request["owner_state"]
        if not await self._finance_authorized(ctx.author, owner_state):
            label = (
                "Minister of Finance"
                if owner_state == "NIGERIA"
                else f"{owner_state.title()} Commissioner of Finance"
            )
            await ctx.send(f"Only the {label} can approve this purchase.")
            return

        price = float(request["cost"])
        try:
            if owner_state == "NIGERIA":
                await debit_national_treasury(price)
            else:
                await debit_treasury(owner_state, price)
        except InsufficientTreasuryFunds as e:
            await ctx.send(str(e))
            return

        vehicle_type = request["vehicle_type"]
        home = await self._home_location(owner_state)
        if home is None:
            await ctx.send("No depot is set up to park it at yet -- funds were not moved.")
            return

        unit_number = await database.pool().fetchval(
            "SELECT COALESCE(MAX(unit_number), 0) + 1 FROM state_vehicles WHERE owner_state = $1 AND vehicle_type = $2",
            owner_state,
            vehicle_type,
        )
        await database.pool().execute(
            """
            INSERT INTO state_vehicles
                (owner_state, vehicle_type, unit_number, current_location_id, purchased_by, price_paid)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            owner_state,
            vehicle_type,
            unit_number,
            home["id"],
            request["requested_by"],
            price,
        )
        await database.pool().execute(
            "UPDATE vehicle_purchase_requests SET status = 'approved', decided_by = $2, decided_at = now() WHERE id = $1",
            request_id,
            ctx.author.id,
        )
        name = _vehicle_name(owner_state, vehicle_type, unit_number)
        await ctx.send(
            f"Approved. Purchased {name} for {price:,.2f} -- parked at {home['state'].title()} {home['channel_name']}."
        )

    @commands.command(name="decline-vehicle")
    async def decline_vehicle(self, ctx: commands.Context, request_id: int):
        request = await database.pool().fetchrow(
            "SELECT * FROM vehicle_purchase_requests WHERE id = $1", request_id
        )
        if request is None or request["status"] != "pending":
            await ctx.send("No pending purchase request with that ID.")
            return

        owner_state = request["owner_state"]
        if not await self._finance_authorized(ctx.author, owner_state):
            label = (
                "Minister of Finance"
                if owner_state == "NIGERIA"
                else f"{owner_state.title()} Commissioner of Finance"
            )
            await ctx.send(f"Only the {label} can decline this purchase.")
            return

        await database.pool().execute(
            "UPDATE vehicle_purchase_requests SET status = 'denied', decided_by = $2, decided_at = now() WHERE id = $1",
            request_id,
            ctx.author.id,
        )
        await ctx.send(f"Declined purchase request #{request_id}. No funds were moved.")

    # =====================================================================
    # ORDER / HIRE
    # =====================================================================

    @commands.command(name="order-trailer")
    async def order_trailer(self, ctx: commands.Context, fleet: str = "own", *, destination: str = None):
        await self._order_vehicle(ctx, "trailer", fleet, destination)

    @commands.command(name="order-tanker")
    async def order_tanker(self, ctx: commands.Context, fleet: str = "own", *, destination: str = None):
        await self._order_vehicle(ctx, "tanker", fleet, destination)

    async def _resolve_destination(self, text: str):
        """Parses a typed '<state> <channel-name>' destination from the
        phone's Order Trailer/Tanker modal -- 'Lagos refinery' or 'lagos
        nnpc fuel station' both work; spaces in the channel part are
        tried both as-is and hyphenated."""
        parts = text.strip().split(None, 1)
        if len(parts) < 2:
            return None
        state = parts[0].upper()
        channel_raw = parts[1].strip().lower()
        for channel_name in {channel_raw, channel_raw.replace(" ", "-")}:
            location = await database.pool().fetchrow(
                "SELECT * FROM locations WHERE state = $1 AND channel_name = $2",
                state,
                channel_name,
            )
            if location is not None:
                return location
        return None

    async def _order_vehicle(self, ctx: commands.Context, vehicle_type: str, fleet: str, destination_text: str = None):
        if not getattr(ctx, "from_phone", False):
            await ctx.send(
                "Order a trailer/tanker from your phone -- `!phone` -> Dispatch -> "
                "Order Trailer/Tanker -- not as a typed command."
            )
            return

        if not destination_text:
            await ctx.send("You need to enter a destination -- `<state> <channel-name>`, e.g. `lagos refinery`.")
            return

        destination = await self._resolve_destination(destination_text)
        if destination is None:
            await ctx.send(
                "Couldn't find that destination -- try `<state> <channel-name>`, e.g. `lagos refinery`."
            )
            return

        authority_state = await _ordering_authority(ctx.author)
        if authority_state is None:
            await ctx.send(
                "Only a Commissioner of Commerce or the Minister of Trade and Commerce can order a trailer or tanker."
            )
            return

        fleet = fleet.strip().lower()
        if fleet == "own":
            if authority_state == "NIGERIA":
                await ctx.send("The Minister of Trade and Commerce should order with `nigeria` as the fleet.")
                return
            owner_state = authority_state
        elif fleet == "nigeria":
            owner_state = "NIGERIA"
        else:
            owner_state = fleet.upper()
            if owner_state not in VALID_OWNER_STATES:
                await ctx.send("Unknown fleet -- try `own`, `nigeria`, or a state name.")
                return
            if owner_state == authority_state:
                await ctx.send("That's your own fleet -- just use `own`.")
                return

        vehicle = await database.pool().fetchrow(
            "SELECT * FROM state_vehicles WHERE owner_state = $1 AND vehicle_type = $2 AND status = 'idle' ORDER BY id LIMIT 1",
            owner_state,
            vehicle_type,
        )
        if vehicle is None:
            await ctx.send(f"No idle {('Nigeria' if owner_state == 'NIGERIA' else owner_state.title())}-owned {vehicle_type} is available.")
            return

        origin = await database.pool().fetchrow("SELECT * FROM locations WHERE id = $1", vehicle["current_location_id"])
        if origin["id"] == destination["id"]:
            await ctx.send(f"{_vehicle_name(owner_state, vehicle_type, vehicle['unit_number'])} is already here.")
            return

        try:
            distance_km, is_interstate = await self._route_distance(origin, destination)
        except VehicleOrderError as e:
            await ctx.send(str(e))
            return

        if owner_state == authority_state and not is_interstate:
            fare = 0.0
            requires_approval = False
        elif owner_state == "NIGERIA" and not is_interstate:
            fare = HIRE_FLAT_NIGERIA_INTRASTATE[vehicle_type] + distance_km * HIRE_PER_KM[vehicle_type]
            requires_approval = True
        else:
            fare = HIRE_FLAT_INTERSTATE[vehicle_type] + distance_km * HIRE_PER_KM[vehicle_type]
            requires_approval = True

        if not requires_approval:
            if fare > 0:
                try:
                    if authority_state == "NIGERIA":
                        await debit_national_treasury(fare)
                    else:
                        await debit_treasury(authority_state, fare)
                except InsufficientTreasuryFunds as e:
                    await ctx.send(str(e))
                    return
                if owner_state == "NIGERIA":
                    await credit_national_treasury(fare)
                else:
                    await credit_ministry_of_commerce(owner_state, fare)
            await self._dispatch_vehicle(vehicle, origin, destination, fare, distance_km)
            fare_note = f" Fare: {fare:,.2f}." if fare else " No charge -- your own state's vehicle."
            await ctx.send(f"{_vehicle_name(owner_state, vehicle_type, vehicle['unit_number'])} is on its way.{fare_note}")
            return

        request_id = await database.pool().fetchval(
            """
            INSERT INTO vehicle_hire_requests
                (vehicle_id, requested_by, requester_state, destination_location_id, distance_km, fare)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING id
            """,
            vehicle["id"],
            ctx.author.id,
            authority_state,
            destination["id"],
            distance_km,
            fare,
        )
        await database.pool().execute("UPDATE state_vehicles SET status = 'pending' WHERE id = $1", vehicle["id"])
        await self._post_hire_request(request_id, owner_state, vehicle, destination, fare, distance_km, ctx.author)
        owner_label = "Nigeria" if owner_state == "NIGERIA" else owner_state.title()
        await ctx.send(
            f"Hire request #{request_id} for {_vehicle_name(owner_state, vehicle_type, vehicle['unit_number'])} "
            f"sent to {owner_label}'s Ministry of Commerce -- fare {fare:,.2f}, awaiting approval."
        )

    async def _route_distance(self, origin, destination) -> tuple[float, bool]:
        """Returns (distance_km, is_interstate). Mirrors cars.py's own
        intrastate (straight-line) / interstate (checkpoint -> fixed leg
        -> checkpoint) distance logic so a haul costs the same km a
        private car trip between the same two points would."""
        if origin["state"] == destination["state"]:
            return await cars_module._get_distance_km(origin, destination), False

        leg = await cars_module._get_interstate_leg(origin["state"], destination["state"])
        if leg is None:
            raise VehicleOrderError("No interstate route exists between those states.")

        origin_cp = await cars_module._get_location(
            origin["state"], cars_module.ROAD_CHECKPOINT_CATEGORY, cars_module.ROAD_CHECKPOINT_CHANNEL
        )
        dest_cp = await cars_module._get_location(
            destination["state"], cars_module.ROAD_CHECKPOINT_CATEGORY, cars_module.ROAD_CHECKPOINT_CHANNEL
        )
        leg1 = await cars_module._get_distance_km(origin, origin_cp) if origin_cp else 0.0
        leg2 = await cars_module._get_distance_km(dest_cp, destination) if dest_cp else 0.0
        return leg1 + float(leg["distance_km"]) + leg2, True

    async def _dispatch_vehicle(self, vehicle, origin, destination, fare: float, distance_km: float) -> None:
        travel_seconds = (distance_km / HAUL_SPEED_KMH) * 3600 if distance_km > 0 else 30
        arrives_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=travel_seconds)
        await database.pool().execute(
            """
            INSERT INTO vehicle_trips (vehicle_id, origin_location_id, destination_location_id, fare_paid, arrives_at)
            VALUES ($1, $2, $3, $4, $5)
            """,
            vehicle["id"],
            origin["id"],
            destination["id"],
            fare,
            arrives_at,
        )
        await database.pool().execute("UPDATE state_vehicles SET status = 'en_route' WHERE id = $1", vehicle["id"])

        name = _vehicle_name(vehicle["owner_state"], vehicle["vehicle_type"], vehicle["unit_number"])
        if origin["channel_id"]:
            channel = self.bot.get_channel(origin["channel_id"])
            if channel is not None:
                await channel.send(f"{name} departing for {destination['state'].title()} {destination['channel_name']}...")

    async def _post_hire_request(self, request_id, owner_state, vehicle, destination, fare, distance_km, requester) -> None:
        ministry = await self._ministry_of_commerce_location(owner_state)
        if ministry is None or not ministry["channel_id"]:
            return
        channel = self.bot.get_channel(ministry["channel_id"])
        if channel is None:
            return

        name = _vehicle_name(owner_state, vehicle["vehicle_type"], vehicle["unit_number"])
        embed = discord.Embed(
            title=f"Hire Request #{request_id}",
            description=(
                f"{requester.mention} wants to hire **{name}** to "
                f"{destination['state'].title()} {destination['channel_name']}.\n"
                f"Distance: {distance_km:,.1f}km\n"
                f"Fare: **{fare:,.2f}**\n\n"
                f"Approve only after payment has been confirmed."
            ),
            color=discord.Color.gold(),
        )
        view = HireDecisionView(request_id)
        await channel.send(embed=embed, view=view)

    async def _hire_authorized(self, member: discord.Member, owner_state: str) -> bool:
        if owner_state == "NIGERIA":
            return await _member_has_role(member, "Minister of Trade and Commerce")
        state = await member_commissioner_state(member, "Commerce")
        return state is not None and state.upper() == owner_state

    async def handle_hire_decision(self, interaction: discord.Interaction, request_id: int, approve: bool) -> None:
        request = await database.pool().fetchrow("SELECT * FROM vehicle_hire_requests WHERE id = $1", request_id)
        if request is None or request["status"] != "pending":
            await interaction.response.send_message("That request has already been handled.", ephemeral=True)
            return

        vehicle = await database.pool().fetchrow("SELECT * FROM state_vehicles WHERE id = $1", request["vehicle_id"])
        owner_state = vehicle["owner_state"]
        if not await self._hire_authorized(interaction.user, owner_state):
            await interaction.response.send_message("You're not authorized to decide this request.", ephemeral=True)
            return

        if not approve:
            await database.pool().execute(
                "UPDATE vehicle_hire_requests SET status = 'denied', decided_by = $2, decided_at = now() WHERE id = $1",
                request_id,
                interaction.user.id,
            )
            await database.pool().execute("UPDATE state_vehicles SET status = 'idle' WHERE id = $1", vehicle["id"])
            await interaction.response.edit_message(
                content=f"\u274c Hire request #{request_id} declined.", embed=None, view=None
            )
            return

        fare = float(request["fare"])
        try:
            if request["requester_state"] == "NIGERIA":
                await debit_national_treasury(fare)
            else:
                await debit_treasury(request["requester_state"], fare)
        except InsufficientTreasuryFunds as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return

        if owner_state == "NIGERIA":
            await credit_national_treasury(fare)
        else:
            await credit_ministry_of_commerce(owner_state, fare)

        origin = await database.pool().fetchrow("SELECT * FROM locations WHERE id = $1", vehicle["current_location_id"])
        destination = await database.pool().fetchrow(
            "SELECT * FROM locations WHERE id = $1", request["destination_location_id"]
        )
        await self._dispatch_vehicle(vehicle, origin, destination, fare, float(request["distance_km"]))
        await database.pool().execute(
            "UPDATE vehicle_hire_requests SET status = 'approved', decided_by = $2, decided_at = now() WHERE id = $1",
            request_id,
            interaction.user.id,
        )
        await interaction.response.edit_message(
            content=f"\u2705 Hire request #{request_id} approved. Vehicle dispatched.", embed=None, view=None
        )

    # =====================================================================
    # BACKGROUND TICK -- drilling / refining / vehicle arrivals
    # =====================================================================

    @tasks.loop(seconds=TICK_SECONDS)
    async def petro_tick(self):
        now = datetime.datetime.now(datetime.timezone.utc)

        ready_wells = await database.pool().fetch(
            "SELECT * FROM oil_well_stock WHERE drilling_ready_at IS NOT NULL AND drilling_ready_at <= now()"
        )
        for well in ready_wells:
            new_total = min(float(well["crude_barrels"]) + DRILL_YIELD_BARRELS, CRUDE_CAP_BARRELS)
            await database.pool().execute(
                "UPDATE oil_well_stock SET crude_barrels = $2, drilling_ready_at = NULL, cooldown_until = $3 WHERE state = $1",
                well["state"],
                new_total,
                now + DRILL_COOLDOWN,
            )
            await self._announce(
                well["state"], OIL_WELL_CHANNEL, f"Drilling complete -- crude stock now {new_total:,.0f}/{CRUDE_CAP_BARRELS} barrels."
            )

        ready_refineries = await database.pool().fetch(
            "SELECT * FROM refinery_stock WHERE refining_ready_at IS NOT NULL AND refining_ready_at <= now()"
        )
        for refinery in ready_refineries:
            barrels = float(refinery["refining_barrels"] or 0)
            fuel_add = max(min(barrels * BARREL_TO_FUEL_LITRES, FUEL_CAP_LITRES - float(refinery["fuel_litres"])), 0)
            gas_add = max(min(barrels * BARREL_TO_GAS_KG, GAS_CAP_KG - float(refinery["gas_kg"])), 0)
            await database.pool().execute(
                "UPDATE refinery_stock SET fuel_litres = fuel_litres + $2, gas_kg = gas_kg + $3, refining_barrels = NULL, refining_ready_at = NULL WHERE state = $1",
                refinery["state"],
                fuel_add,
                gas_add,
            )
            note = ""
            if fuel_add < barrels * BARREL_TO_FUEL_LITRES or gas_add < barrels * BARREL_TO_GAS_KG:
                note = " (some output was lost -- refinery storage is at capacity)"
            await self._announce(
                refinery["state"], REFINERY_CHANNEL, f"Refining complete -- +{fuel_add:,.0f}L fuel, +{gas_add:,.0f}kg gas.{note}"
            )

        trips = await database.pool().fetch("SELECT * FROM vehicle_trips WHERE arrives_at <= now()")
        for trip in trips:
            await self._resolve_trip(trip)

    @petro_tick.before_loop
    async def before_petro_tick(self):
        await self.bot.wait_until_ready()

    async def _resolve_trip(self, trip) -> None:
        vehicle = await database.pool().fetchrow("SELECT * FROM state_vehicles WHERE id = $1", trip["vehicle_id"])
        destination = await database.pool().fetchrow("SELECT * FROM locations WHERE id = $1", trip["destination_location_id"])

        await database.pool().execute(
            "UPDATE state_vehicles SET status = 'idle', current_location_id = $2 WHERE id = $1",
            trip["vehicle_id"],
            destination["id"],
        )
        await database.pool().execute("DELETE FROM vehicle_trips WHERE id = $1", trip["id"])

        name = _vehicle_name(vehicle["owner_state"], vehicle["vehicle_type"], vehicle["unit_number"])
        if destination["channel_id"]:
            channel = self.bot.get_channel(destination["channel_id"])
            if channel is not None:
                await channel.send(f"{name} arriving at {destination['state'].title()} {destination['channel_name']}...")


async def setup(bot: commands.Bot):
    await bot.add_cog(Petroleum(bot))

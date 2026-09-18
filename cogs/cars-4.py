"""
Private cars: dealership (!car / !buy-car / !use-car), driving (!drive),
fuel (!refuel / !set-fuel-price / !restock-fuel).

DESIGN NOTES (read before changing numbers):

    Distance is in km, speed in km/h. Every seeded location has a real
    (x_km, y_km) position (location_coordinates, see
    location_coordinates_seed.sql for how it's generated); cogs/cars.py
    computes the straight-line distance between any two same-state
    locations from those coordinates, so every pair gets a genuine
    distance, not a zone-level bucket. `location_distances` is only a
    manual override for a specific pair, checked first, for whenever a
    straight line is wrong (a river with no bridge, etc). Farthest
    intrastate pair in any state is 2.95km, so a standard car (60 km/h)
    at full condition covers the farthest possible intrastate trip in
    2.95 minutes -- just under the 3-minute cap.

    Condition (0-100) degrades by `distance_km * tier.wear_pct_per_km` per
    trip. BREAKDOWN_THRESHOLD (20) is the floor: a car at or below it can't
    start a new trip, and a trip that would push condition to/below it while
    en route breaks down instead of arriving. Degraded condition also
    scales performance continuously (see effective_speed / effective_fuel_use
    below) -- a beat-up car is slower and thirstier well before it actually
    breaks down.

    A trip can also run out of fuel before arriving. Both cases are handled
    identically: the trip is flagged will_break_down at !drive time (we
    already know, from current fuel/condition, whether it can finish), the
    background tick still takes the same amount of time to resolve as
    however far the car actually got, and then instead of arriving, the
    player is dropped at that trip's breakdown_state's Auto Repair channel
    with a pending repair_requests row waiting for a (not-yet-built)
    Mechanic role/command.

    Interstate trips are three legs: origin -> home state's road-checkpoint
    (distance-based, like any other intrastate leg) -> the fixed interstate
    leg from `interstate_routes` -> destination state's road-checkpoint ->
    final destination (distance-based again). One cooldown
    (players.interstate_cooldown_until) blocks ALL interstate driving for
    that player, not just the pair just driven, and is set the moment the
    trip departs.
"""

import datetime
import math

import discord
from discord.ext import commands, tasks

import database
import discord_utils
from cogs.transportation import (
    credit_ministry_of_commerce,
    member_commissioner_state,
)

TICK_SECONDS = 5

# Safety net only -- every seeded location has a location_coordinates row
# (see location_coordinates_seed.sql), so this should never actually be hit
# except for a location added later without one.
FALLBACK_DISTANCE_KM = 2.0

BREAKDOWN_THRESHOLD = 20  # condition points; at/below this a car can't drive, or a trip breaks down instead of arriving

MAX_VEHICLES_PER_PLAYER = 2

DEALERSHIP_CATEGORY = "BUSINESS & COMMERCE"
DEALERSHIP_CHANNEL = "dealership"
NNPC_CHANNEL = "nnpc-fuel-station"
AUTO_REPAIR_CHANNEL = "auto-repair"
ROAD_CHECKPOINT_CATEGORY = "BORDER & ENTRY"
ROAD_CHECKPOINT_CHANNEL = "road-checkpoint"


# ---------------------------------------------------------------------------
# Small standalone helpers
# ---------------------------------------------------------------------------

async def _get_location(state: str, category: str, channel_name: str):
    return await database.pool().fetchrow(
        "SELECT * FROM locations WHERE state = $1 AND category = $2 AND channel_name = $3",
        state.upper(),
        category,
        channel_name,
    )


async def _get_distance_km(loc_a, loc_b) -> float:
    """Real point-to-point distance in km. A location_distances override
    takes priority if one exists for this pair; otherwise it's the
    Euclidean distance between the two locations' location_coordinates rows
    -- every seeded location has one, so this is a genuine per-location
    distance, not a same-zone/different-zone bucket."""
    if loc_a["id"] == loc_b["id"]:
        return 0.0

    override = await database.pool().fetchrow(
        """
        SELECT distance_km FROM location_distances
        WHERE (location_a_id = $1 AND location_b_id = $2)
           OR (location_a_id = $2 AND location_b_id = $1)
        """,
        loc_a["id"],
        loc_b["id"],
    )
    if override is not None:
        return float(override["distance_km"])

    coords = await database.pool().fetch(
        "SELECT location_id, x_km, y_km FROM location_coordinates WHERE location_id = ANY($1::int[])",
        [loc_a["id"], loc_b["id"]],
    )
    by_id = {row["location_id"]: row for row in coords}
    coord_a, coord_b = by_id.get(loc_a["id"]), by_id.get(loc_b["id"])
    if coord_a is None or coord_b is None:
        return FALLBACK_DISTANCE_KM

    dx = float(coord_a["x_km"]) - float(coord_b["x_km"])
    dy = float(coord_a["y_km"]) - float(coord_b["y_km"])
    return math.hypot(dx, dy)


async def _get_interstate_leg(state_a: str, state_b: str):
    a, b = sorted((state_a.upper(), state_b.upper()))
    return await database.pool().fetchrow(
        "SELECT * FROM interstate_routes WHERE state_a = $1 AND state_b = $2", a, b
    )


async def _get_vehicle_with_tier(vehicle_id: int):
    """player_vehicles row joined with its model's tier stats."""
    return await database.pool().fetchrow(
        """
        SELECT pv.*, vm.tier, vm.price, vt.speed_kmh, vt.fuel_use_l_per_km,
               vt.wear_pct_per_km, vt.tank_capacity_l
        FROM player_vehicles pv
        JOIN vehicle_models vm ON vm.name = pv.model_name
        JOIN vehicle_tiers vt ON vt.tier = vm.tier
        WHERE pv.id = $1
        """,
        vehicle_id,
    )


def _effective_speed(vehicle) -> float:
    condition_ratio = float(vehicle["condition"]) / 100
    return float(vehicle["speed_kmh"]) * (0.5 + 0.5 * condition_ratio)


def _effective_fuel_use(vehicle) -> float:
    condition_ratio = float(vehicle["condition"]) / 100
    return float(vehicle["fuel_use_l_per_km"]) * (2 - condition_ratio)


def _format_duration(seconds: float) -> str:
    seconds = max(int(round(seconds)), 0)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


class VehicleError(Exception):
    """Raised for any driving/fuel/purchase failure that should just be
    shown to the player as a plain message."""
    pass


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class Cars(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.car_tick.start()

    def cog_unload(self):
        self.car_tick.cancel()

    async def _log_transaction(self, state: str, message: str) -> None:
        channel_id = await database.get_transaction_log_channel_id(state)
        channel = self.bot.get_channel(channel_id) if channel_id else None
        if channel:
            await channel.send(message)

    # -- !car -----------------------------------------------------------

    @commands.command(name="car")
    async def car(self, ctx: commands.Context):
        location = await database.get_location_by_channel(ctx.channel.id)
        if location is None or location["category"] != DEALERSHIP_CATEGORY or location["channel_name"] != DEALERSHIP_CHANNEL:
            await ctx.send("You need to be at a dealership to see what's for sale.")
            return

        rows = await database.pool().fetch(
            """
            SELECT vm.name, vm.tier, vm.price, vt.speed_kmh, vt.fuel_use_l_per_km,
                   vt.wear_pct_per_km, vt.tank_capacity_l
            FROM vehicle_models vm
            JOIN vehicle_tiers vt ON vt.tier = vm.tier
            WHERE vm.active
            ORDER BY vm.price
            """
        )
        if not rows:
            await ctx.send("Nothing on the lot yet -- check back soon.")
            return

        lines = ["**Dealership**"]
        for r in rows:
            lines.append(
                f"**{r['name']}** ({r['tier'].title()}) -- \u20a6{float(r['price']):,.2f}\n"
                f"  Speed: {float(r['speed_kmh']):.0f} km/h | "
                f"Fuel use: {float(r['fuel_use_l_per_km']):.2f} L/km | "
                f"Wear: {float(r['wear_pct_per_km']):.2f}%/km | "
                f"Tank: {float(r['tank_capacity_l']):.0f}L"
            )
        await ctx.send("\n".join(lines))

    # -- !buy-car ---------------------------------------------------------

    @commands.command(name="buy-car")
    async def buy_car(self, ctx: commands.Context, *, car_name: str):
        location = await database.get_location_by_channel(ctx.channel.id)
        if location is None or location["category"] != DEALERSHIP_CATEGORY or location["channel_name"] != DEALERSHIP_CHANNEL:
            await ctx.send("You need to be at a dealership to buy a car.")
            return

        await database.ensure_player_exists(ctx.author.id)

        model = await database.pool().fetchrow(
            "SELECT * FROM vehicle_models WHERE lower(name) = lower($1) AND active", car_name
        )
        if model is None:
            await ctx.send(f"No car called `{car_name}` on the lot.")
            return

        owned_count = await database.pool().fetchval(
            "SELECT count(*) FROM player_vehicles WHERE owner_discord_id = $1", ctx.author.id
        )
        if owned_count >= MAX_VEHICLES_PER_PLAYER:
            await ctx.send(f"You already own {MAX_VEHICLES_PER_PLAYER} vehicles -- the max allowed.")
            return

        already_owned = await database.pool().fetchrow(
            "SELECT 1 FROM player_vehicles WHERE owner_discord_id = $1 AND lower(model_name) = lower($2)",
            ctx.author.id,
            car_name,
        )
        if already_owned is not None:
            await ctx.send(f"You already own a {model['name']}.")
            return

        price = float(model["price"])
        try:
            await database.debit_bank_account(ctx.author.id, price)
        except (database.NoBankAccount, database.InsufficientFunds) as e:
            await ctx.send(str(e))
            return

        await credit_ministry_of_commerce(location["state"], price)

        await database.pool().execute(
            """
            INSERT INTO player_vehicles (owner_discord_id, model_name, current_location_id)
            VALUES ($1, $2, $3)
            """,
            ctx.author.id,
            model["name"],
            location["id"],
        )

        await ctx.send(
            f"Sold! You now own a **{model['name']}**, parked right here. "
            f"Its tank is empty -- run `!use-car {model['name']}` then `!refuel` at an NNPC station."
        )
        await self._log_transaction(
            location["state"],
            f"Vehicle purchase: {ctx.author.mention} bought a {model['name']} for \u20a6{price:,.2f} "
            f"(narration: vehicle purchase). Credited to {location['state']} Ministry of Commerce.",
        )

    # -- !use-car -----------------------------------------------------------

    @commands.command(name="use-car")
    async def use_car(self, ctx: commands.Context, *, car_name: str):
        await database.ensure_player_exists(ctx.author.id)

        vehicle = await database.pool().fetchrow(
            "SELECT * FROM player_vehicles WHERE owner_discord_id = $1 AND lower(model_name) = lower($2)",
            ctx.author.id,
            car_name,
        )
        if vehicle is None:
            await ctx.send(f"You don't own a car called `{car_name}`.")
            return

        await database.pool().execute(
            "UPDATE players SET active_vehicle_id = $2 WHERE discord_id = $1",
            ctx.author.id,
            vehicle["id"],
        )

        location_note = ""
        if vehicle["current_location_id"] is not None:
            loc = await database.pool().fetchrow(
                "SELECT * FROM locations WHERE id = $1", vehicle["current_location_id"]
            )
            if loc is not None:
                location_note = f" It's currently parked at {loc['state'].title()} {loc['channel_name']}."

        status_note = " It's broken down and needs a mechanic before it can be driven." if vehicle["status"] == "broken_down" else ""

        await ctx.send(
            f"**{vehicle['model_name']}** is now your active car "
            f"({float(vehicle['condition']):.0f}% condition, {float(vehicle['fuel_level_l']):.1f}L fuel).{location_note}{status_note}"
        )

    # -- Commissioner of Petroleum: !set-fuel-price / !restock-fuel -----------

    @commands.command(name="set-fuel-price")
    async def set_fuel_price(self, ctx: commands.Context, price_per_litre: float):
        state = await member_commissioner_state(ctx.author, "Petroleum")
        if state is None:
            await ctx.send("Only a Commissioner of Petroleum can set the fuel price.")
            return
        if price_per_litre <= 0:
            await ctx.send("Price must be positive.")
            return

        await database.pool().execute(
            "UPDATE fuel_stations SET price_per_litre = $2 WHERE state = $1",
            state.upper(),
            price_per_litre,
        )
        await ctx.send(f"{state} NNPC price set to \u20a6{price_per_litre:,.2f}/L.")

    @commands.command(name="restock-fuel")
    async def restock_fuel(self, ctx: commands.Context, litres: float):
        """No treasury cost for now -- this is a placeholder until a real
        tanker-delivery system exists; it just sets the new stock directly."""
        state = await member_commissioner_state(ctx.author, "Petroleum")
        if state is None:
            await ctx.send("Only a Commissioner of Petroleum can restock an NNPC station.")
            return
        if litres <= 0:
            await ctx.send("Amount must be positive.")
            return

        new_stock = await database.pool().fetchval(
            "UPDATE fuel_stations SET stock_litres = stock_litres + $2 WHERE state = $1 RETURNING stock_litres",
            state.upper(),
            litres,
        )
        await ctx.send(f"{state} NNPC restocked by {litres:,.2f}L -- now at {float(new_stock):,.2f}L.")

    # -- !refuel -----------------------------------------------------------

    @commands.command(name="refuel")
    async def refuel(self, ctx: commands.Context, litres: float):
        location = await database.get_location_by_channel(ctx.channel.id)
        if location is None or location["category"] != DEALERSHIP_CATEGORY or location["channel_name"] != NNPC_CHANNEL:
            await ctx.send("You need to be at an NNPC station to refuel.")
            return
        if litres <= 0:
            await ctx.send("Litres must be positive.")
            return

        await database.ensure_player_exists(ctx.author.id)

        player = await database.get_player(ctx.author.id)
        if player["active_vehicle_id"] is None:
            await ctx.send("You don't have an active car. Use `!use-car <name>` first.")
            return

        vehicle = await _get_vehicle_with_tier(player["active_vehicle_id"])
        if vehicle["status"] == "broken_down":
            await ctx.send("Your car is broken down and needs a mechanic before it can be refueled.")
            return
        if vehicle["current_location_id"] != location["id"]:
            await ctx.send("Your car isn't here -- bring it to this NNPC station first.")
            return

        room_left = float(vehicle["tank_capacity_l"]) - float(vehicle["fuel_level_l"])
        if litres > room_left:
            await ctx.send(f"Your tank only has room for {room_left:.1f}L more.")
            return

        fuel_station = await database.pool().fetchrow(
            "SELECT * FROM fuel_stations WHERE state = $1", location["state"]
        )
        if fuel_station is None or float(fuel_station["stock_litres"]) < litres:
            await ctx.send("No fuel available.")
            return

        cost = litres * float(fuel_station["price_per_litre"])
        try:
            await database.debit_bank_account(ctx.author.id, cost)
        except (database.NoBankAccount, database.InsufficientFunds) as e:
            await ctx.send(str(e))
            return

        await credit_ministry_of_commerce(location["state"], cost)
        await database.pool().execute(
            "UPDATE fuel_stations SET stock_litres = stock_litres - $2 WHERE state = $1",
            location["state"],
            litres,
        )
        await database.pool().execute(
            "UPDATE player_vehicles SET fuel_level_l = fuel_level_l + $2 WHERE id = $1",
            vehicle["id"],
            litres,
        )

        await ctx.send(
            f"Pumped {litres:.1f}L into your {vehicle['model_name']} for \u20a6{cost:,.2f}. "
            f"Tank now at {float(vehicle['fuel_level_l']) + litres:.1f}/{float(vehicle['tank_capacity_l']):.0f}L."
        )
        await self._log_transaction(
            location["state"],
            f"Fuel purchase: {ctx.author.mention} bought {litres:.1f}L for \u20a6{cost:,.2f} "
            f"(narration: fuel purchase). Credited to {location['state']} Ministry of Commerce.",
        )

    # -- !drive -----------------------------------------------------------

    @commands.command(name="drive")
    async def drive(self, ctx: commands.Context, destination_state: str, *, destination_channel: str):
        try:
            await self._start_drive(ctx, destination_state, destination_channel)
        except VehicleError as e:
            await ctx.send(str(e))

    async def _start_drive(self, ctx: commands.Context, destination_state: str, destination_channel: str) -> None:
        await database.ensure_player_exists(ctx.author.id)
        player = await database.get_player(ctx.author.id)

        if player["current_location_id"] is None:
            raise VehicleError("Your current location isn't set -- can't drive from nowhere.")
        origin = await database.pool().fetchrow(
            "SELECT * FROM locations WHERE id = $1", player["current_location_id"]
        )
        if origin is None:
            raise VehicleError("Your current location couldn't be resolved.")

        if player["active_vehicle_id"] is None:
            raise VehicleError("You don't have an active car. Use `!use-car <name>` first.")
        vehicle = await _get_vehicle_with_tier(player["active_vehicle_id"])

        if vehicle["status"] == "broken_down":
            raise VehicleError("Your car is broken down and needs a mechanic before it can be driven.")
        if float(vehicle["condition"]) <= BREAKDOWN_THRESHOLD:
            raise VehicleError("Your car's condition is too low to drive -- it needs a mechanic.")
        if vehicle["current_location_id"] != origin["id"]:
            raise VehicleError("Your car isn't with you -- go to where it's parked first.")

        existing_trip = await database.pool().fetchrow(
            "SELECT 1 FROM car_trips WHERE player_discord_id = $1", ctx.author.id
        )
        if existing_trip is not None:
            raise VehicleError("You're already driving somewhere.")

        destination_state_upper = destination_state.upper()
        destination = await database.pool().fetchrow(
            "SELECT * FROM locations WHERE state = $1 AND lower(channel_name) = lower($2)",
            destination_state_upper,
            destination_channel,
        )
        if destination is None:
            raise VehicleError(f"Unknown destination `{destination_channel}` in {destination_state.title()}.")

        is_interstate = destination_state_upper != origin["state"]

        if is_interstate:
            if player["interstate_cooldown_until"] is not None and player["interstate_cooldown_until"] > datetime.datetime.now(datetime.timezone.utc):
                remaining = (player["interstate_cooldown_until"] - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
                raise VehicleError(f"You're still on interstate cooldown for {_format_duration(remaining)}.")

            interstate_leg = await _get_interstate_leg(origin["state"], destination_state_upper)
            if interstate_leg is None:
                raise VehicleError("No interstate route exists between those states.")

            home_checkpoint = await _get_location(origin["state"], ROAD_CHECKPOINT_CATEGORY, ROAD_CHECKPOINT_CHANNEL)
            dest_checkpoint = await _get_location(destination_state_upper, ROAD_CHECKPOINT_CATEGORY, ROAD_CHECKPOINT_CHANNEL)
            if home_checkpoint is None or dest_checkpoint is None:
                raise VehicleError("Road checkpoints aren't set up for one of those states yet.")

            leg1 = await _get_distance_km(origin, home_checkpoint)
            leg2 = await _get_distance_km(dest_checkpoint, destination)
            interstate_km = float(interstate_leg["distance_km"])
            total_distance = leg1 + interstate_km + leg2
            cooldown_hours = float(interstate_leg["cooldown_hours"])
            leg2_boundary = leg1 + interstate_km
        else:
            total_distance = await _get_distance_km(origin, destination)
            cooldown_hours = None
            leg2_boundary = None

        if total_distance <= 0:
            raise VehicleError("You're already there.")

        effective_speed = _effective_speed(vehicle)
        effective_fuel_use = _effective_fuel_use(vehicle)
        wear_cost = total_distance * float(vehicle["wear_pct_per_km"])
        fuel_required = total_distance * effective_fuel_use

        fuel_available = float(vehicle["fuel_level_l"])
        fuel_fraction = 1.0 if fuel_required <= 0 else min(fuel_available / fuel_required, 1.0)

        condition_available = float(vehicle["condition"]) - BREAKDOWN_THRESHOLD
        condition_fraction = 1.0 if wear_cost <= 0 else min(max(condition_available / wear_cost, 0.0), 1.0)

        travel_fraction = min(fuel_fraction, condition_fraction, 1.0)
        will_break_down = travel_fraction < 1.0

        travel_time_hours = total_distance / effective_speed
        elapsed_seconds = travel_time_hours * 3600 * travel_fraction

        breakdown_state = None
        if will_break_down:
            effective_distance = total_distance * travel_fraction
            if is_interstate:
                # Past the interstate leg's far boundary -> broke down after
                # crossing into the destination state; otherwise still in
                # (or just short of leaving) the origin state.
                breakdown_state = origin["state"] if effective_distance <= leg2_boundary else destination_state_upper
            else:
                breakdown_state = origin["state"]

        arrives_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=elapsed_seconds)

        await database.pool().execute(
            """
            INSERT INTO car_trips
                (player_discord_id, vehicle_id, origin_location_id, destination_location_id,
                 is_interstate, total_distance_km, fuel_required_l, wear_cost,
                 will_break_down, breakdown_state, arrives_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            """,
            ctx.author.id,
            vehicle["id"],
            origin["id"],
            destination["id"],
            is_interstate,
            total_distance,
            fuel_required,
            wear_cost,
            will_break_down,
            breakdown_state,
            arrives_at,
        )

        if is_interstate:
            await database.pool().execute(
                "UPDATE players SET interstate_cooldown_until = $2 WHERE discord_id = $1",
                ctx.author.id,
                datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=cooldown_hours),
            )

        await self._revoke_channel(ctx.author, origin["channel_id"])

        eta_text = _format_duration(elapsed_seconds)
        await ctx.send(
            f"You pull out in your {vehicle['model_name']}, heading for "
            f"{destination_state.title()} {destination['channel_name']}. ETA: {eta_text}."
        )

    # -- background tick: resolve arrivals / breakdowns ---------------------

    @tasks.loop(seconds=TICK_SECONDS)
    async def car_tick(self):
        trips = await database.pool().fetch(
            "SELECT * FROM car_trips WHERE arrives_at <= now()"
        )
        for trip in trips:
            await self._resolve_trip(trip)

    @car_tick.before_loop
    async def before_car_tick(self):
        await self.bot.wait_until_ready()

    async def _swap_state_role(self, member: discord.Member, old_state: str | None, new_state: str) -> None:
        """The state role (Delta/Lagos/Abuja) marks where a player currently
        IS, not everywhere they've ever been -- it's swapped, not added to.
        Old and new state are the uppercase form (players.current_state /
        locations.state); role names are title-case ("Delta", "Abuja")."""
        if old_state and old_state.upper() != new_state.upper():
            old_role = discord_utils.get_role(member.guild, old_state.title())
            if old_role is not None and old_role in member.roles:
                await member.remove_roles(old_role)

        new_role = discord_utils.get_role(member.guild, new_state.title())
        if new_role is not None and new_role not in member.roles:
            await member.add_roles(new_role)

    async def _resolve_trip(self, trip) -> None:
        vehicle = await database.pool().fetchrow(
            "SELECT * FROM player_vehicles WHERE id = $1", trip["vehicle_id"]
        )
        member = self._get_member(trip["player_discord_id"])
        player = await database.get_player(trip["player_discord_id"])
        old_state = player["current_state"] if player else None

        if trip["will_break_down"]:
            repair_location = await _get_location(trip["breakdown_state"], DEALERSHIP_CATEGORY, AUTO_REPAIR_CHANNEL)

            await database.pool().execute(
                """
                UPDATE player_vehicles
                SET fuel_level_l = 0, condition = $2, status = 'broken_down',
                    current_location_id = NULL
                WHERE id = $1
                """,
                trip["vehicle_id"],
                BREAKDOWN_THRESHOLD,
            )
            await database.pool().execute(
                "INSERT INTO repair_requests (vehicle_id, requested_by) VALUES ($1, $2)",
                trip["vehicle_id"],
                trip["player_discord_id"],
            )

            if repair_location is not None:
                await database.set_player_location(trip["player_discord_id"], repair_location["id"])
                if trip["is_interstate"] and trip["breakdown_state"] and old_state and trip["breakdown_state"].upper() != old_state.upper():
                    # Broke down after actually crossing into the destination
                    # state -- they're there now, even though they didn't
                    # reach the exact channel they were driving to.
                    await database.set_player_state(trip["player_discord_id"], trip["breakdown_state"].upper())
                    if member is not None:
                        await self._swap_state_role(member, old_state, trip["breakdown_state"].upper())
                if member is not None:
                    await self._grant_full_access(member, repair_location["channel_id"])

            if member is not None:
                try:
                    await member.send(
                        f"Your {vehicle['model_name']} broke down on the way. "
                        f"You've been left at {trip['breakdown_state'].title()} Auto Repair -- "
                        f"it'll need a mechanic before you can drive it again."
                    )
                except discord.Forbidden:
                    pass
        else:
            await database.pool().execute(
                """
                UPDATE player_vehicles
                SET fuel_level_l = GREATEST(fuel_level_l - $2, 0),
                    condition = GREATEST(condition - $3, 0),
                    current_location_id = $4
                WHERE id = $1
                """,
                trip["vehicle_id"],
                trip["fuel_required_l"],
                trip["wear_cost"],
                trip["destination_location_id"],
            )

            destination = await database.pool().fetchrow(
                "SELECT * FROM locations WHERE id = $1", trip["destination_location_id"]
            )
            await database.set_player_location(trip["player_discord_id"], destination["id"])
            if trip["is_interstate"]:
                await database.set_player_state(trip["player_discord_id"], destination["state"])
                if member is not None:
                    await self._swap_state_role(member, old_state, destination["state"])
            if member is not None:
                await self._grant_full_access(member, destination["channel_id"])
                try:
                    await member.send(
                        f"You've arrived at {destination['state'].title()} {destination['channel_name']}."
                    )
                except discord.Forbidden:
                    pass

        await database.pool().execute("DELETE FROM car_trips WHERE id = $1", trip["id"])

    def _get_member(self, discord_id: int) -> discord.Member | None:
        for guild in self.bot.guilds:
            member = guild.get_member(discord_id)
            if member is not None:
                return member
        return None

    async def _revoke_channel(self, member: discord.Member, channel_id: int | None) -> None:
        if not channel_id:
            return
        channel = self.bot.get_channel(channel_id)
        if channel is not None:
            await channel.set_permissions(member, overwrite=None)

    async def _grant_full_access(self, member: discord.Member, channel_id: int | None) -> None:
        if not channel_id:
            return
        channel = self.bot.get_channel(channel_id)
        if channel is not None:
            await channel.set_permissions(member, view_channel=True, send_messages=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Cars(bot))

import datetime

import discord
from discord.ext import commands, tasks

import database
import discord_utils

# Same FCT-labeling quirk as banking.py -- Abuja's government/staff roles
# say "FCT", everywhere else (players.current_state, locations.state) it's
# "Abuja". locations.state is always the upper-case form of the Python-side
# state name, so `.upper()` alone gets from one to the other.
STATE_ROLE_LABEL = {
    "Abuja": "FCT",
    "Lagos": "Lagos",
    "Delta": "Delta",
}

SEAT_CAP = 10
DWELL_SECONDS_EMPTY = 3     # dwell time when nobody boarded/is boarding at this stop
DWELL_SECONDS_PASSENGER = 5  # dwell time when at least one passenger boarded/is aboard
TICK_SECONDS = 5            # how often the background loop checks for buses due to move
DEFAULT_BUS_PRICE = 5_000_000  # placeholder purchase price -- adjust freely


# ---------------------------------------------------------------------------
# Small standalone ledger helpers -- see the STAND-IN note in
# schema_transportation.sql. Replace these two functions with real calls
# into the banking module once schema_banking.sql / org_accounts_seed.sql
# are available, and everything else in this cog stays the same.
# ---------------------------------------------------------------------------

class InsufficientTreasuryFunds(Exception):
    pass


async def debit_treasury(state: str, amount: float) -> None:
    await database.pool().fetchrow(
        """
        INSERT INTO state_accounts (state, account_type, balance)
        VALUES ($1, 'treasury', 0)
        ON CONFLICT (state, account_type) DO NOTHING
        RETURNING balance
        """,
        state,
    )
    balance = await database.pool().fetchval(
        "SELECT balance FROM state_accounts WHERE state = $1 AND account_type = 'treasury'",
        state,
    )
    if balance < amount:
        raise InsufficientTreasuryFunds(f"{state} treasury only has {balance:,.2f}.")
    await database.pool().execute(
        "UPDATE state_accounts SET balance = balance - $2 WHERE state = $1 AND account_type = 'treasury'",
        state,
        amount,
    )


async def credit_ministry_of_commerce(state: str, amount: float) -> None:
    await database.pool().execute(
        """
        INSERT INTO state_accounts (state, account_type, balance)
        VALUES ($1, 'ministry_of_commerce', $2)
        ON CONFLICT (state, account_type)
        DO UPDATE SET balance = state_accounts.balance + EXCLUDED.balance
        """,
        state,
        amount,
    )


# ---------------------------------------------------------------------------
# Route / stop helpers
# ---------------------------------------------------------------------------

async def list_route_stops(route: object) -> list:
    """Every `locations` row belonging to either end of a route, ordered
    zone_a's channels first then zone_b's (both in id order). This is the
    virtual stop list a bus walks back and forth along -- see the comment
    on `buses` in schema_transportation.sql for why it isn't materialized."""
    categories = await database.pool().fetch(
        """
        SELECT zc.zone_id, zc.category
        FROM zone_categories zc
        WHERE zc.zone_id = ANY($1::int[])
        """,
        [route["zone_a_id"], route["zone_b_id"]],
    )
    by_zone = {route["zone_a_id"]: [], route["zone_b_id"]: []}
    for row in categories:
        by_zone[row["zone_id"]].append(row["category"])

    stops = []
    for zone_id in (route["zone_a_id"], route["zone_b_id"]):
        cats = by_zone[zone_id]
        if not cats:
            continue
        rows = await database.pool().fetch(
            """
            SELECT * FROM locations
            WHERE state = $1 AND category = ANY($2::text[])
              AND parent_location_id IS NULL
            ORDER BY id
            """,
            route["state"],
            cats,
        )
        stops.extend(rows)
    return stops


async def get_zone_for_location(location: object) -> object | None:
    return await database.pool().fetchrow(
        """
        SELECT z.* FROM zones z
        JOIN zone_categories zc ON zc.zone_id = z.id
        WHERE z.state = $1 AND zc.category = $2
        """,
        location["state"],
        location["category"],
    )


async def find_route(state: str, zone_a_id: int, zone_b_id: int) -> object | None:
    return await database.pool().fetchrow(
        """
        SELECT * FROM routes
        WHERE state = $1
          AND ((zone_a_id = $2 AND zone_b_id = $3) OR (zone_a_id = $3 AND zone_b_id = $2))
        """,
        state,
        zone_a_id,
        zone_b_id,
    )


def commissioner_role_name(office: str, state: str) -> str:
    """office is 'Commerce' or 'Finance'. e.g. 'Delta Commissioner of Commerce'."""
    label = STATE_ROLE_LABEL[state]
    return f"{label} Commissioner of {office}"


async def member_commissioner_state(member: discord.Member, office: str) -> str | None:
    """Which state (if any) this member holds a Commissioner of <office> role for."""
    for state in STATE_ROLE_LABEL:
        role = discord_utils.get_role(member.guild, commissioner_role_name(office, state))
        if role is not None and role in member.roles:
            return state
    return None


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class Transportation(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bus_tick.start()

    def cog_unload(self):
        self.bus_tick.cancel()

    # -- !buy-brt -----------------------------------------------------------

    @commands.command(name="buy-brt")
    async def buy_brt(self, ctx: commands.Context, zone_a: str, zone_b: str):
        """Requested by a Commissioner of Commerce. e.g. !buy-brt A B"""
        state = await member_commissioner_state(ctx.author, "Commerce")
        if state is None:
            await ctx.send("Only a Commissioner of Commerce can request a bus.")
            return

        z1 = await database.pool().fetchrow(
            "SELECT * FROM zones WHERE state = $1 AND code = $2", state.upper(), zone_a.upper()
        )
        z2 = await database.pool().fetchrow(
            "SELECT * FROM zones WHERE state = $1 AND code = $2", state.upper(), zone_b.upper()
        )
        if z1 is None or z2 is None:
            await ctx.send("Unknown zone code(s). Zones are A, B, C.")
            return

        route = await find_route(state.upper(), z1["id"], z2["id"])
        if route is None:
            await ctx.send("No route exists between those zones.")
            return

        request_id = await database.pool().fetchval(
            """
            INSERT INTO bus_purchase_requests (route_id, requested_by, price)
            VALUES ($1, $2, $3)
            RETURNING id
            """,
            route["id"],
            ctx.author.id,
            DEFAULT_BUS_PRICE,
        )
        await ctx.send(
            f"Bus request #{request_id} submitted for the {zone_a.upper()}-{zone_b.upper()} route "
            f"in {state} at {DEFAULT_BUS_PRICE:,.2f}. Awaiting Commissioner of Finance approval."
        )

    # -- !approve-brt ---------------------------------------------------------

    @commands.command(name="approve-brt")
    async def approve_brt(self, ctx: commands.Context, request_id: int):
        state = await member_commissioner_state(ctx.author, "Finance")
        if state is None:
            await ctx.send("Only a Commissioner of Finance can approve a bus purchase.")
            return

        request = await database.pool().fetchrow(
            """
            SELECT bpr.*, r.state AS route_state, r.zone_a_id, r.zone_b_id
            FROM bus_purchase_requests bpr
            JOIN routes r ON r.id = bpr.route_id
            WHERE bpr.id = $1
            """,
            request_id,
        )
        if request is None or request["status"] != "pending":
            await ctx.send("No pending request with that ID.")
            return
        if request["route_state"] != state.upper():
            await ctx.send(f"That request is for {request['route_state'].title()}, not {state}.")
            return

        try:
            await debit_treasury(state.upper(), float(request["price"]))
        except InsufficientTreasuryFunds as e:
            await ctx.send(str(e))
            return

        route = await database.pool().fetchrow("SELECT * FROM routes WHERE id = $1", request["route_id"])
        stops = await list_route_stops(route)
        if not stops:
            await ctx.send("That route has no channels mapped yet -- can't spawn a bus.")
            return

        bus_id = await database.pool().fetchval(
            """
            INSERT INTO buses (route_id, forward, stop_index, current_location_id, purchased_by, price_paid)
            VALUES ($1, TRUE, 0, $2, $3, $4)
            RETURNING id
            """,
            route["id"],
            stops[0]["id"],
            ctx.author.id,
            request["price"],
        )
        await database.pool().execute(
            "UPDATE bus_purchase_requests SET status = 'approved', decided_by = $2, decided_at = now() WHERE id = $1",
            request_id,
            ctx.author.id,
        )
        await ctx.send(f"Approved. Bus #{bus_id} purchased and now running, debited from {state} treasury.")

    # -- !decline-brt -----------------------------------------------------------

    @commands.command(name="decline-brt")
    async def decline_brt(self, ctx: commands.Context, request_id: int):
        """Requested by a Commissioner of Finance -- rejects a pending bus
        purchase request. No treasury debit, no bus spawned."""
        state = await member_commissioner_state(ctx.author, "Finance")
        if state is None:
            await ctx.send("Only a Commissioner of Finance can decline a bus purchase.")
            return

        request = await database.pool().fetchrow(
            """
            SELECT bpr.*, r.state AS route_state
            FROM bus_purchase_requests bpr
            JOIN routes r ON r.id = bpr.route_id
            WHERE bpr.id = $1
            """,
            request_id,
        )
        if request is None or request["status"] != "pending":
            await ctx.send("No pending request with that ID.")
            return
        if request["route_state"] != state.upper():
            await ctx.send(f"That request is for {request['route_state'].title()}, not {state}.")
            return

        await database.pool().execute(
            """
            UPDATE bus_purchase_requests
            SET status = 'denied', decided_by = $2, decided_at = now()
            WHERE id = $1
            """,
            request_id,
            ctx.author.id,
        )
        await ctx.send(f"Declined bus request #{request_id}. No funds were moved.")

    # -- !book-bus ------------------------------------------------------------

    @commands.command(name="book-bus")
    async def book_bus(self, ctx: commands.Context, destination_zone: str):
        location = await database.get_location_by_channel(ctx.channel.id)
        if location is None:
            await ctx.send("You can't book a bus from here.")
            return

        origin_zone = await get_zone_for_location(location)
        if origin_zone is None:
            await ctx.send("This channel isn't on any bus zone.")
            return

        dest_zone = await database.pool().fetchrow(
            "SELECT * FROM zones WHERE state = $1 AND code = $2",
            location["state"],
            destination_zone.upper(),
        )
        if dest_zone is None:
            await ctx.send("Unknown destination zone. Zones are A, B, C.")
            return
        if dest_zone["id"] == origin_zone["id"]:
            await ctx.send("You're already in that zone.")
            return

        route = await find_route(location["state"], origin_zone["id"], dest_zone["id"])
        if route is None:
            await ctx.send("No route connects those zones.")
            return

        existing = await database.pool().fetchrow(
            "SELECT * FROM bus_bookings WHERE player_discord_id = $1 AND status = 'waiting'",
            ctx.author.id,
        )
        if existing is not None:
            await ctx.send("You already have a pending booking.")
            return

        await database.pool().execute(
            """
            INSERT INTO bus_bookings
                (player_discord_id, route_id, origin_zone_id, destination_zone_id, origin_location_id)
            VALUES ($1, $2, $3, $4, $5)
            """,
            ctx.author.id,
            route["id"],
            origin_zone["id"],
            dest_zone["id"],
            location["id"],
        )
        await ctx.send(
            f"Booked. You'll auto-board the next {route['state'].title()} bus for "
            f"{origin_zone['code']}\u2192{dest_zone['code']} that reaches {location['channel_name']}."
        )

    # -- !queue -----------------------------------------------------------------

    @commands.command(name="queue")
    async def queue(self, ctx: commands.Context):
        """Shows the player's position in line for a pickup they've already
        booked at this channel, plus an estimate of how long the bus will
        take to reach here."""
        location = await database.get_location_by_channel(ctx.channel.id)
        if location is None:
            await ctx.send("There's no bus queue in this channel.")
            return

        booking = await database.pool().fetchrow(
            """
            SELECT * FROM bus_bookings
            WHERE player_discord_id = $1 AND origin_location_id = $2 AND status = 'waiting'
            """,
            ctx.author.id,
            location["id"],
        )
        if booking is None:
            await ctx.send("You don't have a pending booking here -- book one with `!book-bus` first.")
            return

        ahead = await database.pool().fetchval(
            """
            SELECT count(*) FROM bus_bookings
            WHERE origin_location_id = $1 AND status = 'waiting' AND booked_at < $2
            """,
            location["id"],
            booking["booked_at"],
        )
        queue_number = ahead + 1

        route = await database.pool().fetchrow("SELECT * FROM routes WHERE id = $1", booking["route_id"])
        # NOTE: assumes one bus per route. If a route ever gets a second
        # bus, this picks whichever one the query happens to return first.
        bus = await database.pool().fetchrow(
            "SELECT * FROM buses WHERE route_id = $1", booking["route_id"]
        )
        if bus is None:
            await ctx.send(
                f"You're #{queue_number} in line at this stop, but no bus is currently "
                f"running this route \u2014 ask a Commissioner of Commerce to request one."
            )
            return

        stops = await list_route_stops(route)
        eta_seconds = self._estimate_eta_seconds(bus, stops, location["id"])
        if eta_seconds is None:
            await ctx.send(f"You're #{queue_number} in line at this stop. Couldn't compute an ETA for the bus.")
            return

        minutes, seconds = divmod(int(round(eta_seconds)), 60)
        eta_text = f"{minutes}m {seconds}s" if minutes else f"{seconds}s"
        note = ""
        if queue_number > SEAT_CAP:
            note = " (you're past this bus's seat cap, so you may end up waiting for the one after)"
        await ctx.send(
            f"You're #{queue_number} in line at this stop. The bus is roughly {eta_text} away.{note} "
            f"(ETA is an estimate -- exact time depends on whether it picks up passengers along the way.)"
        )

    def _estimate_eta_seconds(self, bus, stops: list, target_location_id: int) -> float | None:
        """Walks the same ping-pong path _advance_bus follows, from the
        bus's current stop, counting stops (and dwell time) until it
        reaches target_location_id. Per-stop dwell time is unknown this
        far ahead (it depends on whether someone boards/alights there),
        so the average of the two dwell constants is used as an estimate
        for every stop after the current leg."""
        if not stops:
            return None

        target_index = next((i for i, s in enumerate(stops) if s["id"] == target_location_id), None)
        if target_index is None:
            return None

        idx = bus["stop_index"]
        forward = bus["forward"]

        now = datetime.datetime.now(datetime.timezone.utc)
        remaining_on_current_leg = max((bus["next_move_at"] - now).total_seconds(), 0)

        if idx == target_index:
            return remaining_on_current_leg

        avg_dwell = (DWELL_SECONDS_EMPTY + DWELL_SECONDS_PASSENGER) / 2
        total = remaining_on_current_leg

        # Safety cap so a malformed route can't loop forever.
        for _ in range(len(stops) * 4):
            next_index = idx + (1 if forward else -1)
            if next_index >= len(stops):
                next_index = len(stops) - 2 if len(stops) > 1 else 0
                forward = False
            elif next_index < 0:
                next_index = 1 if len(stops) > 1 else 0
                forward = True
            idx = next_index
            if idx == target_index:
                return total
            total += avg_dwell

        return None

    # -- background movement / boarding / alighting tick -----------------------

    @tasks.loop(seconds=TICK_SECONDS)
    async def bus_tick(self):
        buses = await database.pool().fetch(
            "SELECT b.*, r.state, r.zone_a_id, r.zone_b_id, r.fare FROM buses b "
            "JOIN routes r ON r.id = b.route_id WHERE b.next_move_at <= now()"
        )
        for bus in buses:
            await self._advance_bus(bus)

    @bus_tick.before_loop
    async def before_bus_tick(self):
        await self.bot.wait_until_ready()

    async def _advance_bus(self, bus: object) -> None:
        route = await database.pool().fetchrow("SELECT * FROM routes WHERE id = $1", bus["route_id"])
        stops = await list_route_stops(route)
        if not stops:
            return

        # 1. Alight anyone boarded whose destination zone is this stop.
        current = stops[bus["stop_index"]]
        current_zone = await get_zone_for_location(current)
        boarded = await database.pool().fetch(
            "SELECT * FROM bus_bookings WHERE bus_id = $1 AND status = 'boarded'", bus["id"]
        )
        for booking in boarded:
            if current_zone is not None and booking["destination_zone_id"] == current_zone["id"]:
                await self._alight(booking, current, float(route["fare"]), route["state"])

        # 2. Board anyone waiting at this exact channel, up to seat cap.
        seats_taken = await database.pool().fetchval(
            "SELECT count(*) FROM bus_bookings WHERE bus_id = $1 AND status = 'boarded'", bus["id"]
        )
        waiting = await database.pool().fetch(
            """
            SELECT * FROM bus_bookings
            WHERE route_id = $1 AND origin_location_id = $2 AND status = 'waiting'
            ORDER BY booked_at
            """,
            route["id"],
            current["id"],
        )
        for booking in waiting:
            if seats_taken >= SEAT_CAP:
                break
            boarded_ok = await self._try_board(booking, bus, current, float(route["fare"]), route["state"])
            if boarded_ok:
                seats_taken += 1

        # 3. Move every still-boarded player's visibility to the next stop,
        #    then step the bus forward/back and flip direction at the ends.
        next_index = bus["stop_index"] + (1 if bus["forward"] else -1)
        forward = bus["forward"]
        if next_index >= len(stops):
            next_index = len(stops) - 2 if len(stops) > 1 else 0
            forward = False
        elif next_index < 0:
            next_index = 1 if len(stops) > 1 else 0
            forward = True

        still_boarded = await database.pool().fetch(
            "SELECT * FROM bus_bookings WHERE bus_id = $1 AND status = 'boarded'", bus["id"]
        )
        next_stop = stops[next_index]
        for booking in still_boarded:
            await self._move_transit_view(booking["player_discord_id"], current, next_stop)

        # Dwell longer at a stop where someone actually got on or off, since
        # that's the case boarding/alighting messages need time to be read.
        # still_boarded (post-boarding) covers "someone's aboard"; `boarded`
        # (pre-boarding, from step 1) covers "someone just got off here" even
        # if the bus is now empty.
        had_passenger = len(still_boarded) > 0 or len(boarded) > 0
        dwell_seconds = DWELL_SECONDS_PASSENGER if had_passenger else DWELL_SECONDS_EMPTY

        await database.pool().execute(
            """
            UPDATE buses
            SET stop_index = $2, forward = $3, current_location_id = $4,
                next_move_at = now() + make_interval(secs => $5)
            WHERE id = $1
            """,
            bus["id"],
            next_index,
            forward,
            next_stop["id"],
            dwell_seconds,
        )

    async def _try_board(self, booking, bus, location, fare: float, state: str) -> bool:
        member = self._get_member(location["state"], booking["player_discord_id"])
        card = await database.pool().fetchrow(
            "SELECT * FROM brt_cards WHERE player_discord_id = $1 AND state = $2",
            booking["player_discord_id"],
            state,
        )
        if card is None or float(card["balance"]) < fare:
            # "You just missed the bus" -- drop the booking outright.
            await database.pool().execute("DELETE FROM bus_bookings WHERE id = $1", booking["id"])
            if member is not None:
                try:
                    await member.send(
                        f"You just missed the bus -- insufficient funds on your {state} BRT card."
                    )
                except discord.Forbidden:
                    pass
            return False

        await database.pool().execute(
            "UPDATE bus_bookings SET status = 'boarded', bus_id = $2 WHERE id = $1",
            booking["id"],
            bus["id"],
        )
        if member is not None:
            await self._revoke_channel(member, location["channel_id"])
            await self._grant_read_only(member, location["channel_id"])
        return True

    async def _alight(self, booking, location, fare: float, state: str) -> None:
        await database.pool().execute(
            "UPDATE brt_cards SET balance = balance - $3 WHERE player_discord_id = $1 AND state = $2",
            booking["player_discord_id"],
            state,
            fare,
        )
        await credit_ministry_of_commerce(state, fare)
        await database.pool().execute("DELETE FROM bus_bookings WHERE id = $1", booking["id"])
        await database.set_player_location(booking["player_discord_id"], location["id"])

        member = self._get_member(state, booking["player_discord_id"])
        if member is not None:
            await self._grant_full_access(member, location["channel_id"])

    async def _move_transit_view(self, discord_id: int, from_location, to_location) -> None:
        member = self._get_member(from_location["state"], discord_id)
        if member is None:
            return
        await self._revoke_channel(member, from_location["channel_id"])
        await self._grant_read_only(member, to_location["channel_id"])

    def _get_member(self, state: str, discord_id: int) -> discord.Member | None:
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

    async def _grant_read_only(self, member: discord.Member, channel_id: int | None) -> None:
        if not channel_id:
            return
        channel = self.bot.get_channel(channel_id)
        if channel is not None:
            await channel.set_permissions(member, view_channel=True, send_messages=False)

    async def _grant_full_access(self, member: discord.Member, channel_id: int | None) -> None:
        if not channel_id:
            return
        channel = self.bot.get_channel(channel_id)
        if channel is not None:
            await channel.set_permissions(member, view_channel=True, send_messages=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Transportation(bot))

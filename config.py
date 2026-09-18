"""
Nothing in here needs a Discord ID anymore. Roles are looked up by name at
runtime (see discord_utils.get_role) -- every role name is unique across
the server. The two per-state channels (Arrival Terminal, Immigration
Office) are looked up by (category, channel name) instead (see
discord_utils.get_channel) -- category names have the state baked in
("Delta Border & Entry" vs "Lagos Border & Entry"), which makes that pair
unique too.

STATES dict is the single source of truth for per-state setup. Add a new
state by adding a new key here -- no other code needs to change.
"""

import os

# ---------------------------------------------------------------------------
# Global roles
# ---------------------------------------------------------------------------

# Role held by Immigration Officers -- only they can run !name and !immigrate
IMMIGRATION_OFFICER_ROLE_NAME = "Immigration Officer"

# ---------------------------------------------------------------------------
# Per-state configuration
# ---------------------------------------------------------------------------
# Destination is chosen via Discord's own onboarding Customization Question
# (Server Settings -> Customize -> Onboarding), not a bot-posted dropdown.
# That question assigns the "{State} Arrival" role directly to the member
# the moment they answer -- the bot just needs to notice it happened.
#
# Role names, all looked up by name (not stored here) since they're
# derivable from the state key -- e.g. Delta's are "Delta Arrival" and
# "Delta":
#   "{State} Arrival"  -> limited-access role granted by Discord onboarding
#                         (sees only Arrival Terminal, Refugee Camp,
#                         Immigration Office)
#   "{State}"           -> the general location role, granted by !immigrate
#                         (the final step) -- this is what unlocks every
#                         other state-gated channel. Arrival role IS
#                         removed at this step.
#
# "indigene_role_name" -> granted by !name once the Immigration Officer has
#                         named the player. Arrival role is NOT removed at
#                         this step. Spelled out explicitly here because
#                         Abuja's doesn't follow the "{State} Indigene"
#                         pattern -- it's "FCT Indigene", not "Abuja
#                         Indigene" (same FCT-naming quirk as employee
#                         roles -- see cogs/banking.py's STATE_ROLE_LABEL).
#
# "state_code"          -> the suffix used when the bot manufactures a
#                         player's NIN role at !name time, e.g. "NIN-0001-LA".
#                         Spelled out explicitly for the same reason as
#                         above -- Abuja's is "FCT", not "AB".
#
# Arrival Terminal and Immigration Office channels are looked up by
# discord_utils.get_channel(guild, state, "BORDER & ENTRY", channel_name)
# when needed -- not stored here, since (category, channel name) is
# already unique per state (see discord_utils.py).

STATES = {
    "Abuja": {
        "state_code": "FCT",
        "indigene_role_name": "FCT Indigene",
    },
    "Lagos": {
        "state_code": "LA",
        "indigene_role_name": "Lagos Indigene",
    },
    "Delta": {
        "state_code": "DE",
        "indigene_role_name": "Delta Indigene",
    },
}

# ---------------------------------------------------------------------------
# Secrets / env
# ---------------------------------------------------------------------------

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

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
# "indigine_role_name" -> granted by !name once the Immigration Officer has
#                         named the player. Arrival role is NOT removed at
#                         this step. Spelled out explicitly here because
#                         Abuja's doesn't follow the "{State} Indigine"
#                         pattern -- it's "FCT Indigine", not "Abuja
#                         Indigine" (same FCT-naming quirk as employee
#                         roles -- see cogs/banking.py's STATE_ROLE_LABEL).
#
# Arrival Terminal and Immigration Office channels are looked up by
# discord_utils.get_channel(guild, state, "BORDER & ENTRY", channel_name)
# when needed -- not stored here, since (category, channel name) is
# already unique per state (see discord_utils.py).

STATES = {
    "Abuja": {
        "indigine_role_name": "FCT Indigine",
    },
    "Lagos": {
        "indigine_role_name": "Lagos Indigine",
    },
    "Delta": {
        "indigine_role_name": "Delta Indigine",
    },
}

# ---------------------------------------------------------------------------
# Secrets / env
# ---------------------------------------------------------------------------

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

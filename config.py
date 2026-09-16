"""
Fill in every ID below with the real role/channel IDs from your Discord server.
Right-click a role or channel -> Copy ID (Developer Mode must be on in
Discord > Settings > Advanced).

STATES dict is the single source of truth for per-state setup. Add a new
state by adding a new key here -- no other code needs to change.
"""

import os

# ---------------------------------------------------------------------------
# Global roles
# ---------------------------------------------------------------------------

# Role given to a brand-new member before they pick a destination.
# Should only be able to see the destination-select channel.
NEWCOMER_ROLE_ID = 0  # TODO: fill in

# Role held by Immigration Officers -- only they can run /immigrate
IMMIGRATION_OFFICER_ROLE_ID = 0  # TODO: fill in

# Channel where the destination-select dropdown is posted
DESTINATION_SELECT_CHANNEL_ID = 0  # TODO: fill in

# ---------------------------------------------------------------------------
# Per-state configuration
# ---------------------------------------------------------------------------
# "arrival_role_id"   -> limited-access role granted on choosing this state
#                        (sees only Arrival Terminal, Refugee Camp, Immigration Office)
# "indigene_role_id"  -> full-access role granted after immigration is complete
# "arrival_terminal_channel_id" -> where the arrival announcement is posted
# "immigration_office_channel_id" -> where /immigrate is allowed to run

STATES = {
    "Abuja": {
        "arrival_role_id": 0,        # TODO
        "indigene_role_id": 0,       # TODO
        "arrival_terminal_channel_id": 0,      # TODO
        "immigration_office_channel_id": 0,    # TODO
    },
    "Lagos": {
        "arrival_role_id": 0,        # TODO
        "indigene_role_id": 0,       # TODO
        "arrival_terminal_channel_id": 0,      # TODO
        "immigration_office_channel_id": 0,    # TODO
    },
    "Delta": {
        "arrival_role_id": 0,        # TODO
        "indigene_role_id": 0,       # TODO
        "arrival_terminal_channel_id": 0,      # TODO
        "immigration_office_channel_id": 0,    # TODO
    },
}

# ---------------------------------------------------------------------------
# Secrets / env
# ---------------------------------------------------------------------------

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

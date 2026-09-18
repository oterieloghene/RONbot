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

# Role held by Immigration Officers -- only they can run !name and !immigrate
IMMIGRATION_OFFICER_ROLE_ID = 0  # TODO: fill in

# ---------------------------------------------------------------------------
# Per-state configuration
# ---------------------------------------------------------------------------
# Destination is chosen via Discord's own onboarding Customization Question
# (Server Settings -> Customize -> Onboarding), not a bot-posted dropdown.
# That question assigns "arrival_role_id" directly to the member the moment
# they answer -- the bot just needs to notice it happened.
#
# "arrival_role_id"   -> limited-access role granted by Discord onboarding
#                        when this state is chosen
#                        (sees only Arrival Terminal, Refugee Camp, Immigration Office)
# "indigine_role_id"  -> "{State} Indigine" role, granted by !name once the
#                        Immigration Officer has named the player. Arrival
#                        role is NOT removed at this step.
# "state_role_id"     -> the general "{State}" location role, granted by
#                        !immigrate (the final step) -- this is what unlocks
#                        every other state-gated channel. Arrival role IS
#                        removed at this step.
# "arrival_terminal_channel_id" -> where the arrival announcement is posted
# "immigration_office_channel_id" -> where !name and !immigrate are allowed to run

STATES = {
    "Abuja": {
        "arrival_role_id": 0,        # TODO
        "indigine_role_id": 0,       # TODO
        "state_role_id": 0,          # TODO
        "arrival_terminal_channel_id": 0,      # TODO
        "immigration_office_channel_id": 0,    # TODO
    },
    "Lagos": {
        "arrival_role_id": 0,        # TODO
        "indigine_role_id": 0,       # TODO
        "state_role_id": 0,          # TODO
        "arrival_terminal_channel_id": 0,      # TODO
        "immigration_office_channel_id": 0,    # TODO
    },
    "Delta": {
        "arrival_role_id": 0,        # TODO
        "indigine_role_id": 0,       # TODO
        "state_role_id": 0,          # TODO
        "arrival_terminal_channel_id": 0,      # TODO
        "immigration_office_channel_id": 0,    # TODO
    },
}

# ---------------------------------------------------------------------------
# Secrets / env
# ---------------------------------------------------------------------------

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

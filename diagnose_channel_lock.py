#!/usr/bin/env python3
"""One-shot diagnostic: which channels does the bot NOT effectively have
MANAGE_ROLES ("Manage Permissions") on, and WHY (role deny, channel
overwrite deny, or category-inherited deny). Read-only: it never edits
anything.

MANAGE_ROLES is the permission Discord's API actually checks when a bot
edits a channel permission overwrite (PUT /channels/{id}/permissions/{ow}).
"Manage Channel Permissions" (MANAGE_CHANNELS) only governs channel
create/rename/delete -- having it on does NOT let the bot edit the
Permissions tab, which is why overwrites 403 with 50013 even when the
global "Manage Channel Permissions" toggle has been on from the start.

Run:  python diagnose_channel_lock.py
Env:  DISCORD_TOKEN (and optionally GUILD_ID) — same vars the bot uses.

It prints the (or however many) problem channels with the exact deny
source so you can point at the right row in Discord's UI.
"""
import asyncio
import os
import sys

import discord

MANAGE_ROLES = 1 << 28  # 0x10000000 = 268435456


async def main() -> int:
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        print("DISCORD_TOKEN is not set in this environment.")
        print("Run this from the Render shell (or export the token first).")
        return 1
    guild_id_env = os.getenv("GUILD_ID")

    intents = discord.Intents.default()
    bot = discord.Client(intents=intents)

    @bot.event
    async def on_ready() -> None:
        guilds = bot.guilds
        target = None
        if guild_id_env:
            target = discord.utils.get(guilds, id=int(guild_id_env))
        if target is None and len(guilds) == 1:
            target = guilds[0]
        if target is None:
            print(f"Could not resolve target guild. Bots see {len(guilds)} guilds.")
            if guild_id_env is None:
                print("Set GUILD_ID to force one (discord.utils.get needs it with multiple).")
            for g in guilds:
                print(f"  - {g.name} (id={g.id})")
            await bot.close()
            return

        print(f"Guild: {target.name} (id={target.id})")
        me = target.me
        print(f"Bot user: {me.display_name} (id={me.id})")

        mr_in_role = bool(me.guild_permissions.manage_roles)
        print(f"\nGlobal: bot role has MANAGE_ROLES ('Manage Roles' / the 'Manage Permissions' channel override) = {mr_in_role}")
        if not mr_in_role:
            print("  -> FIX: Server Settings > Roles > [bot role] > toggle on")
            print("     'Manage Roles' (Advanced section, the GLOBAL row -- not per-channel).")
            print("     This is what the API checks when editing channel overwrites.")
            print("     Note: 'Manage Channel Permissions' alone is NOT enough.")

        admin = bool(me.guild_permissions.administrator)
        print(f"Global: bot role has ADMINISTRATOR = {admin}")

        # ---- per-channel audit ----
        problems = []
        for channel in target.text_channels:
            perms = me.guild_permissions  # baseline role perms
            allowed = bool(perms.manage_roles)

            # walk overwrites: guild default, category, then channel
            # (mirrors Discord's effective-permission model closely enough
            # for a diagnostic: denies that apply beat allows)
            def deny_from(overwrites):
                for tag, ow in overwrites:
                    if ow is None:
                        continue
                    if getattr(ow, "deny", 0) & MANAGE_ROLES:
                        return tag
                return None

            cat_deny = None
            if channel.category is not None:
                cat_deny = deny_from(
                    [("category @everyone", channel.category.overwrites.get(
                        target.default_role, None))]
                    + [(f"category role {r.name}", ow)
                       for r, ow in channel.category.overwrites.items()
                       if r is not target.default_role]
                )
                # also check the bot role's category overwrite specifically
                bot_ow = channel.category.overwrites.get(me.roles[0] if me.roles else None)
                if bot_ow is not None and getattr(bot_ow, "deny", 0) & MANAGE_ROLES:
                    cat_deny = cat_deny or "category bot-role deny"

            chan_deny = deny_from(
                [("channel @everyone", channel.overwrites.get(target.default_role, None))]
                + [(f"channel role {r.name}", ow)
                   for r, ow in channel.overwrites.items()
                   if r is not target.default_role]
            )

            # effective: start allowed, any applicable deny flips it off
            effective = allowed
            if chan_deny:
                effective = False
            elif cat_deny:
                effective = False

            if not effective:
                problems.append((channel, cat_deny, chan_deny))

        print(f"\nChannels where the bot CANNOT manage permissions (overwrites 403): {len(problems)}")
        for channel, cat_deny, chan_deny in sorted(problems, key=lambda p: p[0].name):
            reason = chan_deny or cat_deny or "unknown (check raw overwrites)"
            print(f"  #{channel.name} (id={channel.id})")
            print(f"    category: {channel.category.name if channel.category else 'none'}")
            print(f"    deny source: {reason}")

        if not problems:
            print("  (none) — the bot can manage permissions everywhere.")
            print("  If overwrites still 403 on next boot, the global role is")
            print("  the remaining suspect (see the Global line above).")
        else:
            print("\n  SUMMARY OF THE FIX:")
            print("  1. Go to Server Settings > Roles > [bot role]")
            print("  2. Turn OFF 'Manage Channel Permissions' (the setting that's been on)")
            print("  3. Turn ON 'Manage Roles' (in the 'Advanced Permissions' section)")
            print("  These are TWO DIFFERENT toggles — both can appear in the same UI area.")
            print("  The API only checks 'Manage Roles' when editing channel overwrites.")

        await bot.close()

    await bot.start(token)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
#!/usr/bin/env python3
"""One-shot diagnostic for why overwrite edits 403 (code 50013) even when
the bot's role has every permission bit on. Read-only: never edits anything.

Discord's API rejects an overwrite edit with 50013 for exactly two reasons,
and both are GLOBAL (role settings), never per-channel:
  1. The bot's role lacks MANAGE_ROLES (the API checks this for overwrite
     edits, NOT MANAGE_CHANNELS -- 'Manage Channel Permissions').
  2. The overwrite TARGET ranks above the bot's top role in the server's
     role list. Roles (and members) above the bot's top role can never be
     overwrite targets -- so gating channels with a state role (Delta, ...)
     that sits ABOVE the bot makes every sync overwrite 50013, on every
     channel, no matter how the individual channels are configured.
Channel/category overwrites (the per-channel gates you set) are RELEVANT
only if they deny the bot MANAGE_ROLES specifically; a view-channel gate
(@everyone denied view, state role allowed view) does NOT block the bot.

Run:  python diagnose_channel_lock.py
Env:  DISCORD_TOKEN (and optionally GUILD_ID) — same vars the bot uses.
It prints a short report: global bits, the full role ladder with the bot's
position marked, the exact roles to move, and a compact per-channel gate
audit. No more scrolling through 13+ bulky channel dumps.
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
        print(f"\nGlobal: bot role has MANAGE_ROLES ('Manage Roles') = {mr_in_role}")
        if not mr_in_role:
            print("  -> FIX: Server Settings > Roles > [bot role] > toggle ON")
            print("     'Manage Roles' (Advanced section). This is what the API")
            print("     checks when editing channel overwrites.")
            print("     Note: 'Manage Channel Permissions' alone is NOT enough.")
        else:
            print("  -> Manage Roles is ON, so cause #1 is ruled out.")
            print("     If overwrites still 50013, the ONLY remaining cause is the")
            print("     role ladder below: a role/member ABOVE the bot's top role.")

        admin = bool(me.guild_permissions.administrator)
        print(f"Global: bot role has ADMINISTRATOR = {admin}")

        # ---- role ladder audit (the real 50013 suspect) ----
        top = me.top_role
        ranked = sorted(target.roles, key=lambda r: r.position)
        above = [r for r in ranked if r > top]
        print(f"\nRole ladder (bottom to top); bot's top role '{top.name}' marked with [BOT]:")
        for r in ranked:
            marker = " [BOT]" if r.id == top.id else ""
            print(f"  pos {r.position:>4}  {r.name}{marker}")
        if above:
            print(f"\nROLES ABOVE THE BOT'S TOP ROLE ({len(above)}):")
            for r in above:
                print(f"  - '{r.name}' (id={r.id}, pos {r.position})")
            print("  -> FIX: drag the bot's role ABOVE every role listed above it")
            print("     (Server Settings > Roles). Any role/member ranked above the")
            print("     bot's top role can never be an overwrite target -- Discord")
            print("     50013s those edits on EVERY channel. State roles (Delta, ...),")
            print("     staff roles, and player roles all count.")
            print("     Keep only true admin roles above the bot if you must.")
        else:
            print("\nNo role ranks above the bot's top role -- the role ladder is clean.")
            print("If overwrites still 50013, check the per-channel audit below for an")
            print("explicit MANAGE_ROLES deny on @everyone or the bot's role.")

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

        if not problems and not above_top:
            print("  (none) — every overwrite the bot needs can land.")
            print("  If overwrites still 403 on next boot, re-check the")
            print("  Global lines above (a per-channel MANAGE_ROLES deny is")
            print("  the only per-channel cause left).")
        else:
            print("\n  SUMMARY OF THE FIX:")
            if above_top:
                print("  1. Your channel gating is fine as-is: view stays role-gated")
                print("     (state role = read), and the bot only ever writes the")
                print("     per-member Send Messages overwrite. It never touches")
                print("     View Channel, so Delta's read-only channels stay that way.")
                print("  2. The 50013 comes from role RANK, not permission bits:")
                print("     Discord rejects an overwrite whenever its TARGET sits")
                print("     above the bot's top role in Server Settings > Roles.")
                print("  3. Move the bot's role to the TOP of the role list — only")
                print("     the admin/owner role may stay above it — or drag every")
                print("     role listed in the ladder above BELOW the bot's role.")
                print("     The state roles (Delta, ...) must sit below the bot too,")
                print("     because the bot edits overwrites on channels gated by")
                print("     them and on members holding them.")
                print("  4. No permission toggle needs to change: 'Manage Roles'")
                print("     (which the API actually checks for overwrites) is already")
                print("     on; 'Manage Channel Permissions' is not what's failing.")
            elif not mr_in_role:
                print("  1. Go to Server Settings > Roles > [bot role] > Advanced")
                print("  2. Turn ON 'Manage Roles' — that is the permission the API")
                print("     checks when editing channel overwrites. ('Manage Channel")
                print("     Permissions' alone is NOT enough; it only governs channel")
                print("     create/rename/delete.)")
            else:
                print("  A per-channel or category overwrite is denying the bot")
                print("  'Manage Permissions' (MANAGE_ROLES). Remove that deny on")
                print("  the rows above — the view-channel gates can stay.")

        await bot.close()

    await bot.start(token)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
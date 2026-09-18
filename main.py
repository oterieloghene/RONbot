import asyncio
import logging
import os

import discord
from aiohttp import web
from discord.ext import commands
from dotenv import load_dotenv

import config
import database
import discord_utils

load_dotenv()

logging.basicConfig(level=logging.INFO)

intents = discord.Intents.default()
intents.members = True  # required for on_member_update (arrival role detection)
intents.message_content = True  # required to read !name, !immigrate, !dep etc.

bot = commands.Bot(command_prefix="!", intents=intents)

# Bumped every time command behavior changes. If a deployed bot logs any
# other marker, the running process is executing stale code -- redeploy from
# the latest main and fully restart the process (kill the old PID, don't
# just reload). Compare this against the PR that last changed !name/!immigrate.
BOT_BUILD = "2025-01-16-permission-setup+429-backoff"

INITIAL_COGS = [
    "cogs.onboarding",
    "cogs.immigration",
    "cogs.banking",
    "cogs.transportation",
    "cogs.cars",
    "cogs.petroleum",
    "cogs.phone",
]


@bot.event
async def on_ready():
    print(f"RONbot build {BOT_BUILD} | logged in as {bot.user} ({bot.user.id})")
    synced = await bot.tree.sync()
    print(f"Synced {len(synced)} slash command(s)")
    for guild in bot.guilds:
        try:
            summary = await discord_utils.setup_permissions(guild)
            print(f"Permission setup for {guild.name} ({guild.id}): {summary}")
        except Exception:
            # One guild's setup failing must not kill the whole bot -- the
            # channel bindings/overwrites can be repaired by a restart.
            logging.exception(
                f"Permission setup failed for {guild.name} ({guild.id})"
            )


async def handle_ping(request: web.Request) -> web.Response:
    return web.Response(text="OK")


async def start_web_server() -> None:
    """Render Web Services need something bound to $PORT or the deploy is
    marked unhealthy -- this just exists to satisfy that, it doesn't serve
    any real traffic."""
    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/health", handle_ping)

    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"Health-check server listening on port {port}")


async def main():
    print(f"Starting RONbot build {BOT_BUILD}")
    await database.init_pool()
    await start_web_server()

    # Discord globally rate-limits login attempts per token/IP. If the bot
    # dies and Render restarts it in a tight loop, every restart burns login
    # budget until the /users/@me call itself starts returning 429 -- then
    # every subsequent restart fails the same way (crash loop). So instead of
    # exiting on a startup failure (which lets Render hammer the API again),
    # stay alive and retry in-process with exponential backoff.
    attempt = 0
    delay = 30
    while True:
        attempt += 1
        try:
            async with bot:
                for cog in INITIAL_COGS:
                    await bot.load_extension(cog)
                await bot.start(config.DISCORD_TOKEN)
            return  # clean shutdown (SIGINT path) -- no retry needed
        except discord.LoginFailure:
            # Bad token. Retrying won't help, but exiting just makes Render
            # restart-loop forever; stay alive with a long, flat backoff so
            # the log tells you what to fix and the API is never spammed.
            logging.critical(
                "Discord rejected DISCORD_TOKEN (login failure). Check the "
                "token in Render env vars. Retrying every 5 minutes."
            )
            await asyncio.sleep(300)
            delay = 30
        except discord.HTTPException as e:
            # 429 with error code 0 = blocked by global rate limits; wait
            # out the block with growing backoff (capped) before retrying.
            # Any other HTTP error gets a shorter, flat backoff.
            is_429 = e.status == 429
            retry_after = e.retry_after or 0
            wait = max(retry_after, delay) if is_429 else 15
            if is_429:
                delay = min(delay * 2, 900)
            print(
                f"Discord API error at startup (attempt {attempt}): "
                f"{e.status} {e.text or e!r}"
            )
            print(
                f"Rate limit -- waiting {wait:.0f}s before retry. The bot "
                "stays alive so Render doesn't restart-loop against the "
                "rate limit."
            )
            await asyncio.sleep(wait)


if __name__ == "__main__":
    asyncio.run(main())

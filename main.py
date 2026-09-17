import asyncio
import logging
import os

import discord
from aiohttp import web
from discord.ext import commands
from dotenv import load_dotenv

import config
import database

load_dotenv()

logging.basicConfig(level=logging.INFO)

intents = discord.Intents.default()
intents.members = True  # required for on_member_update (arrival role detection)

bot = commands.Bot(command_prefix="!", intents=intents)

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
    print(f"Logged in as {bot.user} ({bot.user.id})")
    synced = await bot.tree.sync()
    print(f"Synced {len(synced)} slash command(s)")


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
    await database.init_pool()
    await start_web_server()
    async with bot:
        for cog in INITIAL_COGS:
            await bot.load_extension(cog)
        await bot.start(config.DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())

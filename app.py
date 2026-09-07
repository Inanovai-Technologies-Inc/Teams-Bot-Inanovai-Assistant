"""
THE SERVER.

Starts a small web server with one important URL: POST /api/messages.
Teams delivers every message there. This file wires that URL to bot.py.

Run it with:   python app.py
"""

import logging
import sys
import traceback
from datetime import datetime

from aiohttp import web
from aiohttp.web import Request, Response, json_response
from botbuilder.core import TurnContext
from botbuilder.core.integration import aiohttp_error_middleware
from botbuilder.integration.aiohttp import (
    CloudAdapter,
    ConfigurationBotFrameworkAuthentication,
)
from botbuilder.schema import Activity, ActivityTypes

from bot import TeamsBot
from config import Config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("app")

CONFIG = Config()

# The adapter handles auth with Microsoft and the request/response plumbing.
ADAPTER = CloudAdapter(ConfigurationBotFrameworkAuthentication(CONFIG))


async def on_error(context: TurnContext, error: Exception):
    """Anything that blows up mid-conversation lands here."""
    print(f"\n [on_turn_error] {error}", file=sys.stderr)
    traceback.print_exc()

    await context.send_activity("The bot hit an error. Please try again.")

    # Surface the error in the Bot Framework Emulator's trace pane.
    if context.activity.channel_id == "emulator":
        await context.send_activity(
            Activity(
                label="TurnError",
                name="on_turn_error Trace",
                timestamp=datetime.utcnow(),
                type=ActivityTypes.trace,
                value=f"{error}",
                value_type="https://www.botframework.com/schemas/error",
            )
        )


ADAPTER.on_turn_error = on_error

BOT = TeamsBot()


async def messages(req: Request) -> Response:
    """Teams POSTs every message here."""
    return await ADAPTER.process(req, BOT)


async def health(req: Request) -> Response:
    """Open this in a browser to confirm the server is up."""
    return json_response(
        {
            "status": "ok",
            "bot": "running",
            "agent": BOT.agent.name,
            "endpoint": "/api/messages",
        }
    )


APP = web.Application(middlewares=[aiohttp_error_middleware])
APP.router.add_post("/api/messages", messages)
APP.router.add_get("/", health)


if __name__ == "__main__":
    log.info("=" * 58)
    log.info("  Teams bot is running")
    log.info("  Health check : http://localhost:%s/", CONFIG.PORT)
    log.info("  Teams posts to: http://localhost:%s/api/messages", CONFIG.PORT)
    log.info("  Agent        : %s", BOT.agent.name)
    log.info("  Press Ctrl+C to stop")
    log.info("=" * 58)
    try:
        web.run_app(APP, host="localhost", port=CONFIG.PORT)
    except Exception as error:
        raise error

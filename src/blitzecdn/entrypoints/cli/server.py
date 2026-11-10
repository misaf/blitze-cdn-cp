"""`blitzecdn serve`: run the HTTP control plane in the foreground.

It lives with the CLI's assembly rather than in a capability because it starts
the assembled API, and nothing outside `entrypoints` may import that.
"""

from __future__ import annotations

from typing import Annotated

import typer
import uvicorn

from blitzecdn.delivery.cli import common
from blitzecdn.entrypoints.api import create_app
from blitzecdn.entrypoints.cli.root import app


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="Bind address.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(min=1, max=65535)] = 8000,
) -> None:
    """Run the authenticated HTTP control plane."""
    settings = common.settings()
    if not settings.api_keys:
        raise typer.BadParameter("configure BLITZE_API_KEYS before starting the API")
    uvicorn.run(
        create_app(settings), host=host, port=port, access_log=True, proxy_headers=False
    )

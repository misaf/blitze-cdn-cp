"""Start the installed API with binding derived from its validated allowlist."""

import uvicorn

from blitzecdn.api.app import create_app
from blitzecdn.core.config import Settings


def main() -> None:
    settings = Settings.from_environment()
    # Public binding is opt-in and every remote request passes the IP gate.
    host = "127.0.0.1"
    if settings.allowed_ips:
        host = "0.0.0.0"  # noqa: S104 # nosec B104
    uvicorn.run(
        create_app(settings),
        host=host,
        port=8000,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()

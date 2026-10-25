"""Start the installed API with binding derived from its validated allowlist."""

import uvicorn

from blitzecdn.core.config import Settings
from blitzecdn.entrypoints.api.app import create_app

#: The installed API port, which the control-plane role also opens in the
#: firewall; `test_role_contracts` holds the role's default to this value.
API_PORT = 8000


def main() -> None:
    settings = Settings.from_environment()
    # Public binding is opt-in and every remote request passes the IP gate.
    host = "127.0.0.1"
    if settings.allowed_ips:
        host = "0.0.0.0"  # noqa: S104 # nosec B104
    uvicorn.run(
        create_app(settings),
        host=host,
        port=API_PORT,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()

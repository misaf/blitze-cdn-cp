"""Start the installed API with binding derived from its validated allowlist."""

import uvicorn

from blitzecdn.api.app import create_app
from blitzecdn.core.config import Settings

#: The port the installed API listens on, and the one the control-plane role
#: admits through the host firewall. Named here rather than written twice
#: because the two are the same fact: a rule on a port nothing serves and a
#: listener no rule admits are both silent, and the second one is silent until
#: an operator is locked out. `test_role_contracts` holds the role's default to
#: this value, the way it already holds the Dockerfile path to its constant.
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

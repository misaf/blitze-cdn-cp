"""MaxMind credentials and the refresh schedule, as typed controller configuration."""

from pydantic import Field, SecretStr
from pydantic.config import JsonDict

from blitzecdn.core.features import ConfigurationModel

#: Kept to the controller: the controller downloads GeoLite2 for the whole
#: fleet, so no playbook, and therefore no edge, is ever handed the key.
_CONTROLLER_ONLY: JsonDict = {"ansible": False}


class GeoIpConfig(ConfigurationModel):
    account_id: SecretStr = Field(
        default=SecretStr(""),
        alias="BLITZE_MAXMIND_ACCOUNT_ID",
        description=("The MaxMind account id the controller downloads GeoLite2 with."),
        json_schema_extra=_CONTROLLER_ONLY,
    )
    license_key: SecretStr = Field(
        default=SecretStr(""),
        alias="BLITZE_MAXMIND_LICENSE_KEY",
        description=("The MaxMind license key the controller downloads GeoLite2 with."),
        json_schema_extra=_CONTROLLER_ONLY,
    )
    refresh_interval_seconds: int = Field(
        default=86_400,
        ge=0,
        alias="BLITZE_GEOIP_REFRESH_INTERVAL_SECONDS",
        description=(
            "How often the controller downloads GeoLite2 for the fleet; 0 "
            "disables the refresh. MaxMind publishes twice a week, so a day "
            "costs one download per controller, not one per edge."
        ),
    )

    @property
    def credentials_configured(self) -> bool:
        return bool(
            self.account_id.get_secret_value() and self.license_key.get_secret_value()
        )


CONFIGURATION = GeoIpConfig.configuration("geoip")

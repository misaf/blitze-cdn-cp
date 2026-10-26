"""MaxMind credentials, declared once as typed controller configuration."""

from pydantic import Field, SecretStr

from blitzecdn.core.features import ConfigurationModel


class GeoIpConfig(ConfigurationModel):
    account_id: SecretStr = Field(
        default=SecretStr(""),
        alias="BLITZE_MAXMIND_ACCOUNT_ID",
        description=("The MaxMind account id the GeoLite2 updater authenticates with."),
    )
    license_key: SecretStr = Field(
        default=SecretStr(""),
        alias="BLITZE_MAXMIND_LICENSE_KEY",
        description=(
            "The MaxMind license key the GeoLite2 updater authenticates with."
        ),
    )


CONFIGURATION = GeoIpConfig.configuration("geoip")

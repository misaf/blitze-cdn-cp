from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class CdnSiteBase(BaseModel):
    server_names: list[str] = Field(min_length=1)
    origin_host: str = Field(min_length=1)
    origin_scheme: Literal["http", "https"] = "https"
    origin_request_host: str | None = None
    origin_sni: str | None = None
    enabled: bool = True
    ssl_enabled: bool = False
    ssl_provider: Literal["custom", "letsencrypt"] = "custom"
    letsencrypt_manage: bool | None = None
    letsencrypt_email: str | None = None
    http2_enabled: bool = True
    cache_enabled: bool = True
    cache_valid_success: str | None = None
    cache_valid_not_found: str | None = None

    @field_validator("server_names")
    @classmethod
    def normalize_server_names(cls, value: list[str]) -> list[str]:
        names = [name.strip().lower() for name in value if name.strip()]
        if not names:
            raise ValueError("server_names cannot be empty")
        return names

    @model_validator(mode="after")
    def default_letsencrypt_manage(self) -> "CdnSiteBase":
        if self.ssl_enabled and self.ssl_provider == "letsencrypt" and self.letsencrypt_manage is None:
            self.letsencrypt_manage = True
        return self


class CdnSiteCreate(CdnSiteBase):
    name: str = Field(pattern=r"^[A-Za-z0-9_.-]+$", min_length=1)


class CdnSite(CdnSiteCreate):
    pass


class CdnSiteUpdate(BaseModel):
    server_names: list[str] | None = Field(default=None, min_length=1)
    origin_host: str | None = None
    origin_scheme: Literal["http", "https"] | None = None
    origin_request_host: str | None = None
    origin_sni: str | None = None
    enabled: bool | None = None
    ssl_enabled: bool | None = None
    ssl_provider: Literal["custom", "letsencrypt"] | None = None
    letsencrypt_manage: bool | None = None
    letsencrypt_email: str | None = None
    http2_enabled: bool | None = None
    cache_enabled: bool | None = None
    cache_valid_success: str | None = None
    cache_valid_not_found: str | None = None


class DeploymentResult(BaseModel):
    started: bool
    returncode: int | None
    stdout: str
    stderr: str

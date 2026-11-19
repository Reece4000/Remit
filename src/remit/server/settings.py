"""How a server is configured, from the environment, as a container is.

REMIT_DATABASE      the PostgreSQL connection string
REMIT_APPLICATION   what to run, as `package.module:name` (an Application, or a
                    function returning one)
REMIT_PROVIDERS     the identity providers trusted, as JSON:
                    [{"issuer": "https://id.example.com/realms/acme",
                      "audience": "remit"}]
REMIT_INSECURE_PROVIDERS
                    allow providers on plain http; for a local identity
                    provider in development only
REMIT_CONNECTIONS   database connections per process (default 10)
"""

import importlib
from dataclasses import dataclass
from typing import Self

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from remit import access
from remit.kernel import Kernel, Module, RegistrationError, Store

from .auth import Provider


@dataclass(frozen=True)
class Application:
    """The modules a deployment runs, with their settings.

    The access module is always added: the API signs members in through it.
    """

    modules: tuple[Module, ...]
    settings: tuple[BaseModel, ...] = ()

    @property
    def installed(self) -> tuple[Module, ...]:
        return (access.module, *self.modules)

    def kernel(self, store: Store) -> Kernel:
        return Kernel(self.installed, settings=self.settings, store=store)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="REMIT_", frozen=True)

    database: str = Field(min_length=1)
    application: str = Field(pattern=r"^[\w.]+:\w+$")
    providers: tuple[Provider, ...] = ()
    insecure_providers: bool = False
    connections: int = Field(default=10, ge=1)

    @model_validator(mode="after")
    def _providers_are_secure(self) -> Self:
        plain = [p.issuer for p in self.providers if not p.issuer.startswith("https://")]
        if plain and not self.insecure_providers:
            raise ValueError(
                f"providers on plain http ({', '.join(plain)}) are refused: their tokens and "
                "keys could be altered in transit; set REMIT_INSECURE_PROVIDERS only for a "
                "local identity provider in development"
            )
        return self

    def load(self) -> Application:
        """The application named by `application`."""
        path, _, name = self.application.partition(":")
        found = getattr(importlib.import_module(path), name, None)
        application = found() if callable(found) and not isinstance(found, Application) else found
        if not isinstance(application, Application):
            raise RegistrationError(f"{self.application} is not an Application")
        return application

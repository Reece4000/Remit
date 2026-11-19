"""Authentication: checking that an identity provider vouches for whoever sent a request.

Remit never sees a password. A person or agent signs in with an OpenID Connect provider
(Keycloak, Zitadel, Entra ID, Okta, Auth0 and others), which issues them a signed access
token; each request carries it. This module checks the token against the provider's
published keys and returns who it names. What that person or agent may do is decided by
the access module, not by anything in the token.

A token is accepted only when all of these hold:

- its issuer is one this server is configured to trust, exactly as configured;
- it is signed, with an asymmetric algorithm, by a key that issuer publishes;
- its audience includes the audience configured for that issuer, which should name this
  API and nothing else, so that a token issued to another service is not accepted here;
- it has a subject, has not expired, and is not for later, allowing a little clock skew.

Keys are found through the issuer's discovery document and cached. A token signed by
a key not yet cached makes the server fetch the keys again, at most once every ten
seconds, so that a provider can rotate its keys without anyone being refused.
"""

import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

import httpx2
from joserfc import jws, jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet
from joserfc.jwt import ClaimsOption
from pydantic import BaseModel, ConfigDict, Field

log = logging.getLogger(__name__)

ALGORITHMS = (
    "RS256",
    "RS384",
    "RS512",
    "PS256",
    "PS384",
    "PS512",
    "ES256",
    "ES384",
    "ES512",
    "EdDSA",
)
"""Signatures that use a key pair. Shared-secret algorithms (HS256 and the like) are
never accepted: a published key is public, so anyone could sign with it as a secret."""

LEEWAY = 30
"""Seconds of clock skew allowed between this server and an identity provider."""

KEYS_KEPT_FOR = 3600.0
"""Seconds an issuer's keys are used before they are fetched again."""

REFETCH_AT_MOST_EVERY = 10.0
"""The least time between fetches of an issuer's keys made because a token named a key
not yet known, however many such tokens arrive."""


class Provider(BaseModel):
    """An identity provider this server trusts."""

    model_config = ConfigDict(frozen=True)

    issuer: str = Field(pattern=r"^https?://\S+$")
    """Exactly as the provider's tokens and discovery document name it."""
    audience: str = Field(min_length=1)
    """What the provider puts in the audience of tokens meant for this API."""
    discovery: str | None = Field(default=None, pattern=r"^https?://\S+$")
    """Where this server fetches the provider's discovery document, when it reaches the
    provider at another address than the issuer, such as inside a private network.
    The document must still name `issuer` as its issuer. By default, the issuer's own
    `/.well-known/openid-configuration`."""

    @property
    def discovered_at(self) -> str:
        return self.discovery or f"{self.issuer.rstrip('/')}/.well-known/openid-configuration"


@dataclass(frozen=True)
class Identity:
    """Who a verified token names, and until when it may be relied on."""

    issuer: str
    subject: str
    expires: float
    """When the token expires, in seconds since the epoch."""


class _Unfetchable(Exception):
    """A provider's keys are not to be had from what it publishes."""


class Unauthenticated(Exception):
    """The request carried no token that this server accepts.

    The message says why, for whoever operates the server; it is safe to show the
    sender too, since it names nothing secret.
    """


class Verifier:
    def __init__(
        self,
        providers: tuple[Provider, ...],
        *,
        http: httpx2.Client,
        clock: Callable[[], float] = time.time,
        leeway: int = LEEWAY,
    ) -> None:
        self._issuers = {provider.issuer: _Issuer(provider, http, clock) for provider in providers}
        self._clock = clock
        self._leeway = leeway

    def verify(self, token: str) -> Identity:
        """Who the token names, or `Unauthenticated` saying why it is refused."""
        try:
            # The payload is read before the signature is checked, only to learn which
            # trusted issuer's keys to check it with. Nothing else is taken from it.
            unverified = jws.extract_compact(token.encode())
            claimed = _json_object(unverified.payload)
        except JoseError, ValueError, UnicodeError:
            raise Unauthenticated("the token is not a signed JWT") from None
        named = claimed.get("iss")
        issuer = self._issuers.get(named) if isinstance(named, str) else None
        if issuer is None:
            raise Unauthenticated("the token was not issued by a provider this server trusts")

        kid = unverified.headers().get("kid")
        try:
            verified = jwt.decode(token, issuer.keys(kid), algorithms=ALGORITHMS)
        except JoseError as error:
            raise Unauthenticated(f"the token's signature is not valid: {_said(error)}") from None

        claims = verified.claims
        registry = jwt.JWTClaimsRegistry(
            now=lambda: int(self._clock()), leeway=self._leeway, **_required(issuer.provider)
        )
        try:
            registry.validate(claims)
        except JoseError as error:
            raise Unauthenticated(f"the token is not acceptable: {_said(error)}") from None
        return Identity(issuer.provider.issuer, cast(str, claims["sub"]), float(claims["exp"]))


class _Issuer:
    """One trusted issuer, and the keys it publishes, fetched as they are needed."""

    def __init__(self, provider: Provider, http: httpx2.Client, clock: Callable[[], float]) -> None:
        self.provider = provider
        self._http = http
        self._clock = clock
        self._lock = threading.Lock()
        self._keys: KeySet | None = None
        self._fetched = float("-inf")
        """When the keys held were fetched."""
        self._refetched = float("-inf")
        """When they were last fetched early, for a token naming a key not held."""
        self._failed = float("-inf")
        """When fetching them last failed."""

    def keys(self, kid: object) -> KeySet:
        """The keys to check a token with, fetched when none are held, when they are due,
        or when the token names a key not held. While fetching fails, the last keys
        fetched go on being used, and fetching is tried again only now and then."""
        with self._lock:
            now = self._clock()
            if self._keys is None or now - self._fetched >= KEYS_KEPT_FOR:
                if now - self._failed >= REFETCH_AT_MOST_EVERY:
                    self._refresh(now)
            elif not _holds(self._keys, kid) and now - self._refetched >= REFETCH_AT_MOST_EVERY:
                self._refetched = now
                self._refresh(now)
            if self._keys is None:
                raise Unauthenticated(f"the keys of {self.provider.issuer} cannot be fetched")
            return self._keys

    def _refresh(self, now: float) -> None:
        try:
            self._keys, self._fetched = self._fetch(), now
        except (_Unfetchable, httpx2.HTTPError, ValueError, JoseError) as error:
            # The reason may name addresses inside the network, so it goes to the log, not
            # to whoever sent the token.
            self._failed = now
            log.warning("could not fetch the keys of %s: %s", self.provider.issuer, error)

    def _fetch(self) -> KeySet:
        """The issuer's keys, from its discovery document."""
        discovery = self._get(self.provider.discovered_at)
        if discovery.get("issuer") != self.provider.issuer:
            raise _Unfetchable(f"its discovery document names {discovery.get('issuer')!r}")
        jwks_uri = discovery.get("jwks_uri")
        if not isinstance(jwks_uri, str):
            raise _Unfetchable("its discovery document names no jwks_uri")
        return KeySet.import_key_set(cast(Any, self._get(jwks_uri)))

    def _get(self, url: str) -> dict[str, Any]:
        response = self._http.get(url, timeout=10)
        response.raise_for_status()
        return _json_object(response.content)


def _required(provider: Provider) -> dict[str, ClaimsOption]:
    """The claims a token must carry, and the values that some of them must have."""
    return {
        "iss": {"essential": True, "value": provider.issuer},
        "aud": {"essential": True, "value": provider.audience},
        "sub": {"essential": True},
        "exp": {"essential": True},
    }


def _said(error: JoseError) -> str:
    return error.description or error.error.replace("_", " ")


def _holds(keys: KeySet, kid: object) -> bool:
    return any(key.kid == kid for key in keys.keys)


def _json_object(content: bytes) -> dict[str, Any]:
    found = json.loads(content)
    if not isinstance(found, dict):
        raise ValueError("expected a JSON object")
    return cast(dict[str, Any], found)

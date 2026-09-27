"""Cloudflare Access identity.

When the dashboard sits behind a Cloudflare Access application, every request
that reaches us carries a signed JWT (`Cf-Access-Jwt-Assertion` header, also the
`CF_Authorization` cookie). Verifying it against the team's public keys and the
application's audience tag tells us *who* is making the request, so the shared
admin password isn't needed and the event log can name people.

Never trust the email in a request without this verification: anyone reaching
the origin some other way could forge the header.
"""

from __future__ import annotations

import logging

import jwt
from jwt import PyJWKClient

log = logging.getLogger("controller.access")


class AccessVerifier:
    def __init__(self, team_domain: str, audience: str):
        team_domain = team_domain.strip().removeprefix("https://").rstrip("/")
        self.team_domain = team_domain
        self.audience = audience.strip()
        self.issuer = f"https://{team_domain}"
        self._jwks = PyJWKClient(f"{self.issuer}/cdn-cgi/access/certs", cache_keys=True, lifespan=3600)

    @property
    def logout_url(self) -> str:
        return "/cdn-cgi/access/logout"

    def verify(self, token: str | None) -> str | None:
        """Return the authenticated email, or None if the token is missing/invalid."""
        if not token:
            return None
        try:
            key = self._jwks.get_signing_key_from_jwt(token)
            claims = jwt.decode(token, key.key, algorithms=["RS256"], audience=self.audience,
                                issuer=self.issuer, options={"require": ["exp", "iat", "aud", "iss"]})
        except jwt.PyJWTError as exc:
            log.warning("rejected Cloudflare Access token: %s", exc)
            return None
        email = claims.get("email")
        if email:
            return str(email).lower()
        # Service tokens carry no email; identify them by their client id.
        common = claims.get("common_name")
        return f"service-token:{common}" if common else None

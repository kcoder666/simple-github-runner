import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.access import AccessVerifier
from app.store import Store, current_actor

TEAM = "cremi.cloudflareaccess.com"
AUD = "a" * 64


def _key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def signer():
    key = _key()
    verifier = AccessVerifier(TEAM, AUD)
    # Stand in for the team's JWKS endpoint.
    verifier._jwks = SimpleNamespace(get_signing_key_from_jwt=lambda token: SimpleNamespace(key=key.public_key()))

    def sign(claims=None, signing_key=key, **overrides):
        now = int(time.time())
        payload = {"aud": [AUD], "iss": f"https://{TEAM}", "iat": now, "exp": now + 600,
                   "email": "Alice@Cremi.ai", **(claims or {})}
        payload.update(overrides)
        return jwt.encode(payload, signing_key, algorithm="RS256")

    return verifier, sign


def test_valid_token_returns_lowercased_email(signer):
    verifier, sign = signer
    assert verifier.verify(sign()) == "alice@cremi.ai"


def test_missing_token(signer):
    verifier, _ = signer
    assert verifier.verify(None) is None and verifier.verify("") is None


@pytest.mark.parametrize("override", [
    {"aud": ["some-other-app"]},
    {"iss": "https://evil.cloudflareaccess.com"},
    {"exp": int(time.time()) - 10},
])
def test_rejects_wrong_claims(signer, override):
    verifier, sign = signer
    assert verifier.verify(sign(**override)) is None


def test_rejects_forged_signature(signer):
    verifier, sign = signer
    assert verifier.verify(sign(signing_key=_key())) is None


def test_service_token_identity(signer):
    verifier, sign = signer
    token = sign(claims={"email": None, "common_name": "abc.access"})
    assert verifier.verify(token) == "service-token:abc.access"


def test_team_domain_normalised():
    assert AccessVerifier("https://cremi.cloudflareaccess.com/", AUD).issuer == f"https://{TEAM}"


def test_events_record_actor(tmp_path):
    store = Store(str(tmp_path))
    store.add_event("info", "auto", "controller did this")
    token = current_actor.set("alice@cremi.ai")
    try:
        store.add_event("info", "target", "target web updated")
    finally:
        current_actor.reset(token)
    events = {e["message"]: e["actor"] for e in store.events()}
    assert events == {"controller did this": None, "target web updated": "alice@cremi.ai"}

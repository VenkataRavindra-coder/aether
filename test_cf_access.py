"""Offline tests for cf_access.py's JWT validation. No real network calls -- the JWKS client is
replaced with a fake that serves a locally-generated RSA keypair, so this tests the actual
verification logic (signature, audience, expiry) without touching Cloudflare.
Run: python3 test_cf_access.py
"""
import os
import time
import importlib

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

results = []


def t(name, cond):
    results.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL") + ": " + name)


# ---------- unconfigured: must pass everything through, never block ----------
os.environ["CF_ACCESS_TEAM_DOMAIN"] = ""
os.environ["CF_ACCESS_AUD"] = ""
import cf_access
importlib.reload(cf_access)

t("configured() is False with nothing set", cf_access.configured() is False)
t("verify() passes through (True) when not configured, even with garbage input", cf_access.verify("not-a-real-token") is True)
t("verify() passes through (True) when not configured, even with an empty token", cf_access.verify("") is True)

# ---------- configured: real signature/audience/expiry checks ----------
os.environ["CF_ACCESS_TEAM_DOMAIN"] = "test-team.cloudflareaccess.com"
os.environ["CF_ACCESS_AUD"] = "test-audience-tag"
importlib.reload(cf_access)
t("configured() is True once both env vars are set", cf_access.configured() is True)

private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class FakeSigningKey:
    def __init__(self, key):
        self.key = key


class FakeJWKSClient:
    """Stands in for PyJWKClient: returns the same test public key for any token, instead of
    fetching real JWKS over the network."""
    def get_signing_key_from_jwt(self, token):
        return FakeSigningKey(private_key.public_key())


cf_access._client = FakeJWKSClient()  # skip the real network-backed client entirely

now = int(time.time())
good_token = jwt.encode(
    {"aud": "test-audience-tag", "exp": now + 3600, "iat": now, "email": "ravindra@example.com"},
    private_key, algorithm="RS256",
)
t("a correctly signed token with the right audience verifies", cf_access.verify(good_token) is True)

wrong_aud_token = jwt.encode(
    {"aud": "some-other-app", "exp": now + 3600, "iat": now}, private_key, algorithm="RS256",
)
t("a token for a DIFFERENT Access application (wrong audience) is rejected", cf_access.verify(wrong_aud_token) is False)

expired_token = jwt.encode(
    {"aud": "test-audience-tag", "exp": now - 10, "iat": now - 3600}, private_key, algorithm="RS256",
)
t("an expired token is rejected", cf_access.verify(expired_token) is False)

other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
forged_token = jwt.encode(
    {"aud": "test-audience-tag", "exp": now + 3600, "iat": now}, other_key, algorithm="RS256",
)
t("a token signed with a DIFFERENT private key (forged/not actually from Access) is rejected", cf_access.verify(forged_token) is False)

t("an empty token is rejected outright when configured", cf_access.verify("") is False)
t("garbage input is rejected, not an exception", cf_access.verify("not-a-real-token") is False)

fails = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(fails)}/{len(results)} passed")
if fails:
    print("FAILED:", fails)
    raise SystemExit(1)

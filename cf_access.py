"""Validates Cloudflare Access JWTs (the Cf-Access-Jwt-Assertion header), so the app itself can
confirm a request actually passed through Access -- a second line of defense independent of the
edge policy. Without this, a single Access misconfiguration (a path pattern that's too wide, a
second hostname pointed at the same origin, a stale DNS record) would expose /login and every
/api/* route to the open internet with nothing in the app itself to catch it.

Needs two values from the Cloudflare Zero Trust dashboard (Access -> Applications -> this app):
  CF_ACCESS_TEAM_DOMAIN   e.g. "yourteam.cloudflareaccess.com"
  CF_ACCESS_AUD           the Application Audience (AUD) tag shown on the application's page

Until BOTH are set in .env, verify() always returns True (logged once) -- this is deliberately
an additive layer on top of the Access policy, not a replacement for it, so an install that
hasn't configured it yet keeps working exactly as before instead of locking anyone out the
moment this code ships.
"""
import logging
import os
import threading

logger = logging.getLogger("AETHER_CF_ACCESS")

TEAM_DOMAIN = os.getenv("CF_ACCESS_TEAM_DOMAIN", "").strip()
AUD = os.getenv("CF_ACCESS_AUD", "").strip()

_client = None
_client_lock = threading.Lock()
_warned_unconfigured = False


def configured() -> bool:
    return bool(TEAM_DOMAIN and AUD)


def _jwks_client():
    """One PyJWKClient per process, lazily created. It does its own fetching, caching and key
    rotation (5 minute default lifespan) -- no need to hand-roll any of that here."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                from jwt import PyJWKClient
                _client = PyJWKClient(f"https://{TEAM_DOMAIN}/cdn-cgi/access/certs", timeout=10)
    return _client


def verify(token: str) -> bool:
    """True if `token` (the Cf-Access-Jwt-Assertion header value) is a valid, current, correctly
    audienced Access JWT. Never raises: a bad signature, an expired token, a JWKS fetch failure,
    or validation simply not being configured yet are all handled without throwing, since this
    runs on every protected request and a crash here must not take the whole dashboard down."""
    global _warned_unconfigured
    if not configured():
        if not _warned_unconfigured:
            logger.warning("CF_ACCESS_TEAM_DOMAIN / CF_ACCESS_AUD not set in .env -- Access JWT "
                           "validation is NOT active; the app is relying on the Cloudflare Access "
                           "edge policy alone. See strategy.md for how to turn this on.")
            _warned_unconfigured = True
        return True
    if not token:
        return False
    try:
        import jwt
        signing_key = _jwks_client().get_signing_key_from_jwt(token)
        jwt.decode(token, signing_key.key, algorithms=["RS256"], audience=AUD)
        return True
    except Exception as e:
        logger.warning(f"Access JWT rejected: {type(e).__name__}: {str(e)[:150]}")
        return False

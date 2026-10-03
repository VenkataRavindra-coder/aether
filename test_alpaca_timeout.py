"""Offline test that every Alpaca HTTP call actually carries a timeout. alpaca-py's own
TradingClient/RESTClient take no timeout parameter at all; this is only enforced by mounting a
custom requests.adapters.HTTPAdapter onto the client's session. No network, no real credentials.
Run: python3 test_alpaca_timeout.py
"""
import os

os.environ["APCA_API_KEY_ID"] = "test-key"
os.environ["APCA_API_SECRET_KEY"] = "test-secret"
os.environ.setdefault("RISK_LOG", "0")

import requests.adapters
from alpaca_executor import AlpacaExecutionClient, _TimeoutHTTPAdapter, ALPACA_HTTP_TIMEOUT_SECONDS

results = []


def t(name, cond):
    results.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL") + ": " + name)


c = AlpacaExecutionClient(paper=True)
t("the client actually initialized (not simulation/stub mode)", c.client is not None)

adapter_https = c.client._session.get_adapter("https://paper-api.alpaca.markets/v2/account")
adapter_http = c.client._session.get_adapter("http://paper-api.alpaca.markets/v2/account")
t("our timeout adapter is mounted for https://", isinstance(adapter_https, _TimeoutHTTPAdapter))
t("our timeout adapter is mounted for http:// too", isinstance(adapter_http, _TimeoutHTTPAdapter))
t("the configured default timeout is a sane positive number",
  isinstance(ALPACA_HTTP_TIMEOUT_SECONDS, float) and 0 < ALPACA_HTTP_TIMEOUT_SECONDS < 120)

# Prove the adapter actually injects the timeout into the real send() call path, by stubbing
# just the base class's send() (one level up) and inspecting what it receives.
captured = {}


class FakeResp:
    status_code = 200
    headers = {}


orig_send = requests.adapters.HTTPAdapter.send


def fake_send(self, request, **kwargs):
    captured.update(kwargs)
    return FakeResp()


requests.adapters.HTTPAdapter.send = fake_send
try:
    adapter_https.send(requests.PreparedRequest())
    t("a call with no explicit timeout gets the default injected",
      captured.get("timeout") == ALPACA_HTTP_TIMEOUT_SECONDS)

    captured.clear()
    adapter_https.send(requests.PreparedRequest(), timeout=3)
    t("an explicit timeout passed by the caller is respected, not overridden",
      captured.get("timeout") == 3)
finally:
    requests.adapters.HTTPAdapter.send = orig_send

# A second client (e.g. the US live broker) must get its own independently-working adapter too.
c2 = AlpacaExecutionClient(paper=False, key_env="APCA_API_KEY_ID", secret_env="APCA_API_SECRET_KEY")
adapter2 = c2.client._session.get_adapter("https://api.alpaca.markets/v2/account")
t("a second client instance also has the timeout adapter mounted", isinstance(adapter2, _TimeoutHTTPAdapter))

fails = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(fails)}/{len(results)} passed")
if fails:
    print("FAILED:", fails)
    raise SystemExit(1)

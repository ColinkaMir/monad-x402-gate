#!/usr/bin/env python3
"""Offline smoke test: boots the gate against the sample data and checks the
unpaid surface. No network, no chain, stdlib only.

    python3 test_gate.py

The paid path talks to a real chain by design; test it end to end against
testnet with faucet tMON and demo-client.mjs (see CONTRIBUTING.md).
"""

import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

PORT = 8489
HERE = os.path.dirname(os.path.abspath(__file__))
URL = f"http://127.0.0.1:{PORT}/monad/x402/network-concentration-report"


def req(url, method="GET", headers=None):
    r = urllib.request.Request(url, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def main():
    env = {**os.environ,
           "X402_PORT": str(PORT),
           "X402_DB": "/tmp/x402-gate-smoke.db",
           "X402_DATA_DIR": os.path.join(HERE, "data")}
    if os.path.exists("/tmp/x402-gate-smoke.db"):
        os.unlink("/tmp/x402-gate-smoke.db")
    proc = subprocess.Popen([sys.executable, os.path.join(HERE, "gate.py")], env=env)
    try:
        for _ in range(50):
            try:
                req(URL)
                break
            except Exception:
                time.sleep(0.1)

        # 1. The unpaid GET is a 402 carrying the offer in body AND v2 header.
        status, headers, body = req(URL)
        assert status == 402, status
        offer = json.loads(body)
        assert offer["x402Version"] == 2
        assert offer["accepts"][0]["payTo"].startswith("0x")
        assert offer["extensions"]["bazaar"]["info"]["input"]["method"] == "GET"
        hdr = headers.get("PAYMENT-REQUIRED")
        assert hdr and json.loads(base64.b64decode(hdr))["accepts"], "v2 header missing"

        # 2. HEAD answers like GET, minus the body (RFC 9110 6.4.2).
        status, headers, body = req(URL, method="HEAD")
        assert status == 402 and body == b"" and headers.get("PAYMENT-REQUIRED")

        # 3. Unknown resource is a 404 that names what exists.
        status, _, body = req(f"http://127.0.0.1:{PORT}/monad/x402/nope")
        assert status == 404 and b"available" in body

        # 4. A malformed payment header fails loudly, not silently.
        status, _, body = req(URL, headers={"X-PAYMENT": "not base64!!"})
        assert status == 400, status

        # 5. The report the paid path would serve builds from the sample data.
        sys.path.insert(0, HERE)
        import gate
        report = gate.build_report()
        assert set(report["networks"]) == {"mainnet", "testnet"}
        assert report["networks"]["mainnet"]["hhi"]["provider_asn"] > 0

        print("SMOKE_OK: 402 offer (body+header), HEAD parity, 404, bad-payment 400, report from samples")
    finally:
        proc.terminate()


if __name__ == "__main__":
    main()

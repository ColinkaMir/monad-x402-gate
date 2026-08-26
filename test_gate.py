#!/usr/bin/env python3
"""Offline smoke test: boots the gate against the sample data and checks the
unpaid surface of every resource. No network, no chain, stdlib only.

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
BASE = f"http://127.0.0.1:{PORT}/monad/x402"
URL = f"{BASE}/network-concentration-report"


def req(url, method="GET", headers=None):
    r = urllib.request.Request(url, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def paid_path(gate):
    """Exercise the paid branch against a stubbed chain.

    The real thing needs faucet MON and a key, so CI cannot run it; what CAN be
    checked without money is everything the gate itself decides — that a payment
    buys the resource it was made for, that it works exactly once, that a
    cheaper payment does not open the bundle, and that a data outage does not
    consume it.
    """
    import threading
    from http.server import ThreadingHTTPServer

    now = int(time.time())
    paid_value = {"wei": gate.MICRO_PRICE_WEI}

    def fake_rpc(method, params):
        if method == "eth_getTransactionByHash":
            return {"to": gate.PAY_TO, "value": hex(paid_value["wei"])}
        if method == "eth_getTransactionReceipt":
            return {"status": "0x1", "blockNumber": "0x64"}
        if method == "eth_getBlockByNumber":
            return {"timestamp": hex(now)}
        return None

    gate.rpc = fake_rpc
    srv = ThreadingHTTPServer(("127.0.0.1", PORT + 1), gate.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{PORT + 1}/monad/x402"

    def pay(name, tx):
        return req(f"{base}/{name}",
                   headers={"X-PAYMENT": base64.b64encode(
                       json.dumps({"txHash": tx}).encode()).decode()})

    try:
        # a. a valid payment returns the answer and names the resource it bought
        tx1 = "0x" + "11" * 32
        status, headers, body = pay("concentration-hhi", tx1)
        assert status == 200, (status, body[:200])
        assert "hhi" in json.loads(body)["networks"]["mainnet"]
        receipt = json.loads(base64.b64decode(headers["X-PAYMENT-RESPONSE"]))
        assert receipt["resource"] == "concentration-hhi", receipt

        # b. the same tx a second time is spent, whatever it is pointed at
        status, _, body = pay("concentration-hhi", tx1)
        assert status == 402 and b"already used" in body, body[:200]
        status, _, body = pay("latency-bands", tx1)
        assert status == 402 and b"already used" in body, body[:200]

        # c. the single-question price does not open the bundle
        status, _, body = pay("network-concentration-report", "0x" + "22" * 32)
        assert status == 402 and b"insufficient amount" in body, body[:200]

        # d. a data outage answers 503 and leaves the payment UNSPENT, so the
        #    same tx still works once the feed is back. This is why the burn
        #    happens after the answer is built, not inside verification.
        tx2 = "0x" + "33" * 32
        good_dir, gate.GEO_DIR = gate.GEO_DIR, "/nonexistent-feed-dir"
        status, _, body = pay("top-providers", tx2)
        assert status == 503 and b"not consumed" in body, (status, body[:200])
        gate.GEO_DIR = good_dir
        status, _, body = pay("top-providers", tx2)
        assert status == 200 and json.loads(body)["networks"]["mainnet"]["providers"], body[:200]
    finally:
        srv.shutdown()


def main():
    # Set these in OUR environment too, not just the child's: the test imports
    # gate in-process for the paid-path checks, and an unconfigured import would
    # write a replay DB into the working directory and read the wrong data dir.
    os.environ["X402_PORT"] = str(PORT)
    os.environ["X402_DB"] = "/tmp/x402-gate-smoke.db"
    os.environ["X402_DATA_DIR"] = os.path.join(HERE, "data")
    env = dict(os.environ)
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

        sys.path.insert(0, HERE)
        import gate

        # 1. The catalogue is free and lists every resource with its price, so an
        #    agent can pick one question instead of paying for the bundle.
        status, _, body = req(BASE)
        assert status == 200, status
        cat = json.loads(body)
        names = [r["name"] for r in cat["resources"]]
        assert set(names) == set(gate.RESOURCES), names
        assert len(names) >= 5, names
        prices = {r["name"]: int(r["price"]["native_mon_wei"]) for r in cat["resources"]}
        bundle = prices["network-concentration-report"]
        assert all(p < bundle for n, p in prices.items() if n != "network-concentration-report"), prices

        # 2. EVERY resource answers an unpaid GET with a 402 carrying its own offer
        #    in the body AND in the v2 header, and its own price.
        for name, res in gate.RESOURCES.items():
            status, headers, body = req(f"{BASE}/{name}")
            assert status == 402, (name, status)
            offer = json.loads(body)
            assert offer["x402Version"] == 2, name
            assert offer["accepts"][0]["amount"] == str(res["price_wei"]), name
            assert offer["resource"]["url"].endswith(name), name
            assert offer["extensions"]["bazaar"]["info"]["input"]["method"] == "GET", name
            hdr = headers.get("PAYMENT-REQUIRED")
            assert hdr and json.loads(base64.b64decode(hdr))["accepts"], f"v2 header missing on {name}"
            # the bazaar example must be the shape this resource really returns
            ex = offer["extensions"]["bazaar"]["info"]["output"]["example"]
            assert "networks" in ex and "mainnet" in ex["networks"], name

        # 3. A browser gets the catalogue as a page, an agent as JSON: same URL.
        status, headers, body = req(BASE, headers={"Accept": "text/html"})
        assert status == 200 and headers["Content-Type"].startswith("text/html"), status
        assert b"<table>" in body and b"network-concentration-report" in body

        # 4. HEAD answers like GET, minus the body (RFC 9110 6.4.2).
        status, headers, body = req(URL, method="HEAD")
        assert status == 402 and body == b"" and headers.get("PAYMENT-REQUIRED")

        # 5. Unknown resource is a 404 that names what exists.
        status, _, body = req(f"{BASE}/nope")
        assert status == 404 and b"available" in body

        # 6. A malformed payment header fails loudly, not silently.
        status, _, body = req(URL, headers={"X-PAYMENT": "not base64!!"})
        assert status == 400, status

        # 7. Every resource builds a real answer from the sample data, and the
        #    single-question ones are genuinely smaller than the bundle.
        sizes = {}
        for name, res in gate.RESOURCES.items():
            out = res["build"]()
            assert set(out["networks"]) == {"mainnet", "testnet"}, name
            assert gate.has_answer(out), name
            sizes[name] = len(json.dumps(out))
        assert sizes["concentration-hhi"] < sizes["network-concentration-report"] / 10, sizes
        rep = gate.RESOURCES["network-concentration-report"]["build"]()
        assert rep["networks"]["mainnet"]["hhi"]["provider_asn"] > 0

        # 8. The split answers agree with the bundle — one pipeline, not five.
        hhi = gate.build_hhi()["networks"]["mainnet"]
        bft = gate.build_bft_thresholds()["networks"]["mainnet"]
        m = rep["networks"]["mainnet"]
        assert hhi["hhi"] == m["hhi"], (hhi["hhi"], m["hhi"])
        assert bft["min_providers_to_33_pct"] == m["bft_thresholds"]["min_providers_to_33_pct"]
        assert bft["hosting_nakamoto_coefficient"] == bft["min_providers_to_33_pct"]
        top = gate.build_top_providers()["networks"]["mainnet"]["providers"]
        assert top[0]["cum_pct"] == top[0]["stake_pct"]
        assert all(top[i]["cum_pct"] >= top[i - 1]["cum_pct"] for i in range(1, len(top)))

        # 9. A payment is only consumed once, and the INSERT is what decides it.
        assert gate.burn_payment("0x" + "ab" * 32, "network-concentration-report") is True
        assert gate.burn_payment("0x" + "ab" * 32, "network-concentration-report") is False

        paid_path(gate)

        print(f"SMOKE_OK: catalogue + {len(gate.RESOURCES)} resources (402 body+header, own price), "
              "human page, HEAD parity, 404, bad-payment 400, split agrees with bundle, replay burn once, "
              "paid path + per-resource price + outage-does-not-charge (stubbed chain)")
    finally:
        proc.terminate()


if __name__ == "__main__":
    main()

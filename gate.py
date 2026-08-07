#!/usr/bin/env python3
"""x402-style pay-per-call gate for prooflines premium data (Monad testnet).

Scheme "monad-native-exact", self-verified (no facilitator): the client pays
native tMON to PAY_TO, retries with X-PAYMENT: base64({"txHash": "0x.."}),
we verify the tx via public RPC (recipient/value/status/freshness) and burn
the hash in a sqlite replay store. Loopback-only; nginx proxies /monad/x402/.
Watch-only: no keys on this host.
"""

import base64, json, os, sqlite3, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.getenv("X402_PORT", "8412"))
PAY_TO = os.getenv("X402_PAY_TO", "0x7e3a2040F8D13a425E93C797630aB9828B59c806")
PRICE_WEI = int(os.getenv("X402_PRICE_WEI", str(10**16)))  # default 0.01 MON
FRESH_SECONDS = 900
RPC = os.getenv("X402_RPC", "https://testnet-rpc.monad.xyz")
CHAIN_ID = int(os.getenv("X402_CHAIN_ID", "10143"))
NETWORK = os.getenv("X402_NETWORK", "monad-testnet")
DB = os.getenv("X402_DB", "./replay.db")
GEO_DIR = os.getenv("X402_DATA_DIR", "./data")
RESOURCE = "network-concentration-report"
X402_VERSION = 1


def rpc(method, params):
    req = urllib.request.Request(RPC, json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "prooflines-x402"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r).get("result")


def payment_requirements():
    return {
        "x402Version": X402_VERSION,
        "error": "payment required",
        "accepts": [{
            "scheme": "monad-native-exact",
            "network": NETWORK,
            "chainId": CHAIN_ID,
            "payTo": PAY_TO,
            "maxAmountRequired": str(PRICE_WEI),
            "asset": "native",
            "resource": f"/monad/x402/{RESOURCE}",
            "description": "Prooflines network concentration report (mainnet+testnet, "
                           "HHI by ASN/country/continent, BFT threshold distances, epoch deltas). "
                           f"Pay the exact amount in native MON on {NETWORK}, then retry with "
                           "header X-PAYMENT: base64({\"txHash\": \"0x..\"}).",
            "validityWindowSeconds": FRESH_SECONDS,
        }],
    }


def db():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    c = sqlite3.connect(DB)
    c.execute("CREATE TABLE IF NOT EXISTS used (txhash TEXT PRIMARY KEY, ts INTEGER, resource TEXT)")
    return c


def verify_payment(tx_hash):
    """Returns (ok, error_string, block_number)."""
    if not (isinstance(tx_hash, str) and tx_hash.startswith("0x") and len(tx_hash) == 66):
        return False, "malformed txHash", None
    tx_hash = tx_hash.lower()
    c = db()
    if c.execute("SELECT 1 FROM used WHERE txhash=?", (tx_hash,)).fetchone():
        return False, "payment already used", None
    tx = rpc("eth_getTransactionByHash", [tx_hash])
    if not tx:
        return False, "transaction not found", None
    if (tx.get("to") or "").lower() != PAY_TO.lower():
        return False, "wrong recipient", None
    if int(tx.get("value", "0x0"), 16) < PRICE_WEI:
        return False, "insufficient amount", None
    rcpt = rpc("eth_getTransactionReceipt", [tx_hash])
    if not rcpt or rcpt.get("status") != "0x1":
        return False, "transaction not confirmed successful", None
    blk = rpc("eth_getBlockByNumber", [rcpt["blockNumber"], False])
    if not blk or time.time() - int(blk["timestamp"], 16) > FRESH_SECONDS:
        return False, "payment too old (validity window expired)", None
    c.execute("INSERT INTO used VALUES (?,?,?)", (tx_hash, int(time.time()), RESOURCE))
    c.commit()
    return True, None, int(rcpt["blockNumber"], 16)


def hhi(shares):
    return round(sum(s * s for s in shares), 2)  # shares in percent -> HHI 0..10000


def build_report():
    out = {"generated_at_utc": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
           "source": "prooflines geo-latency pipeline", "networks": {}}
    for net, cur_f, hist_f in [("mainnet", "geo-latency-data-mainnet.json", "geo-latency-history-mainnet.json"),
                               ("testnet", "geo-latency-data.json", "geo-latency-history.json")]:
        try:
            d = json.load(open(os.path.join(GEO_DIR, cur_f)))
        except Exception:
            continue
        provs = d.get("providers") or []
        conts = d.get("continents") or []
        ctrys = d.get("countries") or []
        def shares(rows):
            return [float(r.get("stake_share_pct") or r.get("share_pct") or r.get("stake_pct") or 0) for r in rows]
        p_sh = sorted(shares(provs), reverse=True)
        top = lambda n: round(sum(p_sh[:n]), 2)
        net_out = {
            "epoch": d.get("epoch"), "as_of": d.get("generated_at_utc"),
            "hhi": {"provider_asn": hhi(p_sh), "country": hhi(shares(ctrys)), "continent": hhi(shares(conts))},
            "top_provider_share_pct": {"top1": top(1), "top2": top(2), "top4": top(4)},
            "bft_thresholds": {
                "liveness_33_distance_pct": round(33.33 - top(1), 2),
                "min_providers_to_33_pct": next((i + 1 for i in range(len(p_sh)) if sum(p_sh[:i + 1]) >= 33.33), None),
                "min_providers_to_50_pct": next((i + 1 for i in range(len(p_sh)) if sum(p_sh[:i + 1]) >= 50.0), None),
            },
            "providers_full": provs, "countries_full": ctrys, "continents_full": conts,
        }
        try:
            hist = json.load(open(os.path.join(GEO_DIR, hist_f)))
            if isinstance(hist, list) and len(hist) >= 2:
                net_out["history_points"] = len(hist)
                net_out["history_tail"] = hist[-8:]
        except Exception:
            pass
        out["networks"][net] = net_out
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "prooflines-x402/1.0"

    def _json(self, code, obj, extra=None):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")
        if not path.endswith(RESOURCE):
            self._json(404, {"error": "unknown resource",
                             "available": [f"/monad/x402/{RESOURCE}"]})
            return
        hdr = self.headers.get("X-PAYMENT")
        if not hdr:
            reqs = payment_requirements()
            self._json(402, reqs, {"X-PAYMENT-REQUIRED":
                       base64.b64encode(json.dumps(reqs).encode()).decode()})
            return
        try:
            payload = json.loads(base64.b64decode(hdr))
            tx_hash = payload.get("txHash", "")
        except Exception:
            self._json(400, {"error": "X-PAYMENT must be base64 of {\"txHash\": \"0x..\"}"})
            return
        try:
            ok, err, blk = verify_payment(tx_hash)
        except Exception as e:
            self._json(502, {"error": f"verification backend error: {e}"})
            return
        if not ok:
            reqs = payment_requirements()
            reqs["error"] = f"payment rejected: {err}"
            self._json(402, reqs)
            return
        resp = {"ok": True, "txHash": tx_hash.lower(), "block": blk, "scheme": "monad-native-exact"}
        self._json(200, build_report(), {"X-PAYMENT-RESPONSE":
                   base64.b64encode(json.dumps(resp).encode()).decode()})

    def log_message(self, fmt, *args):
        print("%s %s" % (self.address_string(), fmt % args))


if __name__ == "__main__":
    db()
    print(f"x402 gate on 127.0.0.1:{PORT}, payTo {PAY_TO}, price {PRICE_WEI} wei")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()

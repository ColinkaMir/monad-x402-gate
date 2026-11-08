#!/usr/bin/env python3
"""x402-style pay-per-call gate for prooflines premium data (Monad).

Scheme "monad-native-exact", self-verified (no facilitator): the client pays
native MON to PAY_TO, retries with X-PAYMENT: base64({"txHash": "0x.."}),
we verify the tx via public RPC (recipient/value/status/freshness) and burn
the hash in a sqlite replay store. Loopback-only; nginx proxies /monad/x402/.
Watch-only: no keys on this host.

The gate serves SEVERAL resources, not one. Each is a single question with its
own price, its own description and its own discovery entry, because that is the
shape that actually sells on x402: the operators with repeat customers run many
small well-described endpoints, not one large report (see the market analysis in
the README). The full report stays as the bundle for whoever wants everything.

What you pay for is the COMPUTED ANSWER, not access to the data: the underlying
measurements are published free at https://prooflines.org/monad/geo-latency/ and
every description says so.
"""

import base64, json, os, sqlite3, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.getenv("X402_PORT", "8412"))
PAY_TO = os.getenv("X402_PAY_TO", "0x7e3a2040F8D13a425E93C797630aB9828B59c806")
PRICE_WEI = int(os.getenv("X402_PRICE_WEI", str(10**16)))  # default 0.01 MON, the full report
# Single-question endpoints are priced well under the bundle: the point of the
# split is that an agent asking one question should not pay for the other four.
MICRO_PRICE_WEI = int(os.getenv("X402_MICRO_PRICE_WEI", str(3 * 10**15)))  # 0.003 MON
FRESH_SECONDS = 900
RPC = os.getenv("X402_RPC", "https://testnet-rpc.monad.xyz")
CHAIN_ID = int(os.getenv("X402_CHAIN_ID", "10143"))
NETWORK = os.getenv("X402_NETWORK", "monad-testnet")
CAIP2 = f"eip155:{CHAIN_ID}"
# Optional second rail: official x402 v2 `exact` scheme (USDC via a facilitator).
# Enabled when X402_USDC_ASSET is set; the facilitator verifies and settles.
USDC_ASSET = os.getenv("X402_USDC_ASSET", "")
USDC_AMOUNT = os.getenv("X402_USDC_AMOUNT", "10000")  # atomic units (6 decimals): 0.01 USDC
MICRO_USDC_AMOUNT = os.getenv("X402_MICRO_USDC_AMOUNT", "3000")  # 0.003 USDC
FACILITATOR = os.getenv("X402_FACILITATOR", "https://x402-facilitator.molandak.org")
DB = os.getenv("X402_DB", "./replay.db")
GEO_DIR = os.getenv("X402_DATA_DIR", "./data")
# x402 v2. Bumped from 1 on 2026-08-20: the CDP bazaar validator rejects v1 outright
# ("upgrade to x402 v2 to be discoverable"), and every discovery index speaks v2.
X402_VERSION = 2
HOW_TO_PAY = ("Send the exact amount to payTo on {net}, then retry with header "
              "X-PAYMENT: base64({{\"txHash\":\"0x..\"}}). One payment = one delivery.")
FEED_URL = "https://prooflines.org/monad/geo-latency/"

RESOURCE = "network-concentration-report"  # the bundle, and the historical path


def _public_base():
    """Absolute base URL these resources are served under, as an index must
    advertise them. Testnet and mainnet sit behind different nginx prefixes.

    X402_PUBLIC_URL (the full URL of the report) predates the split and is what
    the deployed units set, so it is still honoured: its parent is the base."""
    base = os.getenv("X402_PUBLIC_BASE", "")
    if base:
        return base.rstrip("/")
    url = os.getenv("X402_PUBLIC_URL", "")
    if url:
        return url.rstrip("/").rsplit("/", 1)[0]
    return "https://prooflines.org/monad/x402"


PUBLIC_BASE = _public_base()
PUBLIC_PATH = "/" + PUBLIC_BASE.split("://", 1)[-1].split("/", 1)[-1] if "://" in PUBLIC_BASE else PUBLIC_BASE


def rpc(method, params):
    req = urllib.request.Request(RPC, json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "prooflines-x402"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r).get("result")


def facilitator(path, payment_payload, requirements):
    body = json.dumps({"x402Version": 2, "paymentPayload": payment_payload,
                       "paymentRequirements": requirements}).encode()
    req = urllib.request.Request(FACILITATOR + path, body,
        headers={"Content-Type": "application/json", "User-Agent": "prooflines-x402"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


# ── the data layer ────────────────────────────────────────────────────────────
# One loader, five answers. Every builder returns {"networks": {...}} so the
# handler has one availability check ("did any network resolve?") for all of them.

NETS = [("mainnet", "geo-latency-data-mainnet.json", "geo-latency-history-mainnet.json"),
        ("testnet", "geo-latency-data.json", "geo-latency-history.json")]


def _feed(fname):
    try:
        return json.load(open(os.path.join(GEO_DIR, fname)))
    except Exception:
        return None


def _shares(rows):
    return [float(r.get("stake_share_pct") or r.get("share_pct") or r.get("stake_pct") or 0)
            for r in (rows or [])]


def hhi(shares):
    return round(sum(s * s for s in shares), 2)  # shares in percent -> HHI 0..10000


def _per_network(fn):
    """Run a per-network builder over every feed that is present.

    A missing file is skipped rather than faked: an answer that quietly covers
    one network while claiming both is worse than a short one."""
    out = {"generated_at_utc": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
           "source": "prooflines geo-latency pipeline",
           "raw_measurements": FEED_URL,
           "networks": {}}
    for net, cur_f, hist_f in NETS:
        d = _feed(cur_f)
        if d is None:
            continue
        out["networks"][net] = fn(d, hist_f)
    return out


def _provider_shares(d):
    return sorted(_shares(d.get("providers")), reverse=True)


def build_report():
    """The bundle: every axis, full tables, history tail. 0.01 MON."""
    def one(d, hist_f):
        provs = d.get("providers") or []
        conts = d.get("continents") or []
        ctrys = d.get("countries") or []
        p_sh = sorted(_shares(provs), reverse=True)
        top = lambda n: round(sum(p_sh[:n]), 2)
        net_out = {
            "epoch": d.get("epoch"), "as_of": d.get("generated_at_utc"),
            "hhi": {"provider_asn": hhi(p_sh), "country": hhi(_shares(ctrys)),
                    "continent": hhi(_shares(conts))},
            "top_provider_share_pct": {"top1": top(1), "top2": top(2), "top4": top(4)},
            "bft_thresholds": {
                "liveness_33_distance_pct": round(33.33 - top(1), 2),
                "min_providers_to_33_pct": next((i + 1 for i in range(len(p_sh)) if sum(p_sh[:i + 1]) >= 33.33), None),
                "min_providers_to_50_pct": next((i + 1 for i in range(len(p_sh)) if sum(p_sh[:i + 1]) >= 50.0), None),
            },
            "providers_full": provs, "countries_full": ctrys, "continents_full": conts,
        }
        hist = _feed(hist_f)
        if isinstance(hist, list) and len(hist) >= 2:
            net_out["history_points"] = len(hist)
            net_out["history_tail"] = hist[-8:]
        return net_out
    return _per_network(one)


def build_hhi():
    """Concentration indices only, all three axes, both networks."""
    def one(d, _hist):
        p_sh = _provider_shares(d)
        top = lambda n: round(sum(p_sh[:n]), 2)
        return {
            "epoch": d.get("epoch"), "as_of": d.get("generated_at_utc"),
            "hhi": {"provider_asn": hhi(p_sh),
                    "country": hhi(_shares(d.get("countries"))),
                    "continent": hhi(_shares(d.get("continents")))},
            "hhi_scale": "0..10000; >2500 is 'highly concentrated' under the US "
                         "horizontal-merger guidelines, the closest standard scale that exists",
            "top_provider_share_pct": {"top1": top(1), "top2": top(2), "top4": top(4)},
            "providers_counted": len(p_sh),
        }
    return _per_network(one)


def build_bft_thresholds():
    """How far the hosting distribution is from the consensus thresholds."""
    def one(d, _hist):
        p_sh = _provider_shares(d)
        top = lambda n: round(sum(p_sh[:n]), 2)
        reach = lambda pct: next((i + 1 for i in range(len(p_sh)) if sum(p_sh[:i + 1]) >= pct), None)
        return {
            "epoch": d.get("epoch"), "as_of": d.get("generated_at_utc"),
            "top_provider_share_pct": {"top1": top(1), "top2": top(2), "top4": top(4)},
            "liveness_33_distance_pct": round(33.33 - top(1), 2),
            "min_providers_to_33_pct": reach(33.33),
            "min_providers_to_50_pct": reach(50.0),
            "hosting_nakamoto_coefficient": reach(33.33),
            "reading": "min_providers_to_33_pct is the hosting Nakamoto coefficient: how many "
                       "providers would have to fail together to put a third of stake offline. "
                       "It is a correlated-failure measure, not a claim that any threshold has "
                       "been crossed.",
        }
    return _per_network(one)


def build_latency_bands():
    """Stake by round-trip time from the vantage, with the silent share named."""
    def one(d, _hist):
        cov = d.get("coverage") or {}
        return {
            "epoch": d.get("epoch"), "as_of": d.get("generated_at_utc"),
            "vantage": d.get("vantage"), "vantage_point": d.get("vantage_point"),
            "rtt_method": d.get("rtt_method"),
            "latency_bands": d.get("latency_bands"),
            "measured_stake_pct": cov.get("rtt_measured_stake_pct"),
            "unmeasured_stake_pct": cov.get("rtt_unmeasured_stake_pct"),
            "coverage": cov,
        }
    return _per_network(one)


def build_top_providers():
    """The provider ranking with cumulative share and measured RTT."""
    top_n = int(os.getenv("X402_TOP_N", "20"))
    def one(d, _hist):
        provs = d.get("providers") or []
        rows, cum = [], 0.0
        for i, p in enumerate(provs[:top_n], 1):
            share = float(p.get("stake_share_pct") or p.get("share_pct") or p.get("stake_pct") or 0)
            cum += share
            rows.append({"rank": i, "name": p.get("name") or p.get("provider"),
                         "asn": p.get("asn"), "stake_pct": round(share, 2),
                         "cum_pct": round(cum, 2), "validators": p.get("count"),
                         "rtt_ms": p.get("rtt_ms")})
        return {"epoch": d.get("epoch"), "as_of": d.get("generated_at_utc"),
                "providers_total": len(provs), "providers_returned": len(rows),
                "providers": rows}
    return _per_network(one)


# ── the resource catalogue ────────────────────────────────────────────────────

def _r(name, price_wei, usdc_amount, build, description, example, out_schema):
    return {"name": name, "price_wei": price_wei, "usdc_amount": usdc_amount,
            "build": build, "description": description, "example": example,
            "output_schema": out_schema, "url": f"{PUBLIC_BASE}/{name}"}


_EX_NET = {"epoch": 812, "as_of": "2026-08-26"}

RESOURCES = {r["name"]: r for r in [
    _r(RESOURCE, PRICE_WEI, USDC_AMOUNT, build_report,
       "Monad network concentration report, mainnet and testnet in one call: stake HHI by "
       "provider ASN, country and continent; top-1/2/4 provider shares; distance to the 33% "
       "BFT liveness threshold and the minimum number of providers needed to reach 33% and "
       "50%; full per-provider stake with measured RTT; epoch-over-epoch history tail. The "
       "bundle of every single-question endpoint on this gate. Computed from ProofLines' own "
       "off-node measurements, published at " + FEED_URL + ", not resold from an explorer.",
       {**_EX_NET, "hhi": {"provider_asn": 812.4, "country": 1740.9, "continent": 4133.1},
        "top_provider_share_pct": {"top1": 19.89, "top2": 34.65, "top4": 52.07},
        "bft_thresholds": {"liveness_33_distance_pct": 1.65, "min_providers_to_33_pct": 2,
                           "min_providers_to_50_pct": 4},
        "providers_full": [{"name": "Hetzner", "asn": "AS24940", "stake_pct": 19.89,
                            "cum_pct": 19.89, "count": 41, "rtt_ms": 27}]},
       {"required": ["epoch", "hhi", "top_provider_share_pct", "bft_thresholds", "providers_full"],
        "properties": {"epoch": {"type": "integer"}, "hhi": {"type": "object"},
                       "top_provider_share_pct": {"type": "object"},
                       "bft_thresholds": {"type": "object"},
                       "providers_full": {"type": "array"},
                       "history_tail": {"type": "array"}}}),

    _r("concentration-hhi", MICRO_PRICE_WEI, MICRO_USDC_AMOUNT, build_hhi,
       "One question: how concentrated is Monad stake? Herfindahl-Hirschman index by hosting "
       "provider ASN, by country and by continent, plus top-1/2/4 provider shares, for mainnet "
       "and testnet. Computed from ProofLines' own off-node measurements (raw feed public at "
       + FEED_URL + "); you are paying for the computed index, not for access to the data.",
       {**_EX_NET, "hhi": {"provider_asn": 812.4, "country": 1740.9, "continent": 4133.1},
        "top_provider_share_pct": {"top1": 19.89, "top2": 34.65, "top4": 52.07},
        "providers_counted": 37},
       {"required": ["epoch", "hhi", "top_provider_share_pct"],
        "properties": {"epoch": {"type": "integer"},
                       "hhi": {"type": "object",
                               "description": "0..10000 by dimension: provider_asn, country, continent."},
                       "top_provider_share_pct": {"type": "object"},
                       "providers_counted": {"type": "integer"}}}),

    _r("bft-thresholds", MICRO_PRICE_WEI, MICRO_USDC_AMOUNT, build_bft_thresholds,
       "One question: how far is Monad's hosting distribution from the consensus thresholds? "
       "Distance from the largest provider to 33% of stake, and the minimum number of providers "
       "that together reach 33% and 50% (the hosting Nakamoto coefficient) for mainnet and "
       "testnet. A correlated-failure measure, not a claim that a threshold has been crossed. "
       "Computed from ProofLines' own measurements, raw feed public at " + FEED_URL + ".",
       {**_EX_NET, "top_provider_share_pct": {"top1": 19.89, "top2": 34.65, "top4": 52.07},
        "liveness_33_distance_pct": 13.44, "min_providers_to_33_pct": 2,
        "min_providers_to_50_pct": 4, "hosting_nakamoto_coefficient": 2},
       {"required": ["epoch", "liveness_33_distance_pct", "min_providers_to_33_pct"],
        "properties": {"epoch": {"type": "integer"},
                       "liveness_33_distance_pct": {"type": "number"},
                       "min_providers_to_33_pct": {"type": "integer"},
                       "min_providers_to_50_pct": {"type": "integer"},
                       "hosting_nakamoto_coefficient": {"type": "integer"},
                       "top_provider_share_pct": {"type": "object"}}}),

    _r("latency-bands", MICRO_PRICE_WEI, MICRO_USDC_AMOUNT, build_latency_bands,
       "One question: where does Monad stake sit in round-trip time? Share of stake per RTT band "
       "measured from a fixed European vantage, with the ICMP-silent share reported separately "
       "instead of folded into a band, plus the measurement coverage behind the number. Mainnet "
       "and testnet. Computed from ProofLines' own probes, raw feed public at " + FEED_URL + ".",
       {**_EX_NET, "vantage": "Falkenstein, DE",
        "latency_bands": {"bands": [{"range_ms": [0, 25], "stake_pct": 36.67},
                                    {"range_ms": [25, 50], "stake_pct": 17.61}],
                          "unmeasured_stake_pct": 9.95},
        "measured_stake_pct": 90.05, "unmeasured_stake_pct": 9.95},
       {"required": ["epoch", "latency_bands"],
        "properties": {"epoch": {"type": "integer"}, "vantage": {"type": "string"},
                       "latency_bands": {"type": "object"},
                       "measured_stake_pct": {"type": "number"},
                       "unmeasured_stake_pct": {"type": "number"},
                       "coverage": {"type": "object"}}}),

    _r("top-providers", MICRO_PRICE_WEI, MICRO_USDC_AMOUNT, build_top_providers,
       "One question: who hosts Monad stake? The provider ranking with each one's stake share, "
       "running cumulative share, validator count and measured RTT, mainnet and testnet. The "
       "cumulative column is what answers 'how few providers hold a third of the network'. "
       "Computed from ProofLines' own off-node measurements, raw feed public at " + FEED_URL + ".",
       {**_EX_NET, "providers_total": 37, "providers_returned": 20,
        "providers": [{"rank": 1, "name": "Hetzner", "asn": "AS24940", "stake_pct": 19.89,
                       "cum_pct": 19.89, "validators": 41, "rtt_ms": 27}]},
       {"required": ["epoch", "providers"],
        "properties": {"epoch": {"type": "integer"},
                       "providers_total": {"type": "integer"},
                       "providers_returned": {"type": "integer"},
                       "providers": {"type": "array", "items": {"type": "object"}}}}),
]}


def bazaar_extension(res):
    """The `extensions.bazaar` object a discovery index needs to catalogue an endpoint.

    Required by the CDP validator: info.input.type, info.input.method and a JSON Schema.
    info.output.example is advisory but it is what lets an agent decide whether the
    response is worth paying for before it spends anything, so it is filled in properly.
    """
    return {"bazaar": {
        "info": {
            "input": {"type": "http", "method": "GET"},
            "output": {"type": "json", "example": {
                "generated_at_utc": "2026-08-26 10:00 UTC",
                "source": "prooflines geo-latency pipeline",
                "raw_measurements": FEED_URL,
                "networks": {"mainnet": res["example"]},
            }},
        },
        # `schema` is validated against the `info` object above, not against the request
        # and response payloads: the CDP validator rejected a payload-shaped schema with
        # "(root).input: Additional property type is not allowed" and "(root).output:
        # generated_at_utc is required", i.e. it fed info.input/info.output through it.
        # So this describes info; the data shape is carried by info.output.example.
        "schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {
                "input": {
                    "type": "object",
                    "description": "No parameters. A GET returns both networks in one response.",
                    "required": ["type", "method"],
                    "properties": {"type": {"const": "http"}, "method": {"const": "GET"}},
                },
                "output": {
                    "type": "object",
                    "required": ["type", "example"],
                    "properties": {"type": {"const": "json"}, "example": {"type": "object"}},
                },
            },
        },
        "_payloadSchema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "description": "Shape of the paid response body itself, kept beside the "
                           "index-facing schema so the detail is not lost.",
            "properties": {
                "output": {
                    "type": "object",
                    "required": ["generated_at_utc", "networks"],
                    "properties": {
                        "generated_at_utc": {"type": "string",
                                             "description": "Generation time, 'YYYY-MM-DD HH:MM UTC'."},
                        "source": {"type": "string"},
                        "raw_measurements": {"type": "string",
                                             "description": "Where the free underlying feed lives."},
                        "networks": {
                            "type": "object",
                            "description": "Keyed by network name: 'mainnet' and 'testnet'.",
                            "additionalProperties": {"type": "object", **res["output_schema"]},
                        },
                    },
                },
            },
        },
    }}


def payment_requirements(res):
    price = str(res["price_wei"])
    return {
        "x402Version": X402_VERSION,
        "error": "payment required",
        "resource": {"url": res["url"], "description": res["description"],
                     "mimeType": "application/json"},
        "extensions": bazaar_extension(res),
        "accepts": [{
            "scheme": "monad-native-exact",
            "network": NETWORK,
            "chainId": CHAIN_ID,
            "payTo": PAY_TO,
            # v2 renamed this field to `amount`; `maxAmountRequired` stays for the
            # published demo client and anyone who integrated against v1.
            "amount": price,
            "maxAmountRequired": price,
            "maxTimeoutSeconds": FRESH_SECONDS,
            "asset": "native",
            "resource": f"{PUBLIC_PATH}/{res['name']}",
            "description": f"{res['description'][:180]} "
                           f"Pay the exact amount in native MON on {NETWORK}, then retry with "
                           "header X-PAYMENT: base64({\"txHash\": \"0x..\"}).",
            "validityWindowSeconds": FRESH_SECONDS,
            # Non-standard instructions belong in `extra`; /.well-known/x402 nests the same
            # text there, so the live 402 and the discovery file now agree field for field.
            "extra": {"howToPay": HOW_TO_PAY.format(net=NETWORK)},
        }] + ([{
            "scheme": "exact",
            "network": CAIP2,
            "amount": res["usdc_amount"],
            "asset": USDC_ASSET,
            "payTo": PAY_TO,
            "maxTimeoutSeconds": 300,
            "extra": {"name": "USDC", "version": "2"},
        }] if USDC_ASSET else []),
    }


def catalogue():
    """Free: what this gate sells, at what price. An agent should be able to pick
    the one endpoint that answers its question without paying to find out."""
    return {
        "service": "ProofLines Monad network intelligence",
        "x402Version": X402_VERSION,
        "network": NETWORK,
        "payTo": PAY_TO,
        "raw_measurements": FEED_URL,
        "note": "Each resource answers one question and is priced on its own. The "
                "bundle resource returns all of them in a single call.",
        "resources": [{
            "name": r["name"], "url": r["url"],
            "price": {"native_mon_wei": str(r["price_wei"]),
                      **({"usdc_atomic": r["usdc_amount"]} if USDC_ASSET else {})},
            "description": r["description"],
        } for r in RESOURCES.values()],
    }


def catalogue_html():
    """The catalogue for a person. A browser that lands on the gate gets a page,
    an agent gets the JSON: same URL, decided by Accept. Machine endpoints that
    answer humans with a raw error read as broken."""
    mon = lambda wei: f"{int(wei) / 10**18:g}"
    rows = "".join(
        f"<tr><td><a href='{r['url']}'>{r['name']}</a></td>"
        f"<td class='p'>{mon(r['price_wei'])} MON</td>"
        f"<td>{r['description']}</td></tr>" for r in RESOURCES.values())
    first = next(iter(RESOURCES.values()))
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ProofLines Monad data · x402 gate ({NETWORK})</title>
<style>body{{font:15px/1.55 system-ui,sans-serif;max-width:920px;margin:3rem auto;
padding:0 1rem;color:#1b1f24}}code,pre{{background:#f2f2f4;border-radius:4px;padding:1px 5px}}
pre{{padding:10px;overflow-x:auto}}td{{padding:7px 10px;border-top:1px solid #e3e3e8;
vertical-align:top;font-size:14px}}td.p{{white-space:nowrap;font-variant-numeric:tabular-nums}}
a{{color:#6a4fd8}}</style></head><body>
<h1>Monad network intelligence, pay per call</h1>
<p>An <a href="https://x402.org">x402</a> gate on <b>{NETWORK}</b>. Each resource answers one
question and is priced on its own; the report is the bundle of all of them. Payment is native MON
to <code>{PAY_TO}</code>, verified by this server against the chain, no facilitator.</p>
<table>{rows}</table>
<p>Unpaid, any resource returns <code>402</code> with the offer in the body and in the
<code>PAYMENT-REQUIRED</code> header. Pay, then retry with the transaction hash:</p>
<pre>curl -s {first['url']}
curl -s {first['url']} -H "X-PAYMENT: $(echo -n '{{"txHash":"0x..."}}' | base64 -w0)"</pre>
<p>This same list is JSON at this URL for anything that is not a browser. The measurements behind
the answers are published free at <a href="{FEED_URL}">{FEED_URL}</a>: what is paid for is the
computed answer, not access to the data. Also served over
<a href="https://prooflines.org/mcp">MCP</a> and <a href="https://prooflines.org/a2a">A2A</a>.
Source: <a href="https://github.com/ColinkaMir/monad-x402-gate">monad-x402-gate</a>.</p>
</body></html>"""


def db():
    d = os.path.dirname(DB)
    if d:
        os.makedirs(d, exist_ok=True)
    c = sqlite3.connect(DB)
    c.execute("CREATE TABLE IF NOT EXISTS used (txhash TEXT PRIMARY KEY, ts INTEGER, resource TEXT)")
    return c


def check_payment(tx_hash, price_wei):
    """Validate a payment WITHOUT consuming it. Returns (ok, error, block).

    Consuming is a separate step (`burn_payment`) so that a data outage cannot
    swallow a customer's money: nothing is burned until the answer is in hand."""
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
    if int(tx.get("value", "0x0"), 16) < price_wei:
        return False, "insufficient amount", None
    rcpt = rpc("eth_getTransactionReceipt", [tx_hash])
    if not rcpt or rcpt.get("status") != "0x1":
        return False, "transaction not confirmed successful", None
    blk = rpc("eth_getBlockByNumber", [rcpt["blockNumber"], False])
    if not blk or time.time() - int(blk["timestamp"], 16) > FRESH_SECONDS:
        return False, "payment too old (validity window expired)", None
    return True, None, int(rcpt["blockNumber"], 16)


def burn_payment(tx_hash, resource):
    """Consume the payment. False means it was already spent — the INSERT itself
    decides that, so two concurrent requests carrying one tx cannot both win."""
    c = db()
    cur = c.execute("INSERT OR IGNORE INTO used VALUES (?,?,?)",
                    (tx_hash.lower(), int(time.time()), resource))
    c.commit()
    return cur.rowcount == 1


def has_answer(payload):
    return bool(payload) and bool(payload.get("networks"))


class Handler(BaseHTTPRequestHandler):
    server_version = "prooflines-x402/2.0"
    head_only = False

    def _json(self, code, obj, extra=None):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if not self.head_only:
            self.wfile.write(body)

    def do_HEAD(self):
        # RFC 9110 6.4.2: HEAD must answer with the status and headers GET would
        # send, minus the body. Without this, BaseHTTPRequestHandler answers 501,
        # so a link checker probing with HEAD sees a broken endpoint instead of
        # the 402 payment offer, and the PAYMENT-REQUIRED header never reaches
        # an agent that only wants to learn the price before committing to a GET.
        self.head_only = True
        try:
            self.do_GET()
        finally:
            self.head_only = False

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")
        name = path.rsplit("/", 1)[-1] if path else ""
        res = RESOURCES.get(name)
        if res is None:
            # Landing on the gate prefix itself returns the catalogue: an agent
            # should be able to see what is for sale, and at what price, without
            # paying to find out. Anything else is a 404 that names what exists.
            if not name or path == PUBLIC_PATH:
                if "text/html" in (self.headers.get("Accept") or ""):
                    body = catalogue_html().encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    if not self.head_only:
                        self.wfile.write(body)
                    return
                self._json(200, catalogue())
            else:
                self._json(404, {"error": "unknown resource",
                                 "available": [r["url"] for r in RESOURCES.values()],
                                 "catalogue": PUBLIC_BASE})
            return

        sig_hdr = self.headers.get("PAYMENT-SIGNATURE")
        if sig_hdr and USDC_ASSET:
            try:
                envelope = json.loads(base64.b64decode(sig_hdr))
                accepted = envelope.get("accepted") or {}
                if accepted.get("scheme") != "exact":
                    raise ValueError("only the exact scheme is facilitated here")
            except Exception as e:
                self._json(400, {"error": f"malformed PAYMENT-SIGNATURE: {e}"})
                return
            # Select the facilitated rail by scheme, never by position: the order of
            # `accepts` is presentation, and an index or a future rail may reorder it.
            reqs_entry = next(a for a in payment_requirements(res)["accepts"]
                              if a.get("scheme") == "exact")
            try:
                v = facilitator("/verify", envelope, reqs_entry)
            except Exception as e:
                self._json(502, {"error": f"facilitator verify error: {e}"})
                return
            if not v.get("isValid"):
                out = payment_requirements(res)
                out["error"] = f"payment rejected: {v.get('invalidReason')}"
                self._json(402, out)
                return
            # Build BEFORE settling: an outage must not cost the client money.
            payload = res["build"]()
            if not has_answer(payload):
                self._json(503, {"error": "data temporarily unavailable; nothing was settled",
                                 "resource": res["url"]})
                return
            try:
                st = facilitator("/settle", envelope, reqs_entry)
            except Exception as e:
                self._json(502, {"error": f"facilitator settle error: {e}"})
                return
            if not st.get("success"):
                out = payment_requirements(res)
                out["error"] = f"settlement failed: {st.get('errorReason')}"
                self._json(402, out)
                return
            burn_payment(st.get("transaction") or "0x", res["name"])
            self._json(200, payload, {"PAYMENT-RESPONSE":
                       base64.b64encode(json.dumps(st).encode()).decode()})
            return

        hdr = self.headers.get("X-PAYMENT")
        if not hdr:
            reqs = payment_requirements(res)
            # x402 v2 requires the FULL PaymentRequired object, `extensions.bazaar`
            # included, in the `PAYMENT-REQUIRED` header: the CDP validator says
            # outright that "the indexer reads only the header for v2". That payload
            # runs past nginx's default 4k header buffer, so the proxy needs
            # proxy_buffer_size raised (done 2026-08-20) or it answers 502 while the
            # gate itself is serving a perfectly valid 402.
            # `X-PAYMENT-REQUIRED` stays as the v1 name our published demo client reads.
            encoded = base64.b64encode(json.dumps(reqs).encode()).decode()
            # NOTE: an earlier version rebuilt a reduced v2 object here whenever a
            # facilitated rail was configured, and overwrote the header with it. That
            # dropped `extensions` on exactly the instance that has real money on it:
            # the mainnet endpoint validated as "no bazaar discovery extension found"
            # while testnet passed, because testnet has no USDC rail to trigger it.
            # The full object now goes out on both, unconditionally.
            self._json(402, reqs, {"PAYMENT-REQUIRED": encoded,
                                   "X-PAYMENT-REQUIRED": encoded})
            return
        try:
            payload_hdr = json.loads(base64.b64decode(hdr))
            tx_hash = payload_hdr.get("txHash", "")
        except Exception:
            self._json(400, {"error": "X-PAYMENT must be base64 of {\"txHash\": \"0x..\"}"})
            return
        try:
            ok, err, blk = check_payment(tx_hash, res["price_wei"])
        except Exception as e:
            self._json(502, {"error": f"verification backend error: {e}"})
            return
        if not ok:
            reqs = payment_requirements(res)
            reqs["error"] = f"payment rejected: {err}"
            self._json(402, reqs)
            return
        answer = res["build"]()
        if not has_answer(answer):
            self._json(503, {"error": "data temporarily unavailable; your payment was not "
                                      "consumed and stays valid inside its window",
                             "resource": res["url"]})
            return
        if not burn_payment(tx_hash, res["name"]):
            reqs = payment_requirements(res)
            reqs["error"] = "payment rejected: payment already used"
            self._json(402, reqs)
            return
        resp = {"ok": True, "txHash": tx_hash.lower(), "block": blk,
                "scheme": "monad-native-exact", "resource": res["name"]}
        self._json(200, answer, {"X-PAYMENT-RESPONSE":
                   base64.b64encode(json.dumps(resp).encode()).decode()})

    def log_message(self, fmt, *args):
        print("%s %s" % (self.address_string(), fmt % args))


if __name__ == "__main__":
    db()
    print(f"x402 gate on 127.0.0.1:{PORT}, payTo {PAY_TO}, "
          f"{len(RESOURCES)} resources, bundle {PRICE_WEI} wei / single {MICRO_PRICE_WEI} wei")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()

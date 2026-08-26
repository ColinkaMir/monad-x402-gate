# monad-x402-gate

A minimal x402-style pay-per-call gate for Monad: stdlib Python, no facilitator, no contracts,
no SDK. The client pays **native MON**, the server verifies the payment itself over plain RPC and
serves the resource. Running in production at
[prooflines.org/monad/x402/](https://prooflines.org/monad/x402/) (testnet) and
[/monad/x402-mainnet/](https://prooflines.org/monad/x402-mainnet/) (real MON, plus a USDC rail).

## One resource per question

The gate serves several resources, each answering one question at its own price, plus a bundle
that returns all of them. That shape is not a preference: crawling the whole CDP Bazaar (15,145
paid resources) showed that the operators with repeat customers run many small well-described
endpoints, while one large report is hard to discover and hard to justify buying.

```
GET /monad/x402/                            → free catalogue: every resource and its price
GET /monad/x402/concentration-hhi           → 0.003 MON  HHI by ASN / country / continent
GET /monad/x402/bft-thresholds              → 0.003 MON  distance to 33%, hosting Nakamoto coefficient
GET /monad/x402/latency-bands               → 0.003 MON  stake per RTT band, silent share stated
GET /monad/x402/top-providers               → 0.003 MON  provider ranking with cumulative share
GET /monad/x402/network-concentration-report→ 0.01  MON  all of the above, full tables + history
```

An agent should be able to see what is for sale before paying, so the catalogue at the gate's own
prefix is free.

## Data

The resources are built from files in `X402_DATA_DIR` (default `./data`, trimmed real samples
included, so a fresh clone serves working answers). Point it at your own documents and rewrite the
builders in `RESOURCES` to sell your own data.

What ProofLines charges for is the **computed answer**, not access: the underlying measurements are
published free at [prooflines.org/monad/geo-latency/](https://prooflines.org/monad/geo-latency/),
and every resource description says so.

## Why this exists

The official Monad x402 stack settles USDC through a facilitator
([guide](https://docs.monad.xyz/guides/x402)); that is the right default for gasless UX. This gate
is the sovereign variant for cases where you want zero third-party dependencies: your server, your
RPC view, the chain's own coin. Trade-off: the client pays gas and makes a real transaction per
purchase; there is no facilitator to batch, refund, or sponsor.

The x402 spec explicitly allows local verification ("the server verifies locally or via
facilitator"), so this stays inside the standard's envelope with a custom scheme name.

## Scheme: `monad-native-exact`

1. `GET /monad/x402/<resource>` with no payment → `402` with a `PaymentRequired` JSON body
   (also base64 in the `X-PAYMENT-REQUIRED` header): scheme, chainId, `payTo`, price in wei,
   validity window.
2. Client sends the exact amount of native MON to `payTo` and retries with
   `X-PAYMENT: base64({"txHash": "0x…"})`.
3. Server verifies via RPC: recipient matches, value covers **that resource's** price, receipt
   status is success, block timestamp inside the validity window, and the hash is unused (sqlite
   replay store).
4. `200` + resource + `X-PAYMENT-RESPONSE: base64({ok, txHash, block, scheme, resource})`.

Verification is by txHash, not by unique-amount matching, so concurrent payers never collide.
One payment = one delivery.

**The payment is burned after the answer is built, not before.** If the underlying data is
unavailable the gate answers `503` and leaves the payment unspent, so the same transaction still
works once the feed is back. Taking money and then failing to deliver is the one failure mode a
paywall must not have. The burn itself is the `INSERT`, so two concurrent requests carrying one
transaction cannot both win.

## Run it

```bash
X402_PAY_TO=0xYourAddress \
X402_PRICE_WEI=10000000000000000 \
X402_RPC=https://testnet-rpc.monad.xyz \
X402_DB=./replay.db \
python3 gate.py
```

Put nginx (or any proxy) in front; the gate binds to loopback. `X402_MICRO_PRICE_WEI` prices the
single-question resources, `X402_PRICE_WEI` the bundle. The bundled renderers serve concentration
answers from the ProofLines geo-latency pipeline; replace the builders in `RESOURCES` with whatever
you sell.

Note for anyone putting this behind nginx: an x402 v2 offer carries the whole `PaymentRequired`
object, bazaar extension included, in the `PAYMENT-REQUIRED` **header**, which runs past nginx's
default 4k header buffer. Without `proxy_buffer_size 32k` the proxy answers 502 while the gate is
serving a perfectly valid 402.

## Demo client

```bash
X402_KEY=0x<funded testnet key> node demo-client.mjs   # needs ethers in node_modules
```

Output of the real first run against the production gate (2026-08-06):

```
step 1: GET -> 402
step 2: paying from 0x9E66867adfDC613891A96d82a53988829cD39004
  tx sent: 0x372385ee8c03c6b93744cfa9a458dec45880ae653e0f60fb447f9bd65962ad3a
  confirmed
step 3: retry with X-PAYMENT -> 200
  settlement: {"ok": true, "block": 51476148, "scheme": "monad-native-exact"}
```

Replay protection is live: retrying the same txHash returns `402 payment rejected: payment
already used`.

## Security notes

- Watch-only by design: no private keys on the gate host, ever. You only need the receiving
  address.
- The replay store is the single stateful piece; back it up if double-delivery matters to you.
- Rate-limit the endpoint at the proxy (each bogus retry costs you an RPC call).
- Payments are testnet MON here; the mechanics are identical on mainnet, the threat model is not.
  Re-read everything before pointing this at real money.

## Related work on Monad

Official USDC facilitator flow ([docs](https://docs.monad.xyz/guides/x402)), the
[molandak facilitator](https://x402-facilitator.molandak.org), MetaMask's Monad facilitator,
[PayGate](https://github.com/YOUZYX/PayGate) (escrow-based native-MON paywalls with per-byte
metering), AxilProtocol (contract-based 402 splits), and state-channel work over x402. This gate
takes the opposite corner: the least machinery that still pays.

## License

MIT

## On-chain feedback (ERC-8004)

This gate's operator runs registered agent **#10231** in the [ERC-8004 Identity
Registry](https://prooflines.org/monad/erc8004/) on Monad. If you paid for the
report and want to say so on-chain, `rate-agent.mjs` submits feedback to the
reputation registry from the wallet that paid:

```bash
RATE_KEY=<your-keyfile> node rate-agent.mjs 10231 95 x402 report-quality
```

Rate agents you actually used, from the wallet that used them. The spec forbids
self-feedback from the agent owner, and wash ratings are exactly the noise that
makes the registry's raw totals meaningless.

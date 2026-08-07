# monad-x402-gate

A minimal x402-style pay-per-call gate for Monad: ~200 lines of stdlib Python, no facilitator,
no contracts, no SDK. The client pays **native MON**, the server verifies the payment itself over
plain RPC and serves the resource. Running in production at
[prooflines.org/monad/x402/network-concentration-report](https://prooflines.org/monad/x402/network-concentration-report).

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
3. Server verifies via RPC: recipient matches, value covers the price, receipt status is success,
   block timestamp inside the validity window, and the hash is unused (sqlite replay store,
   hash burned on delivery).
4. `200` + resource + `X-PAYMENT-RESPONSE: base64({ok, txHash, block, scheme})`.

Verification is by txHash, not by unique-amount matching, so concurrent payers never collide.
One payment = one delivery.

## Run it

```bash
X402_PAY_TO=0xYourAddress \
X402_PRICE_WEI=10000000000000000 \
X402_RPC=https://testnet-rpc.monad.xyz \
X402_DB=./replay.db \
python3 gate.py
```

Put nginx (or any proxy) in front; the gate binds to loopback. The bundled resource renderer
serves a network-concentration report from the ProofLines geo-latency pipeline; replace
`build_report()` with whatever you sell.

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

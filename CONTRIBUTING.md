# Contributing to monad-x402-gate

## Running it

```bash
X402_DATA_DIR=./data python3 gate.py     # serves the sample report on :8412
python3 test_gate.py                     # offline smoke test, no network
```

Stdlib only. `data/` holds trimmed real documents from the ProofLines geo
pipeline so the paid resource has something to serve on any machine; point
`X402_DATA_DIR` at your own files to sell your own data.

## Testing a payment for real

The paid path verifies transactions on-chain, so its end-to-end test runs
against Monad testnet and costs faucet money only:

```bash
X402_DATA_DIR=./data X402_PAY_TO=<your address> python3 gate.py
node demo-client.mjs        # pays 0.01 tMON and prints the report
```

Get tMON from the faucet, point `demo-client.mjs` at your key, and the full
402 -> pay -> verify -> 200 loop runs with a real transaction.

## What a change needs

- `python3 test_gate.py` must print `SMOKE_OK` before you open a PR.
- Keep the gate stdlib-only; if a change seems to need a dependency, open an
  issue first and say why.
- The offer must stay valid x402 v2: the full PaymentRequired object goes in
  the `PAYMENT-REQUIRED` header (indexers read only the header), and the
  `extensions.bazaar` block must keep validating against the CDP checker
  (`POST https://api.cdp.coinbase.com/platform/v2/x402/validate`).
- Behind a proxy, mind the header size: the encoded offer is ~4 KB, above
  nginx's default `proxy_buffer_size`. See the README note.
- One change per PR.

## Security posture

The gate is watch-only by design: no private keys on the host, payments are
verified against public RPC and burned in a replay store. Keep it that way —
a change that wants a key on the server is the wrong shape for this repo.

// x402 demo client: fetch -> 402 -> pay native tMON -> retry with X-PAYMENT -> data.
// Usage: X402_KEY=0x<funded testnet private key> node x402-demo-client.mjs
// (run from a dir with ethers available, e.g. /home/solana/most-work/nad-agent)
import { JsonRpcProvider, Wallet, getAddress } from "ethers";

const URL = "https://prooflines.org/monad/x402/network-concentration-report";

const first = await fetch(URL);
console.log("step 1: GET ->", first.status);
if (first.status !== 402) { console.log(await first.text()); process.exit(1); }
const reqs = (await first.json()).accepts[0];
console.log("  scheme:", reqs.scheme, "| price:", reqs.maxAmountRequired, "wei | payTo:", reqs.payTo);

const key = process.env.X402_KEY;
if (!key) { console.error("set X402_KEY to a funded Monad-testnet private key"); process.exit(1); }
const provider = new JsonRpcProvider("https://testnet-rpc.monad.xyz", reqs.chainId);
const wallet = new Wallet(key, provider);
console.log("step 2: paying from", wallet.address);
const tx = await wallet.sendTransaction({ to: getAddress(reqs.payTo), value: BigInt(reqs.maxAmountRequired) });
console.log("  tx sent:", tx.hash);
await tx.wait();
console.log("  confirmed");

const payment = Buffer.from(JSON.stringify({ txHash: tx.hash })).toString("base64");
const paid = await fetch(URL, { headers: { "X-PAYMENT": payment } });
console.log("step 3: retry with X-PAYMENT ->", paid.status);
const settle = paid.headers.get("x-payment-response");
if (settle) console.log("  settlement:", Buffer.from(settle, "base64").toString());
const data = await paid.json();
console.log("  report networks:", Object.keys(data.networks || {}), "| generated:", data.generated_at_utc);

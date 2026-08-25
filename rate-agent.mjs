// Leave on-chain ERC-8004 feedback for an agent you have actually used.
//
// The reputation registry records value + tags against the agent's id; the
// spec forbids the agent's own owner from rating itself, and so do we: rate
// agents you PAID, from the wallet that paid them.
//
//   RATE_KEY=path-to-key node rate-agent.mjs <agentId> <value> [tag1] [tag2]
//   e.g. node rate-agent.mjs 10231 95 x402 report-quality
import { JsonRpcProvider, Wallet, Contract } from "ethers";
import { readFileSync } from "fs";

const REPUTATION = "0x8004BAa17C55a88189AE136b182e5fdA19dE9b63";
const [agentId, value, tag1 = "", tag2 = ""] = process.argv.slice(2);
if (!agentId || value === undefined) {
  console.error("usage: RATE_KEY=<keyfile> node rate-agent.mjs <agentId> <value 0-100> [tag1] [tag2]");
  process.exit(1);
}
const provider = new JsonRpcProvider(process.env.RATE_RPC || "https://rpc.monad.xyz", 143, { staticNetwork: true });
const wallet = new Wallet(readFileSync(process.env.RATE_KEY, "utf8").trim(), provider);
const reg = new Contract(REPUTATION,
  ["function giveFeedback(uint256 agentId, int128 value, uint8 valueDecimals, string tag1, string tag2, string endpoint, string feedbackURI, bytes32 feedbackHash)"],
  wallet);
const tx = await reg.giveFeedback(BigInt(agentId), BigInt(value), 0, tag1, tag2, "", "",
  "0x" + "0".repeat(64));
console.log("tx:", tx.hash);
const rcpt = await tx.wait();
console.log("status:", rcpt.status, "block:", rcpt.blockNumber);

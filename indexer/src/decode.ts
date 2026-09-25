// CoreWriter payload: 1 byte version (=1), 3 bytes big-endian action id, then ABI-encoded fields.
// Spec: hyperliquid-docs/for-developers/hyperevm/interacting-with-hypercore
import { decodeAbiParameters, parseAbiParameters, type Hex } from "viem";

const ACTIONS: Record<number, [string, string]> = {
  1: ["LimitOrder", "uint32 asset, bool isBuy, uint64 limitPx, uint64 sz, bool reduceOnly, uint8 tif, uint128 cloid"],
  2: ["VaultTransfer", "address vault, bool isDeposit, uint64 usd"],
  3: ["TokenDelegate", "address validator, uint64 wei, bool isUndelegate"],
  4: ["StakingDeposit", "uint64 wei"],
  5: ["StakingWithdraw", "uint64 wei"],
  6: ["SpotSend", "address destination, uint64 token, uint64 wei"],
  7: ["UsdClassTransfer", "uint64 ntl, bool toPerp"],
  8: ["FinalizeEvmContract", "uint64 token, uint8 variant, uint64 createNonce"],
  9: ["AddApiWallet", "address wallet, string name"],
  10: ["CancelByOid", "uint32 asset, uint64 oid"],
  11: ["CancelByCloid", "uint32 asset, uint128 cloid"],
  12: ["ApproveBuilderFee", "uint64 maxFeeRate, address builder"],
  13: ["SendAsset", "address destination, address subAccount, uint32 sourceDex, uint32 destinationDex, uint64 token, uint64 wei"],
  15: ["BorrowLend", "uint8 operation, uint64 token, uint64 amount"],
  16: ["SetAbstraction", "address user, uint8 abstraction"],
  17: ["OutcomeOperation", "uint8 operation, uint32 arg1, uint32 arg2, uint64 amount"],
};
const TIF: Record<number, string> = { 1: "Alo", 2: "Gtc", 3: "Ioc" };
const E8 = 1e8; // limitPx and sz are sent as value * 10^8
// uint64 max is how contracts say "no limit": limitPx = any price (a market order), sz = any size.
const U64_MAX = (1n << 64n) - 1n;
const scaled = (v: string) => (BigInt(v) === U64_MAX ? undefined : Number(v) / E8);

export type Decoded = {
  actionId: number;
  action: string;
  decoded: boolean;
  args: Record<string, string | number | boolean>;
  asset?: number;
  order?: { isBuy: boolean; limitPx?: number; sz?: number; market: boolean; reduceOnly: boolean; tif: string; cloid?: string };
};

export function decodeAction(data: Hex): Decoded {
  // Too short or wrong version: HyperCore rejects these, so group them as one bucket (id -1).
  const version = data.length >= 10 ? parseInt(data.slice(2, 4), 16) : -1;
  if (version !== 1) return { actionId: -1, action: "Malformed", decoded: false, args: { raw: data } };
  const actionId = parseInt(data.slice(4, 10), 16);
  const spec = ACTIONS[actionId];
  if (!spec) return { actionId, action: `Unknown${actionId}`, decoded: false, args: { raw: data } };
  const [action, sig] = spec;
  const params = parseAbiParameters(sig);
  let values: readonly unknown[];
  try {
    values = decodeAbiParameters(params, `0x${data.slice(10)}`);
  } catch {
    return { actionId, action, decoded: false, args: { raw: data } };
  }
  const args: Decoded["args"] = {};
  params.forEach((p, i) => {
    const v = values[i];
    args[p.name!] = typeof v === "bigint" ? v.toString() : (v as string | number | boolean);
  });
  const out: Decoded = { actionId, action, decoded: true, args };
  if ("asset" in args) out.asset = Number(args.asset);
  if (actionId === 1) {
    const cloid = BigInt(args.cloid as string);
    out.order = {
      isBuy: args.isBuy as boolean,
      limitPx: scaled(args.limitPx as string),
      sz: scaled(args.sz as string),
      market: BigInt(args.limitPx as string) === U64_MAX,
      reduceOnly: args.reduceOnly as boolean,
      tif: TIF[args.tif as number] ?? String(args.tif),
      cloid: cloid ? "0x" + cloid.toString(16) : undefined,
    };
  }
  return out;
}

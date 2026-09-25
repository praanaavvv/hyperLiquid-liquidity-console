import { describe, it, expect } from "vitest";
import { decodeAction } from "./decode";

// Real RawAction payload, HyperEVM block 46827178: IOC reduce-only sell on HIP-3 asset 110004 (xyz dex).
const LIMIT =
  "0x01000001000000000000000000000000000000000000000000000000000000000001adb4000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000002d0cc894000000000000000000000000000000000000000000000000000000000007ea5e000000000000000000000000000000000000000000000000000000000000000010000000000000000000000000000000000000000000000000000000000000003000000000000000000000000000000000000000000000000000000000000002b";

describe("decodeAction", () => {
  it("decodes a real limit order", () => {
    const d = decodeAction(LIMIT);
    expect(d).toMatchObject({ actionId: 1, action: "LimitOrder", decoded: true, asset: 110004 });
    expect(d.order).toEqual({ isBuy: false, limitPx: 120.93, sz: 0.083, market: false, reduceOnly: true, tif: "Ioc", cloid: "0x2b" });
  });
  it("reads uint64 max as 'no limit', not a $184B price", () => {
    const max = "ffffffffffffffff".padStart(64, "0");
    const d = decodeAction(`0x01000001${LIMIT.slice(10, 74)}${"0".repeat(64)}${max}${max}${LIMIT.slice(266)}` as `0x${string}`);
    expect(d.order).toMatchObject({ market: true, limitPx: undefined, sz: undefined });
  });
  it("never throws on junk", () => {
    expect(decodeAction("0x01").action).toBe("Malformed");
    expect(decodeAction("0x1a7d2c0000").actionId).toBe(-1); // wrong version byte
    expect(decodeAction("0x0100006300").decoded).toBe(false); // unknown action 99
    expect(decodeAction("0x01000001dead").decoded).toBe(false); // truncated ABI body
  });
});

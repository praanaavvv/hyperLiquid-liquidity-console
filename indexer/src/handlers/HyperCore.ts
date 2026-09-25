import { indexer, type Totals } from "envio";
import { decodeAction } from "../decode";

const EMPTY: Totals = { id: "all", actions: 0, orders: 0, marketOrders: 0, senders: 0, hypeDeposits: 0, hypeIn: 0, lastBlock: 0, lastTimestamp: 0 };

indexer.onEvent(
  { contract: "CoreWriter", event: "RawAction", fields: { block: ["timestamp"], transaction: ["hash"] } },
  async ({ event, context }) => {
    const d = decodeAction(event.params.data as `0x${string}`), o = d.order;
    const block = event.block.number;
    const notional = o?.limitPx != null && o.sz != null ? o.limitPx * o.sz : undefined;
    context.CoreAction.set({
      id: `${block}_${event.logIndex}`,
      sender: event.params.user,
      actionId: d.actionId,
      action: d.action,
      block,
      timestamp: event.block.timestamp,
      txHash: event.transaction.hash,
      asset: d.asset,
      isBuy: o?.isBuy,
      limitPx: o?.limitPx,
      sz: o?.sz,
      market: o?.market,
      notional,
      tif: o?.tif,
      reduceOnly: o?.reduceOnly,
      cloid: o?.cloid,
      args: JSON.stringify(d.args),
      decoded: d.decoded,
    });

    const [stat, sender, totals] = await Promise.all([
      context.ActionStat.get(String(d.actionId)),
      context.Sender.get(event.params.user),
      context.Totals.get("all"),
    ]);
    context.ActionStat.set({ id: String(d.actionId), actionId: d.actionId, action: d.action, count: (stat?.count ?? 0) + 1, lastBlock: block });
    context.Sender.set({
      id: event.params.user,
      actions: (sender?.actions ?? 0) + 1,
      orders: (sender?.orders ?? 0) + (o ? 1 : 0),
      firstBlock: sender?.firstBlock ?? block,
      lastBlock: block,
    });
    const t = totals ?? EMPTY;
    context.Totals.set({
      ...t,
      actions: t.actions + 1,
      orders: t.orders + (o ? 1 : 0),
      marketOrders: t.marketOrders + (o?.market ? 1 : 0),
      senders: t.senders + (sender ? 0 : 1),
      lastBlock: block,
      lastTimestamp: event.block.timestamp,
    });
  },
);

indexer.onEvent(
  { contract: "HypeSystem", event: "Received", fields: { block: ["timestamp"], transaction: ["hash"] } },
  async ({ event, context }) => {
    const amount = Number(event.params.amount) / 1e18;
    context.HypeDeposit.set({
      id: `${event.block.number}_${event.logIndex}`,
      user: event.params.user,
      amountWei: event.params.amount,
      amount,
      block: event.block.number,
      timestamp: event.block.timestamp,
      txHash: event.transaction.hash,
    });
    const t = (await context.Totals.get("all")) ?? EMPTY;
    context.Totals.set({ ...t, hypeDeposits: t.hypeDeposits + 1, hypeIn: t.hypeIn + amount, lastBlock: event.block.number, lastTimestamp: event.block.timestamp });
  },
);

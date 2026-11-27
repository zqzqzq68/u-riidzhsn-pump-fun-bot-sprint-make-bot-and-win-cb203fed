#!/usr/bin/env python3
"""Pump.fun paper-trading bot for the agent-fun/replay/v2 harness.

Wire protocol (newline-delimited JSON on stdin/stdout):
  harness -> bot:  {"type": "hello", ...} then {"type": "batch", "seq", "slot",
                   "fills", "account", "marks", "events"} ... then {"type": "done", ...}
  bot -> harness:  exactly one {"type": "reply", "seq": <batch seq>, "orders": [...]}
                   per batch. stdout carries replies only; diagnostics go to stderr.

Strategy (fully deterministic: no randomness, no clock, no environment):
  * Track every bonding curve's price (virtual quote reserves / virtual token
    reserves) and its recent trades.
  * BUY a young coin when its last 24 trades are buy-dominated (buy SOL share
    >= 60%) AND its price has risen >= 2% across that window, with a fixed
    0.15 SOL size clamped to the harness per-coin size cap.
  * SELL a whole position when it gains >= 20%, loses >= 12%, falls >= 12%
    from its peak since entry, ages past 2400 slots, or the curve completes.
  * At most max_open_positions open at once; never exceed the per-batch order
    cap; never trade a coin already held or with a buy pending.

Run as: PYTHONHASHSEED=0 python3 -P -s main.py
"""

import json
import sys

LAMPORTS_PER_SOL = 1_000_000_000

# --- strategy constants (all deterministic, tuned for the dev window) ---
BUY_SIZE_LAMPORTS = 150_000_000        # 0.15 SOL per entry
MIN_BUY_LAMPORTS = 10_000_000          # skip a coin whose size cap is below this
WINDOW_TRADES = 24                     # trades kept per coin for the signal
MIN_TRADES = 6                         # trades needed before a coin is buyable
MIN_BUY_SHARE = 0.60                   # buy SOL share across the window
MIN_MOMENTUM = 0.02                    # price rise across the window
MAX_AGE_SLOTS = 20000                  # only coins created within this many slots
MAX_CHASE_MULTIPLE = 3.0               # never buy above 3x the first seen price
TAKE_PROFIT = 0.20                     # sell at +20%
STOP_LOSS = -0.12                      # sell at -12%
TRAILING_DROP = 0.12                   # sell at -12% from the peak since entry
MAX_HOLD_SLOTS = 2400                  # sell once a position is this old
MAX_SLIPPAGE_BPS = 500


class Curve:
    """One bonding curve's latest known state plus its recent trade tape."""

    __slots__ = (
        "mint", "created_slot", "first_seen_slot", "first_price", "price",
        "real_quote_reserves", "complete", "trades", "peak_since_entry",
        "entry_slot",
    )

    def __init__(self, mint, slot):
        self.mint = mint
        self.created_slot = slot
        self.first_seen_slot = slot
        self.first_price = 0.0
        self.price = 0.0
        self.real_quote_reserves = 0
        self.complete = False
        self.trades = []  # (slot, is_buy, sol_lamports, price) oldest first
        self.peak_since_entry = 0.0
        self.entry_slot = -1

    def note_price(self, price, real_quote_reserves, slot):
        if price <= 0.0:
            return
        if self.first_price == 0.0:
            self.first_price = price
        self.price = price
        self.real_quote_reserves = real_quote_reserves

    def note_trade(self, slot, is_buy, sol_lamports, price):
        self.note_price(price, self.real_quote_reserves, slot)
        self.trades.append((slot, is_buy, sol_lamports, price))
        if len(self.trades) > WINDOW_TRADES:
            del self.trades[: len(self.trades) - WINDOW_TRADES]

    def window_stats(self):
        """(buy SOL share, momentum) over the kept trade window."""
        if len(self.trades) < MIN_TRADES:
            return 0.0, 0.0
        buy_sol = 0
        sell_sol = 0
        for _slot, is_buy, sol, _price in self.trades:
            if is_buy:
                buy_sol += sol
            else:
                sell_sol += sol
        total = buy_sol + sell_sol
        share = (buy_sol / total) if total > 0 else 0.0
        first_price = self.trades[0][3]
        momentum = (self.price / first_price - 1.0) if first_price > 0.0 else 0.0
        return share, momentum


class Bot:
    def __init__(self):
        self.hello = None
        self.capital = 0
        self.max_open_positions = 5
        self.max_order_bps = 100
        self.max_orders_per_batch = 16
        self.tx_fee = 0
        self.slot = -1
        self.seq = -1
        self.cash = 0
        self.positions = {}   # mint -> {"tokens": int, "cost": int}
        self.curves = {}      # mint -> Curve
        self.pending_buys = set()  # mints with a buy order awaiting its fill
        self.next_client = 1
        self.batches_seen = 0
        self.buys_sent = 0
        self.sells_sent = 0
        self.distinct_bought = set()

    # -- wire helpers -----------------------------------------------------

    def fresh_client_id(self):
        cid = "o%d" % self.next_client
        self.next_client += 1
        return cid

    @staticmethod
    def price_of(virtual_quote, virtual_token):
        if virtual_token <= 0 or virtual_quote < 0:
            return 0.0
        return virtual_quote / virtual_token

    def curve_for(self, mint, slot):
        curve = self.curves.get(mint)
        if curve is None:
            curve = Curve(mint, slot)
            self.curves[mint] = curve
        return curve

    # -- message handling ---------------------------------------------------

    def on_hello(self, msg):
        self.hello = msg
        self.capital = int(msg["capital"]["lamports"])
        caps = msg["caps"]
        self.max_open_positions = int(caps["max_open_positions"])
        self.max_order_bps = int(caps["max_order_bps_of_real_reserve"])
        self.max_orders_per_batch = int(caps["max_orders_per_batch"])
        fees = msg["fees"]
        self.tx_fee = int(fees["base_fee_lamports"]) + int(
            fees["priority_fee_lamports"]
        )
        self.cash = self.capital

    def apply_fills(self, fills):
        # Every fill clears its mint's pending buy, whatever its status: the
        # SDK pops pending for every fill, and a rejected buy left pending
        # would block that mint (and count against the position cap) forever.
        for fill in fills or []:
            self.pending_buys.discard(fill.get("mint", ""))

    def apply_account(self, account):
        self.cash = int(account["cash_lamports"])
        new_positions = {}
        for pos in account.get("positions", []):
            tokens = int(pos["token_amount"])
            if tokens <= 0:
                continue
            mint = pos["mint"]
            new_positions[mint] = {
                "tokens": tokens,
                "cost": int(pos["cost_lamports"]),
            }
            # A held coin may never have appeared in events/marks (e.g. it
            # was created before the replay window): make sure it has a curve
            # so its exit rules (peak, age) can fire instead of stranding it.
            curve = self.curve_for(mint, self.slot)
            if mint not in self.positions:
                if curve.entry_slot < 0:
                    curve.entry_slot = self.slot
                if curve.peak_since_entry <= 0.0 and curve.price > 0.0:
                    curve.peak_since_entry = curve.price
            if curve.price > curve.peak_since_entry:
                curve.peak_since_entry = curve.price
        self.positions = new_positions

    def apply_marks(self, marks):
        for mark in marks or []:
            curve = self.curve_for(mark["mint"], int(mark["slot"]))
            price = self.price_of(
                int(mark["virtual_quote_reserves"]),
                int(mark["virtual_token_reserves"]),
            )
            curve.note_price(
                price, int(mark["real_quote_reserves"]), int(mark["slot"])
            )

    def apply_events(self, events):
        for event in events or []:
            kind = event.get("event")
            mint = event.get("mint")
            if not mint:
                continue
            slot = int(event.get("slot", self.slot))
            if kind == "CreateEvent":
                curve = self.curve_for(mint, slot)
                curve.created_slot = slot
                curve.note_price(
                    self.price_of(
                        int(event["virtual_sol_reserves"]),
                        int(event["virtual_token_reserves"]),
                    ),
                    int(event["real_token_reserves"]),
                    slot,
                )
            elif kind == "TradeEvent":
                curve = self.curve_for(mint, slot)
                price = self.price_of(
                    int(event["virtual_sol_reserves"]),
                    int(event["virtual_token_reserves"]),
                )
                curve.note_trade(
                    slot,
                    bool(event["is_buy"]),
                    int(event["sol_amount"]),
                    price,
                )
                curve.real_quote_reserves = int(event["real_sol_reserves"])
            elif kind == "CompleteEvent":
                curve = self.curve_for(mint, slot)
                curve.complete = True

    # -- decisions ----------------------------------------------------------

    def max_buy_for(self, curve):
        """Per-coin size cap for this batch: bps of the real quote reserves."""
        return curve.real_quote_reserves * self.max_order_bps // 10000

    def exit_orders(self):
        orders = []
        for mint in sorted(self.positions):
            pos = self.positions[mint]
            curve = self.curves.get(mint)
            if curve is None:
                continue
            age = self.slot - curve.entry_slot if curve.entry_slot >= 0 else 0
            # Age and completion exits need no current price: a coin whose
            # curve went quiet must still be sold, never stranded.
            if age >= MAX_HOLD_SLOTS or curve.complete:
                orders.append(
                    {
                        "side": "sell",
                        "client_id": self.fresh_client_id(),
                        "mint": mint,
                        "token_amount": str(pos["tokens"]),
                        "max_slippage_bps": MAX_SLIPPAGE_BPS,
                    }
                )
                continue
            if curve.price <= 0.0:
                continue
            avg_cost = pos["cost"] / pos["tokens"]
            gain = curve.price / avg_cost - 1.0 if avg_cost > 0 else 0.0
            trailing = (
                curve.price <= (1.0 - TRAILING_DROP) * curve.peak_since_entry
                if curve.peak_since_entry > 0.0
                else False
            )
            if gain >= TAKE_PROFIT or gain <= STOP_LOSS or trailing:
                orders.append(
                    {
                        "side": "sell",
                        "client_id": self.fresh_client_id(),
                        "mint": mint,
                        "token_amount": str(pos["tokens"]),
                        "max_slippage_bps": MAX_SLIPPAGE_BPS,
                    }
                )
        return orders

    def entry_orders(self, budget_orders):
        candidates = []
        for mint in sorted(self.curves):
            curve = self.curves[mint]
            if curve.complete or curve.price <= 0.0:
                continue
            if mint in self.positions or mint in self.pending_buys:
                continue
            if self.slot - curve.created_slot > MAX_AGE_SLOTS:
                continue
            if curve.first_price > 0.0 and (
                curve.price > MAX_CHASE_MULTIPLE * curve.first_price
            ):
                continue
            share, momentum = curve.window_stats()
            if share < MIN_BUY_SHARE or momentum < MIN_MOMENTUM:
                continue
            candidates.append((momentum, mint, curve))
        # Strongest momentum first; mint breaks ties deterministically.
        candidates.sort(key=lambda item: (-item[0], item[1]))
        orders = []
        committed = len(self.positions) + len(self.pending_buys)
        free_cash = self.cash
        for _momentum, mint, curve in candidates:
            if len(orders) >= budget_orders:
                break
            if committed >= self.max_open_positions:
                break
            cap = self.max_buy_for(curve)
            if cap < MIN_BUY_LAMPORTS:
                continue
            size = BUY_SIZE_LAMPORTS
            if size > cap:
                size = cap
            if size > free_cash - self.tx_fee:
                size = free_cash - self.tx_fee
            if size < MIN_BUY_LAMPORTS:
                continue
            orders.append(
                {
                    "side": "buy",
                    "client_id": self.fresh_client_id(),
                    "mint": mint,
                    "quote_lamports": str(size),
                    "max_slippage_bps": MAX_SLIPPAGE_BPS,
                }
            )
            self.pending_buys.add(mint)
            self.distinct_bought.add(mint)
            committed += 1
            free_cash -= size + self.tx_fee
        return orders

    def decide(self):
        orders = self.exit_orders()
        room = self.max_orders_per_batch - len(orders)
        if room > 0:
            orders.extend(self.entry_orders(room))
        return orders[: self.max_orders_per_batch]

    # -- main loop ----------------------------------------------------------

    def run(self):
        out = sys.stdout
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            msg = json.loads(line)
            msg_type = msg.get("type")
            if msg_type == "hello":
                self.on_hello(msg)
            elif msg_type == "batch":
                self.seq = int(msg["seq"])
                self.slot = int(msg["slot"])
                self.apply_fills(msg.get("fills"))
                self.apply_account(msg["account"])
                self.apply_marks(msg.get("marks"))
                self.apply_events(msg.get("events"))
                orders = self.decide()
                for order in orders:
                    if order["side"] == "buy":
                        self.buys_sent += 1
                    else:
                        self.sells_sent += 1
                reply = {"type": "reply", "seq": self.seq, "orders": orders}
                out.write(json.dumps(reply, separators=(",", ":")) + "\n")
                out.flush()
                self.batches_seen += 1
            elif msg_type == "done":
                self.apply_account(msg["account"])
                print(
                    "done: batches=%d buys=%d sells=%d distinct_bought=%d "
                    "cash=%d open_positions=%d"
                    % (
                        self.batches_seen,
                        self.buys_sent,
                        self.sells_sent,
                        len(self.distinct_bought),
                        self.cash,
                        len(self.positions),
                    ),
                    file=sys.stderr,
                )
                return


def main():
    Bot().run()


if __name__ == "__main__":
    main()

"""Your ledger. This is the whole assignment.

`client.py` handles the network and hands you one event at a time. You return
the journal legs it produced. Some events correctly produce none: return an
empty list, not None-as-an-accident.

One event type is implemented as a worked example. The rest raise, with the rule
from PROTOCOL.md quoted in the message, so a practice run tells you exactly what
is left rather than silently scoring zero.

Two things to get right before anything else:

  * Use `Decimal`, never `float`. Money here does not always divide evenly, and
    a float implementation will disagree with us by a cent in places you will
    struggle to find.
  * Key balances by (customer, account), not by account. At least one event
    moves money between two customers on the same account, and an
    account-level book shows nothing wrong at all.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP

D = Decimal
ZERO = D("0.00")


def money(x: Decimal) -> Decimal:
    """2 decimal places, half away from zero. Not round(), which is half-even."""
    return x.quantize(D("0.01"), rounding=ROUND_HALF_UP)


def qstr(x: Decimal) -> str:
    """Plain decimal string for a quantity, trailing zeros stripped."""
    return format(D(x).normalize(), "f")


def qty6(x: Decimal) -> Decimal:
    """Share quantity to at most 6 decimal places, half away from zero."""
    return D(x).quantize(D("0.000001"), rounding=ROUND_HALF_UP)


def leg(account: str, customer_id: str, debit=ZERO, credit=ZERO) -> dict:
    return {"account": account, "customer_id": customer_id,
            "debit": str(money(D(debit))), "credit": str(money(D(credit)))}


# -- tariff ---------------------------------------------------------------
# All per unit of principal. bps = basis points (1 bps = 0.0001).
TARIFF: dict[str, dict] = {
    "BRK-A": {"classes": ("equity", "etf"),  "brokerage_bps": D("0.0020"),
              "custody_bps": D("0.0004"), "broker_cost_bps": D("0.0009"),
              "custody_cost_bps": D("0.0002"), "min_fee": D("1.00"),
              "ticket": D("0.35")},
    "BRK-B": {"classes": ("equity", "bond"), "brokerage_bps": D("0.0015"),
              "custody_bps": D("0.0005"), "broker_cost_bps": D("0.0008"),
              "custody_cost_bps": D("0.0003"), "min_fee": D("2.50"),
              "ticket": D("3.00")},
    "BRK-C": {"classes": ("etf", "bond"),   "brokerage_bps": D("0.0025"),
              "custody_bps": D("0.0003"), "broker_cost_bps": D("0.0012"),
              "custody_cost_bps": D("0.0001"), "min_fee": D("0.50"),
              "ticket": D("0.20")},
}
REG_BPS = D("0.0008")

BROKER_ACCOUNT = {"BRK-A": "2411", "BRK-B": "2412", "BRK-C": "2413"}


def broker_fees(broker: str, principal: Decimal) -> tuple[Decimal, ...]:
    """Derive the fill fee chain for one broker and principal. Each amount is
    rounded to the cent independently, before use. Broker cost includes the
    broker's flat ticket fee whatever the size of the fill.

    Returns (brokerage, custody, regulatory, broker_cost, custody_cost).
    """
    t = TARIFF[broker]
    brokerage = max(money(principal * t["brokerage_bps"]), t["min_fee"])
    custody = money(principal * t["custody_bps"])
    reg = money(principal * REG_BPS)
    broker_cost = money(principal * t["broker_cost_bps"]) + t["ticket"]
    custody_cost = money(principal * t["custody_cost_bps"])
    return brokerage, custody, reg, broker_cost, custody_cost


def fill_fees(broker: str, principal: Decimal, partner_rate: Decimal
              ) -> tuple[Decimal, ...]:
    """All six derived amounts for one fill, each rounded to the cent
    independently before use.

    partner_rate x (revenue - cost), where revenue is what the customer was
    charged (brokerage + custody) and cost is what broker + custodian charged
    the firm (broker cost + custody cost). Where cost exceeds revenue the share
    is zero; there is no clawback. partner_rate can be 0.50, so the product can
    land exactly on a half cent and the rounding convention above decides it.

    Returns (brokerage, custody, regulatory, broker_cost, custody_cost,
    partner_share).
    """
    brokerage, custody, reg, broker_cost, custody_cost = \
        broker_fees(broker, principal)
    revenue = brokerage + custody
    cost = broker_cost + custody_cost
    if cost >= revenue:
        partner_share = ZERO
    else:
        partner_share = money(partner_rate * (revenue - cost))
    return brokerage, custody, reg, broker_cost, custody_cost, partner_share


INV_OP = {"add": "del_lot", "del_lot": "add",
          "consume_full": "ins_lot", "ins_lot": "consume_full",
          "consume_part": "repl_lot", "repl_lot": "consume_part"}


class Book:
    def __init__(self) -> None:
        # balances[(customer_id, account)] = debit-positive balance
        self.balances: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)
        self.seen: set[str] = set()
        # What you have not written yet. An unimplemented handler must not stop
        # the run: the client keeps consuming and tells you the list at the end.
        self.todo: dict[str, int] = defaultdict(int)

        # Every account ever posted to, even those netted back to zero.
        self.touched: set[str] = set()

        # FIFO lot book per (customer, symbol). Delivery order = list order.
        # Each lot: {"id": int, "qty": Decimal, "cost": Decimal (total cost)}
        self.lots: dict[tuple[str, str], list[dict]] = defaultdict(list)
        self._next_lot_id = 0

        # Order lifecycle (filled in by later phases).
        self.orders: dict[str, dict] = {}          # order_id -> order data
        self.holds: dict[str, dict] = {}           # order_id -> {side, total, remaining, order_qty}
        self.open_routes: dict[str, str] = {}      # order_id -> broker (still open)
        self.closed_orders: set[str] = set()       # fully filled, cancelled, or rejected

        # Lookups for events that reference earlier ones.
        self.withdrawals: dict[str, dict] = {}     # withdrawal_id -> {amount, customer_id}
        self.fee_amounts: dict[str, dict] = {}     # fee_charged event_id -> amount
        self.refunded_fees: set[str] = set()       # fee_charged event_ids already refunded
        self.trades: dict[str, dict] = {}          # trade_id -> {side, principal, customer_id}

        # Full history in delivery order. Each entry:
        #   {"event": ev, "legs": [...], "lot_ops": [...], "status": ...}
        # Powers as-of checkpoints (replay) and reversals (lookup + inverse).
        self.event_log: list[dict] = []
        self._ops: list[dict] = []                 # lot ops of the event being applied

    # -----------------------------------------------------------------------
    # Lot book helpers. Every mutation is recorded into self._ops so a reversal
    # can undo it exactly, and so as-of replay is reproducible.
    # -----------------------------------------------------------------------
    def _add_lot(self, cid: str, symbol: str, q: Decimal, cost: Decimal) -> int:
        lot = {"id": self._next_lot_id, "qty": q, "cost": cost}
        self._next_lot_id += 1
        self.lots[(cid, symbol)].append(lot)
        self._ops.append({"op": "add", "cid": cid, "symbol": symbol,
                          "id": lot["id"], "qty": q, "cost": cost})
        return lot["id"]

    def _consume_lots(self, cid: str, symbol: str, q: Decimal) -> Decimal:
        """FIFO cost relief. Raises Rejected on oversell BEFORE touching lots.

        Cost relieved is round(lot_total x sold_qty / lot_qty); the remainder
        stays with the lot. No lots are half-consumed on a rejected sell.
        """
        lots = self.lots.get((cid, symbol), [])
        if q > sum(l["qty"] for l in lots):
            raise Rejected("oversell")
        remaining = q
        total_cost = ZERO
        while remaining > 0:
            lot = lots[0]
            take = min(lot["qty"], remaining)
            if take == lot["qty"]:
                cost = lot["cost"]
                lots.pop(0)
                self._ops.append({"op": "consume_full", "cid": cid, "symbol": symbol,
                                  "id": lot["id"], "qty": take, "cost": cost})
            else:
                cost = money(lot["cost"] * take / lot["qty"])
                lot["qty"] -= take
                lot["cost"] -= cost
                self._ops.append({"op": "consume_part", "cid": cid, "symbol": symbol,
                                  "id": lot["id"], "qty": take, "cost": cost})
            total_cost += cost
            remaining -= take
        return total_cost

    def _position(self, cid: str, symbol: str) -> tuple[Decimal, Decimal]:
        lots = self.lots.get((cid, symbol), [])
        return sum(l["qty"] for l in lots), sum(l["cost"] for l in lots)

    def _apply_lot_ops(self, ops: list[dict]) -> None:
        """Apply recorded lot ops to this book, recording each applied op into
        the current event's lot_ops so a reversal of this event (or of a
        reversal) can invert them symmetrically.

        op -- inverse -- meaning
        add -- del_lot -- append a lot (id pre-allocated)
        del_lot -- add -- remove the lot by id
        consume_full -- ins_lot -- a lot was fully consumed (re-insert it)
        ins_lot -- consume_full -- remove a re-inserted lot again
        consume_part -- repl_lot -- part of a lot was consumed (restore it)
        repl_lot -- consume_part -- re-deduct a restored portion
        """
        for op in ops:
            key = (op["cid"], op["symbol"])
            t = op["op"]
            if t == "add":
                self.lots[key].append({"id": op["id"], "qty": op["qty"],
                                       "cost": op["cost"]})
            elif t == "del_lot":
                lst = self.lots.get(key, [])
                for i, lot in enumerate(lst):
                    if lot["id"] == op["id"]:
                        lst.pop(i)
                        break
            elif t == "consume_full":
                self.lots[key].insert(0, {"id": op["id"], "qty": op["qty"],
                                          "cost": op["cost"]})
            elif t == "ins_lot":
                lst = self.lots.get(key, [])
                for i, lot in enumerate(lst):
                    if lot["id"] == op["id"]:
                        lst.pop(i)
                        break
            elif t == "consume_part":
                for lot in self.lots.get(key, []):
                    if lot["id"] == op["id"]:
                        lot["qty"] -= op["qty"]
                        lot["cost"] -= op["cost"]
                        break
            elif t == "repl_lot":
                for lot in self.lots.get(key, []):
                    if lot["id"] == op["id"]:
                        lot["qty"] += op["qty"]
                        lot["cost"] += op["cost"]
                        break
            self._ops.append(op)

    def find_event(self, event_id: str) -> dict | None:
        """The raw event as first delivered, or None."""
        for entry in self.event_log:
            if entry["event"]["event_id"] == event_id:
                return entry["event"]
        return None

    # -----------------------------------------------------------------------
    def apply(self, ev: dict) -> list[dict]:
        """Post one event and return its legs.

        The same event_id can arrive more than once, and the server will
        deliberately re-send several hundred events partway through the run.
        Posting twice is the single most expensive mistake available here.
        """
        eid = ev["event_id"]
        if eid in self.seen:
            return []                      # already seen; nothing new happens
        self.seen.add(eid)

        entry = {"event": ev, "legs": [], "lot_ops": [], "status": "noop"}
        self._ops = []
        handler = getattr(self, "on_" + ev["type"], None)
        if handler is not None:
            try:
                legs = handler(ev["payload"], ev) or []
                if legs:
                    self._post(legs)
                    entry["legs"] = legs
                    entry["status"] = "posted"
            except NotImplementedError:
                # Not written yet. Submit nothing for it and carry on.
                self.todo[ev["type"]] += 1
                entry["status"] = "unimplemented"
            except Rejected:
                # An event you refuse still gets a submission, with no legs, and
                # it must leave your book exactly as it was. A redelivered
                # rejected event stays one rejection: it is already in `seen`.
                entry["status"] = "rejected"
            except Exception as exc:
                # A payload that will not parse or a handler bug must never stop
                # the run: reject the event and carry on. This is also how the
                # feed's deliberate malformed events are handled.
                print(f"  rejecting {ev['type']} {eid}: "
                      f"{type(exc).__name__}: {exc}", flush=True)
                entry["status"] = "rejected"
        else:
            self.todo[ev["type"]] += 1
            entry["status"] = "unimplemented"
        entry["lot_ops"] = self._ops
        self.event_log.append(entry)
        return entry["legs"]

    def _post(self, legs: list[dict]) -> None:
        dr = sum(D(l["debit"]) for l in legs)
        cr = sum(D(l["credit"]) for l in legs)
        if money(dr) != money(cr):
            raise AssertionError(f"unbalanced: dr {dr} cr {cr}")
        for l in legs:
            self.touched.add(l["account"])
            self.balances[(l["customer_id"], l["account"])] += (
                D(l["debit"]) - D(l["credit"]))

    # -----------------------------------------------------------------------
    # As-of answering: replay the event log into a fresh book and snapshot it.
    # At 800-6,000 events this is comfortably under a second, well inside the
    # checkpoint grace period.
    # -----------------------------------------------------------------------
    def _replay_to(self, target_event_id: str) -> "Book":
        book = Book()
        found = False
        for entry in self.event_log:
            book.apply(entry["event"])
            if entry["event"]["event_id"] == target_event_id:
                found = True
                break
        if not found:
            print(f"  as-of target {target_event_id} not in log; using current state",
                  flush=True)
            return self
        return book

    # -- worked example -----------------------------------------------------
    def on_deposit(self, p: dict, ev: dict) -> list[dict]:
        """Cash arrives, and the firm owes the customer more.

            Dr 1100 amount        Cr 2010 amount
        """
        amount = money(D(p["amount"]))
        cid = p["customer_id"]
        return [leg("1100", cid, debit=amount),
                leg("2010", cid, credit=amount)]

    # -- yours --------------------------------------------------------------
    def on_fee_charged(self, p, ev):
        """The customer pays the firm's fee out of their wallet.

            Dr 2010 amount        Cr 1100 amount
        """
        amount = money(D(p["amount"]))
        cid = p["customer_id"]
        self.fee_amounts[ev["event_id"]] = {"customer_id": cid, "amount": amount}
        return [leg("2010", cid, debit=amount),
                leg("1100", cid, credit=amount)]

    def on_fee_refund(self, p, ev):
        """Undo a fee_charged in full. The amount is NOT in this payload.

            Dr 1100 amount        Cr 2010 amount
        """
        src = p["refunds_source_id"]
        if src in self.refunded_fees:
            raise Rejected("fee already refunded")
        fee = self.fee_amounts.get(src)
        if fee is None:
            raise Rejected("refund of unknown fee_charged")
        cid = p["customer_id"]
        self.refunded_fees.add(src)
        return [leg("1100", cid, debit=fee["amount"]),
                leg("2010", cid, credit=fee["amount"])]

    def on_interest_credited(self, p, ev):
        """Interest on the omnibus balance, shared with the customer. The firm
        keeps the remainder, so this is not a pass-through.

            Dr 1100 gross             Cr 2010 customer_share
                                    Cr 4200 gross - customer_share
        """
        gross = money(D(p["gross_amount"]))
        share = money(D(p["customer_share"]))
        cid = p["customer_id"]
        return [leg("1100", cid, debit=gross),
                leg("2010", cid, credit=share),
                leg("4200", cid, credit=gross - share)]

    def on_transfer_between_customers(self, p, ev):
        """No external cash moves. Both legs land on 2010, so the ACCOUNT nets
        to zero: a book keyed per account shows nothing happening at all.

            Dr 2010 amount  (from_customer_id)
                                    Cr 2010 amount  (to_customer_id)
        """
        amount = money(D(p["amount"]))
        return [leg("2010", p["from_customer_id"], debit=amount),
                leg("2010", p["to_customer_id"], credit=amount)]

    def on_fx_deposit(self, p, ev):
        """Money arrives in another currency and is converted. The omnibus
        account gets the market value; the customer is credited at their worse
        rate; the gap is the firm's spread, earned now.

            Dr 1100 usd_at_market_rate       Cr 2010 usd_at_customer_rate
                                             Cr 4100 the difference
        """
        market = money(D(p["usd_at_market_rate"]))
        customer = money(D(p["usd_at_customer_rate"]))
        if customer > market:
            raise Rejected("customer rate better than market (negative spread)")
        cid = p["customer_id"]
        return [leg("1100", cid, debit=market),
                leg("2010", cid, credit=customer),
                leg("4100", cid, credit=market - customer)]

    def on_withdrawal_requested(self, p, ev):
        """The money leaves the wallet but not yet the broker: it is now owed
        as a withdrawal being processed, not as wallet money.

            Dr 2010 amount        Cr 2300 amount
        """
        amount = money(D(p["amount"]))
        cid = p["customer_id"]
        wid = p["withdrawal_id"]
        self.withdrawals[wid] = {"customer_id": cid, "amount": amount}
        return [leg("2010", cid, debit=amount),
                leg("2300", cid, credit=amount)]

    def on_withdrawal_settled(self, p, ev):
        """The cash actually leaves the broker. Amount comes from the request.

            Dr 2300 amount        Cr 1100 amount
        """
        w = self.withdrawals.get(p["withdrawal_id"])
        if w is None:
            raise Rejected("settle of unknown withdrawal")
        cid = w["customer_id"]
        return [leg("2300", cid, debit=w["amount"]),
                leg("1100", cid, credit=w["amount"])]

    def on_withdrawal_rejected(self, p, ev):
        """The withdrawal fails; the money is owed as wallet money again. No
        cash moved at any point.

            Dr 2300 amount        Cr 2010 amount
        """
        w = self.withdrawals.get(p["withdrawal_id"])
        if w is None:
            raise Rejected("rejection of unknown withdrawal")
        cid = w["customer_id"]
        return [leg("2300", cid, debit=w["amount"]),
                leg("2010", cid, credit=w["amount"])]

    def on_order_placed(self, p, ev):
        """No legs. A placement moves no money: it creates a hold, which is
        reported at checkpoints and never posted. It also records the routing
        decision for the still-open order.

        A fill may arrive before its placement (the stream is not date-ordered);
        the order record and hold then account for the quantity already filled.
        """
        oid = p["order_id"]
        order = self.orders.get(oid) or {}
        order.update({
            "customer_id": p["customer_id"],
            "side": p["side"],
            "symbol": p["symbol"],
            "quantity": D(p["quantity"]),
            "limit_price": money(D(p["limit_price"])),
            "asset_class": p["asset_class"],
            "est_charges": money(D(p["est_charges"])),
            "filled_qty": order.get("filled_qty", ZERO),
        })
        self.orders[oid] = order

        if oid in self.closed_orders:
            return []                      # already filled or cancelled; nothing to hold

        side = order["side"]
        q = order["quantity"]
        unfilled = q - order["filled_qty"]
        if unfilled <= 0:
            self._close_order(oid)
            return []

        if side == "buy":
            total = money(q * order["limit_price"]) + order["est_charges"]
            remaining = money(total * unfilled / q)
        else:
            total = q
            remaining = unfilled
        self.holds[oid] = {"side": side, "total": total,
                           "remaining": remaining, "order_qty": q}
        principal = money(q * order["limit_price"])
        self.open_routes[oid] = self._route(order["asset_class"], principal)
        return []

    def on_order_partially_filled(self, p, ev):
        return self.on_order_filled(p, ev)

    def on_order_filled(self, p, ev):
        """A fill posts the full buy/sell economics. Cash does NOT move on the
        trade date; a trade_settled event discharges the obligation later.

        Buy (P principal, b/c/r customer charges, bc/cc/ps firm costs):
            Dr 2010 P+b+c+r        Cr 2350 P
            Dr 1200 P              Cr 2100 P
            Dr 5000 bc             Cr 4000 b
            Dr 5010 cc             Cr 4010 c
            Dr 5100 ps             Cr 2400 r
                                   Cr 241x bc
                                   Cr 2420 cc
                                   Cr 2430 ps
        Sell: same firm economics, with Dr 1150 P / Cr 2010 P-b-c-r, and
            Dr 2100 cost / Cr 1200 cost where cost is the FIFO cost of the
            shares sold (never the realised P&L directly).

        The fee amounts are not in the payload: the tariff turns the broker and
        the principal into money. A fill releases a proportional share of the
        hold that order placed; order_filled is the last fill and closes it.
        """
        oid = p["order_id"]
        cid = p["customer_id"]
        side = p["side"]
        symbol = p["symbol"]
        q = D(p["quantity"])
        principal = money(D(p["principal"]))
        broker = p["broker"]
        partner_rate = D(p["partner_rate"])
        broker_payable = BROKER_ACCOUNT[broker]

        b, c, r, bc, cc, ps = fill_fees(broker, principal, partner_rate)

        # Consume/receive lots BEFORE touching any other state, so an oversell
        # raises Rejected leaving the book exactly as it was.
        if side == "sell":
            cost = self._consume_lots(cid, symbol, q)
        else:
            cost = ZERO

        order = self.orders.get(oid) or {}
        order.setdefault("customer_id", cid)
        order.setdefault("side", side)
        order.setdefault("symbol", symbol)
        order.setdefault("asset_class", p.get("asset_class"))
        order["filled_qty"] = order.get("filled_qty", ZERO) + q
        self.orders[oid] = order

        self.trades[p["trade_id"]] = {"side": side, "principal": principal,
                                      "customer_id": cid}
        self._release_hold(oid, q)
        if ev["type"] == "order_filled":
            self._close_order(oid)

        if side == "buy":
            self._add_lot(cid, symbol, q, principal)
            return [
                leg("2010", cid, debit=principal + b + c + r),
                leg("1200", cid, debit=principal),
                leg("5000", cid, debit=bc),
                leg("5010", cid, debit=cc),
                leg("5100", cid, debit=ps),
                leg("2350", cid, credit=principal),
                leg("2100", cid, credit=principal),
                leg("4000", cid, credit=b),
                leg("4010", cid, credit=c),
                leg("2400", cid, credit=r),
                leg(broker_payable, cid, credit=bc),
                leg("2420", cid, credit=cc),
                leg("2430", cid, credit=ps),
            ]
        return [
            leg("1150", cid, debit=principal),
            leg("2100", cid, debit=cost),
            leg("5000", cid, debit=bc),
            leg("5010", cid, debit=cc),
            leg("5100", cid, debit=ps),
            leg("2010", cid, credit=principal - b - c - r),
            leg("1200", cid, credit=cost),
            leg("4000", cid, credit=b),
            leg("4010", cid, credit=c),
            leg("2400", cid, credit=r),
            leg(broker_payable, cid, credit=bc),
            leg("2420", cid, credit=cc),
            leg("2430", cid, credit=ps),
        ]

    def on_trade_settled(self, p, ev):
        """Settlement day: the cash from that fill actually moves, discharging
        the obligation the fill created. Nothing else about the trade changes.

            buy    Dr 2350 principal     Cr 1100 principal
            sell   Dr 1100 principal     Cr 1150 principal
        """
        t = self.trades.get(p["trade_id"])
        if t is None:
            raise Rejected("settle of unknown trade")
        cid = t["customer_id"]
        principal = t["principal"]
        if t["side"] == "buy":
            return [leg("2350", cid, debit=principal),
                    leg("1100", cid, credit=principal)]
        return [leg("1100", cid, debit=principal),
                leg("1150", cid, credit=principal)]

    # -- paying it all onward ---------------------------------------------
    # Four payables accrue a few cents per trade and are discharged in full,
    # one customer at a time, paid out of omnibus cash. The amount is never in
    # the payload: it is whatever has accumulated on that account for that
    # customer, so each of these audits every per-trade rounding since the last
    # one. Settling an account with nothing outstanding is an error.
    def _settle_payable(self, cid: str, account: str) -> list[dict]:
        outstanding = -self.balances.get((cid, account), ZERO)
        if outstanding <= 0:
            raise Rejected(f"nothing outstanding to settle on {account}")
        return [leg(account, cid, debit=outstanding),
                leg("1100", cid, credit=outstanding)]

    def on_broker_fees_settled(self, p, ev):
        return self._settle_payable(p["customer_id"],
                                    BROKER_ACCOUNT[p["broker"]])

    def on_custodian_fees_settled(self, p, ev):
        return self._settle_payable(p["customer_id"], "2420")

    def on_reg_fees_remitted(self, p, ev):
        return self._settle_payable(p["customer_id"], "2400")

    def on_partner_payout(self, p, ev):
        return self._settle_payable(p["customer_id"], "2430")

    def on_order_cancelled(self, p, ev):
        """No legs. Release the remaining hold; the order is closed."""
        self._close_order(p["order_id"])
        return []

    def on_order_rejected(self, p, ev):
        return self.on_order_cancelled(p, ev)

    # -- order lifecycle helpers -------------------------------------------
    def _route(self, asset_class: str, principal: Decimal) -> str:
        """Cheapest total customer charge (brokerage + custody) at this
        principal, among brokers trading the asset class. Ties break on broker
        id ascending, so there is always exactly one right answer.
        """
        best, best_cost = None, None
        for broker in sorted(TARIFF):
            t = TARIFF[broker]
            if asset_class not in t["classes"]:
                continue
            brokerage = max(money(principal * t["brokerage_bps"]), t["min_fee"])
            custody = money(principal * t["custody_bps"])
            cost = brokerage + custody
            if best_cost is None or cost < best_cost:
                best, best_cost = broker, cost
        return best

    def _release_hold(self, oid: str, fill_qty: Decimal) -> None:
        """A fill releases a proportional share of the hold that order placed.
        The final fill or a cancellation releases whatever remains, so a closed
        order always returns its hold to exactly zero (see _close_order).
        """
        hold = self.holds.get(oid)
        if hold is None or hold["order_qty"] == 0:
            return
        if hold["side"] == "buy":
            release = money(hold["total"] * fill_qty / hold["order_qty"])
        else:
            release = fill_qty
        hold["remaining"] = max(hold["remaining"] - release, ZERO)

    def _close_order(self, oid: str) -> None:
        """Final fill, cancellation, or rejection: release the rest of the hold
        and drop the open-order route. A released hold stays released even if a
        fill is later reversed.
        """
        self.closed_orders.add(oid)
        self.open_routes.pop(oid, None)
        if oid in self.holds:
            self.holds[oid]["remaining"] = ZERO

    def on_dividend_cash(self, p, ev):
        """A dividend arrives. Tax was withheld at source, so only the net ever
        reaches the firm and the firm owes the tax to nobody.

            Dr 1100 net           Cr 2010 net
        """
        net = money(D(p["net_amount"]))
        cid = p["customer_id"]
        return [leg("1100", cid, debit=net),
                leg("2010", cid, credit=net)]

    def on_dividend_reinvested(self, p, ev):
        """The broker reinvests the net directly. Cash is never involved: the
        customer's holding grows by a new lot of reinvest_quantity whose cost
        is the net amount.

            Dr 1200 net           Cr 2100 net       and add a lot
        """
        net = money(D(p["net_amount"]))
        cid = p["customer_id"]
        q = D(p["reinvest_quantity"])
        self._add_lot(cid, p["symbol"], q, net)
        return [leg("1200", cid, debit=net),
                leg("2100", cid, credit=net)]

    def on_stock_split(self, p, ev):
        """No legs. Quantity scales by ratio_to / ratio_from; the total cost of
        each lot is unchanged, so cost per share moves.
        """
        cid = p["customer_id"]
        symbol = p["symbol"]
        factor = D(p["ratio_to"]) / D(p["ratio_from"])
        for lot in self.lots.get((cid, symbol), []):
            lot["qty"] = qty6(lot["qty"] * factor)
        return []

    def on_symbol_change(self, p, ev):
        """No legs. Re-key the holding from the old symbol to the new one."""
        cid = p["customer_id"]
        old_key = (cid, p["old_symbol"])
        if old_key not in self.lots:
            return []
        lots = self.lots.pop(old_key)
        new_key = (cid, p["new_symbol"])
        if new_key in self.lots:
            self.lots[new_key].extend(lots)
        else:
            self.lots[new_key] = lots
        return []

    def on_reversal(self, p, ev):
        """Post the exact inverse of the original's legs and undo its effect on
        the lot book too. A reversed buy whose lot you leave in place balances
        perfectly and quietly corrupts every later cost basis. Both entries are
        kept: the audit trail retains the original and its reversal.

        Reversing a fill does not restore the hold: a released hold stays
        released. A reversal of an event you never received is rejected.
        """
        src = p["reverses_event_id"]
        entry = None
        for e in self.event_log:
            if e["event"]["event_id"] == src:
                entry = e
                break
        if entry is None:
            raise Rejected("reversal of unknown event")
        inv_legs = [{"account": l["account"], "customer_id": l["customer_id"],
                     "debit": l["credit"], "credit": l["debit"]}
                    for l in entry["legs"]]
        for op in reversed(entry["lot_ops"]):
            self._apply_lot_ops([{**op, "op": INV_OP[op["op"]]}])
        return inv_legs

    # -- reporting ----------------------------------------------------------
    def snapshot(self, as_of_event_id: str | None = None) -> dict:
        """What a checkpoint_request wants: your whole state, right now.

        With as_of_event_id, the state as it stood once you had processed that
        event in delivery order, and nothing after it (backdated events that
        arrived later are excluded).

        Report every account you have ever posted to, including any that have
        netted back to zero. Trial balance values are debit-positive, so
        liabilities carry a negative sign.
        """
        book = self._replay_to(as_of_event_id) if as_of_event_id else self

        tb: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for (_cid, acct), bal in book.balances.items():
            tb[acct] += bal
        for acct in book.touched:
            tb.setdefault(acct, ZERO)

        customers: dict[str, dict] = {}
        for (cid, acct), bal in book.balances.items():
            c = customers.setdefault(cid, {"wallet_cash": ZERO,
                                           "cash_hold": ZERO, "positions": {}})
            if acct == "2010":
                c["wallet_cash"] += -bal          # a liability, so credit-positive

        for oid, hold in book.holds.items():
            if hold["side"] == "buy" and hold["remaining"] > 0:
                cid = book.orders[oid]["customer_id"]
                c = customers.setdefault(cid, {"wallet_cash": ZERO,
                                               "cash_hold": ZERO,
                                               "positions": {}})
                c["cash_hold"] += hold["remaining"]

        for (cid, symbol), lots in book.lots.items():
            total_qty = sum(l["qty"] for l in lots)
            if total_qty > 0:
                c = customers.setdefault(cid, {"wallet_cash": ZERO,
                                               "cash_hold": ZERO,
                                               "positions": {}})
                c["positions"][symbol] = {
                    "quantity": qstr(total_qty),
                    "cost_basis": str(money(sum(l["cost"] for l in lots))),
                }

        return {
            "trial_balance": {a: str(money(v)) for a, v in sorted(tb.items())},
            "customers": {cid: {"wallet_cash": str(money(c["wallet_cash"])),
                                "cash_hold": str(money(c["cash_hold"])),
                                "positions": dict(sorted(c["positions"].items()))}
                          for cid, c in sorted(customers.items())},
            "open_order_routes": dict(sorted(book.open_routes.items())),
        }


class Rejected(Exception):
    """Raise from a handler for an event you refuse to post.

    An oversell, a reversal of something you never received, a payload that
    will not parse. Rejecting one event and carrying on beats stopping: a
    server that stalls misses everything after it.
    """

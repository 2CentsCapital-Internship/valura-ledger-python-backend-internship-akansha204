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


def leg(account: str, customer_id: str, debit=ZERO, credit=ZERO) -> dict:
    return {"account": account, "customer_id": customer_id,
            "debit": str(money(D(debit))), "credit": str(money(D(credit)))}


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
        self.holds: dict[str, dict] = {}           # order_id -> {side, total, remaining}
        self.open_routes: dict[str, str] = {}      # order_id -> broker (still open)

        # Lookups for events that reference earlier ones.
        self.withdrawals: dict[str, dict] = {}     # withdrawal_id -> {amount, customer_id}
        self.fee_amounts: dict[str, dict] = {}     # fee_charged event_id -> amount
        self.refunded_fees: set[str] = set()       # fee_charged event_ids already refunded

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
                self._ops.append({"op": "remove", "cid": cid, "symbol": symbol,
                                  "id": lot["id"], "qty": take, "cost": cost})
            else:
                cost = money(lot["cost"] * take / lot["qty"])
                lot["qty"] -= take
                lot["cost"] -= cost
                self._ops.append({"op": "relieve", "cid": cid, "symbol": symbol,
                                  "id": lot["id"], "qty": take, "cost": cost})
            total_cost += cost
            remaining -= take
        return total_cost

    def _position(self, cid: str, symbol: str) -> tuple[Decimal, Decimal]:
        lots = self.lots.get((cid, symbol), [])
        return sum(l["qty"] for l in lots), sum(l["cost"] for l in lots)

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
        raise NotImplementedError(
            "No legs. A placement moves no money: it creates a hold, which is "
            "reported at checkpoints and never posted")

    def on_order_partially_filled(self, p, ev):
        return self.on_order_filled(p, ev)

    def on_order_filled(self, p, ev):
        raise NotImplementedError(
            "buy:  Dr 2010 principal+commission, Dr 1200 principal / "
            "Cr 2350 principal, Cr 2100 principal, Cr 4000 commission. "
            "sell: Dr 1150 principal, Dr 2100 FIFO cost / Cr 2010 "
            "principal-commission-reg, Cr 1200 cost, Cr 4000 commission, "
            "Cr 2400 reg. Cash does NOT move on the trade date")

    def on_trade_settled(self, p, ev):
        raise NotImplementedError(
            "buy: Dr 2350 / Cr 1100.  sell: Dr 1100 / Cr 1150")

    def on_order_cancelled(self, p, ev):
        raise NotImplementedError("No legs. Release the remaining hold")

    def on_order_rejected(self, p, ev):
        return self.on_order_cancelled(p, ev)

    def on_dividend_cash(self, p, ev):
        raise NotImplementedError(
            "Dr 1100 net / Cr 2010 net. Tax is withheld at source, so raise no "
            "payable")

    def on_dividend_reinvested(self, p, ev):
        raise NotImplementedError(
            "Dr 1200 net / Cr 2100 net, and add a lot. Cash is not involved")

    def on_stock_split(self, p, ev):
        raise NotImplementedError(
            "No legs. Quantity scales; total cost does not change")

    def on_symbol_change(self, p, ev):
        raise NotImplementedError("No legs. Re-key the holding")

    def on_reversal(self, p, ev):
        raise NotImplementedError(
            "Post the exact inverse of the original's legs, and undo its effect "
            "on your LOT BOOK too. A reversed buy whose lot you leave behind "
            "balances perfectly and corrupts every later cost basis")

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

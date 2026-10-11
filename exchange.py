"""BingX perpetuos vía ccxt (modo hedge, SL/TP como órdenes condicionales)."""
import logging
import time

import ccxt

from engine import Bar, BAR_MS

log = logging.getLogger("bingx")


def retry(fn, *a, tries=3, **k):
    """Reintenta errores transitorios de red/exchange con espera creciente."""
    for i in range(tries):
        try:
            return fn(*a, **k)
        except (ccxt.NetworkError, ccxt.RateLimitExceeded) as e:
            if i == tries - 1:
                raise
            log.warning("reintento %d/%d: %s", i + 1, tries, e)
            time.sleep(2 * (i + 1))


def public_exchange(cfg):
    ex = ccxt.bingx({"enableRateLimit": True, "options": {"defaultType": "swap"}})
    ex.load_markets()
    return ex


def fetch_candles(ex, symbol, since, until=None, page=1000):
    """Velas 5m paginadas, ordenadas y sin duplicados (incluye la vela en formación)."""
    out = {}
    cur = since
    for _ in range(200):
        rows = retry(ex.fetch_ohlcv, symbol, "5m", since=cur, limit=page)
        if not rows:
            break
        new = [r for r in rows if r[0] not in out]
        for r in rows:
            out[r[0]] = Bar(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]))
        last = max(r[0] for r in rows)
        if not new or last <= cur or (until and last >= until) or len(rows) < 50:
            break
        cur = last + 1
    return [out[k] for k in sorted(out)]


class BingxBroker:
    paper = False

    def __init__(self, cfg):
        self.cfg = cfg
        self.sym = cfg.symbol
        self.ex = ccxt.bingx({"apiKey": cfg.api_key, "secret": cfg.api_secret, "enableRateLimit": True,
                              "options": {"defaultType": "swap"}})
        if cfg.demo:
            self.ex.set_sandbox_mode(True)  # cuenta demo (VST)
        self.ex.load_markets()
        self.m = self.ex.market(self.sym)
        for fn, args in (
            (self.ex.set_position_mode, (True, self.sym)),
            (self.ex.set_margin_mode, ("isolated", self.sym)),
        ):
            try:
                fn(*args)
            except Exception as e:
                log.warning("setup %s: %s", fn.__name__, e)
        for side in ("LONG", "SHORT"):
            try:
                self.ex.set_leverage(cfg.leverage, self.sym, {"side": side})
            except Exception as e:
                log.warning("leverage %s: %s", side, e)

    # ── utilidades ──
    def _ps(self, side): return "LONG" if side == 1 else "SHORT"
    def _close_side(self, side): return "sell" if side == 1 else "buy"

    def equity(self):
        bal = retry(self.ex.fetch_balance)
        try:
            return float(bal["USDT"]["total"])
        except Exception:
            return float(bal["total"]["USDT"])

    def min_qty(self):
        return float((self.m.get("limits", {}).get("amount", {}) or {}).get("min") or 0)

    def round_qty(self, q):
        return float(self.ex.amount_to_precision(self.sym, q))

    def get_position(self, side):
        for p in retry(self.ex.fetch_positions, [self.sym]):
            if float(p.get("contracts") or 0) > 0 and p.get("side") == ("long" if side == 1 else "short"):
                return p
        return None

    def has_position(self, side):
        return self.get_position(side) is not None

    def diag(self):
        out = {"equity": None, "min_qty": self.min_qty(), "hedge": None}
        try:
            out["equity"] = self.equity()
        except Exception as e:
            out["equity"] = f"ERROR: {e}"
        try:
            out["hedge"] = self.ex.fetch_position_mode(self.sym).get("hedged")
        except Exception as e:
            out["hedge"] = f"?: {str(e)[:80]}"
        return out

    # ── órdenes ──
    def open(self, side, qty, ref):
        o = self.ex.create_order(self.sym, "market", "buy" if side == 1 else "sell", qty, None,
                                 {"positionSide": self._ps(side)})
        time.sleep(1.5)
        p = self.get_position(side)
        if p and p.get("entryPrice"):
            return float(p["entryPrice"])
        return float(o.get("average") or o.get("price") or ref)

    def cancel_stops(self, pos):
        for oid in getattr(pos, "stop_ids", []) or []:
            try:
                self.ex.cancel_order(oid, self.sym)
            except Exception as e:
                log.warning("cancel %s: %s", oid, e)
        pos.stop_ids = []

    def set_stops(self, pos):
        """Cancela y recrea SL + TP sobre la cantidad restante. Lanza excepción si falla."""
        self.cancel_stops(pos)
        cs, ps = self._close_side(pos.side), self._ps(pos.side)
        amt = self.round_qty(pos.qty)
        ids = []
        try:
            for kind, px, alt in (("stopLossPrice", pos.sl, "STOP_MARKET"), ("takeProfitPrice", pos.tp, "TAKE_PROFIT_MARKET")):
                price = float(self.ex.price_to_precision(self.sym, px))
                try:
                    o = self.ex.create_order(self.sym, "market", cs, amt, None,
                                             {kind: price, "positionSide": ps, "workingType": "MARK_PRICE"})
                except Exception as e1:
                    log.warning("%s vía ccxt unificado falló (%s); pruebo tipo nativo", kind, e1)
                    o = self.ex.create_order(self.sym, alt, cs, amt, None,
                                             {"stopPrice": price, "positionSide": ps, "workingType": "MARK_PRICE"})
                ids.append(o["id"])
        finally:
            pos.stop_ids = ids

    def reduce(self, pos, qty, px):
        o = self.ex.create_order(self.sym, "market", self._close_side(pos.side), qty, None,
                                 {"positionSide": self._ps(pos.side)})
        return float(o.get("average") or o.get("price") or px)

    def close_all(self, pos, px):
        self.cancel_stops(pos)
        p = self.get_position(pos.side)
        out = px
        if p:
            out = self.reduce(pos, float(p["contracts"]), px)
        try:
            self.ex.cancel_all_orders(self.sym)
        except Exception:
            pass
        return out

    def _exit_price(self, pos):
        try:
            tr = self.ex.fetch_my_trades(self.sym, since=pos.entry_t, limit=100)
            cs = self._close_side(pos.side)
            xs = [t for t in tr if t.get("side") == cs and t["timestamp"] >= pos.entry_t]
            if xs:
                q = sum(t["amount"] for t in xs)
                return sum(t["price"] * t["amount"] for t in xs) / q
        except Exception as e:
            log.warning("fetch_my_trades: %s", e)
        return None

    def _label(self, pos, px):
        if abs(px - pos.tp) < abs(px - pos.sl):
            return "TP"
        return "Trailing" if pos.trail_on else "BE" if pos.be_done else "SL"

    def poll_exit(self, pos):
        """¿Se cerró en el exchange (SL/TP)? Confirma 2 veces para evitar falsos positivos."""
        try:
            if self.has_position(pos.side):
                return None
            time.sleep(3)
            if self.has_position(pos.side):
                return None
        except Exception as e:
            log.warning("poll_exit: %s", e)
            return None
        px = self._exit_price(pos)
        if px is None:
            px = float(self.ex.fetch_ticker(self.sym)["last"])
        try:
            self.ex.cancel_all_orders(self.sym)  # limpia la orden condicional que quede viva
        except Exception:
            pass
        return px, self._label(pos, px)

    def check_exit(self, pos, b):
        return self.poll_exit(pos)

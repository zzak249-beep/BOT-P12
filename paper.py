"""Broker simulado (DRY_RUN y backtest). Misma interfaz que BingxBroker."""
import math


class PaperBroker:
    paper = True

    def __init__(self, cfg):
        self.cfg = cfg
        self.eq = cfg.paper_equity

    def equity(self): return self.eq
    def record_pnl(self, usd): self.eq += usd
    def min_qty(self): return 0.0
    def min_cost(self): return 0.0
    def ensure_setup(self): pass
    def round_qty(self, q): return math.floor(q * 1e6) / 1e6

    def open(self, side, qty, ref):
        return ref * (1 + side * self.cfg.entry_slip / 100)

    def set_stops(self, pos): pass
    def reduce(self, pos, qty, px): return px
    def close_all(self, pos, px): return px

    def check_exit(self, pos, b):
        d = pos.side
        if (d == 1 and b.l <= pos.sl) or (d == -1 and b.h >= pos.sl):  # SL primero (conservador)
            px = pos.sl - d * pos.sl * self.cfg.stop_slip / 100
            return px, ("Trailing" if pos.trail_on else "BE" if pos.be_done and pos.sl == pos.entry else "SL")
        if (d == 1 and b.h >= pos.tp) or (d == -1 and b.l <= pos.tp):
            return pos.tp, "TP"
        return None

    def has_position(self, side): return True
    def poll_exit(self, pos): return None

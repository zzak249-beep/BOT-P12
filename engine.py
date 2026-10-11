"""Motor P12 Hunter v4.6 — port del Pine v6 (modo 'Confirmación 5m', Cripto 24/7).

Se alimenta con velas 5m CERRADAS, en orden. No toca el exchange: devuelve
(señal, eventos). El módulo trader.py ejecuta y gestiona la posición.
"""
from __future__ import annotations

import statistics
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
BAR_MS = 300_000
TF_MIN = 5


def hm(x: int) -> int:
    return (x // 100) * 60 + x % 100


def in_rng(m: int, s: int, e: int) -> bool:
    return (s <= m < e) if s < e else (m >= s or m < e)


def sgn(x: float) -> int:
    return (x > 0) - (x < 0)


def ny_dt(t_ms: int) -> datetime:
    return datetime.fromtimestamp(t_ms / 1000, NY)


def ny_mins(t_ms: int) -> int:
    d = ny_dt(t_ms)
    return d.hour * 60 + d.minute


@dataclass
class Bar:
    t: int  # apertura, ms UTC
    o: float
    h: float
    l: float
    c: float
    v: float


class Atr:
    """ta.atr de Pine (RMA de True Range)."""

    def __init__(self, n: int):
        self.n, self.pc, self.v, self.buf = n, None, None, []

    def update(self, h, l, c):
        tr = h - l if self.pc is None else max(h - l, abs(h - self.pc), abs(l - self.pc))
        self.pc = c
        if self.v is None:
            self.buf.append(tr)
            if len(self.buf) == self.n:
                self.v = sum(self.buf) / self.n
        else:
            self.v = (self.v * (self.n - 1) + tr) / self.n
        return self.v


class Engine:
    def __init__(self, cfg):
        self.cfg = c = cfg
        self.p12S, self.lonS, self.p12E = hm(c.p12_start), hm(c.london_start), hm(c.p12_end)
        self.readE, self.openM, self.entE, self.exitM = hm(c.read_end), hm(c.open), hm(c.last_entry), hm(c.force_exit)
        self.atr_calc = Atr(c.atr_len)
        self.atr = None
        self.vols = deque(maxlen=288)
        self.prev_asia = False
        self.prev_dom = None
        self.widths, self.atrs = [], []
        self.closes = deque(maxlen=290)
        self.read_hist = []
        self.loss_streak = 0
        self.day_on = False
        self.mins = 0
        self.day_id = None
        self._reset_day(None)

    # ───────────── helpers ─────────────
    def _reset_day(self, day_id):
        self.day_id = day_id
        self.aO = self.aH = self.aL = self.aC = None
        self.lO = self.lH = self.lL = self.lC = None
        self.pH = self.pL = self.pM = None
        self.mo_px = None
        self.scen = 0
        self.ndir = 0
        self.rd = 0
        self.accH = self.accL = False
        self.cntH = self.cntL = 0
        self.vH = self.vL = 0.0
        self.mpH, self.mpL = [], []
        self.scen_done = self.read_done = self.open_done = self.eod_done = False
        self.bias = 0
        self.coinc = False
        self.sz_mult = 0.0
        self.traded = False
        self.dead = False
        self.rej_cost = self.rej_stop = False
        self.zEdge = self.zDeep = self.stopPx = None
        self.weekend = False
        self.wRatio = None
        self.wB = 1
        self.atrRatio = None
        self.atrB = 1
        self.mo_fav = False
        self.ev_day = False
        self.pause_today = False
        self.why = []
        self.read_vol = 0.0
        self.rvol_read = None
        self.mom_ret = None
        self.size_mult = 1.0

    def mark_traded(self):
        self.traded = True

    def scen_text(self):
        s = {1: "Mismo lado " + ("↑" if self.ndir == 1 else "↓"), 2: "Se contradicen", 3: "Ambos lados"}
        return s.get(self.scen, "—")

    def read_text(self):
        return {1: "ACEPTA ↑", -1: "ACEPTA ↓"}.get(self.rd, "sin aceptación")

    def calc_target(self, entry: float, side: int, r_unit: float) -> float:
        c = self.cfg
        tr = entry + side * c.rr * r_unit
        if c.tgt_mode == "fixed_r":
            return tr
        tx = self.zEdge + side * c.ext_k * (self.pH - self.pL)
        return tx if (tx - entry) * side > 0.5 * r_unit else tr

    # ───────────── barra ─────────────
    def on_bar(self, b: Bar, flat: bool = True):
        c = self.cfg
        atr = self.atr = self.atr_calc.update(b.h, b.l, b.c)
        self.vols.append(b.v)
        self.closes.append(b.c)
        dt = ny_dt(b.t)
        mins = self.mins = dt.hour * 60 + dt.minute
        in_asia = in_rng(mins, self.p12S, self.lonS)
        in_lon = in_rng(mins, self.lonS, self.p12E)
        in_p12 = in_asia or in_lon
        in_read = in_rng(mins, self.p12E, self.readE)
        new_day = in_asia and not self.prev_asia
        self.prev_asia = in_asia
        events, signal = [], None

        if self.day_on and self.open_done and not self.eod_done and new_day:
            self.eod_done = True
            self.pause_today = False

        if new_day:
            self.day_on = True
            self._reset_day(dt.strftime("%Y-%m-%d"))
            self.aO, self.aH, self.aL, self.aC = b.o, b.h, b.l, b.c
            self.pH, self.pL = b.h, b.l
        elif self.day_on:
            if self.mo_px is None and self.prev_dom is not None and dt.day != self.prev_dom:
                self.mo_px = b.o
            if in_asia:
                self.aH, self.aL, self.aC = max(self.aH, b.h), min(self.aL, b.l), b.c
            if in_lon:
                if self.lO is None:
                    self.lO, self.lH, self.lL = b.o, b.h, b.l
                else:
                    self.lH, self.lL = max(self.lH, b.h), min(self.lL, b.l)
                self.lC = b.c
            if in_p12:
                self.pH, self.pL = max(self.pH, b.h), min(self.pL, b.l)

            if in_read:
                self.read_vol += b.v
            if not self.scen_done and not in_p12:
                self._scenario(dt, atr)
            if self.scen_done and not self.read_done and in_read:
                self._read(b, mins)
            if self.scen_done and not self.read_done and not in_read:
                self.read_done = True
                self.rd = 0 if (self.accH and self.accL) else 1 if self.accH else -1 if self.accL else 0
            if self.read_done and not self.open_done and in_rng(mins, self.openM, self.p12S):
                self._open(b, dt, atr, events)
        self.prev_dom = dt.day

        # ── entradas ──
        wait_win = self.day_on and self.open_done and not self.eod_done and in_rng(mins, self.openM, self.entE)
        allowed = (self.bias != 0 and self.sz_mult > 0 and not self.traded
                   and not (c.skip_weekends and self.weekend) and not self.pause_today)
        if wait_win and allowed and flat and not self.dead:
            if (self.bias == 1 and b.c < self.stopPx) or (self.bias == -1 and b.c > self.stopPx):
                self.dead = True
                events.append(("dead", "Tesis rota: el precio cerró más allá del stop. Hoy no se opera."))
        if wait_win and allowed and flat and not self.dead:
            bi = self.bias
            sig = ((bi == 1 and b.l <= self.zEdge and b.l > self.stopPx and b.c > b.o and b.c > self.zDeep) or
                   (bi == -1 and b.h >= self.zEdge and b.h < self.stopPx and b.c < b.o and b.c < self.zDeep))
            if sig:
                ent_adj = b.c + bi * b.c * c.entry_slip / 100
                rU = abs(ent_adj - self.stopPx)
                if rU > 0:
                    cost_r = c.cost_rt / 100 * ent_adj / rU
                    stop_pct = rU / ent_adj * 100
                    if cost_r > c.max_cost_r:
                        if not self.rej_cost:
                            events.append(("reject", f"Señal descartada: coste {cost_r:.2f}R > máx {c.max_cost_r}R"))
                        self.rej_cost = True
                    elif stop_pct > c.max_stop_pct:
                        if not self.rej_stop:
                            events.append(("reject", f"Señal descartada: stop {stop_pct:.2f}% > máx {c.max_stop_pct}%"))
                        self.rej_stop = True
                    else:
                        signal = dict(side=bi, ref=b.c, entry_adj=ent_adj, stop=self.stopPx, r_unit=rU,
                                      sz_mult=self.sz_mult, zEdge=self.zEdge, zDeep=self.zDeep,
                                      tp=self.calc_target(ent_adj, bi, rU), day_id=self.day_id)
        return signal, events

    # ───────────── fases ─────────────
    def _scenario(self, dt, atr):
        c = self.cfg
        self.scen_done = True
        self.pM = (self.pH + self.pL) / 2
        a_dir = sgn(self.aC - self.aO)
        l_dir = 0 if self.lO is None else sgn(self.lC - self.lO)
        both = self.lO is not None and self.lH > self.aH and self.lL < self.aL
        self.scen = 0 if self.lO is None else 3 if both else 1 if (a_dir == l_dir and a_dir != 0) else 2
        self.ndir = a_dir if self.scen == 1 else 0
        w = self.pH - self.pL
        if len(self.widths) >= 5:
            med = statistics.median(self.widths)
            self.wRatio = w / med if med > 0 else None
        self.wB = 1 if self.wRatio is None else 0 if self.wRatio < c.w_narrow else 2 if self.wRatio > c.w_wide else 1
        if len(self.atrs) >= 5 and atr:
            med_a = statistics.median(self.atrs)
            self.atrRatio = atr / med_a if med_a > 0 else None
        self.atrB = (1 if self.atrRatio is None else 0 if self.atrRatio < c.vol_narrow
                     else 2 if self.atrRatio > c.vol_wide else 1)
        if not (c.skip_weekends and dt.isoweekday() >= 6):
            self.widths.append(w)
            self.widths = self.widths[-c.w_look:]
            if atr:
                self.atrs.append(atr)
                self.atrs = self.atrs[-c.w_look:]

    def _read(self, b, mins):
        c = self.cfg
        vol_avg = sum(self.vols) / len(self.vols)
        if c.accept_mode == "mp":
            if (mins + TF_MIN) % 30 == 0:
                vol_ok = c.rvol_min <= 0 or b.v >= c.rvol_min * vol_avg
                self.mpH.append(1 if (b.c > self.pH and vol_ok) else 0)
                self.mpL.append(1 if (b.c < self.pL and vol_ok) else 0)
                self.mpH, self.mpL = self.mpH[-c.mp_m:], self.mpL[-c.mp_m:]
                if sum(self.mpH) >= c.mp_n:
                    self.accH = True
                if sum(self.mpL) >= c.mp_n:
                    self.accL = True
        else:
            acc_bars = max(1, round(c.accept_min / TF_MIN))
            if min(b.o, b.c) > self.pH:
                self.cntH += 1
                self.vH += b.v
            else:
                self.cntH, self.vH = 0, 0.0
            if max(b.o, b.c) < self.pL:
                self.cntL += 1
                self.vL += b.v
            else:
                self.cntL, self.vL = 0, 0.0
            if self.cntH >= acc_bars and (c.rvol_min <= 0 or self.vH / self.cntH >= c.rvol_min * vol_avg):
                self.accH = True
            if self.cntL >= acc_bars and (c.rvol_min <= 0 or self.vL / self.cntL >= c.rvol_min * vol_avg):
                self.accL = True

    def _open(self, b, dt, atr, events):
        c = self.cfg
        self.open_done = True
        rd = self.rd
        opx = b.o
        self.weekend = dt.isoweekday() >= 6
        self.ev_day = dt.strftime("%Y-%m-%d") in c.event_dates
        self.mo_fav = False if self.mo_px is None else (rd == 1 and opx > self.mo_px) or (rd == -1 and opx < self.mo_px)
        why = []
        if rd == 0:
            why.append("sin aceptación clara")
        night_ok = (True if c.night_filter == "none" else self.scen != 3 if c.night_filter == "no_both"
                    else (self.scen == 1 and self.ndir == rd))
        if not night_ok:
            why.append("filtro noche")
        mo_ok = True if (c.mo_filter == "off" or self.mo_px is None) else (self.mo_fav if c.mo_filter == "with" else not self.mo_fav)
        if not mo_ok:
            why.append("filtro Midnight Open")
        w_ok = {"off": True, "no_narrow": self.wB != 0, "no_wide": self.wB != 2}.get(c.width_filter, self.wB == 1)
        if not w_ok:
            why.append("filtro ancho P12")
        ev_ok = {"off": True, "exclude": not self.ev_day}.get(c.event_filter, self.ev_day)
        if not ev_ok:
            why.append("día macro")
        vol_ok = {"off": True, "no_high": self.atrB != 2, "no_low": self.atrB != 0}.get(c.vol_filter, self.atrB == 1)
        if not vol_ok:
            why.append("filtro volatilidad")
        # volumen de la ventana de lectura vs mediana de días previos
        if len(self.read_hist) >= 5:
            med = statistics.median(self.read_hist)
            self.rvol_read = self.read_vol / med if med > 0 else None
        if not (c.skip_weekends and self.weekend):
            self.read_hist = (self.read_hist + [self.read_vol])[-c.w_look:]
        rv_ok = True
        self.size_mult = 1.0
        if c.rvol_read_filter == "min" and self.rvol_read is not None:
            rv_ok = self.rvol_read >= c.rvol_read_min
        elif c.rvol_read_filter == "size" and self.rvol_read is not None:
            self.size_mult = min(1.5, max(0.5, self.rvol_read))
        if not rv_ok:
            why.append(f"volumen lectura bajo (×{self.rvol_read:.2f})")
        # momentum 24h
        mom_ok = True
        if len(self.closes) == self.closes.maxlen:
            self.mom_ret = self.closes[-1] / self.closes[0] - 1
            if c.daily_mom != "off" and rd != 0:
                m = sgn(self.mom_ret)
                mom_ok = (m == rd) if c.daily_mom == "with" else (m == -rd)
        if not mom_ok:
            why.append("momentum 24h en contra")
        streak_ok = c.loss_action == "ignore" or self.loss_streak < c.loss_n
        if c.loss_action == "pause" and self.loss_streak >= c.loss_n:
            self.pause_today = True
            streak_ok = False
            why.append(f"pausa por racha ({self.loss_streak} pérdidas)")
        streak_mult = c.loss_reduce if (c.loss_action == "reduce" and self.loss_streak >= c.loss_n) else 1.0
        if self.weekend and c.skip_weekends:
            why.append("fin de semana")

        ok = rd != 0 and night_ok and mo_ok and w_ok and ev_ok and vol_ok and rv_ok and mom_ok and streak_ok and not self.pause_today
        self.bias = rd if ok else 0
        if self.bias == 1:
            self.zEdge, self.zDeep = self.pH, self.pM
            self.stopPx = self.pM - c.stop_buf * atr
            self.coinc = opx >= self.pM if c.coinc_mode == "mid" else opx > self.pH
        elif self.bias == -1:
            self.zEdge, self.zDeep = self.pL, self.pM
            self.stopPx = self.pM + c.stop_buf * atr
            self.coinc = opx <= self.pM if c.coinc_mode == "mid" else opx < self.pL
        self.sz_mult = 0.0 if self.bias == 0 else (1.0 if self.coinc else 0.0 if c.mismatch == "discard" else c.reduce_f) * streak_mult * self.size_mult
        self.why = why
        events.append(("open", None))

"""Ejecución y gestión de la posición: parcial, BE, trailing, time stop, cierre forzado, racha."""
from __future__ import annotations

import logging
import math
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from engine import Bar, in_rng, ny_mins, NY, BAR_MS

log = logging.getLogger("trader")


@dataclass
class Pos:
    side: int
    qty0: float
    qty: float
    entry: float
    r_unit: float
    sl: float
    tp: float
    entry_t: int
    day_id: str
    be_done: bool = False
    partial_done: bool = False
    trail_on: bool = False
    trail_stop: float | None = None
    realized: float = 0.0
    stop_ids: list = field(default_factory=list)


class Trader:
    def __init__(self, cfg, engine, broker, notify, store):
        self.cfg, self.eng, self.broker, self.n, self.store = cfg, engine, broker, notify, store
        self.pos: Pos | None = Pos(**store.d["pos"]) if store.d.get("pos") else None
        self.eng.loss_streak = store.d.get("loss_streak", 0)
        self.trades: list[float] = []
        self.trade_log: list[tuple] = []
        self.tag = "[PAPER] " if broker.paper else ("[DEMO] " if cfg.demo else "")

    # ───────────── persistencia ─────────────
    def _save(self):
        self.store.d["pos"] = asdict(self.pos) if self.pos else None
        self.store.d["loss_streak"] = self.eng.loss_streak
        self.store.save()

    # ───────────── barra ─────────────
    def on_bar(self, b: Bar, live: bool, fresh: bool = True):
        if live and self.pos and b.t > self.pos.entry_t:
            self._manage(b)
        sig, events = self.eng.on_bar(b, flat=self.pos is None)
        if not live:
            return
        if fresh:
            for kind, data in events:
                self._event(kind, data)
        if sig and fresh and self.pos is None:
            if self.store.d.get("paused"):
                self.n.send(f"⏸ {self.tag}Señal ignorada (bot en pausa): {'LARGO' if sig['side']==1 else 'CORTO'}")
            else:
                self._enter(sig, b)

    # ───────────── eventos del motor ─────────────
    def _event(self, kind, data):
        e, c = self.eng, self.cfg
        if kind == "open":
            if e.bias != 0 and not (c.skip_weekends and e.weekend):
                lim = datetime.now(NY).replace(hour=self.eng.entE // 60, minute=self.eng.entE % 60)
                lim_loc = lim.astimezone(ZoneInfo(c.tz_local)).strftime("%H:%M")
                extra = (f"Vol. lectura ×{e.rvol_read:.2f} · " if e.rvol_read is not None else "") + \
                        (f"mom 24h {e.mom_ret * 100:+.1f}%\n" if e.mom_ret is not None else "\n" if e.rvol_read is not None else "")
                self.n.send(
                    f"📊 <b>P12 · Lectura del día</b> {self.tag}\n{e.scen_text()} · {e.read_text()}\n"
                    f"{'🟢 BUSCA COMPRAS' if e.bias == 1 else '🔴 BUSCA VENTAS'}\n"
                    f"Zona: {e.zEdge:.2f} → {e.zDeep:.2f}\nSL: {e.stopPx:.2f}\n"
                    f"Apertura {'coincide' if e.coinc else 'NO coincide'} · tamaño ×{e.sz_mult:.2f}\n"
                    f"{extra}"
                    f"Entradas hasta {lim_loc} ({c.tz_local})")
            else:
                self.n.send(f"✋ <b>P12</b> Hoy no se opera: {', '.join(e.why) or 'sin condiciones'} · {e.scen_text()} · {e.read_text()}")
        else:
            self.n.send(f"ℹ️ P12: {data}")

    # ───────────── entrada ─────────────
    def _enter(self, s, b):
        c, br = self.cfg, self.broker
        try:
            eq = c.equity_override or br.equity()
            qty = eq * c.risk_pct / 100 / s["r_unit"] * s["sz_mult"]
            qty = min(qty, eq * c.leverage * 0.95 / s["entry_adj"])
            qty = br.round_qty(qty)
            if qty <= 0 or qty < br.min_qty():
                self.n.send(f"⚠️ {self.tag}Señal válida pero cantidad {qty} < mínimo del exchange ({br.min_qty()}). Sube el capital o el riesgo.")
                return
            side = s["side"]
            fill = br.open(side, qty, s["ref"])
            r_unit = abs(fill - s["stop"])
            if r_unit <= 0 or (fill - s["stop"]) * side <= 0:
                raise RuntimeError("El precio de entrada quedó más allá del stop")
            pos = Pos(side=side, qty0=qty, qty=qty, entry=fill, r_unit=r_unit, sl=s["stop"],
                      tp=self.eng.calc_target(fill, side, r_unit), entry_t=b.t, day_id=s["day_id"])
            self.pos = pos
            self.eng.mark_traded()
            self.store.d["traded_day"] = s["day_id"]
            try:
                br.set_stops(pos)
            except Exception as e:  # sin stop no se mantiene la posición
                log.exception("set_stops")
                br.close_all(pos, fill)
                self.pos = None
                self._save()
                self.n.send(f"🚨 {self.tag}No pude colocar SL/TP ({e}). Posición cerrada por seguridad.")
                return
            self._save()
            self.n.send(
                f"{'🟢 LARGO' if side == 1 else '🔴 CORTO'} {self.tag}<b>{c.symbol}</b>\n"
                f"Entrada: {fill:.2f}\nSL: {pos.sl:.2f}\nTP: {pos.tp:.2f}\n"
                f"Cantidad: {qty} · Riesgo {c.risk_pct * s['sz_mult']:.2f}% · R = {r_unit:.2f}")
        except Exception as e:
            log.exception("enter")
            self.n.send(f"🚨 {self.tag}Error al entrar: {e}")

    # ───────────── gestión ─────────────
    def _manage(self, b: Bar):
        c, br, p = self.cfg, self.broker, self.pos
        res = br.check_exit(p, b)
        if res:
            self._close(*res, t=b.t + BAR_MS)
            return
        d = p.side
        fav = (b.h - p.entry) if d == 1 else (p.entry - b.l)
        cur_r = fav / p.r_unit
        changed = False

        if c.be_r > 0 and not p.be_done and fav >= c.be_r * p.r_unit:
            if (p.entry - p.sl) * d > 0:
                p.sl = p.entry
            p.be_done = changed = True
            self.n.send(f"🔒 {self.tag}Breakeven activado")

        if c.partial_r > 0 and not p.partial_done and fav >= c.partial_r * p.r_unit:
            q = br.round_qty(p.qty * c.partial_pct / 100)
            if 0 < q < p.qty:
                px = br.reduce(p, q, b.c)
                p.realized += d * (px - p.entry) * q
                p.qty = round(p.qty - q, 8)
                changed = True
                self.n.send(f"💰 {self.tag}Parcial {c.partial_pct:.0f}% a +{c.partial_r}R @ {px:.2f}")
            p.partial_done = True

        if c.trail_act_r > 0 and fav >= c.trail_act_r * p.r_unit and self.eng.atr:
            if c.trail_mode == "atr":
                new = b.h - c.trail_atr * self.eng.atr if d == 1 else b.l + c.trail_atr * self.eng.atr
            else:
                new = p.entry + d * (cur_r - c.trail_r) * p.r_unit
            p.trail_stop = new if p.trail_stop is None else (max(p.trail_stop, new) if d == 1 else min(p.trail_stop, new))
            better = max(p.sl, p.trail_stop) if d == 1 else min(p.sl, p.trail_stop)  # nunca aflojar el SL
            if better != p.sl:
                p.sl = better
                p.trail_on = changed = True

        if changed:
            try:
                br.set_stops(p)
            except Exception as e:
                log.exception("set_stops")
                self.n.send(f"🚨 {self.tag}Fallo al mover SL/TP: {e}. Cierro por seguridad.")
                self._force("Error stops", b)
                return
            self._save()

        hours = (b.t - p.entry_t) / 3_600_000
        if c.max_hours > 0 and hours >= c.max_hours:
            self._force("Time Stop", b)
        elif in_rng(ny_mins(b.t), self.eng.exitM, self.eng.p12S):
            self._force("Cierre NY", b)

    def _force(self, why, b):
        px = self.broker.close_all(self.pos, b.c)
        self._close(px, why, t=b.t + BAR_MS)

    def _close(self, px, reason, t=None):
        p, c = self.pos, self.cfg
        t = t or int(time.time() * 1000)
        pnl_px = p.realized + p.side * (px - p.entry) * p.qty
        r = pnl_px / (p.qty0 * p.r_unit) - c.cost_rt / 100 * p.entry / p.r_unit
        # funding: periodos cruzados entre entrada y salida
        h = c.fund_h * 3_600_000
        n_fund = max(0, math.floor(t / h) - math.floor(p.entry_t / h))
        r -= n_fund * c.fund_pct / 100 * p.entry / p.r_unit
        self.broker.record_pnl(r * p.r_unit * p.qty0) if hasattr(self.broker, "record_pnl") else None
        self.eng.loss_streak = 0 if r > 0 else self.eng.loss_streak + 1
        self.trades.append(r)
        self.trade_log.append((p.day_id, r, reason))
        d = self.store.d
        d["eq_r"] = d.get("eq_r", 0.0) + r
        d["peak_r"] = max(d.get("peak_r", 0.0), d["eq_r"])
        d["n"] = d.get("n", 0) + 1
        d["wins"] = d.get("wins", 0) + (1 if r > 0 else 0)
        d["sum_win"] = d.get("sum_win", 0.0) + max(r, 0)
        d["sum_loss"] = d.get("sum_loss", 0.0) + max(-r, 0)
        d["max_dd"] = max(d.get("max_dd", 0.0), d["peak_r"] - d["eq_r"])
        d["trades"] = (d.setdefault("trades", []) + [{"day": p.day_id, "side": p.side, "r": round(r, 3), "why": reason}])[-200:]
        self.pos = None
        self._save()
        self.n.send(f"{'✅' if r > 0 else '❌'} {self.tag}Cierre <b>{reason}</b> @ {px:.2f} · {r:+.2f}R"
                    f"{f' (funding {n_fund}×)' if n_fund else ''}\n"
                    f"Racha pérdidas: {self.eng.loss_streak} · Acumulado: {d['eq_r']:+.2f}R · DD: {d['peak_r'] - d['eq_r']:.1f}R")
        dd = d["peak_r"] - d["eq_r"]
        if c.max_dd_r > 0 and dd >= c.max_dd_r and not d.get("paused"):
            d["paused"] = True
            self.store.save()
            self.n.send(f"🛑 <b>FRENO DE SEGURIDAD</b>: drawdown {dd:.1f}R ≥ {c.max_dd_r}R. Bot en pausa. "
                        f"Revisa la estrategia y usa /resume cuando quieras continuar (reinicia el contador con /resetdd).")

    def reset_dd(self):
        d = self.store.d
        d["peak_r"] = d["eq_r"] = 0.0
        self.store.save()
        return "Contador de drawdown reiniciado."

    def stats(self):
        d = self.store.d
        n = d.get("n", 0)
        if not n:
            return "Aún no hay operaciones cerradas."
        pf = d["sum_win"] / d["sum_loss"] if d.get("sum_loss") else float("inf")
        last = " ".join(("✅" if x["r"] > 0 else "❌") for x in d.get("trades", [])[-10:])
        return (f"<b>Estadísticas</b>\nOps: {n} · acierto {100 * d['wins'] / n:.0f}%\n"
                f"Expectativa: {d['eq_r'] / n:+.2f}R · total {d['eq_r']:+.1f}R\nPF {pf:.2f} · DD máx {d['max_dd']:.1f}R\nÚltimas: {last}")

    # ───────────── entre velas ─────────────
    def poll(self):
        """Detecta cierres por SL/TP en el exchange sin esperar a la vela (solo modo real)."""
        if self.broker.paper or not self.pos:
            return
        res = self.broker.poll_exit(self.pos)
        if res:
            self._close(*res)

    def reconcile(self):
        """Al arrancar: compara estado guardado con el exchange."""
        if self.broker.paper:
            return
        sides = [s for s in (1, -1) if self.broker.has_position(s)]
        if self.pos and not sides:
            res = self.broker.poll_exit(self.pos)
            if res:
                self.n.send("ℹ️ La posición se cerró mientras el bot estaba apagado.")
                self._close(*res)
        elif sides and not self.pos:
            self.n.send("⚠️ Hay una posición abierta en BingX que el bot NO conoce (sin estado guardado). "
                        "No la gestionaré. Revisa SL/TP a mano. ¿Montaste el Volume en /data?")

    def diag(self):
        e, c, br = self.eng, self.cfg, self.broker
        s = [f"<b>Diagnóstico</b>",
             f"DRY_RUN={c.dry_run} · DEMO={c.demo} → {'⚠️ SOLO SIMULA, NO OPERA EN BINGX' if br.paper else 'envía órdenes a BingX'}",
             f"Pausa manual/freno: {self.store.d.get('paused', False)} · símbolo {c.symbol} · apal. {c.leverage}x"]
        if not br.paper:
            d = br.diag()
            s.append(f"Equity: {d['equity']} · cantidad mínima: {d['min_qty']} · modo hedge: {d['hedge']}")
        s.append(f"Día {e.day_id}: lectura {'hecha' if e.read_done else 'pendiente'} · apertura {'hecha' if e.open_done else 'pendiente'}")
        if e.open_done:
            s.append(f"Sesgo: {e.bias} · {e.read_text()} · {e.scen_text()}")
            if e.why:
                s.append("Motivo de no operar: " + ", ".join(e.why))
            if e.bias:
                s.append(f"Zona {e.zEdge:.2f}→{e.zDeep:.2f} · SL {e.stopPx:.2f} · tesis rota: {e.dead} · ya operó hoy: {e.traded}")
                s.append(f"Rechazos: coste={e.rej_cost} stop%={e.rej_stop} · tamaño ×{e.sz_mult:.2f}")
        s.append("Ventana de entrada: 09:30–12:00 Nueva York.")
        return "\n".join(s)

    def test_order(self):
        """Abre la cantidad mínima, coloca SL/TP lejanos y cierra. Prueba la ruta real de BingX."""
        br, c = self.broker, self.cfg
        if br.paper:
            return "Estás en DRY_RUN=true: no hay conexión de órdenes. Pon DRY_RUN=false."
        if self.pos:
            return "Hay una posición abierta; ciérrala antes."
        log_ = []
        pos = None
        try:
            px = float(br.ex.fetch_ticker(c.symbol)["last"])
            q = br.round_qty(max(br.min_qty(), 0.0001))
            log_.append(f"1) Precio {px} · qty {q}")
            fill = br.open(1, q, px)
            log_.append(f"2) Compra OK @ {fill}")
            pos = Pos(side=1, qty0=q, qty=q, entry=fill, r_unit=fill * 0.05, sl=fill * 0.95, tp=fill * 1.05,
                      entry_t=int(time.time() * 1000), day_id="test")
            br.set_stops(pos)
            log_.append(f"3) SL/TP OK (ids {pos.stop_ids})")
        except Exception as e:
            log_.append(f"❌ FALLO: {type(e).__name__}: {str(e)[:300]}")
        finally:
            if pos:
                try:
                    br.close_all(pos, pos.entry)
                    log_.append("4) Cerrada y órdenes canceladas ✅")
                except Exception as e:
                    log_.append(f"🚨 NO pude cerrar: {e}. CIERRA A MANO en BingX.")
        return "<b>Test de orden</b>\n" + "\n".join(log_)

    def force_close(self, b_price):
        if not self.pos:
            return "No hay posición abierta."
        px = self.broker.close_all(self.pos, b_price)
        self._close(px, "Manual")
        return "Posición cerrada."

    def status(self):
        e, c = self.eng, self.cfg
        mode = "PAPER (solo señales)" if self.broker.paper else ("DEMO" if c.demo else "REAL")
        s = [f"<b>P12 Hunter</b> · {c.symbol} · modo {mode}"]
        s.append(f"Pausa manual: {'SÍ' if self.store.d.get('paused') else 'no'} · racha pérdidas: {e.loss_streak}")
        if e.open_done:
            s.append(f"Hoy: {e.scen_text()} · {e.read_text()} · sesgo {'COMPRAS' if e.bias==1 else 'VENTAS' if e.bias==-1 else 'ninguno'}"
                     + (" · tesis rota" if e.dead else "") + (" · operación hecha" if e.traded else ""))
        elif e.read_done:
            s.append("Lectura hecha, esperando apertura")
        else:
            s.append("Lectura en curso / fuera de sesión")
        if self.pos:
            p = self.pos
            s.append(f"Posición: {'LARGO' if p.side==1 else 'CORTO'} {p.qty} @ {p.entry:.2f} · SL {p.sl:.2f} · TP {p.tp:.2f}"
                     + (" · trailing" if p.trail_on else ""))
        if self.trades:
            s.append(f"Esta sesión: {len(self.trades)} ops · {sum(self.trades):+.2f}R")
        return "\n".join(s)

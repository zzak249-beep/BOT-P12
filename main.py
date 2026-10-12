"""P12 Hunter bot — BingX multi-moneda + Telegram. Punto de entrada para Railway."""
import logging
import time
from datetime import datetime
from types import SimpleNamespace

from config import Config
from engine import Engine, BAR_MS, NY
from notifier import Notifier
from state import Store
from trader import Trader, Book, fp

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")

MIN_BARS = 3 * 288          # mínimo 3 días de historial para operar un símbolo
MIN_COVERAGE = 0.97         # <97% de velas 5m presentes ⇒ no es 24/7 (acciones/materias primas tokenizadas)


def norm_symbol(x):
    """Acepta BTC, BTCUSDT, BTC-USDT, BTC/USDT, BTCUSDT.P o el formato ccxt BTC/USDT:USDT."""
    x = x.strip().upper().replace(".P", "")
    if not x or x.startswith("TOP:") or x == "ALL":
        return x
    if "/" in x and ":" in x:
        return x
    for sep in ("-", "/", "_"):
        if sep in x:
            base, quote = x.split(sep)[:2]
            return f"{base}/{quote}:{quote}"
    for q in ("USDT", "USDC"):
        if x.endswith(q) and len(x) > len(q):
            return f"{x[:-len(q)]}/{q}:{q}"
    return f"{x}/USDT:USDT"


def select_symbols(cfg, ex):
    spec = cfg.symbols or cfg.symbol
    up = spec.upper()
    if up != "ALL" and not up.startswith("TOP:"):
        return [norm_symbol(x) for x in spec.split(",") if x.strip()]
    try:
        tick = ex.fetch_tickers()
    except Exception as e:
        log.warning("fetch_tickers falló (%s): sin filtro de volumen", e)
        tick = {}
    no_vol = not any((v or {}).get("quoteVolume") for v in tick.values())
    rows = []
    for s, m in ex.markets.items():
        if not (m.get("swap") and m.get("linear") and m.get("quote") == "USDT" and m.get("active") is not False):
            continue
        if (m.get("base") or "").upper() in cfg.exclude:
            continue
        q = float((tick.get(s) or {}).get("quoteVolume") or 0)
        if not no_vol and q < cfg.min_volume:
            continue
        rows.append((q, s))
    rows.sort(reverse=True)
    n = int(up.split(":")[1]) if up.startswith("TOP:") else cfg.max_symbols
    return [s for _, s in rows[:n]]


class Bot:
    def __init__(self, cfg, ex, mk_broker, notify, store, fetch):
        self.cfg, self.ex, self.mk_broker, self.n, self.store, self.fetch = cfg, ex, mk_broker, notify, store, fetch
        self.book = Book(cfg, store)
        self.U = {}
        self.pending = []
        self.skipped = {}
        self.last_refresh = time.time()
        self.multi = False
        self.last_progress = time.time()
        self.last_warn = 0

    # ── alta de símbolos ──
    def add(self, sym, now, warm_days):
        bars = [b for b in self.fetch(self.ex, sym, now - warm_days * 86_400_000) if b.t + BAR_MS <= now]
        has_pos = sym in self.store.d["positions"]
        if len(bars) < MIN_BARS and not has_pos:
            self.skipped[sym] = "poco historial"
            return False
        expected = (bars[-1].t - bars[0].t) // BAR_MS + 1
        if len(bars) / expected < MIN_COVERAGE and not has_pos:
            self.skipped[sym] = "no opera 24/7"
            return False
        eng = Engine(self.cfg)
        tr = Trader(self.cfg, eng, self.mk_broker(sym), self.n, self.store, sym=sym, book=self.book, quiet=self.multi)
        for b in bars:
            tr.on_bar(b, live=False)
        eng.traded = self.store.d.get("traded_day", {}).get(sym) == eng.day_id and eng.day_id is not None
        self.U[sym] = SimpleNamespace(sym=sym, eng=eng, tr=tr, last_t=bars[-1].t)
        return True

    def setup(self, now=None):
        now = now or int(time.time() * 1000)
        self.summary_day = None
        syms = select_symbols(self.cfg, self.ex)
        for s in self.store.d["positions"]:           # las posiciones abiertas se gestionan siempre
            if s not in syms:
                syms.insert(0, s)
        for s in [s for s in syms if s not in self.ex.markets]:
            self.skipped[s] = "no existe en BingX"
            log.error("Símbolo '%s' no existe en BingX perpetuos (¿SYMBOLS bien escrito?)", s)
        syms = [s for s in syms if s in self.ex.markets]
        self.multi = len(syms) > 1
        warm = self.cfg.warmup_days if len(syms) <= 10 else min(self.cfg.warmup_days, 10)
        self.n.send(f"⏳ Calentando {len(syms)} símbolo(s) con {warm} días de velas…")
        for i, s in enumerate(syms):
            try:
                self.add(s, now, warm)
            except Exception as e:
                self.skipped[s] = f"error: {str(e)[:60]}"
                log.warning("add %s: %s", s, e)
            if (i + 1) % 10 == 0:
                log.info("calentados %d/%d", i + 1, len(syms))
        self.warm = warm
        if not self.U:
            raise SystemExit("Ningún símbolo válido")
        if not self.cfg.dry_run:
            try:
                ex_syms = {p["symbol"] for p in self.ex.fetch_positions() if float(p.get("contracts") or 0) > 0}
            except Exception as e:
                log.warning("fetch_positions: %s", e)
                ex_syms = set(self.store.d["positions"])
            for u in self.U.values():
                u.tr.reconcile(ex_syms)
            unknown = ex_syms - set(self.store.d["positions"]) - set()
            if unknown:
                self.n.send("⚠️ Posiciones en BingX que el bot NO conoce (no las gestionaré): " + ", ".join(sorted(unknown)))
        self.n.send(f"✅ {len(self.U)} símbolos activos · {len(self.skipped)} descartados (24/7, historial o error)")

    def refresh(self):
        """Una vez al día: añade listados nuevos (con calentamiento escalonado)."""
        self.last_refresh = time.time()
        try:
            self.pending = [s for s in select_symbols(self.cfg, self.ex) if s not in self.U and s not in self.pending]
        except Exception as e:
            log.warning("refresh: %s", e)

    # ── bucle ──
    def tick(self, now):
        d = datetime.fromtimestamp(now / 1000, NY)
        if 580 <= d.hour * 60 + d.minute < 720 and getattr(self, 'summary_day', None) != d.date():
            self.summary_day = d.date()
            self.summary(d)
        due = [u for u in self.U.values() if now >= u.last_t + 2 * BAR_MS + 3000]

        def prio(u):
            e = u.eng
            return 0 if u.tr.pos else 1 if (e.bias != 0 and e.open_done and not e.traded and not e.eod_done) else 2
        due.sort(key=prio)
        for u in due:
            try:
                rows = self.fetch(self.ex, u.sym, u.last_t + 1)
                for b in [x for x in rows if x.t > u.last_t and x.t + BAR_MS <= now - 1000]:
                    u.tr.on_bar(b, live=True, fresh=now - (b.t + BAR_MS) < 180_000)
                    u.last_t = b.t
                    self.last_progress = time.time()
            except Exception as e:
                log.warning("tick %s: %s", u.sym, e)
        if time.time() - self.last_progress > 900 and time.time() - self.last_warn > 3600:
            self.last_warn = time.time()
            self.n.send("⚠️ Hace más de 15 min que no llegan velas nuevas de BingX. Revisa los logs de Railway.")
        for u in list(self.U.values()):
            if u.tr.pos:
                u.tr.poll()
        for _ in range(2):  # altas pendientes, pocas por vuelta para no bloquear la gestión
            if self.pending:
                s = self.pending.pop(0)
                try:
                    self.add(s, now, self.warm)
                except Exception as e:
                    self.skipped[s] = f"error: {str(e)[:60]}"
        if time.time() - self.last_refresh > 86_400 and self.multi:
            self.refresh()

    def summary(self, d):
        c = self.cfg
        if c.skip_weekends and d.isoweekday() >= 6:
            self.n.send("📊 <b>P12</b> Fin de semana: no se opera.")
            return
        rd = [u for u in self.U.values() if u.eng.open_done]
        cands = [u for u in rd if u.eng.bias != 0]
        no_acc = sum(1 for u in rd if u.eng.rd == 0)
        filt = sum(1 for u in rd if u.eng.rd != 0 and u.eng.bias == 0)
        tags = " ".join(f"{u.tr.name}{'▲' if u.eng.bias == 1 else '▼'}" for u in cands[:30])
        self.n.send(f"📊 <b>P12 · resumen 09:40 NY</b>\n{len(rd)}/{len(self.U)} monedas leídas · "
                    f"<b>{len(cands)}</b> con operación posible{': ' + tags if tags else ''}\n"
                    f"{no_acc} sin aceptación · {filt} filtradas\nEntradas hasta 12:00 NY")

    # ── comandos ──
    def find(self, arg):
        a = (arg or "").strip().upper()
        for s, u in self.U.items():
            if a and (a == s.upper() or a == s.split("/")[0].upper()):
                return u
        return None

    def open_positions(self):
        return [u for u in self.U.values() if u.tr.pos]

    def g_status(self, arg=""):
        u = self.find(arg)
        if u:
            return u.tr.status()
        c = self.cfg
        mode = "PAPER (solo señales)" if c.dry_run else ("DEMO" if c.demo else "REAL")
        d = self.store.d
        pos = self.open_positions()
        cands = [x.sym.split("/")[0] for x in self.U.values()
                 if x.eng.open_done and x.eng.bias != 0 and not x.eng.traded and not x.eng.dead and not x.eng.eod_done]
        s = [f"<b>P12 Hunter</b> · modo {mode} · {len(self.U)} símbolos",
             f"Pausa: {'SÍ' if d.get('paused') else 'no'} · racha pérdidas: {d.get('loss_streak', 0)} · "
             f"acumulado {d.get('eq_r', 0):+.1f}R"]
        if pos:
            s.append(f"<b>Abiertas ({len(pos)}/{c.max_open}):</b>")
            for x in pos:
                p = x.tr.pos
                s.append(f"{'▲' if p.side == 1 else '▼'} {x.tr.name} @ {fp(p.entry)} · SL {fp(p.sl)} · TP {fp(p.tp)}" + (" · trail" if p.trail_on else ""))
        else:
            s.append("Sin posiciones abiertas")
        if cands:
            s.append(f"Candidatos hoy ({len(cands)}): " + ", ".join(cands[:25]) + ("…" if len(cands) > 25 else ""))
        return "\n".join(s)

    def g_diag(self, arg=""):
        u = self.find(arg)
        if u:
            return u.tr.diag()
        c = self.cfg
        newest = max(u.last_t for u in self.U.values())
        age = int(time.time() - (newest + BAR_MS) / 1000)
        s = [f"<b>Diagnóstico global</b>",
             f"Última vela procesada: {datetime.fromtimestamp(newest / 1000, NY).strftime('%d/%m %H:%M')} NY (hace {age // 60} min)",
             f"DRY_RUN={c.dry_run} · DEMO={c.demo} → {'⚠️ SOLO SIMULA, NO OPERA EN BINGX' if c.dry_run else 'envía órdenes a BingX'}",
             f"Símbolos activos {len(self.U)} · descartados {len(self.skipped)} · pausa {self.store.d.get('paused', False)}",
             f"Límites: {c.max_open} posiciones · riesgo total {c.max_total_risk}% · riesgo/op {c.risk_pct}% · apal. {c.leverage}x"]
        if not c.dry_run:
            try:
                u0 = next(iter(self.U.values()))
                d = u0.tr.broker.diag()
                s.append(f"Equity: {d['equity']} · modo hedge: {d['hedge']}")
            except Exception as e:
                s.append(f"Equity/hedge: error {e}")
        reasons = {}
        for r in self.skipped.values():
            reasons[r.split(":")[0]] = reasons.get(r.split(":")[0], 0) + 1
        if reasons:
            s.append("Descartados: " + ", ".join(f"{k} {v}" for k, v in reasons.items()))
        s.append("Detalle de una moneda: /diag BTC")
        return "\n".join(s)

    def g_pause(self, arg=""):
        self.store.d["paused"] = True; self.store.save()
        return "⏸ Pausado: no abriré nuevas operaciones (las abiertas siguen gestionadas)."

    def g_resume(self, arg=""):
        self.store.d["paused"] = False; self.store.save()
        return "▶️ Reanudado."

    def g_close(self, arg=""):
        a = (arg or "").strip()
        targets = self.open_positions() if a.lower() in ("", "all", "todo") else [self.find(a)]
        out = []
        for u in targets:
            if not u:
                return "Símbolo no encontrado."
            try:
                out.append(f"{u.tr.name}: " + u.tr.force_close(float(self.ex.fetch_ticker(u.sym)["last"])))
            except Exception as e:
                out.append(f"{u.sym}: error {e}")
        return "\n".join(out) or "No hay posiciones abiertas."

    def g_test(self, arg=""):
        u = self.find(arg) or self.find("BTC") or next(iter(self.U.values()))
        return u.tr.test_order()

    def g_symbols(self, arg=""):
        names = [s.split("/")[0] for s in self.U]
        return f"{len(names)} activos: " + ", ".join(names[:80]) + ("…" if len(names) > 80 else "")


def main():
    cfg = Config()
    notify = Notifier(cfg.tg_token, cfg.tg_chat)
    from exchange import fetch_candles
    if cfg.dry_run:
        from exchange import public_exchange
        from paper import PaperBroker
        ex = public_exchange(cfg)
        paper = PaperBroker(cfg)
        mk = lambda sym: paper
    else:
        from exchange import BingxBroker, make_exchange
        if not cfg.api_key or not cfg.api_secret:
            raise SystemExit("Faltan BINGX_API_KEY / BINGX_API_SECRET (o pon DRY_RUN=true)")
        ex = make_exchange(cfg)
        mk = lambda sym: BingxBroker(cfg, sym, ex)

    err = notify.check()
    if err:
        log.error("TELEGRAM NO FUNCIONA: %s", err)
    store = Store(cfg.state_path)
    store.d.setdefault("positions", {})
    bot = Bot(cfg, ex, mk, notify, store, fetch_candles)
    try:
        bot.setup()
    except BaseException as e:
        log.exception("fallo al arrancar")
        notify.send(f"🚨 El bot no pudo arrancar: {type(e).__name__}: {str(e)[:300]}")
        raise

    any_tr = next(iter(bot.U.values())).tr
    notify.start_commands({
        "/status": bot.g_status, "/diag": bot.g_diag, "/pause": bot.g_pause, "/resume": bot.g_resume,
        "/close": bot.g_close, "/testorder": bot.g_test, "/symbols": bot.g_symbols,
        "/stats": lambda a="": any_tr.stats(), "/resetdd": lambda a="": any_tr.reset_dd(),
        "/ping": lambda a="": "pong ✅ el bot está vivo",
        "/help": lambda a="": "/ping · /status [BTC] · /diag [BTC] · /stats · /symbols · /pause · /resume · /close [BTC|all] · /testorder [BTC] · /resetdd"})
    notify.send(f"🤖 <b>P12 Hunter v4.6</b> iniciado · {len(bot.U)} símbolos · "
                f"{'PAPER (solo señales)' if cfg.dry_run else 'DEMO' if cfg.demo else 'REAL'}\n/help para comandos")

    last_err = 0
    while True:
        try:
            bot.tick(int(time.time() * 1000))
        except Exception as e:
            log.exception("loop")
            if time.time() - last_err > 900:
                notify.send(f"⚠️ Error en el bucle: {e}")
                last_err = time.time()
        time.sleep(10)


if __name__ == "__main__":
    main()

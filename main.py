"""P12 Hunter bot — BingX + Telegram. Punto de entrada para Railway."""
import logging
import time

from config import Config
from engine import Engine, BAR_MS
from notifier import Notifier
from state import Store
from trader import Trader

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")


def main():
    cfg = Config()
    notify = Notifier(cfg.tg_token, cfg.tg_chat)
    if cfg.dry_run:
        from exchange import public_exchange
        from paper import PaperBroker
        broker, ex = PaperBroker(cfg), public_exchange(cfg)
    else:
        from exchange import BingxBroker
        if not cfg.api_key or not cfg.api_secret:
            raise SystemExit("Faltan BINGX_API_KEY / BINGX_API_SECRET (o pon DRY_RUN=true)")
        broker = BingxBroker(cfg)
        ex = broker.ex
    from exchange import fetch_candles

    store = Store(cfg.state_path)
    engine = Engine(cfg)
    trader = Trader(cfg, engine, broker, notify, store)

    # ── calentamiento: reconstruye anchos P12, ATR y estado del día ──
    now = int(time.time() * 1000)
    bars = [b for b in fetch_candles(ex, cfg.symbol, now - cfg.warmup_days * 86_400_000) if b.t + BAR_MS <= now]
    if not bars:
        raise SystemExit("No se pudieron descargar velas")
    for b in bars:
        trader.on_bar(b, live=False)
    last_t = bars[-1].t
    engine.traded = store.d.get("traded_day") == engine.day_id and engine.day_id is not None
    trader.reconcile()
    log.info("Calentamiento OK: %d velas, última %s", len(bars), last_t)

    def h_pause():
        store.d["paused"] = True; store.save(); return "⏸ Pausado: no abriré nuevas operaciones."
    def h_resume():
        store.d["paused"] = False; store.save(); return "▶️ Reanudado."
    def h_close():
        return trader.force_close(float(ex.fetch_ticker(cfg.symbol)["last"]))
    notify.start_commands({"/status": trader.status, "/pause": h_pause, "/resume": h_resume, "/close": h_close,
                           "/stats": trader.stats, "/diag": trader.diag, "/testorder": trader.test_order, "/resetdd": trader.reset_dd,
                           "/help": lambda: "/diag · /testorder · /status · /stats · /pause · /resume · /close · /resetdd"})
    notify.send(f"🤖 <b>P12 Hunter v4.6</b> iniciado · {cfg.symbol} · "
                f"{'PAPER (solo señales)' if cfg.dry_run else 'DEMO' if cfg.demo else 'REAL'}\n/help para comandos")

    last_err = 0
    while True:
        try:
            now = int(time.time() * 1000)
            if now >= last_t + 2 * BAR_MS + 3000:
                rows = fetch_candles(ex, cfg.symbol, last_t + 1)
                for b in [x for x in rows if x.t > last_t and x.t + BAR_MS <= now - 1000]:
                    fresh = now - (b.t + BAR_MS) < 120_000  # no operar velas viejas tras una caída
                    trader.on_bar(b, live=True, fresh=fresh)
                    last_t = b.t
            trader.poll()
        except Exception as e:
            log.exception("loop")
            if time.time() - last_err > 900:
                notify.send(f"⚠️ Error en el bucle: {e}")
                last_err = time.time()
        time.sleep(10)


if __name__ == "__main__":
    main()

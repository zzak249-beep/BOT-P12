"""Compara variantes de filtros con el mismo motor, con muestra de entrenamiento (70%) y fuera de muestra (30%).
   python compare.py --days 365            (velas reales de BingX, públicas)
   python compare.py --synthetic --days 400 (solo para probar que corre)
Cuantas más variantes pruebas, más fácil es 'encontrar' algo por azar: el umbral t se ajusta (Bonferroni)."""
import argparse
import math
import os
import statistics
import time

from backtest import synthetic
from config import Config
from engine import Engine, BAR_MS
from notifier import NullNotifier
from paper import PaperBroker
from state import Store
from trader import Trader

VARIANTS = [
    ("base", {}),
    ("rvol_accept>=1.0", {"RVOL_MIN": "1.0"}),
    ("rvol_accept>=1.5", {"RVOL_MIN": "1.5"}),
    ("vol_lectura min0.8", {"RVOL_READ_FILTER": "min", "RVOL_READ_MIN": "0.8"}),
    ("vol_lectura min1.0", {"RVOL_READ_FILTER": "min", "RVOL_READ_MIN": "1.0"}),
    ("vol_lectura size", {"RVOL_READ_FILTER": "size"}),
    ("mom24h a favor", {"DAILY_MOM": "with"}),
]


def run(bars, env):
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        cfg = Config()
    finally:
        for k, v in old.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
    tr = Trader(cfg, Engine(cfg), PaperBroker(cfg), NullNotifier(), Store(None))
    for b in bars:
        tr.on_bar(b, live=True, fresh=True)
    return tr.trade_log


def stats(rs):
    n = len(rs)
    if n == 0:
        return "  0", "   —  ", "  —  ", "  —  ", "  —  "
    w = sum(x for x in rs if x > 0)
    l = sum(-x for x in rs if x <= 0)
    eq = pk = dd = 0.0
    for x in rs:
        eq += x; pk = max(pk, eq); dd = max(dd, pk - eq)
    m = sum(rs) / n
    t = m / (statistics.stdev(rs) / math.sqrt(n)) if n > 2 and statistics.stdev(rs) > 0 else 0.0
    return f"{n:>3}", f"{m:+.2f}R", f"{(w / l if l else float('inf')):.2f}", f"{dd:.1f}R", f"{t:+.1f}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--synthetic", action="store_true")
    a = ap.parse_args()
    if a.synthetic:
        bars = synthetic(a.days)
    else:
        from exchange import public_exchange, fetch_candles
        cfg = Config()
        bars = fetch_candles(public_exchange(cfg), cfg.symbol, int(time.time() * 1000) - a.days * 86_400_000)[:-1]
    cut_t = bars[int(len(bars) * 0.7)].t
    cut_day = time.strftime("%Y-%m-%d", time.gmtime(cut_t / 1000))
    k = len(VARIANTS)
    zc = statistics.NormalDist().inv_cdf(1 - 0.025 / k)
    print(f"Velas {len(bars)} · corte train/test {cut_day} · {k} variantes → |t| necesario ≈ {zc:.1f} (Bonferroni)\n")
    print(f"{'variante':<22}{'n':>4} {'E':>7} {'PF':>6} {'DD':>6} {'t':>5} | TEST: {'n':>3} {'E':>7} {'t':>5}")
    for name, env in VARIANTS:
        log = run(bars, env)
        allr = [x for _, x, _ in log]
        te = [x for d, x, _ in log if d >= cut_day]
        n, m, pf, dd, t = stats(allr)
        n2, m2, _, _, t2 = stats(te)
        print(f"{name:<22}{n:>4} {m:>7} {pf:>6} {dd:>6} {t:>5} | {n2:>8} {m2:>7} {t2:>5}")
    print("\nLectura: un filtro solo merece la pena si mejora en TRAIN y TEST, con |t| sobre el umbral y n suficiente (≥30).")


if __name__ == "__main__":
    main()

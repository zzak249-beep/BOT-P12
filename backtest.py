"""Backtest/validación offline con el mismo motor.  python backtest.py --days 90   |   --synthetic"""
import argparse
import math
import random
import time

from config import Config
from engine import Bar, Engine, BAR_MS
from notifier import NullNotifier
from paper import PaperBroker
from state import Store
from trader import Trader


def synthetic(days, seed=1):
    random.seed(seed)
    t0 = (int(time.time()) // 86400 - days) * 86400 * 1000
    px, out = 60000.0, []
    for i in range(days * 288):
        t = t0 + i * BAR_MS
        hour = (t // 3_600_000) % 24
        vol = 0.0009 * (1.8 if 13 <= hour <= 20 else 1.0)
        o = px
        c = o * (1 + random.gauss(0, vol))
        h, l = max(o, c) * (1 + abs(random.gauss(0, vol / 2))), min(o, c) * (1 - abs(random.gauss(0, vol / 2)))
        out.append(Bar(t, o, h, l, c, random.uniform(50, 150)))
        px = c
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--csv", default="")
    a = ap.parse_args()
    cfg = Config()
    if a.synthetic:
        bars = synthetic(a.days)
    else:
        from exchange import public_exchange, fetch_candles
        ex = public_exchange(cfg)
        bars = fetch_candles(ex, cfg.symbol, int(time.time() * 1000) - a.days * 86_400_000)[:-1]
    broker = PaperBroker(cfg)
    eng = Engine(cfg)
    tr = Trader(cfg, eng, broker, NullNotifier(), Store(None))
    for b in bars:
        tr.on_bar(b, live=True, fresh=True)
    r = tr.trades
    n = len(r)
    if not n:
        print("Sin operaciones")
        return
    wins = [x for x in r if x > 0]
    loss = [-x for x in r if x <= 0]
    eq = peak = dd = 0.0
    for x in r:
        eq += x; peak = max(peak, eq); dd = max(dd, peak - eq)
    print(f"Velas {len(bars)} · ops {n} · acierto {100*len(wins)/n:.0f}% · E {sum(r)/n:+.3f}R · "
          f"PF {sum(wins)/sum(loss) if loss else float('inf'):.2f} · total {sum(r):+.1f}R · maxDD {dd:.1f}R · "
          f"equity {broker.eq:.0f}")

    by = {}
    for day, x, why in tr.trade_log:
        by.setdefault(day[:7], []).append(x)
    print("Mes      ops  R")
    for k in sorted(by):
        print(f"{k}  {len(by[k]):>3}  {sum(by[k]):+.1f}")
    if a.csv:
        import csv
        with open(a.csv, "w", newline="") as f:
            w = csv.writer(f); w.writerow(["dia", "R", "salida"]); w.writerows(tr.trade_log)
        print("CSV:", a.csv)


if __name__ == "__main__":
    main()

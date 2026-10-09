"""
Pruebas sin red del P12 bot 1.3: bloqueo/pausa de BingX, riesgo con R real, cortacircuitos, guardián de stops,
tope de cuenta, sweep y regresión del motor con velas sintéticas.   python test_p12.py
"""
import os, random, tempfile, types
os.environ.update(MODE="LIVE", DRY_RUN="false", BINGX_API_KEY="k", BINGX_SECRET_KEY="s", STATE_DIR=tempfile.mkdtemp(),
                  SYMBOLS="BTC-USDT,ETH-USDT", NEWS="false", MAX_CONSEC_LOSS="3", MAX_DD_PCT="8", MAX_ACCOUNT_POS="2", TELEGRAM_TOKEN="", TELEGRAM_CHAT_ID="")
import time
import bot as B
B.time.sleep = lambda s: None          # sin esperas reales en las pruebas
B.tg.send = lambda *a, **k: MSG.append(a[0] if a else "")
MSG = []


class Resp:
    def __init__(s, j): s.j = j
    def json(s): return s.j


# 1) md_get: 109429 bloquea la ruta sin volver a llamar; 109415 pausa el símbolo
calls = []
fake = types.SimpleNamespace(get=lambda url, params=None, timeout=0: (calls.append(url), Resp({"code": 109429, "msg": "over 10 error ... can retry after time: %d" % int((time.time() + 600) * 1000)}))[1])
B.ses = lambda: fake
try:
    B.klines("BTC-USDT", "5m", 10); raise SystemExit("debía fallar")
except B.MDBlocked:
    pass
n = len(calls)
try:
    B.klines("ETH-USDT", "5m", 10)
except B.MDBlocked:
    pass
assert len(calls) == n, "ruta bloqueada: no debe haber HTTP"
B.MD_BLOCK["until"] = 0.0
calls.clear()
fake.get = lambda url, params=None, timeout=0: (calls.append(url), Resp({"code": 109415, "msg": "NCFXUSD2ILS-USDT is pause currently"}))[1]
try:
    B.klines("NCFXUSD2ILS-USDT", "5m", 10)
except B.MDPaused:
    pass
n = len(calls)
try:
    B.klines("NCFXUSD2ILS-USDT", "5m", 10)
except B.MDPaused:
    pass
assert len(calls) == n and B.PAUSED["NCFXUSD2ILS-USDT"] > time.time()
assert B.process("NCFXUSD2ILS-USDT", 0, None) is None
print("1 OK bloqueo 109429 respetado y contrato pausado 109415 apartado")

# 2) riesgo con R REAL: 3 pérdidas reales seguidas pausan; las hipotéticas ya no
B.STATE.update(paused=False, live={}, risk={}, real={})
class BXF:
    lock = B.BX.lock
B.CONTRACT["AAA-USDT"] = dict(p=2, q=3, minq=0.001, minusdt=1.0, open=True, maint=0, taker=0.0005)
B.BX.realized = lambda sym, t0: -10.0
for i in range(3):
    B.STATE["live"]["AAA-USDT"] = dict(dry=False, amt=1.0, rU=10.0, t_open=0, dir=1, key=f"k{i}")
    B.finish_live("AAA-USDT", B.STATE["live"]["AAA-USDT"], "x")
assert B.STATE["paused"] and B.real_risk()["consec"] == 3 and B.real_risk()["dayR"] == -3.0
B.STATE.update(paused=False, real={}, risk={})
for i in range(5):
    B.record_trade("AAA-USDT", {"td": "2026-10-07", "isBtc": True, "btcRd": 0, "moPx": None, "wB": 1, "scen": 1, "coinc": True, "news": None, "nDir": 0},
                   dict(dir=1, R=-1.0, why="SL", mfe=0.1)) if False else None
B.STATE["risk"] = {"consec": 9, "day": "x", "dayR": -9}
assert B.risk_gate("AAA-USDT", {"td": "x"}, 1) == "", "las pérdidas hipotéticas no deben bloquear la ejecución real"
print("2 OK pausa por pérdidas reales, no por las hipotéticas del top")

# 3) cortacircuitos de equity y reinicio por cambio de cuenta
B.STATE.update(paused=False, hwm=1000.0, hwm_acct=B.acct_id())
B.BX.balance = lambda: (900.0, 900.0)
B.equity_guard()
assert B.STATE["paused"], "−10% debe pausar"
B.STATE.update(paused=False, hwm=100000.0, hwm_acct="otra")
B.equity_guard()
assert not B.STATE["paused"] and B.STATE["hwm"] == 900.0, "no arrastrar el máximo de otra cuenta"
print("3 OK cortacircuitos de equity y máximo por cuenta")

# 4) tope de posiciones de la cuenta y apertura normal con SL adjunto
S = dict(td=B.tdate(B.now_ms()), szMult=1.0, zEdge=101.0, pH=105.0, pL=95.0)
e = dict(dir=1, sl=98.0, ent=100.0, rU=2.0)
B.STATE.update(paused=False, live={}, risk={}, real={}, hwm=1000.0, hwm_acct=B.acct_id())
B.BX.balance = lambda: (1000.0, 1000.0)
B.BX.positions = lambda sym=None: []
B.BX.all_positions = lambda: [{"symbol": "X-USDT", "positionSide": "LONG", "positionAmt": "1"}, {"symbol": "Y-USDT", "positionSide": "SHORT", "positionAmt": "-1"}]
B.mark_price = lambda sym: 100.0
out = B.live_open("AAA-USDT", S, e)
assert "MAX_ACCOUNT_POS" in out and "AAA-USDT" not in B.STATE["live"], out
orders = []
B.BX.all_positions = lambda: []
B.BX.leverage = lambda sym: None
B.BX.cancel_all = lambda sym: None
posbook = []
B.BX.position = lambda sym, d: ({"amt": posbook[0], "avg": 100.0} if posbook else None)
B.BX.find_stop = lambda sym, ps, ot: "sl9"
B.BX.is_hedge = lambda: True
def fake_order(sym, side, ps, ot, qty, stop=None, reduce=False, cid=None, stop_loss=None):
    orders.append((ot, side, qty, stop, bool(stop_loss)))
    if ot == "MARKET" and not reduce:
        posbook.append(float(qty))
    return f"id{len(orders)}"
B.BX.order = fake_order
out = B.live_open("AAA-USDT", S, e)
assert "EJECUTADA" in out and orders[0][0] == "MARKET" and orders[0][4], out
assert orders[1][0] == "TAKE_PROFIT_MARKET"
print("4 OK tope de cuenta y apertura con SL adjunto + TP")

# 5) guardián: repone SL/TP si faltan; no inventa si la consulta falla
orders.clear()
L = B.STATE["live"]["AAA-USDT"]
B.BX.open_orders = lambda sym: []
B.guard_stops()
kinds = sorted(o[0] for o in orders)
assert kinds == ["STOP_MARKET", "TAKE_PROFIT_MARKET"], kinds
orders.clear()
B.BX.open_orders = lambda sym: None
B.guard_stops()
assert not orders, "si no se puede consultar no se debe colocar nada"
B.BX.open_orders = lambda sym: [{"type": "STOP_MARKET", "positionSide": "LONG"}, {"type": "TAKE_PROFIT_MARKET", "positionSide": "LONG"}]
B.guard_stops()
assert not orders, "con SL y TP vivos no hace nada"
print("5 OK guardián de stops")

# 6) motor sintético: regresión y filtros por parámetro (sin tocar globales)
def synth_bars(days, seed):
    rnd, p, out = random.Random(seed), 100.0, []
    t0 = B.ny_ms(B.tdate(B.now_ms()) - B.timedelta(days=days + 3), B.P12S)
    drift = 0.0
    for i in range(int((days + 3) * 288)):
        t = t0 + i * B.BAR_MS
        m = B.nymin(t)
        if m == B.P12E or i % 288 == 0:
            drift = rnd.choice((-1, 1)) * rnd.uniform(0.0002, 0.0009)
        o = p
        c = o * (1 + drift * (1 if B.in_(m, B.P12E, B.EXITM) else 0.0) + rnd.gauss(0, 0.0014))
        out.append(B.Bar(t, o, max(o, c) * 1.0004, min(o, c) * 0.9996, c, 1000.0))
        p = c
    return out
tot, tot_f = [], 0
for seed in range(1, 5):
    bars = synth_bars(160, seed)
    ts = [b.t for b in bars]
    td = B.tdate(bars[0].t) + B.timedelta(days=2)
    last = B.tdate(bars[-1].t) - B.timedelta(days=1)
    while td <= last:
        if td.weekday() < 5:
            de = B.ny_ms(td + B.timedelta(days=1), B.P12S)
            j = B.bisect.bisect_right(ts, de - B.BAR_MS)
            win = bars[max(0, j - B.KL_LIMIT):j]
            if len(win) >= 300:
                S1 = B.simulate("T-USDT", win, td, None, 0, 0, True, cost=0.12, F=B.F_OFF)
                if S1["trade"] and not S1["incomplete"]:
                    tot.append(B.trade_rec("T-USDT", S1, S1["trade"]))
                S2 = B.simulate("T-USDT", win, td, None, 0, 0, True, cost=0.12)
                tot_f += 1 if (S2["trade"] and not S2["incomplete"]) else 0
        td += B.timedelta(days=1)
assert B.NIGHT_FILTER == "EXCL_BOTH" and B.MISMATCH == "REDUCE", "las globales no deben cambiar"
assert len(tot) >= tot_f, "sin filtros nunca puede haber menos operaciones que con filtros"
print(f"6 OK motor sintético: {len(tot)} operaciones sin filtros, {tot_f} con los de config")

# 7) sweep sobre registros: detecta un filtro con ventaja real y NO inventa uno con ruido
rnd = random.Random(5)
def rec(i, good):
    td = str(B.datetime(2026, 1, 1).date() + B.timedelta(days=i // 3))
    scen = 3 if (i % 4 == 0) else 1
    r = rnd.gauss(-1.0 if (scen == 3 and good) else 0.4, 1.0)
    return dict(sym="S", td=td, R=r, scen=scen, nal=1, btc=1, isbtc=False, mo=True, wB=1, coinc=True)
sig = B.sweep_text([rec(i, True) for i in range(900)], 300)
assert "✅ Noche: sin 'ambos lados'" in sig, sig
noise = B.sweep_text([rec(i, False) for i in range(900)], 300)
assert not [l for l in noise.split("\n") if l.startswith("✅ ") and "Candidatos" not in l], noise
assert "solo" in B.sweep_text([rec(i, True) for i in range(10)], 5)
print("7 OK sweep: encuentra el filtro real, no inventa con ruido, rechaza muestras pequeñas")
print("TODO OK")

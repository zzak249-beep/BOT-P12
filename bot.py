#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════════════════
# P12 HUNTER BOT — motor de "P12 Hunter v4.2" (Pine) en Python
# BingX perpetuos 5m → señales Telegram + ejecución opcional (MODE=LIVE)
#
#  Motor: replay determinista del día estadístico (18:00→18:00 NY) a cada cierre
#  de vela 5m. Mismas reglas que el Pine: P12 Asia 18:00-02:30 + Londres 02:30-06:00,
#  lectura 06:00-09:00 (Market Profile N×30m o velas), apertura 09:30, entradas
#  hasta 12:00, cierre forzado 15:55, una operación por día y símbolo.
#  Los eventos se deduplican en disco: un redeploy no reenvía nada.
#
#  Telegram: tarjeta del día por símbolo que se EDITA (sin spam), mensajes nuevos
#  con sonido solo para lo accionable (zona activa, precio en zona, entrada, salida),
#  todo lo informativo en silencio, respuestas encadenadas a la tarjeta/entrada,
#  gráfico PNG con P12/zona/SL/TP, botones TradingView/BingX, horas en tu zona,
#  señal "tardía" marcada y nunca ejecutada, cola con ritmo por chat y 429 respetado,
#  comandos /estado /hoy /stats /pausa /reanuda, resumen diario.
# ═══════════════════════════════════════════════════════════════════════════
import os, io, json, time, math, hmac, hashlib, logging, threading, statistics, html
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from urllib.parse import urlencode
from http.server import BaseHTTPRequestHandler, HTTPServer
from collections import namedtuple
import requests

os.environ.setdefault("MPLBACKEND", "Agg")
CODE_VERSION = "P12-BOT 1.0.0 · 2026-10-02 · motor P12 v4.2"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("p12")


# ───────────────────────── CONFIG (todas las env sin comillas) ─────────────────────────
def _e(k, d=""):
    v = os.getenv(k)
    if v is None:
        return str(d)
    return v.strip().strip('"').strip("'").strip()


def _b(k, d):
    return _e(k, "true" if d else "false").lower() in ("1", "true", "yes", "y", "si", "sí", "on")


def _f(k, d):
    try:
        return float(_e(k, d))
    except ValueError:
        return float(d)


def _i(k, d):
    try:
        return int(float(_e(k, d)))
    except ValueError:
        return int(d)


def _hhmm(k, d):
    v = _i(k, d)
    return (v // 100) * 60 + v % 100


def _sym(s):
    s = s.strip().upper().replace("/", "-").replace(".P", "").replace("BINGX:", "")
    if "-" not in s and s.endswith("USDT"):
        s = s[:-4] + "-USDT"
    return s


MODE = _e("MODE", "SIGNAL").upper()            # SIGNAL | LIVE
LIVE = MODE == "LIVE"
DRY_RUN = _b("DRY_RUN", True)
SYMBOLS = [_sym(s) for s in _e("SYMBOLS", "BTC-USDT,ETH-USDT,SOL-USDT").split(",") if s.strip()]
BTC_SYMBOL = _sym(_e("BTC_SYMBOL", "BTC-USDT"))

TZ_NY = ZoneInfo(_e("TZ_NY", "America/New_York"))
TZ_LOC = ZoneInfo(_e("TZ_LOCAL", "Europe/Madrid"))
P12S = _hhmm("P12_START", 1800)
LONS = _hhmm("LONDON_START", 230)
P12E = _hhmm("P12_END", 600)
READE = _hhmm("READ_END", 900)
OPENM = _hhmm("OPEN_TIME", 930)
ENTE = _hhmm("LAST_ENTRY", 1200)
EXITM = _hhmm("FORCED_EXIT", 1555)
DIGEST = _hhmm("DIGEST_TIME", 1600)
SKIP_WE = _b("SKIP_WEEKEND", True)

ACC_MODE = _e("ACC_MODE", "MP").upper()        # MP | BARS
MP_N = _i("MP_N", 2)
ACCEPT_MIN = _i("ACCEPT_MIN", 30)
RVOL_MIN = _f("RVOL_MIN", 0.0)

NIGHT_FILTER = _e("NIGHT_FILTER", "EXCL_BOTH").upper()   # NONE | EXCL_BOTH | SAME_ALIGNED
COINC_MODE = _e("COINC_MODE", "MID").upper()             # MID | OUTSIDE
MISMATCH = _e("MISMATCH", "REDUCE").upper()              # REDUCE | DISCARD
REDUCE_F = _f("REDUCE_F", 0.5)
BTC_FILTER = _e("BTC_FILTER", "OFF").upper()             # OFF | NOT_AGAINST | SAME
MO_FILTER = _e("MO_FILTER", "OFF").upper()               # OFF | FAVOR | AGAINST
W_LOOK = _i("W_LOOK", 20)
W_NARROW = _f("W_NARROW", 0.75)
W_WIDE = _f("W_WIDE", 1.33)
WIDTH_FILTER = _e("WIDTH_FILTER", "OFF").upper()         # OFF | EXCL_NARROW | EXCL_WIDE | ONLY_NORMAL

ENTRY_MODE = _e("ENTRY_MODE", "CONF").upper()            # CONF | LIMIT
LIM_FRAC = _f("LIM_FRAC", 0.5)
ATR_LEN = _i("ATR_LEN", 14)
STOP_BUF = _f("STOP_BUF", 0.25)
TGT_MODE = _e("TGT_MODE", "R").upper()                   # R | EXT
RR = _f("RR", 2.0)
EXT_K = _f("EXT_K", 1.0)
BE_R = _f("BE_R", 0.0)
RISK_PCT = _f("RISK_PCT", 0.5)

COST_RT = _f("COST_RT", 0.12)
MAX_COST_R = _f("MAX_COST_R", 0.20)
MAX_STOP_PCT = _f("MAX_STOP_PCT", 2.5)
FUND_H = _i("FUND_H", 8)
FUND_PCT = _f("FUND_PCT", 0.01)
EXIT_FUND = _b("EXIT_FUND", False)

LEVERAGE = _i("LEVERAGE", 10)
MAX_POS = _i("MAX_POS", 3)
MIN_N = _i("MIN_N", 20)
STALE_SEC = _i("STALE_SEC", 240)
CYCLE_DELAY = _i("CYCLE_DELAY", 5)
KL_LIMIT = 700

TG_TOKEN = _e("TELEGRAM_TOKEN", "")
TG_CHAT = _e("TELEGRAM_CHAT_ID", "")
TG_THREAD = _e("TELEGRAM_THREAD_ID", "")
TG_ADMINS = {x.strip() for x in _e("TELEGRAM_ADMIN_IDS", "").split(",") if x.strip()}
TG_COMMANDS = _b("TG_COMMANDS", True)
TG_CHARTS = _b("TG_CHARTS", True)
TG_TOUCH = _b("TG_TOUCH_ALERT", True)

BX_KEY = _e("BINGX_API_KEY", "")
BX_SECRET = _e("BINGX_SECRET_KEY", "")
BX_BASE = _e("BINGX_BASE", "https://open-api.bingx.com").rstrip("/")
MD_BASE = "https://open-api.bingx.com"
PORT = _i("PORT", 8080)

if LIVE and ENTRY_MODE == "LIMIT":
    log.warning("ENTRY_MODE=LIMIT es solo de señales; en LIVE se fuerza CONF")
    ENTRY_MODE = "CONF"


def _state_dir():
    for d in (_e("STATE_DIR", "/data"), "./state"):
        try:
            os.makedirs(d, exist_ok=True)
            t = os.path.join(d, ".w")
            with open(t, "w") as fh:
                fh.write("1")
            os.remove(t)
            return d
        except Exception:
            continue
    return "."


STATE_DIR = _state_dir()
SF = os.path.join(STATE_DIR, "p12_state.json")

# ───────────────────────── TIEMPO ─────────────────────────
Bar = namedtuple("Bar", "t o h l c v")
BAR_MS = 300_000
DOW = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
WB = ["estrecho", "normal", "ancho"]


def ny(t):
    return datetime.fromtimestamp(t / 1000, TZ_NY)


def nymin(t):
    d = ny(t)
    return d.hour * 60 + d.minute


def in_(m, s, e):
    return (s <= m < e) if s < e else (m >= s or m < e)


def tdate(t):
    """Fecha de operación del día estadístico que contiene la vela que abre en t."""
    d = ny(t)
    x = d.date()
    return x + timedelta(days=1) if d.hour * 60 + d.minute >= P12S else x


def ny_ms(td, mins):
    d = td - timedelta(days=1) if mins >= P12S else td
    return int(datetime(d.year, d.month, d.day, mins // 60, mins % 60, tzinfo=TZ_NY).timestamp() * 1000)


def loc(td, mins):
    return datetime.fromtimestamp(ny_ms(td, mins) / 1000, TZ_LOC).strftime("%H:%M")


def loc_t(t):
    return datetime.fromtimestamp(t / 1000, TZ_LOC).strftime("%H:%M")


def sign(x):
    return (x > 0) - (x < 0)


# ───────────────────────── DATOS DE MERCADO ─────────────────────────
SES = requests.Session()
SES.headers["User-Agent"] = "p12-bot/1.0"
PREC = {}


def http_get(url, params=None, tries=3):
    for a in range(tries):
        try:
            return SES.get(url, params=params, timeout=15).json()
        except Exception:
            if a == tries - 1:
                raise
            time.sleep(1.5 * (a + 1))


def klines(sym, interval, limit, end_ms=None):
    d = http_get(MD_BASE + "/openApi/swap/v3/quote/klines", {"symbol": sym, "interval": interval, "limit": limit})
    if not isinstance(d, dict) or d.get("code", 0) != 0:
        raise RuntimeError(f"klines {sym}: {str(d)[:200]}")
    out = {}
    for x in d.get("data") or []:
        t = int(x["time"])
        out[t] = Bar(t, float(x["open"]), float(x["high"]), float(x["low"]), float(x["close"]), float(x.get("volume", 0) or 0))
    bars = [out[k] for k in sorted(out)]
    step = BAR_MS if interval == "5m" else 3_600_000
    if end_ms:
        bars = [b for b in bars if b.t + step <= end_ms]
    return bars


def load_contracts():
    try:
        d = http_get(MD_BASE + "/openApi/swap/v2/quote/contracts")
        for c in d.get("data") or []:
            PREC[c["symbol"]] = dict(p=int(c.get("pricePrecision", 4)), q=int(c.get("quantityPrecision", 3)),
                                     minq=float(c.get("tradeMinQuantity", 0) or 0), minusdt=float(c.get("tradeMinUSDT", 0) or 0))
        log.info(f"contratos: {len(PREC)}")
    except Exception as ex:
        log.warning(f"contratos no cargados: {ex}")


def fp(sym, x):
    if x is None:
        return "—"
    p = PREC.get(sym, {}).get("p")
    if p is None:
        p = 2 if abs(x) >= 100 else 4 if abs(x) >= 1 else 6
    return f"{x:.{p}f}"


def fq(sym, q):
    p = PREC.get(sym, {}).get("q", 3)
    f = 10 ** p
    return f"{math.floor(q * f) / f:.{p}f}"


def atr_series(bars, n):
    out, acc, r = [None] * len(bars), [], None
    for i, b in enumerate(bars):
        tr = b.h - b.l if i == 0 else max(b.h - b.l, abs(b.h - bars[i - 1].c), abs(b.l - bars[i - 1].c))
        if r is None:
            acc.append(tr)
            if len(acc) == n:
                r = sum(acc) / n
        else:
            r = (r * (n - 1) + tr) / n
        out[i] = r
    return out


def sma_series(vals, n):
    out, s = [None] * len(vals), 0.0
    for i, v in enumerate(vals):
        s += v
        if i >= n:
            s -= vals[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


WCACHE = {}


def width_median(sym, td):
    """Mediana del ancho P12 de los últimos W_LOOK días laborables anteriores (velas 1h)."""
    c = WCACHE.get(sym)
    if c and c[0] == td:
        return c[1]
    med = None
    try:
        bars = klines(sym, "1h", min(1440, (W_LOOK * 2 + 10) * 24))
        agg = {}
        for b in bars:
            if not in_(nymin(b.t), P12S, P12E):
                continue
            d = tdate(b.t)
            if d >= td or (SKIP_WE and d.weekday() >= 5):
                continue
            a = agg.setdefault(d, [b.h, b.l, 0])
            a[0], a[1], a[2] = max(a[0], b.h), min(a[1], b.l), a[2] + 1
        ws = [a[0] - a[1] for _, a in sorted(agg.items()) if a[2] >= 10][-W_LOOK:]
        med = statistics.median(ws) if len(ws) >= 5 else None
    except Exception as ex:
        log.warning(f"{sym} mediana ancho: {ex}")
    WCACHE[sym] = (td, med)
    return med


def btc_pos(bars, td, upto_ms):
    """Posición de BTC respecto a SU P12 al cierre de la última vela ≤ upto_ms."""
    h = l = None
    pos = 0
    for b in bars:
        if b.t + BAR_MS > upto_ms:
            break
        if tdate(b.t) != td:
            continue
        if in_(nymin(b.t), P12S, P12E):
            h = b.h if h is None else max(h, b.h)
            l = b.l if l is None else min(l, b.l)
            pos = 0
        elif h is not None:
            pos = 1 if b.c > h else -1 if b.c < l else 0
    return pos


# ───────────────────────── MOTOR (réplica del Pine) ─────────────────────────
def new_snap(sym, td):
    return dict(sym=sym, td=td, events=[], incomplete=False, isBtc=False, weekend=False,
                aO=None, aH=None, aL=None, aC=None, lO=None, lH=None, lL=None, lC=None, pH=None, pL=None, pM=None,
                moPx=None, scen=0, nDir=0, wRatio=None, wB=1, scenDone=False, readDone=False, openDone=False,
                accH=False, accL=False, annH=False, annL=False, cntH=0, cntL=0, vH=0.0, vL=0.0, rd=0, btcRd=0, btcNow=0,
                opx=None, moFav=False, nightOk=True, btcOk=True, moOk=True, wOk=True, bias=0, coinc=False, szMult=0.0,
                zEdge=None, zDeep=None, stop=None, atr=None, dead=False, traded=False, touched=False, winClosed=False,
                rejCost=False, rejStop=False, costR=None, stopPct=None, pos=None, trade=None, limit=None,
                last_t=None, last_c=None, last_h=None, last_l=None)


def simulate(sym, bars, td, wmed, btc_rd, btc_now, is_btc):
    S = new_snap(sym, td)
    S["btcNow"], S["isBtc"] = btc_now, is_btc
    idx = [i for i, b in enumerate(bars) if tdate(b.t) == td]
    if not idx:
        return S
    atr = atr_series(bars, ATR_LEN)
    vavg = sma_series([b.v for b in bars], 288)
    S["incomplete"] = nymin(bars[idx[0]].t) != P12S
    accb = max(1, round(ACCEPT_MIN / 5))
    E = S["events"]

    def emit(k, t, **kw):
        E.append(dict(k=k, t=t, **kw))

    def open_pos(i, tc, ent):
        d = S["bias"]
        rU = abs(ent - S["stop"])
        tR = ent + d * RR * rU
        tX = S["zEdge"] + d * EXT_K * (S["pH"] - S["pL"])
        tp = tR if TGT_MODE == "R" else (tX if (tX - ent) * d > 0.5 * rU else tR)
        S["pos"] = dict(dir=d, ent=ent, sl=S["stop"], tp=tp, rU=rU, i=i, t=tc, be=False)
        S["traded"] = True
        emit("entry", tc, dir=d, ent=ent, sl=S["stop"], tp=tp, rU=rU)

    def close_pos(px, tc, why):
        p = S["pos"]
        d, rU = p["dir"], p["rU"]
        per = FUND_H * 3_600_000
        nf = int(math.floor(tc / per) - math.floor(p["t"] / per))
        r = (px - p["ent"]) * d / rU - COST_RT / 100 * p["ent"] / rU - nf * FUND_PCT / 100 * p["ent"] / rU
        S["trade"] = dict(dir=d, ent=p["ent"], exit=px, sl=p["sl"], tp=p["tp"], rU=rU, R=r, why=why, t_in=p["t"], t_out=tc, nf=nf)
        S["pos"] = None
        emit("exit", tc, **S["trade"])

    def entry_ok(tc, ent):
        rU = abs(ent - S["stop"])
        if rU <= 0:
            return False
        S["costR"], S["stopPct"] = COST_RT / 100 * ent / rU, rU / ent * 100
        if S["costR"] > MAX_COST_R:
            if not S["rejCost"]:
                S["rejCost"] = True
                emit("reject", tc, why="coste", val=S["costR"])
            return False
        if S["stopPct"] > MAX_STOP_PCT:
            if not S["rejStop"]:
                S["rejStop"] = True
                emit("reject", tc, why="stop", val=S["stopPct"])
            return False
        return True

    for n, i in enumerate(idx):
        b = bars[i]
        m = nymin(b.t)
        tc = b.t + BAR_MS
        ia, il = in_(m, P12S, LONS), in_(m, LONS, P12E)
        ip, ir = ia or il, in_(m, P12E, READE)

        # (1) órdenes vivas: SL/TP intrabar (SL primero si ambos) y límite pendiente
        p = S["pos"]
        if p and i > p["i"]:
            px = why = None
            if p["dir"] == 1:
                if b.l <= p["sl"]:
                    px, why = min(b.o, p["sl"]), ("BE" if p["be"] else "SL")
                elif b.h >= p["tp"]:
                    px, why = max(b.o, p["tp"]), "TP"
            else:
                if b.h >= p["sl"]:
                    px, why = max(b.o, p["sl"]), ("BE" if p["be"] else "SL")
                elif b.l <= p["tp"]:
                    px, why = min(b.o, p["tp"]), "TP"
            if px is not None:
                close_pos(px, tc, why)
        if S["limit"] is not None and S["pos"] is None and not S["traded"]:
            lim = S["limit"]
            if (S["bias"] == 1 and b.l <= lim) or (S["bias"] == -1 and b.h >= lim):
                S["limit"] = None
                open_pos(i, tc, min(b.o, lim) if S["bias"] == 1 else max(b.o, lim))

        # (2) estado del día
        if n == 0:
            S.update(aO=b.o, aH=b.h, aL=b.l, aC=b.c, pH=b.h, pL=b.l)
        else:
            if S["moPx"] is None and m == 0:
                S["moPx"] = b.o
            if ia:
                S["aH"], S["aL"], S["aC"] = max(S["aH"], b.h), min(S["aL"], b.l), b.c
            if il:
                if S["lO"] is None:
                    S.update(lO=b.o, lH=b.h, lL=b.l)
                else:
                    S["lH"], S["lL"] = max(S["lH"], b.h), min(S["lL"], b.l)
                S["lC"] = b.c
            if ip:
                S["pH"], S["pL"] = max(S["pH"], b.h), min(S["pL"], b.l)

            if not S["scenDone"] and not ip:
                S["scenDone"] = True
                S["pM"] = (S["pH"] + S["pL"]) / 2
                aDir = sign(S["aC"] - S["aO"])
                lDir = 0 if S["lO"] is None else sign(S["lC"] - S["lO"])
                both = S["lO"] is not None and S["lH"] > S["aH"] and S["lL"] < S["aL"]
                S["scen"] = 0 if S["lO"] is None else 3 if both else 1 if (aDir == lDir and aDir != 0) else 2
                S["nDir"] = aDir if S["scen"] == 1 else 0
                w = S["pH"] - S["pL"]
                S["wRatio"] = w / wmed if wmed else None
                S["wB"] = 1 if S["wRatio"] is None else 0 if S["wRatio"] < W_NARROW else 2 if S["wRatio"] > W_WIDE else 1
                emit("p12", tc)

            if S["scenDone"] and not S["readDone"] and ir:
                if ACC_MODE == "MP":
                    if (m + 5) % 30 == 0:
                        S["cntH"] = S["cntH"] + 1 if b.c > S["pH"] else 0
                        S["cntL"] = S["cntL"] + 1 if b.c < S["pL"] else 0
                        S["accH"] = S["accH"] or S["cntH"] >= MP_N
                        S["accL"] = S["accL"] or S["cntL"] >= MP_N
                else:
                    if min(b.o, b.c) > S["pH"]:
                        S["cntH"] += 1
                        S["vH"] += b.v
                    else:
                        S["cntH"], S["vH"] = 0, 0.0
                    if max(b.o, b.c) < S["pL"]:
                        S["cntL"] += 1
                        S["vL"] += b.v
                    else:
                        S["cntL"], S["vL"] = 0, 0.0
                    va = vavg[i] or 0.0
                    if S["cntH"] >= accb and (RVOL_MIN <= 0 or S["vH"] / S["cntH"] >= RVOL_MIN * va):
                        S["accH"] = True
                    if S["cntL"] >= accb and (RVOL_MIN <= 0 or S["vL"] / S["cntL"] >= RVOL_MIN * va):
                        S["accL"] = True
                if b.h > S["pH"] and b.c <= S["pH"]:
                    S["annH"] = True
                if b.l < S["pL"] and b.c >= S["pL"]:
                    S["annL"] = True

            if S["scenDone"] and not S["readDone"] and not ir:
                S["readDone"] = True
                S["rd"] = 0 if (S["accH"] and S["accL"]) else 1 if S["accH"] else -1 if S["accL"] else 0
                S["btcRd"] = 0 if is_btc else btc_rd
                emit("read", tc)

            if S["readDone"] and not S["openDone"] and in_(m, OPENM, P12S):
                S["openDone"], S["opx"], S["atr"] = True, b.o, atr[i]
                rd, mo = S["rd"], S["moPx"]
                S["moFav"] = False if mo is None else (b.o > mo if rd == 1 else b.o < mo if rd == -1 else False)
                S["nightOk"] = True if NIGHT_FILTER == "NONE" else (S["scen"] != 3 if NIGHT_FILTER == "EXCL_BOTH" else (S["scen"] == 1 and S["nDir"] == rd))
                S["btcOk"] = True if (BTC_FILTER == "OFF" or is_btc) else (S["btcRd"] != -rd if BTC_FILTER == "NOT_AGAINST" else S["btcRd"] == rd)
                S["moOk"] = True if (MO_FILTER == "OFF" or mo is None) else (S["moFav"] if MO_FILTER == "FAVOR" else not S["moFav"])
                S["wOk"] = {"OFF": True, "EXCL_NARROW": S["wB"] != 0, "EXCL_WIDE": S["wB"] != 2}.get(WIDTH_FILTER, S["wB"] == 1)
                ok = rd != 0 and S["nightOk"] and S["btcOk"] and S["moOk"] and S["wOk"]
                S["bias"] = rd if ok else 0
                a = atr[i] or 0.0
                if S["bias"] == 1:
                    S["zEdge"], S["zDeep"], S["stop"] = S["pH"], S["pM"], S["pM"] - STOP_BUF * a
                    S["coinc"] = b.o >= S["pM"] if COINC_MODE == "MID" else b.o > S["pH"]
                elif S["bias"] == -1:
                    S["zEdge"], S["zDeep"], S["stop"] = S["pL"], S["pM"], S["pM"] + STOP_BUF * a
                    S["coinc"] = b.o <= S["pM"] if COINC_MODE == "MID" else b.o < S["pL"]
                S["szMult"] = 0.0 if S["bias"] == 0 else 1.0 if S["coinc"] else (0.0 if MISMATCH == "DISCARD" else REDUCE_F)
                emit("open", tc)

        # (3) entradas al cierre de vela
        ww = S["openDone"] and in_(m, OPENM, ENTE)
        allowed = S["bias"] != 0 and S["szMult"] > 0 and not S["traded"]
        if ww and allowed and S["pos"] is None and not S["dead"]:
            if (S["bias"] == 1 and b.c < S["stop"]) or (S["bias"] == -1 and b.c > S["stop"]):
                S["dead"], S["limit"] = True, None
                emit("dead", tc)
        if ww and allowed and S["pos"] is None and not S["dead"]:
            d = S["bias"]
            touching = b.l <= S["zEdge"] if d == 1 else b.h >= S["zEdge"]
            if touching and not S["touched"]:
                S["touched"] = True
                emit("touch", tc, px=b.c)
            if ENTRY_MODE == "CONF":
                sig = (d == 1 and b.l <= S["zEdge"] and b.l > S["stop"] and b.c > b.o and b.c > S["zDeep"]) or \
                      (d == -1 and b.h >= S["zEdge"] and b.h < S["stop"] and b.c < b.o and b.c < S["zDeep"])
                if sig and entry_ok(tc, b.c):
                    open_pos(i, tc, b.c)
            elif S["limit"] is None:
                lim = S["zEdge"] + LIM_FRAC * (S["zDeep"] - S["zEdge"])
                if ((d == 1 and b.c > lim) or (d == -1 and b.c < lim)) and entry_ok(tc, lim):
                    S["limit"] = lim
                    emit("limit", tc, lim=lim)
        if S["limit"] is not None and S["pos"] is None and not (ww and allowed and not S["dead"]):
            S["limit"] = None

        # (4) gestión al cierre: breakeven, cierre forzado, pre-funding
        p = S["pos"]
        if p and i > p["i"]:
            if BE_R > 0 and not p["be"]:
                fav = b.h - p["ent"] if p["dir"] == 1 else p["ent"] - b.l
                if fav >= BE_R * p["rU"]:
                    p["sl"], p["be"] = p["ent"], True
                    emit("be", tc, sl=p["ent"])
            if in_(m, EXITM, P12S):
                close_pos(b.c, tc, "Cierre NY")
            elif EXIT_FUND:
                u = datetime.fromtimestamp(b.t / 1000, timezone.utc)
                if (u.hour * 60 + u.minute + 10) % (FUND_H * 60) == 0:
                    close_pos(b.c, tc, "Pre-funding")

        if S["openDone"] and S["bias"] != 0 and S["szMult"] > 0 and not S["traded"] and not S["dead"] \
                and not S["winClosed"] and in_(m, ENTE, P12S):
            S["winClosed"] = True
            emit("window", tc)

        S.update(last_t=tc, last_c=b.c, last_h=b.h, last_l=b.l)
    return S


# ───────────────────────── TEXTOS ─────────────────────────
def esc(s):
    return html.escape(str(s), quote=False)


def scen_txt(S):
    s = S["scen"]
    return ("Mismo lado " + ("↑" if S["nDir"] == 1 else "↓")) if s == 1 else "Se contradicen" if s == 2 else "Ambos lados" if s == 3 else "—"


def read_txt(S):
    if S["rd"] == 1:
        return "ACEPTA ↑"
    if S["rd"] == -1:
        return "ACEPTA ↓"
    if S["accH"] and S["accL"]:
        return "ACEPTA AMBOS"
    return "ANUNCIA" if (S["annH"] or S["annL"]) else "MID / sin aceptación"


def progress(S):
    if ACC_MODE == "MP":
        return f"↑ {S['cntH']}/{MP_N} · ↓ {S['cntL']}/{MP_N} periodos 30m"
    n = max(1, round(ACCEPT_MIN / 5))
    return f"↑ {S['cntH']}/{n} · ↓ {S['cntL']}/{n} velas"


def why_txt(S):
    if S["rd"] == 0:
        if S["accH"] and S["accL"]:
            return "Aceptó arriba Y abajo: lectura contradictoria"
        if S["annH"] or S["annL"]:
            return "Sin aceptación: solo ANUNCIÓ (mecha) y volvió"
        return "Sin aceptación: el precio se quedó dentro del P12"
    if S["bias"] == 0:
        for k, t in (("nightOk", "filtro de la noche"), ("btcOk", "filtro BTC"), ("moOk", "filtro Midnight Open"), ("wOk", "filtro de ancho del P12")):
            if not S[k]:
                return "Lo bloquea el " + t
        return "Filtro activo"
    return "La apertura no coincide con el P12 (regla 2): descartado"


def entry_rule(S):
    f = lambda x: fp(S["sym"], x)
    if ENTRY_MODE == "LIMIT":
        lim = S["zEdge"] + LIM_FRAC * (S["zDeep"] - S["zEdge"])
        return f"Límite en <code>{f(lim)}</code> · SL <code>{f(S['stop'])}</code>"
    col = "VERDE por encima de" if S["bias"] == 1 else "ROJA por debajo de"
    return f"Entra si una vela 5m toca la zona y cierra {col} <code>{f(S['zDeep'])}</code> · SL <code>{f(S['stop'])}</code>"


def status(S):
    sym, td = S["sym"], S["td"]
    f = lambda x: fp(sym, x)
    nowm = nymin(S["last_t"]) if S["last_t"] else 0
    if S["pos"]:
        p = S["pos"]
        return (("▲ EN LARGO" if p["dir"] == 1 else "▼ EN CORTO") + " · DEJA CORRER",
                f"Entrada {f(p['ent'])} · SL {f(p['sl'])} · TP {f(p['tp'])} · cierre forzado {loc(td, EXITM)}")
    if S["trade"]:
        t = S["trade"]
        return ("OPERACIÓN DEL DÍA HECHA", f"{t['why']} {t['R']:+.2f}R · nueva lectura mañana {loc(td, P12E)}–{loc(td, READE)}")
    if not S["scenDone"]:
        return ("FORMANDO EL P12 · ESPERA", f"Rango nocturno hasta las {loc(td, P12E)} · lectura {loc(td, P12E)}–{loc(td, READE)}")
    if not S["readDone"]:
        st = "ya ha ACEPTADO arriba" if S["accH"] else "ya ha ACEPTADO abajo" if S["accL"] else \
            "solo anuncia (mechas)" if (S["annH"] or S["annL"]) else "dentro del rango"
        return ("LECTURA EN CURSO · NO ENTRES", f"Hasta las {loc(td, READE)}: {st} · {progress(S)}")
    if not S["openDone"]:
        if S["rd"] == 0:
            return ("✋ HOY NO SE OPERA", why_txt(S))
        z = S["pH"] if S["rd"] == 1 else S["pL"]
        return (("ACEPTÓ ARRIBA" if S["rd"] == 1 else "ACEPTÓ ABAJO") + f" · ESPERA A LAS {loc(td, OPENM)}",
                f"A la apertura se confirma · zona prevista {f(z)} – {f(S['pM'])}")
    if S["bias"] == 0 or S["szMult"] == 0:
        return ("✋ HOY NO SE OPERA", why_txt(S))
    if S["dead"]:
        return ("✋ NO OPERAR · TESIS ROTA", "El precio cerró al otro lado del stop antes de entrar")
    d = S["bias"]
    if in_(nowm, OPENM, ENTE):
        size = "" if S["coinc"] else f" · tamaño ×{S['szMult']:g}"
        if S["limit"] is not None:
            return ("⏳ ORDEN LÍMITE PUESTA", f"Límite {f(S['limit'])} · SL {f(S['stop'])} · hasta {loc(td, ENTE)}{size}")
        touching = S["last_l"] <= S["zEdge"] if d == 1 else S["last_h"] >= S["zEdge"]
        if touching:
            return ("⚡ PRECIO EN LA ZONA", entry_rule(S) + size)
        return ("BUSCA COMPRAS · ESPERA RETROCESO" if d == 1 else "BUSCA VENTAS · ESPERA REBOTE",
                f"Zona {f(S['zEdge'])} – {f(S['zDeep'])} · SL {f(S['stop'])} · hasta {loc(td, ENTE)}{size}")
    return ("VENTANA CERRADA SIN ENTRADA", f"No volvió a la zona · nueva lectura mañana {loc(td, P12E)}")


def checks_line(S):
    a = [f"Noche {scen_txt(S)}", read_txt(S)]
    d = S["bias"] or S["rd"]
    if not S["isBtc"] and d:
        b = S["btcRd"]
        a.append("BTC " + ("✓" if b == d else "✗" if b == -d else "·") + ("" if BTC_FILTER != "OFF" else " (info)"))
    if S["moPx"] is not None and S["openDone"]:
        a.append("MO " + ("✓" if S["moFav"] else "✗"))
    if S["wRatio"]:
        a.append(f"P12 {WB[S['wB']]} {S['wRatio']:.2f}×")
    if S["openDone"] and S["bias"]:
        a.append("apertura " + ("✓" if S["coinc"] else f"✗ ×{S['szMult']:g}"))
    return " · ".join(a)


def card_text(S):
    sym, td = S["sym"], S["td"]
    f = lambda x: fp(sym, x)
    L = [f"<b>P12 · {sym}</b>  ·  {DOW[td.weekday()]} {td:%d/%m}"]
    if S["scenDone"]:
        w = f" · ancho {S['wRatio']:.2f}× ({WB[S['wB']]})" if S["wRatio"] else ""
        L.append(f"🌙 Noche: <b>{scen_txt(S)}</b>{w}")
        L.append(f"📐 H <code>{f(S['pH'])}</code> · M <code>{f(S['pM'])}</code> · L <code>{f(S['pL'])}</code>")
        L.append(f"🔎 Lectura {loc(td, P12E)}–{loc(td, READE)}: <b>{read_txt(S) if S['readDone'] else 'en curso'}</b>"
                 + ("" if S["readDone"] else f" · {progress(S)}"))
    if S["readDone"] and not S["isBtc"]:
        b = S["btcRd"]
        L.append("₿ BTC a las " + loc(td, READE) + ": " + ("encima" if b == 1 else "debajo" if b == -1 else "dentro") + " de su P12")
    if S["moPx"] is not None:
        L.append(f"🕛 Midnight Open <code>{f(S['moPx'])}</code>" + ((" · " + ("a favor" if S["moFav"] else "en contra")) if S["openDone"] and S["rd"] else ""))
    if S["openDone"] and S["bias"] and S["szMult"] > 0:
        L.append(f"🎯 Zona <code>{f(S['zEdge'])}</code> – <code>{f(S['zDeep'])}</code> · SL <code>{f(S['stop'])}</code> · apertura "
                 + ("coincide" if S["coinc"] else f"no coincide ×{S['szMult']:g}"))
    h, d = status(S)
    L += ["", f"▶ <b>{h}</b>", d]
    if S["incomplete"]:
        L.append("⚠️ <i>histórico incompleto para hoy: sin señales</i>")
    return "\n".join(L)


def zone_text(S):
    sym, td = S["sym"], S["td"]
    f = lambda x: fp(sym, x)
    head = "🟢 <b>ZONA DE COMPRA</b>" if S["bias"] == 1 else "🔴 <b>ZONA DE VENTA</b>"
    return "\n".join([
        f"{head} · <b>{sym}</b>",
        f"Zona <code>{f(S['zEdge'])}</code> – <code>{f(S['zDeep'])}</code> · SL <code>{f(S['stop'])}</code>",
        entry_rule(S),
        f"Ventana {loc(td, OPENM)}–{loc(td, ENTE)} · cierre forzado {loc(td, EXITM)}" + ("" if S["coinc"] else f" · tamaño ×{S['szMult']:g}"),
        f"<i>{checks_line(S)}</i>"])


def entry_text(S, e, fresh, note):
    sym = S["sym"]
    f = lambda x: fp(sym, x)
    ent, rU = e["ent"], e["rU"]
    head = "▲ <b>COMPRA</b>" if e["dir"] == 1 else "▼ <b>VENTA</b>"
    L = [f"{head} · <b>{sym}</b>" + ("" if fresh else "  ⏱ <i>tardía: no ejecutar</i>"),
         f"Entrada <code>{f(ent)}</code>",
         f"SL <code>{f(e['sl'])}</code>  (−1R · {rU / ent * 100:.2f}%)",
         f"TP <code>{f(e['tp'])}</code>  (+{abs(e['tp'] - ent) / rU:.2f}R)",
         f"Coste {S['costR']:.2f}R · riesgo {RISK_PCT:g}% × {S['szMult']:g}" + ("" if S["coinc"] else " (apertura no coincide)"),
         f"Cierre forzado {loc(S['td'], EXITM)}" + (f" · BE a +{BE_R:g}R" if BE_R > 0 else ""),
         f"<i>{checks_line(S)}</i>"]
    if note:
        L.append(note)
    return "\n".join(L)


def exit_text(S, e, note):
    ico = {"TP": "✅", "SL": "❌", "BE": "🔒"}.get(e["why"], "■")
    L = [f"{ico} <b>{e['why']} {e['R']:+.2f}R</b> · {S['sym']}",
         f"{fp(S['sym'], e['ent'])} → {fp(S['sym'], e['exit'])} · {loc_t(e['t_in'])}→{loc_t(e['t_out'])}"
         + (f" · funding ×{e['nf']}" if e["nf"] else ""),
         f"<i>Acumulado: {agg([t['R'] for t in STATE['trades']])}</i>"]
    if note:
        L.append(note)
    return "\n".join(L)


def agg(rs):
    n = len(rs)
    if n == 0:
        return "—"
    E = sum(rs) / n
    s = f"n {n} · {100 * sum(1 for r in rs if r > 0) / n:.0f}% · E {E:+.2f}R · Σ {sum(rs):+.1f}R"
    if n > 2:
        sd = statistics.stdev(rs)
        if sd > 0:
            t = E / (sd / math.sqrt(n))
            s += f" · t {t:+.2f}" + (" ✓" if abs(t) >= 3 else "")
    return s + (" ⚠️n<" + str(MIN_N) if n < MIN_N else "")


def stats_text():
    T = STATE["trades"]
    if not T:
        return "📊 Sin operaciones cerradas todavía."
    R = lambda cond: [t["R"] for t in T if cond(t)]
    cut = str((datetime.now(TZ_NY) - timedelta(days=30)).date())
    L = ["📊 <b>Estadística P12</b> · R netos de coste y funding",
         "<b>Total</b> " + agg(R(lambda t: True)),
         "<b>30 días</b> " + agg(R(lambda t: t["td"] >= cut)), ""]
    for s in sorted({t["sym"] for t in T}):
        L.append(f"• {s}: " + agg(R(lambda t, s=s: t["sym"] == s)))
    L += ["", "<b>Desgloses</b>",
          "Apertura coincide: " + agg(R(lambda t: t["coinc"])),
          "Apertura no coincide: " + agg(R(lambda t: not t["coinc"])),
          "BTC alineado: " + agg(R(lambda t: t["btc"] == 1)),
          "BTC en contra: " + agg(R(lambda t: t["btc"] == -1)),
          "MO a favor: " + agg(R(lambda t: t["mo"] is True)),
          "MO en contra: " + agg(R(lambda t: t["mo"] is False))]
    for k in range(3):
        L.append(f"P12 {WB[k]}: " + agg(R(lambda t, k=k: t["wB"] == k)))
    L.append(f"<i>Activa un filtro solo si separa con n ≥ {MIN_N} en ambos lados</i>")
    return "\n".join(L)


def estado_text():
    L = [f"<b>P12 · estado</b> · {MODE}" + (" DRY_RUN" if LIVE and DRY_RUN else "") + (" · ⏸ PAUSADO" if STATE["paused"] else "")]
    for s in SYMBOLS:
        S = LAST.get(s)
        if not S:
            L.append(f"• {s}: sin datos")
        elif S.get("weekend"):
            L.append(f"• {s}: fin de semana · no se opera")
        else:
            h, d = status(S)
            L.append(f"• <b>{s}</b> — {h}\n   {d}")
    if LIVE:
        L.append("Posiciones del bot: " + (", ".join(STATE["live"]) or "ninguna"))
    return "\n".join(L)


def digest_text(td):
    L = [f"📊 <b>Resumen P12 · {DOW[td.weekday()]} {td:%d/%m}</b>"]
    for s in SYMBOLS:
        S = LAST.get(s)
        if not S or S.get("weekend") or S["td"] != td:
            continue
        if S["trade"]:
            r = f"{S['trade']['why']} <b>{S['trade']['R']:+.2f}R</b>"
        elif S["bias"] and S["szMult"] > 0:
            r = "tesis rota" if S["dead"] else "sin entrada"
        else:
            r = "no se opera · " + why_txt(S) if S["readDone"] else "—"
        L.append(f"• <b>{s}</b> {scen_txt(S)} · {read_txt(S)} → {r}")
    L.append(f"<i>Acumulado: {agg([t['R'] for t in STATE['trades']])}</i>")
    return "\n".join(L)


HELP = ("<b>P12 Hunter bot</b>\n/estado — qué hacer ahora en cada símbolo\n/hoy [SÍMBOLO] — tarjeta + gráfico\n"
        "/stats — expectativa, t y desgloses\n/pausa — no ejecutar entradas nuevas (LIVE)\n/reanuda — reanudar")


# ───────────────────────── GRÁFICO ─────────────────────────
def chart_png(S):
    if not TG_CHARTS:
        return None
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.patches import Rectangle
    except Exception:
        return None
    try:
        td = S["td"]
        seg = [b for b in (S.get("bars") or []) if b.t >= ny_ms(td, P12S) - 2 * 3_600_000]
        if len(seg) < 10:
            return None
        k_of = {b.t: k for k, b in enumerate(seg)}
        n = len(seg)
        BG, FG, GR, UP, DN, PK = "#131722", "#d1d4dc", "#2a2e39", "#26a69a", "#ef5350", "#ec4899"
        fig, ax = plt.subplots(figsize=(10, 5.4), dpi=110)
        fig.patch.set_facecolor(BG)
        ax.set_facecolor(BG)
        rng = max(b.h for b in seg) - min(b.l for b in seg)
        for k, b in enumerate(seg):
            c = UP if b.c >= b.o else DN
            ax.vlines(k, b.l, b.h, color=c, lw=0.6)
            ax.add_patch(Rectangle((k - 0.35, min(b.o, b.c)), 0.7, max(abs(b.c - b.o), rng * 0.002), color=c, lw=0))
        ys = [b.h for b in seg] + [b.l for b in seg]
        k0, k1 = k_of.get(ny_ms(td, P12S)), k_of.get(ny_ms(td, P12E))
        if S["scenDone"] and k0 is not None:
            k1 = k1 if k1 is not None else n - 1
            ax.add_patch(Rectangle((k0 - 0.5, S["pL"]), k1 - k0, S["pH"] - S["pL"], facecolor=PK, alpha=0.07, edgecolor=PK, lw=0.8))
            for y, ls in ((S["pH"], "-"), (S["pM"], "--"), (S["pL"], "-")):
                ax.hlines(y, k1 - 0.5, n - 0.5, colors=PK, linestyles=ls, lw=1)
            kr = k_of.get(ny_ms(td, READE))
            ax.axvspan(k1 - 0.5, (kr if kr is not None else n) - 0.5, color="gray", alpha=0.08, lw=0)
        if S["moPx"] is not None:
            ax.axhline(S["moPx"], color="gray", ls=":", lw=0.7)
        if S["openDone"] and S["bias"] and S["szMult"] > 0:
            ko = k_of.get(ny_ms(td, OPENM))
            if ko is not None:
                zc = UP if S["bias"] == 1 else DN
                lo, hi = sorted((S["zEdge"], S["zDeep"]))
                ke = k_of.get(ny_ms(td, ENTE), n - 1)
                ax.add_patch(Rectangle((ko - 0.5, lo), ke - ko + 1, hi - lo, facecolor=zc, alpha=0.16, lw=0))
                ax.hlines(S["stop"], ko - 0.5, n - 0.5, colors=DN, linestyles=":", lw=1)
                ys.append(S["stop"])
        tr = S["pos"] or S["trade"]
        if tr:
            ki = k_of.get((tr.get("t") or tr.get("t_in")) - BAR_MS, n - 1)
            ax.hlines(tr["ent"], ki, n - 0.5, colors=FG, lw=1)
            ax.hlines(tr["sl"], ki, n - 0.5, colors=DN, lw=1.6)
            ax.hlines(tr["tp"], ki, n - 0.5, colors=UP, lw=1.6)
            ax.plot(ki, tr["ent"], marker="^" if tr["dir"] == 1 else "v", color=UP if tr["dir"] == 1 else DN, ms=10)
            ys += [tr["sl"], tr["tp"]]
            if S["trade"]:
                kx = k_of.get(S["trade"]["t_out"] - BAR_MS, n - 1)
                ax.plot(kx, S["trade"]["exit"], marker="x", color=FG, ms=9)
        pad = (max(ys) - min(ys)) * 0.04
        ax.set_ylim(min(ys) - pad, max(ys) + pad)
        ax.set_xlim(-1, n + 1)
        ticks = [k for k in range(n) if (seg[k].t // BAR_MS) % 24 == 0]
        ax.set_xticks(ticks)
        ax.set_xticklabels([loc_t(seg[k].t) for k in ticks])
        ax.yaxis.tick_right()
        ax.tick_params(colors=FG, labelsize=8)
        for sp in ax.spines.values():
            sp.set_color(GR)
        ax.grid(color=GR, lw=0.5)
        ax.set_title(f"{S['sym']} · P12 · {DOW[td.weekday()]} {td:%d/%m} · hora {TZ_LOC.key}", color=FG, fontsize=10, loc="left")
        buf = io.BytesIO()
        fig.savefig(buf, format="png", facecolor=BG, bbox_inches="tight")
        plt.close(fig)
        return buf.getvalue()
    except Exception as ex:
        log.warning(f"gráfico {S['sym']}: {ex}")
        return None


# ───────────────────────── TELEGRAM ─────────────────────────
class TG:
    def __init__(self):
        self.on = bool(TG_TOKEN and TG_CHAT)
        self.url = f"https://api.telegram.org/bot{TG_TOKEN}/"
        self.ses = requests.Session()
        self.lock = threading.Lock()
        self.last = 0.0
        self.gap = 3.1 if TG_CHAT.startswith("-") else 1.1   # grupo/canal ≈20/min · privado ≈1/s

    def call(self, method, data, files=None):
        if not self.on:
            return None
        for a in range(4):
            with self.lock:
                w = self.last + self.gap - time.time()
                if w > 0:
                    time.sleep(w)
                try:
                    r = self.ses.post(self.url + method, data=data, files=files, timeout=30)
                except Exception as ex:
                    r = None
                    log.warning(f"TG {method}: {ex}")
                self.last = time.time()
            if r is None:
                time.sleep(2 + 2 * a)
                continue
            try:
                j = r.json()
            except Exception:
                j = {}
            if j.get("ok"):
                return j.get("result")
            desc = str(j.get("description", ""))
            if r.status_code == 429:
                ra = int((j.get("parameters") or {}).get("retry_after", 5))
                log.warning(f"TG 429: espera {ra}s")
                time.sleep(ra + 1)
                continue
            if "not modified" in desc:
                return True
            if r.status_code >= 500:
                time.sleep(2 + 2 * a)
                continue
            log.warning(f"TG {method} {r.status_code}: {desc}")
            return None
        return None

    def _base(self, silent, reply, kb):
        d = {"chat_id": TG_CHAT, "parse_mode": "HTML", "disable_notification": "true" if silent else "false"}
        if TG_THREAD:
            d["message_thread_id"] = TG_THREAD
        if reply:
            d["reply_parameters"] = json.dumps({"message_id": reply, "allow_sending_without_reply": True})
        if kb:
            d["reply_markup"] = json.dumps({"inline_keyboard": kb})
        return d

    def send(self, text, silent=False, reply=None, kb=None):
        d = self._base(silent, reply, kb)
        d["text"] = text[:4096]
        d["link_preview_options"] = json.dumps({"is_disabled": True})
        r = self.call("sendMessage", d)
        return r.get("message_id") if isinstance(r, dict) else None

    def photo(self, png, caption, silent=False, reply=None, kb=None):
        if not png or len(caption) > 1024:
            return self.send(caption, silent, reply, kb)
        d = self._base(silent, reply, kb)
        d["caption"] = caption
        r = self.call("sendPhoto", d, files={"photo": ("p12.png", png, "image/png")})
        if isinstance(r, dict):
            return r.get("message_id")
        return self.send(caption, silent, reply, kb)

    def edit(self, mid, text, kb=None):
        d = {"chat_id": TG_CHAT, "message_id": mid, "text": text[:4096], "parse_mode": "HTML",
             "link_preview_options": json.dumps({"is_disabled": True})}
        if kb:
            d["reply_markup"] = json.dumps({"inline_keyboard": kb})
        return self.call("editMessageText", d)

    def set_commands(self):
        cmds = [("estado", "Qué hacer ahora"), ("hoy", "Tarjeta y gráfico del día"), ("stats", "Estadística"),
                ("pausa", "No ejecutar entradas nuevas"), ("reanuda", "Reanudar ejecución"), ("ayuda", "Ayuda")]
        self.call("setMyCommands", {"commands": json.dumps([{"command": c, "description": d} for c, d in cmds])})

    def poll(self, handler):
        ses, off, warned = requests.Session(), None, False
        while True:
            try:
                p = {"timeout": 50, "allowed_updates": json.dumps(["message"])}
                if off:
                    p["offset"] = off
                r = ses.get(self.url + "getUpdates", params=p, timeout=65)
                j = r.json()
                if not j.get("ok"):
                    if r.status_code == 409 and not warned:
                        log.warning("TG 409: otro proceso lee este token (usa un token propio para este bot). Comandos desactivados.")
                        warned = True
                    time.sleep(60 if r.status_code == 409 else 5)
                    continue
                for u in j["result"]:
                    off = u["update_id"] + 1
                    m = u.get("message") or {}
                    chat = str((m.get("chat") or {}).get("id", ""))
                    uid = str((m.get("from") or {}).get("id", ""))
                    txt = (m.get("text") or "").strip()
                    if not txt.startswith("/") or (chat != TG_CHAT and uid not in TG_ADMINS):
                        continue
                    parts = txt.split()
                    try:
                        handler(parts[0].split("@")[0].lower(), parts[1:])
                    except Exception:
                        log.exception("comando")
            except Exception as ex:
                log.warning(f"poll: {ex}")
                time.sleep(5)


def buttons(sym):
    s = sym.replace("-", "")
    return [[{"text": "📈 TradingView", "url": f"https://www.tradingview.com/chart/?symbol=BINGX:{s}.P"},
             {"text": "BingX", "url": f"https://bingx.com/en/perpetual/{sym}/"}]]


# ───────────────────────── BINGX (LIVE) ─────────────────────────
class BingX:
    def __init__(self):
        self.ses = requests.Session()
        self.hedge = None

    def req(self, method, path, params=None):
        p = {k: v for k, v in (params or {}).items() if v is not None}
        p["timestamp"] = int(time.time() * 1000)
        p["recvWindow"] = 5000
        qs = urlencode(sorted(p.items()))              # mismo string firmado y enviado
        qs += "&signature=" + hmac.new(BX_SECRET.encode(), qs.encode(), hashlib.sha256).hexdigest()
        h = {"X-BX-APIKEY": BX_KEY}
        if method == "POST":
            h["Content-Type"] = "application/x-www-form-urlencoded"
            r = self.ses.post(BX_BASE + path, data=qs, headers=h, timeout=15)
        else:
            r = self.ses.request(method, BX_BASE + path + "?" + qs, headers=h, timeout=15)
        j = r.json()
        if j.get("code", 0) != 0:
            raise RuntimeError(f"BingX {path} {j.get('code')}: {j.get('msg')}")
        return j.get("data")

    def is_hedge(self):
        if self.hedge is None:
            try:
                d = self.req("GET", "/openApi/swap/v1/positionSide/dual") or {}
                self.hedge = str(d.get("dualSidePosition")).lower() == "true"
            except Exception as ex:
                log.warning(f"modo de posición no detectado ({ex}); asumo Hedge")
                self.hedge = True
        return self.hedge

    def pside(self, d):
        return ("LONG" if d == 1 else "SHORT") if self.is_hedge() else "BOTH"

    def equity(self):
        d = self.req("GET", "/openApi/swap/v2/user/balance")
        b = d.get("balance", d) if isinstance(d, dict) else d
        if isinstance(b, list):
            b = next((x for x in b if x.get("asset") == "USDT"), b[0] if b else {})
        return float(b.get("equity") or b.get("balance") or 0)

    def positions(self, sym):
        out = []
        for x in self.req("GET", "/openApi/swap/v2/user/positions", {"symbol": sym}) or []:
            amt = float(x.get("positionAmt", 0) or 0)
            if amt != 0:
                out.append(x)
        return out

    def position(self, sym, d):
        for x in self.positions(sym):
            amt = float(x["positionAmt"])
            ps = x.get("positionSide", "BOTH")
            if self.is_hedge() and ps != ("LONG" if d == 1 else "SHORT"):
                continue
            if not self.is_hedge() and (amt > 0) != (d == 1):
                continue
            return dict(amt=abs(amt), avg=float(x.get("avgPrice") or x.get("entryPrice") or 0))
        return None

    def leverage(self, sym):
        for s in (["LONG", "SHORT"] if self.is_hedge() else ["BOTH"]):
            try:
                self.req("POST", "/openApi/swap/v2/trade/leverage", {"symbol": sym, "side": s, "leverage": LEVERAGE})
            except Exception as ex:
                log.warning(f"{sym} apalancamiento {s}: {ex}")

    def order(self, sym, side, pside, otype, qty, stop=None, reduce=False):
        p = {"symbol": sym, "side": side, "positionSide": pside, "type": otype, "quantity": qty}
        if stop is not None:
            p["stopPrice"], p["workingType"] = stop, "MARK_PRICE"
        if reduce and pside == "BOTH":
            p["reduceOnly"] = "true"
        d = self.req("POST", "/openApi/swap/v2/trade/order", p) or {}
        return (d.get("order") or {}).get("orderId") if isinstance(d, dict) else None

    def cancel(self, sym, oid):
        if oid:
            try:
                self.req("DELETE", "/openApi/swap/v2/trade/order", {"symbol": sym, "orderId": oid})
            except Exception as ex:
                log.warning(f"{sym} cancelar {oid}: {ex}")

    def cancel_all(self, sym):
        try:
            self.req("DELETE", "/openApi/swap/v2/trade/allOpenOrders", {"symbol": sym})
        except Exception as ex:
            log.warning(f"{sym} cancelar todas: {ex}")


BX = BingX()


def fpx(sym, x):
    return f"{x:.{PREC.get(sym, {}).get('p', 4)}f}"


def live_open(sym, S, e):
    if not LIVE:
        return ""
    if STATE["paused"]:
        return "⏸ <i>pausado: no se ejecuta</i>"
    if sym in STATE["live"]:
        return "⚠️ ya hay posición del bot en este símbolo"
    if len(STATE["live"]) >= MAX_POS:
        return f"⚠️ máximo de posiciones ({MAX_POS}): no se ejecuta"
    d, stop = e["dir"], e["sl"]
    if DRY_RUN:
        STATE["live"][sym] = dict(dry=True, dir=d, td=str(S["td"]))
        return "🧪 <i>DRY_RUN: orden simulada</i>"
    try:
        if BX.positions(sym):
            return "⚠️ ya hay una posición abierta en BingX (manual u otro bot): no se ejecuta"
        eq = BX.equity()
        px = S["last_c"]
        q = min(eq * RISK_PCT / 100 / abs(px - stop) * S["szMult"], eq * LEVERAGE * 0.95 / px)
        qs = fq(sym, q)
        pr = PREC.get(sym, {})
        if float(qs) <= 0 or float(qs) < pr.get("minq", 0) or float(qs) * px < pr.get("minusdt", 0):
            return f"⚠️ tamaño {qs} bajo el mínimo del contrato: no se ejecuta"
        BX.leverage(sym)
        BX.cancel_all(sym)                               # huérfanas de redeploys
        ps = BX.pside(d)
        BX.order(sym, "BUY" if d == 1 else "SELL", ps, "MARKET", qs)
        time.sleep(1.5)
        p = BX.position(sym, d)
        if not p:
            return "🚨 orden enviada pero la posición no aparece: revisa BingX"
        avg, amt = p["avg"], fq(sym, p["amt"])
        rUf = abs(avg - stop)
        tX = S["zEdge"] + d * EXT_K * (S["pH"] - S["pL"])
        tp = avg + d * RR * rUf if TGT_MODE == "R" or (tX - avg) * d <= 0.5 * rUf else tX
        cs = "SELL" if d == 1 else "BUY"
        try:
            sl_id = BX.order(sym, cs, ps, "STOP_MARKET", amt, stop=fpx(sym, stop), reduce=True)
        except Exception as ex:
            BX.order(sym, cs, ps, "MARKET", amt, reduce=True)
            BX.cancel_all(sym)
            return f"🚨 SL rechazado ({esc(ex)}): posición cerrada"
        warn = ""
        try:
            tp_id = BX.order(sym, cs, ps, "TAKE_PROFIT_MARKET", amt, stop=fpx(sym, tp), reduce=True)
        except Exception as ex:
            tp_id, warn = None, f"\n⚠️ TP no colocado: {esc(ex)}"
        STATE["live"][sym] = dict(dry=False, dir=d, amt=amt, avg=avg, sl=stop, tp=tp, sl_id=sl_id, tp_id=tp_id, td=str(S["td"]))
        return f"✅ <b>EJECUTADA</b> {amt} @ <code>{fp(sym, avg)}</code> · TP real <code>{fp(sym, tp)}</code>{warn}"
    except Exception as ex:
        log.exception("live_open")
        return f"🚨 error al ejecutar: {esc(ex)[:200]}"


def live_be(sym):
    L = STATE["live"].get(sym)
    if not L or L.get("dry"):
        return ""
    try:
        BX.cancel(sym, L.get("sl_id"))
        ps, cs = BX.pside(L["dir"]), "SELL" if L["dir"] == 1 else "BUY"
        L["sl_id"] = BX.order(sym, cs, ps, "STOP_MARKET", L["amt"], stop=fpx(sym, L["avg"]), reduce=True)
        L["sl"] = L["avg"]
        return "SL en BingX movido a la entrada"
    except Exception as ex:
        return f"🚨 BE no aplicado en BingX: {esc(ex)[:150]}"


def live_close(sym, why):
    L = STATE["live"].get(sym)
    if not L:
        return ""
    if L.get("dry"):
        STATE["live"].pop(sym, None)
        return "🧪 DRY: cerrada"
    try:
        p = BX.position(sym, L["dir"])
        if p:
            BX.order(sym, "SELL" if L["dir"] == 1 else "BUY", BX.pside(L["dir"]), "MARKET", fq(sym, p["amt"]), reduce=True)
            msg = f"BingX: cerrada a mercado ({why})"
        else:
            msg = "BingX: ya cerrada por SL/TP"
        BX.cancel_all(sym)
    except Exception as ex:
        return f"🚨 error al cerrar en BingX: {esc(ex)[:150]}"
    STATE["live"].pop(sym, None)
    return msg


def reconcile(sym):
    L = STATE["live"].get(sym)
    if not L or L.get("dry") or not LIVE or DRY_RUN:
        return
    try:
        if not BX.position(sym, L["dir"]):
            BX.cancel_all(sym)
            STATE["live"].pop(sym, None)
            D = STATE["days"].get(sym) or {}
            tg.send(f"ℹ️ {sym}: posición cerrada en BingX (SL/TP) · órdenes restantes canceladas", silent=True, reply=D.get("entry"))
    except Exception as ex:
        log.warning(f"{sym} reconcile: {ex}")


# ───────────────────────── ESTADO EN DISCO ─────────────────────────
STATE = {"days": {}, "trades": [], "paused": False, "live": {}, "digest": []}
STATE_LOCK = threading.Lock()
LAST = {}
HEALTH = {"version": CODE_VERSION, "mode": MODE, "last_cycle": None}


def load_state():
    try:
        with open(SF) as fh:
            STATE.update(json.load(fh))
        log.info(f"estado cargado: {len(STATE['trades'])} ops, live={list(STATE['live'])}")
    except FileNotFoundError:
        pass
    except Exception as ex:
        log.warning(f"estado ilegible: {ex}")


def save_state():
    with STATE_LOCK:
        STATE["digest"] = STATE["digest"][-60:]
        STATE["trades"] = STATE["trades"][-3000:]
        tmp = SF + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(STATE, fh)
        os.replace(tmp, SF)


def day_state(sym, td):
    D = STATE["days"].get(sym)
    if not D or D.get("td") != str(td):
        D = dict(td=str(td), sent=[], card=None, card_h=None, entry=None, zone=None)
        STATE["days"][sym] = D
    return D


def record_trade(sym, S, e):
    key = f"{sym}@{S['td']}"
    if any(t["key"] == key for t in STATE["trades"][-500:]):
        return
    STATE["trades"].append(dict(key=key, sym=sym, td=str(S["td"]), dir=e["dir"], R=round(e["R"], 4), why=e["why"],
                                coinc=S["coinc"], btc=0 if S["isBtc"] else S["btcRd"] * e["dir"],
                                mo=None if S["moPx"] is None else S["moFav"], wB=S["wB"]))


# ───────────────────────── CICLO ─────────────────────────
tg = TG()


def update_card(sym, S, D):
    txt = card_text(S)
    h = hashlib.md5(txt.encode()).hexdigest()
    if D["card"] and D["card_h"] == h:
        return
    if D["card"] and tg.edit(D["card"], txt, buttons(sym)):
        D["card_h"] = h
        return
    mid = tg.send(txt, silent=True, kb=buttons(sym))
    if mid:
        D["card"], D["card_h"] = mid, h


def handle(sym, S, e, fresh, D):
    k = e["k"]
    if k == "read" and fresh and S["rd"] != 0:
        tg.send(f"🔎 <b>{sym}</b> · {read_txt(S)} → se confirma a las {loc(S['td'], OPENM)}", silent=True, reply=D["card"])
    elif k == "open" and fresh:
        if S["bias"] and S["szMult"] > 0:
            D["zone"] = tg.photo(chart_png(S), zone_text(S), silent=False, reply=D["card"], kb=buttons(sym))
        else:
            tg.send(f"✋ <b>{sym}</b> · hoy no se opera · {why_txt(S)}", silent=True, reply=D["card"])
    elif k == "touch" and fresh and TG_TOUCH and not any(x["k"] == "entry" and x["t"] == e["t"] for x in S["events"]):
        tg.send(f"⚡ <b>{sym}</b> · precio en la zona\n{entry_rule(S)}", silent=False, reply=D["zone"] or D["card"])
    elif k == "limit" and fresh:
        tg.send(f"⏳ <b>{sym}</b> · orden límite <code>{fp(sym, e['lim'])}</code> · SL <code>{fp(sym, S['stop'])}</code> · válida hasta {loc(S['td'], ENTE)}",
                silent=False, reply=D["zone"] or D["card"])
    elif k == "entry":
        note = live_open(sym, S, e) if fresh else ""
        D["entry"] = tg.photo(chart_png(S), entry_text(S, e, fresh, note), silent=not fresh, reply=D["zone"] or D["card"], kb=buttons(sym))
    elif k == "be" and fresh:
        note = live_be(sym) if LIVE else ""
        tg.send(f"🔒 <b>{sym}</b> · SL a breakeven <code>{fp(sym, e['sl'])}</code>" + (f"\n{note}" if note else ""), silent=True, reply=D["entry"])
    elif k == "exit":
        record_trade(sym, S, e)
        note = live_close(sym, e["why"]) if LIVE else ""
        tg.send(exit_text(S, e, note), silent=not fresh, reply=D["entry"])
    elif k == "dead" and fresh:
        tg.send(f"✋ <b>{sym}</b> · tesis rota: cerró al otro lado del stop antes de entrar", silent=True, reply=D["zone"] or D["card"])
    elif k == "reject" and fresh:
        v = f"{e['val']:.2f}R > {MAX_COST_R:g}R" if e["why"] == "coste" else f"{e['val']:.2f}% > {MAX_STOP_PCT:g}%"
        tg.send(f"⚠️ <b>{sym}</b> · señal descartada por {e['why']} ({v}); sigue buscando", silent=True, reply=D["zone"] or D["card"])
    elif k == "window" and fresh:
        tg.send(f"⌛ <b>{sym}</b> · ventana cerrada sin entrada", silent=True, reply=D["zone"] or D["card"])


def process(sym, end_ms, btc_bars):
    bars = klines(sym, "5m", KL_LIMIT, end_ms)
    if bars and bars[-1].t != end_ms - BAR_MS:
        time.sleep(3)
        bars = klines(sym, "5m", KL_LIMIT, end_ms)
    if len(bars) < 300:
        log.warning(f"{sym}: pocas velas ({len(bars)})")
        return
    td = tdate(bars[-1].t)
    if SKIP_WE and td.weekday() >= 5:
        LAST[sym] = dict(sym=sym, td=td, weekend=True)
        return
    is_btc = sym.split("-")[0] == "BTC"
    bb = bars if is_btc else (btc_bars or [])
    S = simulate(sym, bars, td, width_median(sym, td),
                 btc_pos(bb, td, ny_ms(td, READE)), btc_pos(bb, td, end_ms), is_btc)
    S["bars"] = bars
    LAST[sym] = S
    if S["incomplete"]:
        return
    D = day_state(sym, td)
    if S["scenDone"]:
        update_card(sym, S, D)
    for e in S["events"]:
        key = f"{e['k']}@{e['t']}"
        if key in D["sent"]:
            continue
        D["sent"].append(key)
        fresh = end_ms - e["t"] <= STALE_SEC * 1000
        try:
            handle(sym, S, e, fresh, D)
        except Exception:
            log.exception(f"{sym} evento {e['k']}")
    if S["scenDone"]:
        update_card(sym, S, D)
    if LIVE:
        reconcile(sym)


def cycle(end_ms):
    t0 = time.time()
    btc = None
    try:
        btc = klines(BTC_SYMBOL, "5m", KL_LIMIT, end_ms)
    except Exception as ex:
        log.warning(f"BTC referencia: {ex}")
    for s in SYMBOLS:
        try:
            process(s, end_ms, btc)
        except Exception as ex:
            log.warning(f"{s}: {ex}")
    d = ny(end_ms)
    td = tdate(end_ms - BAR_MS)
    if in_(d.hour * 60 + d.minute, DIGEST, P12S) and not (SKIP_WE and td.weekday() >= 5) and str(td) not in STATE["digest"]:
        STATE["digest"].append(str(td))
        tg.send(digest_text(td), silent=True)
    save_state()
    HEALTH["last_cycle"] = datetime.now(timezone.utc).isoformat()
    log.info(f"ciclo {loc_t(end_ms)} · {len(SYMBOLS)} símbolos · {time.time() - t0:.1f}s")


def on_cmd(cmd, args):
    if cmd in ("/estado", "/status"):
        tg.send(estado_text())
    elif cmd == "/stats":
        tg.send(stats_text())
    elif cmd == "/hoy":
        for s in ([_sym(args[0])] if args else SYMBOLS):
            S = LAST.get(s)
            if not S or S.get("weekend") or not S.get("scenDone"):
                tg.send(f"{s}: " + ("fin de semana" if S and S.get("weekend") else "P12 aún formándose / sin datos"))
            else:
                tg.photo(chart_png(S), card_text(S)[:1024], kb=buttons(s))
    elif cmd == "/pausa":
        STATE["paused"] = True
        save_state()
        tg.send("⏸ Pausado: las señales siguen, no se ejecutan entradas nuevas.")
    elif cmd == "/reanuda":
        STATE["paused"] = False
        save_state()
        tg.send("▶️ Reanudado.")
    elif cmd in ("/ayuda", "/help", "/start"):
        tg.send(HELP)


class _H(BaseHTTPRequestHandler):
    def do_GET(self):
        b = json.dumps(HEALTH).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *a):
        pass


def main():
    log.info(f"{CODE_VERSION} · MODE={MODE} DRY_RUN={DRY_RUN} · {SYMBOLS} · estado {SF}")
    load_state()
    load_contracts()
    threading.Thread(target=lambda: HTTPServer(("0.0.0.0", PORT), _H).serve_forever(), daemon=True).start()
    if LIVE and not DRY_RUN:
        if not (BX_KEY and BX_SECRET):
            raise SystemExit("MODE=LIVE con DRY_RUN=false necesita BINGX_API_KEY y BINGX_SECRET_KEY")
        log.info(f"BingX hedge={BX.is_hedge()} equity={BX.equity():.2f}")
    if not tg.on:
        log.warning("Telegram sin configurar: solo logs")
    tg.set_commands()
    td = tdate(int(time.time() * 1000))
    tg.send(f"🤖 <b>P12 Hunter bot</b> · {CODE_VERSION}\nModo <b>{MODE}</b>" + (" · DRY_RUN" if LIVE and DRY_RUN else "")
            + f" · {', '.join(SYMBOLS)}\nP12 {loc(td, P12S)}–{loc(td, P12E)} · lectura hasta {loc(td, READE)} · apertura {loc(td, OPENM)}"
            + f" · entradas hasta {loc(td, ENTE)} (hora {TZ_LOC.key})\n/ayuda", silent=True)
    if TG_COMMANDS and tg.on:
        threading.Thread(target=tg.poll, args=(on_cmd,), daemon=True).start()
    last = 0
    while True:
        try:
            now = time.time()
            bc = int(now // 300 * 300)
            if bc > last and now >= bc + CYCLE_DELAY:
                last = bc
                cycle(bc * 1000)
        except Exception:
            log.exception("ciclo")
        time.sleep(2)


if __name__ == "__main__":
    main()

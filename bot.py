#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════════════════
# P12 HUNTER BOT v1.1 — motor de "P12 Hunter v4.2" (Pine) en Python
# BingX perpetuos 5m → señales Telegram + ejecución (MODE=LIVE)
#
#  Motor: replay determinista del día estadístico (18:00→18:00 NY) a cada cierre
#  de vela 5m con las reglas del Pine. Eventos deduplicados en disco.
#
#  v1.1 (revisión a fondo contra la documentación oficial de BingX):
#   · Velas v3: se aceptan los dos formatos (objeto y array) — la doc oficial
#     documenta arrays; el parser anterior solo leía objetos.
#   · Firma sobre la cadena SIN codificar (requisito BingX para valores JSON);
#     fallback .com → .pro solo ante fallo de red; reloj sincronizado con el
#     servidor; backoff con jitter ante 100410; limitador de datos de mercado
#     (en Railway la IP de salida se comparte con otros).
#   · Entrada idempotente con clientOrderId y SL ADJUNTO a la orden de mercado
#     (nunca hay posición sin stop); BE con cancelReplace atómico; precio real
#     comprobado antes de entrar (no persigue si se escapó); R real desde el
#     flujo de fondos de BingX (PnL + comisiones + funding).
#   · Comisión real (contrato / tu tarifa) y funding HISTÓRICO real con signo.
#   · Riesgo: límite de pérdida diaria en R, pausa por racha de pérdidas,
#     máximo de posiciones en la misma dirección y tamaño reducido para la
#     2ª correlacionada (BTC/ETH/SOL se mueven juntos).
#   · Calendario macro USD (alto impacto): marca las noticias en la lectura y
#     durante la posición; filtro y cierre previo opcionales; desglose en /stats.
#   · Calidad de datos: si faltan velas antes de la apertura, no se opera.
#   · MFE por operación, backtest con el MISMO motor (/backtest), mantenimiento
#     de la API key (BingX borra claves sin IP tras 14 días sin uso), vigilante
#     de ciclo, botón ⛔ Cerrar con confirmación, /riesgo, /cerrar.
# ═══════════════════════════════════════════════════════════════════════════
import os, io, json, time, math, hmac, hashlib, logging, threading, statistics, html, random, bisect
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from urllib.parse import quote
from http.server import BaseHTTPRequestHandler, HTTPServer
from collections import namedtuple
from statistics import NormalDist
import re
import requests

os.environ.setdefault("MPLBACKEND", "Agg")
CODE_VERSION = "P12-BOT 1.3.0 · 2026-10-07 · motor P12 v4.2 + blindaje LIVE"

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
_SYM_RAW = _e("SYMBOLS", "BTC-USDT,ETH-USDT,SOL-USDT")
UNIVERSE = _SYM_RAW.upper() in ("ALL", "TODAS", "*")    # escanear todos los perpetuos USDT de BingX
SYMBOLS = [] if UNIVERSE else [_sym(s) for s in _SYM_RAW.split(",") if s.strip()]
BTC_SYMBOL = _sym(_e("BTC_SYMBOL", "BTC-USDT"))
MIN_VOL_USDT = _f("MIN_VOL_USDT", 20_000_000)           # volumen 24h mínimo para entrar en el escaneo
MAX_UNIVERSE = _i("MAX_UNIVERSE", 300)                   # tope de símbolos escaneados (por volumen)
MAX_SPREAD_PCT = _f("MAX_SPREAD_PCT", 0.05)              # horquilla máxima bid/ask
TOP_N = _i("TOP_N", 15)                                  # símbolos seguidos tras la apertura
EXCLUDE = {_sym(s) for s in _e("EXCLUDE", "").split(",") if s.strip()}

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

NEWS_ON = _b("NEWS", True)
NEWS_CCY = {x.strip().upper() for x in _e("NEWS_CURRENCIES", "USD").split(",") if x.strip()}
NEWS_FILTER = _e("NEWS_FILTER", "OFF").upper()           # OFF | EXCL_READ | EXCL_HOLD | EXCL_ANY
NEWS_FLAT_MIN = _i("NEWS_FLAT_MIN", 0)                   # >0: cerrar N min antes de noticia en la posición
NEWS_URL = _e("NEWS_URL", "https://nfs.faireconomy.media/ff_calendar_thisweek.json")

ENTRY_MODE = _e("ENTRY_MODE", "CONF").upper()            # CONF | LIMIT
LIM_FRAC = _f("LIM_FRAC", 0.5)
ATR_LEN = _i("ATR_LEN", 14)
STOP_BUF = _f("STOP_BUF", 0.25)
TGT_MODE = _e("TGT_MODE", "R").upper()                   # R | EXT
RR = _f("RR", 2.0)
EXT_K = _f("EXT_K", 1.0)
BE_R = _f("BE_R", 0.0)
RISK_PCT = _f("RISK_PCT", 0.5)

COST_RT_RAW = _e("COST_RT", "auto").lower()              # auto | % ida+vuelta
COST_AUTO = COST_RT_RAW == "auto"
COST_RT_NUM = 0.12 if COST_AUTO else _f("COST_RT", 0.12)
SLIP_PCT = _f("SLIP_PCT", 0.02)                          # deslizamiento añadido al coste auto
MAX_COST_R = _f("MAX_COST_R", 0.20)
MAX_STOP_PCT = _f("MAX_STOP_PCT", 2.5)
FUND_H = _i("FUND_H", 8)
FUND_PCT = _f("FUND_PCT", 0.01)                          # solo si no hay histórico real
EXIT_FUND = _b("EXIT_FUND", False)

LEVERAGE = _i("LEVERAGE", 10)
MAX_POS = _i("MAX_POS", 3)
MAX_SAME_DIR = _i("MAX_SAME_DIR", 2)
CORR_SCALE = _f("CORR_SCALE", 0.5)
MAX_DAILY_LOSS_R = _f("MAX_DAILY_LOSS_R", 3.0)
MAX_CONSEC_LOSS = _i("MAX_CONSEC_LOSS", 6)
ENTRY_MAX_SLIP_R = _f("ENTRY_MAX_SLIP_R", 0.30)
MAX_GAPS = _i("MAX_GAPS", 3)
MAX_DD_PCT = _f("MAX_DD_PCT", 8.0)              # caída del equity desde su máximo (%) → pausa hasta /reanuda (0 = off)
MAX_ACCOUNT_POS = _i("MAX_ACCOUNT_POS", 4)      # posiciones de TODA la cuenta (otros bots y manuales también cuentan; 0 = off)
GUARD_STOPS = _b("GUARD_STOPS", True)           # cada ciclo repone el SL/TP de una posición del bot si falta en BingX
MIN_N = _i("MIN_N", 20)
STALE_SEC = _i("STALE_SEC", 240)
CYCLE_DELAY = _i("CYCLE_DELAY", 5)
MD_GAP = _f("MD_GAP", 0.15 if UNIVERSE else 0.5)
BACKTEST_DAYS = _i("BACKTEST_DAYS", 0)
WATCHDOG_MIN = _i("WATCHDOG_MIN", 15)
KL_LIMIT = 700

TG_TOKEN = _e("TELEGRAM_TOKEN", "")
TG_CHAT = _e("TELEGRAM_CHAT_ID", "")
TG_THREAD = _e("TELEGRAM_THREAD_ID", "")
TG_ADMINS = {x.strip() for x in _e("TELEGRAM_ADMIN_IDS", "").split(",") if x.strip()}
TG_COMMANDS = _b("TG_COMMANDS", True)
TG_CHARTS = _b("TG_CHARTS", True)
TG_TOUCH = _b("TG_TOUCH_ALERT", not UNIVERSE)

BX_KEY = _e("BINGX_API_KEY", "")
BX_SECRET = _e("BINGX_SECRET_KEY", "")
BX_BASE = _e("BINGX_BASE", "https://open-api.bingx.com").rstrip("/")
BX_BASES = [BX_BASE] + ([BX_BASE.replace(".com", ".pro")] if BX_BASE.endswith(".com") else [])
MD_BASES = ["https://open-api.bingx.com", "https://open-api.bingx.pro"]
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
DAY_MS = 86_400_000
DOW = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
WB = ["estrecho", "normal", "ancho"]
SCN = {0: "—", 1: "mismo lado", 2: "se contradicen", 3: "ambos lados"}


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


def now_ms():
    return int(time.time() * 1000)


# ───────────────────────── HTTP / DATOS DE MERCADO ─────────────────────────
_TL = threading.local()


def ses():
    s = getattr(_TL, "s", None)
    if s is None:
        s = requests.Session()
        s.headers["User-Agent"] = "p12-bot/1.1"
        _TL.s = s
    return s


class MDBlocked(RuntimeError):
    """109429: BingX bloquea la ruta de velas por exceso de errores; se reintenta cuando ellos digan."""


class MDPaused(RuntimeError):
    """109415: contrato pausado (mercado cerrado / suspendido). No se le piden velas durante horas."""


MD_BLOCK = {"until": 0.0, "alert": 0.0}
PAUSED = {}                 # símbolo → epoch hasta el que no se le piden velas
MD_LOCK = threading.Lock()
MD_LAST = [0.0]
MD_DYN = [MD_GAP]          # separación adaptativa: sube con 100410, baja poco a poco


def md_get(path, params=None):
    """Datos públicos BingX: limitador global adaptativo, backoff ante 100410, .pro solo si falla la red."""
    if path.endswith("/klines") and MD_BLOCK["until"] > time.time():      # no golpear una ruta bloqueada: alarga el bloqueo
        raise MDBlocked(f"{path}: bloqueada por BingX {int(MD_BLOCK['until'] - time.time())}s más")
    for a in range(6):
        with MD_LOCK:
            w = MD_LAST[0] + MD_DYN[0] - time.time()
            if w > 0:
                time.sleep(w)
            MD_LAST[0] = time.time()
        j = None
        for base in MD_BASES:
            try:
                j = ses().get(base + path, params=params, timeout=12).json()
                break
            except (requests.ConnectionError, requests.Timeout):
                continue
            except ValueError:
                break
        if j is None:
            time.sleep(1 + a)
            continue
        if str(j.get("code")) == "109429":
            m = re.search(r"retry after time:\s*(\d+)", str(j.get("msg", "")))
            MD_BLOCK["until"] = (int(m.group(1)) / 1000.0 if m else time.time() + 300) + 2
            raise MDBlocked(f"{path} 109429: rate limit de BingX")
        if str(j.get("code")) == "109415":
            raise MDPaused(f"{path} 109415: {str(j.get('msg', ''))[:80]}")
        if j.get("code") == 100410:
            MD_DYN[0] = min(1.2, MD_DYN[0] * 1.5)
            time.sleep(min(0.2 * 2 ** a, 5) + random.random())
            continue
        MD_DYN[0] = max(MD_GAP, MD_DYN[0] * 0.98)
        if j.get("code", 0) != 0:
            raise RuntimeError(f"{path} {j.get('code')}: {j.get('msg')}")
        return j.get("data")
    raise RuntimeError(f"{path}: sin respuesta")


def _bar(x):
    if isinstance(x, dict):
        return Bar(int(x["time"]), float(x["open"]), float(x["high"]), float(x["low"]), float(x["close"]), float(x.get("volume", 0) or 0))
    return Bar(int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5]) if len(x) > 5 else 0.0)


IV_MS = {"5m": BAR_MS, "1h": 3_600_000}


def klines(sym, interval, limit, end_ms=None, start_ms=None):
    if PAUSED.get(sym, 0.0) > time.time():
        raise MDPaused(f"{sym} pausado")
    p = {"symbol": sym, "interval": interval, "limit": limit}
    if start_ms:
        p["startTime"] = start_ms
    if end_ms and start_ms:
        p["endTime"] = end_ms - 1
    out = {}
    try:
        data = md_get("/openApi/swap/v3/quote/klines", p) or []
    except MDPaused:
        if PAUSED.get(sym, 0.0) <= time.time():
            log.info(f"{sym} pausado en BingX: sin velas durante 6 h")
        PAUSED[sym] = time.time() + 6 * 3600
        raise
    for x in data:
        b = _bar(x)
        out[b.t] = b
    bars = [out[k] for k in sorted(out)]
    if end_ms:
        bars = [b for b in bars if b.t + IV_MS[interval] <= end_ms]
    return bars


def fetch_range(sym, interval, start, end):
    step = IV_MS[interval]
    out, s = {}, start
    while s < end:
        e = min(end, s + 1440 * step)
        for b in klines(sym, interval, 1440, end_ms=e, start_ms=s):
            out[b.t] = b
        s = e
    return [out[k] for k in sorted(out)]


CONTRACT = {}
USER_TAKER = [None]


def load_contracts():
    try:
        for c in md_get("/openApi/swap/v2/quote/contracts") or []:
            CONTRACT[c["symbol"]] = dict(
                p=int(c.get("pricePrecision", 4)), q=int(c.get("quantityPrecision", 3)),
                minq=float(c.get("tradeMinQuantity", 0) or 0), minusdt=float(c.get("tradeMinUSDT", 0) or 0),
                taker=float(c.get("takerFeeRate", 0) or 0) or None,
                open=str(c.get("apiStateOpen", "true")).lower() == "true" and int(c.get("status", 1) or 1) == 1,
                maint=int(c.get("maintainTime", 0) or 0))
        log.info(f"contratos: {len(CONTRACT)}")
        for s in SYMBOLS:
            if s not in CONTRACT:
                log.warning(f"{s} no existe en BingX perpetuos")
    except Exception as ex:
        log.warning(f"contratos no cargados: {ex}")


VOL = {}


def build_universe():
    """SYMBOLS=ALL: perpetuos USDT abiertos por API, con volumen y horquilla aceptables, ordenados por volumen."""
    load_contracts()
    try:
        d = md_get("/openApi/swap/v2/quote/ticker") or []
    except Exception as ex:
        log.warning(f"ticker 24h: {ex}")
        return
    rows = []
    for x in (d if isinstance(d, list) else [d]):
        s = x.get("symbol", "")
        c = CONTRACT.get(s)
        if not s.endswith("-USDT") or not c or not c.get("open") or s in EXCLUDE or PAUSED.get(s, 0.0) > time.time():
            continue
        qv = float(x.get("quoteVolume") or 0)
        bid, ask = float(x.get("bidPrice") or 0), float(x.get("askPrice") or 0)
        VOL[s] = qv
        if qv < MIN_VOL_USDT:
            continue
        if bid > 0 and ask > 0 and (ask - bid) / ((ask + bid) / 2) * 100 > MAX_SPREAD_PCT:
            continue
        rows.append((qv, s))
    rows.sort(reverse=True)
    new = [s for _, s in rows[:MAX_UNIVERSE]]
    if new:
        SYMBOLS[:] = new
    log.info(f"universo: {len(SYMBOLS)} símbolos (vol ≥ {MIN_VOL_USDT / 1e6:g}M, horquilla ≤ {MAX_SPREAD_PCT:g}%)")


def fvol(x):
    return f"{x / 1e9:.1f}B" if x >= 1e9 else f"{x / 1e6:.0f}M"


def base(sym):
    return sym.split("-")[0]


def cost_rt(sym):
    if not COST_AUTO:
        return COST_RT_NUM
    tk = USER_TAKER[0] or CONTRACT.get(sym, {}).get("taker")
    return 2 * tk * 100 + SLIP_PCT if tk else COST_RT_NUM


def fp(sym, x):
    if x is None:
        return "—"
    p = CONTRACT.get(sym, {}).get("p")
    if p is None:
        p = 2 if abs(x) >= 100 else 4 if abs(x) >= 1 else 6
    return f"{x:.{p}f}"


def fpx(sym, x):
    return f"{x:.{CONTRACT.get(sym, {}).get('p', 4)}f}"


def fq(sym, q):
    p = CONTRACT.get(sym, {}).get("q", 3)
    f = 10 ** p
    return f"{math.floor(q * f + 1e-9) / f:.{p}f}"


def mark_price(sym):
    d = md_get("/openApi/swap/v2/quote/premiumIndex", {"symbol": sym})
    if isinstance(d, list):
        d = d[0] if d else {}
    return float(d["markPrice"])


FUND = {}


def funding_hist(sym, start_ms):
    """[(fundingTime, rate)] real desde start_ms (paginado)."""
    out, s = {}, start_ms
    for _ in range(20):
        d = md_get("/openApi/swap/v2/quote/fundingRate", {"symbol": sym, "startTime": s, "endTime": now_ms(), "limit": 1000}) or []
        if isinstance(d, dict):
            d = [d]
        new = 0
        for x in d:
            t = int(x.get("fundingTime", 0) or 0)
            if t and t not in out:
                out[t] = float(x.get("fundingRate", 0) or 0)
                new += 1
        if not d or new == 0 or len(d) < 1000:
            break
        s = max(out) + 1
    return sorted(out.items())


def funding_recent(sym):
    c = FUND.get(sym)
    if c and time.time() - c[0] < 3600:
        return c[1]
    try:
        f = funding_hist(sym, now_ms() - 10 * DAY_MS) or None          # vacío = sin dato → estimación conservadora
    except Exception as ex:
        log.warning(f"{sym} funding: {ex}")
        f = c[1] if c else None
    FUND[sym] = (time.time(), f)
    return f


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


def p12_widths(bars, min_bars):
    """{td: ancho P12} de días laborables con P12 completo."""
    agg = {}
    for b in bars:
        if not in_(nymin(b.t), P12S, P12E):
            continue
        d = tdate(b.t)
        if SKIP_WE and d.weekday() >= 5:
            continue
        a = agg.setdefault(d, [b.h, b.l, 0])
        a[0], a[1], a[2] = max(a[0], b.h), min(a[1], b.l), a[2] + 1
    return {d: a[0] - a[1] for d, a in agg.items() if a[2] >= min_bars}


def median_before(widths, td):
    ws = [w for d, w in sorted(widths.items()) if d < td][-W_LOOK:]
    return statistics.median(ws) if len(ws) >= 5 else None


WCACHE = {}


def width_median(sym, td):
    c = WCACHE.get(sym)
    if c and c[0] == td:
        return c[1]
    med = None
    try:
        med = median_before(p12_widths(klines(sym, "1h", min(1440, (W_LOOK * 2 + 10) * 24)), 10), td)
    except Exception as ex:
        log.warning(f"{sym} mediana ancho: {ex}")
    WCACHE[sym] = (td, med)
    return med


def btc_pos(bars, td, upto_ms):
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


# ───────────────────────── CALENDARIO MACRO ─────────────────────────
NEWS = {"t": 0.0, "ev": [], "range": None}


def news_events():
    if not NEWS_ON:
        return []
    if time.time() - NEWS["t"] > 4 * 3600:
        NEWS["t"] = time.time()
        try:
            d = ses().get(NEWS_URL, timeout=15).json()
            ev = []
            for x in d:
                if str(x.get("country", "")).upper() not in NEWS_CCY:
                    continue
                imp = str(x.get("impact", ""))
                if imp not in ("High", "Holiday"):
                    continue
                t = int(datetime.fromisoformat(x["date"]).timestamp() * 1000)
                ev.append((t, str(x.get("title", "")), imp))
            ts = [int(datetime.fromisoformat(x["date"]).timestamp() * 1000) for x in d if x.get("date")]
            NEWS["ev"], NEWS["range"] = sorted(ev), ((min(ts), max(ts)) if ts else None)
            log.info(f"calendario: {len(ev)} eventos USD alto impacto/festivos")
        except Exception as ex:
            log.warning(f"calendario no disponible: {ex}")
    return NEWS["ev"]


def news_for(td):
    """None si el calendario no cubre ese día (p. ej. backtest)."""
    ev = news_events()
    rg = NEWS["range"]
    a, b = ny_ms(td, P12E), ny_ms(td, P12S)
    if not NEWS_ON or not rg or not (rg[0] - DAY_MS <= a <= rg[1] + DAY_MS):
        return None
    op, ex = ny_ms(td, OPENM), ny_ms(td, EXITM)
    return dict(
        read=[(t, ti) for t, ti, im in ev if im == "High" and a <= t < op],
        hold=[(t, ti) for t, ti, im in ev if im == "High" and op <= t <= ex],
        holiday=[ti for t, ti, im in ev if im == "Holiday" and ny(t).date() == td])


# ───────────────────────── MOTOR (réplica del Pine) ─────────────────────────
def new_snap(sym, td):
    return dict(sym=sym, td=td, events=[], incomplete=False, isBtc=False, weekend=False, gaps=0, dataBad=False,
                aO=None, aH=None, aL=None, aC=None, lO=None, lH=None, lL=None, lC=None, pH=None, pL=None, pM=None,
                moPx=None, scen=0, nDir=0, wRatio=None, wB=1, scenDone=False, readDone=False, openDone=False,
                accH=False, accL=False, annH=False, annL=False, cntH=0, cntL=0, vH=0.0, vL=0.0, rd=0, btcRd=0, btcNow=0,
                opx=None, moFav=False, nightOk=True, btcOk=True, moOk=True, wOk=True, newsOk=True, bias=0, coinc=False,
                szMult=0.0, zEdge=None, zDeep=None, stop=None, atr=None, dead=False, traded=False, touched=False,
                winClosed=False, rejCost=False, rejStop=False, costR=None, stopPct=None, pos=None, trade=None,
                limit=None, cost=COST_RT_NUM, news=None, last_t=None, last_c=None, last_h=None, last_l=None)


def simulate(sym, bars, td, wmed, btc_rd, btc_now, is_btc, cost=None, fund=None, news=None, F=None):
    S = new_snap(sym, td)
    fl = dict(NIGHT=NIGHT_FILTER, BTC=BTC_FILTER, MO=MO_FILTER, W=WIDTH_FILTER, NEWS=NEWS_FILTER, MIS=MISMATCH)
    fl.update(F or {})   # solo el sweep lo usa: NO se tocan las variables globales con el bot operando
    S["btcNow"], S["isBtc"], S["news"] = btc_now, is_btc, news
    S["cost"] = COST_RT_NUM if cost is None else cost
    cost = S["cost"]
    idx = [i for i, b in enumerate(bars) if tdate(b.t) == td]
    if not idx:
        return S
    atr = atr_series(bars, ATR_LEN)
    vavg = sma_series([b.v for b in bars], 288)
    S["incomplete"] = nymin(bars[idx[0]].t) != P12S
    open_t = ny_ms(td, OPENM)
    pre = [bars[i].t for i in idx if bars[i].t < open_t]
    if pre:
        S["gaps"] = int((pre[-1] - pre[0]) // BAR_MS + 1 - len(pre))
        S["dataBad"] = S["gaps"] > MAX_GAPS
    accb = max(1, round(ACCEPT_MIN / 5))
    E = S["events"]
    flat_win = [(t - NEWS_FLAT_MIN * 60_000, t) for t, _ in (news or {}).get("hold", [])] if NEWS_FLAT_MIN > 0 else []
    fts = [t for t, _ in fund] if fund else None

    def emit(k, t, **kw):
        E.append(dict(k=k, t=t, **kw))

    def open_pos(i, tc, ent):
        d = S["bias"]
        rU = abs(ent - S["stop"])
        tR = ent + d * RR * rU
        tX = S["zEdge"] + d * EXT_K * (S["pH"] - S["pL"])
        tp = tR if TGT_MODE == "R" else (tX if (tX - ent) * d > 0.5 * rU else tR)
        S["pos"] = dict(dir=d, ent=ent, sl=S["stop"], sl0=S["stop"], tp=tp, rU=rU, i=i, t=tc, be=False, mfe=0.0)
        S["traded"] = True
        emit("entry", tc, dir=d, ent=ent, sl=S["stop"], tp=tp, rU=rU)

    def close_pos(px, tc, why):
        p = S["pos"]
        d, rU, ent = p["dir"], p["rU"], p["ent"]
        if fts is not None:
            a, b = bisect.bisect_right(fts, p["t"]), bisect.bisect_right(fts, tc)
            frate = sum(r for _, r in fund[a:b])
            fR, nf = d * frate * ent / rU, b - a          # long paga si funding > 0
        else:
            per = FUND_H * 3_600_000
            nf = int(math.floor(tc / per) - math.floor(p["t"] / per))
            fR = nf * FUND_PCT / 100 * ent / rU
        r = (px - ent) * d / rU - cost / 100 * ent / rU - fR
        S["trade"] = dict(dir=d, ent=ent, exit=px, sl=p["sl"], sl0=p["sl0"], tp=p["tp"], rU=rU, R=r, why=why,
                          t_in=p["t"], t_out=tc, nf=nf, fR=fR, mfe=p["mfe"])
        S["pos"] = None
        emit("exit", tc, **S["trade"])

    def entry_ok(tc, ent):
        rU = abs(ent - S["stop"])
        if rU <= 0:
            return False
        if any(a <= tc <= b for a, b in flat_win):
            return False
        S["costR"], S["stopPct"] = cost / 100 * ent / rU, rU / ent * 100
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
            d = p["dir"]
            fav = (b.h - p["ent"]) if d == 1 else (p["ent"] - b.l)
            p["mfe"] = max(p["mfe"], min(fav, abs(p["tp"] - p["ent"])) / p["rU"])
            px = why = None
            if d == 1:
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
                S["nightOk"] = True if fl["NIGHT"] == "NONE" else (S["scen"] != 3 if fl["NIGHT"] == "EXCL_BOTH" else (S["scen"] == 1 and S["nDir"] == rd))
                S["btcOk"] = True if (fl["BTC"] == "OFF" or is_btc) else (S["btcRd"] != -rd if fl["BTC"] == "NOT_AGAINST" else S["btcRd"] == rd)
                S["moOk"] = True if (fl["MO"] == "OFF" or mo is None) else (S["moFav"] if fl["MO"] == "FAVOR" else not S["moFav"])
                S["wOk"] = {"OFF": True, "EXCL_NARROW": S["wB"] != 0, "EXCL_WIDE": S["wB"] != 2}.get(fl["W"], S["wB"] == 1)
                nr, nh = bool(news and news["read"]), bool(news and news["hold"])
                S["newsOk"] = {"OFF": True, "EXCL_READ": not nr, "EXCL_HOLD": not nh}.get(fl["NEWS"], not (nr or nh))
                ok = rd != 0 and S["nightOk"] and S["btcOk"] and S["moOk"] and S["wOk"] and S["newsOk"] and not S["dataBad"]
                S["bias"] = rd if ok else 0
                a = atr[i] or 0.0
                if S["bias"] == 1:
                    S["zEdge"], S["zDeep"], S["stop"] = S["pH"], S["pM"], S["pM"] - STOP_BUF * a
                    S["coinc"] = b.o >= S["pM"] if COINC_MODE == "MID" else b.o > S["pH"]
                elif S["bias"] == -1:
                    S["zEdge"], S["zDeep"], S["stop"] = S["pL"], S["pM"], S["pM"] + STOP_BUF * a
                    S["coinc"] = b.o <= S["pM"] if COINC_MODE == "MID" else b.o < S["pL"]
                S["szMult"] = 0.0 if S["bias"] == 0 else 1.0 if S["coinc"] else (0.0 if fl["MIS"] == "DISCARD" else REDUCE_F)
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

        # (4) gestión al cierre: breakeven, noticia, cierre forzado, pre-funding
        p = S["pos"]
        if p and i > p["i"]:
            if BE_R > 0 and not p["be"]:
                fav = b.h - p["ent"] if p["dir"] == 1 else p["ent"] - b.l
                if fav >= BE_R * p["rU"]:
                    p["sl"], p["be"] = p["ent"], True
                    emit("be", tc, sl=p["ent"])
            if any(a <= tc <= bb for a, bb in flat_win):
                close_pos(b.c, tc, "Noticia")
            elif in_(m, EXITM, P12S):
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
        if S["dataBad"]:
            return f"Datos incompletos ({S['gaps']} velas perdidas antes de la apertura)"
        for k, t in (("nightOk", "filtro de la noche"), ("btcOk", "filtro BTC"), ("moOk", "filtro Midnight Open"),
                     ("wOk", "filtro de ancho del P12"), ("newsOk", "filtro de noticias")):
            if not S[k]:
                return "Lo bloquea el " + t
        return "Filtro activo"
    return "La apertura no coincide con el P12 (regla 2): descartado"


def news_line(S):
    n = S.get("news")
    if n is None:
        return "📰 calendario: sin datos" if NEWS_ON else ""
    parts = []
    for t, ti in n["read"]:
        parts.append(f"{loc_t(t)} {esc(ti)} (lectura)")
    for t, ti in n["hold"]:
        parts.append(f"{loc_t(t)} {esc(ti)} (con posición)")
    for ti in n["holiday"]:
        parts.append(f"festivo: {esc(ti)}")
    return ("📰 " + " · ".join(parts[:4])) if parts else "📰 sin noticias USD de alto impacto"


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
                f"Entrada {f(p['ent'])} · SL {f(p['sl'])} · TP {f(p['tp'])} · MFE {p['mfe']:.2f}R · cierre {loc(td, EXITM)}")
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
    n = S.get("news")
    if n and (n["read"] or n["hold"]):
        a.append("noticias ⚠")
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
    nl = news_line(S)
    if nl:
        L.append(nl)
    if S["openDone"] and S["bias"] and S["szMult"] > 0:
        L.append(f"🎯 Zona <code>{f(S['zEdge'])}</code> – <code>{f(S['zDeep'])}</code> · SL <code>{f(S['stop'])}</code> · apertura "
                 + ("coincide" if S["coinc"] else f"no coincide ×{S['szMult']:g}"))
    h, d = status(S)
    L += ["", f"▶ <b>{h}</b>", d]
    if S["gaps"]:
        L.append(f"⚠️ <i>{S['gaps']} velas perdidas antes de la apertura</i>")
    return "\n".join(L)


def zone_text(S):
    sym, td = S["sym"], S["td"]
    f = lambda x: fp(sym, x)
    head = "🟢 <b>ZONA DE COMPRA</b>" if S["bias"] == 1 else "🔴 <b>ZONA DE VENTA</b>"
    L = [f"{head} · <b>{sym}</b>",
         f"Zona <code>{f(S['zEdge'])}</code> – <code>{f(S['zDeep'])}</code> · SL <code>{f(S['stop'])}</code>",
         entry_rule(S),
         f"Ventana {loc(td, OPENM)}–{loc(td, ENTE)} · cierre forzado {loc(td, EXITM)}" + ("" if S["coinc"] else f" · tamaño ×{S['szMult']:g}"),
         f"<i>{checks_line(S)}</i>"]
    n = S.get("news")
    if n and n["hold"]:
        L.append("⚠️ " + " · ".join(f"{loc_t(t)} {esc(ti)}" for t, ti in n["hold"][:3]))
    return "\n".join(L)


def entry_text(S, e, fresh, note):
    sym = S["sym"]
    f = lambda x: fp(sym, x)
    ent, rU = e["ent"], e["rU"]
    head = "▲ <b>COMPRA</b>" if e["dir"] == 1 else "▼ <b>VENTA</b>"
    L = [f"{head} · <b>{sym}</b>" + ("" if fresh else "  ⏱ <i>tardía: no ejecutar</i>"),
         f"Entrada <code>{f(ent)}</code>",
         f"SL <code>{f(e['sl'])}</code>  (−1R · {rU / ent * 100:.2f}%)",
         f"TP <code>{f(e['tp'])}</code>  (+{abs(e['tp'] - ent) / rU:.2f}R)",
         f"Coste {S['costR']:.2f}R ({S['cost']:.3f}%) · riesgo {RISK_PCT:g}% × {S['szMult']:g}" + ("" if S["coinc"] else " (apertura no coincide)"),
         f"Cierre forzado {loc(S['td'], EXITM)}" + (f" · BE a +{BE_R:g}R" if BE_R > 0 else ""),
         f"<i>{checks_line(S)}</i>"]
    if note:
        L.append(note)
    return "\n".join(L)


def exit_text(S, e, note):
    ico = {"TP": "✅", "SL": "❌", "BE": "🔒", "Noticia": "📰"}.get(e["why"], "■")
    L = [f"{ico} <b>{e['why']} {e['R']:+.2f}R</b> · {S['sym']}",
         f"{fp(S['sym'], e['ent'])} → {fp(S['sym'], e['exit'])} · {loc_t(e['t_in'])}→{loc_t(e['t_out'])} · MFE {e['mfe']:.2f}R"
         + (f" · funding {e['fR']:+.2f}R" if e["nf"] else ""),
         f"<i>Acumulado: {agg([t['R'] for t in STATE['trades']])}</i>"]
    if note:
        L.append(note)
    return "\n".join(L)


def agg(rs, dd=False):
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
    if dd:
        eq = pk = mdd = 0.0
        for r in rs:
            eq += r
            pk = max(pk, eq)
            mdd = min(mdd, eq - pk)
        s += f" · DD {mdd:.1f}R"
    return s + (" ⚠️n<" + str(MIN_N) if n < MIN_N else "")


def breakdown(T):
    R = lambda cond: [t["R"] for t in T if cond(t)]
    L = ["Apertura coincide: " + agg(R(lambda t: t.get("coinc"))),
         "Apertura no coincide: " + agg(R(lambda t: not t.get("coinc"))),
         "BTC alineado: " + agg(R(lambda t: t.get("btc") == 1)),
         "BTC en contra: " + agg(R(lambda t: t.get("btc") == -1)),
         "MO a favor: " + agg(R(lambda t: t.get("mo") is True)),
         "MO en contra: " + agg(R(lambda t: t.get("mo") is False))]
    for k in range(3):
        L.append(f"P12 {WB[k]}: " + agg(R(lambda t, k=k: t.get("wB") == k)))
    for k in (1, 2, 3):
        L.append(f"Noche {SCN[k]}: " + agg(R(lambda t, k=k: t.get("scen") == k)))
    if any(t.get("news") is not None for t in T):
        L.append("Con noticia: " + agg(R(lambda t: t.get("news") is True)))
        L.append("Sin noticia: " + agg(R(lambda t: t.get("news") is False)))
    mf = [t["mfe"] for t in T if t.get("mfe") is not None]
    if mf:
        L.append("MFE ≥1R {:.0f}% · ≥2R {:.0f}% · ≥3R {:.0f}%".format(*(100 * sum(1 for x in mf if x >= k) / len(mf) for k in (1, 2, 3))))
    return L


def stats_text():
    T = STATE["trades"]
    if not T:
        return "📊 Sin operaciones cerradas todavía."
    cut = str((datetime.now(TZ_NY) - timedelta(days=30)).date())
    L = ["📊 <b>Estadística P12</b> · señales, R netos de coste y funding",
         "<b>Total</b> " + agg([t["R"] for t in T], dd=True),
         "<b>30 días</b> " + agg([t["R"] for t in T if t["td"] >= cut]), ""]
    for s in sorted({t["sym"] for t in T}):
        L.append(f"• {s}: " + agg([t["R"] for t in T if t["sym"] == s]))
    LR = STATE["live_res"]
    if LR:
        L += ["", "<b>Real en BingX</b> " + agg([x["R"] for x in LR if x.get("R") is not None]),
              "Deslizamiento medio de entrada: {:+.2f}R".format(statistics.mean([x.get("slip", 0) for x in LR]))]
    L += ["", "<b>Desgloses</b>"] + breakdown(T)
    L.append(f"<i>Activa un filtro solo si separa con n ≥ {MIN_N} en ambos lados</i>")
    return "\n".join(L)


def risk_text():
    rk = STATE["risk"]
    L = [f"🛡 <b>Riesgo</b> · {MODE}" + (" DRY_RUN" if LIVE and DRY_RUN else "") + (" · ⏸ PAUSADO" if STATE["paused"] else ""),
         f"Riesgo por operación {RISK_PCT:g}% · apalancamiento {LEVERAGE}x",
         f"Hoy ({rk.get('day', '—')}): señales {rk.get('dayR', 0):+.2f}R · límite −{MAX_DAILY_LOSS_R:g}R",
         f"Hoy REAL: {real_risk().get('dayR', 0):+.2f}R · racha real {real_risk().get('consec', 0)} (señales {rk.get('consec', 0)}) · pausa a {MAX_CONSEC_LOSS}",
         f"Equity: máximo {STATE.get('hwm', 0):.2f} USDT · pausa si cae {MAX_DD_PCT:g}% · tope de cuenta {MAX_ACCOUNT_POS} posiciones",
         f"Máx. posiciones {MAX_POS} · misma dirección {MAX_SAME_DIR} (2ª ×{CORR_SCALE:g})",
         "Abiertas: " + (", ".join(f"{s} {'▲' if L_['dir'] == 1 else '▼'}" for s, L_ in STATE["live"].items()) or "ninguna")]
    return "\n".join(L)


def estado_text():
    L = [f"<b>P12 · estado</b> · {MODE}" + (" DRY_RUN" if LIVE and DRY_RUN else "") + (" · ⏸ PAUSADO" if STATE["paused"] else "")]
    syms = SYMBOLS
    if UNIVERSE:
        U = STATE.get("uni", {})
        L.append(f"Modo TODAS · {len(SYMBOLS)} en el universo · hoy aceptan {len(U.get('acc', []))} · top seguido {len(U.get('active', []))}"
                 + ("" if U.get("read") else f" · escaneo a las {loc(tdate(now_ms()), READE)}"))
        syms = U.get("active", []) or []
    for s in syms:
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
    syms = SYMBOLS
    if UNIVERSE:
        U = STATE.get("uni", {})
        L.append(f"{U.get('n_scan', 0)} escaneadas · {len(U.get('acc', []))} aceptan · {len(U.get('active', []))} seguidas")
        syms = U.get("active", []) or []
    for s in syms:
        S = LAST.get(s)
        if not S or S.get("weekend") or S["td"] != td:
            continue
        if S["trade"]:
            r = f"{S['trade']['why']} <b>{S['trade']['R']:+.2f}R</b> (MFE {S['trade']['mfe']:.1f}R)"
        elif S["bias"] and S["szMult"] > 0:
            r = "tesis rota" if S["dead"] else "sin entrada"
        else:
            r = "no se opera · " + why_txt(S) if S["readDone"] else "—"
        L.append(f"• <b>{s}</b> {scen_txt(S)} · {read_txt(S)} → {r}")
    L.append(f"<i>Acumulado: {agg([t['R'] for t in STATE['trades']])}</i>")
    if td.weekday() == 4:
        L.append("")
        L.append(stats_text())
    return "\n".join(L)


HELP = ("<b>P12 Hunter bot</b>\n/estado — qué hacer ahora en cada símbolo\n/hoy [SÍMBOLO] — tarjeta + gráfico\n"
        "/stats — expectativa, t y desgloses\n/riesgo — límites y posiciones\n/backtest [días] [top] — mismo motor sobre histórico BingX\n"
        "/sweep [días] [top] — ¿qué filtros separan de verdad? (70/30 + Bonferroni)\n/pausa · /reanuda — ejecución (LIVE)\n/hwm — reiniciar el máximo de equity\n/cerrar SÍMBOLO — cerrar posición del bot a mercado")


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
        for t, _ in (S.get("news") or {}).get("read", []) + (S.get("news") or {}).get("hold", []):
            kn = k_of.get(t - t % BAR_MS)
            if kn is not None:
                ax.axvline(kn, color="#e6a23c", ls="--", lw=0.8)
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
            if r.status_code in (401, 403):
                log.error(f"TG {r.status_code}: {desc} — revisa TELEGRAM_TOKEN y que el bot esté en el chat")
            else:
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

    def answer(self, cb_id, text=""):
        self.call("answerCallbackQuery", {"callback_query_id": cb_id, "text": text[:190]})

    def set_commands(self):
        cmds = [("estado", "Qué hacer ahora"), ("hoy", "Tarjeta y gráfico del día"), ("stats", "Estadística"),
                ("riesgo", "Límites y posiciones"), ("backtest", "Backtest con el motor del bot"),
                ("pausa", "No ejecutar entradas nuevas"), ("reanuda", "Reanudar ejecución"),
                ("cerrar", "Cerrar posición del bot"), ("ayuda", "Ayuda")]
        self.call("setMyCommands", {"commands": json.dumps([{"command": c, "description": d} for c, d in cmds])})

    def poll(self, handler):
        s, off, warned = requests.Session(), None, False
        while True:
            try:
                p = {"timeout": 50, "allowed_updates": json.dumps(["message", "callback_query"])}
                if off:
                    p["offset"] = off
                r = s.get(self.url + "getUpdates", params=p, timeout=65)
                j = r.json()
                if not j.get("ok"):
                    if r.status_code == 409 and not warned:
                        log.warning("TG 409: otro proceso lee este token (usa un token propio para este bot). Comandos desactivados.")
                        warned = True
                    time.sleep(60 if r.status_code == 409 else 5)
                    continue
                for u in j["result"]:
                    off = u["update_id"] + 1
                    try:
                        if "callback_query" in u:
                            q = u["callback_query"]
                            m = q.get("message") or {}
                            handler("cb", [q.get("data", ""), q.get("id")], str((q.get("from") or {}).get("id", "")),
                                    str((m.get("chat") or {}).get("id", "")))
                            continue
                        m = u.get("message") or {}
                        txt = (m.get("text") or "").strip()
                        if not txt.startswith("/"):
                            continue
                        parts = txt.split()
                        handler(parts[0].split("@")[0].lower(), parts[1:], str((m.get("from") or {}).get("id", "")),
                                str((m.get("chat") or {}).get("id", "")))
                    except Exception:
                        log.exception("comando")
            except Exception as ex:
                log.warning(f"poll: {ex}")
                time.sleep(5)


def buttons(sym, close=False):
    s = sym.replace("-", "")
    kb = [[{"text": "📈 TradingView", "url": f"https://www.tradingview.com/chart/?symbol=BINGX:{s}.P"},
           {"text": "BingX", "url": f"https://bingx.com/en/perpetual/{sym}/"}]]
    if close:
        kb.append([{"text": "⛔ Cerrar ya", "callback_data": f"close:{sym}"}])
    return kb


# ───────────────────────── BINGX (LIVE) ─────────────────────────
class BXError(RuntimeError):
    def __init__(self, code, msg, path=""):
        super().__init__(f"BingX {path} {code}: {msg}")
        self.code = code


TIME_OFF = [0.0, 0.0]          # [offset ms, última sync]
AUTH_CODES = {100001, 100004, 100413, 100419}


def sync_time():
    try:
        t0 = time.time()
        d = md_get("/openApi/swap/v2/server/time")
        st = int((d or {}).get("serverTime"))
        TIME_OFF[0] = st - (t0 + time.time()) / 2 * 1000
        TIME_OFF[1] = time.time()
        if abs(TIME_OFF[0]) > 1000:
            log.warning(f"reloj desviado {TIME_OFF[0]:.0f} ms respecto a BingX (corregido)")
    except Exception as ex:
        log.warning(f"hora del servidor: {ex}")


class BingX:
    def __init__(self):
        self.hedge = None
        self.lock = threading.Lock()

    def req(self, method, path, params=None):
        if time.time() - TIME_OFF[1] > 1800:
            sync_time()
        for a in range(5):
            p = {k: v for k, v in (params or {}).items() if v is not None}
            p["timestamp"] = int(time.time() * 1000 + TIME_OFF[0])
            p["recvWindow"] = 5000
            canon = "&".join(f"{k}={p[k]}" for k in sorted(p))        # firma sobre la cadena SIN codificar
            sig = hmac.new(BX_SECRET.encode(), canon.encode(), hashlib.sha256).hexdigest()
            h = {"X-BX-APIKEY": BX_KEY}
            j, err = None, None
            for base in BX_BASES:
                try:
                    if method == "POST":
                        h2 = dict(h)
                        h2["Content-Type"] = "application/x-www-form-urlencoded"
                        r = ses().post(base + path, data=(canon + "&signature=" + sig).encode(), headers=h2, timeout=12)
                    else:
                        qs = "&".join(f"{k}={quote(str(p[k]), safe='')}" for k in sorted(p)) if ("{" in canon or "[" in canon) else canon
                        r = ses().request(method, base + path + "?" + qs + "&signature=" + sig, headers=h, timeout=12)
                    j = r.json()
                    break
                except requests.ConnectionError as ex:          # no llegó: probar .pro
                    err = ex
                    continue
                except requests.Timeout as ex:                   # pudo llegar: no reintentar un POST
                    if method == "POST":
                        raise BXError(-1, f"timeout {ex}", path)
                    err = ex
                    break
                except ValueError as ex:
                    err = ex
                    break
            if j is None:
                if a < 4:
                    time.sleep(1 + a)
                    continue
                raise BXError(-1, f"red: {err}", path)
            c = j.get("code", 0)
            if c == 100410:
                time.sleep(min(0.2 * 2 ** a, 5) + random.random())
                continue
            if c == 100421 and a == 0:
                sync_time()
                continue
            if c != 0:
                if c in AUTH_CODES:
                    auth_alert(c, j.get("msg"))
                raise BXError(c, j.get("msg"), path)
            return j.get("data")
        raise BXError(100410, "límite de peticiones", path)

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

    def balance(self):
        d = None
        for path in ("/openApi/swap/v3/user/balance", "/openApi/swap/v2/user/balance"):
            try:
                d = self.req("GET", path)
                break
            except BXError as ex:
                if ex.code in AUTH_CODES:
                    raise
        b = d.get("balance", d) if isinstance(d, dict) else d
        if isinstance(b, list):
            b = next((x for x in b if x.get("asset") == "USDT"), b[0] if b else {})
        b = b or {}
        eq = float(b.get("equity") or b.get("balance") or 0)
        av = float(b.get("availableMargin") or eq)
        return eq, av

    def positions(self, sym):
        return [x for x in (self.req("GET", "/openApi/swap/v2/user/positions", {"symbol": sym}) or [])
                if float(x.get("positionAmt", 0) or 0) != 0]

    def all_positions(self):
        return [x for x in (self.req("GET", "/openApi/swap/v2/user/positions") or [])
                if float(x.get("positionAmt", 0) or 0) != 0]

    def open_orders(self, sym):
        """Lista de órdenes abiertas o None si la consulta falla (None ≠ «no hay órdenes»)."""
        try:
            d = self.req("GET", "/openApi/swap/v2/trade/openOrders", {"symbol": sym}) or {}
            return d.get("orders", []) if isinstance(d, dict) else list(d)
        except Exception as ex:
            log.warning(f"{sym} órdenes abiertas: {ex}")
            return None

    def position(self, sym, d):
        for x in self.positions(sym):
            amt = float(x["positionAmt"])
            ps = str(x.get("positionSide", "BOTH")).upper()
            xd = 1 if ps == "LONG" else -1 if ps == "SHORT" else (1 if amt > 0 else -1)
            if xd == d:
                return dict(amt=abs(amt), avg=float(x.get("avgPrice") or x.get("entryPrice") or 0))
        return None

    def leverage(self, sym):
        for s in (["LONG", "SHORT"] if self.is_hedge() else ["BOTH"]):
            try:
                self.req("POST", "/openApi/swap/v2/trade/leverage", {"symbol": sym, "side": s, "leverage": LEVERAGE})
            except Exception as ex:
                log.warning(f"{sym} apalancamiento {s}: {ex}")

    def order(self, sym, side, pside, otype, qty, stop=None, reduce=False, cid=None, stop_loss=None):
        p = {"symbol": sym, "side": side, "positionSide": pside, "type": otype, "quantity": qty}
        if stop is not None:
            p["stopPrice"], p["workingType"] = stop, "MARK_PRICE"
        if reduce and pside == "BOTH":
            p["reduceOnly"] = "true"
        if cid:
            p["clientOrderId"] = cid
        if stop_loss:
            p["stopLoss"] = stop_loss
        d = self.req("POST", "/openApi/swap/v2/trade/order", p) or {}
        o = d.get("order", d) if isinstance(d, dict) else {}
        return o.get("orderId") or o.get("orderID")

    def order_info(self, sym, cid):
        try:
            d = self.req("GET", "/openApi/swap/v2/trade/order", {"symbol": sym, "clientOrderId": cid}) or {}
            return d.get("order", d)
        except Exception:
            return {}

    def find_stop(self, sym, pside, otype):
        try:
            d = self.req("GET", "/openApi/swap/v2/trade/openOrders", {"symbol": sym}) or {}
            for o in (d.get("orders", []) if isinstance(d, dict) else d):
                if o.get("type") == otype and str(o.get("positionSide", pside)).upper() == pside:
                    return o.get("orderId")
        except Exception as ex:
            log.warning(f"{sym} órdenes abiertas: {ex}")
        return None

    def move_stop(self, sym, oid, side, pside, qty, stop):
        p = {"symbol": sym, "cancelOrderId": oid, "cancelReplaceMode": "STOP_ON_FAILURE", "side": side, "positionSide": pside,
             "type": "STOP_MARKET", "quantity": qty, "stopPrice": stop, "workingType": "MARK_PRICE"}
        if pside == "BOTH":
            p["reduceOnly"] = "true"
        try:
            d = self.req("POST", "/openApi/swap/v1/trade/cancelReplace", p) or {}
            if str(d.get("newOrderResult", "")).upper() == "SUCCESS" or d.get("newOrderId"):
                return d.get("newOrderId")
        except Exception as ex:
            log.warning(f"{sym} cancelReplace: {ex}")
        self.cancel(sym, oid)
        return self.order(sym, side, pside, "STOP_MARKET", qty, stop=stop, reduce=True)

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

    def realized(self, sym, t0):
        rows = self.req("GET", "/openApi/swap/v2/user/income", {"symbol": sym, "startTime": t0 - 60_000, "endTime": now_ms(), "limit": 1000}) or []
        return sum(float(r.get("income", 0) or 0) for r in rows if r.get("incomeType") in ("REALIZED_PNL", "TRADING_FEE", "FUNDING_FEE"))

    def commission(self):
        try:
            d = self.req("GET", "/openApi/swap/v2/user/commissionRate") or {}
            c = d.get("commission", d)
            return float(c.get("takerCommissionRate")) or None
        except Exception as ex:
            log.warning(f"comisión: {ex}")
            return None


BX = BingX()
AUTH_WARNED = [0.0]


def auth_alert(code, msg):
    if time.time() - AUTH_WARNED[0] > 3600:
        AUTH_WARNED[0] = time.time()
        tg.send(f"🚨 <b>BingX rechaza la API key</b> ({code}: {esc(msg)}). Revisa clave, permisos de trading y lista blanca de IP.")


# ───────────────────────── EJECUCIÓN ─────────────────────────
def risk_gate(sym, S, d):
    if STATE["paused"]:
        return "⏸ <i>pausado: no se ejecuta</i>"
    if sym in STATE["live"]:
        return "⚠️ ya hay posición del bot en este símbolo"
    if len(STATE["live"]) >= MAX_POS:
        return f"⚠️ máximo de posiciones ({MAX_POS}): no se ejecuta"
    same = sum(1 for L in STATE["live"].values() if L.get("dir") == d)
    if same >= MAX_SAME_DIR:
        return f"⚠️ ya hay {same} posiciones en la misma dirección (correlación): no se ejecuta"
    rk = real_risk() if (LIVE and not DRY_RUN) else STATE["risk"]
    if rk.get("day") == str(S["td"]) and rk.get("dayR", 0) <= -MAX_DAILY_LOSS_R:
        return f"🛑 límite de pérdida diaria alcanzado ({rk['dayR']:+.2f}R): no se ejecuta"
    return ""


def real_risk():
    """Contadores de las operaciones EJECUTADAS (R real de BingX). Los de STATE['risk'] cuentan también las
    hipotéticas de todo el top seguido y solo sirven para la estadística de la señal."""
    return STATE.setdefault("real", {})


def real_risk_update(R):
    if R is None:
        return
    rr = real_risk()
    td = str(tdate(now_ms()))
    if rr.get("day") != td:
        rr["day"], rr["dayR"] = td, 0.0
    rr["dayR"] = rr.get("dayR", 0.0) + R
    rr["consec"] = rr.get("consec", 0) + 1 if R < 0 else 0
    if MAX_CONSEC_LOSS and rr["consec"] >= MAX_CONSEC_LOSS and not STATE["paused"]:
        STATE["paused"] = True
        tg.send(f"⏸ <b>Pausa automática</b>: {rr['consec']} pérdidas REALES seguidas. Revisa /stats y usa /reanuda cuando decidas.")


def acct_id():
    return hashlib.sha256((BX_BASE + "|" + BX_KEY).encode()).hexdigest()[:10]


def dd_update(eq):
    """Máximo de equity histórico (por cuenta: si cambias de clave/demo no arrastra el máximo de otra) y caída actual en %."""
    if STATE.get("hwm_acct") != acct_id():
        STATE["hwm_acct"], STATE["hwm"] = acct_id(), eq
    STATE["hwm"] = max(STATE.get("hwm", 0.0), eq)
    hw = STATE["hwm"]
    return (hw - eq) / hw * 100 if hw > 0 else 0.0


def equity_guard():
    """Cortacircuitos de equity: pausa las aperturas si cae MAX_DD_PCT % desde su máximo."""
    if not (LIVE and not DRY_RUN) or MAX_DD_PCT <= 0:
        return
    eq, _ = BX.balance()
    dd = dd_update(eq)
    if dd >= MAX_DD_PCT and not STATE["paused"]:
        STATE["paused"] = True
        save_state()
        tg.send(f"🛑 <b>Equity {eq:.2f} USDT: -{dd:.1f}% desde el máximo ({STATE['hwm']:.2f})</b> ≥ {MAX_DD_PCT:g}%. Bot PAUSADO. "
                f"Si has retirado fondos usa /hwm; si no, revisa antes de /reanuda.")


def guard_stops():
    """Cada ciclo: toda posición del bot debe tener SL (y TP) vivos en BingX; si falta uno, se repone."""
    if not (LIVE and not DRY_RUN and GUARD_STOPS):
        return
    for sym, L in list(STATE["live"].items()):
        if L.get("dry"):
            continue
        with BX.lock:
            orders = BX.open_orders(sym)
            if orders is None:                      # no se pudo consultar: no inventar que falta
                continue
            try:
                p = BX.position(sym, L["dir"])
            except Exception as ex:
                log.warning(f"{sym} guardián posición: {ex}")
                continue
            if not p:                               # cerrada: de eso se ocupa reconcile()
                continue
            ps, cs = BX.pside(L["dir"]), "SELL" if L["dir"] == 1 else "BUY"
            same = [o for o in orders if str(o.get("positionSide", ps)).upper() == ps]
            has_sl = any(o.get("type") in ("STOP_MARKET", "STOP") for o in same)
            has_tp = any(o.get("type") in ("TAKE_PROFIT_MARKET", "TAKE_PROFIT") for o in same)
            amt = fq(sym, p["amt"])
            if not has_sl:
                try:
                    L["sl_id"] = BX.order(sym, cs, ps, "STOP_MARKET", amt, stop=fpx(sym, L["sl"]), reduce=True)
                    tg.send(f"🛡 <b>{sym}</b>: el SL no estaba en BingX → repuesto en <code>{fp(sym, L['sl'])}</code>")
                except Exception as ex:
                    breached = False
                    try:
                        breached = (mark_price(sym) - L["sl"]) * L["dir"] <= 0
                    except Exception:
                        pass
                    if breached:
                        tg.send(f"🚨 <b>{sym}</b>: sin SL y el precio ya lo pasó ({esc(ex)[:80]}): cierro a mercado")
                        BX.order(sym, cs, ps, "MARKET", amt, reduce=True)
                    else:
                        tg.send(f"🚨 <b>{sym}</b>: SIN STOP en BingX y no se pudo reponer ({esc(ex)[:100]}). Ciérrala a mano o usa /cerrar {sym}")
            if not has_tp and L.get("tp") and not L.get("tp_warned"):
                try:
                    L["tp_id"] = BX.order(sym, cs, ps, "TAKE_PROFIT_MARKET", amt, stop=fpx(sym, L["tp"]), reduce=True)
                    tg.send(f"🛡 <b>{sym}</b>: el TP no estaba en BingX → repuesto en <code>{fp(sym, L['tp'])}</code>")
                except Exception as ex:
                    L["tp_warned"] = True
                    tg.send(f"⚠️ <b>{sym}</b>: sin TP en BingX y no se pudo reponer ({esc(ex)[:100]})")
    save_state()


def live_open(sym, S, e):
    if not LIVE:
        return ""
    gate = risk_gate(sym, S, e["dir"])
    if gate:
        return gate
    d, stop = e["dir"], e["sl"]
    same = sum(1 for L in STATE["live"].values() if L.get("dir") == d)
    mult = S["szMult"] * (CORR_SCALE if same >= 1 else 1.0)
    if DRY_RUN:
        STATE["live"][sym] = dict(dry=True, dir=d, td=str(S["td"]))
        return f"🧪 <i>DRY_RUN: orden simulada (tamaño ×{mult:g})</i>"
    with BX.lock:
        try:
            c = CONTRACT.get(sym, {})
            if c.get("open") is False:
                return "⚠️ el contrato no admite aperturas por API ahora mismo"
            if c.get("maint") and c["maint"] > now_ms() - 3600_000 and c["maint"] < now_ms() + 3600_000:
                return "⚠️ mantenimiento del contrato en curso o inminente"
            if BX.positions(sym):
                return "⚠️ ya hay una posición abierta en BingX (manual u otro bot): no se ejecuta"
            if MAX_ACCOUNT_POS:
                n_acc = len({(x.get("symbol"), str(x.get("positionSide", "BOTH")).upper()) for x in BX.all_positions()})
                if n_acc >= MAX_ACCOUNT_POS:
                    return f"⚠️ la cuenta ya tiene {n_acc} posiciones abiertas (MAX_ACCOUNT_POS={MAX_ACCOUNT_POS}): no se ejecuta"
            px = mark_price(sym)
            if (px - stop) * d <= 0:
                return "✋ el precio ya está al otro lado del stop: no se ejecuta"
            slip = (px - e["ent"]) * d / e["rU"]
            if slip > ENTRY_MAX_SLIP_R:
                return f"✋ el precio se escapó {slip:.2f}R desde la señal: no se persigue"
            rU = abs(px - stop)
            eq, av = BX.balance()
            dd = dd_update(eq)
            if MAX_DD_PCT > 0 and dd >= MAX_DD_PCT:
                STATE["paused"] = True
                save_state()
                tg.send(f"🛑 Equity -{dd:.1f}% desde el máximo: bot PAUSADO (MAX_DD_PCT={MAX_DD_PCT:g}%).")
                return f"🛑 equity -{dd:.1f}% desde el máximo: no se ejecuta"
            q = min(eq * RISK_PCT / 100 / rU * mult, av * LEVERAGE * 0.9 / px)
            qs = fq(sym, q)
            if float(qs) <= 0 or float(qs) < c.get("minq", 0) or float(qs) * px < c.get("minusdt", 0):
                return f"⚠️ tamaño {qs} bajo el mínimo del contrato: no se ejecuta"
            BX.leverage(sym)
            BX.cancel_all(sym)
            ps, side, cs = BX.pside(d), ("BUY" if d == 1 else "SELL"), ("SELL" if d == 1 else "BUY")
            cid = f"p12{sym.split('-')[0].lower()}{S['td']:%y%m%d}"[:40]
            sl_json = json.dumps({"type": "STOP_MARKET", "stopPrice": float(fpx(sym, stop)), "workingType": "MARK_PRICE"}, separators=(",", ":"))
            try:
                BX.order(sym, side, ps, "MARKET", qs, cid=cid, stop_loss=sl_json)
            except BXError as ex:
                if ex.code == 101481:
                    pass                                         # ya enviada antes (idempotencia)
                elif ex.code in (101204, 101206):
                    return "⚠️ margen insuficiente en BingX: no se ejecuta"
                elif ex.code == 101415:
                    return "⚠️ par suspendido para abrir posiciones"
                elif not BX.position(sym, d):
                    log.warning(f"{sym} entrada con SL adjunto rechazada ({ex}); reintento sin adjunto")
                    BX.order(sym, side, ps, "MARKET", qs, cid=cid + "b")
            time.sleep(1.5)
            p = BX.position(sym, d)
            if not p:
                return "🚨 orden enviada pero la posición no aparece: revisa BingX"
            avg, amt = p["avg"], fq(sym, p["amt"])
            sl_id = BX.find_stop(sym, ps, "STOP_MARKET")
            if not sl_id:
                try:
                    sl_id = BX.order(sym, cs, ps, "STOP_MARKET", amt, stop=fpx(sym, stop), reduce=True)
                except Exception as ex:
                    BX.order(sym, cs, ps, "MARKET", amt, reduce=True)
                    BX.cancel_all(sym)
                    return f"🚨 SL rechazado ({esc(ex)}): posición cerrada"
            rUf = abs(avg - stop)
            tX = S["zEdge"] + d * EXT_K * (S["pH"] - S["pL"])
            tp = avg + d * RR * rUf if TGT_MODE == "R" or (tX - avg) * d <= 0.5 * rUf else tX
            warn = ""
            try:
                tp_id = BX.order(sym, cs, ps, "TAKE_PROFIT_MARKET", amt, stop=fpx(sym, tp), reduce=True)
            except Exception as ex:
                tp_id, warn = None, f"\n⚠️ TP no colocado: {esc(ex)}"
            slip_f = (avg - e["ent"]) * d / e["rU"]
            STATE["live"][sym] = dict(dry=False, dir=d, amt=amt, avg=avg, sl=stop, tp=tp, sl_id=sl_id, tp_id=tp_id,
                                      td=str(S["td"]), t_open=now_ms(), rU=rUf, slip=slip_f, key=f"{sym}@{S['td']}")
            save_state()
            return (f"✅ <b>EJECUTADA</b> {amt} @ <code>{fp(sym, avg)}</code> · desliz {slip_f:+.2f}R · "
                    f"SL en BingX · TP real <code>{fp(sym, tp)}</code>{warn}")
        except Exception as ex:
            log.exception("live_open")
            return f"🚨 error al ejecutar: {esc(ex)[:200]}"


def live_be(sym):
    L = STATE["live"].get(sym)
    if not L or L.get("dry"):
        return ""
    with BX.lock:
        try:
            ps, cs = BX.pside(L["dir"]), "SELL" if L["dir"] == 1 else "BUY"
            L["sl_id"] = BX.move_stop(sym, L.get("sl_id"), cs, ps, L["amt"], fpx(sym, L["avg"]))
            L["sl"] = L["avg"]
            return "SL en BingX movido a la entrada"
        except Exception as ex:
            return f"🚨 BE no aplicado en BingX: {esc(ex)[:150]}"


def finish_live(sym, L, how):
    """Cierra el registro: R real desde el flujo de fondos de BingX."""
    STATE["live"].pop(sym, None)
    if L.get("dry"):
        return "🧪 DRY: cerrada"
    msg = how
    try:
        time.sleep(2)
        pnl = BX.realized(sym, L["t_open"])
        risk = float(L["amt"]) * L["rU"]
        R = pnl / risk if risk > 0 else None
        STATE["live_res"].append(dict(key=L.get("key"), sym=sym, R=R, pnl=pnl, slip=L.get("slip", 0.0)))
        STATE["live_res"] = STATE["live_res"][-1000:]
        if R is not None:
            msg += f" · real <b>{R:+.2f}R</b> ({pnl:+.2f} USDT)"
            real_risk_update(R)
    except Exception as ex:
        log.warning(f"{sym} PnL real: {ex}")
    save_state()
    return msg


def live_close(sym, why):
    L = STATE["live"].get(sym)
    if not L:
        return ""
    if L.get("dry"):
        return finish_live(sym, L, "")
    with BX.lock:
        try:
            p = BX.position(sym, L["dir"])
            if p:
                BX.order(sym, "SELL" if L["dir"] == 1 else "BUY", BX.pside(L["dir"]), "MARKET", fq(sym, p["amt"]), reduce=True)
                how = f"BingX: cerrada a mercado ({why})"
            else:
                how = "BingX: ya cerrada por SL/TP"
            BX.cancel_all(sym)
        except Exception as ex:
            return f"🚨 error al cerrar en BingX: {esc(ex)[:150]}"
    return finish_live(sym, L, how)


def reconcile(sym):
    L = STATE["live"].get(sym)
    if not L or L.get("dry") or not LIVE or DRY_RUN:
        return
    try:
        if not BX.position(sym, L["dir"]):
            BX.cancel_all(sym)
            msg = finish_live(sym, L, f"ℹ️ {sym}: posición cerrada en BingX (SL/TP) · órdenes restantes canceladas")
            D = STATE["days"].get(sym) or {}
            tg.send(msg, silent=True, reply=D.get("entry"))
    except Exception as ex:
        log.warning(f"{sym} reconcile: {ex}")


# ───────────────────────── ESTADO EN DISCO ─────────────────────────
STATE = {"days": {}, "trades": [], "paused": False, "live": {}, "digest": [], "risk": {}, "live_res": [], "keepalive": ""}
STATE_LOCK = threading.Lock()
LAST = {}
HEALTH = {"version": CODE_VERSION, "mode": MODE, "last_cycle": None}
LAST_CYCLE = [time.time()]


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


def trade_rec(sym, S, e):
    n = S.get("news")
    return dict(key=f"{sym}@{S['td']}", sym=sym, td=str(S["td"]), dir=e["dir"], R=round(e["R"], 4), why=e["why"],
                mfe=round(e["mfe"], 3), coinc=S["coinc"], isbtc=S["isBtc"], nal=1 if (S["scen"] == 1 and S["nDir"] == e["dir"]) else 0, btc=0 if S["isBtc"] else S["btcRd"] * e["dir"],
                mo=None if S["moPx"] is None else S["moFav"], wB=S["wB"], scen=S["scen"],
                news=None if n is None else bool(n["read"] or n["hold"]))


def record_trade(sym, S, e):
    t = trade_rec(sym, S, e)
    if any(x["key"] == t["key"] for x in STATE["trades"][-500:]):
        return
    STATE["trades"].append(t)
    rk = STATE["risk"]
    if rk.get("day") != t["td"]:
        rk["day"], rk["dayR"] = t["td"], 0.0
    rk["dayR"] = rk.get("dayR", 0.0) + t["R"]
    rk["consec"] = rk.get("consec", 0) + 1 if t["R"] < 0 else 0
    if LIVE and DRY_RUN and MAX_CONSEC_LOSS and rk["consec"] >= MAX_CONSEC_LOSS and not STATE["paused"]:
        STATE["paused"] = True
        tg.send(f"⏸ <b>Pausa automática</b>: {rk['consec']} pérdidas seguidas. Revisa /stats y usa /reanuda cuando decidas.")


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
        live_real = LIVE and not DRY_RUN and sym in STATE["live"]
        D["entry"] = tg.photo(chart_png(S), entry_text(S, e, fresh, note), silent=not fresh, reply=D["zone"] or D["card"],
                              kb=buttons(sym, close=live_real))
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


UNI_QUIET = {"p12", "read", "open", "touch", "dead", "reject", "window"}   # en modo TODAS van en resúmenes


def process(sym, end_ms, btc_bars, emit=True, deep=True):
    """emit=False: solo calcula (escaneo). deep=False: sin ancho P12 ni funding (ahorra peticiones)."""
    try:
        bars = klines(sym, "5m", KL_LIMIT, end_ms)
        if bars and bars[-1].t != end_ms - BAR_MS:
            time.sleep(3)
            bars = klines(sym, "5m", KL_LIMIT, end_ms)
    except (MDPaused, MDBlocked):
        return None
    if len(bars) < 300:
        if not UNIVERSE:
            log.warning(f"{sym}: pocas velas ({len(bars)})")
        return None
    td = tdate(bars[-1].t)
    if SKIP_WE and td.weekday() >= 5:
        LAST[sym] = dict(sym=sym, td=td, weekend=True)
        if LIVE:
            reconcile(sym)
        return None
    is_btc = base(sym) == "BTC"
    bb = bars if is_btc else (btc_bars or [])
    S = simulate(sym, bars, td, width_median(sym, td) if deep else None, btc_pos(bb, td, ny_ms(td, READE)),
                 btc_pos(bb, td, end_ms), is_btc, cost=cost_rt(sym), fund=funding_recent(sym) if deep else None,
                 news=news_for(td))
    if emit or not UNIVERSE:
        S["bars"] = bars
    LAST[sym] = S
    if S["incomplete"] or not emit:
        return S
    D = day_state(sym, td)
    if S["scenDone"] and not UNIVERSE:
        update_card(sym, S, D)
    for e in S["events"]:
        key = f"{e['k']}@{e['t']}"
        if key in D["sent"]:
            continue
        D["sent"].append(key)
        fresh = end_ms - e["t"] <= STALE_SEC * 1000
        if UNIVERSE and e["k"] in UNI_QUIET and not (e["k"] == "touch" and TG_TOUCH):
            continue
        try:
            handle(sym, S, e, fresh, D)
        except Exception:
            log.exception(f"{sym} evento {e['k']}")
    if S["scenDone"] and not UNIVERSE:
        update_card(sym, S, D)
    if LIVE:
        reconcile(sym)
    return S


def finished(S):
    if S is None:
        return False                       # sin datos aún (p. ej. tras reinicio): seguir procesando
    return S.get("weekend") or (S["pos"] is None and (S["trade"] is not None or S["dead"] or S["winClosed"]))


def zone_ok(S):
    """Preselección en la apertura: mismo coste/stop que la entrada, medido sobre el borde de la zona."""
    if not (S and S.get("openDone") and S["bias"] and S["szMult"] > 0 and not S["dataBad"]):
        return False
    rU = abs(S["zEdge"] - S["stop"])
    return rU > 0 and S["cost"] / 100 * S["zEdge"] / rU <= MAX_COST_R and rU / S["zEdge"] * 100 <= MAX_STOP_PCT


def read_summary(td, n_scan, acc):
    up = [base(s) for s in acc if LAST[s]["rd"] == 1]
    dn = [base(s) for s in acc if LAST[s]["rd"] == -1]
    cut = lambda xs: ", ".join(xs[:40]) + (f" … +{len(xs) - 40}" if len(xs) > 40 else "")
    return "\n".join([f"🔎 <b>P12 · lectura cerrada</b> · {DOW[td.weekday()]} {td:%d/%m} · {n_scan} monedas escaneadas",
                      f"<b>ACEPTAN ↑ ({len(up)})</b>: {cut(up) or '—'}",
                      f"<b>ACEPTAN ↓ ({len(dn)})</b>: {cut(dn) or '—'}",
                      f"Se confirma a las {loc(td, OPENM)} (apertura, filtros y coste).",
                      news_line(dict(news=news_for(td))) if NEWS_ON else ""]).strip()


def open_summary(td, n_scan, n_acc, top, rest):
    L = [f"🎯 <b>P12 · apertura {loc(td, OPENM)}</b> · {len(top) + rest} operables de {n_acc} aceptadas ({n_scan} escaneadas)"]
    for d, head in ((1, "▲ <b>COMPRAS</b> (busca retroceso a la zona)"), (-1, "▼ <b>VENTAS</b> (busca rebote a la zona)")):
        rows = [s for s in top if LAST[s]["bias"] == d]
        if rows:
            L.append(head)
            for s in rows:
                S = LAST[s]
                L.append(f"• <b>{base(s)}</b> {fp(s, S['zEdge'])}–{fp(s, S['zDeep'])} · SL {fp(s, S['stop'])}"
                         + ("" if S["coinc"] else f" · ×{S['szMult']:g}") + f" · {fvol(VOL.get(s, 0))}")
    if rest:
        L.append(f"<i>+{rest} operables fuera del top {TOP_N} por volumen</i>")
    if not top:
        L.append("✋ Hoy no hay ninguna operable.")
    else:
        L.append(f"Entrada: vela 5m que toca la zona y cierra a favor del mid · hasta {loc(td, ENTE)}. Aviso cada entrada.")
    return "\n".join(L)


def schedule(end_ms, btc):
    """Modo TODAS: escaneo completo al cerrar la lectura, preselección en la apertura y después
    solo se siguen vela a vela las monedas del top (y las posiciones abiertas)."""
    td = tdate(end_ms - BAR_MS)
    U = STATE.setdefault("uni", {})
    if U.get("td") != str(td):
        U.clear()
        U.update(td=str(td), read=False, open=False, acc=[], active=[], n_scan=0)
    live = [s for s in STATE["live"]]
    if SKIP_WE and td.weekday() >= 5:
        return live
    rd_t, op_t, en_t = ny_ms(td, READE) + BAR_MS, ny_ms(td, OPENM) + BAR_MS, ny_ms(td, ENTE)
    if not U["read"] and rd_t <= end_ms < en_t:
        build_universe()
        acc = []
        for s in list(SYMBOLS):
            try:
                S = process(s, end_ms, btc, emit=False, deep=False)
                if S and not S.get("weekend") and S.get("readDone") and S["rd"] != 0 and not S["dataBad"]:
                    acc.append(s)
            except Exception as ex:
                log.warning(f"{s}: {ex}")
        U.update(read=True, acc=acc, n_scan=len(SYMBOLS))
        save_state()
        tg.send(read_summary(td, len(SYMBOLS), acc), silent=True)
    if U["read"] and not U["open"] and op_t <= end_ms < en_t:
        ok = []
        for s in U["acc"]:
            try:
                S = process(s, end_ms, btc, emit=False, deep=WIDTH_FILTER != "OFF")
                if zone_ok(S):
                    ok.append(s)
            except Exception as ex:
                log.warning(f"{s}: {ex}")
        ok.sort(key=lambda s: -VOL.get(s, 0))
        U.update(open=True, active=ok[:TOP_N])
        save_state()
        tg.send(open_summary(td, U["n_scan"], len(U["acc"]), ok[:TOP_N], max(0, len(ok) - TOP_N)))
    act = [s for s in U["active"] if not finished(LAST.get(s))] if U["open"] else []
    return list(dict.fromkeys(act + live))


def keepalive():
    """BingX borra claves sin IP fijada tras 14 días sin uso: una llamada firmada al día."""
    if not (LIVE and BX_KEY and BX_SECRET):
        return
    today = str(datetime.now(TZ_NY).date())
    if STATE.get("keepalive") == today:
        return
    try:
        eq, av = BX.balance()
        STATE["keepalive"] = today
        log.info(f"keepalive BingX ok · equity {eq:.2f}")
    except Exception as ex:
        log.warning(f"keepalive BingX: {ex}")


def cycle(end_ms):
    t0 = time.time()
    btc = None
    try:
        btc = klines(BTC_SYMBOL, "5m", KL_LIMIT, end_ms)
    except Exception as ex:
        log.warning(f"BTC referencia: {ex}")
    if MD_BLOCK["until"] > time.time() and time.time() - MD_BLOCK["alert"] > 600:
        MD_BLOCK["alert"] = time.time()
        tg.send(f"⚠️ BingX limita las peticiones de velas hasta {datetime.fromtimestamp(MD_BLOCK['until'], TZ_LOC):%H:%M}. "
                f"Se salta ese tramo y se reanuda solo (el SL/TP de lo abierto sigue en BingX).")
    syms = schedule(end_ms, btc) if UNIVERSE else SYMBOLS
    for s in syms:
        try:
            process(s, end_ms, btc)
        except Exception as ex:
            log.warning(f"{s}: {ex}")
    try:
        guard_stops()
        equity_guard()
    except Exception:
        log.exception("guardianes")
    if UNIVERSE:                            # no guardar velas de lo que ya no se sigue
        keep = set(syms)
        for s, S in LAST.items():
            if s not in keep and S and "bars" in S:
                S.pop("bars", None)
    d = ny(end_ms)
    td = tdate(end_ms - BAR_MS)
    if in_(d.hour * 60 + d.minute, DIGEST, P12S) and not (SKIP_WE and td.weekday() >= 5) and str(td) not in STATE["digest"]:
        STATE["digest"].append(str(td))
        tg.send(digest_text(td), silent=True)
    keepalive()
    save_state()
    LAST_CYCLE[0] = time.time()
    HEALTH["last_cycle"] = datetime.now(timezone.utc).isoformat()
    log.info(f"ciclo {loc_t(end_ms)} · {len(syms)} símbolos · {time.time() - t0:.1f}s")


# ───────────────────────── BACKTEST (mismo motor) ─────────────────────────
BT_LOCK = threading.Lock()


def backtest(days, top=20):
    days = max(10, min(int(days), 365))
    syms = SYMBOLS[:max(1, top)] if UNIVERSE else SYMBOLS
    end = now_ms() // BAR_MS * BAR_MS
    start = end - (days + int(W_LOOK * 1.6) + 4) * DAY_MS
    first_td = (datetime.now(TZ_NY) - timedelta(days=days)).date()
    btc = fetch_range(BTC_SYMBOL, "5m", start, end)
    bt_btc = [b.t for b in btc]
    allT, L = [], [f"🧪 <b>Backtest P12</b> · {days} días · mismo motor que en vivo · funding real · coste por contrato"]
    for sym in syms:
        try:
            bars = btc if sym == BTC_SYMBOL else fetch_range(sym, "5m", start, end)
            ts = [b.t for b in bars]
            fund = funding_hist(sym, start)
            widths = p12_widths(bars, 140)
            is_btc = sym.split("-")[0] == "BTC"
            T, td = [], first_td
            while td <= tdate(end - BAR_MS):
                if not (SKIP_WE and td.weekday() >= 5):
                    de = ny_ms(td + timedelta(days=1), P12S)          # fin del día estadístico (18:00 de td)
                    if de <= end:
                        j = bisect.bisect_right(ts, de - BAR_MS)
                        win = bars[max(0, j - KL_LIMIT):j]
                        jb = bisect.bisect_right(bt_btc, de - BAR_MS)
                        bw = win if is_btc else btc[max(0, jb - KL_LIMIT):jb]
                        if len(win) >= 300:
                            S = simulate(sym, win, td, median_before(widths, td), btc_pos(bw, td, ny_ms(td, READE)), 0,
                                         is_btc, cost=cost_rt(sym), fund=fund, news=None)
                            if S["trade"] and not S["incomplete"]:
                                T.append(trade_rec(sym, S, S["trade"]))
                td += timedelta(days=1)
            allT += T
            k = max(1, int(len(T) * 0.7))
            tr, te = T[:k], T[k:]
            ofit = len(tr) >= 5 and len(te) >= 3 and sum(x["R"] for x in tr) > 0 and sum(x["R"] for x in te) <= 0
            L.append(f"\n<b>{sym}</b> " + agg([x["R"] for x in T], dd=True))
            if T:
                L.append(f"   entreno {agg([x['R'] for x in tr])}\n   prueba {agg([x['R'] for x in te])}" + (" ⚠️ sobreajuste" if ofit else ""))
        except Exception as ex:
            log.exception("backtest")
            L.append(f"\n<b>{sym}</b> error: {esc(ex)[:150]}")
    if allT:
        allT.sort(key=lambda x: x["td"])
        L += ["", "<b>Total</b> " + agg([x["R"] for x in allT], dd=True), "", "<b>Desgloses</b>"] + breakdown(allT)
    L.append("<i>Noticias: sin histórico del calendario (solo en vivo). Compara con el panel del Pine en 5m.</i>")
    return "\n".join(L)


def run_backtest(days, top=20):
    if not BT_LOCK.acquire(blocking=False):
        tg.send("🧪 Ya hay un backtest en marcha.")
        return
    try:
        tg.send(f"🧪 Backtest de {days} días en marcha… (descargando histórico de BingX)", silent=True)
        txt = backtest(days, top)
        for i in range(0, len(txt), 3900):
            tg.send(txt[i:i + 3900], silent=True)
        log.info("backtest terminado")
    finally:
        BT_LOCK.release()


# ───────────────────────── SWEEP: ¿qué filtro separa de verdad? ─────────────────────────
F_OFF = dict(NIGHT="NONE", BTC="OFF", MO="OFF", W="OFF", NEWS="OFF", MIS="REDUCE")
SWEEP_FILTERS = [
    ("Noche: sin 'ambos lados'", lambda t: t.get("scen") != 3),
    ("Noche: solo mismo lado alineado", lambda t: t.get("nal") == 1),
    ("BTC: no en contra", lambda t: bool(t.get("isbtc")) or t.get("btc") != -1),
    ("BTC: solo alineado", lambda t: bool(t.get("isbtc")) or t.get("btc") == 1),
    ("MO: a favor", lambda t: t.get("mo") is not False),
    ("MO: en contra", lambda t: t.get("mo") is not True),
    ("P12: sin estrechos", lambda t: t.get("wB") != 0),
    ("P12: sin anchos", lambda t: t.get("wB") != 2),
    ("P12: solo normal", lambda t: t.get("wB") == 1),
    ("Apertura coincide con el P12", lambda t: bool(t.get("coinc"))),
]


def collect_trades(days, top=20, F=None):
    """Operaciones del motor SIN filtros de entrada (una por símbolo y día). Pasa los filtros a simulate() por
    parámetro: no se tocan variables globales, así que el bot en vivo no se ve afectado."""
    F = F or F_OFF
    days = max(10, min(int(days), 365))
    syms = SYMBOLS[:max(1, top)] if UNIVERSE else SYMBOLS
    end = now_ms() // BAR_MS * BAR_MS
    start = end - (days + int(W_LOOK * 1.6) + 4) * DAY_MS
    first_td = (datetime.now(TZ_NY) - timedelta(days=days)).date()
    btc = fetch_range(BTC_SYMBOL, "5m", start, end)
    bt_btc = [b.t for b in btc]
    allT = []
    for sym in syms:
        try:
            bars = btc if sym == BTC_SYMBOL else fetch_range(sym, "5m", start, end)
            ts = [b.t for b in bars]
            fund = funding_hist(sym, start)
            widths = p12_widths(bars, 140)
            is_btc = sym.split("-")[0] == "BTC"
            td = first_td
            while td <= tdate(end - BAR_MS):
                if not (SKIP_WE and td.weekday() >= 5):
                    de = ny_ms(td + timedelta(days=1), P12S)
                    if de <= end:
                        j = bisect.bisect_right(ts, de - BAR_MS)
                        win = bars[max(0, j - KL_LIMIT):j]
                        jb = bisect.bisect_right(bt_btc, de - BAR_MS)
                        bw = win if is_btc else btc[max(0, jb - KL_LIMIT):jb]
                        if len(win) >= 300:
                            S = simulate(sym, win, td, median_before(widths, td), btc_pos(bw, td, ny_ms(td, READE)), 0,
                                         is_btc, cost=cost_rt(sym), fund=fund, news=None, F=F)
                            if S["trade"] and not S["incomplete"]:
                                allT.append(trade_rec(sym, S, S["trade"]))
                td += timedelta(days=1)
        except (MDPaused, MDBlocked) as ex:
            log.warning(f"sweep {sym}: {ex}")
        except Exception:
            log.exception(f"sweep {sym}")
    return allT


def tstat(rs):
    n = len(rs)
    if n < 3:
        return 0.0
    sd = statistics.stdev(rs)
    return (sum(rs) / n) / (sd / math.sqrt(n)) if sd > 0 else 0.0


def sweep_text(T, days=0):
    """Compara cada filtro con 'no filtrar': entrenamiento (70% de los DÍAS) vs prueba (30% final), con Bonferroni.
    Un filtro solo se recomienda si mejora la media en ambos tramos, la prueba es > 0 y t_prueba ≥ umbral corregido."""
    if len(T) < 30:
        return f"🧪 Sweep: solo {len(T)} operaciones sin filtros. Con tan pocas no se decide nada: usa más días o más símbolos."
    dias = sorted({t["td"] for t in T})
    dcut = dias[int(len(dias) * 0.7)]
    tr, te = [t for t in T if t["td"] < dcut], [t for t in T if t["td"] >= dcut]
    R = lambda xs: [x["R"] for x in xs]
    E = lambda rs: sum(rs) / len(rs) if rs else 0.0
    crit = NormalDist().inv_cdf(1 - 0.025 / len(SWEEP_FILTERS))
    L = [f"🧪 <b>Sweep P12</b> · {days} días · {len(T)} operaciones SIN filtros · prueba desde {dcut}",
         f"t crítico (Bonferroni, {len(SWEEP_FILTERS)} filtros): {crit:.2f} · decide la columna PRUEBA, no la de entrenamiento",
         f"<b>Sin filtrar</b> entren. n {len(tr)} E {E(R(tr)):+.3f}R · prueba n {len(te)} E {E(R(te)):+.3f}R t {tstat(R(te)):+.2f}", ""]
    ok = []
    for name, fn in SWEEP_FILTERS:
        ftr, fte = [t for t in tr if fn(t)], [t for t in te if fn(t)]
        if (len(ftr) == len(tr) and len(fte) == len(te)) or not ftr or not fte:
            continue   # no cambia nada, o deja sin operaciones: no hay nada que comparar
        t_te = tstat(R(fte))
        good = (len(fte) >= max(10, MIN_N // 2) and E(R(ftr)) > E(R(tr)) and E(R(fte)) > E(R(te))
                and E(R(fte)) > 0 and t_te >= crit)
        if good:
            ok.append(name)
        L.append(f"{'✅' if good else '•'} {name}: entren. n {len(ftr)} E {E(R(ftr)):+.3f} · prueba n {len(fte)} E {E(R(fte)):+.3f}R t {t_te:+.2f}")
    L.append("")
    L.append("✅ Candidatos que superan el umbral: " + ", ".join(ok) if ok else
             "Ningún filtro mejora de forma distinguible del azar con estos datos: déjalos en OFF.")
    L.append("<i>Noticias: sin histórico. Un ✅ no es una orden: confírmalo en DRY_RUN antes de activarlo.</i>")
    return "\n".join(L)


def run_sweep(days, top=20):
    if not BT_LOCK.acquire(blocking=False):
        tg.send("🧪 Ya hay un backtest o sweep en marcha.")
        return
    try:
        tg.send(f"🧪 Sweep de {days} días en marcha… (descargando histórico de BingX)", silent=True)
        txt = sweep_text(collect_trades(days, top), days)
        for i in range(0, len(txt), 3900):
            tg.send(txt[i:i + 3900], silent=True)
    except Exception as ex:
        log.exception("sweep")
        tg.send(f"🧪 Sweep falló: {esc(ex)[:200]}")
    finally:
        BT_LOCK.release()


# ───────────────────────── COMANDOS ─────────────────────────
def is_admin(uid, chat):
    if TG_ADMINS:
        return uid in TG_ADMINS
    return chat == TG_CHAT and not TG_CHAT.startswith("-")


def can_read(uid, chat):
    return chat == TG_CHAT or uid in TG_ADMINS


def on_cmd(cmd, args, uid, chat):
    if cmd == "cb":
        data, cb_id = args
        if not is_admin(uid, chat):
            tg.answer(cb_id, "Sin permiso (configura TELEGRAM_ADMIN_IDS)")
            return
        if data.startswith("close:"):
            sym = data.split(":", 1)[1]
            tg.answer(cb_id)
            tg.send(f"¿Cerrar {sym} a mercado ahora?", kb=[[{"text": "Sí, cerrar", "callback_data": f"closeok:{sym}"},
                                                             {"text": "No", "callback_data": "noop"}]])
        elif data.startswith("closeok:"):
            sym = data.split(":", 1)[1]
            tg.answer(cb_id, "Cerrando…")
            tg.send(live_close(sym, "manual") or f"{sym}: no hay posición del bot")
        else:
            tg.answer(cb_id, "Cancelado")
        return
    if not can_read(uid, chat):
        return
    if cmd in ("/estado", "/status"):
        tg.send(estado_text())
    elif cmd == "/stats":
        tg.send(stats_text())
    elif cmd == "/riesgo":
        tg.send(risk_text())
    elif cmd == "/hoy":
        dflt = (STATE.get("uni", {}).get("active") or []) if UNIVERSE else SYMBOLS
        for s in ([_sym(args[0])] if args else dflt):
            S = LAST.get(s)
            if not S or S.get("weekend") or not S.get("scenDone"):
                tg.send(f"{s}: " + ("fin de semana" if S and S.get("weekend") else "P12 aún formándose / sin datos"))
            else:
                tg.photo(chart_png(S), card_text(S)[:1024], kb=buttons(s))
    elif cmd == "/backtest":
        days = int(args[0]) if args and args[0].isdigit() else 90
        top = int(args[1]) if len(args) > 1 and args[1].isdigit() else 20
        threading.Thread(target=run_backtest, args=(days, top), daemon=True).start()
    elif cmd == "/sweep":
        days = int(args[0]) if args and args[0].isdigit() else 180
        top = int(args[1]) if len(args) > 1 and args[1].isdigit() else 20
        threading.Thread(target=run_sweep, args=(days, top), daemon=True).start()
    elif cmd == "/hwm":
        if not is_admin(uid, chat):
            tg.send("Sin permiso.")
        else:
            try:
                STATE["hwm"], STATE["hwm_acct"] = BX.balance()[0], acct_id()
                save_state()
                tg.send(f"Máximo de equity reiniciado a {STATE['hwm']:.2f} USDT.")
            except Exception as ex:
                tg.send(f"No se pudo leer el equity: {esc(ex)[:120]}")
    elif cmd in ("/pausa", "/reanuda", "/cerrar"):
        if not is_admin(uid, chat):
            tg.send("Sin permiso: configura TELEGRAM_ADMIN_IDS con tu id de usuario.")
            return
        if cmd == "/pausa":
            STATE["paused"] = True
            save_state()
            tg.send("⏸ Pausado: las señales siguen, no se ejecutan entradas nuevas.")
        elif cmd == "/reanuda":
            STATE["paused"] = False
            STATE["risk"]["consec"] = 0
            real_risk()["consec"] = 0
            try:
                if LIVE and not DRY_RUN:
                    STATE["hwm"], STATE["hwm_acct"] = BX.balance()[0], acct_id()
            except Exception as ex:
                log.warning(f"reanuda: {ex}")
            save_state()
            tg.send("▶️ Reanudado (racha de pérdidas y máximo de equity reiniciados).")
        else:
            if not args:
                tg.send("Uso: /cerrar SÍMBOLO")
            else:
                s = _sym(args[0])
                tg.send(live_close(s, "manual") or f"{s}: no hay posición del bot")
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


def watchdog():
    warned = False
    while True:
        time.sleep(60)
        late = time.time() - LAST_CYCLE[0] > WATCHDOG_MIN * 60
        if late and not warned:
            tg.send(f"🚨 P12 bot: sin ciclos desde hace más de {WATCHDOG_MIN} min (BingX o red). Revisa Railway.")
            warned = True
        elif not late and warned:
            tg.send("✅ P12 bot: ciclos recuperados.", silent=True)
            warned = False


STARTUP_NOTE = []


def main():
    log.info(f"{CODE_VERSION} · MODE={MODE} DRY_RUN={DRY_RUN} · {SYMBOLS} · estado {SF}")
    load_state()
    for k, v in (("risk", {}), ("live_res", []), ("keepalive", "")):
        STATE.setdefault(k, v)
    if UNIVERSE:
        build_universe()
    else:
        load_contracts()
    threading.Thread(target=lambda: HTTPServer(("0.0.0.0", PORT), _H).serve_forever(), daemon=True).start()
    if LIVE and not DRY_RUN:
        if not (BX_KEY and BX_SECRET):
            raise SystemExit("MODE=LIVE con DRY_RUN=false necesita BINGX_API_KEY y BINGX_SECRET_KEY")
        sync_time()
        eq, av = BX.balance()
        USER_TAKER[0] = BX.commission()
        log.info(f"BingX hedge={BX.is_hedge()} equity={eq:.2f} disponible={av:.2f} taker={USER_TAKER[0]}")
        dd_update(eq)
        try:
            pos = BX.all_positions()
            ajenas = [f"{x.get('symbol')} {x.get('positionSide', '')}" for x in pos if x.get("symbol") not in STATE["live"]]
            huerf = [s for s, L_ in STATE["live"].items() if not L_.get("dry") and not any(x.get("symbol") == s for x in pos)]
            audit = f"\n🔎 Cuenta {'DEMO' if 'vst' in BX_BASE else 'REAL'}: equity {eq:.2f} USDT · {len(pos)} posiciones abiertas"
            if ajenas:
                audit += f" (ajenas al bot: {', '.join(ajenas[:6])})"
            if huerf:
                audit += f"\n⚠️ El estado guarda posiciones que ya no existen en BingX: {', '.join(huerf)} (se concilian solas)"
            STARTUP_NOTE.append(audit)
        except Exception as ex:
            log.warning(f"auditoría de cuenta: {ex}")
    if not tg.on:
        log.warning("Telegram sin configurar: solo logs")
    tg.set_commands()
    td = tdate(now_ms())
    warn = ""
    if LIVE and not STATE_DIR.startswith("/data"):
        warn = "\n⚠️ <b>Sin volumen en /data</b>: un redeploy con posición abierta pierde su gestión. Añade un Volume."
    tg.send(f"🤖 <b>P12 Hunter bot</b> · {CODE_VERSION}\nModo <b>{MODE}</b>" + (" · DRY_RUN" if LIVE and DRY_RUN else "")
            + (f" · TODAS las monedas (vol ≥ {fvol(MIN_VOL_USDT)}, máx {MAX_UNIVERSE}, top {TOP_N})" if UNIVERSE else f" · {', '.join(SYMBOLS)}") + "\nP12 {loc(td, P12S)}–{loc(td, P12E)} · lectura hasta {loc(td, READE)} · apertura {loc(td, OPENM)}"
            + f" · entradas hasta {loc(td, ENTE)} (hora {TZ_LOC.key})"
            + ("" if UNIVERSE else f"\nCoste {', '.join(f'{base(s)} {cost_rt(s):.3f}%' for s in SYMBOLS)}")
            + f"{warn}{''.join(STARTUP_NOTE)}\n/ayuda", silent=True)
    if TG_COMMANDS and tg.on:
        threading.Thread(target=tg.poll, args=(on_cmd,), daemon=True).start()
    threading.Thread(target=watchdog, daemon=True).start()
    if BACKTEST_DAYS > 0:
        threading.Thread(target=run_backtest, args=(BACKTEST_DAYS,), daemon=True).start()
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

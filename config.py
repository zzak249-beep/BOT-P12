"""Configuración por variables de entorno (Railway > Variables)."""
import os


def _b(v: str) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "si", "sí", "on")


class Config:
    def __init__(self):
        e = os.getenv
        # ── Ejecución ──
        self.dry_run = _b(e("DRY_RUN", "true"))          # true = solo señales + paper trading
        self.demo = _b(e("BINGX_DEMO", "false"))         # true = cuenta demo VST de BingX
        self.api_key = e("BINGX_API_KEY", "")
        self.api_secret = e("BINGX_API_SECRET", "")
        self.tg_token = e("TELEGRAM_TOKEN", "")
        self.tg_chat = e("TELEGRAM_CHAT_ID", "")
        self.symbol = e("SYMBOL", "BTC/USDT:USDT")
        self.symbols = e("SYMBOLS", "").strip()          # "" = solo SYMBOL | "A,B" | "TOP:30" | "ALL"
        self.min_volume = float(e("MIN_VOLUME_USDT", "10000000"))  # volumen 24h mínimo (modo ALL/TOP)
        self.max_symbols = int(e("MAX_SYMBOLS", "150"))
        self.exclude = {x.strip().upper() for x in e("EXCLUDE", "").split(",") if x.strip()}
        self.max_open = int(e("MAX_OPEN_POSITIONS", "3"))
        self.max_total_risk = float(e("MAX_TOTAL_RISK_PCT", "1.0"))  # suma de riesgo de posiciones abiertas
        self.leverage = int(e("LEVERAGE", "10"))
        self.equity_override = float(e("EQUITY_OVERRIDE", "0"))  # 0 = leer saldo real
        self.paper_equity = float(e("PAPER_EQUITY", "1000"))
        self.state_path = e("STATE_PATH", "state.json")
        self.tz_local = e("TZ_LOCAL", "Europe/Madrid")
        self.warmup_days = int(e("WARMUP_DAYS", "25"))
        # ── Horario (HHMM hora Nueva York) ──
        self.p12_start = int(e("P12_START", "1800"))
        self.london_start = int(e("LONDON_START", "230"))
        self.p12_end = int(e("P12_END", "600"))
        self.read_end = int(e("READ_END", "900"))
        self.open = int(e("OPEN", "930"))
        self.last_entry = int(e("LAST_ENTRY", "1200"))
        self.force_exit = int(e("FORCE_EXIT", "1555"))
        self.skip_weekends = _b(e("SKIP_WEEKENDS", "true"))
        # ── Aceptación ──
        self.accept_mode = e("ACCEPT_MODE", "mp")        # mp | candles
        self.mp_n = int(e("MP_N", "2"))
        self.mp_m = int(e("MP_M", "3"))
        self.accept_min = int(e("ACCEPT_MIN", "30"))
        self.rvol_min = float(e("RVOL_MIN", "0"))
        # ── Filtros (por defecto todos apagados, igual que el Pine) ──
        self.night_filter = e("NIGHT_FILTER", "none")    # none | no_both | same_side
        self.coinc_mode = e("COINC_MODE", "mid")         # mid | outside
        self.mismatch = e("MISMATCH", "reduce")          # reduce | discard
        self.reduce_f = float(e("REDUCE_F", "0.5"))
        self.mo_filter = e("MO_FILTER", "off")           # off | with | against
        self.w_look = int(e("W_LOOK", "20"))
        self.w_narrow = float(e("W_NARROW", "0.75"))
        self.w_wide = float(e("W_WIDE", "1.33"))
        self.width_filter = e("WIDTH_FILTER", "off")     # off | no_narrow | no_wide | normal_only
        self.event_filter = e("EVENT_FILTER", "off")     # off | exclude | only
        self.event_dates = [x.strip() for x in e("EVENT_DATES", "").split(",") if x.strip()]
        self.rvol_read_filter = e("RVOL_READ_FILTER", "off")  # off | min | size  (volumen ventana lectura vs mediana)
        self.rvol_read_min = float(e("RVOL_READ_MIN", "0.8"))
        self.daily_mom = e("DAILY_MOM", "off")           # off | with | against (signo retorno 24h vs sesgo)
        self.vol_filter = e("VOL_FILTER", "off")         # off | no_high | no_low | normal_only
        self.vol_narrow = float(e("VOL_NARROW", "0.70"))
        self.vol_wide = float(e("VOL_WIDE", "1.50"))
        # ── Entrada / salida ──
        self.atr_len = int(e("ATR_LEN", "14"))
        self.stop_buf = float(e("STOP_BUF", "0.25"))
        self.tgt_mode = e("TGT_MODE", "fixed_r")         # fixed_r | extension
        self.rr = float(e("RR", "2.0"))
        self.ext_k = float(e("EXT_K", "1.0"))
        self.be_r = float(e("BE_R", "0"))
        self.partial_r = float(e("PARTIAL_R", "1.0"))
        self.partial_pct = float(e("PARTIAL_PCT", "50"))
        self.trail_act_r = float(e("TRAIL_ACT_R", "1.5"))
        self.trail_mode = e("TRAIL_MODE", "atr")         # atr | r
        self.trail_atr = float(e("TRAIL_ATR", "1.0"))
        self.trail_r = float(e("TRAIL_R", "0.8"))
        self.max_hours = float(e("MAX_HOURS", "6"))
        self.risk_pct = float(e("RISK_PCT", "0.5"))
        # ── Racha ──
        self.loss_n = int(e("LOSS_STREAK_N", "3"))
        self.loss_action = e("LOSS_ACTION", "reduce")    # reduce | pause | ignore
        self.loss_reduce = float(e("LOSS_REDUCE", "0.5"))
        # ── Costes ──
        self.cost_rt = float(e("COST_RT", "0.12"))
        self.stop_slip = float(e("STOP_SLIP", "0.05"))
        self.entry_slip = float(e("ENTRY_SLIP", "0.02"))
        self.max_cost_r = float(e("MAX_COST_R", "0.20"))
        self.max_stop_pct = float(e("MAX_STOP_PCT", "2.5"))
        self.fund_h = int(e("FUND_H", "8"))
        self.fund_pct = float(e("FUND_PCT", "0.01"))      # % por periodo, se descuenta del R
        # ── Freno de seguridad ──
        self.max_dd_r = float(e("MAX_DD_R", "8"))         # pausa automática si el DD supera X R (0 = off)

# P12 Hunter Bot — BingX + Telegram

Port a Python de la estrategia **P12 Hunter v4.6** (Pine v6): lectura de la sesión asiática/Londres (rango P12),
aceptación Market Profile 30m, entrada por confirmación 5m en la zona P12→mid, parcial, trailing, time stop y racha de pérdidas.

## Qué hace
- Descarga velas 5m de BingX, reconstruye el estado (anchos P12, ATR) con 25 días de historial y opera en cada cierre de vela.
- **Telegram**: lectura del día, entrada, parcial, BE, trailing, cierre (con R) y errores. Comandos: `/status /stats /pause /resume /close /resetdd /help`.
- **BingX**: orden de mercado + SL y TP como órdenes condicionales en el exchange (la posición queda protegida si el bot cae). El bot mueve el SL (BE/trailing) y cierra por time stop o 15:55 NY.
- Si no puede colocar el SL, **cierra la posición** por seguridad.

## Multi-moneda (v1.2)
- `SYMBOLS=ALL` opera todas las perpetuas USDT de BingX que pasen los filtros; `TOP:30` las 30 de mayor volumen; o una lista separada por comas.
- **24/7 automático:** al arrancar descarta símbolos con huecos en las velas (acciones/materias primas tokenizadas), con poco historial (<3 días) o con volumen 24h < `MIN_VOLUME_USDT`. Una vez al día añade listados nuevos.
- **Límites globales:** `MAX_OPEN_POSITIONS` y `MAX_TOTAL_RISK_PCT` se aplican entre todas las monedas (las altcoins se mueven con BTC: 3 posiciones suelen ser una sola apuesta).
- Telegram solo avisa de monedas con operación posible hoy. `/status`, `/diag BTC`, `/close BTC|all`, `/symbols`.
- Con muchas monedas el arranque tarda minutos (calentamiento de velas) y cada vela de 5 min implica una petición por moneda.

## Seguridad añadida (v1.1)
- **Freno por drawdown**: si el DD acumulado supera `MAX_DD_R` (8R por defecto) el bot se pausa solo y te avisa. `/resume` + `/resetdd` para continuar.
- **Funding** descontado del R de cada operación (`FUND_H`, `FUND_PCT`).
- **Reintentos** automáticos ante errores de red/rate limit de BingX.
- `/stats`: operaciones, acierto, expectativa, PF, DD y últimas 10. `backtest.py` ahora da resumen mensual y `--csv`.

## ¿No abre trades? (orden de diagnóstico)
1. `/diag` en Telegram: dice si estás en DRY_RUN (solo simula), si hay pausa, el equity, la cantidad mínima y por qué hoy no se opera.
2. `/testorder` (solo con `DRY_RUN=false`): compra la cantidad mínima, coloca SL/TP lejanos y cierra. Si falla, el mensaje trae el error exacto de BingX.
3. La estrategia es selectiva: máximo 1 operación/día, solo entre 09:30 y 12:00 Nueva York (15:30–18:00 Madrid), nunca en fin de semana.

## Filtros de volumen y momentum (opcionales, apagados por defecto)
- `RVOL_READ_FILTER=min|size` + `RVOL_READ_MIN`: volumen de la ventana de lectura (06:00–09:00 NY) frente a la mediana de los últimos días. `min` descarta el día si es bajo; `size` escala el tamaño (×0,5–×1,5).
- `DAILY_MOM=with|against`: exige que el signo del retorno de las últimas 24 h coincida (o no) con el sesgo del día.
- `RVOL_MIN`: volumen mínimo en las velas de aceptación (ya existía).
- **No los actives sin probarlos:** `python compare.py --days 365` compara cada variante con la base, separando entrenamiento (70%) y test (30%), y calcula el umbral de |t| ajustado por el número de variantes probadas.

## Modos (sube de nivel solo cuando el anterior funcione)
1. `DRY_RUN=true` → solo señales por Telegram + paper trading simulado. No necesita API keys de BingX.
2. `DRY_RUN=false` + `BINGX_DEMO=true` → órdenes reales en la cuenta demo VST de BingX.
3. `DRY_RUN=false` + `BINGX_DEMO=false` → dinero real. Empieza con `RISK_PCT=0.1` y capital pequeño.

## Puesta en marcha
1. **Telegram**: habla con @BotFather → `/newbot` → copia el token. Escribe algo a tu bot y abre
   `https://api.telegram.org/bot<TOKEN>/getUpdates` para ver tu `chat.id`.
2. **GitHub**: sube esta carpeta a un repo **privado**.
3. **Railway**: New Project → Deploy from GitHub → añade las variables de `.env.example`.
   Añade un **Volume** montado en `/data` y `STATE_PATH=/data/state.json` (así sobrevive a redeploys la posición abierta y la racha).
4. **BingX** (modo real): API Key con permiso **solo Futuros/Trade**, **sin retiros**. Cuenta de Futuros Perpetuos con USDT.
   El bot activa modo hedge, margen aislado y apalancamiento en el símbolo.
5. Revisa los logs de Railway: debe aparecer "Calentamiento OK" y el mensaje 🤖 en Telegram.

## Validar antes de arriesgar
```
pip install -r requirements.txt
python tests/test_manage.py            # gestión de la posición
python backtest.py --days 90           # mismo motor sobre velas reales de BingX (públicas)
```
Compara el resultado con el panel del Pine en TradingView (mismo símbolo y periodo). Si no se parecen, no pases a real.

## Diferencias con el Pine (léelas)
- Solo implementado: Cripto 24/7, entrada **Confirmación 5m**. No incluye: modo "Límite en zona", filtro BTC, mercado Índices USA.
- Entrada: orden de mercado al cierre de la vela de señal (Pine: apertura de la siguiente). Salida de SL/TP: stop-market en el exchange.
- El trailing **nunca afloja** el SL (el Pine podía dejarlo por debajo del SL original).
- La racha cuenta la operación completa (parcial + resto) como una sola.
- Las velas de BingX pueden diferir ligeramente de las de TradingView (volumen/ATR).
- Con SL fallido o reinicio sin Volume, el bot avisa y no gestiona posiciones que no conoce.

## Variables
Todos los inputs del Pine tienen su variable en `config.py` (mismos valores por defecto), p. ej. `RR`, `PARTIAL_R`,
`TRAIL_ACT_R`, `MAX_HOURS`, `LOSS_STREAK_N`, `MAX_STOP_PCT`, `NIGHT_FILTER`, `WIDTH_FILTER`…

> Aviso: software sin garantía. No es asesoramiento financiero. El trading con apalancamiento puede hacerte perder todo el capital.
> La estrategia no está validada estadísticamente; el backtest sintético solo comprueba que el código funciona.

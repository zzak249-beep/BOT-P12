# P12 bot 1.3.0 — qué cambia y por qué

## Fallos corregidos (todos con prueba en `test_p12.py`)
| Problema en 1.2.0 | Efecto con dinero real | Arreglo |
|---|---|---|
| El límite de pérdida diaria y la pausa por racha sumaban las operaciones **hipotéticas de todo el top seguido**, no las ejecutadas | Podía pausarse sin haber perdido nada, o no pararse cuando las pérdidas reales sí llegaban | Con LIVE y DRY_RUN=false mandan las **R reales** de BingX (`finish_live`); las hipotéticas quedan solo como estadística |
| `md_get` no conocía 109429 (bloqueo por exceso de errores) ni 109415 (contrato pausado) | Seguía llamando a una ruta bloqueada, alargando el bloqueo | Respeta la hora de reintento que da BingX y aparta 6 h los contratos pausados |
| Nada comprobaba que SL/TP siguieran vivos tras abrir | Una posición podía quedarse sin stop | `guard_stops()` cada ciclo: repone SL/TP si faltan (y no inventa nada si la consulta falla) |
| `MAX_POS` solo contaba posiciones de este bot | Con otro bot en la misma cuenta, exposición doble | `MAX_ACCOUNT_POS` cuenta TODA la cuenta |
| Sin cortacircuitos de equity | Solo había límites en R | `MAX_DD_PCT` (8%) pausa si el equity cae desde su máximo; el máximo es por cuenta (no arrastra el de demo) |
| Sin auditoría al arrancar | Posiciones ajenas o huérfanas pasaban desapercibidas | Mensaje de arranque con equity, posiciones abiertas y las ajenas al bot |

## Herramienta nueva: `/sweep [días] [top]`
Ejecuta el motor **sin ningún filtro** y compara cada uno con "no filtrar": 70 % de los días para entrenar, 30 % final para probar, con corrección de Bonferroni. Un filtro solo sale ✅ si mejora en ambos tramos, la prueba es > 0 y t ≥ umbral. No toca las variables del bot en vivo (los filtros se pasan por parámetro a `simulate`).

## Qué NO está verificado
- Todo se ha probado con un exchange simulado y velas sintéticas, **no contra BingX real**.
- No hay datos que demuestren que la estrategia P12 gane: eso lo dice `/sweep` y `/backtest` sobre tus pares, y luego DRY_RUN.
- El sweep no incluye noticias (el calendario no tiene histórico).

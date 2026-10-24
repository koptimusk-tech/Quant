# Investigación, backtesting y validación (Fase 2)

## Contrato de hipótesis (`qal/research/contract.py`)

Campos obligatorios: título, premisa (≥ 40 caracteres), mecanismo (≥ 40), anomalías de origen, universo
(datasets concretos, inmutables), variables, estrategia (tipo, parámetros fijos y rejilla a explorar), reglas de
entrada y salida, horizonte, benchmark, costes por dataset (o valores por defecto de la clase de activo), riesgos,
condiciones de invalidación, criterios de éxito y diseño fuera de muestra.

Se rechaza: lenguaje vago ("parece que va a subir", "obviously", "garantizado"…), parámetros desconocidos,
número de activos incompatible con la estrategia, rejillas de más de 200 variantes y embargo menor que el
horizonte (purging). El contrato se guarda con su hash y **no se puede editar**: se crea una versión nueva
(`parent_id`) y los ensayos de todas las versiones se cuentan juntos.

## Hipótesis sugeridas (`qal/research/proposals.py`)

El Research Lab propone hipótesis a partir de los datos y anomalías cargados (`GET /research/proposals`). Se
recalculan de forma determinista cada vez y **no ejecutan nada**: son contratos válidos que la persona revisa,
registra y valida a mano ("Use this proposal" rellena el formulario). Plantillas:

| Tipo | Cuándo aparece | Rejilla (variantes = ensayos) | Prioridad (orden, no probabilidad) |
|---|---|---|---|
| Momentum de serie temporal | Datasets 1d o 1wk con historia suficiente | lookback 20/60/120 (1d) o 4/12/26 (1wk) | 0,6 fija |
| Reacción tras shocks | ≥ 10 `return_shock` detectados en el dataset | modo reversión/continuación × 3 periodos de tenencia | 0,25 + 0,35·min(n/100, 1) + 0,3 si alguno es significativo tras BH |
| Reversión del spread | Dos datasets de la misma frecuencia y clase, correlación de retornos ≥ 0,5 en ≥ 300 barras | entry_z 2,0/2,5 | 0,3 + 0,4·(corr − 0,5)/0,5 |

El diseño fuera de muestra se dimensiona con la longitud del dataset: holdout en el último 20 % del tiempo,
train = 40 % del tramo de investigación (entre 50 y 2000 barras), test = 10 % (entre 10 y 500) y embargo igual al
horizonte. Las propuestas sobre datos reales van antes que las sintéticas, y las que ya están registradas (misma
estrategia e instrumentos) bajan al final con un aviso: volver a probarlas suma ensayos a la misma familia.

## Estrategias de reglas (`qal/research/strategies.py`)

| Tipo | Descripción |
|---|---|
| `event` | Tras un evento de un detector (decidido al cierre de la barra), posición a favor (`continuation`) o en contra (`reversal`) durante `hold_bars`. |
| `ts_momentum` | Signo del retorno de `lookback` barras, opcionalmente con objetivo de volatilidad y activo solo en un régimen de volatilidad (`regime`: `all`, `high_vol`, `low_vol`; ratio de volatilidad realizada corta/larga conocido al cierre). |
| `pair_mean_reversion` | Spread de OLS rodante (ventana que termina en t-1); entrada en `entry_z`, salida en `exit_z` o `max_hold`. Exposición bruta 1. |
| `cross_sectional_factor` | Universo de 3 o más instrumentos ordenado por un factor de precios (`momentum`: retorno de t-lookback a t-skip; `low_vol`: menor volatilidad); largo el cuantil superior, corto el inferior, neutral en dólares, rebalanceo cada `rebalance_bars`. |
| `buy_and_hold` | Benchmark. |

## Motor de backtesting (`qal/research/backtest.py`)

- Pesos decididos al cierre de t, ejecutados en la **apertura de t+1**. Retorno de la barra = tramo nocturno con
  la posición anterior + tramo intrabarra con la nueva.
- Costes por lado: comisión + medio spread + slippage fijo + fracción del rango de la barra anterior. Costes de
  mantenimiento: préstamo de cortos, financiación de largos, funding diario (perpetuos), rollover periódico.
- Liquidez: lo que supere `max_participation` del volumen en dólares de la barra anterior no se ejecuta en esa barra
  (se sigue trabajando la orden) y se cuenta como incumplimiento de capacidad.
- Exposición bruta limitada a 1.

## Validación (`qal/research/validation.py`)

1. **Separación**: las barras disponibles antes de `holdout_start` forman el periodo de investigación. El holdout no
   se toca en la validación.
2. **Walk-forward**: ventanas train → embargo → test, avanzando `test_bars`. En cada ventana se elige la variante
   con mejor métrica **en train** y se evalúa en test. Los tests se encadenan como resultado fuera de muestra (OOS).
3. **Robustez**: sensibilidad a costes (0×, 0.5×, 1×, 2×, 3×), rejilla de parámetros (in-sample, etiquetada como tal),
   fracción de ventanas OOS positivas, régimen de alta/baja volatilidad (volatilidad previa conocida), bootstrap
   estacionario por bloques del Sharpe, placebo por desplazamiento circular de la trayectoria de posiciones,
   concentración en las 5 mejores operaciones.
4. **Sobreajuste**: Deflated Sharpe Ratio con el número de ensayos de toda la familia (incluidas ejecuciones
   fallidas) y la varianza de Sharpe entre variantes; PBO por CSCV cuando hay más de una variante.
5. **Leakage**: causalidad de la estrategia (truncado al 60 % y 85 %), retraso de ejecución (impulso), aislamiento
   del holdout y separación temporal de cada ventana.
6. **Test final**: endpoint explícito con confirmación; cada acceso queda contado y auditado.

`purged_kfold` implementa la validación cruzada purgada con embargo para etiquetas que se solapan; la triple barrera
y los modelos supervisados que la usarán llegan en la Fase 5.

## Hard gates (`qal/research/policy.py`)

| Gate | Bloquea si |
|---|---|
| A | algún dataset tiene calidad `fail` |
| B | falla una comprobación de leakage, hay menos operaciones OOS que `min_trades`, la métrica OOS no alcanza `min_value`, el DSR < `dsr_min`, el placebo p > `placebo_max_pvalue` (y, si se exige, el IC bootstrap incluye 0) |
| C | pocas ventanas OOS positivas, pocas variantes de parámetros positivas, drawdown excesivo, beneficio concentrado en 5 operaciones, PBO > `max_pbo` |
| D | la ventaja desaparece con `cost_multiplier_survive`× costes o hay demasiados incumplimientos de capacidad |
| E, F | no implementados: cualquier promoción a paper trading, aprobación de riesgo o ejecución real queda bloqueada |

Las decisiones se guardan en `gate_decisions` con su versión de política. La transición a `VALIDATION_PASSED` /
`VALIDATION_FAILED` solo la ejecuta el motor de políticas; la API rechaza promociones manuales (409).

## Estados de una hipótesis

`HYPOTHESIS_FORMULATED → EXPERIMENT_PENDING → IN_SAMPLE_EVALUATED → OUT_OF_SAMPLE_EVALUATED →
VALIDATION_PASSED | VALIDATION_FAILED`, y `VALIDATION_PASSED → PAPER_TRADING_ELIGIBLE`. Cada paso posterior a
`EXPERIMENT_PENDING` lo da solo el motor de políticas, con su evidencia en la auditoría; no hay saltos. Las filas
antiguas en `BACKTEST_PENDING` siguen funcionando. `PAPER_TRADING_ELIGIBLE` (`qal/research/promotion.py`) exige
`VALIDATION_PASSED`, gates A–D superados, exactamente un acceso al holdout que cumpla la métrica principal y el
drawdown prerregistrados, costes explícitos por dataset, límites de riesgo y la aprobación explícita de una persona;
los agentes no pueden aprobar. Solo marca un candidato: no se envían órdenes y la ejecución real sigue desactivada.

## Alpha Discovery Engine (`qal/alpha/`)

Un ciclo (`alpha-cycle` en la CLI, `POST /alpha/runs` en la API) es un flujo fijo de cuatro agentes deterministas,
sin modelo de lenguaje (0 tokens) y sin debate abierto:

1. **Alpha Researcher** (`agents.AlphaResearcher`) genera ideas por tres rutas:
   - *anomalías*: reversión y continuación tras shocks de retorno (con los parámetros del último escaneo), momentum
     condicionado al régimen de volatilidad, reversión de divergencias de pares;
   - *factores* (`factors.py`): momentum y baja volatilidad transversales sobre universos de 3+ instrumentos diarios
     comparables; Value y Quality se declaran no comprobables porque no hay fundamentales point-in-time;
   - *relaciones* (`relationships.py`): todas las parejas comparables se prueban en el periodo de investigación
     (primer 80 % de las barras comunes) con cointegración de Engle-Granger, correlación de retornos, estabilidad de
     la correlación entre mitades y vida media del residuo; BH sobre todas las pruebas; solo se proponen parejas
     cointegradas tras BH con vida media entre 1 y 60 barras. Todas las combinaciones quedan guardadas.
   También momentum de serie temporal como efecto documentado (ruta `prior`) y huecos de datos (volumen de ticks
   en FX, futuros/spot/vencimientos, fundamentales).
2. **Quant Analyst** convierte cada idea en un contrato congelado: variables, regla, horizonte, costes, métrica
   principal y umbrales prerregistrados, diseño walk-forward con embargo ≥ horizonte y holdout aislado del 20 %.
3. **Adversarial Reviewer** objeta con referencias a registros guardados. Bloquean: duplicado, mezcla de datos
   sintéticos y reales, menos eventos antes del holdout que `min_trades` (evidencia insuficiente) o pocos
   rebalanceos. Mayores: el origen leyó el periodo del holdout, veredicto previo sin evidencia, relación inestable o
   no cointegrada, costes por defecto, universo pequeño, sesgo largo contra efectivo. Menores: calidad `warn`,
   solapamiento de tenencias, horario FX intradía, volatilidad no es dirección, multiplicidad.
4. **Research Coordinator** deduplica (estrategia + mecanismo + instrumentos), registra lo que no tiene objeciones
   bloqueantes (las mayores quedan como limitaciones del contrato), ejecuta `run_validation` para las mejores dentro
   del presupuesto (`Budget`: ideas, hipótesis, experimentos, variantes, segundos) y deja cada decisión con su motivo.

Todos los mensajes (`agent_messages`) se guardan, también los de ideas rechazadas. Los números que citan los agentes
se copian de registros guardados; los resultados oficiales los calculan el motor de validación y el de políticas.
La UI (**Alpha Research**) separa prioridad de investigación (heurística), evidencia predictiva (walk-forward y
gates) y rentabilidad neta validada (holdout una vez, costes explícitos, aprobación).

## Limitaciones conocidas

- Los factores transversales usan pesos iguales y universos pequeños; la gestión de cartera multiactivo llega en la Fase 6.
- Las anomalías y estudios de eventos que originan ideas leen todo el dataset, periodo del holdout incluido: el holdout no toca los backtests, pero no es independiente de la elección de la idea (queda anotado en el contrato).
- Sin contratos de futuros individuales ni calendario de vencimientos (el rollover es un coste periódico supuesto).
- El placebo usa retornos cierre a cierre para la estrategia real y para las simulaciones (comparación coherente,
  aproximada respecto al motor apertura/cierre).
- Los umbrales por defecto de los criterios son orientativos: cada contrato debe justificar los suyos.

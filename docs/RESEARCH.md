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

## Estrategias de reglas (`qal/research/strategies.py`)

| Tipo | Descripción |
|---|---|
| `event` | Tras un evento de un detector (decidido al cierre de la barra), posición a favor (`continuation`) o en contra (`reversal`) durante `hold_bars`. |
| `ts_momentum` | Signo del retorno de `lookback` barras, opcionalmente con objetivo de volatilidad. |
| `pair_mean_reversion` | Spread de OLS rodante (ventana que termina en t-1); entrada en `entry_z`, salida en `exit_z` o `max_hold`. Exposición bruta 1. |
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

## Limitaciones conocidas

- Universos de uno o dos activos por hipótesis; carteras multiactivo en la Fase 6.
- Sin contratos de futuros individuales ni calendario de vencimientos (el rollover es un coste periódico supuesto).
- El placebo usa retornos cierre a cierre para la estrategia real y para las simulaciones (comparación coherente,
  aproximada respecto al motor apertura/cierre).
- Los umbrales por defecto de los criterios son orientativos: cada contrato debe justificar los suyos.

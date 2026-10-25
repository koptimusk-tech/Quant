# Robustness Lab (Fase 11)

El Robustness Lab evalúa un candidato del Experiment Lab (`COMPLETED`) en otros activos, periodos y regímenes,
con parámetros perturbados, con costes y ejecución peores, por capacidad, en escenarios de estrés y por su
contribución a una cartera de referencia. Es solo investigación y simulación:

- ninguna ruta puede enviar órdenes ni conecta con brokers;
- no se modifican resultados del laboratorio, datasets, hipótesis, sesiones de paper trading ni políticas de riesgo;
- un estado de robustez describe las pruebas realizadas bajo sus supuestos: no es una previsión, no es una
  probabilidad de éxito, no sustituye la validación estadística y no promociona nada.

## 1. Qué se reutiliza

| Necesidad | Componente existente |
|---|---|
| Reconstruir el candidato y su selección walk-forward | `lab.pipeline.Pipeline` (`integrity_pre`) y `research.validation.Validator.walk_forward` |
| Backtest con costes, retraso y límite de participación | `research.backtest.run_backtest`, `research.costs.CostModel` |
| Costes por clase de activo cuando no hay costes declarados | `research.costs.default_cost_model` (estimaciones) |
| Rupturas por año, longitud de entrenamiento y subconjuntos del universo | etapa `D_robustness` del pipeline del laboratorio |
| Decisión y p ajustado con Benjamini-Hochberg en todo el laboratorio | `lab.service.ranking`, `lab.selection.decide` |
| Bloqueo de órdenes con datos no fiables | `paper.feed.check_bar` (la misma puerta que usa el motor de paper trading) |
| Cola, cancelación y persistencia | `jobs.runner` (trabajo `robustness_run`) y la tabla `experiments` (kind `robustness_run`) |

Lo nuevo está en `backend/qal/robustness/` (`engine.py`, `report.py`, `service.py`), `api/routers/robustness.py`,
el comando `robustness-run` y la página `/robustness`.

## 2. Matriz por mercado y activo

Para estrategias de un solo activo, el candidato se evalúa en:

- **design**: el activo para el que se diseñó;
- **same_market**: otros activos de la misma clase;
- **other_market**: otras clases de activo.

Solo se aceptan datasets compatibles: el mismo timeframe, calidad distinta de `fail` y el mismo tipo de dato (sintético
con sintético, real con real). En cada activo destino se repite la misma rejilla y la misma selección walk-forward
sobre sus propios datos ("re-selected") y, aparte, se aplican sin cambios los parámetros elegidos en el activo de
diseño ("transfer"). El periodo final reservado nunca se usa.

Cada fila registra activo, mercado, timeframe, periodo, fuente y calidad, costes y su origen, método de validación,
barras y operaciones, métricas brutas y netas, estado y motivo. Los costes de los destinos son los de su clase de
activo y se etiquetan como **estimaciones**.

Para que activos muy correlacionados no cuenten como evidencia independiente:

- se calcula la correlación diaria con el activo de diseño;
- las filas con |ρ| ≥ 0,8 se marcan como `near_duplicate_of_design`;
- se informa el número efectivo de activos independientes, n_eff = n² / Σρ².

La dimensión `cross_asset` exige n_eff ≥ 2.

Las estrategias de pares y de varios activos devuelven `not_applicable`, y la dimensión queda en `INSUFFICIENT_DATA`.
Trasladarlas a otros universos no está implementado.

## 3. Generalización temporal

- Todas las ventanas walk-forward predefinidas, incluidas las desfavorables. Cada una lleva sus barras y un error
  estándar aproximado del Sharpe (Lo, 2002).
- Años naturales. Solo cuentan para el criterio los que tienen al menos 60 barras.
- Longitudes de entrenamiento distintas, tomadas de la etapa D del laboratorio.
- Regímenes causales, cada uno calculado solo con datos anteriores a la barra:
  - volatilidad: la de 20 barras frente a su mediana expansiva;
  - tendencia frente a rango: ratio de eficiencia de 60 barras mayor que 0,3;
  - estrés: drawdown de 252 barras peor que −15 %.
- Dependencia de régimen: los regímenes con al menos 120 barras y media neta negativa.

La selección de parámetros solo usa las ventanas de entrenamiento. No se elige ningún periodo a posteriori.

## 4. Sensibilidad a parámetros

El centro es la configuración que el walk-forward eligió más veces. Los vecinos se generan perturbando un parámetro
numérico cada vez a ×0,75 y ×1,25; los booleanos y los textos no se tocan.

Cada configuración se mantiene fija en todas las ventanas de test y se informa el número de configuraciones
probadas. Es un diagnóstico: de aquí no se selecciona nada y no se usa el periodo final.

## 5. Escenarios de costes y ejecución

| Escenario | Costes | Retraso | Límite de participación |
|---|---|---|---|
| base | ×1 (declarados en el contrato o estimaciones por clase de activo) | 0 barras | ×1 |
| adverse | ×2 | +1 barra | ×0,5 |
| severe | ×3 | +2 barras | ×0,25 |

Los costes incluyen comisión, medio spread y slippage (fijo y una fracción del rango de la barra).

Una orden que supera el límite de participación sobre el volumen en dólares de la barra anterior se recorta, y cada
recorte cuenta como `partial_fill_events`. Esto modela tanto los fills parciales como la parte no ejecutada.

No se modelan y se declara así:

- órdenes limitadas (solo existen en paper trading);
- spreads históricos (el OHLCV no los tiene);
- rechazos del mercado.

## 6. Capacidad

| Estado | Cuándo |
|---|---|
| `OBSERVED` | Nunca en esta versión: haría falta spread y profundidad de libro, que el proyecto no tiene. |
| `APPROXIMATE` | Hay volumen en al menos el 90 % de las barras evaluadas. Se usa el volumen observado y un modelo de impacto supuesto. |
| `UNKNOWN` | Falta volumen o vale cero en más del 10 % de las barras. No se estima nada. |

Para cada nivel de capital (por defecto 1e5, 3e5, 1e6, 3e6, 1e7, 3e7 y 1e8) se calculan:

- el impacto con raíz cuadrada, Y·σ·√participación (Y = 1, supuesto);
- la media y el Sharpe netos tras el impacto;
- el percentil 95 de la participación;
- la fracción de operaciones por encima del límite;
- el nocional en el quintil de menor liquidez.

La estimación es el mayor capital probado que conserva al menos la mitad de la media neta base con como mucho un 5 %
de operaciones por encima del límite. Si no hay ventaja neta positiva en el escenario base, la estimación es 0.
**No es una medición del impacto real de mercado.**

## 7. Estrés y perturbaciones

Los escenarios sintéticos son **pruebas de estrés, no predicciones**. Son deterministas, se aplican sobre los datos de
investigación y mantienen los parámetros seleccionados:

- `volatility_x2`: retornos duplicados;
- `price_gaps`: cinco gaps de −8 %;
- `liquidity_drop`: volumen ×0,1;
- `stale_data`: una barra de cada 20 congelada y con volumen 0;
- `regime_break`: la segunda mitad con los retornos invertidos;
- `correlation_spike`: solo con varios activos;
- `cost_spike_x5`: costes ×5.

Los shocks históricos se informan aparte: los drawdowns más profundos y distintos (más de 5 %) de la referencia en el
periodo evaluado, con el resultado del candidato en cada uno. La pérdida de fuentes reutiliza los subconjuntos del
universo del laboratorio.

`risk_controls` comprueba con `paper.feed.check_bar` que un precio negativo, un precio ausente o una barra publicada
antes de su cierre devuelven `fail`. Con ese resultado, el motor de paper trading pausa la sesión antes de cualquier
orden.

## 8. Diversificación

La referencia es la cartera equiponderada de los demás candidatos completados y no rechazados con el mismo tipo de
dato (o los que se indiquen). Se compara con la combinación que añade el candidato, en los mismos días y con la misma
metodología. Se informan:

- la correlación total y en los días de estrés (el peor 10 % de la referencia);
- el rango de la correlación móvil de 126 días;
- el solapamiento de días activos;
- el resultado del candidato durante el máximo drawdown de la referencia.

Una correlación histórica baja no garantiza diversificación en estrés. No se modifica ninguna asignación del paper
trading.

## 9. Dimensiones, criterios y estados

Cada dimensión es `PASS`, `FAIL` o `INSUFFICIENT_DATA`. Las dimensiones nunca se promedian en una puntuación, y una
dimensión sin datos nunca se considera favorable. Los criterios exactos se devuelven en `GET /robustness/meta` y
están en `report.CRITERIA`.

Las dimensiones esenciales son `temporal`, `cross_asset`, `parameters`, `costs` y `uncertainty`. El estado global es
la primera regla que se cumple:

1. el candidato no se puede reconstruir (los datos cambiaron, el run se canceló o falló): `NOT_EVALUATED`;
2. `temporal`, `parameters` o `costs` es `FAIL`: `FRAGILE`. No se compensa con otras dimensiones;
3. alguna dimensión esencial es `INSUFFICIENT_DATA`: `INSUFFICIENT_DATA`;
4. cualquier otro `FAIL` (cross_asset, uncertainty, stress, capacity, diversification): `MIXED_EVIDENCE`;
5. en otro caso: `ROBUST_UNDER_TESTED_ASSUMPTIONS`, listando las dimensiones no esenciales sin datos.

`uncertainty` usa la muestra, el IC del Sharpe y el DSR del laboratorio. Una muestra insuficiente da
`INSUFFICIENT_DATA`, de modo que nada sin evidencia estadística suficiente puede llegar a
`ROBUST_UNDER_TESTED_ASSUMPTIONS`.

## 10. Persistencia y reproducibilidad

Un run es un `Experiment` de kind `robustness_run`. Su identidad es el hash de:

- la configuración;
- la del experimento de laboratorio;
- el contenido de cada dataset destino;
- la versión del motor (`robustness@1.0.0`).

Repetir la misma petición devuelve el run guardado. Cualquier cambio crea uno nuevo junto al anterior, y no se
sobrescribe nada. Cada run guarda la versión de código y las referencias de datos. El trabajo se puede cancelar entre
etapas y tiene un límite de tiempo (`max_seconds`).

## API

| Método | Ruta | Descripción |
|---|---|---|
| GET | `/robustness/meta` | Estados, dimensiones, criterios, reglas, escenarios y valores por defecto. |
| GET | `/robustness/candidates` | Auditoría de candidatos: decisión del laboratorio, p ajustado, exploratorio y último estado de robustez. |
| POST | `/robustness/runs` | `RobustnessConfig` + `run_async`: encola un trabajo `robustness_run` o lo ejecuta ahora. Experimento no `COMPLETED` → 400. |
| GET | `/robustness/runs?experiment_id=&state=` · `/robustness/runs/{id}` | Runs y resultado completo. |
| GET | `/robustness/runs/{id}/export.json` · `/robustness/runs/{id}/matrix.csv` | Exportaciones. |

La cancelación usa `POST /jobs/{job_id}/cancel`.

## Pantalla (`/robustness`)

Filtros por estrategia, activo, mercado, timeframe y periodo. El periodo filtra los candidatos, las filas de la
matriz, las ventanas y los años.

La pantalla muestra:

- los candidatos y su evidencia, con los botones Evaluate, Cancel y View;
- la **matriz de robustez por estrategia** (sección 9);
- para cada run:
  1. comparación entre activos y mercados;
  2. ventanas, años y regímenes;
  3. sensibilidad a parámetros;
  4. costes base, adverso y severo;
  5. curvas netas por escenario;
  6. estrés;
  7. capacidad y supuestos;
  8. contribución a cartera;
  9. (la matriz de arriba);
  10. limitaciones y datos ausentes.

Cada conclusión muestra la métrica, el supuesto y el tamaño de muestra en que se basa.

## Ejemplo reproducible (datos sintéticos)

```powershell
cd C:\Apps\Quant\backend
$env:QAL_DATABASE_URL = "sqlite:///rob_demo/qal.db"; $env:QAL_DATA_DIR = "rob_demo/data"
..\.venv\Scripts\python -m qal.cli demo
..\.venv\Scripts\python -m qal.cli lab-run --generate --limit 20
..\.venv\Scripts\python -m qal.cli robustness-run --limit 15 --out robustness_runs
```

Resultado en una base de datos nueva: 15 runs (unos 15 s), 6 `INSUFFICIENT_DATA` y 9 `FRAGILE`. Ninguno llega a
`ROBUST_UNDER_TESTED_ASSUMPTIONS`. Las causas:

- la muestra del laboratorio es insuficiente, así que `uncertainty` queda en `INSUFFICIENT_DATA` en todos;
- pares y cross-sectional no tienen evaluación entre activos;
- `volatility_x2` supera el drawdown máximo en SYN-D momentum;
- las variantes rechazadas fallan en temporal, parámetros o costes.

Son datos sintéticos: prueban el procedimiento, no los mercados.

Con datos reales, la evaluación se hace con `--real-only` sobre una base de datos con los datos ingeridos.

## Limitaciones y siguientes pasos

- Sin spreads ni profundidad: la capacidad es como mucho `APPROXIMATE`, y los costes de los destinos son estimaciones.
- No hay evaluación entre activos para pares ni carteras cross-sectional.
- No se modelan órdenes limitadas ni rechazos del mercado en el backtest.
- Los regímenes y los criterios son reglas fijas y versionadas: cambiarlas es una nueva versión del motor.
- Ampliar el universo, por ejemplo a FX o cripto, solo tiene sentido cuando haya candidatos con evidencia estadística
  suficiente. Con los datos actuales ninguno la tiene.

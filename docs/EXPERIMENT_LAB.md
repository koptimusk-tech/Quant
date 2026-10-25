# Experiment Lab (Fase 10)

Motor de experimentación cuantitativa: genera, ejecuta, compara y prioriza hipótesis de estrategia con un único
pipeline de validación reproducible. Solo investigación y simulación: el laboratorio no puede enviar órdenes, no
cambia el estado de ninguna hipótesis, no toca sesiones de paper trading ni políticas de riesgo, y ninguna decisión
promociona una estrategia.

Código: `backend/qal/lab/` (`spec.py`, `pipeline.py`, `selection.py`, `service.py`, `generator.py`), API
`backend/qal/api/routers/lab.py`, trabajo `lab_experiment` en `qal/jobs/tasks.py`, CLI `qal.cli lab-run`,
pantalla `frontend/src/pages/ExperimentLab.tsx` (ruta `/lab`). Pruebas: `backend/tests/test_lab.py`,
`frontend/src/__tests__/experimentlab.test.tsx`, `frontend/e2e/lab.spec.ts`.

## 1. Qué se reutiliza

| Necesidad | Componente existente |
|---|---|
| Contrato de hipótesis (mecanismo, reglas, costes, diseño OOS, criterios fijados de antemano) | `qal.research.contract.HypothesisContract` |
| Backtest point-in-time, ejecución en la apertura siguiente, costes completos | `qal.research.backtest`, `qal.research.costs` |
| Estrategias | `qal.research.strategies.STRATEGIES` (+ `mean_reversion`, nueva) |
| Walk-forward con embargo, test final aislado, placebo, bootstrap por bloques, DSR, PBO, comprobaciones de leakage | `qal.research.validation.Validator` |
| Gates A–D deterministas | `qal.research.policy.evaluate_gates` |
| Propuestas de hipótesis a partir de datos | `qal.research.proposals.build_proposals` |
| Correlaciones por régimen y en estrés, covarianza, contribución marginal, exposición a factores | `qal.portfolio.risk` |
| Benjamini-Hochberg | `qal.anomaly.engine.benjamini_hochberg` |
| Cola persistente, reintentos, idempotencia, cancelación | `qal.jobs.runner` |
| Registro de experimentos, auditoría, hashes y versión de código | `Experiment`, `qal.audit`, `qal.versioning` |

**Migraciones: ninguna.** Los experimentos se guardan en la tabla `experiments` (tipos `lab_experiment` y
`lab_selection`) y los trabajos en `jobs`.

## 2. Arquitectura y contrato

```
spec (JSON, lab-exp@1) ──► service.create ──► Experiment QUEUED ──► job lab_experiment ──► service.run
                                  │                                                       │
               hash(spec + hashes de datos + versión del pipeline)            Pipeline A → C → B → D → E
                                                                                          │
                       ranking (BH en todo el laboratorio + decide) ◄── COMPLETED / INVALID / FAILED / CANCELLED
```

**Contrato versionado** (`ExperimentSpec`, `lab-exp@1`) = `HypothesisContract` congelado + lo que añade el
laboratorio:

| Campo del brief | Dónde |
|---|---|
| id | `Experiment.id`; identidad = `config_hash` |
| hipótesis y fundamento económico | `contract.premise`, `contract.mechanism` |
| familia | `family`: momentum, mean_reversion, factor, anomaly, pairs, regime |
| universo, timeframe, periodo | `contract.dataset_ids` (misma frecuencia; sin mezclar sintético y real), `validation.holdout_start` |
| fuentes y versiones de datos | `data_refs`: dataset, `content_hash`, símbolo, frecuencia, calidad, filas, `is_synthetic` |
| features y etiquetas | `contract.features`, `entry_rule`, `exit_rule`, `horizon_bars` |
| parámetros | `contract.strategy.params` + `param_grid` (cada variante cuenta como ensayo) |
| baseline | `baselines` (por defecto según familia), `ablation` |
| costes y ejecución | `contract.costs` por dataset (si falta: coste por defecto de la clase de activo, con aviso) |
| método de validación | `contract.validation` (train/test/embargo/holdout/placebo/bootstrap/seed) + `robustness` |
| métricas objetivo y restricciones | `contract.success` |
| semilla | `contract.validation.seed` → `Experiment.seed` |
| estado, resultados y errores | `status`, `results.pipeline`, `error`, `results.attempts`, `results.stage` |
| versiones de código y modelo | `code_version`, `algorithm_version` (`lab-pipeline@1.0.0+lab-exp@1`), `selection.version` |
| límites de recursos | `limits`: `max_seconds`, `max_variants`, `max_bars` |

**Planificador determinista.** Estados `QUEUED → RUNNING → COMPLETED | FAILED | CANCELLED | INVALID`.

- *Idempotencia*: la misma spec sobre los mismos datos devuelve el experimento guardado. Un trabajo por intento
  (`lab:{id}:{intento}`), así que reintentar el envío no duplica la ejecución.
- *Reintentos*: solo tras `FAILED` o `CANCELLED`; se crea una fila nueva con `previous_attempt`. Un error dentro del
  pipeline termina en `FAILED` registrado y no se reintenta solo: el pipeline es determinista, fallaría igual.
- *Cancelación*: `QUEUED` pasa a `CANCELLED` al momento; en `RUNNING` el trabajo se detiene entre unidades de trabajo.
  El presupuesto de tiempo agotado también termina en `CANCELLED`.
- *Interrupción y recuperación*: al arrancar el worker, los `RUNNING` sin proceso vuelven a `QUEUED` y reciben un
  trabajo nuevo; el contador de intentos sigue.
- *INVALID*: falla una comprobación crítica de integridad (calidad, marcas de tiempo, separación temporal, leakage).
  Sus resultados no entran en la selección.
- *Trazabilidad*: cada transición queda en `audit_events`. Cambiar un parámetro, un coste o una versión crea un
  experimento nuevo; el anterior no se toca. `lineage` muestra la historia de la misma familia, estrategia y
  universo con los campos que cambiaron.
- *Datos inmutables*: antes de ejecutar se comprueba que el `content_hash` de cada dataset no ha cambiado.

## 3. Métodos de validación (pipeline común)

**A. Integridad** (críticas): calidad de datos ≠ `fail`, misma frecuencia, marcas de tiempo ordenadas y únicas,
`available_ts ≥ event_ts`, precios positivos, embargo ≥ horizonte, barras suficientes para al menos una ventana,
validador construible y, tras el walk-forward, las comprobaciones de leakage del `Validator`. No críticas, con aviso:
supervivencia (fin de series a más de 10 días, y siempre para acciones/ETF elegidos con la lista de hoy), costes
explícitos y tamaño del periodo final.

**B. Baselines** sobre las mismas barras fuera de muestra (ventanas walk-forward cosidas):

| Baseline | Qué es | Familias |
|---|---|---|
| `no_trade` | rendimiento cero, coste cero | todas |
| `buy_and_hold` | largo en el instrumento, mismos costes | momentum, mean_reversion, anomaly, regime (no en pares ni factores neutrales) |
| `permuted_signal` | las propias posiciones OOS desplazadas circularmente (200 desplazamientos): misma exposición, timing aleatorio | todas |
| `simple_reference` | la misma estrategia con sus parámetros por defecto, sin optimizar | todas |
| `ablation` | los parámetros elegidos con la feature desactivada (p. ej. sin filtro de régimen) | regime, o cuando se declara |

Diferencia de medias con t HAC (Newey-West, lag = horizonte) e IC bootstrap por bloques al 95 %; "superado" exige
que el límite inferior del IC sea positivo (para la señal permutada, p del placebo ≤ 0,1).

**C. Temporal**: walk-forward con selección de parámetros solo en entrenamiento, purga y embargo entre
entrenamiento y test, y un periodo final reservado que solo se evalúa si la spec lo pide (`evaluate_holdout`). Cada
acceso al periodo final con los mismos datos y corte se cuenta; desde el segundo deja de ser independiente y se avisa.

**D. Robustez**: rejilla de parámetros (fracción positiva en muestra), longitud de entrenamiento (×0,67, ×1,5),
ventanas de evaluación y años, costes y slippage (×0; 0,5; 1; 2; 3), retrasos de ejecución (+1 y +2 barras),
regímenes de volatilidad y subconjuntos de universo (quitar un instrumento, con 4 o más). `breaks_under` lista
cada variación que vuelve negativa la media OOS (años con menos de 60 barras o sin posición no cuentan).

**E. Estadística**: retornos brutos y netos, Sharpe y drawdown, IC bootstrap del Sharpe y de la media, t HAC y p
unilateral, operaciones independientes, extremos (Sharpe sin las 5 mejores barras y sin el 1 % mejor),
concentración de las 5 mejores operaciones, DSR con todos los ensayos del laboratorio sobre el mismo universo, PBO.
Un p-value no es la probabilidad de que la estrategia funcione ni de que sea rentable.

## 4. Criterios de selección y sus limitaciones

**Decisiones** (`lab-selection@1.0.0`), en este orden; la primera que aplica gana:

1. `INVALID` → `REJECTED`.
2. Muestra insuficiente (< 120 barras OOS o menos operaciones que `max(20, success.min_trades)`) → `INCONCLUSIVE`.
3. Media neta OOS ≤ 0 con muestra adecuada → `REJECTED`.
4. Falta evidencia (media positiva con IC > 0, todos los baselines superados, p ajustado BH < 0,05, DSR ≥ mínimo) →
   `INCONCLUSIVE`.
5. Falta robustez (sobrevive a ×2 costes y a una barra de retraso, vecindad de parámetros, no concentrado, PBO ≤ 0,5)
   → `NEEDS_RESEARCH`.
6. Falta algo para paper (periodo final evaluado una sola vez y cumplido, costes explícitos, calidad `pass`, gates
   A–D) → `OOS_SUPPORTED`.
7. Todo cumplido → `PAPER_ELIGIBLE`: es una etiqueta para una revisión humana; no cambia nada en paper trading.

**Puntuación** transparente y versionada, con dimensiones separadas y visibles: metodología 0,15, evidencia OOS 0,20,
rendimiento neto 0,15, robustez de parámetros 0,15, estabilidad 0,15, incertidumbre 0,10, capacidad 0,05,
complejidad 0,05. La clasificación ordena primero por decisión y después por puntuación: un control crítico que
falla no se compensa con una puntuación alta. Cada fila enlaza a todas las métricas que la sustentan.

**Pruebas múltiples**: cada variante de cada experimento previo sobre el mismo universo cuenta como ensayo para el
DSR, y la clasificación aplica Benjamini-Hochberg a los p de todos los experimentos completados cada vez que se
consulta. La decisión guardada al completar usa el p propio (lo único conocido entonces); la clasificación usa el
ajustado.

**Limitaciones**: los pesos son una elección documentada, no un óptimo; la puntuación ordena evidencia, no predice
rentabilidad. BH controla la tasa de falsos descubrimientos entre experimentos, no la probabilidad de que uno sea
real. El recuento de ensayos solo cubre lo que pasa por el laboratorio (no lo que alguien probó fuera). Los costes
por defecto son estimaciones. El periodo final pierde independencia con cada consulta. Sobre datos sintéticos nada de
esto es evidencia de mercado.

## Contribución a cartera

`POST /lab/portfolio` con 2 a 12 experimentos completados: rendimientos OOS diarios, correlación estática, rodante,
por régimen y en días de estrés, exposición a factores comunes, contribución marginal al riesgo, diversificación de
pérdidas (qué pierde la combinación en los peores días de cada candidato), costes y rotación, drawdown y
concentración; la combinación a pesos iguales frente a cada candidato y a liquidez. No cambia ninguna asignación.

## Generador de hipótesis

Reglas deterministas, sin LLM. Reutiliza `build_proposals` (momentum, eventos tras shocks, pares correlacionados) y
añade reversión a la media de un instrumento, momentum condicionado al régimen de volatilidad (con baseline de
ablación) y momentum transversal (3 o más instrumentos diarios de la misma clase). Cada propuesta declara mecanismo,
señal, entrada, salida, horizonte, universo, costes, riesgos, baselines e invalidación. Una propuesta por (familia,
estrategia, universo); si ya existe un experimento con esa clave no se vuelve a proponer. El generador informa de
cuántas specs y variantes propone y cuántas omite. Una propuesta es investigación, no evidencia.

## API

| Método | Ruta | Descripción |
|---|---|---|
| GET | `/lab/meta` | Familias, baselines, versiones, decisiones, pesos, mínimos de muestra. |
| POST | `/lab/generate` | `{families, limit}` → propuestas (no ejecuta ni guarda nada). |
| POST | `/lab/experiments` | `{spec, run_async, generator_key}`: crear o importar; spec inválida → 400. |
| GET | `/lab/experiments?status=` | Cola e historial con estado, etapa, intentos, decisión al completar. |
| GET | `/lab/experiments/{id}` | Registro completo, trabajo activo y `lineage`. |
| POST | `/lab/experiments/{id}/cancel` | Cancelar; terminado → 409. |
| GET | `/lab/experiments/{id}/export.json` | Exportación completa. |
| GET | `/lab/ranking?include_synthetic=` · `/lab/ranking/export.csv` | Clasificación con BH del laboratorio. |
| POST · GET | `/lab/selections` | Congelar la clasificación actual como registro auditado (no aprueba nada). |
| POST | `/lab/portfolio` | Contribución a cartera. |

## Pantalla Experiment Lab (`/lab`)

Generar propuestas por familia y elegir cuáles encolar; importar o editar una spec JSON; cola con estado, etapa en
curso, intentos, errores y cancelación; detalle por pestañas (decisión con motivos y condiciones, A integridad,
B baselines, C temporal con IS frente a OOS por ventana y curva OOS, D robustez, E estadística, versiones e
historial); clasificación con p bruto y ajustado, avisos de muestra pequeña, test reutilizado y costes ausentes;
contribución a cartera; registrar selección; exportar JSON y CSV.

## Ejemplo reproducible completo

Windows (PowerShell), desde la raíz del repositorio con el entorno de la guía de instalación del README:

```powershell
cd backend
$env:QAL_DATABASE_URL = "sqlite:///lab_demo/qal.db"; $env:QAL_DATA_DIR = "lab_demo/data"
..\.venv\Scripts\python -m qal.cli demo
..\.venv\Scripts\python -m qal.cli lab-run --generate --limit 20 --out lab_runs
```

Linux/macOS: igual con `export` y `../.venv/bin/python`. Para ejecutar una spec propia:
`python -m qal.cli lab-run --spec mi_spec.json` (repetible). Con la app abierta, la misma operación está en
**Experiment Lab → Generate proposals → Queue selected**.

Sobre los datos sintéticos de la demo el resultado es idéntico en cada ejecución (comprobado dos veces sobre bases de
datos nuevas): 15 experimentos completados, 12 `INCONCLUSIVE` y 3 `REJECTED`, ninguno `OOS_SUPPORTED`. El primero,
momentum sobre SYN-D, tiene p ajustado 0,092 y puntuación 0,67, y es `INCONCLUSIVE` por muestra insuficiente. Son
datos sintéticos: el ejemplo demuestra el proceso, no evidencia de mercado.

## Limitaciones y siguientes pasos

- Sin acceso a datos reales desde el entorno de desarrollo en la nube (Yahoo y Stooq bloqueados): el pipeline está
  probado solo con datos sintéticos y CSV. Hay que repetir el ejemplo con datos reales en local.
- La cancelación de una ejecución síncrona (CLI o `run_async=false`) no es posible desde la API; las ejecuciones
  síncronas están limitadas por `max_seconds`.
- El progreso se informa por etapa (A, C, B, D, E, final), no por porcentaje.
- Pendiente: generación avanzada (combinaciones de señales validadas, LLM como proponente con las mismas
  restricciones), CPCV como alternativa al walk-forward, recuento de ensayos de fuera del laboratorio (Research Lab y
  Alpha Research) en un único registro, y capacidad con datos de volumen reales.

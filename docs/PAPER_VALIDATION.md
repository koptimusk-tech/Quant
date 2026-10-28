# Programa de validación en paper trading (Fase 12)

Objetivo: recopilar evidencia adicional y comprobar el funcionamiento operativo **simulado** de candidatas ya
investigadas, con criterios fijados antes de mirar el periodo de evaluación. No es una demostración de rentabilidad:
nada en este programa envía órdenes a brókers, exchanges o cuentas, y ninguna clasificación autoriza operar con dinero
real.

## 1. Auditoría inicial (lo que había y lo que faltaba)

| Pieza | Estado antes de la Fase 12 | Qué se hizo |
|---|---|---|
| Motor de paper trading (Fase 7: `qal/paper`) | Barra a barra, una transacción por barra, órdenes con `client_order_id` único, ejecución en la barra siguiente, costes, gate de riesgo pre-trade, conciliación, kill switch, modos `replay` y `new_data` | Reutilizado sin reimplementar. Se añade el tipo de estrategia `P:<candidata>` |
| Integridad temporal | Señales con datos disponibles en el cierre (`available_ts`), nunca se ejecuta en la barra de la decisión | Pruebas nuevas: orden temporal y lookahead (reescribir el futuro no cambia decisiones pasadas) |
| Riesgo | Gate pre-trade con la política HRM, kill switch | Se añaden causas de pausa del programa: drawdown (pausa y stop), pérdida por barra, exposición, costes anómalos, rechazos repetidos del gate, errores repetidos del motor, persistencia, inconsistencias, datos obsoletos o corruptos |
| Simulación de ejecución | Llenado en la barra siguiente, límites, stops, participación, parciales | Sin cambios |
| Registro formal de candidatas | **Faltaba** (solo hipótesis `H:` o demos) | Registro inmutable y versionado |
| Protocolo previo | **Faltaba**: los `EvaluationCriteria` de la Fase 7 se fijan al crear la sesión, pero sin horizonte, métrica primaria ni reglas de parada | Protocolo congelado y con hash antes del inicio, horizonte derivado de la candidata |
| Historial | `reset` y `delete` de una sesión borraban sus filas (el resumen quedaba en el experimento) | Las sesiones de un programa no se pueden reiniciar, borrar ni reconfigurar, ni reanudar fuera del procedimiento auditado |
| Clasificación | Veredicto `consistent_with_backtest` / `deviates` / `insufficient_sample` | Estados CONTINUE_PAPER, PAUSED_REVIEW, REJECTED, INCONCLUSIVE, COMPLETED con revisión humana documentada |

Se corrigieron estos huecos antes de iniciar ningún programa, como pide el enunciado.

## 2. Arquitectura

```
Experiment Lab (Fase 10) ─┐
Robustness Lab (Fase 11) ─┼─> Registro de candidatas ──> Protocolo (congelado) ──> Programa
                          │    paper_candidate             paper_protocol            paper_program
                          │                                                            │
                          │                                     sesión de paper (Fase 7) con estrategia P:<candidata>
                          │                                                            │
                          └──────── backtest de la misma versión en las mismas barras ─┤
                                                                                       v
                                 seguimiento estadístico · comparación · monitor de pausas · reglas · revisión · informe
```

Módulos (`backend/qal/program/`):

| Módulo | Responsabilidad |
|---|---|
| `registry.py` | Admisión, comprobaciones, instantánea inmutable con hash, versiones, verificación de integridad |
| `protocol.py` | Protocolo, horizonte adaptado, modos A/B/C, causas de pausa, criterios de aceptación y rechazo |
| `service.py` | Inicio, ejecución barra a barra con monitor, pausas, reanudación auditada, parada anticipada, kill switch, revisión |
| `tracking.py` | Métricas acumuladas y móviles con incertidumbre, operaciones, baseline |
| `compare.py` | Comparación con el backtest de la misma versión en las mismas barras |
| `classify.py` | Reglas de estado y revisiones permitidas |
| `report.py` | Informe final exportable con la configuración y las limitaciones |

Persistencia: todo son filas `Experiment` (`paper_candidate`, `paper_protocol`, `paper_program`) más las tablas de
paper de la Fase 7. No hay tablas nuevas ni migraciones. Cambios en la Fase 7: `signals.resolve` acepta
`P:<id>`; el gate acepta los estados `PAPER_CANDIDATE` y `PAPER_CANDIDATE_EXPLORATORY` (una candidata alterada da
`UNKNOWN` y se rechaza en cada orden); `SessionConfig.program_id`; `reset`, `delete_session`, `change_config` y
`start` se niegan para sesiones de un programa; `evaluation.backtest_on_bars` se extrae para reutilizarlo.

## 3. Esquema del registro y del protocolo

**Candidata** (`paper_candidate`, `status=admitted|refused`, `config_hash = sha256(config)`):

| Campo | Contenido |
|---|---|
| `name`, `version` | Línea de candidata y versión (1, 2, ...). Otra instantánea con el mismo nombre es la versión siguiente |
| `hypothesis` | Título, premisa, mecanismo, reglas de entrada y salida, invalidación, riesgos, horizonte |
| `family`, `features`, `strategy` | Tipo, parámetros **seleccionados en la última ventana walk-forward** (no se reajustan en paper), rejilla |
| `assets`, `timeframe`, `is_synthetic`, `allowed_modes` | Datasets con hash de contenido, proveedor, periodo; sintético solo permite el modo C |
| `code_version` | Versión del código del experimento, del pipeline y del registro |
| `lab`, `results` | Decisión del laboratorio, p ajustado, métricas OOS, CI, DSR, PBO, ensayos, periodo reservado |
| `robustness` | Run, versión, estado y dimensiones de la Fase 11 |
| `baselines`, `costs` | Resultados frente a baselines; modelos de coste con su fuente (siempre "estimación, no observado") |
| `risk_limits`, `allocation` | Drawdown de pausa y de stop, exposición bruta máxima, pérdida máxima por barra |
| `admission` | Tipo (validated / exploratory), quién, cuándo, justificación y todas las comprobaciones |

**Protocolo** (`paper_protocol`, `status=frozen`): candidata y su hash, modo y etiqueta, `frozen_at`, horizonte
(con su derivación), periodo de replay, `min_trades`, métrica primaria y secundarias, baseline, ventana móvil,
tolerancias (seguimiento, correlación, acuerdo de señales, tasa de llenado, costes, drawdown), umbral de coste
anómalo, fracción máxima de barras ausentes, errores consecutivos, barras seguidas con rechazos, antigüedad máxima
del feed, causas de pausa, condiciones de aceptación y rechazo, método de comparación y tratamiento de datos
ausentes. Un protocolo gobierna un único programa; un cambio es un protocolo nuevo.

**Programa** (`paper_program`): candidata, protocolo, sesión, modo; `results` guarda la línea temporal de cambios
de estado (con el número de evento de la sesión), pausas (causa, evidencia, hora real y de simulación, quién y
cómo se resolvió), errores, registro de feed por barra (cierre, disponibilidad, procesado, retraso, prospectiva),
barras ausentes, revisiones y el estado de reglas.

## 4. Estrategias admitidas y justificación

Reglas de admisión (`registry.eligibility`):

- Siempre: experimento COMPLETED, datasets sin cambios desde que se ejecutó (hash), ninguno con calidad `fail`, una
  persona con nombre (los agentes y procesos automáticos se rechazan) y justificación escrita.
- Requisitos de evidencia: decisión del laboratorio OOS_SUPPORTED o PAPER_ELIGIBLE y un run de robustez del
  experimento en estado ROBUST_UNDER_TESTED_ASSUMPTIONS.
- Si faltan los requisitos de evidencia, la admisión se rechaza (y el rechazo queda registrado). Solo se admite si
  una persona registra una admisión **exploratoria** explícita; la candidata queda marcada como exploratoria en el
  registro, el protocolo, el programa, el informe y la interfaz, y su resultado nunca se presenta como evidencia
  de validación. Un buen backtest no basta para admitir nada.

Con los datos reales de la Fase 11 ninguna candidata cumple los requisitos de evidencia (decisiones INCONCLUSIVE o
inferiores y estados FRAGILE, MIXED_EVIDENCE o INSUFFICIENT_DATA). Por tanto **no se ha admitido ninguna estrategia
como validada**, y no se ha admitido ninguna como exploratoria porque eso requiere una decisión humana que este
programa no toma por sí mismo. `qal.cli program-audit --real-only` lista las comprobaciones de cada candidata.

## 5. Métricas, horizonte y criterios

**Horizonte adaptado** (no hay un plazo universal):

```
bars_for_trades    = ceil(min_trades / tasa_de_operaciones_OOS)
bars_for_precision = ceil(ppy · (1 + SR_barra² / 2) / target_sharpe_se²)       (error estándar de Lo, 2002)
horizon            = max(min_observations_floor, bars_for_trades, bars_for_precision)
```

Si al llegar al horizonte faltan operaciones, se continúa hasta alcanzarlas o hasta `max_duration_bars` (regla
fijada en el protocolo). La decisión nunca se adelanta: las comprobaciones intermedias solo pausan o paran por
seguridad e integridad. Si el horizonte supera el máximo, o el periodo de replay no tiene barras suficientes, el
protocolo se rechaza al crearlo (el resultado solo podría ser inconcluso).

Ejemplo: una candidata diaria con 30 operaciones en 900 barras OOS y `min_trades=20` necesita 600 barras por las
operaciones; con `target_sharpe_se=1.0` necesita unas 252 por precisión: horizonte 600 barras (unos 2,4 años).

**Métrica primaria**: rentabilidad neta media por barra en exceso sobre la baseline (`no_trade` o
`buy_and_hold`), con IC 95 % por bootstrap de bloques móviles (bloques de 5 barras, semilla fija).

**Secundarias**: Sharpe anualizado con su error estándar, drawdown máximo, volatilidad, tasa de acierto por barra,
operaciones, llenados, costes papel/backtest, diferencia y correlación con el backtest, tasa de llenado,
implementation shortfall y exposición bruta. Ninguna decide por sí sola: no se declara éxito por Sharpe, tasa de
acierto o rentabilidad acumulada.

**Aceptación (COMPLETED, todas)**: muestra completa; IC del exceso sobre la baseline por encima de cero;
consistencia con el backtest (IC de la diferencia media dentro de ±tolerancia, correlación y acuerdo de señales
mínimos); costes papel/backtest ≤ máximo; tasa de llenado ≥ mínimo; drawdown papel/backtest ≤ máximo y nunca más
allá del stop; barras ausentes ≤ máximo; ninguna pausa abierta y conciliación siempre correcta.

**Rechazo (REJECTED, cualquiera)**: drawdown más allá del stop (en cualquier momento); con la muestra completa, IC
del exceso por debajo de cero, IC de la diferencia con el backtest fuera de la tolerancia (la simulación no
reproduce la candidata) o costes por encima del ratio de rechazo.

**Estados**: CONTINUE_PAPER (horizonte no alcanzado), PAUSED_REVIEW (pausa abierta), REJECTED, INCONCLUSIVE
(horizonte, duración máxima, fin de datos de replay o parada anticipada sin cumplir aceptación ni rechazo) y
COMPLETED. Con muestra insuficiente no se fuerza nada: los criterios estadísticos no se aplican y el estado es
CONTINUE_PAPER o INCONCLUSIVE. Una parada anticipada conserva todo el periodo y nunca puede acabar en COMPLETED.

**Revisión**: una persona registra la clasificación con una justificación. Puede confirmar el estado de las
reglas o elegir uno más conservador (`COMPLETED → INCONCLUSIVE/REJECTED`, `INCONCLUSIVE → REJECTED`, ...), nunca
uno mejor. Los agentes no pueden revisar, admitir, crear protocolos, iniciar, reanudar ni parar.

**Modos**: A replay histórico (no prospectivo; si el periodo fue usado por la investigación se indica que solo
comprueba la operación), B paper prospectivo con datos nuevos y reales (una barra es prospectiva solo si cerró
después de congelar el protocolo; se registra el retraso de cada barra), C simulación sintética (solo prueba el
procedimiento). Cada resultado lleva su etiqueta y la interfaz usa un color y un texto distintos para cada modo.

**Causas de pausa**: datos obsoletos o corruptos, incumplimiento de límites de riesgo (pérdida por barra o
rechazos repetidos del gate), drawdown, drawdown de stop (no se puede reanudar), exposición, costes anómalos,
errores repetidos del motor, inconsistencia entre órdenes, posiciones y P&L, fallo de persistencia, kill switch y
pausa manual. Cada pausa guarda causa, evidencia y hora. Reanudar exige persona, motivo, cómo se resolvió,
confirmación explícita y que la comprobación propia de la causa vuelva a pasar (conciliación, persistencia).

## 6. Resultados de las pruebas

Pruebas obligatorias en `backend/tests/test_program.py` (20, todas en verde):

| Requisito | Prueba |
|---|---|
| Protocolo definido antes del inicio | `test_protocol_must_be_fixed_before_the_start_and_governs_one_program` |
| Separación replay / prospectivo | `test_modes_are_separated_and_labelled` |
| Orden temporal, P&L y costes | `test_temporal_order_no_lookahead_and_costs_counted_once` |
| Lookahead | `test_lookahead_future_bars_do_not_change_past_decisions` |
| Conciliación | `test_reconciliation_failure_pauses_and_resume_requires_it_to_pass` |
| Recuperación tras reinicio e idempotencia | `test_restart_after_a_crash_resumes_identically_without_duplicates` |
| Circuit breakers | `test_drawdown_exposure_and_cost_breakers_pause_with_evidence`, `test_drawdown_stop_rejects_and_cannot_resume_and_kill_switch_blocks`, `test_repeated_engine_errors_pause_the_program` |
| Criterios de aceptación y rechazo, muestra insuficiente | `test_accept_reject_and_insufficient_sample_rules`, `test_early_stop_keeps_the_period_and_is_never_completed` |
| Versionado | `test_candidates_are_immutable_and_versioned`, `test_versions_are_tracked_separately_and_comparison_checks_conventions` |
| Comparación con el backtest | `test_versions_are_tracked_separately_and_comparison_checks_conventions`, `test_program_runs_to_its_horizon_and_needs_a_conservative_review` |
| Sin órdenes reales ni credenciales | `test_no_real_orders_no_credentials` |
| Historial no borrable | `test_program_session_history_cannot_be_reset_deleted_or_reconfigured` |
| Registro y admisión | `test_admission_needs_evidence_or_an_explicit_exploratory_decision_by_a_person` |
| Horizonte adaptado | `test_horizon_depends_on_the_candidate_and_infeasible_protocols_are_refused` |
| API | `test_api_flow` |

Interfaz: `frontend/src/__tests__/papervalidation.test.tsx` (vitest) y `frontend/e2e/validation.spec.ts`
(navegador, flujo completo sobre datos sintéticos). Ninguna prueba previa se eliminó ni se debilitó.

## 7. Ficheros

- Nuevos: `backend/qal/program/{__init__,registry,protocol,service,tracking,compare,classify,report}.py`,
  `backend/qal/api/routers/program.py`, `backend/tests/test_program.py`, `frontend/src/pages/PaperValidation.tsx`,
  `frontend/src/__tests__/papervalidation.test.tsx`, `frontend/e2e/validation.spec.ts`, este documento.
- Modificados: `backend/qal/paper/{signals,engine,risk_gate,config,service,evaluation}.py`, `backend/qal/api/main.py`,
  `backend/qal/jobs/tasks.py` (trabajo `paper_program_run`), `backend/qal/cli.py` (`program-audit`, `program-run`,
  `program-report`), `frontend/src/{api.ts,App.tsx}`, README, `docs/API.md`, ROADMAP.

## 8. Instrucciones en Windows

Desde `C:\Apps\Quant\backend` con la base de datos real (`lab_real`):

```powershell
git pull
..\.venv\Scripts\python -m pytest -q tests\test_program.py
..\.venv\Scripts\python -m qal.cli program-audit --real-only --out program_runs
```

`program-audit` no admite nada: muestra, para cada candidata del laboratorio, qué comprobaciones pasa y cuáles no.
Para iniciar un programa: abre **Paper Validation** en la app, admite una candidata (1), congela un protocolo con
**Preview horizon** y **Freeze protocol** (2), pulsa **Start program** y **Process bars** (3). En modo B el
programa avanza con cada ingesta de datos nuevos (también con un trabajo programado `paper_program_run`). Informe:

```powershell
..\.venv\Scripts\python -m qal.cli program-run --program <id> --max-bars 500
..\.venv\Scripts\python -m qal.cli program-report --program <id> --out program_runs
```

## 9. Limitaciones

- Ninguna candidata real cumple hoy los requisitos de evidencia: no hay resultados de programas sobre datos reales,
  y no se inventan.
- El modo B con datos diarios necesita meses o años para llegar a horizontes con precisión útil; un replay (A) no
  sustituye a esa evidencia.
- Ejecución simulada: llenado en la barra siguiente con modelos de coste configurados (estimaciones), participación
  limitada; no se observan spreads reales, cola ni impacto.
- La comparación por activo del backtest es aproximada (posición ejecutada de la barra anterior por la
  rentabilidad cierre a cierre) y se etiqueta así; las diferencias no explicadas por costes se listan sin causa.
- Las operaciones del paper se segmentan con las decisiones ejecutadas una barra después; con llenados parciales
  o rechazos la exposición real puede diferir (visible en exposición y llenados).
- El error estándar del Sharpe asume rendimientos i.i.d.; el bootstrap por bloques usa un bloque fijo de 5 barras.
- Un programa ejecuta una candidata; la combinación de varias en una cartera de paper queda fuera de esta fase.

## 10. Plan de seguimiento

1. Revisar `program-audit --real-only` tras cada nuevo run del laboratorio y de robustez (motor 1.0.1).
2. Si alguna candidata llega a OOS_SUPPORTED y ROBUST_UNDER_TESTED_ASSUMPTIONS, admitirla con justificación y
   congelar un protocolo B; programar `paper_program_run` tras cada ingesta diaria.
3. Revisar las pausas en cuanto aparezcan y registrar cada reanudación; no cambiar parámetros durante la
   evaluación (cualquier cambio es una versión nueva con su propio protocolo).
4. Al llegar al horizonte, registrar la revisión con su justificación y exportar el informe.
5. Pendiente técnico: carteras de varias candidatas en paper, spreads observados y bloque del bootstrap ajustado
   a la autocorrelación.

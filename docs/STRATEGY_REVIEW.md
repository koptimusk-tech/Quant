# Revisión de resultados de paper trading y decisión de preparación (Fase 13)

Objetivo: evaluar con reglas fijas y auditables lo que la Fase 12 ha observado realmente en paper trading, y decidir
para cada versión de estrategia si conviene continuar, pausar, descartar o cerrar el protocolo. No es una
autorización para operar: nada aquí envía órdenes a brókers, exchanges ni cuentas, y ningún estado, tampoco
`COMPLETED` ni `SUPPORTED_FOR_FURTHER_RESEARCH`, autoriza operar con dinero real. Una conclusión inconclusa pero
honesta es preferible a una declaración de rentabilidad sin evidencia suficiente.

## 1. Auditoría de la implementación de la Fase 12

| Pieza | Estado | Comprobado |
|---|---|---|
| Registro de candidatas (`program/registry.py`) | Implementado | Instantánea con hash, versiones, admisión por una persona, rechazos guardados (pruebas de la Fase 12 y verificación en tu PC) |
| Protocolo congelado (`program/protocol.py`) | Implementado | Hash, `frozen_at` anterior al inicio, horizonte adaptado, un protocolo por programa |
| Programa y monitor de pausas (`program/service.py`) | Implementado | Pausas con evidencia, reanudación auditada, kill switch, parada anticipada que conserva el periodo |
| Seguimiento y comparación (`program/tracking.py`, `compare.py`) | Implementado | Bootstrap de bloques, comparación en las mismas barras con convenciones |
| Clasificación por reglas (`program/classify.py`) | Implementado | Aceptación y rechazo; revisión humana solo igual o más conservadora |
| Integridad de resultados antes de evaluar | **Parcial** en la Fase 12: conciliación y orden temporal se probaban, pero no se comprobaban sobre cada programa al evaluarlo | Fase 13: diez controles por sesión (sección 4 de `qal/review/integrity.py`) |
| Operaciones independientes y dependencia | **No implementado**: el horizonte contaba operaciones, sin comprobar solapamientos ni autocorrelación | Fase 13: operaciones independientes, tamaño efectivo de muestra, HAC |
| Comparaciones múltiples entre estrategias | **No implementado** | Fase 13: Holm sobre todas las sesiones revisadas juntas |
| Métricas por operación (profit factor, expectativa, peores operaciones) | **No implementado** en la Fase 12 | Fase 13: con estado `INSUFFICIENT_DATA` cuando falta muestra |
| Cambios de criterios | **No existía un procedimiento**: un protocolo no se podía editar, pero tampoco se podía registrar un cambio justificado | Fase 13: enmiendas registradas aparte, que nunca deciden |
| Ejecución en tu PC | Verificado el 28/10 | 314 pruebas en verde; `program-audit --real-only`: 0 de 36 candidatas admisibles |

**Sesiones disponibles.** En tu base de datos real (`lab_real`) las ocho tablas de paper están vacías: no hay
ninguna sesión de paper trading, ni histórica, ni prospectiva, ni sintética. Nadie ha admitido candidatas ni iniciado
programas, porque ninguna candidata real cumple los requisitos de evidencia y una admisión exploratoria requiere una
decisión humana.

## 2. Inventario de sesiones y evidencia

`GET /review/inventory`, `qal.cli review-audit` y la sección 3 de cada informe dan, por estrategia y versión:
identificador y versión, hipótesis, protocolo (id, hash, `frozen_at`, horizonte, criterios), primera y última barra,
clase de evidencia, mercados, activos, timeframes, observaciones, señales y cambios de señal, operaciones y
operaciones independientes, barras ausentes, barras sin exposición y racha plana más larga, pausas y errores,
resultado del backtest de referencia (periodo reservado), resultados fuera de muestra, resultado del paper, fuente de
cada modelo de coste, límites de riesgo y la línea temporal de alertas, pausas y cambios de estado.

Clases de evidencia, que nunca se mezclan:

| Clase | Cuándo |
|---|---|
| `PROSPECTIVE` | Programa en modo B: solo las barras que cerraron después de congelar el protocolo |
| `HISTORICAL` | Replay de datos ya conocidos, aunque se procesen barra a barra en tiempo real |
| `SYNTHETIC` | Datos sintéticos: prueban el procedimiento, no un mercado |
| `NEW_DATA_WITHOUT_PROTOCOL` | Datos nuevos procesados sin un protocolo congelado antes |

**Inventario real:** vacío. `review-audit` sobre tus datos devuelve `INSUFFICIENT_EVIDENCE` con lo que falta por
recopilar.

## 3. Resultados cuantitativos

Integridad primero (`qal/review/integrity.py`), con estado `PASS`, `WARN`, `FAIL` o `NOT_EVALUABLE` y su evidencia:
sin lookahead (llenados después de la barra de decisión, señales decididas cuando su barra estaba disponible, y una
muestra de 20 decisiones recalculada reescribiendo los datos posteriores), orden temporal, señales frente a
decisiones de riesgo, órdenes frente a llenados, conciliación de posiciones, caja y P&L, costes aplicados una vez y con
el modelo configurado, duplicados, posiciones abiertas y datos ausentes, configuración frente al protocolo, y cambios
de código o parámetros durante el periodo. Un fallo *material* bloquea la evaluación (`BLOCKED`); el resto la
limita (`LIMITED`). Nada se corrige en silencio.

Métricas (`qal/review/metrics.py`), cada una con `n`, mínimo y nota; si no hay muestra suficiente el valor se retiene
como `INSUFFICIENT_DATA`, no se pone a cero:

- Rendimiento: P&L bruto y neto, retorno acumulado, retorno por operación, distribución por barra, beneficio y
  pérdida medios, expectativa, tasa de acierto, profit factor.
- Riesgo: volatilidad, drawdown máximo, duración de drawdowns, Sharpe y Sortino (60 barras mínimo), exposición
  bruta y neta, concentración por activo, peores operaciones y periodos, pérdidas en el peor 10 % de barras del
  subyacente.
- Ejecución: costes y desglose, slippage simulado, precio frente a referencia, órdenes no ejecutadas, parciales,
  rechazos, cancelaciones y caducadas.

La última operación abierta se valora al último cierre y no entra en las estadísticas de operaciones cerradas.

**Resultados reales:** no hay; no se ha calculado ninguna métrica sobre tus datos porque no existe ninguna sesión.

## 4. Comparaciones con backtest y baseline

Antes de leer una diferencia se comprueba la compatibilidad: versión, parámetros, universo, timeframe, horizonte (las
mismas barras), capital y tamaño, modelo y escenario de costes, supuestos de ejecución (diferencia documentada, que es
lo que se mide), método de P&L, periodo y datos, y régimen. Si algo no es compatible, la diferencia numérica no se
resume. Cada desviación lista sus explicaciones posibles y los datos que permitirían distinguirlas, sin afirmar una
causa. Los resultados fuera de muestra de la investigación son de otro periodo: se muestran como contexto, nunca se
contrastan.

Incertidumbre (`qal/review/uncertainty.py`): autocorrelación y tamaño efectivo de muestra, bootstrap de bloques con
un bloque al menos igual a la duración media de las operaciones (desde 50 barras), test HAC del exceso sobre la
baseline con p unilateral ajustado por Holm entre todas las sesiones revisadas juntas, operaciones independientes
(ventanas sin solapamiento) y Sharpe marcado como no interpretable con menos de 252 barras o 100 observaciones
efectivas. Se separan cinco capas: resultado observado, evidencia estadística, evidencia económica (neta de costes
estimados y con costes duplicados), limitaciones y conclusión permitida. Un p-value no es la probabilidad de que la
estrategia sea rentable.

## 5. Informes por estrategia

`GET /review/decisions/{id}` y `report.json` / `report.md`, o `qal.cli review-report --decision <id>`. Doce secciones:
hipótesis, protocolo original (con enmiendas aparte), evidencia, calidad de datos, métricas, comparación, incertidumbre,
incidencias con diagnóstico, criterios cumplidos e incumplidos, limitaciones, estado final y próximos pasos, más
**Qué podemos concluir y qué no podemos concluir**. Cada informe se reconstruye desde el registro guardado;
`regenerate` lo recalcula desde los datos y dice si se reproduce.

**Informes reales:** ninguno, porque no hay sesiones.

## 6. Decisiones justificadas

Motor `review-decision@1.0.0` (`qal/review/decision.py`). Reglas en orden; la primera que aplica decide:

1. Sin protocolo registrado antes de la sesión: `INCONCLUSIVE`.
2. Integridad bloqueada: `PAUSED_REVIEW`.
3. Criterio de rechazo del protocolo original cumplido: `REJECTED`.
4. Pausa abierta: `PAUSED_REVIEW`.
5. Protocolo no concluido: `CONTINUE_PAPER`.
6. Parada anticipada o criterios de aceptación incumplidos: `INCONCLUSIVE`.
7. Criterios cumplidos, pero la evidencia no respalda la hipótesis (sintética, replay histórico, admisión
   exploratoria, operaciones independientes o estabilidad insuficientes, p ajustado ≥ 0,05): `COMPLETED`.
8. Todo lo anterior cumplido con barras prospectivas de datos reales: `SUPPORTED_FOR_FURTHER_RESEARCH`, que solo
   justifica seguir investigando.

Cada decisión guarda estado, reglas evaluadas, evidencia usada, criterios superados e incumplidos, incertidumbres,
motivo, acciones recomendadas, fecha, versión del motor y los hashes de todo lo que usó. Es inmutable: una revisión
posterior con resultados distintos es una decisión nueva que apunta a la anterior. Una persona puede registrar una
revisión manual con el mismo estado o uno más conservador; los agentes y LLM no pueden cambiar la clasificación.

Los criterios originales no se cambian. Una enmienda justificada (`POST /review/protocols/{id}/amendments`) se guarda
aparte, marcada `post_hoc` si ya se habían visto resultados, y el informe muestra qué cambiaría con ella; la decisión
sigue usando el protocolo original.

El diagnóstico (`qal/review/diagnosis.py`) se activa cuando el resultado es peor de lo esperado y clasifica cada causa
posible (régimen, sobreajuste, costes, llenados, datos y latencia, distribución de señales, concentración, dependencia
de activos, errores de implementación, configuración, muestra) como `CONFIRMED`, `COMPATIBLE` o `NOT_EVALUABLE`. Para
causas mecánicas que los datos descartan (configuraciones iguales, integridad correcta, costes no superiores al
backtest, muestra completa) se usa además `RULED_OUT_BY_DATA`, en lugar de forzarlas en una de las tres. La estrategia
nunca se modifica automáticamente.

**Decisiones reales:** ninguna. El estado de la fase con tus datos es **`INSUFFICIENT_EVIDENCE`**.

## 7. Limitaciones estadísticas y operativas

- No existe evidencia prospectiva: ninguna candidata real es admisible y no se ha ejecutado ningún programa B.
- Los costes, spreads y slippage son modelos configurados (estimaciones), no observados; los llenados son simulados.
- El bootstrap supone que la dependencia es más corta que el bloque y que el periodo es estacionario.
- Holm controla el error de familia entre las sesiones revisadas juntas, no entre todas las ideas probadas en la
  investigación (eso lo cubren el DSR y el PBO de las fases anteriores).
- La regla de estabilidad (mismo signo del exceso en las dos mitades) es de la revisión, no del protocolo: solo puede
  frenar una conclusión, nunca hacerla más favorable.
- Las operaciones independientes se cuentan por ventanas sin solapamiento; en carteras de varias patas una operación es
  un periodo de tenencia.
- La revisión recalcula con el código actual; si cambió desde la sesión, el control de cambios lo indica.

## 8. Archivos creados y modificados

- Nuevos: `backend/qal/review/{__init__,integrity,metrics,uncertainty,comparison,criteria,decision,diagnosis,service,report}.py`,
  `backend/qal/api/routers/review.py`, `backend/tests/test_review.py`, `frontend/src/pages/StrategyReview.tsx`,
  `frontend/src/__tests__/strategyreview.test.tsx`, `frontend/e2e/review.spec.ts`, este documento.
- Modificados: `backend/qal/api/main.py`, `backend/qal/cli.py` (`review-audit`, `review-run`, `review-report`),
  `frontend/src/{api.ts,App.tsx}`, README, `docs/API.md`, ROADMAP.

## 9. Resultados de las pruebas

`backend/tests/test_review.py` (17 pruebas):

| Requisito | Prueba |
|---|---|
| Métricas y muestras insuficientes | `test_metrics_definitions_and_insufficient_samples` |
| Operaciones abiertas, P&L y costes | `test_pnl_costs_and_open_trades` |
| Reconciliación y cambios de parámetros | `test_reconciliation_and_parameter_changes_are_detected`, `test_integrity_passes_on_a_clean_session_and_blocks_on_defects` |
| Datos ausentes y defectuosos | `test_missing_and_defective_data_limit_the_evaluation` |
| Separación replay / prospectivo / sintético | `test_replay_synthetic_and_prospective_are_kept_apart` |
| Configuraciones incompatibles | `test_incompatible_configurations_are_not_compared` |
| Criterios predefinidos y cambios retrospectivos | `test_original_criteria_and_retrospective_amendments` |
| Clasificación | `test_decision_engine_rules_are_deterministic`, `test_continue_and_early_stop_are_not_conclusive`, `test_standalone_session_without_protocol_is_inconclusive` |
| Incertidumbre | `test_uncertainty_dependence_independent_trades_and_holm` |
| Versionado, reproducibilidad e informes | `test_report_sections_and_reproducibility` |
| Persistencia e historial de decisiones | `test_synthetic_program_classification_history_and_manual_review` |
| Evidencia insuficiente | `test_inventory_and_insufficient_evidence_without_sessions` |
| API | `test_api_flow` |
| Sin órdenes reales | `test_no_real_orders_no_credentials` |

Frontend: `strategyreview.test.tsx` (2 pruebas) y `e2e/review.spec.ts` (flujo completo en el navegador). Resultados
exactos en el informe de la fase. Ninguna prueba previa se eliminó ni se debilitó.

## 10. Instrucciones en Windows

Desde `C:\Apps\Quant\backend` con la base de datos real:

```powershell
git pull
..\.venv\Scripts\python -m pytest -q tests\test_review.py
..\.venv\Scripts\python -m qal.cli review-audit --out review_runs
..\.venv\Scripts\python -m qal.cli review-run --by "tu nombre" --out review_runs
```

`review-audit` no cambia nada: dice qué evidencia existe y qué falta. `review-run` guarda una decisión por sesión (si
no hay sesiones, no guarda nada y lo dice). En la app, **Strategy Review** muestra el resumen, los filtros, el detalle
de cada estrategia y el formulario de revisión manual. Para un informe:

```powershell
..\.venv\Scripts\python -m qal.cli review-report --decision <id> --out review_runs
```

Si Smart App Control bloquea pandas en tu PC, usa el entorno de verificación aparte (`C:\Apps\QuantVerify\venv`) con
`PYTHONPATH=C:\Apps\Quant\backend`.

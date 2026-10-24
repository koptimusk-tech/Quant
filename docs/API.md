# API (v1)

Base: `/api/v1`. Especificación completa OpenAPI en `/api/v1/openapi.json` y UI interactiva en `/docs`.

| Método | Ruta | Descripción |
|---|---|---|
| GET | `/health` | Estado, versión, `live_trading_enabled` (siempre false). |
| GET | `/providers` | Proveedores usados, último éxito/error, latencia. |
| GET | `/providers/catalog` | Adaptadores disponibles con licencias, histórico, retrasos y límites. |
| GET | `/instruments` | Instrumentos registrados. |
| GET | `/datasets?latest_only=` | Snapshots de datos. |
| GET | `/datasets/{id}` · `/quality` · `/bars` | Detalle, incidencias de calidad y barras (con los tres relojes). |
| POST | `/data/ingest` | Descarga/carga ahora o como trabajo (`run_async`, `idempotency_key`). |
| POST | `/data/upload` | Subida multipart de CSV/Parquet/MT4/MT5. |
| GET | `/data/health` | Proveedores y datasets: frescura, cobertura, huecos, calidad, latencia, errores. |
| GET | `/detectors` | Detectores, versiones y parámetros por defecto. |
| POST | `/scans` | Ejecuta un escaneo (experimento). Cacheado si datos+config son idénticos; `force` para repetir. |
| GET | `/experiments` · `/experiments/{id}` | Registro de experimentos (incluye fallidos y vacíos). |
| GET | `/anomalies` | Filtros: `asset_class`, `market`, `symbol`, `provider`, `frequency`, `anomaly_type`, `detector`, `status`, `min_stat_score`, `min_econ_score`, `synthetic`, `experiment_id`, `latest_scan_only`, `search`; `sort` (incluye `investigation_priority`), `order`, `limit`, `offset`. Cada anomalía incluye `summary`, `explanations` (rareza, significación, desglose económico, capacidad predictiva), `investigation_priority` y `stage`. |
| GET | `/anomalies/facets` | Valores disponibles para los filtros. |
| GET | `/anomalies/priority-config` | Pesos, umbrales y topes de la prioridad de investigación. |
| GET | `/anomalies/{id}` | Detalle con barras de contexto, serie del estadístico, distribución de referencia, verificación `reproduced`, hipótesis relacionadas y el último estudio de eventos. |
| POST | `/event-studies` | `{dataset_id, detector, related_dataset_id?, horizons?, seed?}`: evaluación predictiva v2 del tipo de anomalía (exceso sobre benchmark causal, IC, BH, fuera de muestra purgado, walk-forward, escenarios de coste; pares con `related_dataset_id`). |
| GET | `/event-studies/{id}` | Resultado guardado de un estudio de eventos. |
| GET/POST/DELETE | `/saved-queries` | Consultas guardadas. |
| GET/POST | `/jobs` · GET `/jobs/{id}` · POST `/jobs/{id}/cancel` | Trabajos (`ingest`, `scan`, `ingest_and_scan`, `validate`, `alpha_cycle`, `model_experiment`). Cancelar un trabajo en cola lo cancela al momento; uno en ejecución pasa a `cancelling` y `model_experiment` se detiene entre unidades de trabajo conservando los resultados parciales (los demás tipos terminan). |
| GET/POST/PATCH/DELETE | `/schedules` | Programaciones periódicas (mínimo 60 s). |
| GET | `/audit` | Eventos de auditoría. |
| GET | `/strategies` | Tipos de estrategia, parámetros y número de activos. |
| GET | `/research/proposals` | Hipótesis sugeridas con los datos y anomalías actuales, como contratos listos para registrar (no ejecuta nada). |
| GET | `/research/meta` | Estados, transiciones, gates, costes por defecto y versión de la política. |
| POST/GET | `/hypotheses` · GET `/hypotheses/{id}` | Crear (contrato validado) y listar hipótesis; el detalle incluye versiones, validaciones y decisiones de gates. |
| PATCH | `/hypotheses/{id}` | Siempre 409: los contratos están congelados; crea una versión nueva con `parent_id`. |
| POST | `/hypotheses/{id}/transition` | Transición manual (rechaza los destinos reservados al motor de políticas). |
| POST | `/hypotheses/{id}/validate` | Validación walk-forward completa y gates A–D (`run_async` para hacerlo como trabajo). |
| POST | `/hypotheses/{id}/final-test?confirm=true` | Test final sobre el holdout aislado; incrementa el contador y marca contaminación. |
| GET | `/alpha/worth-testing?include_synthetic=&dataset_id=` | Qué anomalías e ideas merece la pena testear ahora, con veredicto (`worth_testing`, `already_tested`, `not_worth`, `not_testable`) y motivos. Solo lectura: no registra ni ejecuta nada. |
| GET | `/alpha/factors?include_synthetic=` | Módulos de factores (definición completa) y si el laboratorio puede probarlos, con qué universo o por qué no. |
| POST | `/alpha/runs` | `{budget, costs, run_experiments}`: encola un ciclo acotado de los cuatro agentes (trabajo `alpha_cycle`). `budget.llm_tokens` solo admite 0. |
| GET | `/alpha/runs` · `/alpha/runs/{id}` | Ciclos con sus recuentos; el detalle incluye todos los mensajes tipados de los agentes. |
| GET | `/alpha/relationships/{id}` | Resultado completo de una búsqueda de relaciones (todas las parejas y todas las pruebas). |
| GET | `/alpha/hypotheses` · `/alpha/hypotheses/{id}` | Hipótesis con origen, objeciones y, por separado, prioridad de investigación, evidencia predictiva y rentabilidad neta validada; el detalle compara experimentos y explica cada decisión. |
| POST | `/alpha/hypotheses/{id}/experiments` | Encola una validación del contrato congelado. |
| GET | `/models/detectors` | Detectores de una serie que el Model Lab puede comparar: parámetros, valores por defecto, si son multivariantes y si la semilla cambia el resultado. |
| GET | `/models/features` | Catálogo de características: fórmula, fuente, ventana, disponibilidad, valores ausentes, supuestos y sesgos. |
| POST | `/models/experiments` | `{dataset_ids, models: [{detector, params, label}], seeds, window_variants, predictive, horizons, max_seconds, benchmark, label, run_async}`: compara modelos (trabajo `model_experiment`). Errores de configuración → 400. Una petición idéntica reutiliza el experimento guardado. |
| GET | `/models/experiments` · `/models/experiments/{id}` | Comparaciones guardadas (tipo `model_comparison`), con todas las medidas por dataset × modelo. |
| GET | `/models/experiments/{id}/export.csv` | Una fila por dataset × modelo. |
| GET/POST | `/alpha/hypotheses/{id}/paper-eligibility` | Requisitos para `PAPER_TRADING_ELIGIBLE` y aprobación explícita de una persona con límites de riesgo. No envía órdenes. |

Ejemplo de escaneo con un par:

```json
POST /api/v1/scans
{"dataset_ids": ["<id SPY>", "<id QQQ>"],
 "detectors": {"return_shock": {"threshold": 6}, "volume_anomaly": {}, "volatility_regime": {}},
 "pairs": [["<id QQQ>", "<id SPY>"]],
 "cost_bps": {"etf": 3}}
```

## Portfolio & Risk

| Método | Ruta | Descripción |
|---|---|---|
| POST | `/portfolio/strategies?include_synthetic=` | Cuerpo opcional `EligibilityCriteria`. Todas las hipótesis validadas como estrategias, elegibles o rechazadas con motivos; sintéticas solo si se piden. |
| GET | `/portfolio/methods` | Métodos de construcción. |
| GET | `/portfolio/policies` · `/portfolio/policies/active` | Versiones de la política de riesgo. |
| POST | `/portfolio/policies` | `{name, limits, approved_by, actor_kind: "human", confirm: true}`: nueva versión. Agentes o sin confirmación → 403 (auditado). |
| POST | `/portfolio/runs` | `{strategy_ids, methods, rebalance, lookback_days, cov_method, budgets, cost_multiplier, extra_slippage_bps, execution_delay_days, criteria, policy_id, mc_paths, stress_backtests, run_async}`: backtest del portfolio (trabajo `portfolio_run`). |
| GET | `/portfolio/runs` · `/portfolio/runs/{id}` | Ejecuciones guardadas (`portfolio_backtest`) con métodos, riesgo, correlaciones, estrés y comprobaciones. |
| GET | `/portfolio/runs/{id}/decisions?decision=&rule=` | Decisiones del risk manager: regla, observado, límite, decisión, acción, motivo, fecha, versión de política. |
| POST | `/portfolio/proposals` | `{include_synthetic, strategy_ids}`: el agente propone combinaciones (trabajo `portfolio_proposals`). Enviar `limits` o `policy_id` → 403. |
| GET | `/portfolio/proposals` · `/portfolio/proposals/{id}` | Propuestas con veredicto del risk manager y el registro de mensajes. |
| POST | `/portfolio/sizing` | Tamaño de una posición redondeado al contrato del instrumento. |


## Paper Trading (simulación)

Ninguna ruta envía órdenes fuera de la aplicación. Las acciones llevan `by` (persona que actúa).

| Método | Ruta | Descripción |
|---|---|---|
| GET | `/paper/strategies` | Estrategias validadas (elegibles o no, con motivos) y reglas `DEMO:` no validadas. |
| POST | `/paper/sessions` | `SessionConfig`: `{name, created_by, mode: replay\|new_data, strategies: [{strategy_id, dataset_ids, params, allocation}], capital, account_currency, start, end, cost_scenario, cost_overrides, delay_bars, order_type, limit_offset_bps, order_ttl_bars, stop_loss_pct, stop_order_type, rebalance_threshold, allow_unvalidated_demo, criteria, policy_id}`. |
| GET | `/paper/sessions` · `/paper/sessions/{id}` | Sesiones con estado, etiquetas, resumen, últimas señales, configuración y referencias de datos. |
| POST | `/paper/sessions/{id}/start` · `/pause` | Arrancar o reanudar; pausar. |
| POST | `/paper/sessions/{id}/step` | `{by, bars, skip_bad_bar}`: procesar barras de forma síncrona (máx. 500). |
| POST | `/paper/sessions/{id}/run` | `{by, max_bars}`: trabajo `paper_run` en segundo plano. |
| POST | `/paper/sessions/{id}/kill-switch` | `{by, active, confirm: true, reason}`. |
| POST | `/paper/sessions/{id}/orders` · `/orders/{oid}/cancel` | Orden manual simulada (pasa por el control de riesgo); cancelar. |
| PATCH | `/paper/sessions/{id}/config` | `{by, changes, reason}`: solo campos operativos; versionado y registrado. |
| GET | `/paper/sessions/{id}/orders` · `/fills` · `/positions` · `/equity` · `/risk` · `/events` | Filtros `status`, `strategy`, `instrument`, `start`, `end`, `level`, `category`. |
| POST | `/paper/sessions/{id}/reconcile` | Reconciliación manual. |
| POST · GET | `/paper/sessions/{id}/evaluation` | Evaluar frente al backtest (no es una aprobación) / último resultado. |
| GET | `/paper/sessions/{id}/export?table=orders\|fills\|equity\|ledger\|events\|signals` | CSV. |
| POST | `/paper/sessions/{id}/reset` · `/delete` | `{by, confirm: true}`; el registro del experimento se conserva. |
| GET | `/paper/compare?ids=a,b` | Comparación de sesiones. |

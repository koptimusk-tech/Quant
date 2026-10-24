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
| GET/POST | `/jobs` · GET `/jobs/{id}` · POST `/jobs/{id}/cancel` | Trabajos (`ingest`, `scan`, `ingest_and_scan`, `validate`, `alpha_cycle`). |
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
| GET | `/alpha/factors?include_synthetic=` | Módulos de factores (definición completa) y si el laboratorio puede probarlos, con qué universo o por qué no. |
| POST | `/alpha/runs` | `{budget, costs, run_experiments}`: encola un ciclo acotado de los cuatro agentes (trabajo `alpha_cycle`). `budget.llm_tokens` solo admite 0. |
| GET | `/alpha/runs` · `/alpha/runs/{id}` | Ciclos con sus recuentos; el detalle incluye todos los mensajes tipados de los agentes. |
| GET | `/alpha/relationships/{id}` | Resultado completo de una búsqueda de relaciones (todas las parejas y todas las pruebas). |
| GET | `/alpha/hypotheses` · `/alpha/hypotheses/{id}` | Hipótesis con origen, objeciones y, por separado, prioridad de investigación, evidencia predictiva y rentabilidad neta validada; el detalle compara experimentos y explica cada decisión. |
| POST | `/alpha/hypotheses/{id}/experiments` | Encola una validación del contrato congelado. |
| GET/POST | `/alpha/hypotheses/{id}/paper-eligibility` | Requisitos para `PAPER_TRADING_ELIGIBLE` y aprobación explícita de una persona con límites de riesgo. No envía órdenes. |

Ejemplo de escaneo con un par:

```json
POST /api/v1/scans
{"dataset_ids": ["<id SPY>", "<id QQQ>"],
 "detectors": {"return_shock": {"threshold": 6}, "volume_anomaly": {}, "volatility_regime": {}},
 "pairs": [["<id QQQ>", "<id SPY>"]],
 "cost_bps": {"etf": 3}}
```

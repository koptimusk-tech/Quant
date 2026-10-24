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
| GET | `/anomalies` | Filtros: `asset_class`, `market`, `symbol`, `provider`, `frequency`, `anomaly_type`, `detector`, `status`, `min_stat_score`, `min_econ_score`, `synthetic`, `experiment_id`, `latest_scan_only`, `search`; `sort`, `order`, `limit`, `offset`. |
| GET | `/anomalies/facets` | Valores disponibles para los filtros. |
| GET | `/anomalies/{id}` | Detalle con barras de contexto, serie del estadístico, distribución de referencia y verificación `reproduced`. |
| GET/POST/DELETE | `/saved-queries` | Consultas guardadas. |
| GET/POST | `/jobs` · GET `/jobs/{id}` · POST `/jobs/{id}/cancel` | Trabajos (`ingest`, `scan`, `ingest_and_scan`, `validate`). |
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

Ejemplo de escaneo con un par:

```json
POST /api/v1/scans
{"dataset_ids": ["<id SPY>", "<id QQQ>"],
 "detectors": {"return_shock": {"threshold": 6}, "volume_anomaly": {}, "volatility_regime": {}},
 "pairs": [["<id QQQ>", "<id SPY>"]],
 "cost_bps": {"etf": 3}}
```

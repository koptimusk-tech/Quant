# Arquitectura

## Principios que la arquitectura impone

| Principio | Cómo se materializa en el código |
|---|---|
| La evidencia cuantitativa manda | Las métricas las calcula código determinista (`qal/anomaly`, `qal/features`). Los LLM (Fase 3) solo leerán y propondrán sobre registros estructurados. |
| Reproducibilidad | Snapshots Parquet inmutables con hash de contenido; cada experimento guarda configuración, versión de detectores, versión de código y hashes de datos. El detalle de una anomalía **re-ejecuta** el detector y muestra si se reproduce. |
| Sin look-ahead | Tres relojes por barra (`event_ts`, `available_ts`, `received_ts`). Las líneas base de los detectores terminan en t-1. Pruebas de truncado y "futuro envenenado" en `tests/test_no_lookahead.py`. |
| Resultados negativos registrados | Escaneos vacíos, fallidos y bloqueados por Gate A quedan en `experiments` y `audit_events`. |
| Datos sintéticos distinguibles | Proveedor `synthetic`, columna `is_synthetic` en cada barra, dataset y anomalía; etiqueta SYNTHETIC en la UI y filtro real/sintético. |
| Credenciales separadas | Las credenciales solo se leen de la configuración del proceso (`QAL_*`), nunca de cargas de la API. La ingesta cripto no usa claves. No hay código de ejecución. |

## Módulos (monolito modular)

```
backend/qal/
  config.py, db.py, models.py, audit.py, versioning.py
  data/                    Market Data Service
    schema.py              esquema canónico point-in-time, normalización
    adapters/              local_file (CSV/Parquet/MT4/MT5), yahoo, ccxt, metatrader5, stooq, synthetic
    quality.py             Gate A: comprobaciones de calidad
    store.py               almacén Parquet direccionado por contenido
    ingest.py              fetch → normalizar → fusionar (incremental) → calidad → guardar → registrar
  features/basic.py        Feature Engineering (causal): retornos, EWMA vol, estadísticos robustos de ventana
  anomaly/                 Anomaly Detection Engine
    base.py, detectors.py  detectores versionados
    engine.py              escaneo, BH, contexto económico preliminar, Experiment Registry
  jobs/                    trabajos persistentes, reintentos, idempotencia, planificador
  api/                     Web API (FastAPI, /api/v1)
  cli.py                   CLI (demo, ingest, scan, worker)
frontend/                  React + TypeScript + Vite + Tailwind
```

Los módulos de fases posteriores (Research and Hypothesis Service, Backtesting Engine, Validation Service,
Portfolio and Risk Engine, Agent Orchestrator, Alerting) se añadirán como paquetes hermanos
(`qal/research`, `qal/backtest`, `qal/validation`, `qal/risk`, `qal/agents`, `qal/alerts`). No se separan en
microservicios mientras no haya una necesidad de escalado o aislamiento concreta; la excepción prevista es el
servicio de ejecución (Fase 7), que irá en un proceso aparte con sus propias credenciales (DECISIONS D-008).

## Flujo de datos (Fase 1)

```
Proveedor ──fetch──▶ adaptador ──▶ normalize_bars ──▶ merge con snapshot previo ──▶ check_bars (Gate A)
                                                                                     │
                       datasets (fila) ◀── ParquetStore.write (hash) ◀────────────────┘
                                │
     ScanConfig ──▶ run_scan ──▶ ¿dataset fail? → bloqueado (registrado)
                                │
                                ├─ detectores por serie y por par → p-valores de todas las pruebas
                                ├─ BH sobre todas las pruebas del escaneo
                                ├─ contexto económico preliminar, calidad, explicaciones alternativas
                                └─ experiments + anomaly_detections + audit_events
```

Los trabajos (`ingest`, `scan`, `ingest_and_scan`) se ejecutan en un hilo de fondo dentro de la API
(`QAL_ENABLE_SCHEDULER`) o con `python -m qal.cli worker`. Su estado vive en la base de datos.

## Modelo de datos (Fase 1)

| Tabla | Contenido |
|---|---|
| `providers` | Proveedor/adaptador, si es sintético, último éxito, último error, latencia. |
| `instruments` | Símbolo por proveedor, clase de activo, mercado, divisa, zona horaria. |
| `datasets` | Snapshot inmutable: hash, ruta Parquet, filas, rango, ajuste, estado de calidad, `is_latest`. |
| `data_quality_events` | Incidencias de calidad por dataset (check, severidad, recuento, detalles). |
| `experiments` | Registro de experimentos: tipo, estado, versión de algoritmo y código, configuración y su hash, referencias a datos, semilla, nº de pruebas, resultados, error. |
| `anomaly_detections` | Anomalía: detector y versión, tipo, instrumento(s), dataset(s), `event_ts`, `available_ts`, estadístico, p empírico, p ajustado BH, puntuaciones estadística y económica, métricas, contexto económico, calidad, explicaciones alternativas, `is_synthetic`, estado de validación. |
| `jobs`, `schedules` | Cola persistente y programaciones. |
| `saved_queries` | Consultas guardadas del Market Scanner. |
| `audit_events` | Auditoría de acciones (actor, acción, entidad, detalles). |

Migraciones: `backend/migrations` (Alembic). `tests/test_migrations.py` comprueba que la migración produce
exactamente el esquema ORM.

Entidades previstas en fases siguientes: Hypotheses, Backtest Runs, Validation Reports, Strategy Versions,
Risk Policies, Gate Decisions, Agent Runs, Paper Orders, Portfolio Positions, Features (catálogo).

## Estados de investigación

Toda anomalía nace en `DISCOVERED`. La máquina de estados completa (`INVESTIGATING`, `HYPOTHESIS_FORMULATED`,
`BACKTEST_PENDING`, `VALIDATION_FAILED`, `VALIDATION_PASSED`, `PAPER_TRADING`, `RISK_APPROVED`, `LIVE_ELIGIBLE`,
`LIVE_ENABLED`, `SUSPENDED`, `REJECTED`) y sus transiciones controladas por el motor de políticas se implementan
en las Fases 2, 3 y 6. Las transiciones solo las podrá ejecutar el motor de políticas determinista; los agentes
podrán solicitarlas, nunca forzarlas.

## Puntuaciones

- **Puntuación estadística**: `|s| / (|s| + umbral)` del estadístico del detector. Sirve para ordenar, no es una
  probabilidad. La evidencia estadística es el p ajustado (BH) junto al p empírico.
- **Puntuación económica (preliminar)**: tamaño del movimiento frente a un coste de ida y vuelta supuesto por
  clase de activo, penalizado por liquidez baja. No es un backtest. Los índices no negociables reciben `n/a`.

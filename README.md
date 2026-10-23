# Quant Anomaly Lab

Laboratorio de investigación cuantitativa para **descubrir anomalías estadísticas**, registrarlas de forma
reproducible y, en fases posteriores, convertirlas en hipótesis falsables, intentar refutarlas y validarlas
bajo controles de riesgo estrictos.

> Las anomalías detectadas son observaciones para investigar, no señales de trading. La plataforma no promete
> rentabilidad y **no ejecuta órdenes reales** (no existe código de ejecución en esta versión).

Estado: **Fase 0 (diseño) y Fase 1 (MVP de anomalías) completadas.** Ver [ROADMAP.md](ROADMAP.md).

## Qué hace hoy

- **Descarga automática de datos** desde Yahoo Finance, exchanges cripto vía CCXT (Binance, Kraken, Coinbase,
  Bybit, OKX…), MetaTrader 5 (terminal en Windows), Stooq, ficheros CSV/Parquet y exportaciones de MT4/MT5.
  Descarga incremental, detección de revisiones del proveedor y descargas programadas.
- **Datos point-in-time**: cada barra lleva `event_ts`, `available_ts` y `received_ts`, proveedor, instrumento,
  mercado, frecuencia, referencia de origen y bandera `is_synthetic`. Snapshots Parquet inmutables
  direccionados por hash de contenido.
- **Gate A (calidad de datos)**: duplicados, OHLC incoherente, precios no positivos, huecos, cobertura,
  disponibilidad no reconstruible, acciones corporativas, revisiones. Un dataset en `fail` queda bloqueado.
- **Detectores** (causales, con pruebas de no look-ahead): retornos extremos (z robusto/MAD), anomalías de volumen
  y liquidez, cambios de régimen de volatilidad y divergencia entre activos relacionados (OLS rodante +
  Engle-Granger).
- **Registro de experimentos**: cada escaneo guarda configuración, versiones de detectores y código, hashes de los
  datos, número de pruebas, resultados, errores y escaneos sin resultados o fallidos. Control de pruebas múltiples
  (Benjamini-Hochberg sobre todas las pruebas del escaneo).
- **API REST versionada** (`/api/v1`, OpenAPI en `/docs`), trabajos en segundo plano persistentes con reintentos e
  idempotencia, y planificador.
- **Frontend**: Market Scanner (filtros, búsqueda, consultas guardadas, puntuación estadística y económica por
  separado), Anomaly Detail (gráfico del evento, distribución de referencia, comprobación de reproducibilidad,
  datos usados, explicaciones alternativas) y Data Health (proveedores, frescura, cobertura, huecos, calidad,
  latencia, errores, descargas y programaciones).
- **Datos sintéticos** claramente etiquetados con anomalías inyectadas conocidas, para probar detectores sin red.

## Arranque rápido (desarrollo, sin Docker)

Requisitos: Python ≥ 3.11, Node ≥ 20.

```bash
make install            # crea .venv, instala backend[all] y el frontend
make demo               # carga datos SINTÉTICOS y ejecuta un escaneo (no necesita red)
make api                # API en http://127.0.0.1:8000  (docs: /docs)
make web                # UI en http://127.0.0.1:5173
```

Sin `make`:

```bash
python3 -m venv .venv && .venv/bin/pip install -e "./backend[all]"
cd backend && ../.venv/bin/python -m qal.cli demo
../.venv/bin/uvicorn qal.api.main:app --port 8000
# en otra terminal
cd frontend && npm install && npm run dev
```

Por defecto usa SQLite en `backend/var/qal.db` y guarda Parquet en `backend/var/data`. Copia
[.env.example](.env.example) a `backend/.env` para cambiar la configuración.

## Con Docker Compose (PostgreSQL)

```bash
docker compose up --build
# UI: http://localhost:8080   API: http://localhost:8000/docs
```

La API aplica las migraciones (`alembic upgrade head`) al arrancar.

## Descargar datos reales

Desde la UI (Data Health → *Download market data*) o por CLI/API:

```bash
cd backend
../.venv/bin/python -m qal.cli ingest yahoo SPY etf --start 2010-01-01
../.venv/bin/python -m qal.cli ingest yahoo EURUSD=X fx
../.venv/bin/python -m qal.cli ingest ccxt BTC/USDT crypto --frequency 1h --start 2024-01-01 --exchange binance
../.venv/bin/python -m qal.cli ingest stooq spy.us etf
../.venv/bin/python -m qal.cli ingest local_file EURUSD fx --frequency 1h \
    --options '{"path": "EURUSD_H1.csv", "format": "mt5", "source_timezone": "Etc/GMT-2"}'
../.venv/bin/python -m qal.cli scan            # escanea los últimos datasets
```

Programar una descarga + escaneo automático cada 24 h:

```bash
curl -X POST localhost:8000/api/v1/schedules -H 'content-type: application/json' -d '{
  "name": "ETF diario", "kind": "ingest_and_scan", "interval_seconds": 86400,
  "payload": {"items": [
     {"adapter": "yahoo", "symbol": "SPY", "asset_class": "etf"},
     {"adapter": "yahoo", "symbol": "QQQ", "asset_class": "etf"}],
   "pairs": [["QQQ", "SPY"]]}}'
```

MetaTrader 5: el paquete oficial `MetaTrader5` solo funciona en Windows con el terminal instalado
(`pip install MetaTrader5`). En macOS/Linux exporta el histórico desde MT4/MT5 y súbelo con el adaptador
`local_file` (`format: mt4|mt5`) o `POST /api/v1/data/upload`. Las horas de MT5 son hora del servidor del bróker:
indica `server_utc_offset_hours`.

Si un proveedor falla, el error se registra (Data Health, auditoría) y **no se generan datos de sustitución**.
Licencias, límites y retrasos de cada proveedor: [docs/DATA_PROVIDERS.md](docs/DATA_PROVIDERS.md).

## Pruebas

```bash
make test-backend                     # pytest (SQLite)
cd backend && QAL_TEST_DATABASE_URL=postgresql+psycopg://qal:qal@localhost:5432/qal_test \
  ../.venv/bin/python -m pytest -q   # misma suite contra PostgreSQL
../.venv/bin/python -m pytest -m network            # llamadas reales a Yahoo/Binance (opcional)
cd ..
make test-frontend                    # tsc + Vitest
make e2e                              # Playwright: requiere API con demo cargada y `npm run dev`
```

## Documentación

- [ARCHITECTURE.md](ARCHITECTURE.md): módulos, flujo de datos, modelo de datos, estados.
- [DECISIONS.md](DECISIONS.md): decisiones de diseño y su justificación.
- [ROADMAP.md](ROADMAP.md): fases, incluida la app de escritorio.
- [docs/API.md](docs/API.md): endpoints principales (OpenAPI completo en `/docs`).
- [docs/DETECTORS.md](docs/DETECTORS.md): estadísticos, supuestos y limitaciones de cada detector.
- [docs/DATA_PROVIDERS.md](docs/DATA_PROVIDERS.md): evaluación de proveedores.
- [docs/RISKS.md](docs/RISKS.md): riesgos identificados en la Fase 0.

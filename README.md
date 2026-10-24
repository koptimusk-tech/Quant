# Quant Anomaly Lab

Laboratorio de investigación cuantitativa para **descubrir anomalías estadísticas**, registrarlas de forma
reproducible y, en fases posteriores, convertirlas en hipótesis falsables, intentar refutarlas y validarlas
bajo controles de riesgo estrictos.

> Las anomalías detectadas son observaciones para investigar, no señales de trading. La plataforma no promete
> rentabilidad y **no ejecuta órdenes reales** (no existe código de ejecución en esta versión).

Estado: **Fases 0, 1 y 2 completadas, con app de escritorio.** Ver [ROADMAP.md](ROADMAP.md).

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
- **Explicación y prioridad de anomalías**: cada puntuación se explica por separado (rareza, significación con p
  ajustado, relevancia económica desglosada y capacidad predictiva), con una prioridad de investigación documentada,
  la etapa de cada anomalía y un estudio de eventos fuera de muestra y neto de costes por tipo de anomalía. Una
  anomalía interesante no es una estrategia validada. Ver [docs/DETECTORS.md](docs/DETECTORS.md).
- **Datos sintéticos** claramente etiquetados con anomalías inyectadas conocidas, para probar detectores sin red.
- **Investigación (Fase 2)**: contratos de hipótesis falsables con criterios de éxito fijados de antemano y congelados
  (hash), modelo de costes completo, motor de backtesting point-in-time (ejecución en la apertura siguiente),
  walk-forward con embargo, purged K-fold, test final aislado con contador de accesos, sensibilidad a costes y
  parámetros, estabilidad por ventana y régimen, bootstrap por bloques, placebo, Deflated Sharpe Ratio, PBO,
  comprobaciones de leakage y hard gates A–D deterministas que ningún usuario ni agente puede anular.
- **Pantallas** Tests (elegir, ejecutar y programar pruebas), Quant Research Lab y Backtesting & Validation.
- **App de escritorio** (Tauri) que arranca el motor local y abre la interfaz. Instaladores para Windows, macOS y
  Linux sin necesidad de Python, generados en GitHub Actions. Ver [desktop/README.md](desktop/README.md).
- **Integración continua** (`.github/workflows/ci.yml`): pruebas del backend en SQLite y PostgreSQL, frontend y
  extremo a extremo con navegador en cada cambio.

## Puesta en marcha en Windows (PowerShell)

Probado en Windows 11 con Python 3.12 y Node 24. No hace falta ninguna credencial de proveedores financieros: el
ejemplo usa datos sintéticos etiquetados, y los datos reales se cargan desde un fichero CSV o desde fuentes públicas.

**Requisitos**
- Python 3.11, 3.12 o 3.13 (python.org o `winget install -e --id Python.Python.3.12`). Comprueba con `py -3.12 --version`.
- Node.js 20 o superior (`winget install -e --id OpenJS.NodeJS.LTS`). Comprueba con `node --version`.
- Git (`winget install -e --id Git.Git`).
- Opcional: MetaTrader 5 instalado y con sesión iniciada, para descargar del terminal.

**1. Instalar** (una vez; unos 5 minutos)

```powershell
git clone https://github.com/koptimusk-tech/Quant.git C:\Apps\Quant
cd C:\Apps\Quant
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\pip install -e ".\backend[yahoo,crypto,metatrader,dev]"
cd frontend
npm ci
npm run build
cd ..
```

Si `npm` muestra un aviso sobre `allow-scripts` y `esbuild`, puedes ignorarlo: la compilación funciona igual.
En PowerShell 5.1 algunos símbolos (como σ) pueden verse mal en la consola; es solo visual.

**2. Base de datos y configuración.** No hay que configurar nada: por defecto usa SQLite en
`backend\var\qal.db` y guarda los datos en `backend\var\data`. Para cambiarlo, copia `.env.example` a
`backend\.env` y edita las variables:

| Variable | Por defecto | Para qué |
|---|---|---|
| `QAL_DATABASE_URL` | `sqlite:///./var/qal.db` | Base de datos (SQLite, o PostgreSQL con `postgresql+psycopg://…`). |
| `QAL_DATA_DIR` | `./var/data` | Snapshots Parquet de los datos. |
| `QAL_FRONTEND_DIST` | (vacío) | Carpeta de la interfaz compilada, para servirla desde la API. |
| `QAL_ENABLE_SCHEDULER` | `true` | Trabajos y programaciones en segundo plano. |
| `QAL_MT5_TERMINAL_PATH`, `QAL_MT5_LOGIN`… | (vacío) | Solo si MetaTrader 5 no es el terminal con sesión abierta. |

**3. Cargar el dataset de ejemplo.** Son datos SINTÉTICOS con anomalías inyectadas conocidas, y la interfaz los
marca como `SYNTHETIC`.

```powershell
cd C:\Apps\Quant\backend
..\.venv\Scripts\python -m qal.cli demo
```

**4. Arrancar** (API e interfaz en un solo proceso)

```powershell
cd C:\Apps\Quant\backend
$env:QAL_FRONTEND_DIST = "C:\Apps\Quant\frontend\dist"
..\.venv\Scripts\python -m qal.server --port 8000
```

Abre http://127.0.0.1:8000. La documentación de la API está en http://127.0.0.1:8000/docs. Para parar, pulsa
Ctrl+C.

Para desarrollar la interfaz con recarga automática, deja la API anterior en marcha y en otra ventana ejecuta
`cd C:\Apps\Quant\frontend; npm run dev`. Después abre http://127.0.0.1:5173.

**5. Ejecutar las pruebas**

```powershell
cd C:\Apps\Quant\backend
..\.venv\Scripts\python -m pytest -q              # backend (SQLite)
cd ..\frontend
npm test                                         # interfaz (Vitest)
npx playwright install chromium                  # una vez
$env:QAL_WEB_URL = "http://127.0.0.1:8000"       # con la API del paso 4 en marcha y el demo cargado
npx playwright test                              # navegador, de extremo a extremo
```

**6. Cargar datos reales**
- **Fichero CSV o export de MetaTrader:**
  1. En MT5, abre *Ver → Símbolos → Barras*.
  2. Elige el símbolo (p. ej. EURUSD), el periodo (H1, D1…) y las fechas, y pulsa *Solicitar* y luego
     *Exportar barras*.
  3. En la app, ve a **Data Health → Load a file** y elige el fichero.
  4. Escribe el símbolo y el periodo, elige el formato `mt5` e indica la zona horaria del servidor del bróker.
     La mayoría de brókers de MT5 usan UTC+2 en invierno y UTC+3 en verano (horario de Nueva York + 7 h): para
     ellos usa una zona con cambio de hora, como `Europe/Helsinki` o `Europe/Athens`. `Etc/GMT-2` es UTC+2 fijo,
     sin horario de verano, y solo vale si tu bróker no cambia de hora.
  5. Pulsa **Load file**.

  Para barras diarias (D1), el bróker ya cierra el día a su medianoche. Si indicas una zona distinta de UTC, la
  fecha se convierte a UTC y la barra aparece con la hora 22:00 o 21:00 del día anterior. Para D1 suele ser más
  claro dejar la zona en `UTC`.

  Desde PowerShell 5.1 (`Invoke-RestMethod` no tiene `-Form`), usa `curl.exe`:

  ```powershell
  curl.exe -F "file=@EURUSD_H1.csv" -F symbol=EURUSD -F asset_class=fx -F frequency=1h `
           -F format=mt5 -F source_timezone=Europe/Helsinki http://127.0.0.1:8000/api/v1/data/upload
  ```

  También vale cualquier CSV con columnas date/time, open, high, low, close y, opcionalmente, volume.
- **MetaTrader 5 directo:** con el terminal abierto, ve a **Data Health → Download market data** y elige el
  proveedor *MetaTrader 5 terminal*, el símbolo, la clase `fx` y el periodo.
- **Yahoo Finance (gratis, sin clave):** en el mismo formulario, elige el proveedor *Yahoo Finance* y un símbolo
  como `EURUSD=X` o `SPY`.

Después, ve a **Tests**, marca el instrumento y pulsa **Run now**. Las anomalías aparecen en **Market Scanner**.

## Arranque en Linux o macOS (sin Docker)

Requisitos: Python 3.11–3.13 y Node ≥ 20.

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
../.venv/bin/python -m qal.server --port 8000
# en otra terminal
cd frontend && npm install && npm run dev
```

Por defecto usa SQLite en `backend/var/qal.db` y guarda Parquet en `backend/var/data`. Copia
[.env.example](.env.example) a `backend/.env` para cambiar la configuración.

## Con Docker Compose (PostgreSQL)

Funciona igual en Windows con Docker Desktop.

```bash
docker compose up --build
docker compose exec api python -m qal.cli demo     # opcional: dataset sintético de ejemplo
# UI: http://localhost:8080   API: http://localhost:8000/docs
```

La API aplica las migraciones (`alembic upgrade head`) al arrancar. La integración continua levanta este stack en
cada cambio, carga el demo y lo consulta a través de la interfaz.

## Descargar datos reales por CLI o API

Desde la UI (Data Health → *Download market data*) o por CLI/API:

```bash
cd backend
../.venv/bin/python -m qal.cli ingest yahoo SPY etf --start 2010-01-01
../.venv/bin/python -m qal.cli ingest yahoo EURUSD=X fx
../.venv/bin/python -m qal.cli ingest ccxt BTC/USDT crypto --frequency 1h --start 2024-01-01 --exchange binance
../.venv/bin/python -m qal.cli ingest stooq spy.us etf
../.venv/bin/python -m qal.cli ingest local_file EURUSD fx --frequency 1h \
    --options '{"path": "EURUSD_H1.csv", "format": "mt5", "source_timezone": "Europe/Helsinki"}'
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

## Flujo de investigación

1. *Market Scanner* → abre una anomalía → **Investigate → hypothesis**.
2. *Research Lab*: completa premisa, mecanismo, reglas, riesgos, invalidación, criterios de éxito y diseño fuera de
   muestra (ventanas, embargo, inicio del holdout). Al guardar, el contrato queda congelado.
3. **Run validation**: walk-forward sobre el periodo de investigación, informes de robustez y sobreajuste, y gates.
   El estado pasa a `VALIDATION_PASSED` o `VALIDATION_FAILED` solo por decisión del motor de políticas.
4. **Final test** sobre el holdout, una vez al terminar de iterar. Cada acceso se registra; el segundo marca el
   holdout como contaminado.

Cada validación cuenta como ensayo para el Deflated Sharpe de toda la familia de versiones de la hipótesis.
Metodología: [docs/RESEARCH.md](docs/RESEARCH.md).

## Documentación

- [ARCHITECTURE.md](ARCHITECTURE.md): módulos, flujo de datos, modelo de datos, estados.
- [DECISIONS.md](DECISIONS.md): decisiones de diseño y su justificación.
- [ROADMAP.md](ROADMAP.md): fases, incluida la app de escritorio.
- [docs/API.md](docs/API.md): endpoints principales (OpenAPI completo en `/docs`).
- [docs/RESEARCH.md](docs/RESEARCH.md): contratos, backtesting, validación, gates.
- [desktop/README.md](desktop/README.md): app de escritorio.
- [docs/DETECTORS.md](docs/DETECTORS.md): estadísticos, supuestos y limitaciones de cada detector.
- [docs/DATA_PROVIDERS.md](docs/DATA_PROVIDERS.md): evaluación de proveedores.
- [docs/RISKS.md](docs/RISKS.md): riesgos identificados en la Fase 0.

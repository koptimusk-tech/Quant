# Roadmap

Cada fase se entrega con código, pruebas automatizadas y documentación actualizada. Una fase no se considera
cerrada sin pruebas que demuestren sus criterios de aceptación.

## ✅ Fase 0 — Diseño
Arquitectura (ARCHITECTURE.md), decisiones (DECISIONS.md), riesgos (docs/RISKS.md), evaluación de proveedores
(docs/DATA_PROVIDERS.md).

## ✅ Fase 1 — MVP de anomalías
- Datos reales por proveedores configurables: Yahoo, CCXT (Binance y otros), MetaTrader 5, Stooq.
- CSV, Parquet y exportaciones MT4/MT5 (fichero o subida por API).
- Datos sintéticos etiquetados con anomalías inyectadas conocidas.
- Descarga incremental, revisiones, descargas programadas (`ingest_and_scan`).
- Gate A de calidad de datos.
- Detectores: retornos extremos, volumen/liquidez, régimen de volatilidad, divergencia entre activos.
- Persistencia (SQLite/PostgreSQL, Alembic), registro de experimentos, auditoría.
- API versionada, trabajos en segundo plano con reintentos e idempotencia.
- Market Scanner, Anomaly Detail, Data Health.
- Pruebas unitarias, de integración (API + SQLite + PostgreSQL), Vitest y Playwright.

Pendiente de verificar fuera de este entorno: descargas reales (la red del entorno de desarrollo bloqueaba los
proveedores; los adaptadores están probados con respuestas simuladas y hay pruebas `-m network` para ejecutarlas
en local) y `docker compose build` (Docker Hub limitó las descargas durante la prueba).

## Fase 2 — Investigación y backtesting
- Contratos de hipótesis (Pydantic, validados): premisa, anomalía origen, universo, mecanismo, variables,
  entrada/salida, horizonte, benchmark, costes, invalidación, métrica de éxito y diseño OOS fijados **antes** de
  ver resultados.
- Motor de backtesting event-driven sobre `available_ts`, con modelo de costes por instrumento (comisión, spread,
  slippage, financiación, funding, rollover) y restricciones de liquidez.
- Walk-forward, purging y embargo; controles de leakage; métricas de robustez.
- Máquina de estados de investigación y primeras transiciones controladas por el motor de políticas.
- Pantallas Quant Research Lab y Backtesting & Validation.

## Fase 3 — Agentes de investigación
Anomaly Discovery, LLM Alpha Mining, Quant Research, Adversarial Reviewer y Research Coordinator, con salidas
estructuradas, presupuesto de tokens/tiempo/coste por experimento, límites de debate y reintentos, caché,
cancelación y trazabilidad. Pantalla Agent Research Room. Los agentes no tienen acceso a credenciales ni a
transiciones de estado directas.

## Fase 4 — Validación avanzada
CPCV cuando proceda, Deflated Sharpe Ratio, PBO, bootstrap por bloques, matrices de sensibilidad, control de
pruebas múltiples a nivel de programa de investigación (contando todos los experimentos registrados), test final
aislado con contador de consultas.

## Fase 5 — ML y mercados adicionales
Factores (value, momentum, quality, low vol, tamaño/liquidez) con datos point-in-time, cointegración y cestas,
modelos de régimen, triple barrera, Isolation Forest/LOF, autoencoders cuando se justifique, futuros con
contratos individuales y rollover, microestructura cuando haya datos de libro. Selección automática de pares con
periodo de formación separado. Nuevos adaptadores (p. ej. Alpha Vantage, Polygon, Databento, Tiingo, FRED, Dukascopy)
según la evaluación de licencias.

## Fase 6 — Riesgo y paper trading
Portfolio Construction, Hierarchical Risk Manager con límites deterministas por mercado/instrumento/estrategia/
portfolio, hard gates B–F, simulación de órdenes y paper trading sin credenciales reales, monitorización de
degradación y alertas. Pantallas Portfolio & Risk y Paper Trading.

## Fase 7 — DRL y ejecución real opcional
Interfaz experimental DRL/LLM-RL fuera del camino crítico. Adaptadores de ejecución en un servicio separado,
deshabilitados por defecto, con autorización explícita (Gate F), límites independientes y pruebas de seguridad.

## Aplicación de escritorio (tras estabilizar el núcleo, en paralelo a las Fases 2–3)
Petición del usuario: una app de escritorio para **seleccionar qué pruebas realizar, ejecutarlas y programarlas**.

Diseño previsto:
- Cliente de escritorio (Tauri o Electron) que empaqueta el frontend React existente y arranca/conecta con la API
  local. Tauri es la opción preferida por tamaño y consumo; Electron si se necesita integración Node.
- Catálogo de pruebas desde la API (`GET /api/v1/detectors`, y en fases siguientes validaciones y backtests) con
  formularios de parámetros, selección de universo y datasets.
- Ejecución inmediata (`POST /scans`, `POST /jobs`) con progreso y cancelación, y programación
  (`/schedules`) con vista de calendario e historial de ejecuciones.
- Notificaciones del sistema al terminar un trabajo o al detectar anomalías que cumplan una consulta guardada.
- En Windows, la app permitirá usar el terminal MetaTrader 5 local como fuente de datos.

Las bases ya existen en la Fase 1: trabajos persistentes, idempotencia, cancelación, programaciones y
descargas automáticas expuestas por la API.

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

## ✅ Fase 2 — Investigación y backtesting
- Contratos de hipótesis congelados y versionados (Pydantic): premisa, anomalías origen, universo, mecanismo,
  estrategia y rejilla de parámetros (≤ 200 variantes), horizonte, benchmark, costes, invalidación, criterios de
  éxito y diseño OOS fijados **antes** de ver resultados. Se rechaza redacción vaga.
- Backtest vectorizado sobre `available_ts` con ejecución a la apertura de la barra siguiente; costes completos
  (comisión, spread, slippage fijo y por rango, préstamo, financiación, funding, rollover) y límite de participación.
- Walk-forward con embargo, K-fold purgado, holdout aislado con contador de accesos.
- DSR (contando todas las pruebas de la familia), PBO (CSCV), bootstrap por bloques, placebo por desplazamiento
  circular, sensibilidad a costes y parámetros, regímenes, controles de leakage.
- Gates A–D por el motor de políticas; máquina de estados con transiciones reservadas a la política.
- Pantallas: Pruebas, Quant Research Lab, Hipótesis y Backtesting & Validation.

Adelantado de la Fase 4: DSR, PBO, bootstrap por bloques y test final aislado ya están en la Fase 2.

## ✅ Aplicación de escritorio (primera versión)
Tauri 2 (`desktop/`) que arranca el backend local, sirve la interfaz y abre en la pantalla **Pruebas**: elegir
detectores o hipótesis, ejecutarlos ahora o programarlos, y ver el historial. Pendiente: empaquetar Python con el
instalador (hoy necesita Python 3.11+ con el backend instalado), firma de código, notificaciones del sistema y
pruebas en Windows/macOS (MetaTrader 5 solo funciona en Windows).

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

# Proveedores de datos

Evaluación orientativa a fecha de la Fase 1. **Las condiciones, límites y precios cambian: verifícalos en la web
de cada proveedor antes de depender de ellos.** El catálogo también está en `GET /api/v1/providers/catalog`.

## Implementados

| Adaptador | Mercados | Frecuencias | Coste | Licencia / uso | Histórico | Retraso / notas |
|---|---|---|---|---|---|---|
| `yahoo` (yfinance) | acciones, ETF, índices, FX, algunos futuros y cripto | 1m–1wk | gratis, no oficial | términos de Yahoo: uso personal, no comercial | diario de décadas; intradía corto | sin deslistados (supervivencia); puede romperse sin aviso |
| `ccxt` | cripto (Binance, Kraken, Coinbase, Bybit, OKX, …) | 1m–1wk | gratis (datos públicos) | términos de cada exchange; restricciones geográficas (p. ej. Binance desde EE. UU.) | desde el listado en cada exchange | volumen de un único exchange; se descarta la vela en formación |
| `metatrader5` | FX, CFD de índices/materias primas/acciones, según bróker | M1–W1 | gratis con cuenta (demo incluida) | datos del bróker | según bróker/terminal | solo Windows; hora del servidor del bróker; volumen de ticks |
| `stooq` | acciones, ETF, índices, FX, futuros | diario, semanal | gratis | uso personal; puede requerir clave o bloquear accesos automatizados | largo | precios ya ajustados |
| `local_file` | cualquiera | cualquiera | — | la del fichero | — | CSV, Parquet, exportaciones MT4/MT5; zona horaria y retraso configurables |
| `synthetic` | — | diario | — | generado | configurable | siempre etiquetado como sintético |

## Candidatos para fases siguientes (no implementados)

| Proveedor | Interés | Observaciones a verificar |
|---|---|---|
| Alpha Vantage | acciones, FX, fundamentales, indicadores macro | clave gratuita con cuota diaria baja; planes de pago |
| FRED (St. Louis Fed) | series macro con fechas de publicación (ALFRED, vintages) | gratis con clave; útil para point-in-time macro |
| Tiingo | acciones/ETF EOD e intradía, noticias | plan gratuito limitado; de pago para uso comercial |
| Polygon.io | acciones, opciones, FX, cripto, ticks | de pago para tiempo real e histórico profundo |
| Databento | futuros, acciones, libro de órdenes (MBO/MBP) | de pago por uso; microestructura de calidad |
| Dukascopy | ticks FX históricos | gratis para descarga; formato propio |
| Binance Data (data.binance.vision) | klines, trades, funding en ficheros diarios | gratis; útil para histórico masivo y funding |
| Calendarios/noticias (p. ej. GDELT, feeds de bancos centrales) | Market Intelligence Agent | necesitan timestamp de publicación y primera disponibilidad |

Criterios para añadir un proveedor: licencia compatible con el uso, cobertura histórica, timestamps fiables
(evento y publicación), tratamiento de deslistados y acciones corporativas, límites de peticiones y coste.

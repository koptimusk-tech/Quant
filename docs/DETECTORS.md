# Detectores y características (Fase 1)

Todos los detectores son deterministas, versionados (`name@version`) y causales: el estadístico en la barra t usa
barras ≤ t y líneas base que terminan en t-1. Los parámetros por defecto se exponen en `GET /api/v1/detectors`.

## Características (features)

| Variable | Definición | Supuestos |
|---|---|---|
| `log_return` | `log(P_t / P_{t-1})` con `P` = cierre ajustado si existe (≥95 % de cobertura), si no cierre bruto | Retornos ajustados son point-in-time (D-005). No se diferencia ni normaliza nada más. |
| `ewma_vol` | Volatilidad EWMA (λ=0.94) desplazada una barra | Solo informativa (`ewma_vol_z`). |
| `log_volume` | `log(volume)` excluyendo barras con volumen 0/ausente | Volumen de tick en FX/MT5, de un exchange en cripto. |
| Índice de retorno total | Composición de retornos ajustados desde la primera barra | Para pares; evita usar niveles ajustados. |
| Estadísticos robustos de ventana | Mediana y MAD·1.4826 de la ventana previa, sin la barra actual | Escala 0 → no se evalúa. |

## return_shock (`extreme_return`)
- Estadístico: z robusto `(r_t − mediana) / (MAD·1.4826)` sobre `window` (250) retornos previos.
- Detección: `|z| ≥ threshold` (5).
- p empírico: `(1 + #{|r_s − med| ≥ |r_t − med|}) / (window + 1)`. Mínimo 1/251.
- Limitaciones: con agrupamiento de volatilidad una ventana tranquila hace parecer extremos movimientos normales
  (se informa `ewma_vol_z` como contraste).

## volume_anomaly (`volume_liquidity`)
- z robusto de `log(volume)` sobre 120 barras previas; `|z| ≥ 4`. Dirección +1 pico, −1 sequía de liquidez.
- No aplicable si menos del 80 % de las barras tienen volumen positivo (FX/índices en muchos proveedores).

## volatility_regime (`volatility_shift`)
- `log2(σ_corto / σ_largo)` con σ_corto en 20 barras hasta t y σ_largo en 250 barras que terminan antes de la
  ventana corta. Detección en el inicio del cruce de `|log2| ≥ 1` (la volatilidad se duplica o se reduce a la
  mitad), con enfriamiento de 20 barras.
- Retraso inherente: un cambio de régimen se detecta tras varias barras de la ventana corta.
- Se informa el p-valor de la prueba F de cociente de varianzas, válido solo bajo normalidad iid (referencia).

## pair_divergence (`cross_asset_divergence`)
- OLS rodante de `log TRI_Y` sobre `log TRI_X` en 120 barras que terminan en t-1; residuo en t fuera de muestra
  dividido por la desviación típica de los residuos de la ventana. Inicio de `|z| ≥ 4`, enfriamiento 20 barras.
- Disponibilidad del par = máximo de las disponibilidades de ambas patas.
- En cada evento: p-valor Engle-Granger en la ventana, correlación de retornos en la ventana y reciente.
- Una correlación alta no implica spread estacionario; si Engle-Granger no apoya cointegración se añade como
  explicación alternativa.

## Pruebas que respaldan los detectores
- Recuperación de anomalías sintéticas inyectadas (`tests/test_detectors.py`).
- Serie de control sin eventos (`SYN-D`) sin retornos extremos.
- No look-ahead: truncado en varios puntos y "futuro envenenado" (`tests/test_no_lookahead.py`).
- Determinismo y reproducción desde el snapshot (`tests/test_ingest_engine.py`, `tests/test_api.py`).

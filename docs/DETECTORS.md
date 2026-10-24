# Detectores y características

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
- p empírico: `(1 + #{|r_s − med| ≥ |r_t − med|}) / (window + 1)`.
- Cola extrapolada (desde v1.1.0, todos los detectores): si la barra supera todo lo visto en la ventana, el p por
  rango quedaría en su suelo `1/(window+1)` y, con BH sobre decenas de miles de pruebas, ningún evento podría salir
  significativo. En ese caso se ajusta una cola de Pareto a los `k = max(10, window/10)` mayores valores de la
  ventana (estimador de Hill) y se usa `(k/(window+1)) · (x/u)^(−1/ξ)`, nunca mayor que el suelo. Supone colas de
  tipo Pareto y es ruidoso con ventanas cortas. Pruebas: calibración en ruido t(4) sin anomalías (tasa de p ≤ α
  ≤ 2α y cero rechazos BH) y un choque extremo inyectado que sí supera BH.
  Consecuencia práctica: con colas gruesas, un salto de ~16 desviaciones en datos horarios (unas 55 000 pruebas)
  sigue sin ser significativo tras BH; lo es un evento muy por encima de lo que la cola explica.
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

## Columnas del Market Scanner y su significado

Los valores guardados en cada escaneo no cambian; las explicaciones, la prioridad y la etapa se calculan al leer
(`qal/anomaly/explain.py`, `qal/anomaly/priority.py`), así que no hace falta migrar la base de datos.

| Columna | Fórmula | Rango | Más alto = | Concepto |
|---|---|---|---|---|
| STATISTIC (Deviation) | z robusto (retornos, volumen), log2(vol corta / vol larga) o z del residuo del par | real | más raro | Rareza |
| STAT. SCORE | \|s\| / (\|s\| + umbral) | [0,5, 1) en eventos guardados | más raro | Rareza (orden, no probabilidad) |
| ADJ. P | p por rango en la ventana previa (cola de Pareto más allá del máximo) + Benjamini-Hochberg sobre todas las pruebas del escaneo | (0, 1] | **menos** evidencia | Significación |
| ECON. SCORE | ratio = movimiento / (coste supuesto × patas); ratio / (ratio + 3), ×0,5 con poca liquidez | [0, 1) | movimiento mayor frente al coste | Relevancia económica preliminar |
| STATUS | `DISCOVERED`; `HYPOTHESIS_FORMULATED` cuando una hipótesis cita la anomalía | — | — | Registro |
| INVESTIGATION PRIORITY | ver abajo | [0, 1) | investigar antes | Prioridad de investigación |
| Stage | a partir de las hipótesis que citan la anomalía | detección → investigación → validación OOS → paper trading | — | Etapa |

Significación (indicador visual): fuerte < 0,01, significativa < 0,05, débil < 0,10, no significativa el resto.
Un ADJ. P cercano a 1 es ausencia de evidencia, no una señal fuerte.

**ECON. SCORE no es interpretable en todos los detectores.** El "movimiento" es el retorno absoluto de la barra en
`return_shock` y `volume_anomaly`, pero en `volatility_regime` es la volatilidad por barra de la ventana corta (un
nivel, no un movimiento del evento) y en `pair_divergence` el spread en log. Con un coste FX de 3 pb casi cualquier
cambio de volatilidad saca 0,8 o más, incluso si la volatilidad baja. La API lo marca con `interpretable: false` y la
prioridad lo cuenta a la mitad. Faltan, para validar relevancia económica: retornos posteriores, spread y slippage
reales en el momento del evento, capacidad y confirmación fuera de muestra.

### Prioridad de investigación (`qal/anomaly/priority.py`)

```
base      = 0,35 E + 0,30 P + 0,20 C + 0,15 R
prioridad = base × Q × D × A   (tope 0,49 sin ADJ. P < 0,05 ni apoyo fuera de muestra)
```

| Componente | Cálculo |
|---|---|
| E evidencia | min(1, −log10(ADJ. P) / 3); 0 con p = 1 o nulo |
| P predictiva | estudio de eventos: apoyado OOS 1; solo en muestra 0,4; sin evaluar 0,2; muestra insuficiente 0,1; sin evidencia 0 |
| C económica | ECON. SCORE, a la mitad si no es interpretable para el detector |
| R rareza | (STAT. SCORE − 0,5) / 0,5 |
| Q datos | pass 1, warn/desconocido 0,8, fail 0; ×0,5 si son sintéticos |
| D duplicidad | 0,6 si hay un evento más fuerte del mismo detector y dataset a ≤ 10 barras en el mismo escaneo |
| A vigencia | 0,5 + 0,5 · 2^(−antigüedad / 500 barras), antigüedad hasta el final del dataset |

Niveles: alta ≥ 0,50, media ≥ 0,25, baja. Pesos y umbrales en `PriorityConfig` (`GET /anomalies/priority-config`).
Cada anomalía devuelve sus componentes, contribuciones, multiplicadores y una frase con el motivo del orden. Es una
prioridad de investigación, no de ejecución. **Los pesos son heurísticos: no se han calibrado con rendimientos
futuros.** Rareza (R), significación (E), relevancia económica (C), capacidad predictiva (P) y rentabilidad neta de
una estrategia validada son cosas distintas y se informan por separado. Otras limitaciones: la
antigüedad se aproxima por la fracción del rango temporal del dataset; la duplicidad solo mira el mismo detector.

### Evaluación predictiva (`qal/anomaly/event_study.py`, v2)

`POST /event-studies {dataset_id, detector, related_dataset_id?, horizons?, seed?}` (botón en el detalle) y, para un
lote reproducible sobre datos reales, `python -m qal.cli predictive-study` (ver más abajo).

1. Vuelve a ejecutar el detector (causal) con los parámetros del último escaneo.
2. Entrada en la apertura de la primera barra que empieza en o después de `available_ts`; salida en el cierre de la
   barra h. Horizontes 1, 5, 10 y 20 barras en todas las frecuencias. Reglas continuación y reversión según el signo
   del retorno de la barra del evento.
3. **Benchmark causal.** Exceso = retorno futuro − retorno incondicional medio del mismo horizonte calculado solo con
   la historia anterior a la entrada (media expansiva, mínimo 100 barras; sin ella el evento no cuenta). v1 no tenía
   benchmark y la primera versión de v2 usaba la media de toda la muestra (look-ahead); ambos corregidos.
4. **Evidencia predictiva separada de la rentabilidad.** Selección, p y veredicto usan el exceso, no el neto.
   Los costes se informan aparte en escenarios: `zero_cost` (referencia), `low_estimate` (×0,5), `base_estimate`
   (modelo por clase de activo, `research/costs.py`), `high_estimate` (×2) y, si se aporta y se alinea,
   `historical_spread` (spread de barra de MT5 en la barra previa a la entrada y a la salida, más comisión y
   slippage). Los escenarios estimados son estimaciones, no costes del bróker; sin swap/financiación.
5. Solo eventos independientes (sin solapamiento). Media bruta, exceso, IC 95 % por bootstrap (semilla fija y
   distinta por fila: añadir o quitar horizontes no cambia los IC de las demás filas), tasa de aciertos, peor
   operación, drawdown máximo y neto por escenario.
6. Separación temporal estricta: en muestra = operaciones cerradas antes del corte (70 % de los eventos o un corte
   prerregistrado `split_ts`); fuera de muestra = eventos en o después del corte; las que cruzan el corte se purgan.
   BH sobre todas las combinaciones horizonte × regla en muestra; se elige la de mayor t del exceso en muestra (con
   ≥ 10 eventos) y solo esa se mide fuera de muestra. Walk-forward real: en cada pliegue (2–5) la regla se elige solo
   con los pliegues anteriores.
7. Veredicto: `insufficient_sample` (< 30 eventos independientes o < 10 fuera de muestra; no se relajan criterios),
   `no_evidence`, `in_sample_only` u `oos_supported` (exceso OOS > 0 con IC por encima de 0 y p ajustado en muestra
   < 0,05). `survives_costs_oos` indica por escenario si el neto OOS tiene IC por encima de 0. Ninguno equivale a
   una estrategia validada: eso exige una hipótesis registrada y las puertas A–D.
8. Pares (`pair_divergence`, requiere `related_dataset_id`): posición en el spread largo Y, corto β·X con β del
   momento del evento; se evalúa el comportamiento posterior de la relación (reversión o continuación), nunca el
   nivel del spread como beneficio, con costes de las dos patas (Y + |β|·X). No se mezclan sintéticos y reales.
9. `volatility_regime` se evalúa con retornos posteriores, nunca con el nivel de volatilidad.

Los estudios se guardan como experimentos `event_study` (los repetidos con los mismos datos y configuración se
reutilizan) y cuentan cuántos se han hecho ya sobre el dataset. Las puntuaciones persistidas (STAT., ECON., ADJ. P)
no cambian; la prioridad sigue leyendo el último veredicto.

**Lote reproducible** (`qal/research/predictive.py`):

```
python -m qal.cli predictive-study --out predictive_runs [--symbols EURUSD,USDCHF] [--frequencies 1d,1h]
    [--spread-mt5 --mt5-offset-hours 2] [--spread-csv EURUSD:1h:0.00001:C:\ruta\EURUSD_H1.csv]
```

Selecciona solo datasets reales (los sintéticos se ignoran y se cuentan), audita cada uno (filas, periodo, paso,
duplicados, huecos, OHLC, barras sin rango, retornos extremos, barras de fin de semana, retraso de disponibilidad,
volumen y su tipo, spread), informa de los que faltan con la exportación de MT5 necesaria, ejecuta los detectores por
separado (volumen solo si es utilizable; par solo con ≥ 500 barras sincronizadas), aplica BH otra vez sobre todas las
combinaciones del lote y sobre las p fuera de muestra, y escribe `results.json`, `comparison.csv`, `data_audit.json` y
`report.md` con versión de código, semilla, configuración y hashes de datos. El spread histórico solo se usa si cubre
≥ 50 % de las barras y sus cierres coinciden con los del dataset en el mismo instante (si no, suele ser un desfase
horario y se rechaza).

## Modelos multivariantes (Fase 5): `isolation_forest`, `local_outlier_factor` (`multivariate_outlier`)

Código: `qal/anomaly/ml.py`, características en `qal/features/catalog.py` (catálogo documentado, también en
`GET /api/v1/models/features` y en la página Model Lab).

**Características.** Cada valor en la barra t usa solo barras ≤ t. Las ventanas móviles exigen el 80 % de sus
observaciones, de modo que un hueco aislado de precios deja NaN solo en las filas afectadas. Por defecto:
`ret_1, ret_5, rel_vol, ret_robust_z, range_rel, gap, kurt_60, autocorr_60`. `rel_volume` se descarta (con el motivo)
si el volumen cubre menos del 80 % de las barras o es constante, como ocurre con el volumen de ticks de MT5; las
variables de hora solo se usan en barras intradía. No se aplican transformaciones de estacionariedad indiscriminadas:
se usan retornos, ratios y z-scores frente al pasado reciente.

**Ajuste walk-forward.** En cada punto de reajuste t0 (cada `refit_every` barras; 0 = `max(25, train_window/20)`,
nunca dependiente de la longitud de la serie) se estiman con las `train_window` barras anteriores: la escala robusta
(mediana/MAD), el modelo (con los dos primeros tercios de la ventana) y la distribución de referencia de las
puntuaciones (con el último tercio, que el modelo no ha visto). Las `refit_every` barras siguientes se puntúan con
ese modelo. Las pruebas de truncado y de futuro envenenado lo verifican.

**La puntuación no es una probabilidad.** Cada barra recibe un p-valor empírico frente a la referencia:
`(1 + #{ref > s} + #{ref = s}/2) / (n_ref + 1)`; se marca si p ≤ `contamination` (0,01 por defecto), así que
`contamination` es la proporción esperada de barras marcadas en un mercado estable. Los p-valores entran en la
corrección Benjamini-Hochberg del escaneo como los demás. Cada detección indica las tres características más
extremas (z robusto) y la dirección es el signo del retorno de la barra: el modelo dice que la barra es rara, no hacia
dónde irá el precio.

**Calibración medida** (series de control sintéticas, 3000 barras, 4 semillas): Isolation Forest marca un 1,1–1,2 %
con un nominal del 1 %; LOF marca un 2 % aproximadamente (su estimación de densidad deriva con las características
móviles). Un evento produce varias barras marcadas seguidas porque las características de 60 y 250 barras conservan
su rastro: el Model Lab cuenta episodios (marcas separadas por 5 barras o menos).

**LOF frente a IF.** LOF mide la densidad local respecto a los vecinos de entrenamiento (`n_neighbors` 35, modo
novelty); puede aportar cuando la anomalía es una combinación rara en una zona poco poblada, no un valor extremo. Las
distancias pierden contraste al crecer el número de características, así que conviene un conjunto pequeño.

Están desactivados por defecto en el escaneo de la página Tests (son más lentos); se comparan en el Model Lab.

## Model Lab (`qal/research/model_lab.py`)

Compara detectores sobre los mismos datasets y guarda un experimento `model_comparison`. Por dataset × modelo:
barras marcadas, episodios, tasa frente a la nominal, barras significativas tras BH, tasa por año (y su coeficiente
de variación) y por régimen de volatilidad causal (`rel_vol` > 0), estabilidad entre semillas (solo si el modelo es
estocástico) y con ventanas de entrenamiento más cortas y más largas (solapamiento con tolerancia de ±1 barra),
matriz de coincidencia entre modelos, tiempo de cálculo y la evaluación predictiva del estudio de eventos
(separación cronológica, walk-forward, referencia causal, costes estimados) con BH sobre todas las pruebas fuera de
muestra de la comparación. Los retornos futuros solo se usan ahí, como objetivo de evaluación.

Rareza y capacidad predictiva van en columnas separadas y no hay puntuación agregada. El benchmark sintético mide
recuperación de eventos inyectados y falsas alarmas en la serie de control; no es evidencia sobre mercados reales.
Límites: 20 datasets, 8 modelos, las últimas 200.000 barras por dataset, presupuesto de tiempo y cancelación
comprobados entre unidades de trabajo.

## Pruebas que respaldan los detectores
- Recuperación de anomalías sintéticas inyectadas (`tests/test_detectors.py`).
- Serie de control sin eventos (`SYN-D`) sin retornos extremos.
- No look-ahead: truncado en varios puntos y "futuro envenenado" (`tests/test_no_lookahead.py`).
- Determinismo y reproducción desde el snapshot (`tests/test_ingest_engine.py`, `tests/test_api.py`).
- Explicaciones, prioridad y estudio de eventos (`tests/test_explain_priority.py`): niveles de significación con p
  nulo, NaN o 1; el ejemplo de USDCHF desglosado; un ECON. SCORE alto sin evidencia no queda arriba; tope sin
  significación; multiplicadores; ventaja de continuación conocida detectada fuera de muestra; el movimiento de la
  propia barra del evento no se captura; el retraso de disponibilidad mueve la entrada; muestras insuficientes.
- Modelos multivariantes (`tests/test_ml_detectors.py`, `tests/test_features.py`): recuperación del choque y del
  régimen de volatilidad inyectados, tasa de falsas alarmas en series de control, truncado y futuro envenenado,
  reproducibilidad por semilla, errores de configuración, datos ausentes, cambio de frecuencia (variables de hora) y
  catálogo documentado.
- Model Lab (`tests/test_model_lab.py`): medidas de solapamiento y episodios, benchmark sintético, API, caché,
  exportación CSV, cancelación cooperativa de un trabajo en ejecución y presupuesto de tiempo.

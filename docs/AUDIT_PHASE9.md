# Fase 9 — Auditoría end-to-end, validación científica y preparación

Fecha: 2026-10-25. Rama `claude/phase9-audit`, base `f1867f1` (fin de la Fase 7). Solo investigación, simulación y
paper trading: ninguna parte del sistema envía órdenes reales ni tiene credenciales de trading.

## 1. Resumen

- El sistema está conectado de punta a punta salvo la **monitorización de modelos, deriva y circuit breakers
  (Fase 8), que no existe**. El nuevo comando `qal.cli e2e-audit` recorre datos → calidad → anomalía → estudio de
  eventos → hipótesis → validación → elegibilidad → cartera → riesgo → paper → monitorización → alerta con los
  mismos servicios que la API.
- Con los datos de prueba, **la cadena validada se detiene en la validación**: ninguna hipótesis pasa los gates
  (por ejemplo, un momentum con Sharpe OOS 1,66 se rechaza por tener solo 8 operaciones y una PBO de 0,94). Cartera,
  riesgo y paper se ejercitan después con estrategias demo etiquetadas, y el paper rechaza la hipótesis no validada.
- No se ha encontrado fuga de información futura en los caminos críticos. Se añadieron pruebas automáticas para los
  que no tenían: elección walk-forward por ventana, aislamiento del holdout y decisiones de paper con futuro
  envenenado.
- Se corrigieron 7 defectos (D1–D7), cada uno con una prueba de regresión que falla sin la corrección.
- No hay evidencia económica validada: **0 estrategias validadas**. En fases anteriores las 24 hipótesis sobre
  datos reales fallaron la validación; aquí no se pudo repetir con datos reales porque el entorno de la nube no
  llega a Yahoo ni a Stooq.
- Hay un hallazgo estadístico que necesita una decisión: la familia de Benjamini-Hochberg del escáner (todas las
  barras × detectores × instrumentos) es tan grande que ni un choque inyectado de 9,9 desviaciones sale
  significativo (p ajustado 0,21).

## 2. Matriz de trazabilidad (fases 1–8)

Estados: implementado, parcial, ausente o no verificable.

| Fase · requisito | Componente | Ficheros | Estado real | Pruebas que lo demuestran | Riesgos y acciones |
|---|---|---|---|---|---|
| 1 · Ingesta y normalización | Adaptadores, `normalize_bars`, tres relojes | `qal/data/adapters/*`, `schema.py`, `ingest.py` | Implementado | `test_adapters`, `test_ingest_engine` (revisiones, incremental), `test_audit_phase9` (barra abierta) | Yahoo y Stooq no se pudieron probar desde la nube (2 pruebas de red sin ejecutar). D7 corregido. |
| 1 · Calidad (Gate A) | `check_bars`, eventos de calidad | `quality.py` | Implementado | `test_ingest_engine::test_gate_a_blocks_failed_dataset` | `fail` bloquea todos los usos posteriores. |
| 1 · Persistencia | Parquet con hash de contenido y SQLAlchemy | `store.py`, `models.py` | Implementado | `test_migrations` (ORM = Alembic 0001–0005) | Ningún cambio de esquema en esta fase. |
| 1 · Detección de anomalías | 4 detectores univariantes y de pares | `anomaly/detectors.py` | Implementado | `test_detectors`, `test_no_lookahead` (truncado y futuro envenenado) | D5 corregido; p no calibrados bajo dependencia. |
| 1 · Puntuaciones | STAT, ADJ. P, ECON | `engine.py`, `explain.py`, `priority.py` | Implementado | `test_explain_priority`, `test_audit_phase9` | D1–D3 corregidos. Las puntuaciones no son probabilidades. |
| 1 · Pruebas múltiples | BH por escaneo y por estudio, corrección global en lotes | `engine.benjamini_hochberg`, `event_study`, `predictive` | Implementado | `test_event_study`, `test_ingest_engine` | F3: familia del escáner demasiado amplia. |
| 2 · Contratos de hipótesis | Contrato congelado con hash | `research/contract.py` | Implementado | `test_research` | Exige `embargo ≥ horizonte`. |
| 2 · Backtest | Ejecución en la apertura siguiente, costes, capacidad | `research/backtest.py` | Implementado | `test_backtest` | Costes configurados (estimados). |
| 2 · Validación y gates A–D | Walk-forward, DSR, PBO, placebo, fugas, holdout | `validation.py`, `policy.py`, `service.py` | Implementado | `test_research`, `test_backtest`, `test_audit_phase9` | F11: la DSR no cuenta las pruebas de cribado. |
| 3 · Agentes de investigación | 4 agentes deterministas | `alpha/agents.py` | Implementado (sin LLM) | `test_alpha` | No se llama a ningún modelo de lenguaje. |
| 4 · Estudios de eventos | Entrada tras `available_ts`, coste por escenario, walk-forward | `anomaly/event_study.py`, `research/predictive.py` | Implementado | `test_event_study` | Volatilidad y volumen se evalúan por retornos posteriores (F13). |
| 5 · Modelos ML | Isolation Forest y LOF walk-forward, Model Lab | `anomaly/ml.py`, `research/model_lab.py` | Implementado | `test_ml_detectors` (truncado, futuro envenenado), `test_model_lab` | Sin mejora medida frente a los detectores simples. |
| 5 · Regímenes, cointegración dinámica, triple barrera, autoencoder | — | — | Ausente | — | No se construyeron. |
| 6 · Construcción de cartera | Métodos, covarianzas, backtest de cartera | `portfolio/*` | Implementado | `test_portfolio` | Solo probado con estrategias sintéticas. |
| 6 · Gestor de riesgo jerárquico | `PolicyLimits`, decisiones registradas | `portfolio/risk_manager.py` | Implementado | `test_portfolio` | F9: semántica de drawdown distinta del paper. |
| 6 · Estrés | Escenarios y Monte Carlo | `portfolio/stress.py` | Implementado | `test_portfolio::test_stress_scenarios…` | Escenarios, no predicciones. |
| 7 · Paper trading | Motor, órdenes, ejecución, contabilidad, gate de riesgo, reconciliación, evaluación | `paper/*` | Implementado | `test_paper` (23), `test_audit_phase9` | D4 corregido. F5: los límites aprobados al promover no se aplican. |
| 7 · Frontend y API | React, FastAPI, Playwright | `frontend/*`, `qal/api/*` | Implementado | Vitest (20), Playwright (10 specs en CI) | — |
| 8 · Monitorización, deriva, circuit breakers | — | — | **Ausente** | — | El brief de la Fase 8 quedó incompleto. Solo existen las pausas automáticas, el kill switch y los eventos del paper. |
| Despliegue | Docker, escritorio (Tauri), CI | `docker-compose.yml`, `desktop/`, `.github/` | Implementado | CI: backend, frontend, docker, e2e | — |

## 3. Hallazgos

Estado: corregido (con prueba de regresión) o abierto.

### Altos
- **D1 (corregido)**: `volatility_regime` recibía un ECON. SCORE calculado con el nivel de volatilidad como si fuese
  un retorno negociable. Seguía contando en la prioridad, aunque a la mitad. Ahora no tiene score (método
  `move_vs_cost@2`) y la prioridad ignora los scores antiguos guardados.
- **D2 (corregido)**: `volume_anomaly` se puntuaba con el retorno de la barra, una variable distinta de la detectada.
  Mismo tratamiento que D1.
- **F3 (abierto, decisión)**: la familia de BH del escáner incluye cada barra evaluada de cada detector e
  instrumento (15 696 pruebas en el E2E; 108 790 en un escaneo real anterior). El p por rango tiene un suelo de
  1/(ventana+1) y la cola extrapolada no baja de unos 1e-5, así que en la práctica nada sale significativo. En el E2E
  un choque inyectado con z = −9,9 tiene p = 4,3e-5 y p ajustado = 0,21. Consecuencia: el vínculo anomalía →
  hipótesis por la regla de eventos no se activa, y las propuestas salen de reglas de momentum y pares.
  Recomendación: definir la familia por detector e instrumento sobre un periodo de vigilancia preregistrado, sin
  quitar la corrección global.
- **F4 (abierto)**: la Fase 8 no existe.
- **F5 (abierto)**: los límites que fija una persona al promover a `PAPER_TRADING_ELIGIBLE` (exposición bruta, stop
  por drawdown, pérdida diaria) solo quedan como texto en el registro de auditoría. El paper aplica la política del
  gestor de riesgo, pero no esos límites por estrategia.

### Medios
- **D3 (corregido)**: Isolation Forest y LOF se puntuaban con el retorno de la barra. Ahora no tienen score.
- **D4 (corregido)**: el gate de riesgo del paper no aplicaba `max_market_gross` (exposición por clase de activo),
  que el backtest de cartera sí aplica.
- **D5 (corregido)**: en `volatility_regime` y `pair_divergence` las barras de calentamiento contaban como ceros en
  la ventana de referencia del p por rango. Los p de las ~270 barras siguientes salían demasiado pequeños. Los
  detectores pasan a la versión 1.2.0, así que los experimentos antiguos quedan intactos y separados en la caché.
- **D7 (corregido)**: una barra que aún se estaba formando al descargar se guardaba como cerrada. Ahora se descarta
  (con un evento de calidad) y la siguiente ingesta la trae completa. No se aplica a datos sintéticos.
- **F9 (abierto)**: el drawdown y la pérdida diaria actúan distinto en cada sitio. El backtest de cartera reduce a la
  mitad o cierra durante `cooldown_days` y después reinicia la referencia; el paper rechaza las órdenes que añaden
  riesgo y no reinicia nunca. Además, los límites de construcción (reparto de riesgo, volatilidad ex ante, peso por
  grupo, drawdown e historial por estrategia, contraparte) solo se aplican al construir la cartera, no orden a orden.
- **F10 (abierto)**: los escenarios de coste no son homogéneos. El estudio de eventos usa ×0,5, ×1 y ×2; la
  validación usa ×0 a ×3 con el gate en ×2; el paper usa ×1, ×2 y ×4. Todos los costes son estimaciones
  configuradas; solo el diferencial histórico de MT5 es observado.
- **F11 (abierto)**: `prior_trials` cuenta las variantes validadas de la familia de hipótesis, pero no las pruebas
  del escáner, los estudios de eventos ni las propuestas cribadas. La DSR es optimista cuando la hipótesis sale de un
  cribado.
- **F12 (abierto, por diseño)**: `VALIDATION_PASSED` basta para entrar en paper, sin holdout ni aprobación humana. El
  paper es una simulación, pero conviene exigir `PAPER_TRADING_ELIGIBLE` o mostrarlo claramente.
- **F13 (abierto)**: el estudio de eventos de volatilidad y volumen mide la dirección de los retornos posteriores
  (con el signo de la barra del evento). Es una pregunta económica válida, pero no prueba la persistencia de la
  volatilidad.

### Bajos
- F14: línea muerta en `Validator.final_test` (se calcula una máscara y se sobrescribe).
- F15: los p por rango suponen intercambiabilidad. El agrupamiento de volatilidad y el solapamiento de ventanas los
  vuelven optimistas; BH es válido bajo dependencia positiva (PRDS), que aquí no está garantizada.
- F16: `ruff` reporta avisos de estilo previos y la CI no ejecuta lint.
- F17: no fue posible ejecutar con datos reales desde la nube.

No se ha encontrado ningún hallazgo crítico (fuga de futuro u orden real).

## 4. Integridad temporal

| Punto | Cómo se garantiza | Prueba |
|---|---|---|
| Observación, disponibilidad y decisión | `event_ts` = fin de barra; `available_ts` = `event_ts` + retraso; el paper decide con `available_ts ≤ reloj` | `test_paper` (sin relleno en la barra de decisión), `test_audit_phase9::test_paper_signals…` |
| Ejecución simulada | Apertura de la barra siguiente; el stop-limit disparado intrabarra espera a la siguiente | `test_backtest::test_execution_happens_next_open…`, `test_paper` |
| Escalado | Mediana y MAD solo de la ventana de entrenamiento en cada reentreno (ML) | `test_ml_detectors::test_truncation_invariance`, `test_poisoned_future…` |
| Ventanas de los detectores | Línea base que termina en t−1 | `test_no_lookahead` |
| Cointegración y regresión de pares | OLS rodante desplazado una barra; Engle-Granger en la ventana previa | `test_no_lookahead::test_truncation_invariance_pair` |
| Selección de variante y horizonte | Por ventana, solo con sus barras de entrenamiento; horizonte × regla elegido en muestra | `test_audit_phase9::test_walk_forward_choice…`, `test_event_study::test_in_sample_trades_are_purged…` |
| Hiperparámetros y costes | Rejilla y costes fijados en el contrato congelado | `test_research` (hash del contrato) |
| Reentreno walk-forward, purga y embargo | `embargo ≥ horizonte` exigido; `purged_kfold` | `test_backtest::test_walk_forward_windows_respect_embargo`, `test_purged_kfold…` |
| Separación train / validación / test | Holdout por `available_ts`; contador de accesos | `test_audit_phase9::test_holdout_bars_never_reach_validation`, `test_research` |
| Barras incompletas | Descartadas al ingerir (D7) | `test_audit_phase9::test_bar_still_forming…` |
| Datos revisados | Detectados y marcados en la fusión incremental | `test_ingest_engine::test_incremental_ingest…` |
| Triple barrera | No existe | — |

Riesgos que no se pueden descartar:
- Los proveedores pueden reescribir el histórico sin avisar entre ingestas.
- El retraso de disponibilidad por defecto es una suposición por proveedor.
- Los precios ajustados de Yahoo incorporan dividendos conocidos después; se mitiga con el índice de retorno total.
- La selección humana fuera del sistema (mirar el holdout en otra herramienta) no se puede detectar.

## 5. Calidad estadística

| Detector | Estadístico | Ventana | Umbral | Supuestos | Extremos | Datos faltantes | Autocorrelación y solapamiento |
|---|---|---|---|---|---|---|---|
| return_shock 1.1.0 | z robusto (mediana, MAD) del log retorno | 250 previas | \|z\| ≥ 5 | Intercambiabilidad en la ventana | MAD robusta; cola de Pareto por encima del máximo | Se salta la barra; escala 0 → no se evalúa | El agrupamiento de volatilidad vuelve el p optimista |
| volume_anomaly 1.1.0 | z robusto del log volumen | 120 | \|z\| ≥ 4 | Volumen comparable (tick en FX) | Robusto | No aplicable con menos del 80 % de cobertura | Estacionalidad intradía no modelada |
| volatility_regime 1.2.0 | log2(σ 20 / σ 250 previa) | 20/250; rango 500 | \|log2\| ≥ 1, solo el inicio | Rango entre valores finitos (D5) | — | Se salta la barra | Ventanas solapadas: valores consecutivos muy dependientes |
| pair_divergence 1.2.0 | z del residuo OLS fuera de muestra | 120; rango 250 | \|z\| ≥ 4 | Relación lineal estable | — | Solo barras comunes | Engle-Granger informativo, no condición |
| isolation_forest y LOF 1.0.0 | p empírico frente al tercio final retenido del entrenamiento | 1500, reentreno cada 75 | p ≤ contaminación | Estabilidad del entrenamiento | Robusto | Filas incompletas excluidas | Rachas tras un evento: se cuentan episodios |

Las puntuaciones STAT y ECON son transformaciones monótonas para ordenar, no probabilidades. La
API y la documentación lo dicen. Un p ajustado de BH no es la probabilidad de que una anomalía sea real ni rentable.

Sobreajuste en el E2E:
- Número de variantes registrado por hipótesis: 2, 3 y 3, que se acumulan en `prior_trials`.
- DSR de 0,94, 0,98 y 0,19.
- PBO de 0,34, 0,94 y 0,71.
- Concentración: las 5 mejores operaciones son el 81 % y el 110 % del P&L neto en las dos hipótesis con Sharpe
  positivo, que quedan bloqueadas por el Gate C.
- La estabilidad de parámetros se mide como la fracción de variantes positivas: solo el 33 % en la tercera.
- La DSR y la PBO solo se calculan con más de una variante; con una sola, la PBO no se calcula.

## 6. Economía y ejecución

- **Costes**:
  - Configurados: comisión, medio diferencial, deslizamiento fijo, una fracción del rango de la barra anterior,
    préstamo para cortos y financiación.
  - Observados: solo el diferencial histórico de MT5, si se aporta.
  - No modelados: el impacto no lineal y la latencia real; el paper usa barras de retraso configurables.
- **Unidades**: puntos básicos sobre el nocional; los contratos con multiplicador, lote y paso se toman de los
  metadatos del instrumento (`portfolio/sizing.py`).
- **Divisas**: el P&L en divisa cotizada se convierte a la divisa de la cuenta con el precio del par disponible. Si
  no hay conversión posible, la sesión no se crea.
- **Convenciones**:
  - Cripto spot: sin cortos y sin apalancamiento.
  - Futuros: margen por contrato.
  - FX: tamaño de lote y financiación diaria.
- **Escenarios**: el paper usa base, adversa y severa (×1, ×2 y ×4); la validación prueba de ×0 a ×3 y el Gate D
  exige media neta positiva a ×2.
- **E2E**: la reversión de pares tiene una media neta OOS de 6,2e-5 por barra a ×1, 1,2e-5 a ×2 y −3,9e-5 a ×3. No es
  robusta a costes moderados.
- **Volatilidad y volumen**: ya no se evalúan con move/coste (D1 y D2). Su relevancia económica solo puede venir
  del estudio de eventos de los retornos posteriores.

## 7. Validación de anomalías e hipótesis (E2E sintético)

| Nivel | Resultado |
|---|---|
| 1. Estadísticamente inusual | 45 detecciones; 0 con BH < 0,05 (F3) |
| 2. Asociación posterior reproducible | Los 4 estudios de eventos dan muestra insuficiente |
| 3. Evidencia OOS | Ninguna |
| 4. Interesante en neto | Ninguna |
| 5. Validada para paper | Ninguna; 3 de 3 hipótesis con `VALIDATION_FAILED` |

En los cinco niveles el veredicto es **evidencia insuficiente**. En datos reales (fases anteriores, no repetido
aquí), 24 de 24 hipótesis dieron `VALIDATION_FAILED`.

## 8. Modelos y agentes

- **Modelos ML**: Isolation Forest y LOF tienen semilla y versión, se reentrenan walk-forward, tienen un benchmark
  sintético de recall y falsas alarmas, y se evalúan OOS con el estudio de eventos. No hay modelos supervisados. No
  hay una mejora medida frente a `return_shock` y `volatility_regime` en datos reales, así que su valor está **sin
  validar**.
- **Agentes**: los cuatro son reglas deterministas; no se llama a ningún LLM. Proponen contratos; el cálculo lo hacen
  `run_validation` y el motor de políticas. Cada decisión queda registrada con su motivo. No pueden crear políticas
  de riesgo (`actor_kind` distinto de `human` → error) ni promover estrategias.

## 9. Cartera y riesgo

- **Mismo origen de límites**: el backtest y el paper usan los mismos `PolicyLimits` de la política activa.
- **Límites aplicados en los dos**: exposición bruta y neta, exposición por instrumento, por mercado (D4) y por
  divisa, apalancamiento, liquidez, drawdown y pérdida diaria (con semántica distinta, F9), y calidad de datos.
- **Solo en el paper**: margen, lote mínimo, cortos en spot y kill switch.
- **Solo al construir la cartera**: reparto del riesgo, volatilidad ex ante, límites de grupo, contraparte y fallos de
  ejecución.
- **E2E**: 3 093 decisiones del gestor de riesgo registradas, de las cuales 309 son bloqueos.
- **Estrés**: hay escenarios de choque, costes ×2/×3 y Monte Carlo (`portfolio/stress.py`). La pérdida de datos pausa
  la sesión automáticamente y el kill switch impide órdenes nuevas (`test_paper`).
- **Pausas y reanudaciones**: quedan auditadas en `paper_events` y en el registro de auditoría.

## 10. Recorrido E2E

```
cd backend
python -m qal.cli e2e-audit --out e2e_runs                       # sintético, determinista
python -m qal.cli e2e-audit --adapter yahoo --symbols SPY,QQQ,GLD,TLT --asset-class etf
```

Resultado sintético (semilla 7, 4 × 1500 barras):

| Etapa | Estado |
|---|---|
| Datos | ok |
| Calidad | ok |
| Anomalía | ok |
| Estudio de eventos | ok |
| Hipótesis | ok |
| Validación | **blocked** |
| Estrategia | blocked |
| Cartera (demo) | ok |
| Riesgo | ok |
| Paper (demo) | ok, coherente con el backtest (correlación 0,98) |
| Monitorización | **not_implemented** |
| Alerta | ok (solo eventos del paper) |

La prueba `test_end_to_end_chain…` lo ejecuta en cada CI.

## 11. Matriz de preparación

| Área | Estado | Evidencia |
|---|---|---|
| Calidad de datos | PASS_WITH_LIMITATIONS | Gate A, revisiones, barras abiertas (D7); proveedores reales no reprobados en esta fase |
| Detección estadística | PASS_WITH_LIMITATIONS | Causal y probada; p no calibrados bajo dependencia; BH sin potencia (F3) |
| Evidencia económica | NOT_VALIDATED | Ninguna anomalía con evidencia OOS neta; costes estimados |
| Validación de estrategias | PASS_WITH_LIMITATIONS (el motor) | Gates y fugas probados; 0 estrategias validadas; DSR sin las pruebas de cribado (F11) |
| Modelos ML | NOT_VALIDATED | Causales y reproducibles; sin mejora medida frente a la línea base |
| Construcción de cartera | PASS_WITH_LIMITATIONS | Solo con estrategias sintéticas |
| Controles de riesgo | PASS_WITH_LIMITATIONS | Gestor de riesgo y gate del paper; F5 y F9 abiertos |
| Paper trading | PASS_WITH_LIMITATIONS | Reproducible y reconciliado; solo demo y sintético; replay, no tiempo real |
| Monitorización | NOT_IMPLEMENTED | Fase 8 ausente |
| Reproducibilidad y operación | PASS_WITH_LIMITATIONS | Hashes, caché, semillas, CI; sin repetición con datos reales |

**Listo para seguir en paper trading**: el simulador sí, con estrategias demo o con la primera hipótesis que pase
los gates. **Ninguna estrategia** está lista: no hay ninguna validada.

## 12. Pruebas ejecutadas

| Suite | Antes | Después |
|---|---|---|
| Backend `pytest -q` | 237 pasan, 2 sin ejecutar (marcadas `network`) | 249 pasan, 2 sin ejecutar (`network`) |
| Frontend `tsc` y `vitest` | 20 pasan | 20 pasan (sin cambios) |
| Playwright e2e | 10 specs en CI | En CI |

Las 2 pruebas `network` (Yahoo y Stooq reales) no se ejecutaron: no es que fallaran. Las pruebas de regresión de D1–D5
y D7 se comprobaron en rojo sin la corrección y en verde con ella.

## 13. Recomendaciones priorizadas

1. Decidir la familia de pruebas múltiples del escáner (F3). Sin esto, la significación no informa.
2. Construir la Fase 8 mínima: deriva de la distribución de las variables y de los p del detector, degradación del
   Sharpe en paper frente a la validación, y circuit breakers que pausen la sesión.
3. Guardar y aplicar en el paper los límites aprobados al promover una estrategia (F5).
4. Unificar la semántica de drawdown entre el backtest y el paper (F9) y los multiplicadores de coste (F10).
5. Contar las pruebas de cribado en los ensayos de la DSR (F11).
6. Repetir el E2E con datos reales en el PC (comando de la sección 10).

## 14. Instrucciones en Windows

```powershell
cd C:\Apps\Quant
git pull
.\.venv\Scripts\pip install -e ".\backend[yahoo,crypto,metatrader,dev]"
cd backend
..\.venv\Scripts\python -m pytest -q                         # esperado: 249 passed, 2 deselected
..\.venv\Scripts\python -m pytest -q tests\test_audit_phase9.py
..\.venv\Scripts\python -m qal.cli e2e-audit --out e2e_runs  # sintético
..\.venv\Scripts\python -m qal.cli e2e-audit --adapter yahoo --symbols SPY,QQQ,GLD,TLT --asset-class etf --out e2e_runs
cd ..\frontend
npm test
```

El informe JSON queda en `backend\e2e_runs\e2e_<fecha>.json`.

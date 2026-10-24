# Portfolio & Risk (Fase 6)

Construcción de portfolios y gestión jerárquica del riesgo sobre estrategias ya validadas. Es investigación:
no se generan órdenes, no se toca ninguna cuenta y la ejecución real sigue deshabilitada.

## Qué es una estrategia aquí

Una hipótesis con al menos una validación walk-forward se ve como un flujo de retornos (`qal/portfolio/strategies.py`):

- **retornos**: las barras de las ventanas de test del walk-forward, concatenadas. En cada ventana se usa la variante
  elegida solo con su ventana de entrenamiento (`Validator.walk_forward`). Se agregan a días UTC según `available_ts`
  y se guardan en caché por hash del contrato y de los datos;
- **exposición** ejecutada por instrumento, **costes** (modelo del contrato o el de la clase de activo),
  **liquidez** (`max_participation`, rupturas de capacidad), requisitos de datos, benchmark, resultados de
  validación, limitaciones y estado de aprobación.

Solo son **elegibles** las que cumplen los criterios configurados (`EligibilityCriteria`): estado
`VALIDATION_PASSED` o `PAPER_TRADING_ELIGIBLE`, gates superados, barras OOS mínimas, Sharpe OOS neto mínimo,
drawdown OOS máximo, rupturas de capacidad y calidad de datos. Las rechazadas siguen en el catálogo con **todos** sus
motivos. Una anomalía o una hipótesis no validada nunca es una estrategia.

**Estrategias sintéticas.** Mientras no haya estrategias reales elegibles, el motor se prueba con siete estrategias
sintéticas (`SYN:*`) generadas con un modelo factorial con semilla: dos de tendencia casi redundantes (correlación
≈ 0,9), un carry y un short-vol que cargan sobre un factor de crash activo solo en un tramo de estrés, una reversión a
la media, una con historia incompleta y una débil que las reglas rechazan. Hay que activarlas explícitamente
(`allow_synthetic`), llevan `is_synthetic=True` y cada resultado lo dice. No son evidencia sobre ningún mercado.

## Motor de riesgo (`risk.py`)

- Covarianza anualizada: muestral, **Ledoit-Wolf** (por defecto; robusta cuando hay pocas observaciones o la muestral
  es inestable) o EWMA.
- Correlaciones: estática, móvil (63 días), por **régimen** (volatilidad del portfolio EW en los 60 días previos frente
  a su mediana expansiva, causal) y en **periodos turbulentos** (top 10 % de volatilidad previa). No se usa la
  condición "peores días de retorno", porque seleccionar por la suma sesga la correlación a la baja.
- Contribución al riesgo (Euler), riesgo marginal, HHI de pesos y de riesgo, número efectivo de estrategias,
  ratio de diversificación, **número efectivo de apuestas** (entropía sobre componentes principales), factores
  estadísticos (PCA), solapamiento de exposiciones (similitud de las exposiciones medias por divisa) y drawdown.
- Exposición **look-through**: instrumento = Σ peso × posición de cada estrategia; las FX se descomponen en divisas
  (dos estrategias cortas en USD por pares distintos suman una sola exposición a USD).
- Análisis marginal: para cada estrategia, qué cambia al quitarla. Se marca **redundante** si su correlación con otra
  estrategia (o con el resto) supera 0,7, y **concentradora** si quitarla aumenta el número efectivo de apuestas.

Las correlaciones históricas no se suponen estables en estrés: el módulo de estrés prueba correlaciones de 0,9.

## Métodos de construcción (`construction.py`)

Pesos long-only por estrategia, con topes y redistribución del exceso:

- **Equal Weight**: 1/N.
- **Inverse Volatility**: proporcional a 1/σ.
- **Risk Budgeting**: contribuciones al riesgo iguales a presupuestos configurables (iguales por defecto), resuelto por
  descenso por coordenadas; los topes se aplican después.
- **Media-varianza** (opcional): máximo Sharpe con media histórica encogida al 50 %. Se ofrece para comparar su
  inestabilidad de pesos, no como método recomendado.

Todas usan solo la covarianza de la ventana previa a la decisión. El objetivo de volatilidad escala el total con la
volatilidad ex-ante estimada, con tope de apalancamiento.

## Hierarchical Risk Manager (`risk_manager.py`)

Determinista e independiente de cualquier LLM. Recibe pesos propuestos (de un método o de un agente) y devuelve los
pesos permitidos y una decisión por regla: `approved` o `blocked`, regla, valor observado, límite, motivo, acción,
fecha y versión de la política.

| Nivel | Reglas |
|---|---|
| Estrategia | calidad de datos, antigüedad de datos, historia mínima, drawdown propio, peso máximo |
| Grupo | peso por grupo de estrategias, cuota por contraparte, exposición por instrumento, neta por divisa, bruta por mercado |
| Portfolio | cuota máxima de riesgo de una estrategia, volatilidad ex-ante máxima, apalancamiento, exposición bruta y neta, capacidad de liquidez, fallos de ejecución simulada, pérdida diaria y drawdown (en el backtest) |

Las reglas que no se pueden evaluar se registran como tales: con volumen de ticks (FX) la capacidad de liquidez es
"no evaluable", no infinita; sin contraparte asignada, la regla de contraparte no se aplica a esa estrategia.

**Políticas.** Versionadas, inmutables y con hash (tabla `risk_policies`, migración `0004`). Solo una persona puede
crear una versión (`actor_kind="human"`, nombre del aprobador y confirmación explícita); los intentos de agentes se
rechazan con 403 y quedan en la auditoría. Las decisiones se guardan en `risk_decisions` por ejecución.

## Sizing (`sizing.py`)

Tamaño fijo, volatilidad objetivo (con volatilidad medida **antes** de la decisión), presupuesto de riesgo y ajuste por
liquidez. Redondeo a lotes con multiplicador, lote mínimo, tick y apalancamiento máximo del instrumento (de
`Instrument.metadata_json["contract"]` si existe; si no, supuestos por clase de activo marcados como `assumed`).
Un stop-loss no garantiza la pérdida máxima: el informe muestra la pérdida en el stop y la pérdida con un gap igual al
mayor movimiento histórico.

## Stress tests (`stress.py`)

Cada escenario indica su tipo:

- **histórico**: peores ventanas de 1, 5 y 20 días y episodios con nombre dentro de los datos (Lehman, crisis del euro,
  SNB 2015, Brexit, COVID, 2022);
- **hipotético**: volatilidad ×2, correlaciones a 0,9, caída simultánea de 3σ, gap del 3 % en todos los instrumentos,
  gap del 15 % en CHF, salida forzosa con spreads ×5, covarianza del régimen de alta volatilidad, y repeticiones del
  backtest con datos retrasados 2 días, 30 % de rebalanceos fallidos, costes ×2 + 2 pb y una interrupción de mercado
  durante la peor semana;
- **sintético**: Monte Carlo por bootstrap estacionario de días completos (conserva dependencia entre estrategias y
  colas; los bloques conservan dependencia temporal), también solo con días de alta volatilidad. VaR y ES diarios,
  distribución del retorno a un año y del drawdown.

No son predicciones: el futuro puede ser peor que cualquier escenario.

## Backtest del portfolio (`backtest.py`)

- Al cierre de cada fecha de rebalanceo (diario, semanal, mensual o cada N días) se estima el riesgo con datos
  disponibles hasta ese cierre, se construyen los pesos, se aplica el objetivo de volatilidad y el risk manager, y los
  nuevos pesos rigen desde el día siguiente (más el retraso de ejecución configurado).
- Entre rebalanceos los pesos derivan con el retorno de cada estrategia. El rebalanceo paga el cambio de exposición
  look-through por el coste por lado de cada instrumento; los costes propios de cada estrategia ya están en sus
  retornos netos. Hay multiplicador de costes y slippage extra.
- Controles diarios: una pérdida diaria por encima del límite reduce a la mitad el día siguiente; un drawdown por
  encima del límite reduce a la mitad o aplana durante un periodo de enfriamiento, tras el cual la referencia del
  drawdown se reinicia.
- Salidas: métricas netas por método, estabilidad de pesos, primer tramo frente al último (30 %), benchmark EW sin
  objetivo de volatilidad ni risk manager, cada estrategia sola, sensibilidad a costes ×0/1/2/3.
- Comprobaciones de no look-ahead en cada ejecución: invariancia al truncar los datos (las decisiones anteriores a una
  fecha no cambian al quitar los datos posteriores) y retraso de ejecución.

Se guarda como experimento `portfolio_backtest` (también si falla o no hay estrategias elegibles).

## Agentes (`agent.py`)

El agente `portfolio_researcher` (reglas deterministas) propone combinaciones: todas las elegibles con risk budgeting,
quitar las redundantes y un subconjunto de baja correlación con inverse vol. El motor calcula cada propuesta y el risk
manager decide. Cada paso queda como `AgentMessage` (propuesta, cálculo, decisión, objeciones de elegibilidad). El
agente no puede aprobar carteras, cambiar límites ni elegir otra política.

## Uso

UI: pestaña **Portfolio & Risk**. API: ver [API.md](API.md#portfolio--risk). Ejemplo:

```bash
curl -X POST localhost:8000/api/v1/portfolio/runs -H 'content-type: application/json' \
  -d '{"strategy_ids": ["SYN:trend-eur","SYN:carry","SYN:meanrev"], "criteria": {"allow_synthetic": true}, "run_async": false}'
```

## Limitaciones

- Hoy ninguna estrategia real ha superado la validación, así que no hay ningún portfolio real que construir.
- Las series OOS de cada estrategia vienen del walk-forward, no del holdout; el holdout sigue aislado.
- Las FX se agrupan todas en el mismo mercado; no hay factores macro observables (solo PCA estadístico).
- Liquidez: con volumen de ticks la capacidad no se puede estimar.
- Las contrapartes de estrategias reales no están asignadas.
- La deriva de pesos usa los pesos sin el recorte temporal por drawdown, que se aplica como capa encima.

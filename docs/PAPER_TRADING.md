# Paper Trading (Fase 7): simulación de ejecución

Todo es simulado. El paquete `backend/qal/paper` no contiene ningún cliente de broker, exchange ni cuenta, y un test
(`test_isolated_from_real_execution`) falla si algún módulo importa librerías de red o de brokers. Las órdenes solo
las llena el simulador de ejecución con datos guardados. `GET /api/v1/health` sigue devolviendo
`live_trading_enabled: false`.

## Auditoría previa: qué se reutiliza

| Pieza existente | Uso en paper trading |
|---|---|
| `research.strategies.STRATEGIES` (`weights(frames)`, causales) | Motor de señales: los mismos pesos objetivo, decididos al cierre de la barra. |
| Último `validation` de la hipótesis (`selected_params_last_window`) | Parámetros de una estrategia validada (no se ajusta nada en el periodo de paper). |
| `research.costs.CostModel` y costes del contrato | Spread, comisión, slippage fijo y por rango, financiación, préstamo, funding y rollover. |
| `portfolio.sizing.ContractSpec` / `contract_for` | Multiplicador, lote, lote mínimo, apalancamiento máximo. |
| `portfolio.risk_manager.PolicyLimits` (política activa y versionada) | Límites del control de riesgo pre-trade. Solo se leen. |
| `RiskDecision` | Cada bloqueo pre-trade, con regla, observado, límite, acción, motivo y versión de política. |
| `Dataset`, `latest_dataset`, ingesta incremental | Replay de un snapshot fijo, o datos nuevos al ingerir. |
| `research.backtest.run_backtest` / `align` | Evaluación frente al backtest sobre las mismas barras. |
| `Experiment` (tipo `paper_session`), `AuditEvent`, `Job`, `Schedule` | Registro, auditoría y ejecución en segundo plano. |

No existía ningún modelo de órdenes, fills ni posiciones: se añaden siete tablas (migración `0005`).

## Módulos

| Módulo | Responsabilidad |
|---|---|
| `feed.py` | Market Data Feed: replay o datos nuevos, con procedencia (hash del dataset, sintético o no) y controles por barra. |
| `signals.py` | Strategy Signal Engine: pesos objetivo con los datos disponibles en el reloj simulado. No crea órdenes. |
| `orders.py` | Order Management: tipos, campos, máquina de estados e id de orden idempotente. |
| `execution.py` | Execution Simulator: reglas de llenado sobre OHLCV. |
| `accounting.py` | Portfolio State y P&L: posiciones, realizado y no realizado, divisas, margen. |
| `risk_gate.py` | Risk Gate pre-trade. |
| `reconcile.py` | Reconciliation Engine. |
| `engine.py` | Orquesta un paso por barra; eventos (Monitoring and Alerts) y registro. |
| `evaluation.py` | Evaluación frente al backtest con criterios prefijados. |
| `service.py` | Ciclo de vida: crear, arrancar, pausar, kill switch, órdenes manuales, cambios de configuración, reset, borrar, exportar. |

## Reloj y orden de operaciones (una barra T)

1. **Datos**: barras que empiezan en T. Controles: orden de timestamps, duplicados (iguales: aviso; distintos:
   fallo), huecos frente al calendario (más de `max_gap_bars`: fallo), precios inválidos u OHLC incoherente,
   disponible antes de completarse, retraso de publicación y, con datos nuevos, frescura y errores del proveedor.
   Un fallo **pausa la sesión antes de usar la barra**; una persona puede saltarla (queda registrado).
2. **Ejecución**: las órdenes activas se prueban contra T. Una orden nunca se llena en la barra cuyo cierre la
   decidió: solo en barras que empiezan en o después del momento de decisión (`available_ts`), más `delay_bars`.
3. **Valoración**: fills → posiciones y caja; valoración al cierre de T (no realizado); costes de mantenimiento.
4. **Señales** al cierre de T con barras cuyo `available_ts` ≤ reloj.
5. **Órdenes**: diferencia entre posición actual y objetivo, redondeada a lotes, tras el control de riesgo.
6. **Reconciliación**: si algo no cuadra, la sesión se pausa.

Cada paso es una transacción. Si el proceso cae, la sesión queda en la barra anterior y el paso se repite; el
`client_order_id` (hash de sesión, estrategia, instrumento, momento de decisión y propósito) es único, así que
repetir una barra no duplica órdenes. Un test comprueba que una sesión interrumpida y reanudada desde la base de
datos produce exactamente los mismos fills que una ejecutada de una vez.

## Órdenes

Tipos: market, limit, stop, stop-limit. Campos: id, estrategia, instrumento, lado, cantidad (unidades y lotes),
tipo, precio límite/stop, momento de decisión, estado, precio y momento de llenado, comisiones, slippage, motivo de
rechazo o cancelación, precio de referencia (último cierre conocido) y los controles de riesgo aplicados.

Estados: `CREATED → PENDING | REJECTED`; `PENDING → PARTIALLY_FILLED | FILLED | CANCELLED | EXPIRED`;
`PARTIALLY_FILLED → PARTIALLY_FILLED | FILLED | CANCELLED | EXPIRED`. Una transición no permitida lanza un error.

Las órdenes de estrategia son market o limit (`limit_offset_bps` mejor que el último cierre, caducan a los
`order_ttl_bars`). Con `stop_loss_pct` se coloca un stop (o stop-limit) protector por posición; tras saltar, la
estrategia no vuelve a entrar hasta que su señal cambie de dirección. Las órdenes manuales pasan por el mismo control.

## Supuestos de ejecución con OHLCV (conservadores y explícitos)

Las barras OHLCV no dicen en qué orden se movió el precio dentro de la barra:

- Precios mid; comprar paga medio spread, vender lo recibe.
- **Market**: apertura de la barra + medio spread + slippage fijo + slippage por rango (fracción del rango de la
  barra anterior, conocido antes de abrir).
- **Limit**: solo se llena si el precio cruza estrictamente el límite (tocarlo no basta); al mejor entre apertura y límite.
- **Stop**: se activa si el máximo (compra) o mínimo (venta) alcanza el stop; se llena a `max(apertura, stop)`
  más costes. Si el precio abre más allá del stop, se llena a la apertura, peor que el stop. **Un stop no garantiza
  la pérdida máxima.**
- **Stop-limit**: si se activa en la apertura y la apertura respeta el límite, se llena a la apertura; si se activa
  dentro de la barra, no se llena en esa barra (el recorrido posterior es desconocido) y trabaja como límite desde
  la siguiente.
- **Liquidez**: con volumen, como mucho `max_participation` del volumen de la barra (fills parciales). Sin volumen
  utilizable (FX en muchos proveedores) se llena entera y el fill lo dice: liquidez no evaluada.
- Escenarios de costes: base (1×), adverso (2×), severo (4×). Cada fill guarda spread y slippage estimados y el
  desfase frente al precio de referencia (coste observado en la simulación).

## Posiciones, P&L y divisas

Cuenta de margen en la divisa de la cuenta. La caja solo cambia con P&L realizado, comisiones y costes de
mantenimiento (financiación de largos, préstamo de cortos, funding de perpetuos, rollover de futuros). Las
posiciones abiertas se valoran al último cierre: **P&L no realizado**, que nunca se suma a la caja ni al realizado.
`equity = caja + no realizado`.

- FX: la divisa de cotización es la segunda del par; se convierte con el propio par (base = divisa de cuenta) o con
  otro par de la sesión. Si no hay tipo de cambio, la sesión no se crea (nunca se inventa un tipo).
- Futuros: multiplicador del contrato; el rollover es un coste periódico (los datos continuos no traen vencimientos).
- Cripto: spot (por defecto) sin cortos ni apalancamiento; `contract_type` distinto de `spot` en los metadatos del
  instrumento lo trata como derivado y paga funding.
- Instrumentos sin divisa conocida: se asume la de la cuenta y la sesión lo registra como supuesto.

## Control de riesgo pre-trade

En orden fijo, con los límites de la política activa del HRM: kill switch, estado del sistema (sesión en marcha y
reconciliada), estado de validación (se vuelve a comprobar en cada orden: una hipótesis suspendida deja de operar),
calidad y frescura de los datos, cortos en cripto spot, drawdown y pérdida diaria (solo se aceptan órdenes que
reducen riesgo), exposición por instrumento, bruta, neta y por divisa, apalancamiento, liquidez (participación en el
valor negociado de la barra anterior), margen libre y lote mínimo. Los límites de exposición **reducen** la orden a lo
permitido (registrado como bloqueo con la acción); el resto la **rechazan** con el motivo. Reducir una posición
siempre se permite salvo con kill switch. Las asignaciones por estrategia se limitan a `strategy.max_weight` al crear
la sesión. Ningún agente puede cambiar los límites: este módulo solo los lee.

**Kill switch**: cancela todas las órdenes abiertas, rechaza las nuevas y deja la sesión `HALTED`. Activarlo y
desactivarlo exige una persona con nombre y confirmación explícita; al desactivarlo la sesión queda en pausa.

## Reconciliación y recuperación

Las fuentes de verdad son los fills (posiciones, precio medio, realizado, comisiones) y el libro de caja. En cada
barra se reconstruye todo desde ahí y se compara con lo guardado (posiciones, precio medio, realizado, comisiones,
caja y cantidad llenada de cada orden). Una diferencia pausa la sesión y no se puede reanudar hasta que una
reconciliación manual (`POST /reconcile`) pase. El estado se recupera de la base de datos al reiniciar.

## Evaluación frente al backtest

`run_backtest` sobre exactamente las barras procesadas, con el mismo escenario de costes, ponderado por las
asignaciones. Métricas: señales (porcentaje idéntico al backtest con historia completa), órdenes aceptadas,
rechazadas, caducadas y canceladas, fill rate, desfase frente al cierre de decisión, costes paper frente a backtest,
rentabilidad, drawdown, exposición media, correlación y tracking error por barra, Sharpe, y cambios de régimen
(volatilidad de la sesión frente a las 250 barras previas). Los criterios (`EvaluationCriteria`: muestra mínima,
desfase de rentabilidad, correlación, ratio de drawdown, fill rate, ratio de costes, acuerdo de señales) se fijan y
se guardan con hash al crear la sesión y no se pueden cambiar después.

Veredictos: `insufficient_sample`, `consistent_with_backtest`, `deviates_from_backtest`. **Ninguno es una
aprobación**: la evaluación no cambia el estado de la estrategia ni la declara lista para operar. El estado
`PAPER_TRADING` sigue sin poder alcanzarse porque los criterios del Gate E no están definidos.

## Datos y estrategias de demostración

- **Replay**: un snapshot fijo (hash guardado), barra a barra. Es una reproducción del pasado y se etiqueta así en
  la API, la interfaz y las exportaciones. Por defecto empieza en el último cuarto de la historia común.
- **Datos nuevos**: al arrancar, el cursor se coloca en la última barra existente; solo se operan barras que llegan
  después (ingesta manual o programada; un `Schedule` de tipo `paper_run` mueve la sesión).
- **Sintéticos**: una sesión sobre datasets sintéticos lleva `is_synthetic` y la etiqueta "SYNTHETIC DATA".
- Hoy ninguna estrategia real está validada, así que el simulador se puede ejercitar con reglas `DEMO:<tipo>` de la
  librería. Requieren `allow_unvalidated_demo=true` y se marcan "UNVALIDATED DEMO" en todas partes. No son evidencia
  de ninguna ventaja.

## Persistencia y reproducibilidad

Tablas: `paper_sessions`, `paper_signals`, `paper_orders`, `paper_fills`, `paper_positions`, `paper_ledger`,
`paper_equity`, `paper_events`; decisiones de riesgo en `risk_decisions` (vinculadas al `Experiment`
`paper_session`). Cada sesión guarda configuración y hash, versión de código, referencias y hashes de datos, versión
de la política y criterios. Los cambios de configuración durante la sesión (solo `cost_scenario`, `order_type`,
`limit_offset_bps`, `order_ttl_bars`, `stop_loss_pct`, `stop_order_type`, `rebalance_threshold`, `delay_bars`)
crean una versión nueva con quién, cuándo, por qué, qué cambió y en qué barra. Reset y borrado exigen confirmación;
el registro del experimento se conserva (con el resumen previo o marcado como borrado).

## Interfaz

Pestaña **Paper Trading**: crear sesión (estrategias elegibles, demos con confirmación, instrumentos, capital
virtual, costes, replay o datos nuevos, tipo de orden, stop), arrancar, pausar, avanzar barras o ejecutar en segundo
plano, kill switch, órdenes y posiciones, P&L realizado y no realizado por separado, bloqueos de riesgo, historial
de eventos con filtros (nivel, categoría, estrategia, instrumento, periodo), evaluación frente al backtest,
configuración e historial, exportación CSV y comparación de sesiones.

## Limitaciones

- Barras diarias o intradía OHLCV: la ejecución dentro de la barra es una estimación conservadora, no un libro de órdenes.
- No hay spread observado: los proveedores actuales no traen bid/ask; se usa el spread del modelo de costes.
- Sin vencimientos de futuros ni calendario de festivos por mercado (los festivos aparecen como huecos cortos).
- Las posiciones se llevan por estrategia e instrumento; no se netean órdenes entre estrategias.
- Al empezar la sesión parte plana, mientras que el backtest de comparación puede llevar posición desde el
  calentamiento: pequeña diferencia en las primeras barras.

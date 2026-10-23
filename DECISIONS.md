# Registro de decisiones

Formato: contexto, decisión, consecuencias. Las decisiones se revisan cuando cambian los supuestos.

## D-001 Repositorio nuevo
No había repositorio previo que inspeccionar (el proyecto no tenía repositorio vinculado), así que se parte de cero
con el stack recomendado en la especificación. No se ha descartado ningún componente existente.

## D-002 Monolito modular en lugar de microservicios
Un único paquete Python (`qal`) con módulos separados por dominio y una API. Es más fácil de desarrollar,
probar y desplegar. El único proceso separado previsto es el de ejecución real (D-008).

## D-003 SQLite por defecto, PostgreSQL en Docker y producción
SQLite permite arrancar sin infraestructura. Toda la suite de pruebas se ejecuta también contra PostgreSQL 16
(`QAL_TEST_DATABASE_URL`), lo que ya detectó una diferencia real (longitud de `algorithm_version`). Producción:
PostgreSQL + `alembic upgrade head`.

## D-004 Tres relojes por barra
`event_ts` (fin de barra: antes no se conoce la barra), `available_ts` (`event_ts` + retraso de publicación
conservador del proveedor) y `received_ts` (cuándo la recibió este sistema). Para histórico descargado en bloque
`available_ts` es una estimación conservadora documentada por adaptador, no una observación. Las simulaciones
deben usar `available_ts`. En barras diarias `ts` es la fecha de sesión a 00:00 UTC y `event_ts = ts + 1 día`,
posterior al cierre de cualquier mercado de esa fecha.

## D-005 Precios ajustados solo para retornos
Los niveles ajustados hacia atrás incorporan factores futuros y no son precios negociables. Los retornos entre
cierres ajustados consecutivos solo usan la acción corporativa conocida en la fecha ex, por lo que sí son
point-in-time. Se guardan `close` (bruto) y `adj_close`; los detectores usan retornos ajustados y, para pares,
un índice de retorno total reconstruido compuesto desde la primera barra.

## D-006 Snapshots inmutables por hash de contenido
Cada ingesta produce un snapshot nuevo si el contenido cambia (el hash excluye `received_ts`). Las descargas
incrementales piden las últimas 5 barras de solapamiento; si el proveedor revisa barras ya guardadas se registra
un evento `revisions` (warn). Las barras sin cambios conservan su `received_ts` original.

## D-007 p-valores empíricos y Benjamini-Hochberg sobre todas las pruebas
Cada barra evaluada por cada detector cuenta como una prueba. El p-valor es de rango contra la ventana previa y
es optimista con agrupamiento de volatilidad (documentado). BH se aplica sobre todas las pruebas del escaneo. Con
miles de pruebas casi ninguna anomalía individual será "significativa" tras la corrección, lo cual es el
resultado honesto: una anomalía es un punto de partida para una hipótesis, no evidencia.

## D-008 Credenciales y ejecución separadas
Las credenciales solo se leen del entorno del proceso. La API no acepta credenciales en las peticiones
(`adapter_config` no se usa para secretos). La ingesta cripto usa endpoints públicos sin clave. No existe
código de envío de órdenes. La ejecución (Fase 7) será un servicio aparte con credenciales propias, límites
independientes y autorización explícita.

## D-009 Trabajos en hilo de fondo con estado en base de datos
Para el MVP basta un worker en proceso con reclamación atómica (`UPDATE … WHERE status='queued'`), reintentos con
backoff exponencial, claves de idempotencia y re-encolado de trabajos huérfanos al arrancar. Redis/colas o un
motor durable (Temporal, Prefect, Dagster) se evaluarán cuando haya flujos multietapa largos (agentes, Fase 3).

## D-010 Gate A bloquea calidad `fail`; la antigüedad no bloquea la investigación histórica
Un dataset con incoherencias graves, duplicados conflictivos, precios no positivos o disponibilidad no
reconstruible no se escanea. La antigüedad (`stale`) se informa en Data Health pero no bloquea un escaneo
histórico; sí bloqueará paper trading y ejecución (Fase 6).

## D-011 Escaneos cacheados por hash de configuración y datos
Una petición idéntica (mismos datos por hash, misma configuración y versiones) devuelve el experimento existente
para no repetir trabajo ni inflar el número de experimentos. `force=true` crea un experimento nuevo (y la prueba
de reproducibilidad comprueba que da el mismo resultado).

## D-012 Detección de pares solo con pares explícitos
La selección automática de pares con toda la muestra introduce sesgo de selección y look-ahead. En la Fase 1 los
pares se declaran explícitamente. La selección automática con periodo de formación separado se añadirá con la
validación (Fase 4/5).

## D-013 Sin datos simulados ante fallos
Si un proveedor falla o no está configurado (p. ej. MT5 fuera de Windows) se devuelve un error explícito y se
registra en `providers.last_error` y en auditoría. Nunca se sustituye por datos sintéticos.

## D-014 Contratos de hipótesis congelados y versionados
Una hipótesis no se edita (`PATCH` → 409). Cambiarla crea una versión nueva de la misma familia (`family_id`,
`parent_id`). Así lo que se fijó antes de ver resultados no se puede reescribir a posteriori.

## D-015 Las pruebas se cuentan por familia
Cada variante de parámetros de cada validación, incluidas las fallidas, suma al número de pruebas de la familia.
El DSR usa ese recuento total, de modo que reintentar con variantes penaliza la evidencia en lugar de mejorarla.

## D-016 Ejecución a la apertura de la barra siguiente
La señal calculada con la barra t se ejecuta a la apertura de t+1 (la barra empieza a contar cuando su
`available_ts` ya ha pasado en todos los activos). Es conservador frente a ejecutar al cierre de t y elimina una
fuente habitual de look-ahead. Una prueba con una estrategia que mira el futuro comprueba que Gate B la bloquea.

## D-017 Solo el motor de políticas promueve
`VALIDATION_PASSED` y los estados posteriores solo se alcanzan a través de `evaluate_gates`. La API de
transiciones manuales rechaza los destinos reservados. Los Gates E y F (riesgo y ejecución) no existen todavía,
así que `PAPER_TRADING` y posteriores están bloqueados.

## D-018 La validación no se programa
Los escaneos y descargas se pueden programar; las validaciones no. Repetir validaciones automáticamente sobre la
misma familia aumentaría pruebas sin una decisión humana detrás y favorecería el p-hacking.

## D-019 Escritorio con Tauri y backend Python como proceso hijo
Tauri (Rust + WebView del sistema) en lugar de Electron por tamaño y consumo. La app lanza `uvicorn` como proceso
hijo en un puerto local, y la API sirve también el frontend compilado (`QAL_FRONTEND_DIST`), así que web y
escritorio usan exactamente el mismo código. El backend termina si muere el proceso padre
(`QAL_EXIT_WITH_PARENT`). Datos y base de datos van en el directorio de datos de la aplicación.


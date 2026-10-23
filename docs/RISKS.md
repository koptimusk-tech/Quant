# Riesgos identificados (Fase 0)

| Riesgo | Impacto | Mitigación actual | Pendiente |
|---|---|---|---|
| Look-ahead por timestamps de proveedor (barras diarias fechadas al inicio de sesión, datos revisados) | Resultados inflados | `event_ts`/`available_ts` conservadores, pruebas de truncado y futuro envenenado, registro de revisiones | Motor de backtest sobre `available_ts` (Fase 2) |
| Precios ajustados con información futura | Niveles no negociables | Retornos ajustados únicamente (D-005) | Ajustes corporativos point-in-time por fecha de anuncio |
| Sesgo de supervivencia (Yahoo/Stooq sin deslistados) | Universos optimistas | Documentado en el catálogo de proveedores | Proveedor con universos históricos para factores (Fase 5) |
| Sesgo de selección / pruebas múltiples | Falsos descubrimientos | BH por escaneo, número de pruebas registrado, escaneos cacheados | Contabilidad a nivel de programa, DSR, PBO (Fase 4) |
| p-valores empíricos optimistas por agrupamiento de volatilidad | Sobreconfianza | Documentado; puntuación estadística no es probabilidad | Bootstrap por bloques / modelos GARCH de referencia |
| Correlación alta tomada como estacionariedad | Pares espurios | Engle-Granger en ventana y advertencia explícita | Pruebas de ruptura y estabilidad de parámetros |
| Proveedores no oficiales que cambian o bloquean | Huecos, ingesta caída | Errores visibles en Data Health, reintentos, sin datos sustitutos | Múltiples fuentes y reconciliación |
| Hora de servidor de MT5 distinta de UTC | Desalineación temporal | `server_utc_offset_hours` obligatorio de facto, documentado | Detección automática con sesiones conocidas |
| Volumen no consolidado (cripto por exchange, ticks en FX) | Señales de liquidez engañosas | Documentado por proveedor; detector de volumen no aplicable sin cobertura | Volumen consolidado multi-venue |
| Agentes LLM que eluden controles | Riesgo operativo | Aún no hay agentes; credenciales fuera de la API | Motor de políticas como única autoridad (Fases 3 y 6) |
| Ejecución real accidental | Pérdidas | No existe código de ejecución; flag `live_trading_enabled` siempre false | Servicio separado + Gate F (Fase 7) |
| Coste de modelos | Gasto descontrolado | Sin LLM en la Fase 1 | Presupuestos y caché por experimento (Fase 3) |

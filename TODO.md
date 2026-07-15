# TODO - Kairos Coach Roadmap

## Estado actual
- Arquitectura activa: DB-first multiusuario con Supabase obligatorio.
- RAG ligero operativo con base de conocimiento del atleta.
- Suite de tests: 223 tests. CI/CD con GitHub Actions activo.
- Herramientas internas kairos_* operativas (tendencias, correlaciones, desglose deportivo).

---

## ✅ Completado

### Hitos técnicos base
- Refactor a persistencia multiusuario en Supabase.
- Login/registro de usuario de aplicacion y onboarding inicial.
- Onboarding enriquecido: creacion/persistencia de athlete_knowledge inicial con perfil + datos MCP.
- Estado proactivo de arranque (48h): body battery, HRV, sueno y entrenamientos recientes.
- Deteccion de cambios de perfil Garmin al iniciar y reporte contextual.
- Prompt y prompt compacto alineados al nuevo enfoque de coach.
- Limpieza de memoria JSON legacy en runtime.
- Documentacion principal alineada con DB-first.
- Analisis profundo de actividad por fecha: pre-fetch enriquecido con zonas FC, hidratacion, sueno y body battery calculados en Python (el LLM solo interpreta y hace coaching).
- Arquitectura de dos capas documentada en system prompt: capa datos vs capa coaching.
- Correccion de busqueda de actividades por fecha (campo start_time snake_case del MCP).
- Auto-login con contrasena cifrada Fernet: al arrancar, si el usuario existe, accede directamente sin pedir password. Flujo de recuperacion si la contrasena de Garmin Connect cambia.
- Politica de herramientas MCP implementada para runtime: guia de enrutado por intencion y referencia desde el system prompt para reducir tokens y latencia.
- Compatibilidad MCP actualizada para cambios de contrato: get_body_battery y get_body_composition con start_date/end_date.
- Compatibilidad MCP para PRs: endpoint vigente get_personal_record (singular) con alias defensivo del plural.
- Consulta de records personales mejorada: respuesta directa en tabla de running y follow-up contextual de distancias/marcas.
- Consulta de records por deporte mejorada: separacion running/ciclismo sin mezclar disciplinas.
- Categorias de records personales traducidas al espanol en la salida al usuario.
- Separacion explicita objetivo (goals) vs plan activo (training_plan) en el perfil de usuario.
- Estado proactivo condicionado por training_plan: sin plan muestra aviso; con plan propone adaptar sesion diaria.
- Ruta determinista para estado del plan en chat: responden desde training_plan real sin depender del LLM.
- Prompting reforzado: checklist MCP minimo por intencion, formato de fecha DD/MM/AAAA, respuestas maximos/minimos con valor + actividad + fecha.
- Cuantificacion de carga y fatiga (TSS/ATL/CTL/TSB) con tau ajustados por deporte y percentiles individualizados.
- Series temporales de carga/fatiga persistidas (hasta 120 dias) en Supabase por atleta.

### Ítems numerados cerrados

#### 10) MCP solo consulta para coaching
- Runtime endurecido: filtrado de tools de escritura en initialize y bloqueo en loop de tool-calls.
- Cobertura de tests para garantizar que no se ejecutan tools de escritura en modo read-only.

#### 11) Planes de entrenamiento
- Tablas dedicadas en Supabase (training_plan, training_plan_session, training_plan_version).
- Generacion estructurada en runtime, validacion previa a persistencia, versionado por edicion y resumen de cambios.
- Comandos de gestion: /plan crear, /plan listar, /plan activar, /plan ver.

#### 12) Naming del producto
- Nombre final definido: Kairos Coach. Aplicado en toda la base de codigo, prompts, README y documentacion.

#### 13) Cuantificación de carga y fatiga (TSS/ATL/CTL/TSB)
- Modelo EWMA con tau ajustados por deporte y percentiles individualizados por atleta.
- Integrado en snapshot proactivo de arranque con resumen operativo y regla aplicada de actuacion.
- Series temporales persistidas en Supabase.

#### 14) Pasos de ejecución en README.md
- Instrucciones de instalacion, configuracion, uso basico y ejecucion de tests documentadas.

#### 15) Motor de análisis histórico de métricas
- `kairos_load_trends`: serie temporal de TSS/ATL/CTL/TSB con granularidad diaria y semanal.
- `kairos_correlate`: correlación de Pearson entre dos métricas de carga/fatiga (N, r, interpretación).
- Herramientas internas Python puro, operando sobre load_metrics.series en Supabase.

#### 17) Herramienta de consulta sobre datos históricos
- Implementado via `kairos_load_trends` sobre `load_metrics.series` en Supabase.

#### 19) Desglose semanal y por deporte
- `kairos_weekly_sport_breakdown`: consulta Garmin MCP, agrupa por deporte y devuelve sesiones/horas/km.

#### 21) GitHub Actions CI
- `.github/workflows/tests.yml` ejecuta la suite de 223 tests en cada push y pull request, sin credenciales reales.

#### 23) Principio "relaciones > valores aislados"
- Regla en system_prompt y compact: nunca reportar valor aislado cuando se puede cruzar con otra metrica.

#### 24) Detección de anomalías biométricas
- Flags en system_prompt: FC reposo elevada sin carga, sueno malo >=2 noches, HRV >15% bajo media 7d, body battery <30 >=2 dias.

#### 25) Tendencia siempre junto al valor puntual
- Regla: para HRV, body battery, sueno, FC en reposo y VO2max, reportar valor hoy + media 7d + direccion.

#### 26) Transparencia de datos: calidad y tamaño de muestra
- Regla: declarar N y calidad del dato en toda afirmacion sobre tendencias.

#### 28) Framework de Race Readiness
- Protocolo en system_prompt: monitorizar progresion del largo, desnivel semanal y volumen vs. demanda de la carrera objetivo.

#### 31) Protocolo plan activo ↔ datos del día
- Antes del entreno: cruzar readiness/TSB con sesion planificada -> ejecutar/reducir/posponer/swapear.
- Despues del entreno: comparar ejecutado vs planificado -> analisis de desviacion.

#### 33) Historial profundo como base de generación de planes
- Regla: al generar plan, analizar ultimas 8-12 semanas de actividades reales para calibrar nivel de partida real.

#### 34) Protocolo de revisión post-sesión como entrenador
- Nota estructurada corta al compartir actividad sin pedir analisis profundo: que fue bien / que se desvio / un ajuste.

---

## ⏳ Pendiente

### Prioridad alta

#### 37) Integración TrainingPeaks MCP (capa de escritura)
- Añadir `trainingpeaks-mcp` (https://github.com/JamsusMaximus/trainingpeaks-mcp) como servidor MCP secundario junto a `garmin_mcp`.
- Arquitectura resultante: `garmin_mcp` = capa de lectura. `trainingpeaks-mcp` = capa de escritura (calendario, sesiones estructuradas, notas, eventos).
- Autenticación via cookie del navegador (sin aprobación de API oficial TP). 78 tools disponibles.
- Funcionalidades prioritarias:
  - `tp_create_workout` con estructura de intervalos JSON auto-computando IF/TSS.
  - `tp_pair_workout` — empareja workout planificado con el ejecutado (modelo técnico para #31).
  - `tp_get_fitness` — CTL/ATL/TSB nativo de TP para contrastar con modelo propio desde Garmin.
  - `tp_add_workout_comment` — el coach deja notas en sesiones del calendario.
  - `tp_get_atp` — Plan de Temporada Anual con TSS targets semanales por periodo.
- Requiere cuenta TrainingPeaks (no gratuita en todos los planes).
- Inspirado en `trainingpeaks-mcp` (111 stars, activo, MIT).

#### 1) Endurecimiento final post-implementación
- Al terminar implementacion, ejecutar bateria de seguridad: secretos, datos sensibles, configuraciones inseguras, dependencias y transporte.
- Aplicar remediaciones antes de declarar cierre del proyecto.

### Prioridad media

#### 18) Integración Strava
- Conectar Strava como fuente secundaria de actividades via OAuth2.
- Deduplicación cross-plataforma: misma fecha + deporte + duración/distancia con 5% tolerancia → Garmin como source of truth.
- Añadir campo `source_platform` a actividades en Supabase.
- Inspirado en la arquitectura de providers de FitMCP.

#### 27) [PROMPTING] Umbral de spike semanal >20%
- Si el volumen o la carga de la semana actual supera en más del 20% la semana anterior, advertir activamente aunque el TSB no haya cruzado el umbral individual.
- Útil cuando hay pocos datos históricos para calcular percentiles individualizados.
- Aplicar también en system_prompt_compact.md.

#### 35) [PROMPTING] Contextualización meteorológica en análisis de actividad
- El coach debe considerar las condiciones del día (temperatura, viento, humedad) como variable explicativa del rendimiento (±10–20% de impacto).
- Sección "Condiciones del día" en el análisis si el usuario las reporta, o pedirlas si el rendimiento parece inusualmente alto/bajo.
- Largo plazo: integración con API meteorológica por fecha y coordenadas GPS.
- Inspirado en el Feature 03 de FitMCP.

#### 38) [PROMPTING] Formato de workout estructurado estándar TP
- Añadir al system_prompt el formato JSON de intervalos de TP (steps, reps, intensityClass, %FTP/%HR).
- Mejora la precisión de sesiones: actualmente usamos RPE textual, no intensidad cuantificada por zonas.
- Depende de #37 (integración TP) para ser exportable directamente.
- Inspirado en la arquitectura de workouts estructurados de `trainingpeaks-mcp`.

#### 39) Power PRs granulares por duración (ciclismo/triatlón)
- Power PRs por duración (5s, 1min, 5min, 20min, 60min, 90min) como estándar de rendimiento en ciclismo.
- Si el usuario tiene TP: usar `tp_get_peaks`. Si solo Garmin: usar `get_cycling_ftp` + sesiones clave.
- Inspirado en `tp_get_peaks` de `trainingpeaks-mcp`.

#### 2) Refactor por capas
- Separar claramente presentacion (CLI), negocio (coach) y datos (Garmin/LLM/storage).
- Reducir acoplamiento entre agent/main.py y agent/trainer_agent.py.

#### 4) Logging de producción
- Sustituir mensajes debug de consola por logging con niveles configurables.
- Controlar verbosidad por entorno.

### Prioridad baja

#### 22) Makefile + scripts de setup automatizado
- `Makefile` con targets: `setup`, `login`, `test`, `serve`, `lint`.
- `setup.ps1` (Windows) y `setup.sh` (Unix) que creen el venv, instalen dependencias y generen `.env` scaffold.
- Inspirado en el setup automatizado de FitMCP.

#### 29) [PROMPTING] Regla de composición corporal como tendencia semanal
- Al analizar peso o composición corporal, no interpretar fluctuaciones diarias como señal.
- La unidad mínima de análisis es la tendencia semana a semana cruzada con tipo y volumen de entrenamiento.
- Aplicar también en system_prompt_compact.md.

#### 36) [PROMPTING] Métricas de natación y protocolo de triatlón
- Métricas de natación: SWOLF, cadencia de brazada, distancia por brazada.
- Protocolo de triatlón: análisis por disciplina + tiempos de transición T1/T2 + distribución de carga.
- Inspirado en el Feature 05 de FitMCP.

#### 40) Plan de Temporada Anual (ATP) — periodización a largo plazo
- Fase 1 (prompting): documentar periodos base/construcción/pico, TSS targets por periodo, A/B/C races.
- Fase 2 (datos): tabla en Supabase para el ATP del atleta que complementa a `training_plan`.
- Si el usuario tiene TP: `tp_get_atp` puede ser la fuente de verdad del ATP.
- Inspirado en `tp_get_atp` de `trainingpeaks-mcp`.

#### 5) Dashboard de métricas
- Explorar panel web opcional para tendencias (HRV, VO2max, sueño, estrés, carga).
- Evaluar Streamlit como primer candidato.

#### 6) Resumen diario automatizado
- Ejecutar resumen diario programado (Windows Task Scheduler).
- Salida por Telegram o email.

### Backlog abierto

#### 8) Gestión de tokens por proveedor LLM
- Evaluar tabla dedicada de tokens con campo de proveedor para soportar múltiples LLM de forma ordenada.

#### 9) Congelado del código MCP
- Evaluar vendorizar/congelar el código MCP en el repo para evitar roturas por cambios upstream.
- Analizar qué tools usamos y cuáles no para traernos al código solo las que necesitamos.

---

## Notas de mantenimiento
- Mantener TODO sincronizado con decisiones de arquitectura reales.
- Evitar registrar aqui tareas ya completadas salvo resumen corto de hitos.
- Regla de equipo: documentar siempre los cambios antes de hacer commit.

## 20) Renombrar proyecto en GitHub
Pendiente por tu parte en GitHub: renombrar el repo de garmin-ai-coach a kairos-coach desde Settings → General, y actualizar la URL del git clone en el README. Eso no lo puedo hacer desde aquí.

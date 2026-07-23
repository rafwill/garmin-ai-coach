# MCP Tool Routing Guide (Runtime)

Objetivo: servir como referencia rápida en tiempo de ejecución para decidir que tool llamar segun la intencion del usuario, minimizando exploracion del MCP y consumo de tokens.

Uso recomendado:
1. Identificar intencion principal del usuario.
2. Buscar en este documento la seccion equivalente.
3. Llamar primero la tool prioritaria (o secuencia corta recomendada).
4. Escalar a tools secundarias solo si falta contexto.

Politica de seguridad (modo coach por defecto):
- Este agente opera en modo solo consulta (read-only) para MCP.
- Las secciones de escritura (crear/actualizar/borrar/programar/subir) se consideran modo admin/operativo y no deben ejecutarse en conversaciones normales de coaching.

Notas de compatibilidad MCP (actual):
- `get_personal_record` es el endpoint vigente para PRs (no `get_personal_records`).
- `get_body_battery` requiere `start_date` y `end_date`.
- `get_body_composition` requiere `start_date` y `end_date`.

---

## 1) Estado diario y energia (check-in rapido)

Cuando el usuario pregunta: "como estoy hoy", "puedo entrenar fuerte", "que hago hoy".

Secuencia prioritaria:
1. `get_morning_training_readiness` (si disponible del dia)
2. `get_body_battery`
3. `get_sleep_summary`
4. `get_hrv_data`
5. `get_stress_summary`

Secundarias:
- `get_rhr_day`
- `get_spo2_data`

Nota de arranque (estado proactivo 48h):
- La recomendación inicial depende de `training_plan` activo en perfil interno.
- `goals` (objetivo de carrera) no implica automáticamente plan activo.

---

## 2) Actividad reciente

Cuando el usuario pide analizar su ultima actividad.

Secuencia prioritaria:
1. `get_activities` con `limit=1`
2. `get_activity` usando `activityId`
3. `get_training_load_trend` (para contextualizar carga)

Secundarias:
- `get_activity_splits`
- `get_activity_weather`
- `get_activity_hr_in_timezones`
- `get_activity_power_in_timezones`

---

## 3) Actividad por fecha concreta

Cuando el usuario menciona una fecha exacta o un dia concreto.

Secuencia prioritaria:
1. `get_activities_by_date` (o `get_activities_fordate`)
2. `get_activity` para cada id relevante

Notas:
- Si el sistema ya inyecto bloque pre-computado de actividad, NO repetir llamadas.

---

## 4) Sueno, estres, respiracion, SpO2

Secuencia prioritaria por dominio:
- Sueno rapido: `get_sleep_summary`
- Sueno detalle: `get_sleep_data`
- Estres rapido: `get_stress_summary`
- Estres detalle: `get_stress_data`
- Respiracion rapido: `get_respiration_summary`
- Respiracion detalle: `get_respiration_data`
- Oxigenacion: `get_spo2_data`

---

## 5) Rendimiento y forma (semanas/meses)

Cuando el usuario pregunta por progresion, forma actual o prediccion.

Secuencia prioritaria:
1. `get_training_status`
2. `get_vo2max_trend`
3. `get_endurance_score`
4. `get_race_predictions`
5. `get_lactate_threshold`
6. `get_fitnessage_data`

Secundaria:
- `get_personal_record`

---

## 6) Carga y riesgo de sobreentrenamiento

Secuencia prioritaria:
1. `get_training_load_trend`
2. `get_hrv_trend`
3. `get_respiration_trend`
4. `get_training_status`

Heuristica minima:


## 7) Planificacion semanal

Secuencia prioritaria:
1. `get_training_status`
2. `get_training_load_trend`
3. `get_weekly_intensity_minutes`
4. `get_weekly_steps`
5. `get_weekly_stress`
6. `get_race_predictions`

## 7.2) Generacion y gestion funcional de planes

Cuando la intencion sea de plan de entrenamiento, separar:

1. Estado del plan:
- Resolver con `training_plan` activo (no con `goals`).

2. Generacion/ajuste de plan:
- El coach genera propuesta estructurada con sesiones y criterios de ajuste por fatiga.
- Antes de generar, consultar historial amplio (8-12 semanas) con `get_activities` para calibrar nivel real.
- Si hay cambios, tratarlos como nueva version conceptual del plan.

3. Gestion persistente del plan (CLI):
- Crear: `/plan crear`
- Listar: `/plan listar`
- Ver detalle: `/plan ver <plan_id>`
- Activar: `/plan activar <plan_id>`

Regla:
- No afirmar que un plan se guardó o activó sin ejecutar una accion de gestion real.

## 7.3) Cruce plan activo con datos del dia (OBLIGATORIO cuando hay plan activo)

### Antes del entreno
1. Estado diario completo: `get_morning_training_readiness`, `get_body_battery`, `get_sleep_summary`, `get_hrv_data`, `get_stress_summary`.
2. Consultar sesion planificada del dia en `training_plan_session`.
3. Decidir: ejecutar (\u1f7e2) / reducir intensidad (\u1f7e1) / posponer (\u1f7e0) / swapear por recuperacion (\u1f534).

### Despues del entreno (compliance)
1. Analisis de actividad reciente: `get_activities` + `get_activity`.
2. Consultar sesion planificada del dia en `training_plan_session`.
3. Comparar: distancia real vs planificada, zonas FC reales vs intensidad planificada, tipo de sesion.
4. Dar analisis de desviacion y ajuste propuesto para la semana.

## 7.1) Estado del plan (si tiene plan o no)

Cuando el usuario pregunta por estado de plan actual.

Ejemplos de intencion equivalente:
- "tengo algun plan?"
- "cual es ese plan?"
- "que plan llevo esta semana?"
- "sigo con el plan?"

Regla operativa:
- Resolver contra `training_plan` (perfil interno), no contra `goals`.
- `goals` no implica automaticamente plan activo.

---

## 7.4) Herramientas internas Kairos (kairos_*)

Estas herramientas se invocan igual que las MCP pero no llaman al servidor Garmin:
- `kairos_load_trends`: serie temporal de TSS/ATL/CTL/TSB desde el perfil. Usar para preguntas de tendencias de carga/fatiga/forma a lo largo del tiempo.
- `kairos_correlate`: correlacion de Pearson entre dos metricas (tss, atl, ctl, tsb). Usar para preguntas de relacion entre metricas.
- `kairos_weekly_sport_breakdown`: desglose de actividades por deporte en N semanas. Usar para preguntas de distribucion de entrenamiento entre disciplinas.

**IMPORTANTE — TSS no existe en actividades Garmin MCP:**
Los objetos de actividad de Garmin (get_activities, get_activity, get_activities_by_date) NO contienen
campos de TSS, ATL, CTL ni TSB. No busques esos campos en respuestas de actividades Garmin.
Para cualquier pregunta que incluya TSS, ATL, CTL o TSB usa siempre `kairos_load_trends` primero.

Ejemplos de intencion que deben usar `kairos_load_trends`:
- "cual fue mi TSS ayer / esta semana / el lunes?"
- "como ha evolucionado mi carga esta semana?"
- "cuanto TSS llevo acumulado?"
- "cual es mi ATL/CTL/TSB hoy?"
- "estoy en sobreentrenamiento?"

Respuesta a "TSS de ayer":
1. Llama `kairos_load_trends(metric="tss", weeks_back=1)`
2. En la respuesta, el campo `daily` contiene una entrada por cada dia de los ultimos 14.
3. Busca la entrada cuya `date` sea ayer (hoy - 1 dia) y muestra su `value`.
4. NO llames get_activities_by_date ni get_activity para obtener TSS.

---

## 8) Perfil y composicion corporal

Secuencia prioritaria:
1. `get_user_profile`
2. `get_body_composition`
3. `get_weigh_ins` / `get_daily_weigh_ins` (si se necesita historial de peso)

---

## 9) Nutricion e hidratacion

Consulta diaria:
- `get_nutrition_daily_food_log`
- `get_nutrition_daily_meals`
- `get_nutrition_daily_settings`
- `get_hydration_data`

Acciones de escritura:
- Buscar alimento: `get_custom_foods`
- Crear alimento: `create_custom_food`
- Actualizar alimento: `update_custom_food`
- Borrar alimento: `delete_custom_food`
- Log rapido: `log_food`
- Log desde custom food: `log_custom_food`
- Flujo completo: `upsert_and_log`

---

## 10) Workouts (crear, subir, programar)

Lectura:
- `get_workouts`
- `get_workout_by_id`
- `get_scheduled_workouts`
- `get_training_plan_workouts`

Creacion/subida:
- Alto nivel: `create_walk_run_workout`, `create_run_workout`, `create_z2_walk_workout`, `create_strength_workout`
- JSON Garmin: `upload_workout`, `upload_workouts`

Planificacion:
- Individual: `schedule_workout`
- Lote: `schedule_workouts`, `schedule_week`
- Desprogramar: `unschedule_workout`, `unschedule_workouts`

Mantenimiento:
- `delete_workout`, `delete_workouts`
- `download_workout`

---

## 11) Actividades: edicion manual

Para modificar metadatos de actividad:
- `set_activity_name`
- `set_activity_type`
- `set_activity_description`
- `set_activity_event_type`
- `set_perceived_effort`
- `set_activity_feel`

---

## 12) Gear y dispositivos

Gear:
- `get_gear`
- `add_gear_to_activity`
- `remove_gear_from_activity`

Dispositivos:
- `get_devices`
- `get_device_last_used`
- `get_device_settings`
- `get_primary_training_device`
- `get_device_solar_data`
- `get_device_alarms`

---

## 13) Cursos GPX

Secuencia:
1. `get_courses`
2. `upload_course` (subir GPX)
3. `delete_course` (si aplica)

---

## 14) FIT avanzado (ciclismo)

Cuando el usuario pide analisis tecnico profundo.

Secuencia prioritaria:
1. `get_activity_fit_data`
2. `get_power_duration_curve`

Descarga de archivos:
- `download_activity_file`
- `set_fit_download_dir`

---

## 15) Challenges, badges y records

Secuencia prioritaria:
- `get_goals`
- `get_personal_record`
- `get_earned_badges`
- `get_badge_challenges`
- `get_non_completed_badge_challenges`
- `get_available_badge_challenges`
- `get_adhoc_challenges`
- `get_inprogress_virtual_challenges`

---

## 16) Salud especifica femenina

- `get_pregnancy_summary`
- `get_menstrual_data_for_date`
- `get_menstrual_calendar_data`

---

## 17) Escritura clinica/manual

- `add_body_composition`
- `add_hydration_data`
- `set_blood_pressure`
- `add_weigh_in`
- `add_weigh_in_with_timestamps`
- `delete_weigh_ins`

---

## 18) Reglas de seleccion para ahorrar tokens

1. Preferir endpoints "summary" antes que endpoints detallados.
- Ejemplo: `get_sleep_summary` antes de `get_sleep_data`.
- Ejemplo: `get_heart_rates_summary` antes de `get_heart_rates`.

2. Limitar cardinalidad desde la llamada.
- `get_activities(limit=1..5)` para conversacion normal.
- Evitar rangos largos si el usuario no los pidio.

3. Evitar duplicar llamadas equivalentes en la misma respuesta.
- Si ya se obtuvo `get_activity` para un id, no repetir salvo error.

4. Escalar en capas.
- Capa 1: summary.
- Capa 2: detalle puntual.
- Capa 3: analitica avanzada (FIT/trends largos).

5. En consultas ambiguas, usar la ruta minima.
- Estado diario -> readiness + body battery + sleep summary.

---

## 19) Plantillas rapidas de enrutado

Caso: "Como estoy hoy"
- `get_morning_training_readiness` -> `get_body_battery` -> `get_sleep_summary` -> `get_hrv_data` -> `get_stress_summary`

Caso: "Analiza mi ultima carrera"
- `get_activities(limit=1)` -> `get_activity` -> `get_training_load_trend`

Caso: "Plan para esta semana"
- `get_training_status` -> `get_training_load_trend` -> `get_weekly_intensity_minutes` -> `get_weekly_stress`

Caso: "Quiero subir un GPX"
- `upload_course` (y opcional `get_courses` para verificar)

Caso: "Analisis ciclista profundo"
- `get_activity_fit_data` -> `get_power_duration_curve`

Caso: "Cuales son mis mejores registros personales en running"
- `get_personal_record` (presentar distancia/record + marca, priorizando type_id de running 1..7)
- Mostrar categorias en español para legibilidad.

Caso: "Y mis mejores marcas en ciclismo"
- `get_personal_record` (presentar solo type_id de ciclismo 8, 9 y 11)
- No mezclar con marcas de running.

Caso: "En que distancias son esas marcas"
- Reusar contexto inmediato de la respuesta anterior de PRs
- Si falta contexto, volver a consultar `get_personal_record`

---

## 20) Mantenimiento del documento

Actualizar este fichero cuando:
- se anadan/quiten tools MCP;
- cambie la estrategia de enrutado;
- se detecten rutas mas eficientes en tokens.

Este archivo esta pensado para consulta rapida en runtime por el agente.

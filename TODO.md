# TODO - Kairos Coach Roadmap

## Estado actual
- Arquitectura activa: DB-first multiusuario con Supabase obligatorio.
- RAG ligero operativo con base de conocimiento del atleta.
- Suite de tests actual: mas de 220 tests.

---

## ✅ Completado

### Hitos tecnicos
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
- Politica de herramientas MCP implementada para runtime: guia de enrutado por intencion (consulta diaria, analisis profundo, planificacion, etc.) y referencia desde el system prompt para reducir tokens y latencia.
- Compatibilidad MCP actualizada para cambios de contrato: get_body_battery y get_body_composition con start_date/end_date.
- Compatibilidad MCP para PRs: endpoint vigente get_personal_record (singular) con alias defensivo del plural.
- Consulta de records personales mejorada: respuesta directa en tabla de running y follow-up contextual de distancias/marcas.
- Consulta de records por deporte mejorada: separacion running/ciclismo (sin mezclar disciplinas si no se pide).
- Categorias de records personales traducidas al espanol en la salida al usuario.
- Separacion explicita objetivo (goals) vs plan activo (training_plan) en el perfil de usuario.
- Estado proactivo de arranque condicionado por training_plan: sin plan muestra "No tienes plan asignado. Que quieres hacer hoy?"; con plan propone ajustar la sesion diaria al plan.
- Ruta determinista para estado del plan en chat: preguntas como "tengo plan?" y "cual es ese plan?" se responden desde training_plan real sin depender de inferencia libre del LLM.
- Prompting reforzado con variantes de intencion para estado de plan y regla explicita: goals no implica plan activo.
- Prompting reforzado para formato de fecha de salida en Espana (DD/MM/AAAA) y para respuestas completas en metricas de maximos/minimos (valor + actividad + fecha).
- Prompting reforzado con checklist MCP minimo por intencion para consultas operativas (estado diario, ajuste de sesion, planificacion/ajuste, dolor/sobrecarga y maximos/minimos).
- Cuantificacion de carga y fatiga implementada (modelo tipo TrainingPeaks): calculo TSS diario/sesion, ATL (7d), CTL (42d) y TSB en runtime con reglas de actuacion por rango individual.
- Persistencia en perfil del atleta de series temporales de carga/fatiga (hasta 120 dias), rangos individualizados y estado semanal para adaptar recomendaciones de forma continua.

### Puntos cerrados

#### 10) [COMPLETADO] MCP solo consulta para coaching
- Prompts alineados: el MCP se usa para consulta de datos y el coach (LLM) hace planificacion/recomendaciones.
- Runtime endurecido: filtrado de tools de escritura en initialize y bloqueo en loop de tool-calls si llega una peticion mutadora.
- Cobertura de tests para garantizar que no se ejecutan tools de escritura en modo read-only.

#### 11) [COMPLETADO] Planes de entrenamiento
- Fase 1 completada: tablas dedicadas en Supabase (training_plan, training_plan_session, training_plan_version) con una sola planificacion activa por usuario.
- Fase 1 completada: capa storage con creacion, actualizacion, activacion, listado, sesiones y versionado automatico por edicion.
- Fase 2 completada: trainer_agent prioriza DB como fuente de verdad del plan activo y mantiene fallback backward-compatible a user_profile.training_plan.
- Fase 2 completada: fallback de planificacion persistido en tablas dedicadas (no solo en perfil).
- Fase 3 completada: comandos de gestion de planes (/plan crear, /plan listar, /plan activar, /plan ver).
- Fase 4 completada: validacion funcional del prompting para generacion/manejo de planes + documentacion y tests de regresion.
- Fase 5 completada (v1 funcional): generacion estructurada en runtime, validacion previa a persistencia, versionado por edicion y resumen de cambios entre versiones.

#### 12) [COMPLETADO] Naming del producto
- Nombre final definido: Kairos Coach.
- Aplicado en toda la base de codigo, prompts, README y documentacion.

#### 13) [COMPLETADO] Cuantificacion de carga y fatiga (TSS/ATL/TSB)
- Modelo implementado en runtime inspirado en TrainingPeaks: TSS por sesion/dia, ATL (7d), CTL (42d) y TSB.
- Integrado en snapshot proactivo de arranque con resumen operativo y regla aplicada de actuacion.
- Series temporales y rangos individualizados persistidos por atleta para detectar tendencia, picos y sobrecarga sostenida.
- Prompting actualizado con reglas obligatorias de decision:
	- fatiga alta/TSB bajo -> reducir carga y priorizar recuperacion.
	- disponibilidad adecuada -> permitir calidad/progresion controlada.
	- sobrecarga sostenida -> activar descarga y prevencion de lesion.

#### 14) [COMPLETADO] Pasos de ejecucion en README.md
- Documentar pasos de ejecucion de la aplicacion en README.md.
- Incluir instrucciones de instalacion, configuracion y uso basico.
- Incluir instrucciones de ejecucion de tests y cobertura.

---

## ⏳ Pendiente

### Prioridad alta

#### 1) Endurecimiento final post-implementacion
- Al terminar implementacion (fuera de diseno y pruebas funcionales), ejecutar bateria de seguridad.
- Incluir: secretos, datos sensibles, configuraciones inseguras, dependencias y transporte.
- Aplicar remediaciones antes de declarar cierre del proyecto.

### Prioridad media

#### 2) Refactor por capas
- Separar claramente presentacion (CLI), negocio (coach) y datos (Garmin/LLM/storage).
- Reducir acoplamiento entre agent/main.py y agent/trainer_agent.py.
- Definir interfaces internas para facilitar cambios de proveedor y testing.

#### 4) Logging de produccion
- Sustituir mensajes debug de consola por logging con niveles configurables.
- Controlar verbosidad por entorno.

### Prioridad baja

#### 5) Dashboard de metricas
- Explorar panel web opcional para tendencias (HRV, VO2max, sueno, estres, carga).
- Evaluar Streamlit como primer candidato.

#### 6) Resumen diario automatizado
- Ejecutar resumen diario programado (Windows Task Scheduler).
- Salida por Telegram o email.

### Backlog abierto

#### 8) Gestion de tokens por proveedor LLM
- Evaluar tabla dedicada de tokens con campo de proveedor para soportar multiples LLM de forma ordenada.

#### 9) Congelado del codigo MCP
- Evaluar vendorizar/congelar el codigo MCP en el repo para evitar roturas por cambios upstream.
- Analizar que tools usamos y cuales no para traernos al codigo solo las que necesitamos.

---

## Notas de mantenimiento
- Mantener TODO sincronizado con decisiones de arquitectura reales.
- Evitar registrar aqui tareas ya completadas salvo resumen corto de hitos.
- Regla de equipo: documentar siempre los cambios antes de hacer commit.

# TODO - GarminCoach Roadmap

## Estado actual
- Arquitectura activa: DB-first multiusuario con Supabase obligatorio.
- RAG ligero operativo con base de conocimiento del atleta.
- Suite de tests actual: 116 tests.

---

## Completado recientemente
- Refactor a persistencia multiusuario en Supabase.
- Login/registro de usuario de aplicacion y onboarding inicial.
- Onboarding enriquecido: creacion/persistencia de athlete_knowledge inicial con perfil + datos MCP.
- Estado proactivo de arranque (48h): body battery, HRV, sueno y entrenamientos recientes.
- Deteccion de cambios de perfil Garmin al iniciar y reporte contextual.
- Prompt y prompt compacto alineados al nuevo enfoque de coach.
- Limpieza de memoria JSON legacy en runtime.
- Documentacion principal alineada con DB-first.

---

## Prioridad alta

### 1) Seguridad de credenciales en almacenamiento
- No persistir passwords Garmin en texto claro en base de datos (cubierto por tests de no-regresion).
- Definir estrategia segura para secretos por usuario (cifrado o flujo sin persistencia de password).
- Actualizar documentacion de seguridad para reflejar el comportamiento real.

### 2) Endurecimiento final post-implementacion
- Al terminar implementacion (fuera de diseno y pruebas funcionales), ejecutar bateria de seguridad.
- Incluir: secretos, datos sensibles, configuraciones inseguras, dependencias y transporte.
- Aplicar remediaciones antes de declarar cierre del proyecto.

---

## Prioridad media

### 3) Refactor por capas
- Separar claramente presentacion (CLI), negocio (coach) y datos (Garmin/LLM/storage).
- Reducir acoplamiento entre `agent/main.py` y `agent/trainer_agent.py`.
- Definir interfaces internas para facilitar cambios de proveedor y testing.

### 4) Politica de herramientas MCP
- Revisar conjunto de herramientas realmente necesarias para el caso entrenador.
- Evitar decision manual "essential vs todas" al inicio cuando no aporte valor.
- Definir politica por contexto (consulta diaria, analisis profundo, planificacion, etc.).

### 5) Logging de produccion
- Sustituir mensajes debug de consola por logging con niveles configurables.
- Controlar verbosidad por entorno.

---

## Prioridad baja

### 6) Dashboard de metricas
- Explorar panel web opcional para tendencias (HRV, VO2max, sueno, estres, carga).
- Evaluar Streamlit como primer candidato.

### 7) Resumen diario automatizado
- Ejecutar resumen diario programado (Windows Task Scheduler).
- Salida por Telegram o email.

### 8) Proveedores LLM adicionales
- Evaluar incorporacion de OpenAI, Ollama y Anthropic segun necesidad real.

---

## Notas de mantenimiento
- Mantener TODO sincronizado con decisiones de arquitectura reales.
- Evitar registrar aqui tareas ya completadas salvo resumen corto de hitos.




Cuando arranca la aplicación comprobar el usuario, si ya existe no debería pedir contraseña ya que la deberiamos tener almacenada y truncada. Esa contraseña se debería de comprobar que se accede con ella a Garmin en vez de volver a pedirla, de esta manera la UX para el usuario es mejor

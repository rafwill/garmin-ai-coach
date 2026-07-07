# TODO - GarminCoach Roadmap

## Estado actual
- Arquitectura activa: DB-first multiusuario con Supabase obligatorio.
- RAG ligero operativo con base de conocimiento del atleta.
- Suite de tests actual: 131 tests.

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
- Analisis profundo de actividad por fecha: pre-fetch enriquecido con zonas FC, hidratacion, sueno y body battery calculados en Python (el LLM solo interpreta y hace coaching).
- Arquitectura de dos capas documentada en system prompt: capa datos vs capa coaching.
- Correccion de busqueda de actividades por fecha (campo start_time snake_case del MCP).
- Auto-login con contrasena cifrada Fernet: al arrancar, si el usuario existe, accede directamente sin pedir password. Flujo de recuperacion si la contrasena de Garmin Connect cambia.

---

## Prioridad alta

### 1) Endurecimiento final post-implementacion
- Al terminar implementacion (fuera de diseno y pruebas funcionales), ejecutar bateria de seguridad.
- Incluir: secretos, datos sensibles, configuraciones inseguras, dependencias y transporte.
- Aplicar remediaciones antes de declarar cierre del proyecto.

---

## Prioridad media

### 2) Refactor por capas
- Separar claramente presentacion (CLI), negocio (coach) y datos (Garmin/LLM/storage).
- Reducir acoplamiento entre `agent/main.py` y `agent/trainer_agent.py`.
- Definir interfaces internas para facilitar cambios de proveedor y testing.

### 3) Politica de herramientas MCP
- Revisar conjunto de herramientas realmente necesarias para el caso entrenador.
- Definir politica por contexto (consulta diaria, analisis profundo, planificacion, etc.).
- Ir directamente a la herramienta concreta segun el tipo de pregunta, reduciendo consumo de tokens y tiempo de respuesta.

### 4) Logging de produccion
- Sustituir mensajes debug de consola por logging con niveles configurables.
- Controlar verbosidad por entorno.

---

## Prioridad baja

### 5) Dashboard de metricas
- Explorar panel web opcional para tendencias (HRV, VO2max, sueno, estres, carga).
- Evaluar Streamlit como primer candidato.

### 6) Resumen diario automatizado
- Ejecutar resumen diario programado (Windows Task Scheduler).
- Salida por Telegram o email.

### 7) Proveedores LLM adicionales
- Evaluar incorporacion de OpenAI, Ollama y Anthropic segun necesidad real.

---

## Notas de mantenimiento
- Mantener TODO sincronizado con decisiones de arquitectura reales.
- Evitar registrar aqui tareas ya completadas salvo resumen corto de hitos.

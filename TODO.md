# TODO - GarminCoach Roadmap

## Estado actual
- Arquitectura activa: DB-first multiusuario con Supabase obligatorio.
- RAG ligero operativo con base de conocimiento del atleta.
- Suite de tests actual: más de 100 tests.

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
- Politica de herramientas MCP implementada para runtime: guia de enrutado por intencion (consulta diaria, analisis profundo, planificacion, etc.) y referencia desde el system prompt para reducir tokens y latencia.
- Compatibilidad MCP actualizada para cambios de contrato: `get_body_battery` y `get_body_composition` con `start_date`/`end_date`.
- Compatibilidad MCP para PRs: endpoint vigente `get_personal_record` (singular) con alias defensivo del plural.
- Consulta de records personales mejorada: respuesta directa en tabla de running y follow-up contextual de distancias/marcas.
- Consulta de records por deporte mejorada: separación running/ciclismo (sin mezclar disciplinas si no se pide).
- Categorías de récords personales traducidas al español en la salida al usuario.
- Separación explícita objetivo (`goals`) vs plan activo (`training_plan`) en el perfil de usuario.
- Estado proactivo de arranque condicionado por `training_plan`: sin plan muestra "No tienes plan asignado. ¿Qué quieres hacer hoy?"; con plan propone ajustar la sesión diaria al plan.
- Ruta determinista para estado del plan en chat: preguntas como "¿tengo plan?" y "¿cuál es ese plan?" se responden desde `training_plan` real sin depender de inferencia libre del LLM.
- Prompting reforzado con variantes de intención para estado de plan y regla explícita: `goals` no implica plan activo.
- Prompting reforzado para formato de fecha de salida en España (`DD/MM/AAAA`) y para respuestas completas en métricas de máximos/mínimos (valor + actividad + fecha).
- Punto 10 cerrado: MCP en modo coach solo consulta. Política explícita en prompts y enforcement técnico en runtime con bloqueo de tools de escritura.

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


## Notas de mantenimiento
- Mantener TODO sincronizado con decisiones de arquitectura reales.
- Evitar registrar aqui tareas ya completadas salvo resumen corto de hitos.


### 8) ANtiguamente guardabamos los tokens en una tabla llamda gemini o algo similar. TEndría sentido diferenciar en bbdd una tabla con toknes y en esa tabla cada uno de los proveedores de LLM?


Seria necesario diferenciar en la base de datos una tabla para tokens y dentro de esa tabla, tener un campo que indique el proveedor de LLM correspondiente a cada token. Esto permitiría gestionar múltiples proveedores de manera más organizada y facilitaría la administración de tokens según el proveedor utilizado.

### 9) Congelado del código del MCP

Seria necesario bajar a este proyecto el codigo del mcp para evitar posibles cambios y que algo no funcione en el futuro. Esto permitiría tener un control total sobre la versión del MCP que se está utilizando y evitar problemas de compatibilidad o cambios inesperados en la API que puedan afectar al funcionamiento del proyecto.

### 10) [COMPLETADO] MCP solo consulta para coaching
Implementado en prompts y en ejecución:
- Prompts alineados: el MCP se usa para consulta de datos y el coach (LLM) hace planificación/recomendaciones.
- Runtime endurecido: filtrado de tools de escritura en `initialize` y bloqueo en loop de tool-calls si llega una petición mutadora.
- Cobertura de tests añadida para garantizar que no se ejecutan tools de escritura en modo read-only.


### 11) Planes de entrenamiento

LA creación de un plan de entrenamiento es un proceso complejo que requiere tener en cuenta múltiples factores, como el nivel de condición física del atleta, sus objetivos, su disponibilidad de tiempo, su historial de lesiones y su capacidad de recuperación. Para crear un plan de entrenamiento efectivo, es importante seguir un enfoque estructurado y personalizado que se adapte a las necesidades individuales del atleta. Deberian de guardarse en la base de datos los planes de entrenamiento creados para que el agente pueda acceder a ellos y hacer recomendaciones basadas en el plan de entrenamiento del atleta. Deberiamos de tener en cuenta que el plan de entrenamiento puede cambiar a lo largo del tiempo, por lo que el agente debe ser capaz de adaptarse a los cambios y hacer recomendaciones actualizadas en función del plan de entrenamiento vigente. Cada plan debería tener un titulo, una descripción, un objetivo, un nivel de dificultad, una duración y un conjunto de sesiones de entrenamiento. Cada sesión debería tener un tipo de entrenamiento (carrera, fuerza, movilidad, etc.), una duración, una intensidad y un conjunto de ejercicios específicos. El agente debería ser capaz de analizar el plan de entrenamiento y hacer recomendaciones personalizadas para cada sesión en función del estado físico del atleta y su progreso a lo largo del tiempo.EL atleta podría cambiar el plan de entrenamiento en cualquier momento, por lo que el agente debería ser capaz de adaptarse a los cambios y hacer recomendaciones actualizadas en función del plan de entrenamiento vigente; de ser así, guardariamos el nuevo plan en un registro diferente por si el atleta quiere volver al plan anterior. El agente deberia fijarse tambien en marcas personales y records del atleta para hacer recomendaciones personalizadas en función de su nivel de rendimiento y sus objetivos. El agente debería ser capaz de analizar los datos del atleta y hacer recomendaciones personalizadas para cada sesión en función de su estado físico, su progreso y sus objetivos a largo plazo. El agente debería ser capaz de identificar patrones en el rendimiento del atleta y hacer recomendaciones para mejorar su rendimiento a lo largo del tiempo. El agente debería ser capaz de identificar áreas de mejora en el plan de entrenamiento y hacer recomendaciones para optimizar el plan en función de los objetivos del atleta. El agente debería ser capaz de proporcionar retroalimentación continua al atleta sobre su progreso y su rendimiento, y hacer recomendaciones para mejorar su rendimiento a lo largo del tiempo. El agente debería ser capaz de adaptarse a los cambios en el estado físico del atleta y hacer recomendaciones actualizadas en función de su progreso y sus objetivos a largo plazo.

### 21) Que nombre le damos a esta aplicación?
# TODO — Mejoras futuras de GarminCoach

---

## ✅ Completado

| # | Tarea |
|---|---|
| 2 | **Perfil de usuario enriquecido** — sync automático desde Garmin, setup inicial de objetivos/salud, comandos `/perfil`, `/perfil editar objetivo`, `/perfil editar salud` |
| — | **Separación de memoria** — `user_profile.json` (perfil) y `session_context.json` (historial + resúmenes) |
| — | **SSL / Zscaler** — fix para garmin-mcp (zscaler-ca.pem) y LLM clients (truststore) |
| — | **Auto-detección de red** — `_detect_zscaler()` selecciona GitHub Models (VPN) o mejor proveedor libre |
| — | **Límite de iteraciones** — `_MAX_TOOL_ITER=15` en `chat()` para evitar bucle infinito |
| — | **system_prompt.md reescrito** — herramientas reales del MCP, protocolos DT1, protocolos de análisis correctos |
| — | **`activityId` fuera del strip set** — el LLM puede encadenar `get_activities → get_activity` |
| — | **Creación automática de `memory/`** — `MEMORY_DIR.mkdir()` al importar el módulo |
| 10 | **Validación de inputs del setup** — `target_race_date` (YYYY-MM-DD + fecha futura), `target_time` (H:MM:SS), `weekly_training_hours` (0.5–40), bucle de reintento con mensaje de error |
| 11 | **Comando `/ayuda`** — ejemplos de preguntas, lista de comandos y guía rápida de indicadores (Body Battery, Readiness, HRV, Training Status) |
| 7  | **Tests automatizados** — 93 tests, 0 fallos. `tests/test_trainer_agent.py` (funciones puras + Gemini mock) y `tests/test_main.py` (validaciones + identidad) |
| 1B | **Supabase** — `agent/storage.py` unifica la persistencia: Supabase como primario (3 tablas: `user_profile`, `session_context`, `gemini_usage`) + fallback automático a JSON local si no está configurado. `supabase/schema.sql` listo para ejecutar. |
| 14 | **Formatear salida Coach** — salida normalizada a Markdown estructurado (secciones, tablas y emojis): reglas añadidas en `prompts/system_prompt*.md` y fallback en `agent/main.py` para convertir texto plano a formato legible en terminal/Telegram/email. |

---

## 🗄️ ~~1. Migrar almacenamiento de disco a base de datos~~ ✅ Completado

---

## 📊 3. Dashboard web de métricas

Visualización de tendencias (HRV, VO₂max, sueño, estrés) en una interfaz web sencilla.
- Opciones: **Streamlit** (más rápido), Gradio, o panel estático con Chart.js
- Datos fuente: `session_context.json` + llamadas directas al MCP
- Podría correr como proceso paralelo junto al agente de terminal

---

## 🔔 4. Notificaciones / resumen diario automático

Ejecutar el agente en modo automático cada mañana (tarea programada) para:
- Obtener estado del día (readiness, body battery, sueño)
- Generar resumen del entrenamiento del día anterior
- Enviar por **Telegram** (bot API, gratuito) o email (SMTP)
- En Windows: tarea programada con el Programador de tareas o `schtasks`

---

## 🤖 6. Soporte para más proveedores LLM

- **OpenAI** (`gpt-4o`) — para usuarios con cuenta de pago
- **Ollama** (modelos locales: llama3, mistral) — sin conexión a internet ni coste, ideal para datos sensibles
- **Anthropic Claude** — excelente para análisis largos y razonamiento médico

---

## ~~🧪 7. Tests automatizados~~ ✅ Completado

93 tests, 0 fallos — `pytest tests/` en < 6 s:
- `tests/test_trainer_agent.py`: `_seconds_to_hhmmss`, `_normalize_date_args`, `_strip_garmin_object`, `_compact_tool_result`, `_compact_personal_records`, `_clean_schema_for_gemini`, `_GeminiCompletions._parse`
- `tests/test_main.py`: `_validate_date`, `_validate_time`, `_validate_hours`, `_garmin_user_id`, `_is_first_time`

---

## 🔓 8. Eliminar detección automática de Zscaler

**Cuándo:** Una vez aprobada en MyIT la solicitud de acceso a dominios de IA generativa.

**Pasos:**
1. Verificar que `generativelanguage.googleapis.com`, `api.mistral.ai`, etc. son accesibles desde VPN.
2. Eliminar `_detect_zscaler()`, `_best_available_provider()` y `_auto_select_provider()` de `agent/main.py`.
3. Decidir proveedor por defecto único o restaurar selección manual con `_ask_provider()`.
4. Limpiar `_PLACEHOLDER_VALUES` si ya no se usa en otro lugar.
5. `zscaler-ca.pem` ya no necesario (añadir a `.gitignore`).

---

## 🖨️ 9. Eliminar mensajes de debug en producción

Los `print(f"  [debug] ...")` en `trainer_agent.py` y `main.py` son útiles en desarrollo pero ensucian la interfaz.

**Opciones:**
- Variable de entorno `DEBUG=1` para activarlos selectivamente
- Usar el módulo `logging` con nivel configurable (`logging.DEBUG` / `logging.INFO`)
- Ficheros afectados: `agent/trainer_agent.py` (chat loop), `agent/main.py` (tokens de sesión)

---

## 📅 ~~10. Validación de inputs del setup inicial~~ ✅ Completado

---

## 💬 ~~11. Comando `/ayuda` en el chat~~ ✅ Completado

---

## 📈 12. Historial de evolución de peso

Aprovechar `get_body_composition` para guardar localmente el peso de cada sesión y mostrar la evolución con el comando `/peso` o al pedir análisis de composición corporal. Especialmente útil en DT1 donde el peso fluctúa con la glucemia.

---

## 🗃️ 13. Modelado completo de la base de datos

**Objetivo:** Supabase como fuente de verdad. Los ficheros JSON locales pasan a ser caché de solo lectura (datos actuales de sesión), no almacenamiento primario.

**Qué hay que modelar/migrar a Supabase:**
- **Credenciales ofuscadas** — email Garmin (hash), tokens OAuth Garmin (`~/.garminconnect`) con cifrado en reposo
- **Secrets/API keys** — claves LLM hasheadas o cifradas (ahora solo están en `.env` local)
- **Perfil completo del usuario** — ya en `user_profile` ✅, revisar si falta algún campo
- **Historial de sesiones** — ya en `session_context` ✅, valorar particionar por fecha para no crecer sin límite
- **Uso de tokens por proveedor** — ahora solo se trackea Gemini; añadir Mistral, Groq, Cerebras
- **Historial de actividades cacheado** — evitar re-pedir a Garmin datos que no cambian (actividades pasadas)

**Modelo de caché local propuesto:**
- Solo persisten en JSON los datos de la sesión activa (perfil cargado al arrancar, historial de los últimos N mensajes)
- Al arrancar: cargar desde Supabase → escribir caché local
- Al guardar: escribir en Supabase primero → actualizar caché local
- Sin Supabase: comportamiento actual (JSON como fuente de verdad)

**Consideraciones de seguridad:**
- Nunca almacenar contraseñas en claro (ni local ni en BD)
- Tokens OAuth: cifrar con clave derivada del email (AES-256 o Fernet)
- API keys LLM: valorar guardar solo el hash para tracking de cuota, nunca el valor real en BD
- Row Level Security (RLS) de Supabase para aislar datos por `garmin_user_id`

## ~~🗃️ 14. Formatear salida Coach~~ ✅ Completado
Salida normalizada con dos capas:
- **Capa de instrucción**: reglas de formato obligatorio en `system_prompt.md` y `system_prompt_compact.md`.
- **Capa de seguridad**: fallback `_format_coach_markdown()` en `agent/main.py` para envolver respuestas en texto plano y convertir multilínea a bullets Markdown.


## 15. Refactorizar la aplicación
La apicación quiero que sirva como un entrenador. Quiero que se comporte como un entrenador personal, que pueda dar consejos, motivación y seguimiento de los entrenamientos. Para ello, se necesita un refactor completo del código para separar la lógica de negocio, la interfaz de usuario y la comunicación con Garmin y LLMs. Esto permitirá añadir nuevas funcionalidades más fácilmente y mantener el código limpio y mantenible. Se necesitaria un architectura basada en capas, con una capa de presentación (interfaz de usuario), una capa de negocio (lógica del entrenador) y una capa de datos (comunicación con Garmin y LLMs). Además, se debería implementar un sistema de pruebas unitarias y de integración para asegurar la calidad del código. Se necesitarian varios agentes especializados, uno para la comunicación con Garmin, otro para la comunicación con LLMs y otro para la lógica del entrenador. Cada agente debería tener su propia interfaz y ser capaz de comunicarse con los demás agentes de manera eficiente. Uno de los agentes tendrá como fuente de información un fichero de datos sobre la persona entrenada. Ese fichero contendrá información sobre el perfil del usuario, sus objetivos, su historial de entrenamientos y cualquier otra información relevante para el entrenador como patologias. El agente encargado de la lógica del entrenador utilizará esta información para generar recomendaciones personalizadas y motivación para el usuario (esto anula el fichero de settings.json). También se debería implementar un sistema de seguimiento de objetivos, donde el usuario pueda establecer metas a corto y largo plazo y recibir retroalimentación sobre su progreso hacia esas metas. Además, se debería permitir al usuario registrar sus entrenamientos y ver estadísticas detalladas sobre su rendimiento, incluyendo gráficos y análisis de tendencias. Revisar cuales de las 130 tools son realmente necesarias para esto y evitar tener que preguntar al inicio por las essentials o las completas. Revisión de prompts

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


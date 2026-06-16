# TODO — Mejoras futuras de GarminCoach

## 🗄️ 1. Migrar almacenamiento de disco a base de datos

**Problema actual:** La memoria del agente (historial, resúmenes de sesión, perfil de usuario) se guarda en `memory/user_profile.json`. Esto escala mal, no permite consultas, y no es apto para múltiples usuarios o acceso remoto.

**Solución propuesta:** Sustituir el JSON en disco por una base de datos. Dos opciones:

### Opción A — SQLite (local, sin infraestructura)
- Cero dependencias externas, fichero único `.db` en disco
- Ideal si el agente siempre corre en la misma máquina
- Librería: `aiosqlite` (async) o `sqlite3` (stdlib)
- Tablas sugeridas: `sessions`, `session_summaries`, `user_profile`, `history`

### Opción B — Supabase (cloud, multiusuario)
- PostgreSQL gestionado con API REST y SDK Python (`supabase-py`)
- Permite acceder al historial desde cualquier dispositivo
- Capa gratuita: 500 MB de base de datos, 2 GB de storage
- Registro: https://supabase.com → nuevo proyecto → obtener `SUPABASE_URL` y `SUPABASE_ANON_KEY`
- Variables de entorno a añadir al `.env`:
  ```
  SUPABASE_URL=https://xxxx.supabase.co
  SUPABASE_ANON_KEY=eyJ...
  ```

**Ficheros afectados:**
- `agent/trainer_agent.py` — funciones `_load_user_profile`, `_save_history_entry`, `_persist_session_summary`, `_load_session_summaries`
- `agent/trainer_agent.py` — `_get_gemini_daily_file`, `update_gemini_daily_usage`, `mark_gemini_quota_exhausted`
- `requirements.txt` — añadir `aiosqlite` o `supabase`

---

## 🧠 2. Perfil de usuario enriquecido

Permitir al agente preguntar y persistir datos personales del usuario (nombre, edad, peso, objetivos, carreras objetivo, historial de lesiones) para personalizar las recomendaciones sin repetir la misma información en cada sesión.

---

## 📊 3. Dashboard web de métricas

Visualización de tendencias (HRV, VO₂max, sueño, estrés) en una interfaz web sencilla. Opciones: Streamlit, Gradio, o panel estático con Chart.js.

---

## 🔔 4. Notificaciones / resumen diario automático

Ejecutar el agente en modo automático cada mañana (tarea programada) para generar un resumen del día anterior y enviarlo por email o Telegram.

---

## 🔄 5. Sincronización multi-dispositivo

Si se migra a Supabase (punto 1B), el historial y los resúmenes estarían disponibles desde el móvil, tablet o cualquier PC sin configuración adicional.

---

## 🤖 6. Soporte para más proveedores LLM

- **OpenAI** (gpt-4o) — para usuarios con cuenta de pago
- **Ollama** (modelos locales: llama3, mistral) — sin conexión a internet ni coste
- **Anthropic Claude** — excelente para análisis largos

---

## 🧪 7. Tests automatizados

Añadir tests unitarios para las funciones críticas:
- Normalización de fechas (`_normalize_date_args`)
- Compactación de resultados Garmin (`_compact_tool_result`)
- Parseo de respuestas Gemini (`_GeminiCompletions._parse`)

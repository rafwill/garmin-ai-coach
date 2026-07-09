# 🏃‍♂️ Garmin AI Coach

**Garmin AI Coach** es un asistente y entrenador deportivo personal inteligente. Combina modelos de lenguaje (LLM) con tus datos deportivos reales de Garmin a través del **servidor MCP [`garmin_mcp`](https://github.com/Taxuspt/garmin_mcp)**, usando binario local cuando está instalado y `uvx` como fallback.

El agente analiza tus métricas de rendimiento (VO2Max, HRV, sueño, SPO2, umbral de lactato, puntuación de resistencia...), tus récords personales históricos y tus actividades recientes para darte recomendaciones personalizadas, planes de entrenamiento y análisis de ritmos **100% basados en tus datos reales de Garmin Connect**. Con memoria persistente entre sesiones, recuerda lo que habéis hablado en conversaciones anteriores.

---

## ✨ Características clave

* **🧠 Seis proveedores de IA:**
  | # | Opción | Modelo | Límite gratuito | Requiere |
  |---|--------|--------|-----------------|----------|
  | 1 | **Google Gemini** | `gemini-2.0-flash` | ~1M tokens/día | API key gratuita |
  | 2 | **Mistral** | `mistral-small-latest` | ~1B tokens/mes | API key gratuita |
  | 3 | **Groq** | `llama-3.3-70b-versatile` | 100k tokens/día | API key gratuita |
  | 4 | **Cerebras** | `llama-3.3-70b` | generoso | API key gratuita |
  | 5 | **NVIDIA NIM** | `meta/llama-3.1-70b-instruct` | generoso | API key gratuita |
  | 6 | **GitHub Models** | `gpt-4o-mini` | — | GitHub token + VPN |

  La red se **detecta automáticamente** y te despliega un **menú interactivo** para que selecciones el modelo que quieras usar (también dentro de VPN corporativa con Zscaler, incluyendo GitHub Models si está configurado). Además te permite **cambiar de modelo en caliente** en cualquier momento del chat con el comando `/modelo`.

* **🪵 Logging y compatibilidad Windows:**
  - El agente escribe logs en `agent.log` con timestamps y nivel de severidad.
  - En Windows, la salida de consola se fuerza a UTF-8 para evitar errores de Unicode.

* **⌚ Herramientas de Garmin Connect:**
  - Actividades, zonas FC, splits, progreso, récords personales
  - Salud diaria: frecuencia cardíaca, body battery, estrés, pasos, respiración, SPO2
  - Métricas avanzadas: HRV, VO2Max, predicciones de carrera, umbral de lactato, puntuación de resistencia, edad de fitness
  - Sueño, composición corporal, hidratación, perfil de usuario y objetivos

* **🔒 Política MCP solo consulta (modo coach):**
  - Por defecto, el agente opera con `MCP_READ_ONLY=true`.
  - Las tools MCP de escritura se filtran y bloquean en runtime (`create_`, `update_`, `delete_`, `schedule_`, `upload_`, `add_`, `set_`).
  - El MCP aporta datos; la planificación y recomendaciones las realiza el coach (LLM).
  - El prompting incluye checklist MCP mínimo por intención (estado diario, ajuste de sesión, planificación/ajuste de plan, dolor/sobrecarga y máximos/mínimos) para reducir respuestas genéricas.
  - Solo para mantenimiento/admin se puede desactivar con `MCP_READ_ONLY=false`.

* **👤 Perfil de usuario sincronizado:**
  - Al arrancar, sincroniza automáticamente género, peso, altura y edad desde Garmin Connect.
  - Detecta y reporta cambios de perfil Garmin al inicio de sesión (si los hay).
  - Setup guiado la primera vez: deporte principal, horas/semana, próximo evento, tiempo objetivo y condiciones de salud.
  - Todos los campos del perfil se inyectan en el system prompt para que el agente te conozca desde el primer mensaje.
  - El perfil se mantiene por usuario de aplicación (multiusuario) y no se reinicia automáticamente por cambio de cuenta Garmin.
  - El perfil diferencia entre:
    - `goals`: objetivo deportivo (carrera, fecha, tiempo, horas/semana).
    - `training_plan`: plan activo para el día a día (separado del objetivo).

* **📚 Base de conocimiento del atleta (RAG ligero):**
  - Puedes añadir notas personales del atleta en ficheros `.md`, `.txt` o `.json`.
  - En onboarding de usuario nuevo, se genera y persiste una base inicial enriquecida con perfil + datos MCP de arranque.
  - En cada consulta, el agente recupera los fragmentos más relevantes y los combina con el perfil Garmin y los datos en tiempo real de herramientas.
  - Si no defines rutas, intenta cargar automáticamente:
    - `memory/athlete_knowledge.md`
    - `memory/athlete_knowledge.txt`
    - `memory/athlete_knowledge.json`

* **� Cuantificación de carga y fatiga (TSS/ATL/CTL/TSB):**
  - Al arrancar la sesión, el sistema calcula automáticamente el modelo de carga inspirado en TrainingPeaks:
    - **TSS** (Training Stress Score): carga por sesión y acumulada diaria.
    - **ATL** (fatiga aguda, ventana 7 días por defecto): cuánto estás acumulando a corto plazo.
    - **CTL** (fitness crónico, ventana 42 días por defecto): tu nivel de forma construido en semanas/meses.
    - **TSB** (forma = CTL − ATL): disponibilidad real para entrenar hoy.
  - Los **tau** (constantes de tiempo) y **percentiles** se ajustan automáticamente al deporte principal del perfil:
    | Deporte | ATL tau | CTL tau | Percentiles TSB/ATL |
    |---------|--------:|--------:|---------------------|
    | Running | 7 días | 42 días | estándar |
    | Trail running | 8 días | 42 días | más amplios (sesiones largas) |
    | Ciclismo | 7 días | 45 días | estándar |
    | Triatlón | 7 días | 45 días | más amplios |
  - Los parámetros se pueden **sobreescribir manualmente** en `profile.load_metrics.model`.
  - Genera **rangos individualizados** por atleta usando percentiles de sus propios datos históricos (no umbrales genéricos).
  - **Reglas de actuación automáticas** visibles en el estado proactivo:
    - 🟠 Fatiga alta (TSB por debajo del rango individual) → reduce intensidad/volumen.
    - 🟢 Buena disponibilidad (TSB en rango) → permite calidad o progresión controlada.
    - 🔴 Sobrecarga sostenida → activa descarga y recomendaciones preventivas de lesión.
  - La serie temporal completa (hasta 120 días) se persiste en el perfil del atleta en Supabase para análisis de tendencias.
  - El bloque de carga/fatiga se incluye en el estado proactivo de arranque con resumen operativo (TSS·ATL·CTL·TSB·semana) y la regla aplicada.

* **�🚦 Estado proactivo al iniciar (48h):**
  - Tras seleccionar modelo y conectar herramientas, muestra un briefing automático de últimas 48h.
  - Incluye estado de Body Battery, HRV, sueño y entrenamientos recientes.
  - Muestra fechas analizadas en formato `DD/MM/AAAA`.
  - Recomendación inicial condicional:
    - Sin `training_plan` activo: `No tienes plan asignado. ¿Qué quieres hacer hoy?`
    - Con `training_plan` activo: propone adaptar la sesión de hoy al plan.
  - Sirve como punto de partida antes de la primera pregunta del chat.

* **🧭 Estado de plan coherente (sin alucinaciones):**
  - Preguntas tipo "¿tengo plan?", "¿cuál es ese plan?" o "¿sigo con el plan?" se responden por ruta determinista.
  - La respuesta se basa en `training_plan` real en base de datos (no en inferencias del LLM).
  - `goals` se muestra como objetivo guardado, pero no se interpreta como plan activo.

* **🗂️ Planes de entrenamiento versionados (DB-first):**
  - Los planes se guardan en tablas dedicadas de Supabase (`training_plan`, `training_plan_session`, `training_plan_version`).
  - Cada edición del plan genera una nueva versión (snapshot) para trazabilidad.
  - Generación/ajuste funcional de planes por ruta determinista en runtime (sin depender del LLM para persistir/activar).
  - Validación previa de coherencia (duración, sesiones, carga semanal y rangos de día) antes de guardar.
  - Resumen de cambios entre versiones (duración, dificultad, sesiones y volumen semanal) visible en la respuesta del coach.
  - Existe una única fuente de verdad de plan activo por usuario (máximo uno activo a la vez).
  - Compatibilidad backward: el perfil mantiene `training_plan` como espejo temporal para rutas legacy.

* **🥇 Récords personales de running (mejorado):**
  - Consulta directa de PRs desde Garmin con `get_personal_record`.
  - Respuesta en tabla con distancia/record y marca desde la primera interacción.
  - Categorías traducidas al español para facilitar lectura.
  - Follow-up contextual soportado (ej: "en qué distancias son esas marcas") sin perder acceso a datos.
  - Filtrado priorizando registros de running para evitar mezclar ciclismo/natación en esa consulta.

* **🚴 Récords por deporte (running/ciclismo):**
  - Si el usuario pregunta por ciclismo, solo se muestran marcas de ciclismo.
  - Si pregunta por running, solo se muestran marcas de running.
  - Nunca se mezclan disciplinas en la misma respuesta salvo petición explícita.

* **✅ Validación de inputs:**
  - `target_race_date`: formato `YYYY-MM-DD` + debe ser fecha futura.
  - `target_time`: formato `H:MM:SS` / `HH:MM:SS` con rangos de minutos/segundos.
  - `weekly_training_hours`: número entre 0.5 y 40, acepta coma o punto decimal.
  - Bucle de reintento con mensaje de error en color hasta que el valor sea válido.

* **🔐 Auto-login con contraseña cifrada:**
  - Al arrancar, solo se pide el nombre de usuario. Si ya existe, accede **automáticamente** sin volver a pedir contraseña.
  - La contraseña se almacena cifrada (Fernet AES-128 + HMAC-SHA256) en Supabase — nunca en texto claro.
  - Si la contraseña de Garmin Connect cambia, el sistema lo detecta y ofrece un flujo de actualización sin perder la sesión.
  - La política de seguridad es: contraseña de la app = contraseña de Garmin Connect (una sola contraseña para todo).

* **📊 Análisis profundo de actividades por fecha:**
  - Pregunta directamente: *"Analiza mi competición del 2 de julio"* y el agente localiza la actividad automáticamente.
  - Pre-fetch enriquecido: antes de llamar al LLM, el sistema carga actividad + body battery + sueño previo + HRV + carga de entrenamiento.
  - Todos los cálculos (zonas de FC Z1–Z5 con % y minutos, ritmo en min/km, hidratación estimada, efecto de entrenamiento) se realizan **en Python**, no en el LLM.
  - El LLM recibe un bloque estructurado pre-computado y se dedica exclusivamente a interpretar y hacer coaching.

* **🧠 Arquitectura de dos capas (datos + coaching):**
  - **Capa de datos**: conecta con Garmin Connect, pre-procesa y formatea todas las métricas antes de pasarlas al LLM.
  - **Capa de coaching (LLM)**: recibe datos ya calculados y aporta interpretación, contextualización con el perfil del atleta y recomendaciones accionables.
  - Esta separación está documentada en el system prompt y garantiza que el LLM nunca intente calcular cosas que ya ha hecho el sistema.

* **💾 Memoria persistente entre sesiones:**
  - Al salir, el agente genera automáticamente un resumen compacto de la sesión con el propio LLM.
  - Los últimos 5 resúmenes se inyectan como contexto al arrancar la siguiente sesión — el agente recuerda lo que habéis hablado.
  - Todo el estado de usuario (perfil, historial, base de conocimiento y cuota de Gemini) se guarda en Supabase por usuario.
  - Si el agente crea una planificación base por fallback, persiste un `training_plan` activo mínimo para distinguirlo del objetivo (`goals`).

* **👥 Modo multiusuario (nuevo):**
  - Inicio con `login` o alta de `usuario nuevo` desde terminal.
  - Cada usuario tiene su propio perfil, objetivos, contexto, base de conocimiento y claves en BBDD.
  - En usuarios nuevos, el onboarding conecta con Garmin, sincroniza biometría y crea base de conocimiento inicial.

* **🔧 Sin dependencias de Node.js:**
  - El servidor MCP es 100% Python, lanzado por `garmin-mcp` local o `uvx` en fallback.

---

## 🛠️ Requisitos previos

| Requisito | Versión mínima | Notas |
|-----------|---------------|-------|
| **Python** | 3.10+ | Desarrollado con 3.13 |
| **uv / uvx** | cualquiera | Fallback para arrancar el servidor MCP |
| **Cuenta Garmin Connect** | — | Credenciales de acceso |
| **API key** de Mistral, Groq, Gemini o Cerebras | — | Gratuitas (ver `.env.example`) |

### Instalar uv (si aún no lo tienes)
```powershell
pip install uv
```

---

## 🚀 Instalación y Configuración

### 1. Clonar y preparar el entorno Python
```powershell
git clone https://github.com/rafwill/garmin-ai-coach.git
cd garmin-ai-coach

python -m venv .venv
.venv\Scripts\Activate.ps1

pip install -r requirements.txt
```

### 2. Configurar variables de entorno
```powershell
cp .env.example .env
```
Edita `.env` y rellena al menos:
- `GARMIN_EMAIL` y `GARMIN_PASSWORD` (solo para pre-autenticación OAuth inicial)
- La API key del proveedor que uses (`GEMINI_API_KEY`, `GROQ_API_KEY` o `GITHUB_TOKEN`)
- `SUPABASE_URL` y `SUPABASE_ANON_KEY`
- `ENCRYPTION_KEY` (genera una vez con el comando de abajo):

```powershell
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

*(Consulta los comentarios en `.env.example` para obtener las URLs de registro de cada proveedor.)*

### 2.1 (Opcional) Activar base de conocimiento del atleta
Puedes crear uno o varios ficheros con conocimiento personal (historial, preferencias, estrategia de carrera, limitaciones, nutrición, etc.) y referenciarlos en `.env`:

```dotenv
ATHLETE_KB_PATHS=memory/athlete_knowledge.md,memory/pda_strategy.json
```

Formato recomendado:
- `.md` / `.txt`: texto libre estructurado por secciones.
- `.json`: objetos o listas con campos descriptivos (`objetivos`, `lesiones`, `nutricion`, `estrategia_carrera`, etc.).

Si no configuras `ATHLETE_KB_PATHS`, el agente intenta cargar automáticamente los ficheros por defecto en `memory/`.

### 3. Pre-autenticar con Garmin Connect *(una sola vez)*
Este paso guarda los tokens OAuth en `~/.garminconnect` para que el agente no necesite tu contraseña en cada arranque:
```powershell
$env:GARMIN_EMAIL="tu@email.com"
$env:GARMIN_PASSWORD="tu_contraseña"
uvx --python 3.12 --from git+https://github.com/Taxuspt/garmin_mcp garmin-mcp-auth
```
> Los tokens son válidos aproximadamente **6 meses**. Repite este paso cuando expiren.

### 4. Configurar Supabase (obligatorio)

El modo actual del agente es **DB-first multiusuario**: requiere Supabase para arrancar.

1. Crea un proyecto gratuito en [supabase.com](https://supabase.com) (500 MB, sin tarjeta).
2. Ve a **SQL Editor → New query**, pega el contenido de [`supabase/schema.sql`](supabase/schema.sql) y pulsa **Run**.
3. En **Settings → API** copia tu *Project URL* y *anon public key*.
4. Añade al `.env`:
   ```dotenv
   SUPABASE_URL=https://xxxxxxxxxxxx.supabase.co
   SUPABASE_ANON_KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
   ```

> Si las variables no están configuradas o Supabase no es accesible, el agente mostrará error y no arrancará.

Script disponible para Supabase:
- [`supabase/schema.sql`](supabase/schema.sql): crea el esquema multiusuario para una instalacion limpia.

Incluye además el modelo de planificación versionada:
- `training_plan`: cabecera del plan (título, objetivo, estado, metadatos).
- `training_plan_session`: sesiones por semana/día (tipo, duración, intensidad, ejercicios).
- `training_plan_version`: historial de snapshots por edición/activación.

### 5. Inicio de sesión multiusuario
Al arrancar `python -m agent.main`:
- Se pide el **nombre de usuario**.
- Si el usuario ya existe: **acceso automático** sin contraseña — mensaje *"Usuario encontrado · Accediendo automáticamente"*.
- Si es nuevo: flujo de registro con explicación de la política de contraseña única (app = Garmin Connect).
- Si la contraseña de Garmin Connect ha cambiado: el sistema lo detecta y ofrece actualización sin salir.
- Se precarga el perfil del usuario desde BBDD y se sincroniza Garmin para completar datos personales.
- En usuarios nuevos, se crea una KB inicial enriquecida (perfil + snapshot MCP de 48h).
- En usuarios existentes, se muestra un estado proactivo automático de 48h al inicio.

---

## 🏃‍♂️ Uso

```powershell
python -m agent.main
```

El agente iniciará el servidor MCP con `garmin-mcp` local o con `uvx` como fallback. Después aparecerá el menú de proveedores:

```
  1 · GitHub Models (gpt-4o-mini)           — dentro de VPN
  2 · Groq         (llama-3.3-70b)          — 100k tokens/día
  3 · Google Gemini (gemini-2.0-flash)      — ~1M tokens/día gratis
  4 · Mistral      (mistral-small)          — gratis · function calling nativo  ← recomendado
  5 · Cerebras     (llama-3.3-70b)          — ultrarrápido · gratis
  6 · NVIDIA NIM   (llama3-70b-instruct)    — gratis · API compatible OpenAI
```

A continuación se selecciona el modo de herramientas y el agente conecta con Garmin Connect.

### Comandos disponibles en el chat

| Comando | Descripción |
|---------|-------------|
| `/ayuda` · `/help` · `/?` | Muestra ejemplos de preguntas, todos los comandos y guía de indicadores |
| `/perfil` | Muestra el perfil actual (datos personales + objetivos + salud) |
| `/modelo` · `/model` | Muestra estadísticas de tokens y te permite **cambiar de modelo de IA en caliente** sin perder la sesión |
| `/perfil editar` | Edita todos los campos del perfil |
| `/perfil editar objetivo` | Edita solo los objetivos de entrenamiento |
| `/perfil editar salud` | Edita solo los datos de salud |
| `/plan listar` | Lista planes de entrenamiento y marca el activo |
| `/plan ver <plan_id>` | Muestra detalle del plan y sus sesiones |
| `/plan activar <plan_id>` | Activa un plan y desactiva el anterior |
| `/plan crear` | Crea y activa un plan base persistido en Supabase |
| `/carga` | Tabla semanal de carga/fatiga (TSS · ATL · CTL · TSB) de las últimas 8 semanas |
| `/carga meses` | Vista mensual de carga/fatiga de los últimos 3 meses |
| `salir` | Guarda el resumen de sesión y cierra el agente |

### Ejemplos de preguntas
- *"¿Cuál ha sido mi mejor ritmo en media maratón y qué necesito para bajar de 1h45?"*
- *"Analízame como deportista usando mis métricas de la última semana"*
- *"¿Cuál es mi VO2Max actual y cómo ha evolucionado?"*
- *"Dame un plan de entrenamiento para la próxima carrera de 10K"*
- *"¿Cómo ha sido mi sueño y HRV esta semana?"*
- *"¿Qué indicadores debo vigilar esta noche como diabético tipo 1 tras el entrenamiento?"*

---

## 🧩 Servidor MCP: `garmin_mcp`

Este proyecto usa como backend el servidor MCP **[`Taxuspt/garmin_mcp`](https://github.com/Taxuspt/garmin_mcp)**, desarrollado por [Alexandre Domingues](https://github.com/Taxuspt).

| Detalle | Valor |
|---------|-------|
| **Repositorio** | [github.com/Taxuspt/garmin_mcp](https://github.com/Taxuspt/garmin_mcp) |
| **Herramientas** | 126 (actividades, salud, entrenamiento, workouts, nutrición…) |
| **Transporte** | stdio (`garmin-mcp` local o `uvx` fallback) |
| **Autenticación** | OAuth tokens en `~/.garminconnect` |
| **Licencia** | MIT |

### Modo de herramientas

Al iniciar el agente se pregunta qué conjunto de herramientas cargar:

| Modo | Herramientas | Tokens por petición | Uso recomendado |
|------|-------------|---------------------|------------------|
| **Essential Tools** *(default)* | Subset reducido (configurable) | ~3-5k | Uso diario: salud, actividades, entrenamiento |
| **Todas** | 126 | ~30k | Acceso a workouts, nutrición, challenges, gear… |

Puedes fijar el subconjunto permanentemente añadiendo `GARMIN_ENABLED_TOOLS=tool1,tool2,...` en tu `.env`.

### Compatibilidad MCP (verificado local)

Cambios recientes del servidor MCP de Garmin que ya están contemplados en el código:

- `get_personal_record` es el endpoint vigente para récords personales (el alias plural `get_personal_records` puede no existir según versión).
- `get_body_battery` ahora usa rango de fechas: `start_date` + `end_date`.
- `get_body_composition` ahora usa rango de fechas: `start_date` + `end_date`.

Si actualizas `garmin-mcp`, revisa estos contratos antes de desplegar cambios en prompts o rutas de tools.

---

## 📁 Estructura del Proyecto

```
garmin-ai-coach/
├── agent/
│   ├── __init__.py
│   ├── main.py            # Punto de entrada: menú de proveedor, herramientas, chat e interfaz de usuario.
│   ├── mcp_client.py      # Cliente MCP asíncrono — lanza garmin-mcp local o uvx (fallback).
│   ├── storage.py         # Capa de persistencia multiusuario DB-first (Supabase).
│   └── trainer_agent.py   # Agente: tool-calling, adaptadores LLM, lógica de conversación.
├── memory/                # Base de conocimiento local opcional (RAG).
│   └── .gitkeep
├── prompts/
│   ├── system_prompt.md            # Prompt principal: personalidad, protocolos y uso de MCP por intención.
│   ├── system_prompt_compact.md    # Versión compacta del prompt para reducir tokens manteniendo reglas críticas.
│   └── mcp_tool_routing_guide.md   # Guía operativa de enrutado de tools MCP por intención.
├── supabase/
│   └── schema.sql         # DDL para crear las tablas en Supabase (ejecutar en SQL Editor).
├── tests/
│   ├── __init__.py
│   ├── test_trainer_agent.py  # Tests de funciones puras + mock de Gemini.
│   ├── test_main.py           # Tests de validaciones de input + flujo principal.
│   └── test_storage.py        # Tests de persistencia DB-first y seguridad de credenciales.
├── .env                   # Credenciales locales (no subir a git).
├── .env.example           # Plantilla de configuración con comentarios.
├── agent.log              # Log de ejecución del agente (local, no versionar).
├── requirements.txt       # Dependencias de producción.
├── requirements-dev.txt   # Dependencias de desarrollo: pytest, pytest-asyncio.
├── pytest.ini             # Configuración de pytest.
├── TODO.md                # Roadmap y mejoras futuras planificadas.
└── README.md
```

---

## ⚙️ Arquitectura de flujo interno

Esta sección describe qué ocurre dentro del código en cada operación clave. Útil para entender cómo se generan los outputs y dónde actúa cada capa.

### Arranque de sesión

```
main.py → asyncio.run(run_agent())
  └─ TrainerAgent.initialize()
       └─ list_available_tools(mcp_session)   → filtra tools de escritura si MCP_READ_ONLY=true
  └─ TrainerAgent.build_startup_status_markdown()
       └─ collect_startup_snapshot_48h()
            ├─ call_tool("get_body_battery", start_date, end_date)
            ├─ call_tool("get_hrv_data", date)
            ├─ call_tool("get_sleep_summary", date)
            ├─ call_tool("get_training_load_trend", 56 días)
            ├─ call_tool("get_activities", limit=12)
            └─ _compute_load_fatigue_metrics(activities, trend_payload, profile)
                 └─ persiste en profile["load_metrics"] → _save_user_profile() → Supabase
       └─ _build_proactive_status_markdown(snapshot)  → briefing visible al usuario
```

### Cálculo del modelo TSS/ATL/CTL/TSB

```
_compute_load_fatigue_metrics(activities, trend_payload, profile, days_window)
  │
  ├─ 1. Recopilación: _extract_training_load_points(trend_payload) + _estimate_session_tss(act)
  ├─ 2. Config por deporte: _resolve_sport_model_cfg(profile)
  │       └─ lee profile["goals"]["primary"] → _SPORT_MODEL_DEFAULTS[deporte]
  │       └─ aplica overrides de profile["load_metrics"]["model"] si existen
  ├─ 3. Semilla: profile["load_metrics"]["last"] → atl_prev, ctl_prev (continuidad)
  ├─ 4. EWMA día a día:
  │       atl = atl_prev + (tss - atl_prev) / tau_atl
  │       ctl = ctl_prev + (tss - ctl_prev) / tau_ctl
  │       tsb = ctl - atl
  ├─ 5. Percentiles individualizados (últimos 28 días del propio atleta):
  │       tsb_low = p15, tsb_high = p80, atl_high = p85
  ├─ 6. Decisión de status (por prioridad):
  │       abs_overload  → tsb <= tsb_abs_floor (suelo fijo por deporte)
  │       sustained_overload → todos últimos 7 días TSB <= tsb_low
  │       fatigue_high  → tsb < tsb_low OR atl > atl_high
  │       ready         → tsb en rango AND not fatigue_high
  │       neutral       → resto
  └─ 7. Flag warm-up: days_with_load < 21 → aviso de calibración al usuario
```

### Cada mensaje en el chat

```
TrainerAgent.chat(user_message)
  │
  ├─ Ruta 1 — Plan status (determinista, sin LLM)
  │    └─ _is_plan_status_intent(msg) → _build_training_plan_status_markdown(profile)
  │         └─ _get_active_training_plan() → prioriza DB, fallback a profile
  │
  ├─ Ruta 2 — Planificación estructurada (determinista + LLM para texto)
  │    └─ _is_planning_intent(msg) → _generate_structured_plan_payload(profile, msg)
  │         ├─ Calcula duración, dificultad y razón del ajuste
  │         ├─ Genera 7 sesiones base con duraciones proporcionales
  │         └─ Si trail: _apply_trail_overrides() → tipos y notas específicos de trail
  │
  ├─ Ruta 3 — Récords personales (determinista)
  │    └─ _is_personal_records_intent(msg) → call_tool("get_personal_record") → tabla
  │
  ├─ Ruta 4 — Análisis de actividad por fecha (pre-fetch)
  │    └─ _extract_iso_date_from_text(msg) → _find_activity_id_by_date()
  │         └─ Pre-carga: actividad + body battery + sueño + HRV + carga
  │         └─ _build_activity_analysis_block() → bloque pre-computado para el LLM
  │
  └─ Ruta 5 — LLM con tool-calling (resto de intenciones)
       └─ Bucle hasta 15 iteraciones:
            ├─ LLM decide qué tools llamar
            ├─ call_tool() → resultado → _compact_tool_result() → max 3000 chars
            └─ Si tool de escritura y MCP_READ_ONLY → bloqueo inmediato
```

### Suelos absolutos de TSB por deporte

| Deporte | TSB abs. floor | Motivo |
|---------|---------------:|--------|
| Trail running | −35 | Sesiones largas con picos de TSS muy altos |
| Running | −30 | Volumen moderado, recuperación más rápida |
| Ciclismo | −32 | Mayor volumen horario, fatiga muscular menor |
| Triatlón | −35 | Multimodal, acumulación alta entre disciplinas |

Cuando `TSB ≤ floor` el sistema fuerza `status=OVERLOAD` independientemente de los percentiles históricos del atleta, evitando que atletas crónicamente sobrecargados normalicen rangos peligrosos.

### Training Load de Garmin vs. TSS de TrainingPeaks

El sistema usa el **Training Load de Garmin como proxy de TSS**. Aquí la diferencia técnica entre ambos:

**Training Load de Garmin** se basa en **EPOC** (Excess Post-exercise Oxygen Consumption):

- Garmin estima dos umbrales por atleta: VT1 (aeróbico ligero→moderado) y VT2 (umbral de lactato), usando VO₂max e historial de FC.
- A cada segundo de actividad le asigna un coste metabólico según la zona (por debajo de VT1, entre VT1-VT2, por encima de VT2).
- Integra ese coste durante toda la sesión y lo normaliza en una escala empírica (~0 a 500).
- Se recalibra automáticamente con cada actividad. No requiere configuración manual.

**TSS de TrainingPeaks** (Coggan 2003) nació para ciclismo con potenciómetro:

$$TSS = \frac{t \times NP \times IF}{FTP \times 3600} \times 100$$

Una sesión en FTP durante exactamente 1 hora = **100 TSS**. Para running sin potenciómetro, TP usa hrTSS basado en la fórmula TRIMP de Banister (FC media vs. LTHR).

**Comparativa:**

| Aspecto | Garmin Training Load | TrainingPeaks TSS |
|---------|----------------------|-------------------|
| Fórmula base | EPOC integrado | Potencia normalizada o TRIMP-HR |
| Calibración | Automática (VO₂max + historial) | Manual (FTP o LTHR del atleta) |
| Exactitud | Alta con FC calibrada | Muy alta con potenciómetro |
| Comparabilidad entre atletas | No (relativa al historial propio) | Sí (100 TSS = 1h en umbral) |
| Deportes | Todos (running, trail, cycling, swimming) | Nació en ciclismo; adaptado a running/triatlón |

**Por qué nuestro modelo es válido:** ATL/CTL/TSB son modelos relacionales, no absolutos. Lo que importa es que la unidad de carga sea **consistente para el mismo atleta**, no que sea exactamente 100 en umbral. La individualización está en los tau y percentiles propios de cada atleta, no en el valor absoluto de cada sesión.

**Fallback cuando Garmin no tiene Training Load** (actividades antiguas o importadas): se usa una estimación por FC media aplicando el método Karvonen (%HRR → IF → TSS), similar a hrTSS pero sin requerir que el atleta conozca su LTHR.

---

## 🧪 Tests

El proyecto incluye una suite de **más de 220 tests unitarios** que cubre las funciones críticas sin necesidad de conexión a Garmin ni a ningún LLM.

### Instalar dependencias de desarrollo
```powershell
pip install -r requirements-dev.txt
```

### Ejecutar los tests
```powershell
pytest
```

### Cobertura

| Módulo | Qué cubre |
|--------|-----------|
| `trainer_agent.py` | `_seconds_to_hhmmss`, `_normalize_date_args`, `_strip_garmin_object`, `_compact_tool_result`, `_compact_personal_records`, `_clean_schema_for_gemini`, `_GeminiCompletions._parse`, resolución de actividad por fecha, zonas FC y análisis profundo, estado proactivo 48h, fallbacks de planificación, modelo de carga/fatiga (TSS/ATL/CTL/TSB), configuración por deporte, tabla de tendencia `/carga`, plan trail específico, cálculo TSS por FC media (Karvonen), fetch histórico por fechas |
| `main.py` | `_validate_date`, `_validate_time`, `_validate_hours`, `_is_first_time`, KB enriquecida de onboarding, `_ensure_garmin_credentials`, `_build_enriched_athlete_knowledge` |
| `storage.py` | sanitización de credenciales, no-persistencia de passwords Garmin |

---

## 🔒 Privacidad y Seguridad

- **Contraseña cifrada en BD:** La contraseña se almacena con cifrado simétrico Fernet (AES-128-CBC + HMAC-SHA256) en Supabase. La `ENCRYPTION_KEY` en `.env` es la única clave — nunca la subas a Git.
- **Hash unidireccional para verificación:** Además del cifrado, el login verifica contra un hash PBKDF2-SHA256 (120.000 iteraciones) para autenticación segura.
- **Nunca texto claro:** La `_sanitize_credentials_for_storage` garantiza que `garmin_password` y `garmin_password_strategy` nunca lleguen a la columna `credentials` de Supabase.
- **OAuth tokens de Garmin:** Los tokens OAuth se guardan en `~/.garminconnect` (válidos ~6 meses). La contraseña solo circula en memoria durante la sesión.
- **API keys hasheadas:** El identificador local de cuota de Gemini usa SHA-256 — la clave nunca se escribe en texto plano.
- **Pruning inteligente:** Los metadatos innecesarios de la API de Garmin se eliminan antes de enviarlos al LLM, reduciendo tokens y evitando fugas de datos irrelevantes.

---

## 📝 Contribuciones

¡Las contribuciones, issues y sugerencias son bienvenidas! Si encuentras algún cálculo de ritmos incorrecto o quieres añadir nuevas herramientas, abre un *Pull Request* o una incidencia.

¡Buen entrenamiento! 🏁

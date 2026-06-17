# 🏃‍♂️ Garmin AI Coach

**Garmin AI Coach** es un asistente y entrenador deportivo personal inteligente. Combina modelos de lenguaje (LLM) con tus datos deportivos reales de Garmin a través del **servidor MCP [`garmin_mcp`](https://github.com/Taxuspt/garmin_mcp)**, que se gestiona automáticamente vía `uvx` sin necesidad de instalación manual.

El agente analiza tus métricas de rendimiento (VO2Max, HRV, sueño, SPO2, umbral de lactato, puntuación de resistencia...), tus récords personales históricos y tus actividades recientes para darte recomendaciones personalizadas, planes de entrenamiento y análisis de ritmos **100% basados en tus datos reales de Garmin Connect**. Con memoria persistente entre sesiones, recuerda lo que habéis hablado en conversaciones anteriores.

---

## ✨ Características clave

* **🧠 Cinco proveedores de IA (todos gratuitos salvo VPN):**
  | # | Opción | Modelo | Límite gratuito | Requiere |
  |---|--------|--------|-----------------|----------|
  | 1 | **GitHub Models** | `gpt-4o-mini` | — | GitHub token + VPN |
  | 2 | **Groq** | `llama-3.3-70b-versatile` | 100k tokens/día | API key gratuita |
  | 3 | **Google Gemini** | `gemini-2.0-flash` | ~1M tokens/día | API key gratuita |
  | 4 | **Mistral** *(recomendado)* | `mistral-small-latest` | ~1B tokens/mes | API key gratuita |
  | 5 | **Cerebras** | `llama-3.3-70b` | generoso | API key gratuita |

  La red se **detecta automáticamente**: dentro de VPN corporativa (Zscaler) usa GitHub Models; fuera selecciona el mejor proveedor libre disponible.

* **⌚ Herramientas de Garmin Connect:**
  - Actividades, zonas FC, splits, progreso, récords personales
  - Salud diaria: frecuencia cardíaca, body battery, estrés, pasos, respiración, SPO2
  - Métricas avanzadas: HRV, VO2Max, predicciones de carrera, umbral de lactato, puntuación de resistencia, edad de fitness
  - Sueño, composición corporal, hidratación, perfil de usuario y objetivos

* **👤 Perfil de usuario sincronizado:**
  - Al arrancar, sincroniza automáticamente género, peso, altura y edad desde Garmin Connect.
  - Setup guiado la primera vez: deporte principal, horas/semana, próximo evento, tiempo objetivo y condiciones de salud.
  - Todos los campos del perfil se inyectan en el system prompt para que el agente te conozca desde el primer mensaje.
  - Si cambias de cuenta de Garmin, el perfil se reinicia automáticamente.

* **✅ Validación de inputs:**
  - `target_race_date`: formato `YYYY-MM-DD` + debe ser fecha futura.
  - `target_time`: formato `H:MM:SS` / `HH:MM:SS` con rangos de minutos/segundos.
  - `weekly_training_hours`: número entre 0.5 y 40, acepta coma o punto decimal.
  - Bucle de reintento con mensaje de error en color hasta que el valor sea válido.

* **🔐 Autenticación OAuth con Garmin:**
  - Los tokens OAuth se guardan una sola vez en `~/.garminconnect` (válidos ~6 meses).
  - No se almacenan contraseñas en texto plano en ningún proceso del agente.

* **💾 Memoria persistente entre sesiones:**
  - Al salir, el agente genera automáticamente un resumen compacto de la sesión con el propio LLM.
  - Los últimos 5 resúmenes se inyectan como contexto al arrancar la siguiente sesión — el agente recuerda lo que habéis hablado.
  - Perfil en `memory/user_profile.json`; historial y resúmenes en `memory/session_context.json`.
  - Control local de cuota de Gemini en `memory/gemini_daily_usage.json` (hash SHA-256 de la API key, nunca en claro).

* **🔧 Sin dependencias de Node.js:**
  - El servidor MCP es 100% Python, lanzado automáticamente por `uvx` en un entorno aislado.

---

## 🛠️ Requisitos previos

| Requisito | Versión mínima | Notas |
|-----------|---------------|-------|
| **Python** | 3.10+ | Desarrollado con 3.13 |
| **uv / uvx** | cualquiera | Gestiona el servidor MCP automáticamente |
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
- `GARMIN_EMAIL` y `GARMIN_PASSWORD`
- La API key del proveedor que uses (`GEMINI_API_KEY`, `GROQ_API_KEY` o `GITHUB_TOKEN`)

*(Consulta los comentarios en `.env.example` para obtener las URLs de registro de cada proveedor.)*

### 3. Pre-autenticar con Garmin Connect *(una sola vez)*
Este paso guarda los tokens OAuth en `~/.garminconnect` para que el agente no necesite tu contraseña en cada arranque:
```powershell
$env:GARMIN_EMAIL="tu@email.com"
$env:GARMIN_PASSWORD="tu_contraseña"
uvx --python 3.12 --from git+https://github.com/Taxuspt/garmin_mcp garmin-mcp-auth
```
> Los tokens son válidos aproximadamente **6 meses**. Repite este paso cuando expiren.

---

## 🏃‍♂️ Uso

```powershell
python -m agent.main
```

El agente descargará automáticamente el servidor MCP en el primer arranque (vía `uvx`). Después aparecerá el menú de proveedores:

```
  1 · GitHub Models (gpt-4o-mini)           — dentro de VPN
  2 · Groq         (llama-3.3-70b)          — 100k tokens/día
  3 · Google Gemini (gemini-2.0-flash)      — ~1M tokens/día gratis
  4 · Mistral      (mistral-small)          — gratis · function calling nativo  ← recomendado
  5 · Cerebras     (llama-3.3-70b)          — ultrarrápido · gratis
```

A continuación se selecciona el modo de herramientas y el agente conecta con Garmin Connect.

### Comandos disponibles en el chat

| Comando | Descripción |
|---------|-------------|
| `/ayuda` · `/help` · `/?` | Muestra ejemplos de preguntas, todos los comandos y guía de indicadores |
| `/perfil` | Muestra el perfil actual (datos personales + objetivos + salud) |
| `/perfil editar` | Edita todos los campos del perfil |
| `/perfil editar objetivo` | Edita solo los objetivos de entrenamiento |
| `/perfil editar salud` | Edita solo los datos de salud |
| `salir` | Guarda el resumen de sesión y cierra el agente |

### Ejemplos de preguntas
- *"¿Cuál ha sido mi mejor ritmo en media maratón y qué necesito para bajar de 1h45?"*
- *"Analízame como deportista usando mis métricas de la última semana"*
- *"¿Cuál es mi VO2Max actual y cómo ha evolucionado?"*
- *"Dame un plan de entrenamiento para la próxima carrera de 10K"*
- *"¿Cómo ha sido mi sueño y HRV esta semana?"*
- *"¿Qué indicadores debo vigilar esta noche como diabético tipo 1 tras el entrenamiento?"*

---

## � Servidor MCP: `garmin_mcp`

Este proyecto usa como backend el servidor MCP **[`Taxuspt/garmin_mcp`](https://github.com/Taxuspt/garmin_mcp)**, desarrollado por [Alexandre Domingues](https://github.com/Taxuspt).

| Detalle | Valor |
|---------|-------|
| **Repositorio** | [github.com/Taxuspt/garmin_mcp](https://github.com/Taxuspt/garmin_mcp) |
| **Herramientas** | 126 (actividades, salud, entrenamiento, workouts, nutrición…) |
| **Transporte** | stdio (lanzado automáticamente vía `uvx`) |
| **Autenticación** | OAuth tokens en `~/.garminconnect` |
| **Licencia** | MIT |

### Modo de herramientas

Al iniciar el agente se pregunta qué conjunto de herramientas cargar:

| Modo | Herramientas | Tokens por petición | Uso recomendado |
|------|-------------|---------------------|------------------|
| **Essential Tools** *(default)* | ~28 | ~3-5k | Uso diario: salud, actividades, entrenamiento |
| **Todas** | 126 | ~30k | Acceso a workouts, nutrición, challenges, gear… |

Puedes fijar el subconjunto permanentemente añadiendo `GARMIN_ENABLED_TOOLS=tool1,tool2,...` en tu `.env`.

---

## �📁 Estructura del Proyecto

```
garmin-ai-coach/
├── agent/
│   ├── __init__.py
│   ├── main.py            # Punto de entrada: menú de proveedor, herramientas, chat e interfaz de usuario.
│   ├── mcp_client.py      # Cliente MCP asíncrono — lanza garmin_mcp vía uvx.
│   └── trainer_agent.py   # Agente: tool-calling, adaptadores LLM, memoria persistente.
├── memory/                # Generado automáticamente al arrancar. NO subir a git.
│   ├── user_profile.json       # Perfil del deportista: personal, objetivos, salud.
│   ├── session_context.json    # Historial de conversación y resúmenes de sesiones.
│   └── gemini_daily_usage.json # Control de cuota diaria de Gemini (API key hasheada).
├── prompts/
│   └── system_prompt.md   # Personalidad, herramientas MCP y protocolos del entrenador.
├── tests/
│   ├── __init__.py
│   ├── test_trainer_agent.py  # 54 tests: funciones puras + mock de Gemini.
│   └── test_main.py           # 39 tests: validaciones de input + lógica de identidad.
├── .env                   # Credenciales locales (no subir a git).
├── .env.example           # Plantilla de configuración con comentarios.
├── requirements.txt       # Dependencias de producción.
├── requirements-dev.txt   # Dependencias de desarrollo: pytest, pytest-asyncio.
├── pytest.ini             # Configuración de pytest.
├── TODO.md                # Roadmap y mejoras futuras planificadas.
└── README.md
```

---

## 🧪 Tests

El proyecto incluye una suite de **93 tests unitarios** que cubre las funciones críticas sin necesidad de conexión a Garmin ni a ningún LLM.

### Instalar dependencias de desarrollo
```powershell
pip install -r requirements-dev.txt
```

### Ejecutar los tests
```powershell
pytest
```

### Cobertura

| Módulo | Tests | Qué cubre |
|--------|-------|-----------|
| `trainer_agent.py` | 54 | `_seconds_to_hhmmss`, `_normalize_date_args` (hoy/ayer/today/yesterday), `_strip_garmin_object`, `_compact_tool_result`, `_compact_personal_records` (conversión segundos→HH:MM:SS), `_clean_schema_for_gemini`, `_GeminiCompletions._parse` |
| `main.py` | 39 | `_validate_date`, `_validate_time`, `_validate_hours`, `_garmin_user_id`, `_is_first_time` |

---

## 🔒 Privacidad y Seguridad

- **OAuth seguro:** Garmin autentica vía tokens; la contraseña solo se usa en la pre-autenticación inicial y nunca se guarda en disco por el agente.
- **API keys hasheadas:** El identificador local de cuota de Gemini usa SHA-256 — la clave nunca se escribe en texto plano.
- **Pruning inteligente:** Los metadatos innecesarios de la API de Garmin se eliminan antes de enviarlos al LLM, reduciendo tokens y evitando fugas de datos irrelevantes.

---

## 📝 Contribuciones

¡Las contribuciones, issues y sugerencias son bienvenidas! Si encuentras algún cálculo de ritmos incorrecto o quieres añadir nuevas herramientas, abre un *Pull Request* o una incidencia.

¡Buen entrenamiento! 🏁

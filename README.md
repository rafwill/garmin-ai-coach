# 🏃‍♂️ Garmin AI Coach

**Garmin AI Coach** es un asistente y entrenador deportivo personal inteligente. Funciona combinando el poder de los modelos de lenguaje (LLM) con tus datos deportivos reales de Garmin, utilizando un **servidor de Protocolo de Contexto de Modelo (MCP)** de alto rendimiento.

El agente analiza tus métricas de rendimiento (como VO2Max, HRV, peso, sueño), tus récords personales históricos, tus actividades recientes e interactúa de manera inteligente para darte recomendaciones personalizadas, planes de carrera, análisis de ritmos y más, **100% centrado en tus datos reales de Garmin Connect**.

---

## ✨ Características clave

* **🧠 Multi-Proveedor de Inteligencia Artificial:**
  - **Google Gemini (Recomendado):** Totalmente integrado mediante el SDK de Google GenAI (`gemini-2.5-flash`), con soporte para control de cuotas y estimación diaria persistente en local (límite de 1,000,000 de tokens diarios gratis).
  - **Mistral Local (Ollama / LM Studio):** 100% privado, ilimitado, ejecutándose directo en tu máquina a través de un endpoint compatible con API de OpenAI.
  - **Groq:** Conectividad veloz ejecutando `llama-3.3-70b` (100k tokens gratuitos por día).
  - **GitHub Models:** Acceso optimizado para VPNs corporativas a través de `gpt-4o-mini` y `gpt-4o`.
* **⌚ Integración con Garmin Connect MCP:**
  - Consulta automática de récords (desde 1km hasta natación o carreras de larga distancia).
  - Acceso seguro a estadísticas de salud recientes (HRV / Variabilidad de frecuencia cardíaca, VO2Max, etc.).
  - Pruning recursivo de objetos raw del API para proteger y limpiar los datos que se envían al modelo (reducción masiva de tokens).
* **💾 Memoria Persistente de Sesión y Tokens:**
  - Almacena el perfil deportivo dinámico del usuario de manera segura en [memory/user_profile.json](memory/user_profile.json).
  - Memoriza de forma local las llamadas y los tokens utilizados por cada una de tus API Keys vía hash SHA-256 en [memory/gemini_daily_usage.json](memory/gemini_daily_usage.json).
* **🐳 Robustez en Windows y VPNs:**
  - Resuelve de manera nativa problemas de variables de entorno de Node.js mediante búsquedas directas en el Registro de Windows.
  - Soporte para Certificados SSL corporativos (como Zscaler) autodetectando configuraciones locales de red.

---

## 🛠️ Requisitos previos

1. **Python:** Versión `3.10` en adelante (desarrollado y probado sobre Python `3.14`).
2. **Node.js:** Versión `18.0` o superior (necesario para ejecutar el servidor MCP de Garmin bajo stdio).
3. **Credenciales de Garmin Connect:** Una cuenta activa en Garmin Connect.

---

## 🚀 Instalación y Configuración

Sigue estos sencillos pasos para dejar el agente en funcionamiento en tu equipo:

### 1. Clonar el repositorio y configurar el entorno Python
Asegúrate de preparar tu entorno de ejecución:
```powershell
# Crear y activar tu entorno virtual (Windows)
python -m venv .venv
.venv\Scripts\Activate.ps1

# Instalar las dependencias del proyecto
pip install -r requirements.txt
```

### 2. Configurar variables de entorno
Crea una copia de nuestro archivo de ejemplo `.env.example` y renómbralo a `.env`:
```powershell
cp .env.example .env
```
Abre tu archivo `.env` y configura al menos:
* Tus credenciales de Garmin (`GARMIN_EMAIL` y `GARMIN_PASSWORD`).
* La API Key del proveedor que vayas a utilizar habitualmente.

*(Puedes encontrar instrucciones de registro y URLs de endpoints locales en los comentarios detallados del propio archivo `.env`).*

---

## 🏃‍♂️ Cómo se utiliza

1. **Asegúrate de levantar el modelo (si deseas usar Local Mistral):**
   Si usas **Ollama**, asegúrate de tener el demonio activo ejecutando:
   ```bash
   ollama run mistral
   ```
2. **Lanzar la interfaz interactiva:**
   Inicia la ejecución en tu consola interactiva:
   ```powershell
   python -m agent.main
   ```
3. **Selecciona tu IA favorita:**
   El terminal interactivo te ofrecerá un menú estético para escoger tu IA de la sesión.
4. **¡Empieza a chatear!**
   Pregúntale cosas de este estilo:
   - *"¿Cuál ha sido mi mejor ritmo en media maratón y qué me recomiendas para bajar de 1h45?"*
   - *"Analízame como deportista usando mis métricas de la última semana"*
   - *"¿He corrido hoy? Hazme un resumen de mi última actividad"*

---

## 📁 Estructura del Proyecto

```
garmin-ai-coach/
├── agent/
│   ├── __init__.py
│   ├── main.py            # Punto de entrada principal y terminal interactivo.
│   ├── mcp_client.py      # Conector cliente asíncrono para el Garmin Connect MCP.
│   └── trainer_agent.py   # Sistema del agente, adaptadores de OpenAI/Gemini/Local y formateo de Garmin.
├── memory/
│   ├── user_profile.json  # Historial resumido de tus sesiones y perfil de deportista.
│   └── gemini_daily_usage.json # Recuento de tokens local y de seguridad por API Key.
├── prompts/
│   └── system_prompt.md   # Instrucciones que moldean la personalidad de tu entrenador GarminCoach.
├── .env                   # Variables locales y credenciales (mantener privado).
├── requirements.txt       # Requisitos de librerías de Python.
└── README.md              # Documentación general del coach.
```

---

## 🔒 Privacidad y Seguridad

Tu información física y deportiva es extremadamente valiosa. Por ello:
* **Ejecución Local:** Si escoges la opción `Mistral Local (Ollama)`, absolutamente ningún dato deportivo, peso o altura sale de tu propio equipo.
* **Hasehado de claves:** En el seguimiento de cuota de Gemini, las claves de tus APIs nunca se escriben en texto plano en la máquina; el identificador se almacena como un hash unidireccional SHA-256.
* **Pruning Inteligente:** Los metadatos de sincronización inservibles del Garmin API se limpian y desechan antes de alimentar al procesador para evitar sobrecostes e intrusiones de datos ajenos.

---

## 📝 Contribuciones y Desarrollo

¡Las contribuciones, issues y feedback deportivo son super bienvenidos! Si encuentras alguna fórmula matemática de ritmos mal calculada o si quieres añadir conectores para otros modelos, no dudes en abrir un *Pull Request* o reportar una incidencia.

¡Buen entrenamiento! 🏁

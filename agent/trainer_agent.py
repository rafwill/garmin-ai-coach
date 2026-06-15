"""
trainer_agent.py
Agente entrenador personal que combina OpenAI con las herramientas
de Garmin Connect a través del servidor MCP.
"""

import os
import ssl
import json
from pathlib import Path

import httpx
import truststore
from openai import AsyncOpenAI
from mcp import ClientSession

from agent.mcp_client import list_available_tools, call_tool


PROMPTS_DIR = Path(__file__).parent.parent / "prompts"
MEMORY_DIR = Path(__file__).parent.parent / "memory"


def _load_system_prompt() -> str:
    """Carga el system prompt del entrenador desde el archivo Markdown."""
    prompt_file = PROMPTS_DIR / "system_prompt.md"
    return prompt_file.read_text(encoding="utf-8")


def _load_user_profile() -> dict:
    """Carga el perfil del usuario desde memoria (si existe)."""
    profile_file = MEMORY_DIR / "user_profile.json"
    if profile_file.exists():
        return json.loads(profile_file.read_text(encoding="utf-8"))
    return {}


def _save_history_entry(role: str, content: str) -> None:
    """Guarda una entrada en el historial del perfil del usuario."""
    profile_file = MEMORY_DIR / "user_profile.json"
    profile = _load_user_profile()
    profile.setdefault("history", []).append({"role": role, "content": content})
    # Mantener solo las últimas 50 entradas
    profile["history"] = profile["history"][-50:]
    profile_file.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")


# Herramientas esenciales para el agente entrenador
# Limitamos el número para no superar los límites de tokens del modelo
# Máximo de caracteres por resultado de herramienta para no exceder el límite de tokens
_MAX_TOOL_RESULT_CHARS = 1500


def _compact_tool_result(raw: str | None) -> str:
    """
    Compacta el resultado de una herramienta para que quepa en el contexto.
    - Arrays JSON: conserva solo los primeros 5 elementos.
    - Strings demasiado largos: trunca a _MAX_TOOL_RESULT_CHARS.
    - Cualquier otro caso: trunca por caracteres.
    """
    if not raw:
        return "(sin datos)"
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            data = data[:5]  # máximo 5 elementos de arrays
        compact = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        if len(compact) > _MAX_TOOL_RESULT_CHARS:
            compact = compact[:_MAX_TOOL_RESULT_CHARS] + "...(truncado)"
        return compact
    except (json.JSONDecodeError, TypeError):
        if len(raw) > _MAX_TOOL_RESULT_CHARS:
            return raw[:_MAX_TOOL_RESULT_CHARS] + "...(truncado)"
        return raw


ESSENTIAL_TOOLS = {
    # Actividades
    "get_last_activity", "get_activities_by_date", "get_activity_hr_zones",
    "get_activity_splits", "get_progress_summary", "get_activities",
    # Salud diaria
    "get_daily_summary", "get_heart_rate", "get_body_battery",
    "get_stress", "get_intensity_minutes", "get_resting_heart_rate",
    # Rendimiento
    "get_training_readiness", "get_training_status", "get_hrv",
    "get_vo2max", "get_race_predictions", "get_personal_records",
    # Sueño
    "get_sleep_data",
    # Composición corporal
    "get_body_composition", "get_latest_weight",
    # Perfil
    "get_user_profile", "get_goals",
}


def _build_tools_schema(tools: list[dict]) -> list[dict]:
    """Convierte las herramientas MCP al formato de function calling de OpenAI/GitHub Models.
    Solo incluye las herramientas esenciales para el agente entrenador.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["input_schema"],
            },
        }
        for tool in tools
        if tool["name"] in ESSENTIAL_TOOLS
    ]


class TrainerAgent:
    """
    Agente entrenador personal que usa OpenAI + Garmin MCP.
    Mantiene historial de conversación y llama herramientas de Garmin
    automáticamente según lo que necesite para responder al usuario.
    """

    def __init__(self, mcp_session: ClientSession):
        # GitHub Models usa la API compatible con OpenAI en models.inference.ai.azure.com
        # El certificado de Zscaler es de confianza a través de truststore
        ssl_ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        http_client = httpx.AsyncClient(verify=ssl_ctx)
        self.client = AsyncOpenAI(
            base_url="https://models.inference.ai.azure.com",
            api_key=os.environ["GITHUB_TOKEN"],
            http_client=http_client,
        )
        self.model = os.environ.get("GITHUB_MODEL", "gpt-4o-mini")
        self.mcp_session = mcp_session
        self.system_prompt = _load_system_prompt()
        self.user_profile = _load_user_profile()
        self.conversation_history: list[dict] = []
        self.tools_schema: list[dict] = []

    async def initialize(self) -> None:
        """Carga las herramientas disponibles del MCP."""
        tools = await list_available_tools(self.mcp_session)
        self.tools_schema = _build_tools_schema(tools)

    def _build_system_prompt(self) -> str:
        """Construye el system prompt incluyendo el perfil del usuario si existe."""
        profile_context = ""
        if self.user_profile.get("personal", {}).get("name"):
            p = self.user_profile["personal"]
            g = self.user_profile.get("goals", {})
            profile_context = (
                f"\n\n## Perfil del usuario\n"
                f"- Nombre: {p.get('name', '')}\n"
                f"- Edad: {p.get('age', 'desconocida')}\n"
                f"- Peso: {p.get('weight_kg', 'desconocido')} kg\n"
                f"- Objetivo principal: {g.get('primary', 'no definido')}\n"
                f"- Carrera objetivo: {g.get('target_race', 'ninguna')}\n"
            )
        return self.system_prompt + profile_context

    def _build_messages(self, user_message: str) -> list[dict]:
        """Construye el array de mensajes para la llamada al LLM.
        Limita el historial a los últimos 6 turnos (3 pares user/assistant)
        para no superar el límite de tokens de GitHub Models (8000).
        """
        messages = [{"role": "system", "content": self._build_system_prompt()}]
        # Solo los últimos 6 mensajes del historial (3 intercambios)
        messages.extend(self.conversation_history[-6:])
        messages.append({"role": "user", "content": user_message})
        return messages

    async def chat(self, user_message: str) -> str:
        """
        Procesa un mensaje del usuario y devuelve la respuesta del agente.
        Gestiona automáticamente las llamadas a herramientas de Garmin.
        """
        messages = self._build_messages(user_message)

        iteration = 0
        while True:
            iteration += 1
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=self.tools_schema if self.tools_schema else None,
                tool_choice="auto" if self.tools_schema else None,
            )

            message = response.choices[0].message

            # Debug: muestra si el modelo llama herramientas
            if message.tool_calls:
                tool_names = [tc.function.name for tc in message.tool_calls]
                print(f"  [debug] Iteración {iteration}: llamando tools → {tool_names}")
            else:
                print(f"  [debug] Iteración {iteration}: respuesta directa (sin tool calls)")
                print(f"  [debug] finish_reason: {response.choices[0].finish_reason}")

            # Si el modelo quiere llamar herramientas de Garmin
            if message.tool_calls:
                messages.append(message)

                for tool_call in message.tool_calls:
                    tool_name = tool_call.function.name
                    try:
                        arguments = json.loads(tool_call.function.arguments)
                    except json.JSONDecodeError:
                        arguments = {}

                    print(f"  [debug] Ejecutando: {tool_name}({arguments})")
                    raw_result = await call_tool(
                        self.mcp_session, tool_name, arguments
                    )
                    tool_result = _compact_tool_result(raw_result)
                    print(f"  [debug] Resultado ({len(raw_result or '')} → {len(tool_result)} chars): {tool_result[:150]}")

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": tool_result,
                    })

                # Continúa el loop para que el modelo procese los resultados
                continue

            # Respuesta final del agente
            assistant_reply = message.content or ""

            # Guardar en historial de conversación
            self.conversation_history.append({"role": "user", "content": user_message})
            self.conversation_history.append({"role": "assistant", "content": assistant_reply})

            # Guardar en memoria persistente
            _save_history_entry("user", user_message)
            _save_history_entry("assistant", assistant_reply)

            return assistant_reply

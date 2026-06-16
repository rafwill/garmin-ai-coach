"""
mcp_client.py
Cliente que arranca el servidor MCP de Garmin como subproceso
y expone sus herramientas para que el agente las use.
"""

import os
import shutil
from contextlib import asynccontextmanager
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def _get_server_params(essential_only: bool = True) -> StdioServerParameters:
    """Construye los parámetros de arranque del servidor MCP de Garmin."""
    email = os.environ.get("GARMIN_EMAIL")
    password = os.environ.get("GARMIN_PASSWORD")

    if not email or not password:
        raise ValueError(
            "Las variables GARMIN_EMAIL y GARMIN_PASSWORD son obligatorias. "
            "Copia .env.example a .env y rellena tus credenciales."
        )

    # Localizar uvx (gestor de herramientas Python del proyecto uv)
    uvx_cmd = shutil.which("uvx")
    if not uvx_cmd:
        raise RuntimeError(
            "No se encontró 'uvx' en el PATH.\n"
            "Insálalo con: pip install uv\n"
            "o visita: https://docs.astral.sh/uv/getting-started/installation/"
        )

    # Herramientas esenciales para un agente entrenador personal.
    # Reduce el contexto de ~31k tokens (126 tools) a ~5k tokens (~25 tools).
    # Se puede sobreescribir con la variable GARMIN_ENABLED_TOOLS en .env.
    _DEFAULT_TOOLS = (
        # Actividades
        "get_activities,get_activity,"
        # Salud diaria (versiones ligeras donde existen)
        "get_stats,get_sleep_summary,get_sleep_data,"
        "get_heart_rates_summary,get_stress_summary,get_respiration_summary,"
        "get_body_battery,get_rhr_day,get_spo2_data,get_hrv_data,"
        "get_daily_steps,get_hydration_data,"
        # Composición corporal
        "get_body_composition,"
        # Preparación y entrenamiento
        "get_training_readiness,get_morning_training_readiness,"
        "get_training_status,get_training_load_trend,"
        "get_hrv_trend,get_vo2max_trend,"
        # Rendimiento avanzado
        "get_endurance_score,get_fitnessage_data,"
        "get_lactate_threshold,get_cycling_ftp,"
        # Tendencias semanales
        "get_weekly_steps,get_weekly_intensity_minutes,get_weekly_stress"
    )
    # Si essential_only=False y no hay override en .env, no se filtra (todas las herramientas)
    if essential_only:
        enabled_tools = os.environ.get("GARMIN_ENABLED_TOOLS", _DEFAULT_TOOLS)
    else:
        enabled_tools = os.environ.get("GARMIN_ENABLED_TOOLS", "")

    return StdioServerParameters(
        command=uvx_cmd,
        args=[
            "--python", "3.12",
            "--from", "git+https://github.com/Taxuspt/garmin_mcp",
            "garmin-mcp",
        ],
        env={
            **os.environ,
            "GARMIN_EMAIL": email,
            "GARMIN_PASSWORD": password,
            **({"GARMIN_ENABLED_TOOLS": enabled_tools} if enabled_tools else {}),
        },
    )


@asynccontextmanager
async def garmin_mcp_session(essential_only: bool = True):
    """
    Context manager que inicia el servidor MCP de Garmin y devuelve
    una sesión lista para llamar herramientas.

    Uso:
        async with garmin_mcp_session() as session:
            result = await session.call_tool("get_last_activity", {})
    """
    params = _get_server_params(essential_only=essential_only)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def list_available_tools(session: ClientSession) -> list[dict]:
    """Devuelve la lista de herramientas disponibles en el MCP."""
    tools_response = await session.list_tools()
    return [
        {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.inputSchema,
        }
        for tool in tools_response.tools
    ]


async def call_tool(session: ClientSession, tool_name: str, arguments: dict) -> str:
    """
    Llama a una herramienta del MCP y devuelve el resultado como string.
    Maneja errores y los devuelve de forma legible al agente.
    """
    try:
        result = await session.call_tool(tool_name, arguments)
        # El contenido puede ser texto o JSON estructurado
        if result.content:
            parts = []
            for block in result.content:
                if hasattr(block, "text"):
                    parts.append(block.text)
            return "\n".join(parts)
        return "Sin datos disponibles."
    except Exception as e:
        return f"Error al llamar a '{tool_name}': {str(e)}"

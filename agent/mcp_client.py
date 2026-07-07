"""
mcp_client.py
Cliente que arranca el servidor MCP de Garmin como subproceso
y expone sus herramientas para que el agente las use.
"""

import os
import shutil
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def _resolve_command(command_name: str) -> str | None:
    """Resuelve un ejecutable por PATH o por la carpeta Scripts del Python activo."""
    found = shutil.which(command_name)
    if found:
        return found

    scripts_dir = Path(sys.executable).parent
    candidates = [scripts_dir / command_name]
    if os.name == "nt":
        candidates.append(scripts_dir / f"{command_name}.exe")

    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return str(candidate)
    return None


def _get_server_params(essential_only: bool = True) -> StdioServerParameters:
    """Construye los parámetros de arranque del servidor MCP de Garmin."""
    email = os.environ.get("GARMIN_EMAIL")
    password = os.environ.get("GARMIN_PASSWORD")

    if not email or not password:
        raise ValueError(
            "Las variables GARMIN_EMAIL y GARMIN_PASSWORD son obligatorias. "
            "Copia .env.example a .env y rellena tus credenciales."
        )

    # Localizar garmin-mcp instalado localmente (vía pip install garmin-mcp)
    # o como fallback uvx (requiere descarga de Python 3.12, falla con Zscaler)
    garmin_cmd = _resolve_command("garmin-mcp")
    use_uvx = False
    if not garmin_cmd:
        uvx_cmd = _resolve_command("uvx")
        if not uvx_cmd:
            raise RuntimeError(
                "No se encontró 'garmin-mcp' ni 'uvx' en el PATH.\n"
                "Instala garmin-mcp con: pip install git+https://github.com/Taxuspt/garmin_mcp\n"
                "o uvx con: pip install uv"
            )
        use_uvx = True

    # Herramientas esenciales para un agente entrenador personal.
    # Reduce el contexto de ~31k tokens (126 tools) a ~5k tokens (~31 tools).
    # Se puede sobreescribir con la variable GARMIN_ENABLED_TOOLS en .env.
    _DEFAULT_TOOLS = (
        # Perfil personal del usuario
        "get_user_profile,"
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
        # Predicciones y récords personales
        "get_race_predictions,get_personal_records,"
        # Tendencias semanales
        "get_weekly_steps,get_weekly_intensity_minutes,get_weekly_stress"
    )
    # Si essential_only=False y no hay override en .env, no se filtra (todas las herramientas)
    if essential_only:
        enabled_tools = os.environ.get("GARMIN_ENABLED_TOOLS", _DEFAULT_TOOLS)
    else:
        enabled_tools = os.environ.get("GARMIN_ENABLED_TOOLS", "")

    if use_uvx:
        command = uvx_cmd
        args = [
            "--python", "3.12",
            "--from", "git+https://github.com/Taxuspt/garmin_mcp",
            "garmin-mcp",
        ]
    else:
        command = garmin_cmd
        args = []

    # Certificado SSL de Zscaler — necesario en redes con proxy SSL corporativo.
    # Se exporta automáticamente desde el almacén de Windows con:
    #   Get-ChildItem Cert:\LocalMachine\Root | Where Subject -match Zscaler
    _project_root = Path(__file__).parent.parent
    _zscaler_pem = _project_root / "zscaler-ca.pem"
    ssl_overrides = {}
    if _zscaler_pem.exists():
        _pem_path = str(_zscaler_pem)
        ssl_overrides = {
            "REQUESTS_CA_BUNDLE": _pem_path,
            "CURL_CA_BUNDLE": _pem_path,
            "SSL_CERT_FILE": _pem_path,
        }

    return StdioServerParameters(
        command=command,
        args=args,
        env={
            **os.environ,
            "GARMIN_EMAIL": email,
            "GARMIN_PASSWORD": password,
            **({"GARMIN_ENABLED_TOOLS": enabled_tools} if enabled_tools else {}),
            **ssl_overrides,
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

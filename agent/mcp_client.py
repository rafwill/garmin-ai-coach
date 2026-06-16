"""
mcp_client.py
Cliente que arranca el servidor MCP de Garmin como subproceso
y expone sus herramientas para que el agente las use.
"""

import os
import shutil
import json
import asyncio
from contextlib import asynccontextmanager
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def _get_server_params() -> StdioServerParameters:
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
        },
    )


@asynccontextmanager
async def garmin_mcp_session():
    """
    Context manager que inicia el servidor MCP de Garmin y devuelve
    una sesión lista para llamar herramientas.

    Uso:
        async with garmin_mcp_session() as session:
            result = await session.call_tool("get_last_activity", {})
    """
    params = _get_server_params()
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

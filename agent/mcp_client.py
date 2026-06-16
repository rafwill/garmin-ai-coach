"""
mcp_client.py
Cliente que arranca el servidor MCP de Garmin como subproceso
y expone sus herramientas para que el agente las use.
"""

import os
import sys
import shutil
import json
import asyncio
from pathlib import Path
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

    # En Windows, asegurarse de que PATH contenga las ubicaciones del sistema y usuario de forma robusta
    if sys.platform == "win32":
        try:
            import winreg
            paths = []
            # Añadir el Path actual de la sesión
            if "PATH" in os.environ:
                paths.append(os.environ["PATH"])
            
            # Leer el Path del sistema
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"System\CurrentControlSet\Control\Session Manager\Environment") as key:
                    sys_path = winreg.QueryValueEx(key, "Path")[0]
                    if sys_path:
                        paths.append(sys_path)
            except Exception:
                pass
                
            # Leer el Path del usuario
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as key:
                    user_path = winreg.QueryValueEx(key, "Path")[0]
                    if user_path:
                        paths.append(user_path)
            except Exception:
                pass
                
            # Combinar y limpiar duplicados preservando el orden
            seen = set()
            clean_paths = []
            for path_str in ";".join(paths).split(";"):
                path_str = path_str.strip()
                if path_str and path_str.lower() not in seen:
                    seen.add(path_str.lower())
                    clean_paths.append(path_str)
                    
            os.environ["PATH"] = ";".join(clean_paths)
        except Exception:
            pass

    # Ruta al certificado Zscaler exportado del Windows Certificate Store
    zscaler_cert = str(Path(__file__).parent.parent / "zscaler-ca.pem")

    # Determinar el ejecutable de npx de forma robusta
    cmd_name = "npx.cmd" if sys.platform == "win32" else "npx"
    resolved_cmd = shutil.which(cmd_name)
    if not resolved_cmd and sys.platform == "win32":
        # Buscar en ubicaciones comunes de Windows si no está en PATH actual
        common_paths = [
            Path(os.environ.get("ProgramFiles", "C:\\Program Files")) / "nodejs" / cmd_name,
            Path(os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)")) / "nodejs" / cmd_name,
            Path(os.environ.get("APPDATA", "")) / "npm" / cmd_name,
            Path(os.environ.get("USERPROFILE", "")) / "AppData" / "Roaming" / "npm" / cmd_name,
        ]
        for p in common_paths:
            if p.exists():
                resolved_cmd = str(p)
                break
    if not resolved_cmd:
        resolved_cmd = cmd_name

    return StdioServerParameters(
        command=resolved_cmd,
        args=["-y", "@nicolasvegam/garmin-connect-mcp@latest"],
        env={
            **os.environ,
            "GARMIN_EMAIL": email,
            "GARMIN_PASSWORD": password,
            # Añade el certificado Zscaler al trust store de Node.js
            "NODE_EXTRA_CA_CERTS": zscaler_cert,
            # Fallback: desactiva verificación SSL si el cert no es suficiente
            "NODE_TLS_REJECT_UNAUTHORIZED": "0",
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

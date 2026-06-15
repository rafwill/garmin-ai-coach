"""
main.py
Punto de entrada del agente entrenador personal GarminCoach.
Interfaz de conversación en terminal.
"""

import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Prompt
from rich.rule import Rule

# Cargar variables de entorno desde .env
load_dotenv()

# Configurar el entorno para Node.js (subproceso npx del MCP de Garmin)
# El proxy corporativo Zscaler hace MITM en SSL; Node.js no confía en su CA por defecto.
# NODE_TLS_REJECT_UNAUTHORIZED=0 deshabilita la verificación SSL en Node.js.
_zscaler_pem = Path(__file__).parent.parent / "zscaler-ca.pem"
if _zscaler_pem.exists():
    os.environ["NODE_EXTRA_CA_CERTS"] = str(_zscaler_pem)
os.environ["NODE_TLS_REJECT_UNAUTHORIZED"] = "0"

# Añadir el directorio raíz al path para imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.mcp_client import garmin_mcp_session
from agent.trainer_agent import TrainerAgent


console = Console()


def _check_env() -> None:
    """Verifica que las variables de entorno obligatorias estén definidas."""
    missing = []
    for var in ["GARMIN_EMAIL", "GARMIN_PASSWORD", "GITHUB_TOKEN"]:
        if not os.environ.get(var):
            missing.append(var)
    if missing:
        console.print(
            f"[bold red]Error:[/] Faltan variables de entorno: {', '.join(missing)}\n"
            "Copia [bold].env.example[/] a [bold].env[/] y rellena tus credenciales."
        )
        sys.exit(1)


async def main() -> None:
    _check_env()

    console.print(Panel.fit(
        "[bold green]GarminCoach[/] — Tu entrenador personal con IA\n"
        "[dim]Conectando con Garmin Connect...[/]",
        border_style="green",
    ))

    async with garmin_mcp_session() as session:
        agent = TrainerAgent(mcp_session=session)

        console.print("[dim]Cargando herramientas de Garmin...[/]")
        await agent.initialize()
        console.print(f"[green]✓[/] {len(agent.tools_schema)} herramientas disponibles\n")

        console.print(Rule("[dim]Escribe tu pregunta o 'salir' para terminar[/]"))

        while True:
            try:
                user_input = Prompt.ask("\n[bold cyan]Tú[/]")
            except (KeyboardInterrupt, EOFError):
                break

            if user_input.strip().lower() in {"salir", "exit", "quit", "q"}:
                break

            if not user_input.strip():
                continue

            with console.status("[bold green]GarminCoach está analizando tus datos...[/]"):
                response = await agent.chat(user_input)

            console.print(f"\n[bold green]GarminCoach[/]")
            console.print(Markdown(response))

    console.print("\n[dim]Sesión finalizada. ¡Hasta el próximo entrenamiento![/]")


if __name__ == "__main__":
    asyncio.run(main())

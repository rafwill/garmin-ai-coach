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


_PROVIDER_INFO = {
    "vpn":    ("GITHUB_TOKEN",  "GitHub Models (gpt-4o-mini)",          "VPN activa"),
    "groq":   ("GROQ_API_KEY",  "Groq (llama-3.3-70b-versatile)",       "100k tokens/día"),
    "gemini": ("GEMINI_API_KEY", "Google Gemini (gemini-2.5-flash)",     "~1M tokens/día · recomendado"),
}


def _check_env(provider: str) -> None:
    """Verifica que las variables de entorno obligatorias estén definidas."""
    missing = []
    env_var, label, _ = _PROVIDER_INFO[provider]
    
    # Validar user/password de Garmin siempre
    for var in ["GARMIN_EMAIL", "GARMIN_PASSWORD"]:
        if not os.environ.get(var):
            missing.append(var)
            
    # Validar token solo si el proveedor lo requiere
    if env_var and not os.environ.get(env_var):
        missing.append(env_var)
        
    if missing:
        hints = {
            "GROQ_API_KEY":  "https://console.groq.com",
            "GEMINI_API_KEY": "https://aistudio.google.com  → Get API key",
            "GITHUB_TOKEN":  "https://github.com/settings/tokens",
        }
        for m in missing:
            if m in hints:
                console.print(f"[bold red]Error:[/] Falta [bold]{m}[/] — obtén tu clave gratuita en {hints[m]}")
            else:
                console.print(f"[bold red]Error:[/] Falta la variable [bold]{m}[/] en el archivo .env")
        sys.exit(1)


def _ask_provider() -> str:
    """Pregunta al usuario qué proveedor de IA usar y devuelve 'vpn', 'groq' o 'gemini'."""
    console.print(Panel.fit(
        "[bold]Selecciona el proveedor de IA:[/]\n\n"
        "  [green]1[/green] · GitHub Models [dim](gpt-4o-mini)[/dim]          — dentro de VPN\n"
        "  [yellow]2[/yellow] · Groq         [dim](llama-3.3-70b)[/dim]       — sin VPN · 100k tokens/día\n"
        "  [cyan]3[/cyan] · Google Gemini [dim](gemini-2.0-flash)[/dim]    — sin VPN · [bold]~1M tokens/día GRATIS[/bold]",
        title="[bold blue]GarminCoach — Proveedor de IA[/]",
        border_style="blue",
    ))
    choice = Prompt.ask(
        "  Tu elección",
        choices=["1", "2", "3"],
        default="3",
        case_sensitive=False,
    )
    return {"1": "vpn", "2": "groq", "3": "gemini"}[choice]


async def main() -> None:
    provider = _ask_provider()
    _check_env(provider)

    _, label, note = _PROVIDER_INFO[provider]
    console.print(Panel.fit(
        f"[bold green]GarminCoach[/] — Tu entrenador personal con IA\n"
        f"[dim]{label} · {note}[/dim]\n"
        "[dim]Conectando con Garmin Connect...[/]",
        border_style="green",
    ))

    async with garmin_mcp_session() as session:
        agent = TrainerAgent(mcp_session=session, provider=provider)

        console.print("[dim]Cargando herramientas de Garmin...[/]")
        await agent.initialize()
        console.print(f"[green]✓[/] {len(agent.tools_schema)} herramientas disponibles\n")
        console.print("[dim dimgray][debug] Inicio de la sesión. Tokens gastados: 0[/]")
        if provider == "gemini":
            daily_info = agent.get_gemini_daily_info()
            if daily_info.get("quota_exhausted", False):
                console.print(
                    f"[bold red][Aviso][/] La API Key de Gemini está marcada hoy como agotada por límite de cuota (RESOURCE_EXHAUSTED).\n"
                    f"[dim dimgray]        - Consumo registrado: {daily_info['today_usage']:,} / {daily_info['limit']:,} tokens hoy.\n"
                    f"        - Tokens gratuitos disponibles: 0[/]"
                )
            else:
                console.print(
                    f"[dim dimgray][debug] Acumulado diario de Gemini: "
                    f"{daily_info['today_usage']:,} / {daily_info['limit']:,} tokens gastados hoy. "
                    f"Te quedan {daily_info['remaining']:,} tokens gratuitos hoy.[/]"
                )

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

            try:
                with console.status("[bold green]GarminCoach está analizando tus datos...[/]"):
                    response = await agent.chat(user_input)
                console.print(f"\n[bold green]GarminCoach[/]")
                console.print(Markdown(response))
            except Exception as e:
                console.print(f"\n[bold red]Error en el Agente:[/] {e}")

        # Al salir de la sesión, mostrar resumen de tokens gastados
        p_tokens = agent.total_prompt_tokens
        c_tokens = agent.total_completion_tokens
        t_tokens = p_tokens + c_tokens
        console.print(f"\n[bold dimgray][debug] Fin de la sesión. Resumen de tokens gastados en esta sesión:[/]")
        console.print(f"[dim dimgray]        - Prompt/Entrada:    {p_tokens:,}[/]")
        console.print(f"[dim dimgray]        - Completion/Salida: {c_tokens:,}[/]")
        console.print(f"[dim dimgray]        - Total acumulado:   {t_tokens:,}[/]")
        if provider == "gemini":
            daily_info = agent.get_gemini_daily_info()
            if daily_info.get("quota_exhausted", False):
                console.print(
                    f"[bold red][Aviso][/] La API Key de Gemini ha marcado hoy el límite de cuota agotada (RESOURCE_EXHAUSTED).\n"
                    f"[dim dimgray]        - Consumo global hoy: {daily_info['today_usage']:,} / {daily_info['limit']:,} tokens.\n"
                    f"        - Tokens gratuitos hoy: 0 (La API de Google bloqueó tu clave debido al plan)[/]"
                )
            else:
                console.print(
                    f"[dim dimgray][debug] Acumulado diario de Gemini global: "
                    f"{daily_info['today_usage']:,} / {daily_info['limit']:,} tokens gastados hoy. "
                    f"Te quedan {daily_info['remaining']:,} tokens gratuitos hoy.[/]"
                )

    console.print("\n[dim]Sesión finalizada. ¡Hasta el próximo entrenamiento![/]")


if __name__ == "__main__":
    asyncio.run(main())

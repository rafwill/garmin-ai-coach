"""
main.py
Punto de entrada del agente entrenador personal GarminCoach.
Interfaz de conversación en terminal.
"""

import asyncio
import hashlib
import os
import re
import sys
from datetime import date
from pathlib import Path
import logging

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("agent.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ]
)


# Forzar encoding UTF-8 para evitar errores de Unicode en Windows
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

import httpx
from dotenv import load_dotenv
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Prompt
from rich.rule import Rule

# Cargar variables de entorno desde .env
load_dotenv(encoding="utf-8")

# Parchear SSL para usar el almacén de certificados del sistema (necesario con Zscaler)
try:
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass

# Añadir el directorio raíz al path para imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.mcp_client import garmin_mcp_session
from agent.trainer_agent import TrainerAgent, _load_user_profile, _save_user_profile
from agent.storage import check_supabase_connection, migrate_local_to_supabase


def _garmin_user_id() -> str:
    """Devuelve un identificador único basado en el email de Garmin configurado en .env."""
    email = os.environ.get("GARMIN_EMAIL", "").strip().lower()
    return hashlib.sha256(email.encode()).hexdigest()[:16] if email else "unknown"


async def _sync_from_garmin(agent) -> None:
    """
    Siempre se ejecuta al arrancar.
    Obtiene nombre, edad, género, peso y altura desde Garmin Connect
    y actualiza el perfil local sin hacer ninguna pregunta.
    """
    console.print("[dim]Sincronizando datos personales desde Garmin Connect...[/]")
    try:
        garmin_data = await agent.fetch_garmin_personal_data()
    except Exception:
        garmin_data = {}

    if not garmin_data:
        console.print("[dim yellow]No se pudieron obtener datos personales de Garmin.[/]")
        return

    profile = _load_user_profile()
    p = profile.setdefault("personal", {})
    # Guardar qué usuario de Garmin generó estos datos
    profile["garmin_user_id"] = _garmin_user_id()

    updated = []
    labels = {"name": "nombre", "age": "edad", "gender": "género",
               "weight_kg": "peso", "height_cm": "altura"}
    for field, value in garmin_data.items():
        if p.get(field) != value:
            p[field] = value
            if field in labels:
                updated.append(labels[field])

    _save_user_profile(profile)
    if updated:
        console.print(f"[green]✓[/] Garmin → perfil actualizado: [dim]{', '.join(updated)}[/]")
    else:
        console.print("[dim]✓ Datos de Garmin sin cambios.[/]")


def _is_first_time() -> bool:
    """
    Devuelve True si este usuario de Garmin no ha completado el setup de objetivos.
    Se detecta por la ausencia de 'setup_complete' o por cambio de cuenta de Garmin.
    """
    profile = _load_user_profile()
    current_uid = _garmin_user_id()
    stored_uid = profile.get("garmin_user_id", "")
    # Si cambió el usuario de Garmin, es como una primera vez
    if stored_uid and stored_uid != current_uid:
        return True
    return not profile.get("setup_complete", False)


def _validate_date(value: str) -> tuple[bool, str]:
    """Valida formato YYYY-MM-DD y que la fecha sea futura. Devuelve (valido, mensaje_error)."""
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", value):
        return False, "Formato incorrecto. Usa YYYY-MM-DD (ej: 2026-07-02)"
    try:
        d = date.fromisoformat(value)
    except ValueError:
        return False, "Fecha inválida (mes o día fuera de rango)"
    if d <= date.today():
        return False, f"La fecha debe ser futura (hoy es {date.today().isoformat()})"
    return True, ""


def _validate_time(value: str) -> tuple[bool, str]:
    """Valida formato H:MM:SS o HH:MM:SS. Devuelve (valido, mensaje_error)."""
    m = re.match(r"^(\d{1,3}):(\d{2}):(\d{2})$", value)
    if not m:
        return False, "Formato incorrecto. Usa H:MM:SS (ej: 9:30:00) o HH:MM:SS (ej: 13:45:00)"
    h, mn, s = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if mn > 59 or s > 59:
        return False, "Minutos y segundos deben estar entre 00 y 59"
    if h > 99:
        return False, "Horas deben ser menores de 100"
    return True, ""


def _validate_hours(value: str) -> tuple[bool, str]:
    """Valida horas de entrenamiento semanal (0.5 – 40). Devuelve (valido, mensaje_error)."""
    try:
        h = float(value.replace(",", "."))
    except ValueError:
        return False, "Introduce un número (ej: 8 o 10.5)"
    if h < 0.5:
        return False, "El mínimo es 0.5 horas/semana"
    if h > 40:
        return False, "El máximo es 40 horas/semana. ¿Seguro?"
    return True, ""


def _ask_goals(profile: dict) -> None:
    """Pregunta y guarda nombre + campos de objetivos de entrenamiento."""
    p = profile.setdefault("personal", {})
    g = profile.setdefault("goals", {})

    # Nombre — Garmin no lo expone en su API
    console.print("\n[bold]Datos personales:[/]")
    user_name = Prompt.ask(
        "  Tu nombre [dim](ej: Rafael · Enter para omitir)[/]",
        default=p.get("name", ""),
    ).strip()
    if user_name:
        p["name"] = user_name

    console.print("\n[bold]Objetivos de entrenamiento:[/]")

    sport = Prompt.ask(
        "  Deporte principal [dim](Enter para omitir)[/]",
        choices=["running", "trail running", "triatlón", "ciclismo", "otro"],
        default=g.get("primary", "running") or "running",
        show_choices=True,
    )
    if sport.strip():
        g["primary"] = sport.strip()

    # Horas/semana con validación de rango
    while True:
        hours = Prompt.ask(
            "  Horas de entrenamiento por semana [dim](ej: 10 · Enter para omitir)[/]",
            default=str(g.get("weekly_training_hours", "")),
        )
        if not hours.strip():
            break
        ok, err = _validate_hours(hours.strip())
        if ok:
            g["weekly_training_hours"] = float(hours.strip().replace(",", "."))
            break
        console.print(f"  [red]✗[/] {err}")

    race = Prompt.ask(
        "  Próxima carrera/evento objetivo [dim](ej: Ultra PDA 55km · Enter para omitir)[/]",
        default=g.get("target_race", ""),
    )
    if race.strip():
        g["target_race"] = race.strip()

    # Fecha del evento con validación de formato y fecha futura
    while True:
        race_date = Prompt.ask(
            "  Fecha del evento [dim](YYYY-MM-DD · Enter para omitir)[/]",
            default=g.get("target_race_date", ""),
        )
        if not race_date.strip():
            break
        ok, err = _validate_date(race_date.strip())
        if ok:
            g["target_race_date"] = race_date.strip()
            break
        console.print(f"  [red]✗[/] {err}")

    # Tiempo objetivo con validación de formato
    while True:
        target_time = Prompt.ask(
            "  Tiempo objetivo [dim](H:MM:SS · ej: 9:30:00 · Enter para omitir)[/]",
            default=g.get("target_time", ""),
        )
        if not target_time.strip():
            break
        ok, err = _validate_time(target_time.strip())
        if ok:
            g["target_time"] = target_time.strip()
            break
        console.print(f"  [red]✗[/] {err}")


def _ask_health(profile: dict) -> None:
    """Pregunta y guarda los campos de salud."""
    h = profile.setdefault("health", {})
    console.print("\n[bold]Salud y condiciones médicas:[/]")

    current_injuries = ", ".join(h.get("injuries", []))
    injuries_str = Prompt.ask(
        "  Lesiones o enfermedades [dim](sep. con coma, ej: DT1, tendinitis)[/]",
        default=current_injuries,
    )
    h["injuries"] = [i.strip() for i in injuries_str.split(",") if i.strip()]

    notes = Prompt.ask(
        "  Notas adicionales de salud [dim](opcional)[/]",
        default=h.get("notes", ""),
    )
    h["notes"] = notes.strip()


def _run_first_time_setup() -> None:
    """
    Solo se ejecuta la primera vez.
    Pregunta objetivos + salud y marca el perfil como configurado.
    """
    profile = _load_user_profile()
    p = profile.setdefault("personal", {})
    name = p.get("name", "atleta")

    console.print(Panel.fit(
        f"[bold]Bienvenido, {name}![/] Primera vez configurando GarminCoach.\n"
        "Necesito conocer tus objetivos y condiciones de salud\n"
        "para personalizar todas las recomendaciones.\n"
        "[dim]Pulsa Enter para omitir cualquier campo.[/]",
        title="[bold green]GarminCoach — Configuración inicial[/]",
        border_style="green",
    ))

    _ask_goals(profile)
    _ask_health(profile)
    profile["setup_complete"] = True
    profile["garmin_user_id"] = _garmin_user_id()
    _save_user_profile(profile)
    final_name = profile.get("personal", {}).get("name") or name
    console.print(f"\n[green]✓[/] ¡Todo listo, [bold]{final_name}[/]! Ya puedes empezar.\n")


def _show_profile() -> None:
    """Muestra el perfil actual del usuario."""
    profile = _load_user_profile()
    p = profile.get("personal", {})
    g = profile.get("goals", {})
    h = profile.get("health", {})

    if not p and not g and not h:
        console.print("[yellow]Perfil vacío.[/] Usa [bold]/perfil editar objetivo[/] o [bold]/perfil editar salud[/].")
        return

    lines = [
        f"[bold]Nombre:[/]          {p.get('name', '—')}",
        f"[bold]Edad:[/]            {p.get('age', '—')} años",
        f"[bold]Género:[/]          {p.get('gender', '—')}",
        f"[bold]Peso:[/]            {p.get('weight_kg', '—')} kg",
        f"[bold]Altura:[/]          {p.get('height_cm', '—')} cm",
        "",
        f"[bold]Deporte:[/]         {g.get('primary', '—')}",
        f"[bold]Horas/semana:[/]    {g.get('weekly_training_hours', '—')}",
        f"[bold]Evento objetivo:[/] {g.get('target_race', '—')}",
        f"[bold]Fecha evento:[/]    {g.get('target_race_date', '—')}",
        f"[bold]Tiempo objetivo:[/] {g.get('target_time', '—')}",
    ]
    injuries = h.get("injuries", [])
    lines.append(f"[bold]Lesiones:[/]        {', '.join(injuries) if injuries else '—'}")
    if h.get("notes"):
        lines.append(f"[bold]Notas salud:[/]     {h['notes']}")

    console.print(Panel(
        "\n".join(lines),
        title="[bold blue]Tu perfil[/]",
        border_style="blue",
    ))
    console.print(
        "[dim]Editar: "
        "[bold]/perfil editar objetivo[/bold] — nombre, deporte, evento, tiempo objetivo • "
        "[bold]/perfil editar salud[/bold] — lesiones y notas[/dim]"
    )


def _show_help() -> None:
    """Muestra la ayuda del agente: ejemplos de preguntas, comandos y guía de indicadores."""
    console.print(Panel(
        "[bold]Ejemplos de preguntas:[/]\n"
        "  \u00b7 \u00bfCómo estoy hoy? \u00bfPuedo entrenar fuerte?\n"
        "  \u00b7 Analízame el último entrenamiento\n"
        "  \u00b7 \u00bfCómo ha evolucionado mi VO\u2082máx en las últimas semanas?\n"
        "  \u00b7 Propónme un plan de entrenamiento para esta semana\n"
        "  \u00b7 \u00bfCuáles son mis récords personales?\n"
        "  \u00b7 \u00bfCómo he dormido últimamente?\n"
        "  \u00b7 \u00bfQué ritmo debería llevar en mi próxima carrera?\n"
        "  \u00b7 Analízame mi estado de forma general\n"
        "\n"
        "[bold]Comandos disponibles:[/]\n"
        "  [bold cyan]/perfil[/bold cyan]                  Ver tu perfil completo\n"
        "  [bold cyan]/perfil editar objetivo[/bold cyan]  Cambiar deporte, carrera, tiempo meta\n"
        "  [bold cyan]/perfil editar salud[/bold cyan]     Cambiar lesiones y notas de salud\n"
        "  [bold cyan]/perfil editar[/bold cyan]           Editar todo el perfil\n"
        "  [bold cyan]/modelo[/bold cyan]                  Cambiar el proveedor de modelo de IA activo\n"
        "  [bold cyan]/ayuda[/bold cyan]                   Mostrar esta pantalla\n"
        "  [bold cyan]salir[/bold cyan]                    Terminar la sesión\n"
        "\n"
        "[bold]Guía rápida de indicadores Garmin:[/]\n"
        "  [bold]Body Battery[/bold]       90-100 recuperado \u00b7 70-89 bien \u00b7 40-69 moderado \u00b7 <40 descansa\n"
        "  [bold]Training Readiness[/bold] >70 entrena fuerte \u00b7 40-70 suave \u00b7 <40 recuperación activa\n"
        "  [bold]HRV[/bold]               Caída >20% = fatiga, estrés o mal control glucémico (DT1)\n"
        "  [bold]Training Status[/bold]   Productive=en forma \u00b7 Peaking=pico \u00b7 Overreaching=alarma",
        title="[bold blue]GarminCoach — Ayuda[/]",
        border_style="blue",
    ))


console = Console()


_PROVIDER_INFO = {
    "vpn":      ("GITHUB_TOKEN",     "GitHub Models (gpt-4o-mini)",         "VPN activa"),
    "groq":     ("GROQ_API_KEY",     "Groq (llama-3.3-70b-versatile)",      "100k tokens/día"),
    "gemini":   ("GEMINI_API_KEY",   "Google Gemini (gemini-2.0-flash)",    "~1M tokens/día gratis"),
    "mistral":  ("MISTRAL_API_KEY",  "Mistral (mistral-small-latest)",      "capa gratuita · console.mistral.ai"),
    "cerebras": ("CEREBRAS_API_KEY", "Cerebras (llama-3.3-70b)",            "ultrarrápido · gratis · cloud.cerebras.ai"),
    "nvidia":   ("NVIDIA_API_KEY",   "NVIDIA NIM (llama3-70b-instruct)",    "API compatible OpenAI · build.nvidia.com"),
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
            "GROQ_API_KEY":     "https://console.groq.com",
            "GEMINI_API_KEY":   "https://aistudio.google.com  → Get API key",
            "GITHUB_TOKEN":    "https://github.com/settings/tokens",
            "MISTRAL_API_KEY": "https://console.mistral.ai  → API Keys",
            "CEREBRAS_API_KEY": "https://cloud.cerebras.ai  → API Keys",
            "NVIDIA_API_KEY":   "https://build.nvidia.com/explore/discover",
        }
        for m in missing:
            if m in hints:
                console.print(f"[bold red]Error:[/] Falta [bold]{m}[/] — obtén tu clave gratuita en {hints[m]}")
            else:
                console.print(f"[bold red]Error:[/] Falta la variable [bold]{m}[/] en el archivo .env")
        sys.exit(1)


# Valores placeholder que indican que la clave no está configurada
_PLACEHOLDER_VALUES = {"", "tu_clave_mistral", "tu_clave_gemini", "gsk_...", "AIzaSy_tu_clave_de_gemini", "ghp_..."}


def _detect_zscaler() -> bool:  # noqa: F811  (sobrescribe el import con la misma semántica)
    """
    Detecta si el tráfico sale a través de Zscaler (red corporativa).
    Delegado a agent.storage.is_zscaler_network() para mantener la lógica
    en un único lugar y reutilizar la caché entre módulos.
    """
    from agent.storage import is_zscaler_network
    return is_zscaler_network()


def _best_available_provider() -> str | None:
    """
    Devuelve el mejor proveedor configurado para uso fuera de VPN.
    Prioridad: gemini → mistral → groq → cerebras.
    Gemini se salta si la cuota diaria está marcada como agotada.
    """
    from agent.storage import is_gemini_quota_exhausted

    candidates = [
        ("gemini",   "GEMINI_API_KEY"),
        ("mistral",  "MISTRAL_API_KEY"),
        ("groq",     "GROQ_API_KEY"),
        ("cerebras", "CEREBRAS_API_KEY"),
        ("nvidia",   "NVIDIA_API_KEY"),
    ]
    for name, env_var in candidates:
        val = os.environ.get(env_var, "")
        if val and val not in _PLACEHOLDER_VALUES:
            if name == "gemini" and is_gemini_quota_exhausted(val):
                console.print(
                    "[dim yellow]Gemini: cuota agotada, probando siguiente proveedor...[/]"
                )
                continue
            return name
    return None


def _select_provider_menu(on_vpn: bool) -> str:
    """Muestra el menú de selección de proveedor LLM."""
    from agent.storage import is_gemini_quota_exhausted, get_gemini_daily_usage

    # Sin VPN o cambio de modelo: construir lista de proveedores disponibles
    candidates = [
        ("gemini",   "GEMINI_API_KEY"),
        ("mistral",  "MISTRAL_API_KEY"),
        ("groq",     "GROQ_API_KEY"),
        ("cerebras", "CEREBRAS_API_KEY"),
        ("nvidia",   "NVIDIA_API_KEY"),
    ]
    available = []
    for name, env_var in candidates:
        val = os.environ.get(env_var, "")
        if val and val not in _PLACEHOLDER_VALUES:
            available.append((name, env_var))

    if not available:
        console.print(
            "[bold red]Error:[/] No hay ninguna API key configurada en .env\n"
            "Configura al menos una de: [bold]GEMINI_API_KEY[/], [bold]MISTRAL_API_KEY[/], "
            "[bold]GROQ_API_KEY[/]"
        )
        sys.exit(1)

    # Mostrar menú de selección
    menu_lines = ["[bold]Elige el modelo de IA:[/]\n"]
    for i, (name, env_var) in enumerate(available, 1):
        _, label, note = _PROVIDER_INFO[name]
        api_key = os.environ.get(env_var, "")
        extra = ""
        
        # Consultar la base de datos de forma dinámica
        usage = get_gemini_daily_usage(api_key) if api_key else 0
        exhausted = is_gemini_quota_exhausted(api_key) if api_key else False
        
        if exhausted:
            extra = " [dim red](cuota agotada hoy)[/dim red]"
        elif usage > 0:
            extra = f" [dim cyan]({usage:,} tokens consumidos hoy)[/dim cyan]"
        elif i == 1:
            extra = "  [bold]← recomendado[/bold]"
            
        menu_lines.append(f"  [green]{i}[/green] · {label}  [dim]({note})[/dim]{extra}")

    console.print(Panel.fit(
        "\n".join(menu_lines),
        title="[bold blue]GarminCoach — Selección de modelo[/]",
        border_style="blue",
    ))
    choices = [str(i) for i in range(1, len(available) + 1)]
    choice = Prompt.ask("  Tu elección", choices=choices, default="1", case_sensitive=False)
    return available[int(choice) - 1][0]


def _auto_select_provider() -> str:
    """
    Detecta el entorno de red (VPN corporativa con Zscaler vs. acceso libre)
    y selecciona el proveedor LLM.
    - Con Zscaler → GitHub Models (acceso permitido por la VPN) — automático
    - Sin Zscaler → menú para elegir entre los proveedores configurados en .env
    """
    console.print("[dim]Detectando entorno de red...[/]")
    on_vpn = _detect_zscaler()

    if on_vpn:
        provider = "vpn"
        token = os.environ.get("GITHUB_TOKEN", "")
        if not token or token in _PLACEHOLDER_VALUES:
            console.print(
                "[bold red]Error:[/] Red corporativa (Zscaler) detectada, pero falta "
                "[bold]GITHUB_TOKEN[/] en .env\n"
                "Obtén un token gratuito en: https://github.com/settings/tokens"
            )
            sys.exit(1)
        _, label, note = _PROVIDER_INFO[provider]
        console.print(Panel.fit(
            f"🏢  Red corporativa [dim](Zscaler detectado)[/dim]\n"
            f"[bold green]Proveedor seleccionado:[/] {label}\n"
            f"[dim]{note}[/dim]",
            title="[bold blue]GarminCoach — Entorno detectado[/]",
            border_style="blue",
        ))
        return provider

    provider = _select_provider_menu(on_vpn=False)
    _, label, note = _PROVIDER_INFO[provider]
    console.print(Panel.fit(
        f"🌐  Sin VPN corporativa\n"
        f"[bold green]Proveedor seleccionado:[/] {label}\n"
        f"[dim]{note}[/dim]",
        title="[bold blue]GarminCoach — Entorno detectado[/]",
        border_style="blue",
    ))
    return provider


def _ask_tool_mode(provider: str) -> bool:
    """Pregunta al usuario si quiere usar Essential Tools (28) o todas las herramientas (126).

    Con GitHub Models (vpn) se fuerza Essential Tools: los schemas de 126 herramientas
    ya superan el límite de 8k tokens antes de enviar ninguna pregunta.
    """
    if provider == "vpn":
        console.print(Panel.fit(
            "[yellow]GitHub Models[/yellow] tiene un límite de 8 000 tokens por request.\n"
            "Con 126 herramientas los schemas solos superan ese límite, por lo que\n"
            "se usa automáticamente [bold]Essential Tools (30 tools)[/bold].\n"
            "[dim]Para usar todas las herramientas, sal de la VPN y reinicia (usará Gemini).[/dim]",
            title="[bold blue]GarminCoach — Herramientas[/]",
            border_style="blue",
        ))
        return True  # essential_only=True

    console.print(Panel.fit(
        "[bold]Selecciona el modo de herramientas:[/]\n\n"
        "  [green]1[/green] · Essential Tools [dim](28 tools)[/dim]   — más rápido · menor consumo de tokens  [bold]← recomendado[/bold]\n"
        "  [yellow]2[/yellow] · Todas          [dim](126 tools)[/dim]  — acceso completo · más tokens por petición",
        title="[bold blue]GarminCoach — Herramientas[/]",
        border_style="blue",
    ))
    choice = Prompt.ask(
        "  Tu elección",
        choices=["1", "2"],
        default="1",
        case_sensitive=False,
    )
    return choice == "1"


def _check_and_migrate_supabase() -> None:
    """
    Comprueba la conectividad con Supabase al arrancar y muestra el estado.
    Si Supabase acaba de conectarse y hay datos locales, los migra automáticamente.
    """
    status = check_supabase_connection()

    if not status["configured"]:
        return  # Supabase no configurado, modo solo-ficheros silencioso

    if status["connected"]:
        # Intentar migrar datos locales (solo mueve si la tabla está vacía para este usuario)
        migrated = migrate_local_to_supabase()
        migrated_items = [k for k, v in migrated.items() if v]
        if migrated_items:
            labels = {"profile": "perfil", "context": "historial de sesiones", "gemini": "uso de Gemini"}
            items_str = ", ".join(labels[i] for i in migrated_items)
            console.print(f"[green]✓[/] Supabase — datos migrados desde ficheros locales: [dim]{items_str}[/]")
        else:
            console.print("[green]✓[/] Supabase conectado — memoria guardada en la nube")
    else:
        console.print(
            f"[bold yellow]⚠[/] Supabase configurado pero no accesible — "
            f"datos guardados solo en ficheros locales\n"
            f"  [dim]Error: {status['error']}[/]"
        )


async def main() -> None:
    provider = _auto_select_provider()
    _check_env(provider)
    essential_only = _ask_tool_mode(provider)

    _, label, note = _PROVIDER_INFO[provider]
    console.print(Panel.fit(
        f"[bold green]GarminCoach[/] — Tu entrenador personal con IA\n"
        f"[dim]{label} · {note}[/dim]\n"
        "[dim]Conectando con Garmin Connect...[/]",
        border_style="green",
    ))

    async with garmin_mcp_session(essential_only=essential_only) as session:
        agent = TrainerAgent(mcp_session=session, provider=provider)

        console.print("[dim]Cargando herramientas de Garmin...[/]")
        await agent.initialize()
        console.print(f"[green]✓[/] {len(agent.tools_schema)} herramientas disponibles\n")

        # Comprobar conectividad con Supabase y migrar datos locales si procede
        _check_and_migrate_supabase()

        # Paso 1: comprobar cambio de cuenta ANTES de que sync sobreescriba garmin_user_id
        is_first = _is_first_time()
        if is_first:
            profile = _load_user_profile()
            stored_uid = profile.get("garmin_user_id", "")
            if stored_uid and stored_uid != _garmin_user_id():
                console.print("[yellow]⚠ Cuenta de Garmin diferente detectada. Reiniciando objetivos y salud...[/]")
                profile.pop("goals", None)
                profile.pop("health", None)
                profile.pop("personal", None)
                profile.pop("setup_complete", None)
                _save_user_profile(profile)

        # Paso 2: sincronizar datos personales desde Garmin (sobreescribe garmin_user_id)
        await _sync_from_garmin(agent)
        agent.user_profile = _load_user_profile()

        # Paso 3: solo la primera vez (tras haber limpiado el perfil si cambió la cuenta)
        if is_first:
            _run_first_time_setup()
            agent.user_profile = _load_user_profile()
        console.print("[dim dimgray][debug] Inicio de la sesión. Tokens gastados: 0[/]")
        
        daily_info = agent.get_daily_usage_info()
        provider_name = provider.capitalize() if provider != "vpn" else "GitHub Models"
        if daily_info.get("quota_exhausted", False):
            console.print(
                f"[bold red][Aviso][/] La API Key de {provider_name} está marcada hoy como agotada por límite de cuota.\n"
                f"[dim dimgray]        - Consumo registrado: {daily_info['today_usage']:,} / {daily_info['limit']:,} tokens hoy.\n"
                f"        - Tokens disponibles hoy: 0[/]"
            )
        else:
            console.print(
                f"[dim dimgray][debug] Acumulado diario de {provider_name}: "
                f"{daily_info['today_usage']:,} / {daily_info['limit']:,} tokens gastados hoy. "
                f"Te quedan {daily_info['remaining']:,} tokens hoy.[/]"
            )

        console.print(Rule("[dim]Escribe tu pregunta · [bold]/perfil[/bold] · [bold]/modelo[/bold] · [bold]/perfil editar objetivo[/bold] · [bold]/perfil editar salud[/bold] · [bold]salir[/bold][/]"))

        while True:
            try:
                user_input = Prompt.ask("\n[bold cyan]Tú[/]")
            except (KeyboardInterrupt, EOFError):
                break

            if user_input.strip().lower() in {"salir", "exit", "quit", "q"}:
                break

            # Comandos especiales
            cmd = user_input.strip().lower()

            if cmd in {"/modelo", "/model"}:
                # Mostrar tokens usados para el proveedor que está a punto de desactivarse
                p_tokens = agent.total_prompt_tokens
                c_tokens = agent.total_completion_tokens
                t_tokens = p_tokens + c_tokens
                old_provider_name = provider.capitalize() if provider != "vpn" else "GitHub Models"
                console.print(f"\n[bold dimgray][debug] Cambiando de modelo. Tokens gastados con {old_provider_name} en esta sesión:[/]")
                console.print(f"[dim dimgray]        - Prompt/Entrada:    {p_tokens:,}[/]")
                console.print(f"[dim dimgray]        - Completion/Salida: {c_tokens:,}[/]")
                console.print(f"[dim dimgray]        - Total acumulado:   {t_tokens:,}[/]")
                
                # Seleccionar nuevo proveedor
                on_vpn = _detect_zscaler()
                new_provider = _select_provider_menu(on_vpn=on_vpn)
                
                if new_provider == provider:
                    console.print(f"[yellow]El modelo {old_provider_name} ya está activo.[/]")
                    continue
                
                # Actualizar el proveedor y reiniciar los tokens de sesión para el nuevo tramo
                provider = new_provider
                _check_env(provider)
                agent.set_provider(provider)
                agent.total_prompt_tokens = 0
                agent.total_completion_tokens = 0
                
                new_provider_name = provider.capitalize() if provider != "vpn" else "GitHub Models"
                _, label, note = _PROVIDER_INFO[provider]
                console.print(Panel.fit(
                    f"[bold green]Modelo cambiado con éxito[/]\n"
                    f"Nuevo proveedor: {label}\n"
                    f"[dim]{note}[/dim]",
                    title="[bold blue]GarminCoach — Cambio de modelo[/]",
                    border_style="green",
                ))
                
                # Mostrar info de cuota para el nuevo proveedor
                daily_info = agent.get_daily_usage_info()
                if daily_info.get("quota_exhausted", False):
                    console.print(
                        f"[bold red][Aviso][/] La API Key de {new_provider_name} está marcada hoy como agotada por límite de cuota.\n"
                        f"[dim dimgray]        - Consumo registrado: {daily_info['today_usage']:,} / {daily_info['limit']:,} tokens hoy.\n"
                        f"        - Tokens disponibles hoy: 0[/]"
                    )
                else:
                    console.print(
                        f"[dim dimgray][debug] Acumulado diario de {new_provider_name}: "
                        f"{daily_info['today_usage']:,} / {daily_info['limit']:,} tokens gastados hoy. "
                        f"Te quedan {daily_info['remaining']:,} tokens hoy.[/]"
                    )
                continue

            if cmd in {"/perfil", "/profile"}:
                _show_profile()
                continue

            if cmd in {"/ayuda", "/help", "/?"} :
                _show_help()
                continue

            if cmd in {"/perfil editar objetivo", "/perfil editar goal"}:
                profile = _load_user_profile()
                _ask_goals(profile)
                _save_user_profile(profile)
                agent.user_profile = _load_user_profile()
                console.print("[green]✓[/] Objetivos actualizados.")
                continue

            if cmd in {"/perfil editar salud", "/perfil editar health"}:
                profile = _load_user_profile()
                _ask_health(profile)
                _save_user_profile(profile)
                agent.user_profile = _load_user_profile()
                console.print("[green]✓[/] Datos de salud actualizados.")
                continue

            if cmd in {"/perfil editar", "/profile edit"}:
                profile = _load_user_profile()
                _ask_goals(profile)
                _ask_health(profile)
                _save_user_profile(profile)
                agent.user_profile = _load_user_profile()
                console.print("[green]✓[/] Perfil actualizado.")
                continue

            if not user_input.strip():
                continue

            try:
                with console.status("[bold green]GarminCoach está analizando tus datos...[/]"):
                    response = await agent.chat(user_input)
                console.print(f"\n[bold green]GarminCoach[/]")
                console.print(Markdown(response))
            except Exception as e:
                console.print(f"\n[bold red]Error en el Agente:[/] {e}")

        # Al salir de la sesión, generar y guardar resumen para memoria futura
        if agent.conversation_history:
            try:
                with console.status("[dim]Guardando resumen de sesión...[/]"):
                    summary = await agent.generate_session_summary()
                agent.save_session_summary(summary)
                console.print("[dim]✓ Sesión guardada en memoria[/]")
            except Exception:
                pass

        # Al salir de la sesión, mostrar resumen de tokens gastados
        p_tokens = agent.total_prompt_tokens
        c_tokens = agent.total_completion_tokens
        t_tokens = p_tokens + c_tokens
        console.print(f"\n[bold dimgray][debug] Fin de la sesión. Resumen de tokens gastados en esta sesión:[/]")
        console.print(f"[dim dimgray]        - Prompt/Entrada:    {p_tokens:,}[/]")
        console.print(f"[dim dimgray]        - Completion/Salida: {c_tokens:,}[/]")
        console.print(f"[dim dimgray]        - Total acumulado:   {t_tokens:,}[/]")
        
        daily_info = agent.get_daily_usage_info()
        provider_name = provider.capitalize() if provider != "vpn" else "GitHub Models"
        if daily_info.get("quota_exhausted", False):
            console.print(
                f"[bold red][Aviso][/] La API Key de {provider_name} ha marcado hoy el límite de cuota agotada.\n"
                f"[dim dimgray]        - Consumo global hoy: {daily_info['today_usage']:,} / {daily_info['limit']:,} tokens.\n"
                f"        - Tokens gratuitos hoy: 0[/]"
            )
        else:
            console.print(
                f"[dim dimgray][debug] Acumulado diario de {provider_name} global: "
                f"{daily_info['today_usage']:,} / {daily_info['limit']:,} tokens gastados hoy. "
                f"Te quedan {daily_info['remaining']:,} tokens hoy.[/]"
            )

    console.print("\n[dim]Sesión finalizada. ¡Hasta el próximo entrenamiento![/]")


if __name__ == "__main__":
    asyncio.run(main())

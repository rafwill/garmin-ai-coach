"""
main.py
Punto de entrada del agente entrenador personal Kairos Coach.
Interfaz de conversación en terminal.
"""

import asyncio
import json
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

from dotenv import load_dotenv
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Prompt
from rich.rule import Rule
from rich.table import Table

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
from agent.storage import (
    activate_training_plan,
    authenticate_app_user,
    check_supabase_connection,
    create_training_plan,
    decrypt_password,
    encrypt_password,
    find_user_by_username,
    get_active_user,
    get_training_plan,
    is_zscaler_network as _detect_zscaler,
    list_training_plans,
    list_training_plan_sessions,
    load_athlete_knowledge,
    register_app_user,
    save_athlete_knowledge,
    set_active_user,
    update_app_user_password,
    update_user_credentials,
)


async def _sync_from_garmin(agent) -> list[str]:
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
        return []

    profile = _load_user_profile()
    p = profile.setdefault("personal", {})

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
    return updated


def _is_first_time() -> bool:
    """
    Devuelve True si el usuario activo no ha completado el setup de objetivos.
    Se detecta por la ausencia de 'setup_complete'.
    """
    profile = _load_user_profile()
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


def _build_initial_athlete_knowledge(profile: dict) -> str:
    """Genera una base de conocimiento inicial mínima a partir del perfil."""
    p = profile.get("personal", {})
    g = profile.get("goals", {})
    h = profile.get("health", {})
    injuries = ", ".join(h.get("injuries", [])) if h.get("injuries") else ""

    lines = [
        "# Base de Conocimiento del Atleta",
        "",
        "## Identidad deportiva",
        f"- Nombre: {p.get('name', '')}",
        f"- Deporte principal: {g.get('primary', '')}",
        "",
        "## Perfil fisiologico y biomarcadores",
        f"- Altura: {p.get('height_cm', '')}",
        f"- Peso: {p.get('weight_kg', '')}",
        "",
        "## Objetivo principal de carrera",
        f"- Evento objetivo: {g.get('target_race', '')}",
        f"- Fecha: {g.get('target_race_date', '')}",
        f"- Tiempo objetivo: {g.get('target_time', '')}",
        "",
        "## Salud y limitaciones",
        f"- Condiciones medicas relevantes: {injuries}",
        f"- Notas: {h.get('notes', '')}",
        "",
        "## Reglas del coach",
        "- Priorizar seguridad y continuidad del entrenamiento.",
        "- Ajustar cargas según recuperación y contexto personal.",
    ]
    return "\n".join(lines).strip() + "\n"


def _build_enriched_athlete_knowledge(profile: dict, enrichment: dict) -> str:
    """Combina perfil + contexto MCP para persistir una KB inicial mas completa."""
    base = _build_initial_athlete_knowledge(profile).rstrip()
    personal = enrichment.get("personal", {}) if isinstance(enrichment, dict) else {}
    startup = enrichment.get("startup_48h", {}) if isinstance(enrichment, dict) else {}

    lines = [base, "", "## Enriquecimiento MCP (arranque)"]
    if personal:
        lines.append("- Datos personales confirmados por Garmin Connect:")
        for key in ("age", "gender", "weight_kg", "height_cm"):
            if personal.get(key) is not None and personal.get(key) != "":
                lines.append(f"  - {key}: {personal.get(key)}")
    else:
        lines.append("- Datos personales MCP: no disponibles en este arranque")

    lines.append("")
    lines.append("## Estado de las ultimas 48h")
    bb = (startup.get("body_battery") or {}).get("summary", "sin datos") if isinstance(startup, dict) else "sin datos"
    hrv = (startup.get("hrv") or {}).get("summary", "sin datos") if isinstance(startup, dict) else "sin datos"
    sleep = (startup.get("sleep") or {}).get("summary", "sin datos") if isinstance(startup, dict) else "sin datos"
    lines.append(f"- Body Battery: {bb}")
    lines.append(f"- HRV: {hrv}")
    lines.append(f"- Sueno: {sleep}")

    trainings = startup.get("trainings", []) if isinstance(startup, dict) else []
    if trainings:
        lines.append("- Entrenamientos recientes:")
        for item in trainings[:5]:
            day = item.get("date") or "fecha desconocida"
            name = item.get("name") or "Actividad"
            lines.append(f"  - {day}: {name}")
    else:
        lines.append("- Entrenamientos recientes: sin datos")

    lines.append("")
    lines.append("## Contexto crudo MCP (resumen)")
    try:
        raw_preview = json.dumps(enrichment, ensure_ascii=False)[:1200]
        lines.append(f"```json\n{raw_preview}\n```")
    except Exception:
        lines.append("- No se pudo serializar el resumen MCP")

    return "\n".join(lines).strip() + "\n"


def _authenticate_or_register_user() -> tuple[str, dict, bool, str]:
    """Flujo inicial de acceso: auto-login si usuario existe, registro si no.

    Returns:
        (username, credentials, is_new_user, app_password)
    """
    console.print(Panel.fit(
        "[bold]Acceso a Kairos Coach[/]\n"
        "Trabajarás con un perfil independiente por usuario.",
        title="[bold blue]Identificación[/]",
        border_style="blue",
    ))

    while True:
        username = Prompt.ask("Usuario").strip().lower()
        if not username:
            continue

        # ── Comprobar si el usuario ya existe ─────────────────────────────
        existing = find_user_by_username(username)

        if existing:
            # Usuario encontrado — intentar auto-login con contraseña cifrada
            creds = dict(existing.get("credentials") or {})
            encrypted_pw = creds.get("garmin_password_encrypted", "")
            password = decrypt_password(encrypted_pw) if encrypted_pw else None

            if password and _verify_password_for_login(password, existing.get("password_hash", "")):
                set_active_user(existing["id"], username)
                console.print(
                    f"[green]✓[/] Usuario encontrado · Accediendo automáticamente como [bold]{username}[/]"
                )
                return username, creds, False, password

            # Sin cifrado o clave cambiada: pedir contraseña manualmente
            if encrypted_pw:
                console.print("[yellow]⚠[/]  Necesitamos verificar tu contraseña (clave de cifrado cambiada).")
            password = Prompt.ask("Password", password=True).strip()
            result = authenticate_app_user(username, password)
            if not result.get("ok"):
                console.print(f"[red]✗[/] {result.get('error', 'Contraseña incorrecta')}")
                continue

            # Establecer usuario activo antes de cualquier escritura en storage
            set_active_user(result["user_id"], username)

            # Actualizar cifrado para el próximo arranque
            creds = result.get("credentials") or {}
            creds["garmin_password_encrypted"] = encrypt_password(password)
            update_user_credentials(creds)
            console.print(f"[green]✓[/] Sesión iniciada como [bold]{username}[/].")
            return username, creds, False, password

        # ── Usuario nuevo: registro ────────────────────────────────────────
        console.print(Panel.fit(
            "[bold yellow]⚠ Importante — contraseña única[/]\n\n"
            "Tu contraseña de [bold]Kairos Coach[/] debe ser la misma que usas\n"
            "en [bold]Garmin Connect[/]. Así podrás acceder sin volver a pedirla.\n\n"
            "Si en el futuro cambias tu contraseña en Garmin Connect,\n"
            "el sistema te pedirá actualizarla aquí también.",
            title="[bold]Registro nuevo usuario[/]",
            border_style="yellow",
        ))

        password = Prompt.ask("Contraseña (la misma que en Garmin Connect)", password=True).strip()
        confirm = Prompt.ask("Repite la contraseña", password=True).strip()
        if confirm != password:
            console.print("[red]✗[/] Las contraseñas no coinciden.")
            continue

        default_email = username if "@" in username else ""
        garmin_email = Prompt.ask("Email de Garmin Connect", default=default_email).strip()

        credentials = {
            "garmin_email": garmin_email,
            "garmin_password_encrypted": encrypt_password(password),
        }
        create = register_app_user(username, password, credentials=credentials)
        if not create.get("ok"):
            console.print(f"[red]✗[/] {create.get('error', 'No se pudo crear el usuario')}")
            continue

        set_active_user(create.get("user_id"), username)
        console.print(f"[green]✓[/] Usuario [bold]{username}[/] creado.")
        return username, credentials, True, password


def _verify_password_for_login(password: str, stored_hash: str) -> bool:
    """Wrapper para verificar password sin exponer _verify_password de storage."""
    from agent.storage import _verify_password
    return _verify_password(password, stored_hash)


def _ensure_garmin_credentials(credentials: dict, app_password: str = "") -> dict:
    """Pone GARMIN_EMAIL y GARMIN_PASSWORD en el entorno para el proceso MCP."""
    credentials = dict(credentials or {})
    garmin_email = (credentials.get("garmin_email") or "").strip()

    if not garmin_email:
        garmin_email = Prompt.ask(
            "Email Garmin Connect",
            default=os.environ.get("GARMIN_EMAIL", ""),
        ).strip()
        credentials["garmin_email"] = garmin_email
        credentials["garmin_password_encrypted"] = encrypt_password(app_password)
        update_user_credentials(credentials)

    garmin_password = app_password or os.environ.get("GARMIN_PASSWORD", "")

    os.environ["GARMIN_EMAIL"] = garmin_email
    os.environ["GARMIN_PASSWORD"] = garmin_password
    return credentials


def _handle_garmin_password_change(username: str) -> str | None:
    """Flujo de recuperación cuando la contraseña de Garmin Connect ha cambiado."""
    console.print(Panel.fit(
        "[bold red]❌ Error de autenticación en Garmin Connect[/]\n\n"
        "Parece que tu contraseña de Garmin Connect ha cambiado.\n"
        "Introduce tu nueva contraseña para actualizar el acceso.\n"
        "[dim](Debe coincidir con tu contraseña actual en Garmin Connect)[/]",
        title="[bold]Contraseña desactualizada[/]",
        border_style="red",
    ))
    for _ in range(3):
        new_password = Prompt.ask(
            "Nueva contraseña de Garmin Connect", password=True
        ).strip()
        if len(new_password) < 6:
            console.print("[red]✗[/] Mínimo 6 caracteres.")
            continue
        result = update_app_user_password(username, new_password)
        if not result.get("ok"):
            console.print(f"[red]✗[/] {result.get('error')}")
            continue
        os.environ["GARMIN_PASSWORD"] = new_password
        console.print("[green]✓[/] Contraseña actualizada. Reconectando con Garmin Connect...")
        return new_password
    return None


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
        f"[bold]Bienvenido, {name}![/] Primera vez configurando Kairos Coach.\n"
        "Necesito conocer tus objetivos y condiciones de salud\n"
        "para personalizar todas las recomendaciones.\n"
        "[dim]Pulsa Enter para omitir cualquier campo.[/]",
        title="[bold green]Kairos Coach — Configuración inicial[/]",
        border_style="green",
    ))

    _ask_goals(profile)
    _ask_health(profile)
    profile["setup_complete"] = True
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


def _parse_plan_command(cmd: str) -> tuple[str | None, str | None]:
    """Parsea comandos /plan y devuelve (acción, argumento opcional)."""
    raw = (cmd or "").strip().lower()
    if not raw.startswith("/plan"):
        return None, None

    parts = raw.split()
    if len(parts) == 1:
        return "help", None

    action = parts[1]
    arg = " ".join(parts[2:]).strip() or None

    if action in {"help", "ayuda", "?"}:
        return "help", None
    if action in {"listar", "list", "ls"}:
        return "list", None
    if action in {"ver", "view", "show"}:
        return "view", arg
    if action in {"activar", "activate"}:
        return "activate", arg
    if action in {"crear", "create", "new"}:
        return "create", None
    return "help", None


def _show_plan_help() -> None:
    console.print(Panel.fit(
        "[bold]Comandos de planificación:[/]\n\n"
        "  [bold cyan]/plan listar[/bold cyan]            Lista planes y marca el activo\n"
        "  [bold cyan]/plan ver <plan_id>[/bold cyan]     Muestra detalle del plan y sus sesiones\n"
        "  [bold cyan]/plan activar <plan_id>[/bold cyan] Activa un plan y desactiva el anterior\n"
        "  [bold cyan]/plan crear[/bold cyan]             Crea un plan base en DB (interactivo)",
        title="[bold blue]Kairos Coach — Gestión de planes[/]",
        border_style="blue",
    ))


def _show_training_plans() -> None:
    try:
        plans = list_training_plans(include_archived=False)
    except Exception as exc:
        console.print(f"[bold red]Error listando planes:[/] {exc}")
        return

    if not plans:
        console.print("[yellow]No hay planes registrados para este usuario.[/]")
        return

    table = Table(title="Planes de entrenamiento")
    table.add_column("Activo", justify="center")
    table.add_column("ID")
    table.add_column("Título")
    table.add_column("Estado")
    table.add_column("Duración")
    table.add_column("Objetivo")

    for plan in plans:
        status = str(plan.get("status") or "").strip().lower()
        is_active = status == "active"
        plan_id = str(plan.get("id") or "")
        title = str(plan.get("title") or "Plan").strip()
        duration = str(plan.get("duration_weeks") or 0)
        objective = str(plan.get("objective") or "—").strip() or "—"
        table.add_row("✓" if is_active else "", plan_id, title, status or "—", f"{duration} sem", objective)

    console.print(table)


def _show_training_plan(plan_id: str) -> None:
    if not plan_id:
        console.print("[yellow]Uso: /plan ver <plan_id>[/]")
        return

    try:
        plan = get_training_plan(plan_id)
    except Exception as exc:
        console.print(f"[bold red]Error cargando plan:[/] {exc}")
        return

    if not plan:
        console.print(f"[yellow]No encontré el plan '{plan_id}'.[/]")
        return

    sessions = []
    try:
        sessions = list_training_plan_sessions(plan_id)
    except Exception:
        sessions = []

    lines = [
        f"[bold]ID:[/] {plan.get('id', '—')}",
        f"[bold]Título:[/] {plan.get('title', '—')}",
        f"[bold]Estado:[/] {plan.get('status', '—')}",
        f"[bold]Objetivo:[/] {plan.get('objective', '—')}",
        f"[bold]Dificultad:[/] {plan.get('difficulty', '—')}",
        f"[bold]Duración:[/] {plan.get('duration_weeks', 0)} semanas",
        f"[bold]Fuente:[/] {plan.get('source', '—')}",
    ]

    if sessions:
        lines.append("")
        lines.append("[bold]Sesiones:[/]")
        for s in sessions[:14]:
            lines.append(
                f"- Semana {s.get('week_index', 1)} · Día {s.get('day_index', 1)} · "
                f"{s.get('session_type', 'session')} · {s.get('duration_min') or 'n/d'} min · "
                f"{s.get('intensity') or 'intensidad n/d'}"
            )
    else:
        lines.append("")
        lines.append("[dim]Sin sesiones registradas.[/]")

    console.print(Panel("\n".join(lines), title="[bold blue]Detalle de plan[/]", border_style="blue"))


def _activate_training_plan_cli(plan_id: str, agent: TrainerAgent) -> None:
    if not plan_id:
        console.print("[yellow]Uso: /plan activar <plan_id>[/]")
        return

    try:
        activated = activate_training_plan(plan_id, change_reason="activated_from_cli")
    except Exception as exc:
        console.print(f"[bold red]Error activando plan:[/] {exc}")
        return

    if not activated:
        console.print(f"[yellow]No se pudo activar el plan '{plan_id}'.[/]")
        return

    # Espejo backward-compatible en perfil local en memoria.
    profile = _load_user_profile()
    profile["training_plan"] = {
        "id": activated.get("id"),
        "title": activated.get("title"),
        "status": "active",
        "active": True,
        "source": activated.get("source") or "db",
    }
    _save_user_profile(profile)
    agent.user_profile = _load_user_profile()

    console.print(f"[green]✓[/] Plan activado: [bold]{activated.get('title', plan_id)}[/]")


def _create_training_plan_cli(agent: TrainerAgent) -> None:
    profile = _load_user_profile()
    goals = (profile or {}).get("goals", {})

    console.print(Panel.fit(
        "Crea una planificación base persistida en BD.\n"
        "Puedes ajustar sesiones y detalles después.",
        title="[bold blue]Kairos Coach — Crear plan[/]",
        border_style="blue",
    ))

    default_race = str(goals.get("target_race") or "")
    default_weeks = "8"

    title = Prompt.ask("Título del plan", default=(f"Plan hacia {default_race}" if default_race else "Plan personalizado")).strip()
    objective = Prompt.ask("Objetivo principal", default=(default_race or "Mejora general")).strip()
    description = Prompt.ask("Descripción breve", default="Plan base generado desde CLI").strip()
    difficulty = Prompt.ask(
        "Dificultad",
        choices=["easy", "moderate", "hard"],
        default="moderate",
        show_choices=True,
    ).strip()

    while True:
        duration_weeks_raw = Prompt.ask("Duración (semanas)", default=default_weeks).strip()
        try:
            duration_weeks = max(0, int(duration_weeks_raw))
            break
        except Exception:
            console.print("[red]✗[/] Introduce un número entero de semanas.")

    today_focus = Prompt.ask("Sesión sugerida para hoy (opcional)", default="").strip()

    plan_data = {
        "target_race": goals.get("target_race"),
        "target_race_date": goals.get("target_race_date"),
        "target_time": goals.get("target_time"),
    }
    if today_focus:
        plan_data["today_focus"] = today_focus

    try:
        created = create_training_plan(
            {
                "title": title,
                "description": description,
                "objective": objective,
                "difficulty": difficulty,
                "duration_weeks": duration_weeks,
                "status": "active",
                "source": "cli_plan_create",
                "plan_data": plan_data,
            },
            sessions=None,
            change_reason="created_from_cli",
        )
    except Exception as exc:
        console.print(f"[bold red]Error creando plan:[/] {exc}")
        return

    # Espejo backward-compatible en perfil local.
    profile["training_plan"] = {
        "id": created.get("id"),
        "title": created.get("title"),
        "status": "active",
        "active": True,
        "today_focus": today_focus,
        "source": created.get("source") or "cli_plan_create",
        "target_race": goals.get("target_race"),
        "target_race_date": goals.get("target_race_date"),
    }
    _save_user_profile(profile)
    agent.user_profile = _load_user_profile()

    console.print(
        f"[green]✓[/] Plan creado y activado: [bold]{created.get('title', 'Plan')}[/] "
        f"([dim]{str(created.get('id') or '')[:8]}...[/])"
    )


def _show_load_trend_cli(agent: "TrainerAgent", cmd: str = "/carga") -> None:
    """Muestra la tabla de tendencia de carga/fatiga (semanal o mensual)."""
    from agent.trainer_agent import _build_load_trend_table

    parts = cmd.strip().lower().split()
    mode = "months" if len(parts) > 1 and parts[1] in {"meses", "mensual", "months", "month"} else "weeks"

    load_metrics = (agent.user_profile or {}).get("load_metrics") or {}
    series = load_metrics.get("series") or []

    if not series:
        console.print(Panel.fit(
            "Aún no hay datos de carga/fatiga calculados.\n\n"
            "Se calculan automáticamente al arrancar la sesión.\n"
            "Reinicia el agente o realiza una consulta de estado para generarlos.",
            title="[bold yellow]Kairos Coach — Carga/Fatiga[/]",
            border_style="yellow",
        ))
        return

    md_table = _build_load_trend_table(series, mode=mode)

    # Parsear la tabla Markdown y renderizarla con Rich Table para mejor visualización
    lines = md_table.splitlines()
    title_line = next((l.lstrip("# ").strip() for l in lines if l.startswith("#")), "Tendencia de carga")
    table_lines = [l for l in lines if l.startswith("|") and "---" not in l]
    legend_lines = [l.strip("_") for l in lines if l.startswith("_")]

    if not table_lines:
        console.print(Markdown(md_table))
        return

    headers = [h.strip() for h in table_lines[0].strip("|").split("|")]
    rich_table = Table(title=title_line, border_style="blue")
    for i, h in enumerate(headers):
        justify = "right" if i > 0 and i < len(headers) - 1 else "left"
        rich_table.add_column(h, justify=justify)

    status_colors = {
        "🟢": "green",
        "🟠": "yellow",
        "🔴": "red",
        "🟡": "yellow",
    }
    for row_line in table_lines[1:]:
        cells = [c.strip() for c in row_line.strip("|").split("|")]
        # Colorear la columna de estado según emoji
        colored = []
        for i, cell in enumerate(cells):
            if i == len(cells) - 1:
                color = next((v for k, v in status_colors.items() if k in cell), None)
                colored.append(f"[{color}]{cell}[/]" if color else cell)
            else:
                colored.append(cell)
        rich_table.add_row(*colored)

    console.print(rich_table)
    if legend_lines:
        console.print(f"[dim]{' · '.join(legend_lines)}[/]")


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
        "  [bold cyan]/plan listar[/bold cyan]             Ver planes de entrenamiento\n"
        "  [bold cyan]/plan ver <id>[/bold cyan]           Ver detalle de un plan\n"
        "  [bold cyan]/plan activar <id>[/bold cyan]       Activar plan por id\n"
        "  [bold cyan]/plan crear[/bold cyan]              Crear y activar plan base\n"
        "  [bold cyan]/carga[/bold cyan]                   Tabla semanal de carga/fatiga (TSS·ATL·CTL·TSB)\n"
        "  [bold cyan]/carga meses[/bold cyan]             Vista mensual de carga/fatiga\n"
        "  [bold cyan]/modelo[/bold cyan]                  Cambiar el proveedor de modelo de IA activo\n"
        "  [bold cyan]/ayuda[/bold cyan]                   Mostrar esta pantalla\n"
        "  [bold cyan]salir[/bold cyan]                    Terminar la sesión\n"
        "\n"
        "[bold]Guía rápida de indicadores Garmin:[/]\n"
        "  [bold]Body Battery[/bold]       90-100 recuperado \u00b7 70-89 bien \u00b7 40-69 moderado \u00b7 <40 descansa\n"
        "  [bold]Training Readiness[/bold] >70 entrena fuerte \u00b7 40-70 suave \u00b7 <40 recuperación activa\n"
        "  [bold]HRV[/bold]               Caída >20% = fatiga, estrés o mal control glucémico (DT1)\n"
        "  [bold]Training Status[/bold]   Productive=en forma \u00b7 Peaking=pico \u00b7 Overreaching=alarma",
        title="[bold blue]Kairos Coach — Ayuda[/]",
        border_style="blue",
    ))


def _format_coach_markdown(response: str) -> str:
    """Normaliza la salida del coach a Markdown legible para terminal/email/Telegram.

    Si el modelo responde en texto plano, envuelve el contenido con un encabezado
    y convierte líneas sueltas en bullets para mejorar la lectura.
    """
    text = (response or "").strip()
    if not text:
        return "## 🧭 Resumen del Coach\n\n_No he podido generar contenido en esta respuesta._"

    markdown_markers = ("## ", "### ", "|", "**", "\n1. ", "- ", "* ")
    if any(marker in text for marker in markdown_markers):
        return text

    lines = [line.strip(" \t-•") for line in text.splitlines() if line.strip()]
    if len(lines) <= 1:
        body = lines[0] if lines else text
        return f"## 🧭 Resumen del Coach\n\n{body}"

    bullet_block = "\n".join(f"- {line}" for line in lines)
    return f"## 🧭 Resumen del Coach\n\n{bullet_block}"


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
    """Verifica que las variables obligatorias del proveedor LLM estén definidas."""
    missing = []
    env_var, label, _ = _PROVIDER_INFO[provider]

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


def _check_garmin_env() -> None:
    """Verifica que haya credenciales Garmin cargadas para el usuario activo."""
    missing = [v for v in ("GARMIN_EMAIL", "GARMIN_PASSWORD") if not os.environ.get(v)]
    if missing:
        for m in missing:
            console.print(f"[bold red]Error:[/] Falta [bold]{m}[/] para conectar con Garmin Connect")
        sys.exit(1)


# Valores placeholder que indican que la clave no está configurada
_PLACEHOLDER_VALUES = {"", "tu_clave_mistral", "tu_clave_gemini", "gsk_...", "AIzaSy_tu_clave_de_gemini", "ghp_..."}


def _select_provider_menu(on_vpn: bool) -> str:
    """Muestra el menú de selección de proveedor LLM."""
    from agent.storage import is_gemini_quota_exhausted, get_gemini_daily_usage

    # Construir lista de proveedores disponibles.
    # Si estamos en VPN, incluir GitHub Models como opción seleccionable
    # para evitar que /modelo termine la app cuando solo existe GITHUB_TOKEN.
    candidates = []
    if on_vpn:
        candidates.append(("vpn", "GITHUB_TOKEN"))
    candidates.extend([
        ("gemini",   "GEMINI_API_KEY"),
        ("mistral",  "MISTRAL_API_KEY"),
        ("groq",     "GROQ_API_KEY"),
        ("cerebras", "CEREBRAS_API_KEY"),
        ("nvidia",   "NVIDIA_API_KEY"),
    ])
    available = []
    for name, env_var in candidates:
        val = os.environ.get(env_var, "")
        if val and val not in _PLACEHOLDER_VALUES:
            available.append((name, env_var))

    if not available:
        console.print(
            "[bold red]Error:[/] No hay ninguna API key configurada en .env\n"
            "Configura al menos una de: [bold]GITHUB_TOKEN[/], [bold]GEMINI_API_KEY[/], "
            "[bold]MISTRAL_API_KEY[/], [bold]GROQ_API_KEY[/]"
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
        title="[bold blue]Kairos Coach — Selección de modelo[/]",
        border_style="blue",
    ))
    choices = [str(i) for i in range(1, len(available) + 1)]
    choice = Prompt.ask("  Tu elección", choices=choices, default="1", case_sensitive=False)
    return available[int(choice) - 1][0]


def _auto_select_provider() -> str:
    """
    Detecta el entorno de red (VPN corporativa con Zscaler vs. acceso libre)
    y permite elegir proveedor LLM desde menú.
    - Con Zscaler → menú incluyendo GitHub Models y proveedores externos configurados
    - Sin Zscaler → menú con proveedores externos configurados
    """
    console.print("[dim]Detectando entorno de red...[/]")
    on_vpn = _detect_zscaler()

    provider = _select_provider_menu(on_vpn=on_vpn)
    _, label, note = _PROVIDER_INFO[provider]
    network_line = "🏢  Red corporativa [dim](Zscaler detectado)[/dim]" if on_vpn else "🌐  Sin VPN corporativa"
    console.print(Panel.fit(
        f"{network_line}\n"
        f"[bold green]Proveedor seleccionado:[/] {label}\n"
        f"[dim]{note}[/dim]",
        title="[bold blue]Kairos Coach — Entorno detectado[/]",
        border_style="blue",
    ))
    return provider


def _ask_tool_mode(provider: str) -> bool:
    """Pregunta al usuario si quiere usar Essential Tools o todas las herramientas (126).

    Con GitHub Models (vpn) se fuerza Essential Tools: los schemas de 126 herramientas
    ya superan el límite de 8k tokens antes de enviar ninguna pregunta.
    """
    if provider == "vpn":
        console.print(Panel.fit(
            "[yellow]GitHub Models[/yellow] tiene un límite de 8 000 tokens por request.\n"
            "Con 126 herramientas los schemas solos superan ese límite, por lo que\n"
            "se usa automáticamente [bold]Essential Tools (subset reducido)[/bold].\n"
            "[dim]Para usar todas las herramientas, sal de la VPN y reinicia (usará Gemini).[/dim]",
            title="[bold blue]Kairos Coach — Herramientas[/]",
            border_style="blue",
        ))
        return True  # essential_only=True

    console.print(Panel.fit(
        "[bold]Selecciona el modo de herramientas:[/]\n\n"
        "  [green]1[/green] · Essential Tools [dim](subset reducido)[/dim]   — más rápido · menor consumo de tokens  [bold]← recomendado[/bold]\n"
        "  [yellow]2[/yellow] · Todas          [dim](126 tools)[/dim]  — acceso completo · más tokens por petición",
        title="[bold blue]Kairos Coach — Herramientas[/]",
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
    """Comprueba conectividad con Supabase y exige DB activa (modo DB-first)."""
    status = check_supabase_connection()

    if not status["configured"]:
        console.print("[bold red]Error:[/] Supabase no configurado. Define SUPABASE_URL y SUPABASE_ANON_KEY en .env")
        sys.exit(1)

    if status["connected"]:
        console.print("[green]✓[/] Supabase conectado — modo DB-first activo")
    else:
        console.print(
            f"[bold red]Error:[/] Supabase configurado pero no accesible\n"
            f"  [dim]Error: {status['error']}[/]"
        )
        sys.exit(1)


async def main() -> None:
    # Fail-fast de infraestructura: evita pedir credenciales si la DB no está lista.
    _check_and_migrate_supabase()

    username, credentials, is_new_user, app_password = _authenticate_or_register_user()
    _ensure_garmin_credentials(credentials, app_password=app_password)
    _check_garmin_env()

    provider = _auto_select_provider()
    _check_env(provider)
    essential_only = _ask_tool_mode(provider)

    _, label, note = _PROVIDER_INFO[provider]
    console.print(Panel.fit(
        f"[bold green]Kairos Coach[/] — Tu entrenador personal con IA\n"
        f"[dim]{label} · {note}[/dim]\n"
        "[dim]Conectando con Garmin Connect...[/]",
        border_style="green",
    ))

    async with garmin_mcp_session(essential_only=essential_only) as session:
        agent = TrainerAgent(mcp_session=session, provider=provider)

        console.print("[dim]Cargando herramientas de Garmin...[/]")
        await agent.initialize()
        console.print(f"[green]✓[/] {len(agent.tools_schema)} herramientas disponibles\n")

        # ── Verificación de acceso a Garmin ───────────────────────────────
        # Detectar si la contraseña ha cambiado en Garmin Connect antes de continuar.
        try:
            from agent.mcp_client import call_tool
            test_raw = await call_tool(session, "get_user_profile", {})
            garmin_auth_failed = (
                test_raw is None
                or (isinstance(test_raw, str) and any(
                    kw in test_raw.lower()
                    for kw in ("401", "unauthorized", "invalid credentials", "login failed",
                               "authentication", "forbidden", "403")
                ))
            )
            if garmin_auth_failed:
                new_pw = _handle_garmin_password_change(username)
                if not new_pw:
                    console.print("[bold red]No se pudo actualizar la contraseña. Saliendo.[/]")
                    return
                app_password = new_pw
        except Exception:
            pass  # No bloquear el arranque si el test de verificación falla

        # Sincronizar datos personales desde Garmin
        profile_changes = await _sync_from_garmin(agent)
        agent.user_profile = _load_user_profile()

        # Si es usuario nuevo o aún no terminó setup, completar onboarding.
        is_first = _is_first_time()
        if is_new_user or is_first:
            _run_first_time_setup()
            agent.user_profile = _load_user_profile()

        # Inicializar/enriquecer base de conocimiento para onboarding de usuario nuevo.
        current_kb = (load_athlete_knowledge() or "").strip()
        if is_new_user:
            enrichment = await agent.build_onboarding_mcp_enrichment()
            seeded = _build_enriched_athlete_knowledge(agent.user_profile, enrichment)
            save_athlete_knowledge(seeded)
            console.print("[green]✓[/] Base de conocimiento inicial enriquecida y guardada.")
            agent.knowledge_chunks.append({"source": "db:athlete_knowledge", "text": seeded[:4000]})
            if "db:athlete_knowledge" not in agent.knowledge_sources:
                agent.knowledge_sources.append("db:athlete_knowledge")
        elif not current_kb:
            enrichment = await agent.build_onboarding_mcp_enrichment()
            seeded = _build_enriched_athlete_knowledge(agent.user_profile, enrichment)
            save_athlete_knowledge(seeded)
            console.print("[green]✓[/] Base de conocimiento creada desde perfil + datos MCP.")
            agent.knowledge_chunks.append({"source": "db:athlete_knowledge", "text": seeded[:4000]})
            if "db:athlete_knowledge" not in agent.knowledge_sources:
                agent.knowledge_sources.append("db:athlete_knowledge")

        active = get_active_user()
        console.print(
            f"[dim]Usuario activo: {active.get('username') or username}"
            f" ({active.get('user_id') or 'sin-id'})[/]"
        )

        # Estado proactivo al arranque (especialmente para usuario existente).
        try:
            proactive_status = await agent.build_startup_status_markdown(profile_changes=profile_changes)
            console.print(Markdown(proactive_status))
        except Exception as status_exc:
            console.print(f"[dim yellow]No se pudo generar el estado proactivo inicial: {status_exc}[/]")

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

        console.print(Rule("[dim]Escribe tu pregunta · [bold]/perfil[/bold] · [bold]/plan listar[/bold] · [bold]/plan crear[/bold] · [bold]/carga[/bold] · [bold]/modelo[/bold] · [bold]salir[/bold][/]"))

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
                    title="[bold blue]Kairos Coach — Cambio de modelo[/]",
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

            if cmd.startswith("/carga"):
                _show_load_trend_cli(agent, cmd)
                continue

            if cmd.startswith("/plan"):
                action, arg = _parse_plan_command(cmd)
                if action == "list":
                    _show_training_plans()
                elif action == "view":
                    _show_training_plan(arg or "")
                elif action == "activate":
                    _activate_training_plan_cli(arg or "", agent)
                elif action == "create":
                    _create_training_plan_cli(agent)
                else:
                    _show_plan_help()
                continue

            if not user_input.strip():
                continue

            try:
                with console.status("[bold green]Kairos Coach está analizando tus datos...[/]"):
                    response = await agent.chat(user_input)
                console.print(f"\n[bold green]Kairos Coach[/]")
                console.print(Markdown(_format_coach_markdown(response)))
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

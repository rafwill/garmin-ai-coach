"""
trainer_agent.py
Agente entrenador personal que combina OpenAI con las herramientas
de Garmin Connect a través del servidor MCP.
"""

import os
import logging
import math
import ssl
import json
import asyncio
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import truststore
from openai import AsyncOpenAI
from mcp import ClientSession

from agent.mcp_client import list_available_tools, call_tool
from agent import storage as _storage

log = logging.getLogger(__name__)


PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


def _load_system_prompt(compact: bool = False) -> str:
    """Carga el system prompt del entrenador desde el archivo Markdown.

    Args:
        compact: Si True, carga la versión compacta (para modelos con limite bajo
                 de tokens, como GitHub Models en red corporativa con Zscaler).
    """
    filename = "system_prompt_compact.md" if compact else "system_prompt.md"
    prompt_file = PROMPTS_DIR / filename
    return prompt_file.read_text(encoding="utf-8")


# ─── Funciones de persistencia ───────────────────────────────────────────────
# Thin wrappers sobre agent.storage para mantener compatibilidad con imports
# existentes en main.py y los tests. Toda la lógica de persistencia
# vive en agent/storage.py.

def _load_user_profile() -> dict:
    """Carga el perfil del usuario (datos personales, objetivos, salud)."""
    return _storage.load_user_profile()


def _save_user_profile(profile: dict) -> None:
    """Guarda el perfil del usuario."""
    _storage.save_user_profile(profile)


def _load_session_context() -> dict:
    """Carga el contexto de sesiones (historial de mensajes y resúmenes)."""
    return _storage.load_session_context()


def _save_history_entry(role: str, content: str) -> None:
    """Añade una entrada al historial de conversación persistente."""
    _storage.save_history_entry(role, content)


def _load_session_summaries() -> list[dict]:
    """Carga los resúmenes de sesiones anteriores."""
    return _storage.load_session_summaries()


def _persist_session_summary(summary: str) -> None:
    """Guarda el resumen de la sesión actual en el contexto persistente."""
    _storage.persist_session_summary(summary)


def get_gemini_daily_usage(api_key: str) -> int:
    """Obtiene los tokens consumidos hoy para una API key específica."""
    return _storage.get_gemini_daily_usage(api_key)


def update_gemini_daily_usage(api_key: str, tokens: int) -> int:
    """Actualiza y devuelve los tokens acumulados hoy para una API key específica."""
    return _storage.update_gemini_daily_usage(api_key, tokens)


def mark_gemini_quota_exhausted(api_key: str) -> None:
    """Marca la API key específica como agotada por cuota para el día de hoy."""
    _storage.mark_gemini_quota_exhausted(api_key)


# Herramientas esenciales para el agente entrenador
# Limitamos el número para no superar los límites de tokens del modelo
# Máximo de caracteres por resultado de herramienta para no exceder el límite de tokens
_MAX_TOOL_RESULT_CHARS = 3000
_KB_CHUNK_SIZE_CHARS = 900
_KB_MAX_CHUNKS = 4
_KB_MAX_CHARS_PER_FILE = 50_000
_KB_DEFAULT_FILES = (
    "memory/athlete_knowledge.md",
    "memory/athlete_knowledge.txt",
    "memory/athlete_knowledge.json",
)

# Campos de los objetos Garmin que NO deben llegar al LLM:
# - Timestamps de inicio (prStartTimeGMT, startTimeLocal, etc.) → contienen la
#   HORA DEL DÍA en que empezó la actividad, NO la duración. El LLM los confunde
#   con el tiempo de carrera (ej. "17:48:52" es "las 17h48" no "17 horas").
# - IDs internos y metadatos sin valor analítico.
_GARMIN_STRIP_FIELDS = {
    # Timestamps (hora de inicio, NO duración)
    "prStartTimeGMT", "prStartTimeLocal",
    "startTimeGMT", "startTimeLocal", "startTimeUTC",
    "beginTimestamp", "calendarDate",
    # IDs y referencias internas (NO incluir activityId: el LLM lo necesita para llamar get_activity)
    "id", "userProfileId", "ownerId", "deviceId",
    "garminGUID", "uuid", "userId",
    # Metadatos de presentación sin valor para el análisis
    "displayName", "locationName", "countryCode", "timeZoneId",
}

_WRITE_TOOL_PREFIXES = (
    "create_",
    "update_",
    "delete_",
    "set_",
    "schedule_",
    "unschedule_",
    "upload_",
    "add_",
)


def _is_mcp_read_only_enabled() -> bool:
    """Lee la política de solo lectura para tools MCP (por defecto activada)."""
    raw = str(os.environ.get("MCP_READ_ONLY", "true")).strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _is_write_mcp_tool(tool_name: str) -> bool:
    """Detecta tools MCP de escritura para bloquearlas en modo read-only."""
    name = str(tool_name or "").strip().lower()
    if not name:
        return False
    if name == "request_reload":
        return False
    return name.startswith(_WRITE_TOOL_PREFIXES)


def _build_mcp_read_only_block_message(tool_name: str) -> str:
    """Mensaje estándar cuando se bloquea una tool de escritura."""
    return json.dumps(
        {
            "error": "mcp_read_only_mode",
            "tool": tool_name,
            "message": (
                "Esta sesión está en modo solo consulta: se bloquean herramientas de escritura "
                "(create/update/delete/schedule/upload/add/set)."
            ),
        },
        ensure_ascii=False,
    )


def _strip_garmin_object(obj):
    """Prueba y poda un objeto Garmin de forma recursiva para conservar métricas anidadas
    importantes (como VO2Max, zonas de FC o cargas de entrenamiento) mientras elimina metadatos redundantes.
    """
    if isinstance(obj, list):
        # Limitar longitud de arrays anidados
        return [_strip_garmin_object(item) for item in obj[:4]]
    
    if isinstance(obj, dict):
        cleaned = {}
        # Simplificación de diccionarios pequeños de tipo de actividad/deporte
        if "typeKey" in obj and len(obj) < 10:
            return obj["typeKey"]
            
        for k, v in obj.items():
            if k in _GARMIN_STRIP_FIELDS:
                continue
            if "image" in k.lower() or "url" in k.lower():
                continue
            if k in {"userRoles", "privacy", "userPro", "hasVideo", "favorite", "atpActivity", "parent", "purposeful"}:
                continue
            
            cleaned_v = _strip_garmin_object(v)
            if cleaned_v is not None and cleaned_v != {} and cleaned_v != []:
                if k == "activityType" and isinstance(cleaned_v, dict) and "typeKey" in cleaned_v:
                    cleaned[k] = cleaned_v["typeKey"]
                elif k == "eventType" and isinstance(cleaned_v, dict) and "typeKey" in cleaned_v:
                    cleaned[k] = cleaned_v["typeKey"]
                else:
                    cleaned[k] = cleaned_v
        return cleaned
    
    return obj


def _seconds_to_hhmmss(seconds: float) -> str:
    """Convierte segundos a HH:MM:SS o MM:SS según la duración."""
    total = int(round(seconds))
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _is_cycling_activity(act_type) -> bool:
    """True para cualquier variante de ciclismo (mountain bike, carretera, indoor, virtual, etc.)."""
    if isinstance(act_type, dict):
        act_type = str(act_type.get("typeKey") or act_type.get("typeName") or "")
    t = str(act_type or "").lower()
    return any(kw in t for kw in ("cycling", "biking", "bike", "virtual_ride", "bmx", "cicl"))


# Versión de la fórmula TSS. Incrementar cuando cambie _estimate_session_tss
# para forzar recálculo automático de la serie histórica en el próximo arranque.
_TSS_FORMULA_VERSION = 3  # v3: enrichment de actividades recientes con get_activity (trainingStressScore)


# Metadatos de récords personales de Garmin (mapeado de typeId a categoría y formato)
_PR_METADATA = {
    1: {"tipo": "1K", "unidad": "tiempo"},
    2: {"tipo": "1 Milla", "unidad": "tiempo"},
    3: {"tipo": "5K", "unidad": "tiempo"},
    4: {"tipo": "10K", "unidad": "tiempo"},
    5: {"tipo": "Medio Maratón", "unidad": "tiempo"},
    6: {"tipo": "Maratón", "unidad": "tiempo"},
    7: {"tipo": "Carrera más larga", "unidad": "distancia_km"},
    8: {"tipo": "Ciclismo más largo", "unidad": "distancia_km"},
    9: {"tipo": "Ascenso máximo de ciclismo", "unidad": "elevacion_m"},
    11: {"tipo": "40K ciclismo", "unidad": "tiempo"},
    12: {"tipo": "Pasos máximos en un día", "unidad": "pasos"},
    13: {"tipo": "Pasos máximos en una semana", "unidad": "pasos"},
    14: {"tipo": "Pasos máximos en un mes", "unidad": "pasos"},
    15: {"tipo": "Racha récord de objetivo de pasos", "unidad": "dias"}, # Usamos ASCII/UTF-8 completo
    16: {"tipo": "Racha actual de objetivo de pasos", "unidad": "dias"},
    17: {"tipo": "Natación más larga", "unidad": "distancia_m_y_km"},
    18: {"tipo": "100m natación", "unidad": "tiempo"},
    20: {"tipo": "400m natación", "unidad": "tiempo"},
    22: {"tipo": "1000m natación", "unidad": "tiempo"},
    23: {"tipo": "1500m natación", "unidad": "tiempo"},
}

_PR_CATEGORY_TRANSLATIONS = {
    "fastest 1k": "1K más rápido",
    "fastest mile": "Milla más rápida",
    "fastest 5k": "5K más rápido",
    "fastest 10k": "10K más rápido",
    "fastest half marathon": "Media maratón más rápida",
    "fastest marathon": "Maratón más rápida",
    "longest run": "Carrera más larga",
    "longest ride": "Ciclismo más largo",
    "most elevation gain cycling": "Ascenso máximo en ciclismo",
    "fastest 40k cycling": "40K ciclismo más rápido",
    "most steps day": "Máximos pasos en un día",
    "most steps week": "Máximos pasos en una semana",
    "most steps month": "Máximos pasos en un mes",
    "longest daily goal streak": "Racha más larga de objetivo diario",
    "longest weekly goal streak": "Racha más larga de objetivo semanal",
    "longest pool swim": "Natación más larga en piscina",
    "fastest 100m pool swim": "100m piscina más rápido",
    "fastest 500m pool swim": "500m piscina más rápido",
    "fastest 1500m pool swim": "1500m piscina más rápido",
    "fastest 1 mile pool swim": "1 milla piscina más rápida",
}


def _translate_pr_category_es(category: str) -> str:
    """Traduce categorías comunes de PR de Garmin al español para mostrar al usuario."""
    text = (category or "").strip()
    if not text:
        return "Registro"
    lowered = text.lower()
    if lowered in _PR_CATEGORY_TRANSLATIONS:
        return _PR_CATEGORY_TRANSLATIONS[lowered]
    return text


def _compact_personal_records(data: list) -> str:
    """Convierte los récords personales de Garmin a un formato compacto y legible.
    Transforma el campo `value` (unidades raw de Garmin: segundos para tiempos, metros para distancias/alturas, pasos, días)
    al formato adecuado directamente en Python, facilitando la interpretación por el LLM.
    """
    results = []
    for record in data:
        if not isinstance(record, dict):
            continue
        type_id = record.get("typeId") if record.get("typeId") is not None else record.get("type_id")
        value = record.get("value")
        raw_value = record.get("raw_value") if record.get("raw_value") is not None else value
        record_type = record.get("record_type")
        meta = _PR_METADATA.get(type_id)
        
        if meta:
            tipo_name = meta["tipo"]
            unidad = meta["unidad"]
        else:
            tipo_name = _translate_pr_category_es(record_type) if record_type else f"typeId={type_id}"
            unidad = "valor"
            
        entry: dict = {
            "actividad": record.get("activityName") or record.get("activity_name") or "",
            "tipo": tipo_name,
            "deporte": record.get("activityType") or record.get("activity_type") or "",
            "categoria": tipo_name if meta else (_translate_pr_category_es(record_type) if record_type else tipo_name),
            "type_id": type_id,
            "fecha": record.get("date") or "",
        }
        
        if value is not None:
            try:
                if isinstance(value, str) and value.strip() and (":" in value or any(ch.isalpha() for ch in value)):
                    pretty = value.strip()
                    if unidad == "tiempo":
                        entry["tiempo"] = pretty
                    elif unidad in {"distancia_km", "distancia_m_y_km"}:
                        entry["distancia"] = pretty
                    elif unidad == "elevacion_m":
                        entry["elevacion"] = pretty
                    elif unidad == "pasos":
                        entry["pasos"] = pretty
                    elif unidad == "dias":
                        entry["racha"] = pretty
                    entry["valor"] = pretty
                    results.append(entry)
                    continue

                v_float = float(raw_value)
                if unidad == "tiempo":
                    entry["tiempo"] = _seconds_to_hhmmss(v_float)
                    entry["valor"] = entry["tiempo"]
                elif unidad == "distancia_km":
                    entry["distancia"] = f"{v_float / 1000:.2f} km"
                    entry["valor"] = entry["distancia"]
                elif unidad == "distancia_m_y_km":
                    if v_float >= 1000:
                        entry["distancia"] = f"{v_float / 1000:.2f} km"
                    else:
                        entry["distancia"] = f"{v_float:.0f} m"
                    entry["valor"] = entry["distancia"]
                elif unidad == "elevacion_m":
                    entry["elevacion"] = f"{v_float:.1f} m"
                    entry["valor"] = entry["elevacion"]
                elif unidad == "pasos":
                    entry["pasos"] = f"{int(round(v_float)):,}"
                    entry["valor"] = entry["pasos"]
                elif unidad == "dias":
                    entry["racha"] = f"{int(round(v_float))} días"
                    entry["valor"] = entry["racha"]
                else:
                    entry["valor"] = value
            except (ValueError, TypeError):
                entry["valor"] = value
                
        results.append(entry)
    return json.dumps(results, ensure_ascii=False, separators=(",", ":"))


def _compact_tool_result(raw: str | None, tool_name: str = "") -> str:
    """
    Compacta el resultado de una herramienta para que quepa en el contexto.
    - get_personal_record(s): conversión específica de segundos a HH:MM:SS.
    - Arrays JSON: conserva hasta 8 elementos y elimina campos metadata.
    - Strings demasiado largos: trunca a _MAX_TOOL_RESULT_CHARS.
    """
    if not raw:
        return "(sin datos)"
    try:
        data = json.loads(raw)
        # Procesado específico para récords personales
        if tool_name in {"get_personal_records", "get_personal_record"} and isinstance(data, list):
            return _compact_personal_records(data)
        # Añadir campos normalizados útiles para análisis de actividades
        if tool_name == "get_activity" and isinstance(data, dict):
            # Duración (segundos -> HH:MM:SS)
            duration = data.get("duration") or data.get("movingDuration") or data.get("duration_seconds")
            distance = data.get("distance") or data.get("distance_meters")
            avg_hr   = data.get("avgHr") or data.get("avg_hr_bpm") or data.get("averageHR")
            max_hr   = data.get("maxHr") or data.get("max_hr_bpm") or data.get("maxHR")
            try:
                if duration is not None:
                    dur_s = float(duration)
                    data["duration_hhmmss"] = _seconds_to_hhmmss(dur_s)
                    data["duration_hours"]  = round(dur_s / 3600, 2)
            except (ValueError, TypeError):
                dur_s = None
            try:
                if distance is not None:
                    dist_km = float(distance) / 1000
                    data["distance_km"] = round(dist_km, 2)
                    if dur_s and dist_km > 0:
                        act_type_raw = data.get("activityType") or data.get("type") or ""
                        if _is_cycling_activity(act_type_raw):
                            speed_kmh = dist_km / (dur_s / 3600)
                            data["velocidad_media_kmh"] = round(speed_kmh, 1)
                            # Convertir velocidad máxima si viene en m/s
                            max_spd = data.get("maxSpeed") or data.get("max_speed_ms")
                            if max_spd is not None:
                                try:
                                    ms = float(max_spd)
                                    # Garmin devuelve maxSpeed en m/s
                                    data["velocidad_maxima_kmh"] = round(ms * 3.6, 1)
                                except (ValueError, TypeError):
                                    pass
                        else:
                            pace_s_per_km = dur_s / dist_km
                            pace_min = int(pace_s_per_km // 60)
                            pace_sec = int(pace_s_per_km % 60)
                            data["ritmo_medio_min_km"] = f"{pace_min}:{pace_sec:02d} min/km"
                # Eliminar campos de velocidad en m/s (confusos/irrelevantes):
                # running usa ritmo_medio_min_km, ciclismo usa velocidad_media_kmh.
                # Cubrimos todos los alias posibles de garmin-mcp (camelCase y snake_case).
                for _spd_k in (
                    "avgSpeed", "averageSpeed", "maxSpeed", "minSpeed",
                    "avg_speed", "average_speed", "max_speed", "min_speed",
                    "avg_speed_ms", "max_speed_ms",
                    "enhancedAvgSpeed", "enhancedMaxSpeed",
                    "enhanced_avg_speed", "enhanced_max_speed",
                    "movingSpeed", "moving_speed",
                    "speed",
                ):
                    data.pop(_spd_k, None)
                # Potencia: para actividades de carrera (sin potenciómetro físico tipo Stryd)
                # la potencia es una estimación interna de Garmin — se etiqueta como tal.
                # Para ciclismo la potencia proviene de un potenciómetro real → se deja sin etiquetar.
                if not _is_cycling_activity(act_type_raw):
                    for _pow_src, _pow_dst in (
                        ("avgPower",   "potencia_media_estimada_w"),
                        ("maxPower",   "potencia_maxima_estimada_w"),
                        ("avg_power",  "potencia_media_estimada_w"),
                        ("max_power",  "potencia_maxima_estimada_w"),
                    ):
                        _pv = data.pop(_pow_src, None)
                        if _pv is not None and _pow_dst not in data:
                            try:
                                data[_pow_dst] = round(float(_pv), 1)
                            except (ValueError, TypeError):
                                pass
            except (ValueError, TypeError):
                pass
            # Zonas de FC: prioridad 1 = datos reales del dispositivo; prioridad 2 = estimación gaussiana
            try:
                # Intento 1: datos reales (heartRateZones incluido en get_activity)
                _raw_hr_zones_str = json.dumps(
                    data.get("heartRateZones")
                    or data.get("hr_zones")
                    or data.get("hrZones")
                    or data.get("timeInHeartRateZones")
                    or data.get("heartRateTimeInZones")
                ) if any(data.get(k) for k in ("heartRateZones","hr_zones","hrZones",
                         "timeInHeartRateZones","heartRateTimeInZones")) else None
                _zones_parsed_compact = _parse_hr_zones_list(_raw_hr_zones_str) if _raw_hr_zones_str else None
                if _zones_parsed_compact:
                    _total_z_secs = sum(float(z.get("secsInZone") or 0) for z in _zones_parsed_compact)
                    if _total_z_secs > 0:
                        zonas_reales = {}
                        for z in sorted(_zones_parsed_compact, key=lambda x: int(x.get("zoneNumber") or 0)):
                            _zn  = int(z.get("zoneNumber") or 0)
                            _zs  = float(z.get("secsInZone") or 0)
                            _pct = round(_zs / _total_z_secs * 100, 1)
                            _lo  = z.get("minHeartRateIn") or "?"
                            _hi  = z.get("maxHeartRateIn") or "?"
                            _mins = round(_zs / 60, 0)
                            zonas_reales[f"Z{_zn}_{_lo}-{_hi}bpm"] = f"{_pct}% (~{int(_mins)} min)"
                        data["zonas_fc_reales"] = zonas_reales
                        data["nota_zonas"] = "Zonas reales desde el dispositivo Garmin (configuración del usuario)."
                elif avg_hr and max_hr:
                    # Intento 2: estimación gaussiana como fallback
                    fcmax = float(max_hr)
                    fcmed = float(avg_hr)
                    z_bounds = [
                        ("Z1_recuperacion", 0,    0.60),
                        ("Z2_base_aerobica", 0.60, 0.70),
                        ("Z3_umbral_aerobico", 0.70, 0.80),
                        ("Z4_umbral_anaerobico", 0.80, 0.90),
                        ("Z5_vo2max", 0.90, 1.10),
                    ]
                    sigma = 0.10 * fcmax
                    def normal_cdf(x, mu, s):
                        return 0.5 * (1 + math.erf((x - mu) / (s * math.sqrt(2))))
                    zone_pct = {}
                    total = 0.0
                    for name, lo_pct, hi_pct in z_bounds:
                        lo_bpm = lo_pct * fcmax
                        hi_bpm = hi_pct * fcmax
                        p = normal_cdf(hi_bpm, fcmed, sigma) - normal_cdf(lo_bpm, fcmed, sigma)
                        zone_pct[name] = round(max(p, 0) * 100, 1)
                        total += zone_pct[name]
                    if total > 0:
                        zone_pct = {k: round(v / total * 100, 1) for k, v in zone_pct.items()}
                    if dur_s:
                        for name, pct in zone_pct.items():
                            mins = round(dur_s * pct / 100 / 60, 0)
                            zone_pct[name] = f"{pct}% (~{int(mins)} min)"
                    data["zonas_fc_estimadas"] = zone_pct
                    data["nota_zonas"] = (
                        f"ESTIMACIÓN gaussiana (FC_media={int(fcmed)}bpm, FC_max={int(fcmax)}bpm). "
                        "Puede diferir de las zonas reales configuradas en Garmin."
                    )
            except Exception:
                pass
            # Hidratacion estimada
            try:
                if dur_s:
                    dur_h = dur_s / 3600
                    hydration_low  = round(dur_h * 0.5, 1)
                    hydration_high = round(dur_h * 0.8, 1)
                    data["hidratacion_estimada_litros"] = f"{hydration_low}-{hydration_high}L (base; +25% si temp >25C)"
            except Exception:
                pass
        if isinstance(data, list):
            data = data[:8]  # máximo 8 elementos de arrays
            data = [
                _strip_garmin_object(item) if isinstance(item, dict) else item
                for item in data
            ]
        elif isinstance(data, dict):
            data = _strip_garmin_object(data)
        compact = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        if len(compact) > _MAX_TOOL_RESULT_CHARS:
            compact = compact[:_MAX_TOOL_RESULT_CHARS] + "...(truncado)"
        return compact
    except (json.JSONDecodeError, TypeError):
        if len(raw) > _MAX_TOOL_RESULT_CHARS:
            return raw[:_MAX_TOOL_RESULT_CHARS] + "...(truncado)"
        return raw



def _build_tools_schema(tools: list[dict]) -> list[dict]:
    """Convierte las herramientas MCP al formato de function calling de OpenAI/GitHub Models."""
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
    ]


def _resolve_kb_paths(env_value: str | None, project_root: Path | None = None) -> list[Path]:
    """Resuelve los archivos de base de conocimiento del atleta a rutas absolutas.

    Si ATHLETE_KB_PATHS no está definido, usa una lista de rutas por defecto
    dentro del proyecto.
    """
    root = project_root or (Path(__file__).parent.parent)
    raw_paths = [p.strip() for p in (env_value or "").split(",") if p.strip()]
    if not raw_paths:
        raw_paths = list(_KB_DEFAULT_FILES)

    resolved: list[Path] = []
    seen: set[str] = set()
    for raw in raw_paths:
        p = Path(raw)
        if not p.is_absolute():
            p = root / p
        p = p.resolve()
        key = str(p).lower()
        if key in seen:
            continue
        seen.add(key)
        resolved.append(p)
    return resolved


def _json_to_kb_text(data: Any, prefix: str = "") -> str:
    """Aplana JSON a texto legible para recuperación semántica ligera."""
    lines: list[str] = []

    if isinstance(data, dict):
        for k, v in data.items():
            next_prefix = f"{prefix}.{k}" if prefix else str(k)
            lines.append(_json_to_kb_text(v, next_prefix))
        return "\n".join(line for line in lines if line)

    if isinstance(data, list):
        for idx, item in enumerate(data):
            next_prefix = f"{prefix}[{idx}]" if prefix else f"[{idx}]"
            lines.append(_json_to_kb_text(item, next_prefix))
        return "\n".join(line for line in lines if line)

    value = "" if data is None else str(data).strip()
    if not value:
        return ""
    return f"{prefix}: {value}" if prefix else value


def _load_athlete_knowledge_chunks(
    env_value: str | None = None,
    project_root: Path | None = None,
    chunk_size: int = _KB_CHUNK_SIZE_CHARS,
) -> tuple[list[dict[str, str]], list[str]]:
    """Carga archivos de conocimiento del atleta y devuelve chunks + fuentes.

    Formatos soportados: .md, .txt, .json.
    """
    chunks: list[dict[str, str]] = []
    sources: list[str] = []
    for path in _resolve_kb_paths(env_value, project_root):
        if not path.exists() or not path.is_file():
            continue

        suffix = path.suffix.lower()
        if suffix not in {".md", ".txt", ".json"}:
            continue

        try:
            raw = path.read_text(encoding="utf-8")
        except Exception:
            continue
        if not raw.strip():
            continue

        text = raw
        if suffix == ".json":
            try:
                parsed = json.loads(raw)
                text = _json_to_kb_text(parsed)
            except Exception:
                text = raw

        text = text.strip()[:_KB_MAX_CHARS_PER_FILE]
        if not text:
            continue

        # Preferimos cortar por párrafos para que los fragmentos sean más útiles.
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        if not paragraphs:
            paragraphs = [text]

        current = ""
        for paragraph in paragraphs:
            candidate = f"{current}\n\n{paragraph}".strip() if current else paragraph
            if len(candidate) <= chunk_size:
                current = candidate
                continue

            if current:
                chunks.append({"source": path.name, "text": current})
                current = ""

            # Si un párrafo es demasiado largo, lo partimos por ventanas fijas.
            start = 0
            while start < len(paragraph):
                piece = paragraph[start:start + chunk_size].strip()
                if piece:
                    chunks.append({"source": path.name, "text": piece})
                start += chunk_size

        if current:
            chunks.append({"source": path.name, "text": current})

        sources.append(path.name)

    return chunks, sorted(set(sources))


def _tokenize_for_kb(text: str) -> list[str]:
    """Tokenizador simple para retrieval léxico robusto en español/inglés."""
    return re.findall(r"[a-zA-Z0-9áéíóúñüÁÉÍÓÚÑÜ]{3,}", (text or "").lower())


def _retrieve_athlete_knowledge(
    query: str,
    chunks: list[dict[str, str]],
    top_k: int = _KB_MAX_CHUNKS,
) -> list[dict[str, str]]:
    """Recupera los fragmentos más relevantes de la base del atleta."""
    if not chunks:
        return []

    query_tokens = set(_tokenize_for_kb(query))
    if not query_tokens:
        return chunks[: min(top_k, len(chunks))]

    scored: list[tuple[int, int, dict[str, str]]] = []
    for idx, chunk in enumerate(chunks):
        text = chunk.get("text", "")
        text_tokens = set(_tokenize_for_kb(text))
        overlap = len(query_tokens & text_tokens)
        if overlap <= 0:
            continue
        # Ranking estable: más solape, y en empate mantener orden de carga.
        scored.append((overlap, -idx, chunk))

    if not scored:
        return chunks[: min(top_k, len(chunks))]

    scored.sort(reverse=True)
    return [chunk for _, _, chunk in scored[:top_k]]


def _build_athlete_knowledge_context(query: str, chunks: list[dict[str, str]]) -> str:
    """Construye el bloque de contexto RAG a inyectar en mensajes."""
    selected = _retrieve_athlete_knowledge(query, chunks)
    if not selected:
        return ""

    lines = [
        "## Base de Conocimiento del atleta (RAG)",
        "Combina estos fragmentos con el Perfil del usuario y los datos reales de Garmin.",
    ]
    for item in selected:
        source = item.get("source", "kb")
        text = item.get("text", "").strip()
        if not text:
            continue
        trimmed = text[:900]
        ellipsis = "…" if len(text) > 900 else ""
        lines.append(f"- Fuente: {source}\\n{trimmed}{ellipsis}")
    return "\n".join(lines)


def _try_parse_json(raw: str | None) -> Any:
    """Parsea JSON de forma tolerante; devuelve None si no aplica."""
    if not raw or not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text.startswith("{") and not text.startswith("["):
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def _extract_activities_list(payload: Any) -> list[dict]:
    """Extrae una lista de actividades desde distintas formas de respuesta."""
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        candidates = payload.get("activities")
        if isinstance(candidates, list):
            return [x for x in candidates if isinstance(x, dict)]
    return []


def _is_activity_in_last_48h(activity: dict, now: datetime | None = None) -> bool:
    """Comprueba si una actividad cae en la ventana de últimas 48h."""
    now_dt = now or datetime.now()
    start_local = activity.get("startTimeLocal") or activity.get("startTimeGMT") or ""
    if not isinstance(start_local, str) or "T" not in start_local:
        return False

    date_part = start_local.split("T", 1)[0]
    try:
        act_date = datetime.fromisoformat(date_part)
    except ValueError:
        return False

    return (now_dt - act_date) <= timedelta(hours=48)


def _pick_day_payload(payload: Any, target_date: str) -> dict | None:
    """Intenta extraer el bloque de datos del día objetivo desde payloads heterogéneos."""
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, list):
        # Priorizar coincidencia por fecha cuando exista.
        for item in payload:
            if not isinstance(item, dict):
                continue
            day = str(item.get("date") or item.get("calendarDate") or "")
            if day == target_date:
                return item
        for item in payload:
            if isinstance(item, dict):
                return item
    return None


def _format_body_battery_day(payload: Any, target_date: str) -> str:
    """Formatea Body Battery con datos reales del día si están disponibles."""
    day = _pick_day_payload(payload, target_date)
    if not day:
        return "sin datos"

    level = (
        day.get("body_battery_level")
        or day.get("bodyBatteryLevel")
        or day.get("bodyBatteryMostRecentValue")
        or day.get("current")
    )
    highest = day.get("highestBodyBattery") or day.get("highest") or day.get("body_battery_highest")
    lowest = day.get("lowestBodyBattery") or day.get("lowest") or day.get("body_battery_lowest")
    charged = day.get("charged") or day.get("body_battery_charged")
    drained = day.get("drained") or day.get("body_battery_drained")

    parts: list[str] = []
    if level is not None:
        parts.append(f"nivel {int(level)}")
    if highest is not None and lowest is not None:
        parts.append(f"max {int(highest)}/min {int(lowest)}")
    if charged is not None and drained is not None:
        parts.append(f"+{int(charged)}/-{int(drained)}")

    if parts:
        return " · ".join(parts)
    return "datos disponibles"


def _format_hrv_day(payload: Any, target_date: str) -> str:
    """Formatea HRV con métricas relevantes (ms) del día."""
    day = _pick_day_payload(payload, target_date)
    if not day:
        return "sin datos"

    avg = (
        day.get("last_night_avg_hrv_ms")
        or day.get("lastNightAvg")
        or day.get("avgOvernightHrv")
        or day.get("avgHrv")
    )
    weekly = day.get("weekly_avg_hrv_ms") or day.get("weeklyAvg")
    status = day.get("status")

    parts: list[str] = []
    if avg is not None:
        parts.append(f"{float(avg):.1f} ms")
    if weekly is not None:
        parts.append(f"7d {float(weekly):.1f} ms")
    if status:
        parts.append(str(status))

    if parts:
        return " · ".join(parts)
    return "datos disponibles"


def _format_sleep_day(payload: Any, target_date: str) -> str:
    """Formatea sueño con horas y puntuación cuando exista."""
    day = _pick_day_payload(payload, target_date)
    if not day:
        return "sin datos"

    sleep_hours = day.get("sleep_hours")
    sleep_seconds = day.get("sleep_seconds") or day.get("sleepTimeSeconds")
    score = day.get("sleep_score") or day.get("sleepScore")

    if sleep_hours is None and sleep_seconds is not None:
        try:
            sleep_hours = round(float(sleep_seconds) / 3600, 2)
        except (TypeError, ValueError):
            sleep_hours = None

    parts: list[str] = []
    if sleep_hours is not None:
        parts.append(f"{float(sleep_hours):.2f} h")
    if score is not None:
        parts.append(f"score {int(score)}")

    if parts:
        return " · ".join(parts)
    return "datos disponibles"


def _to_iso_date(value: Any) -> str | None:
    """Normaliza una fecha heterogénea a ISO (YYYY-MM-DD)."""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None

    if "T" in text:
        text = text.split("T", 1)[0]

    try:
        return date.fromisoformat(text).isoformat()
    except Exception:
        return None


def _extract_training_load_points(payload: Any) -> list[dict]:
    """Extrae puntos diarios de carga desde payloads de tendencia heterogéneos."""
    points: list[dict] = []

    def _walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                _walk(item)
            return

        if not isinstance(node, dict):
            return

        d_iso = _to_iso_date(
            node.get("date")
            or node.get("calendarDate")
            or node.get("day")
            or node.get("start_date")
        )
        load_value = (
            node.get("trainingLoad")
            or node.get("training_load")
            or node.get("load")
            or node.get("loadValue")
            or node.get("dailyLoad")
            or node.get("loadScore")
        )

        if d_iso and load_value is not None:
            try:
                load_float = max(0.0, float(load_value))
                points.append({"date": d_iso, "tss": load_float})
            except Exception:
                pass

        for value in node.values():
            if isinstance(value, (list, dict)):
                _walk(value)

    _walk(payload)
    return points


def _estimate_session_tss(activity: dict, ftp: float | None = None) -> tuple[float, str]:
    """Estima TSS de una sesión. Devuelve (valor, etiqueta) donde:
      etiqueta = "TSS"   si la fuente es potenciómetro o dato nativo de Garmin.
      etiqueta = "hrTSS" si la fuente es estimación por FC o genérico.

    Prioridades:
    1.  trainingStressScore / Training Load de Garmin  (más preciso)
    1.5 Potencia (NP o avgPower) + FTP  — solo ciclismo
    2.  FC media  — Ciclismo: IF=%HRR (Coggan)  /  Running: IF=0.40+HRR×0.65
    3.  Training Effect aeróbico
    4.  IF genérico por deporte
    """
    if not isinstance(activity, dict):
        return 0.0, "hrTSS"

    # Detectar ciclismo para aplicar la fórmula IF correcta
    _act_type = activity.get("type") or activity.get("activityType") or ""
    _cycling = _is_cycling_activity(_act_type)

    # ── Prioridad 1: Training Load de Garmin ─────────────────────────────────
    # Nota: se omiten valores <= 0 para permitir el fallback por FC cuando Garmin
    # no calculó el training load (activityTrainingLoad=0 significa "sin dato").
    for key in (
        "trainingStressScore",    # TSS nativo (potencia / cycling)
        "trainingLoad",
        "training_load",
        "activityTrainingLoad",   # Training Load interno de Garmin (TE)
        "loadValue",
    ):
        raw_load = activity.get(key)
        if raw_load is None:
            continue
        try:
            val = float(raw_load)
        except Exception:
            continue
        if val <= 0:
            continue   # 0 = Garmin no lo calculó → caer al fallback de FC
        return max(0.0, min(val, 500.0)), "TSS"

    # ── Duración — necesaria para todas las estimaciones restantes ────────────
    duration_seconds = (
        activity.get("duration_seconds")      # garmin-mcp (snake_case)
        or activity.get("duration")
        or activity.get("durationInSeconds")
        or activity.get("elapsedDuration")
        or activity.get("movingDuration")
        or activity.get("moving_duration_seconds")  # garmin-mcp fallback
        or 0
    )
    try:
        hours = max(0.0, float(duration_seconds) / 3600.0)
    except Exception:
        hours = 0.0
    if hours <= 0:
        return 0.0, "hrTSS"

    if_value: float | None = None

    # ── Prioridad 1.5: potencia + FTP (solo ciclismo, más preciso que FC) ─────────
    # Usa Normalized Power si está disponible (mejor que avg para TSS),
    # o Average Power como alternativa. Si no hay FTP conocido, pasa al fallback FC.
    if _cycling and hours > 0:
        power_raw = (
            activity.get("normalizedPower")
            or activity.get("normalized_power_watts")
            or activity.get("avgPower")
            or activity.get("avg_power_watts")
            or activity.get("averagePower")
            or activity.get("average_power_watts")
        )
        if power_raw is not None:
            try:
                power_w = float(power_raw)
                if power_w > 0 and ftp and ftp > 0:
                    if_pow = power_w / ftp
                    tss_pow = hours * (if_pow ** 2) * 100.0
                    return max(0.0, min(tss_pow, 500.0)), "TSS"
            except (ValueError, TypeError):
                pass
    # (sin potencia o sin FTP → continuar hacia estimación por FC)

    # ── Prioridad 2: estimación por FC media (método Karvonen %HRR) ──────────
    # IF ≈ 0.40 + %HRR × 0.65  →  Z1(~45%HRR)≈0.69 · Z2(~65%)≈0.82 · Z4(~85%)≈0.95
    avg_hr_raw = (
        activity.get("averageHR")
        or activity.get("avgHr")
        or activity.get("avg_hr_bpm")
        or activity.get("averageHeartRate")
    )
    max_hr_raw = (
        activity.get("maxHR")
        or activity.get("maxHr")
        or activity.get("max_hr_bpm")
        or activity.get("maxHeartRate")
    )
    if avg_hr_raw is not None:
        try:
            avg_hr  = float(avg_hr_raw)
            hr_rest = 50.0                                        # RHR típico de atleta
            hr_max  = float(max_hr_raw) if max_hr_raw else 185.0 # máx sesión o estimado
            hr_max  = max(hr_max, avg_hr + 5.0)                  # máx > promedio siempre
            hr_rest = min(hr_rest, avg_hr - 5.0)                 # reposo < promedio siempre

            hrr = (avg_hr - hr_rest) / (hr_max - hr_rest)
            hrr = max(0.30, min(1.00, hrr))

            if _cycling:
                # Ciclismo: IF = %HRR  (Coggan hrTSS, equivalente a TrainingPeaks HR-based)
                # Validado: 129bpm avg / 167bpm max / 2h33min → IF=0.675 → TSS≈116 (TP: 116 HR-based)
                if_value = max(0.35, min(1.05, hrr))
            else:
                # Running/natación/otros: IF = 0.40 + %HRR × 0.65  (Karvonen-TRIMP)
                if_value = max(0.50, min(1.05, 0.40 + hrr * 0.65))
        except Exception:
            if_value = None

    # ── Prioridad 3: Training Effect aeróbico de Garmin (escala 0–5) ─────────
    if if_value is None:
        effect = (
            activity.get("activityTrainingEffect")
            or activity.get("trainingEffect")
            or activity.get("aerobicTrainingEffect")
        )
        if effect is not None:
            try:
                effect_norm = max(0.0, min(float(effect) / 5.0, 1.2))
                if_value = max(0.50, min(1.05, 0.50 + (effect_norm * 0.45)))
            except Exception:
                if_value = None

    # ── Prioridad 4: IF genérico por deporte ─────────────────────────────────
    if if_value is None:
        if_value = 0.60 if _cycling else 0.68  # ciclismo Z2 ≈ 0.60, running Z2 ≈ 0.68

    tss = hours * (if_value ** 2) * 100.0
    return max(0.0, min(tss, 500.0)), "hrTSS"


def _percentile(values: list[float], pct: float, default: float = 0.0) -> float:
    """Calcula un percentil simple sin dependencias externas."""
    cleaned = sorted(float(v) for v in values if v is not None)
    if not cleaned:
        return float(default)
    p = max(0.0, min(float(pct), 1.0))
    idx = int(round((len(cleaned) - 1) * p))
    return cleaned[idx]


# ── Configuración de modelo de carga/fatiga por tipo de deporte ───────────────
# Cada deporte tiene unos tau (constantes de tiempo) y percentiles distintos:
#   - Trail running / ultrafondo: sesiones muy largas y TSS muy variable →
#     ATL más largo (acumula fatiga lento) y percentiles más amplios.
#   - Running de pista/carretera: volumen moderado, respuesta más ágil.
#   - Ciclismo: mayor volumen horario, CTL más largo (el fitness tarda más).
#   - Triatlón: multimodal, se asemeja al ciclismo en tau pero percentiles amplios.
#   - Genérico (otro / desconocido): valores medios conservadores.
#
# Los valores pueden sobreescribirse con profile.load_metrics.model.
_SPORT_MODEL_DEFAULTS: dict[str, dict] = {
    "trail running": {
        "atl_tau_days": 8,
        "ctl_tau_days": 42,
        "tsb_low_pct": 0.15,
        "tsb_high_pct": 0.80,
        "atl_high_pct": 0.85,
        "weekly_target_pct": 0.55,
        "weekly_high_pct": 0.90,
        "tsb_abs_floor": -35.0,   # TSB ≤ esto → OVERLOAD obligatorio
    },
    "running": {
        "atl_tau_days": 7,
        "ctl_tau_days": 42,
        "tsb_low_pct": 0.20,
        "tsb_high_pct": 0.80,
        "atl_high_pct": 0.80,
        "weekly_target_pct": 0.55,
        "weekly_high_pct": 0.85,
        "tsb_abs_floor": -30.0,
    },
    "ciclismo": {
        "atl_tau_days": 7,
        "ctl_tau_days": 45,
        "tsb_low_pct": 0.20,
        "tsb_high_pct": 0.80,
        "atl_high_pct": 0.80,
        "weekly_target_pct": 0.55,
        "weekly_high_pct": 0.85,
        "tsb_abs_floor": -32.0,
    },
    "triatlón": {
        "atl_tau_days": 7,
        "ctl_tau_days": 45,
        "tsb_low_pct": 0.15,
        "tsb_high_pct": 0.80,
        "atl_high_pct": 0.85,
        "weekly_target_pct": 0.55,
        "weekly_high_pct": 0.90,
        "tsb_abs_floor": -35.0,
    },
    "otro": {
        "atl_tau_days": 7,
        "ctl_tau_days": 42,
        "tsb_low_pct": 0.20,
        "tsb_high_pct": 0.80,
        "atl_high_pct": 0.80,
        "weekly_target_pct": 0.55,
        "weekly_high_pct": 0.85,
        "tsb_abs_floor": -30.0,
    },
}
_SPORT_MODEL_DEFAULTS["triaton"] = _SPORT_MODEL_DEFAULTS["triatlón"]   # alias: triatón sin tilde
_SPORT_MODEL_DEFAULTS["triatlon"] = _SPORT_MODEL_DEFAULTS["triatlón"]  # alias: triatlon sin tilde (datos migrados)


def _resolve_sport_model_cfg(profile: dict | None) -> dict:
    """Devuelve la configuración base para el deporte principal del perfil,
    aplicando después cualquier override manual que el usuario haya guardado
    en profile.load_metrics.model."""
    p = profile or {}
    sport_raw = str((p.get("goals") or {}).get("primary") or "running").strip().lower()
    base = dict(_SPORT_MODEL_DEFAULTS.get(sport_raw) or _SPORT_MODEL_DEFAULTS["running"])

    saved_model = (p.get("load_metrics") or {}).get("model") or {}
    for key in ("atl_tau_days", "ctl_tau_days", "tsb_low_pct", "tsb_high_pct",
                "atl_high_pct", "weekly_target_pct", "weekly_high_pct"):
        if key in saved_model:
            try:
                base[key] = float(saved_model[key])
            except Exception:
                pass

    return base


def _compute_load_fatigue_metrics(
    activities: list[dict],
    trend_payload: Any,
    profile: dict | None = None,
    days_window: int = 56,
) -> dict | None:
    """Calcula TSS/ATL/CTL/TSB y reglas de actuación con rangos individualizados por deporte."""
    today = date.today()
    start_day = today - timedelta(days=max(14, days_window - 1))

    tss_by_day: dict[str, float] = {}

    for item in _extract_training_load_points(trend_payload):
        d_iso = item.get("date")
        if not d_iso:
            continue
        try:
            d_obj = date.fromisoformat(d_iso)
        except Exception:
            continue
        if d_obj < start_day or d_obj > today:
            continue
        tss_by_day[d_iso] = max(tss_by_day.get(d_iso, 0.0), float(item.get("tss") or 0.0))

    for act in list(activities or []):
        if not isinstance(act, dict):
            continue
        d_iso = _to_iso_date(
            act.get("startTimeLocal")
            or act.get("startTimeGMT")
            or act.get("date")
            or act.get("calendarDate")
        )
        if not d_iso:
            continue
        try:
            d_obj = date.fromisoformat(d_iso)
        except Exception:
            continue
        if d_obj < start_day or d_obj > today:
            continue
        tss, _ = _estimate_session_tss(act)
        if tss > 0:
            tss_by_day[d_iso] = tss_by_day.get(d_iso, 0.0) + tss

    if not tss_by_day:
        return None

    # ── Configuración de tau y percentiles por deporte (con override por perfil) ──
    model_cfg = _resolve_sport_model_cfg(profile)
    tau_atl = int(round(float(model_cfg.get("atl_tau_days") or 7)))
    tau_ctl = int(round(float(model_cfg.get("ctl_tau_days") or 42)))
    tau_atl = max(3, min(tau_atl, 14))
    tau_ctl = max(21, min(tau_ctl, 90))

    sport_raw = str(((profile or {}).get("goals") or {}).get("primary") or "running").strip().lower()

    saved_last = ((profile or {}).get("load_metrics") or {}).get("last") or {}
    atl_prev = max(0.0, float(saved_last.get("atl") or 0.0))
    ctl_prev = max(0.0, float(saved_last.get("ctl") or 0.0))
    seed_date_iso = _to_iso_date(saved_last.get("date"))
    if seed_date_iso:
        try:
            seed_date = date.fromisoformat(seed_date_iso)
            if seed_date < start_day:
                atl_prev = 0.0
                ctl_prev = 0.0
        except Exception:
            pass

    alpha_atl = 1.0 / float(tau_atl)
    alpha_ctl = 1.0 / float(tau_ctl)

    series: list[dict] = []
    day_cursor = start_day
    while day_cursor <= today:
        d_iso = day_cursor.isoformat()
        tss = max(0.0, float(tss_by_day.get(d_iso, 0.0)))
        atl = atl_prev + (tss - atl_prev) * alpha_atl
        ctl = ctl_prev + (tss - ctl_prev) * alpha_ctl
        tsb = ctl - atl
        row = {
            "date": d_iso,
            "tss": round(tss, 1),
            "atl": round(atl, 1),
            "ctl": round(ctl, 1),
            "tsb": round(tsb, 1),
        }
        series.append(row)
        atl_prev = atl
        ctl_prev = ctl
        day_cursor += timedelta(days=1)

    latest = series[-1]
    last_28 = series[-28:] if len(series) >= 28 else series[:]
    last_42 = series[-42:] if len(series) >= 42 else series[:]
    atl_values = [float(x["atl"]) for x in last_28]
    tsb_values = [float(x["tsb"]) for x in last_28]

    weekly_tss_values: list[float] = []
    for idx in range(0, len(last_42), 7):
        chunk = last_42[idx: idx + 7]
        if chunk:
            weekly_tss_values.append(round(sum(float(x["tss"]) for x in chunk), 1))
    # Semana actual: lunes de esta semana → hoy (no los últimos 7 días del array)
    _week_start_iso = (today - timedelta(days=today.weekday())).isoformat()
    current_week_tss = round(
        sum(float(x["tss"]) for x in series if (x.get("date") or "") >= _week_start_iso),
        1,
    )

    tsb_low = round(_percentile(tsb_values, float(model_cfg.get("tsb_low_pct") or 0.20), default=-10.0), 1)
    tsb_high = round(_percentile(tsb_values, float(model_cfg.get("tsb_high_pct") or 0.80), default=5.0), 1)
    atl_high = round(_percentile(atl_values, float(model_cfg.get("atl_high_pct") or 0.80), default=max(50.0, float(latest["atl"]))), 1)
    weekly_target = round(_percentile(weekly_tss_values, float(model_cfg.get("weekly_target_pct") or 0.55), default=current_week_tss), 1)
    weekly_high = round(_percentile(weekly_tss_values, float(model_cfg.get("weekly_high_pct") or 0.85), default=max(current_week_tss, weekly_target * 1.15)), 1)

    # ── Flag de calibración del modelo ────────────────────────────────────────
    # El modelo EWMA arranca desde ATL=0/CTL=0 y necesita ~3 semanas de datos
    # reales para que los percentiles sean fiables. Durante ese período los
    # colores pueden ser más negativos de lo que corresponde a la carga real.
    days_with_load = sum(1 for x in series if float(x.get("tss") or 0.0) > 0)
    _MIN_DAYS_FOR_RELIABLE_RANGES = 21
    warming_up = days_with_load < _MIN_DAYS_FOR_RELIABLE_RANGES
    warming_up_days_remaining = max(0, _MIN_DAYS_FOR_RELIABLE_RANGES - days_with_load)

    tsb_now = float(latest["tsb"])
    atl_now = float(latest["atl"])
    tsb_abs_floor = float(model_cfg.get("tsb_abs_floor") or -30.0)
    # OVERLOAD absoluto: TSB por debajo del suelo del deporte, independientemente de percentiles.
    # Cubre el caso donde el atleta es crónicamente sobreentrenado y sus percentiles
    # se han adaptado a valores muy negativos (el p15 puede coincidir con el valor actual).
    abs_overload = tsb_now <= tsb_abs_floor
    # Bug fix: usar <= en lugar de < para cubrir el caso límite donde tsb_now == tsb_low
    # (percentil p15 coincide exactamente con el valor actual del último día).
    sustained_overload = len(series) >= 7 and all(float(x["tsb"]) <= tsb_low for x in series[-7:])
    fatigue_high = (tsb_now < tsb_low) or (atl_now > atl_high)
    available_for_quality = (tsb_now >= tsb_low) and (tsb_now <= max(tsb_high, tsb_low + 4.0)) and not fatigue_high

    if abs_overload or sustained_overload or (current_week_tss > weekly_high and tsb_now < tsb_low):
        status = "overload"
        action = "sobrecarga sostenida"
        recommendation = "Activa semana de descarga (−30% a −40% de volumen) y elimina calidad intensa 3-5 dias."
    elif fatigue_high:
        status = "fatigue_high"
        action = "fatiga alta"
        recommendation = "Reduce intensidad/volumen hoy y prioriza recuperación activa, sueño e hidratación."
    elif available_for_quality:
        status = "ready"
        action = "buena disponibilidad"
        recommendation = "Puedes mantener sesión de calidad o progresión controlada según plan."
    else:
        status = "neutral"
        action = "carga estable"
        recommendation = "Mantén carga aeróbica controlada y reevalúa mañana con HRV/sueño/estrés."

    return {
        "model": {
            "name": "tp-inspired-ewma",
            "sport": sport_raw,
            "atl_tau_days": tau_atl,
            "ctl_tau_days": tau_ctl,
            "tsb_low_pct": model_cfg.get("tsb_low_pct") or 0.20,
            "tsb_high_pct": model_cfg.get("tsb_high_pct") or 0.80,
            "atl_high_pct": model_cfg.get("atl_high_pct") or 0.80,
        },
        "latest": latest,
        "series": series[-120:],
        "weekly": {
            "current_tss": current_week_tss,
            "target_tss": weekly_target,
            "high_tss": weekly_high,
        },
        "ranges": {
            "tsb_low": tsb_low,
            "tsb_high": tsb_high,
            "atl_high": atl_high,
            "tsb_abs_floor": tsb_abs_floor,
        },
        "warming_up": warming_up,
        "warming_up_days_remaining": warming_up_days_remaining,
        "days_with_load": days_with_load,
        "flags": {
            "fatigue_high": fatigue_high,
            "sustained_overload": sustained_overload,
            "abs_overload": abs_overload,
            "available_for_quality": available_for_quality,
            "warming_up": warming_up,
        },
        "status": status,
        "action": action,
        "recommendation": recommendation,
    }


def _build_load_trend_table(series: list[dict], mode: str = "weeks") -> str:
    """Genera una tabla Markdown con la tendencia de carga/fatiga.

    Args:
        series: lista de dicts con {date, tss, atl, ctl, tsb} ordenada por fecha asc.
        mode: "weeks" (últimas 8 semanas) o "months" (últimos 3 meses).

    Returns:
        Tabla en Markdown con encabezado, filas por periodo y leyenda de estado.
    """
    if not series:
        return "Sin datos de carga/fatiga disponibles. Inicia una sesión para que el sistema los calcule."

    _STATUS_EMOJI = {
        "overload": "🔴 sobrecarga",
        "fatigue_high": "🟠 fatiga alta",
        "ready": "🟢 disponible",
        "neutral": "🟡 estable",
    }

    def _row_status(tsb: float, tsb_low: float = -10.0, tsb_high: float = 5.0, atl: float = 0.0, atl_high: float = 9999.0) -> str:
        """Clasifica el estado de la fila según TSB/ATL."""
        fatigue = tsb < tsb_low or atl > atl_high
        available = not fatigue and (tsb_low <= tsb <= tsb_high)
        if fatigue and tsb < tsb_low * 1.5:
            return _STATUS_EMOJI["overload"]
        if fatigue:
            return _STATUS_EMOJI["fatigue_high"]
        if available:
            return _STATUS_EMOJI["ready"]
        return _STATUS_EMOJI["neutral"]

    def _fmt_date_range(start_iso: str, end_iso: str) -> str:
        try:
            s = datetime.fromisoformat(start_iso).strftime("%d/%m")
            e = datetime.fromisoformat(end_iso).strftime("%d/%m")
            return f"{s}–{e}"
        except Exception:
            return f"{start_iso}–{end_iso}"

    def _fmt_month(iso: str) -> str:
        _MONTHS_SHORT = {
            "01": "Ene", "02": "Feb", "03": "Mar", "04": "Abr",
            "05": "May", "06": "Jun", "07": "Jul", "08": "Ago",
            "09": "Sep", "10": "Oct", "11": "Nov", "12": "Dic",
        }
        parts = iso.split("-")
        if len(parts) >= 2:
            return f"{_MONTHS_SHORT.get(parts[1], parts[1])} {parts[0]}"
        return iso

    # Ordenar por fecha ascendente
    sorted_series = sorted(series, key=lambda x: str(x.get("date") or ""))

    if mode == "months":
        # Agregar por mes calendario (últimos 3 meses)
        buckets: dict[str, list[dict]] = {}
        for row in sorted_series:
            d_iso = str(row.get("date") or "")
            month_key = d_iso[:7]  # YYYY-MM
            buckets.setdefault(month_key, []).append(row)

        month_keys = sorted(buckets)[-3:]
        if not month_keys:
            return "Sin datos suficientes para vista mensual."

        header = (
            "| Mes | TSS total | ATL fin | CTL fin | TSB fin | Estado |\n"
            "|---|---:|---:|---:|---:|---|\n"
        )
        rows_md: list[str] = []
        for mk in month_keys:
            month_rows = buckets[mk]
            tss_total = round(sum(float(r.get("tss") or 0.0) for r in month_rows), 1)
            last = month_rows[-1]
            atl = float(last.get("atl") or 0.0)
            ctl = float(last.get("ctl") or 0.0)
            tsb = float(last.get("tsb") or 0.0)
            estado = _row_status(tsb, atl=atl)
            rows_md.append(
                f"| {_fmt_month(mk)} | {tss_total:.1f} | {atl:.1f} | {ctl:.1f} | {tsb:+.1f} | {estado} |"
            )

        return (
            "## 📅 Tendencia de carga mensual (últimos 3 meses)\n\n"
            + header
            + "\n".join(rows_md)
            + "\n\n"
            + "_TSS: carga de sesión · ATL: fatiga aguda (7d) · CTL: fitness crónico · TSB: forma (CTL−ATL)_"
        )

    # Vista semanal: últimas 8 semanas naturales lunes→domingo
    today = date.today()
    _week_mon = today - timedelta(days=today.weekday())  # lunes de esta semana
    weeks: list[tuple[str, str, list[dict]]] = []
    for w in range(7, -1, -1):
        mon = _week_mon - timedelta(weeks=w)
        sun = mon + timedelta(days=6)
        week_rows = [
            r for r in sorted_series
            if mon.isoformat() <= str(r.get("date") or "") <= sun.isoformat()
        ]
        weeks.append((mon.isoformat(), sun.isoformat(), week_rows))

    # Descartar semanas vacías al principio
    first_non_empty = next((i for i, (_, _, wr) in enumerate(weeks) if wr), 0)
    weeks = weeks[first_non_empty:]
    if not weeks:
        return "Sin datos suficientes para vista semanal."

    header = (
        "| Semana | TSS | ATL | CTL | TSB | Estado |\n"
        "|---|---:|---:|---:|---:|---|\n"
    )
    rows_md = []
    for start_iso, end_iso, week_rows in weeks:
        tss_sum = round(sum(float(r.get("tss") or 0.0) for r in week_rows), 1)
        if week_rows:
            last = week_rows[-1]
            atl = float(last.get("atl") or 0.0)
            ctl = float(last.get("ctl") or 0.0)
            tsb = float(last.get("tsb") or 0.0)
        else:
            atl = ctl = tsb = 0.0
        estado = _row_status(tsb, atl=atl)
        rows_md.append(
            f"| {_fmt_date_range(start_iso, end_iso)} | {tss_sum:.1f} | {atl:.1f} | {ctl:.1f} | {tsb:+.1f} | {estado} |"
        )

    # Nota de warm-up: si la primera semana con datos tiene CTL < 15, el modelo aún se está calibrando
    first_ctl_values = [
        float(r.get("ctl") or 0.0)
        for (_, _, wr) in weeks
        for r in wr
        if float(r.get("ctl") or 0.0) > 0
    ]
    warmup_note = (
        "\n_⚙️ Las primeras semanas reflejan el arranque del modelo (CTL bajo), no necesariamente una sobrecarga real._"
        if first_ctl_values and first_ctl_values[0] < 15.0
        else ""
    )

    return (
        "## 📊 Tendencia de carga semanal (últimas 8 semanas)\n\n"
        + header
        + "\n".join(rows_md)
        + "\n\n"
        + "_TSS: carga de sesión · ATL: fatiga aguda · CTL: fitness crónico · TSB: forma (CTL−ATL)_\n"
        + "_🟢 disponible = puedes calidad · 🟠 fatiga alta = reduce carga · 🔴 sobrecarga = descarga obligatoria_"
        + warmup_note
    )


def _format_load_fatigue_summary(load_metrics: dict | None) -> str:
    """Genera resumen textual corto para el bloque proactivo."""
    if not isinstance(load_metrics, dict) or not load_metrics.get("latest"):
        return "sin datos suficientes"
    latest = load_metrics.get("latest") or {}
    weekly = load_metrics.get("weekly") or {}
    action = str(load_metrics.get("action") or "carga estable")
    try:
        return (
            f"TSS hoy {float(latest.get('tss', 0.0)):.1f} · "
            f"ATL {float(latest.get('atl', 0.0)):.1f} · "
            f"CTL {float(latest.get('ctl', 0.0)):.1f} · "
            f"TSB {float(latest.get('tsb', 0.0)):.1f} · "
            f"Semana {float(weekly.get('current_tss', 0.0)):.1f} TSS ({action})"
        )
    except Exception:
        return "sin datos suficientes"


def _build_proactive_status_markdown(snapshot: dict) -> str:
    """Genera un bloque Markdown con estado proactivo de últimas 48h."""
    def _is_generic_ok_summary(text: str) -> bool:
        lowered = (text or "").strip().lower()
        return "hoy=ok" in lowered or "ayer=ok" in lowered or "hoy=no" in lowered or "ayer=no" in lowered

    def _to_ddmmyyyy(value: str) -> str:
        try:
            return datetime.fromisoformat(value).strftime("%d/%m/%Y")
        except Exception:
            return value

    profile_changes = snapshot.get("profile_changes", []) or []
    plan_assigned = bool(snapshot.get("plan_assigned", False))
    plan_recommendation = str(snapshot.get("plan_recommendation") or "").strip()
    body_battery = snapshot.get("body_battery", {}) or {}
    hrv = snapshot.get("hrv", {}) or {}
    sleep = snapshot.get("sleep", {}) or {}
    load_fatigue = snapshot.get("load_fatigue") or {}
    trainings = snapshot.get("trainings", []) or []
    dates = snapshot.get("dates", {}) or {}
    today_iso = str(dates.get("today") or date.today().isoformat())
    yesterday_iso = str(dates.get("yesterday") or (date.today() - timedelta(days=1)).isoformat())
    today_display = _to_ddmmyyyy(today_iso)
    yesterday_display = _to_ddmmyyyy(yesterday_iso)

    lines = [
        "## Estado Proactivo (ultimas 48h)",
        "",
    ]

    if profile_changes:
        lines.append(f"- Perfil Garmin actualizado: {', '.join(profile_changes)}")
    else:
        lines.append("- Perfil Garmin sin cambios detectados")

    lines.append(f"- Fechas analizadas: hoy={today_display} · ayer={yesterday_display}")

    body_summary = body_battery.get("summary") or ""
    if body_battery.get("today") is not None or body_battery.get("yesterday") is not None:
        body_summary = (
            f"hoy={_format_body_battery_day(body_battery.get('today'), today_iso)} · "
            f"ayer={_format_body_battery_day(body_battery.get('yesterday'), yesterday_iso)}"
        )
    elif _is_generic_ok_summary(body_summary):
        body_summary = "sin datos recientes"

    hrv_summary = hrv.get("summary") or ""
    if hrv.get("today") is not None or hrv.get("yesterday") is not None:
        hrv_summary = (
            f"hoy={_format_hrv_day(hrv.get('today'), today_iso)} · "
            f"ayer={_format_hrv_day(hrv.get('yesterday'), yesterday_iso)}"
        )
    elif _is_generic_ok_summary(hrv_summary):
        hrv_summary = "sin datos recientes"

    sleep_summary = sleep.get("summary") or ""
    if sleep.get("today") is not None or sleep.get("yesterday") is not None:
        sleep_summary = (
            f"hoy={_format_sleep_day(sleep.get('today'), today_iso)} · "
            f"ayer={_format_sleep_day(sleep.get('yesterday'), yesterday_iso)}"
        )
    elif _is_generic_ok_summary(sleep_summary):
        sleep_summary = "sin datos recientes"

    lines.append("- Body Battery: " + (body_summary or "sin datos recientes"))
    lines.append("- HRV: " + (hrv_summary or "sin datos recientes"))
    lines.append("- Sueno: " + (sleep_summary or "sin datos recientes"))
    lines.append("- Carga/Fatiga (TSS/ATL/CTL/TSB): " + _format_load_fatigue_summary(load_fatigue))

    if load_fatigue and load_fatigue.get("latest"):
        latest = load_fatigue.get("latest") or {}
        ranges = load_fatigue.get("ranges") or {}
        weekly = load_fatigue.get("weekly") or {}
        recommendation = str(load_fatigue.get("recommendation") or "").strip()
        if latest:
            lines.append(
                "  - Estado: "
                f"TSB={float(latest.get('tsb', 0.0)):.1f} "
                f"(objetivo {float(ranges.get('tsb_low', 0.0)):.1f}..{float(ranges.get('tsb_high', 0.0)):.1f}), "
                f"ATL={float(latest.get('atl', 0.0)):.1f} "
                f"(alto>{float(ranges.get('atl_high', 0.0)):.1f}), "
                f"TSS semanal={float(weekly.get('current_tss', 0.0)):.1f}"
            )
        if recommendation:
            lines.append(f"  - Regla aplicada: {recommendation}")
        if load_fatigue.get("warming_up"):
            days_rem = int(load_fatigue.get("warming_up_days_remaining") or 0)
            weeks_rem = max(1, round(days_rem / 7))
            lines.append(
                f"  - ⚙️ Modelo en calibracion ({int(load_fatigue.get('days_with_load') or 0)} dias con datos). "
                f"Los rangos seran fiables en ~{weeks_rem} semana{'s' if weeks_rem != 1 else ''} mas."
            )
    elif not load_fatigue.get("latest"):
        # Sin datos calculados — mostrar diagnóstico para entender el motivo
        load_debug = str(snapshot.get("load_debug") or "").strip()
        if "sin actividades" in load_debug or "usuario nuevo" in load_debug:
            lines.append(
                "  ⚠️ Sin histórico de entrenamientos detectado — "
                "el modelo se calibrará en ~3 semanas una vez se registren actividades en Garmin."
            )
        elif load_debug and load_debug not in ("ok",):
            lines.append(f"  ⚠️ No se pudieron obtener actividades históricas · diagnóstico: {load_debug}")

    if trainings:
        lines.append("- Entrenamientos recientes:")
        for item in trainings[:3]:
            name = item.get("name") or "Actividad"
            day = item.get("date") or "fecha desconocida"
            lines.append(f"  - {day}: {name}")
    else:
        lines.append("- Entrenamientos recientes: no se encontraron en las ultimas 48h")

    if plan_assigned:
        initial_recommendation = (
            plan_recommendation
            or "Tienes un plan activo. ¿Quieres que adapte la sesion de hoy a ese plan?"
        )
    else:
        initial_recommendation = "No tienes plan asignado. ¿Que quieres hacer hoy?"

    lines.extend([
        "",
        "### Recomendacion inicial",
        f"- {initial_recommendation}",
    ])
    return "\n".join(lines)


def _is_generic_needs_more_info_reply(text: str) -> bool:
    """Detecta respuestas genéricas de "falta información" cuando ya hay contexto suficiente."""
    raw = (text or "").strip().lower()
    if not raw:
        return False
    markers = [
        "no puedo crear una planificación",
        "no puedo analizar",
        "sin más información",
        "proporciona más detalles",
        "por favor, proporciona más",
    ]
    return any(marker in raw for marker in markers)


def _is_planning_intent(user_message: str) -> bool:
    """Detecta intención de planificación en la consulta del usuario."""
    text = (user_message or "").strip().lower()
    if not text:
        return False
    # Palabras que indican CREAR o MODIFICAR un plan, no consultar stats.
    # 'semana' y 'bloque' se eliminaron: son demasiado genéricas y
    # provocan falsos positivos en consultas de estadisticas ('cuantos km esta semana').
    planning_markers = [
        "plan", "planifica", "planificación", "planificacion",
        "preparar", "preparación", "preparacion",
        "macro", "microciclo",
    ]
    if not any(marker in text for marker in planning_markers):
        return False
    # Guardia anti-falso-positivo: consultas de estado de objetivo no son planificación.
    # 'objetivo' solo clasifica como planning si va acompañado de un verbo de acción.
    if "objetivo" in text and not any(m in text for m in ("preparar", "planifica", "alcanzar", "lograr", "conseguir")):
        return "plan" in text or any(m in text for m in ("macro", "microciclo", "preparaci"))
    return True


def _is_plan_status_intent(user_message: str) -> bool:
    """Detecta preguntas sobre si existe un plan activo o cuál es ese plan."""
    text = (user_message or "").strip().lower()
    if not text or "plan" not in text:
        return False

    # Peticiones de creación/planificación: no son consultas de estado.
    creation_markers = [
        "planifica", "planificación", "planificacion", "crear", "créame", "creame",
        "hazme", "diseña", "disena", "prepara", "recomienda", "recomiendas",
        "ajusta", "ajusta", "modifica", "cambia", "actualiza",
    ]
    if any(marker in text for marker in creation_markers):
        return False

    status_markers = [
        "tengo", "hay", "existe", "asignado", "asignada",
        "mi plan", "ese plan", "cuál es", "cual es", "qué plan", "que plan",
    ]
    return any(marker in text for marker in status_markers)


def _format_iso_date_es(value: Any) -> str:
    """Convierte fechas ISO (YYYY-MM-DD o ISO datetime) a DD/MM/AAAA para usuario."""
    if not value:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    # Caso ISO date/datetime común
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).strftime("%d/%m/%Y")
    except Exception:
        pass
    # Intento conservador con solo la parte de fecha
    if len(text) >= 10:
        try:
            return date.fromisoformat(text[:10]).strftime("%d/%m/%Y")
        except Exception:
            return text
    return text


def _build_training_plan_status_markdown(profile: dict) -> str:
    """Construye respuesta clara y coherente para consultas de estado de plan."""
    plan = _get_active_training_plan(profile)
    goals = (profile or {}).get("goals", {}) if isinstance(profile, dict) else {}

    if not plan:
        lines = [
            "## 🧭 Resumen",
            "No tienes plan asignado ahora mismo.",
        ]
        if _has_goal_in_profile(profile):
            race = goals.get("target_race") or "objetivo definido"
            race_date = _format_iso_date_es(goals.get("target_race_date")) or "fecha por definir"
            target_time = goals.get("target_time") or "tiempo por definir"
            weekly_hours = goals.get("weekly_training_hours") or "por definir"
            lines.extend([
                "",
                "## 📌 Objetivo guardado",
                f"- Evento: {race}",
                f"- Fecha objetivo: {race_date}",
                f"- Tiempo objetivo: {target_time}",
                f"- Horas/semana: {weekly_hours}",
            ])
        lines.extend([
            "",
            "## ✅ Siguiente paso",
            "Si quieres, te preparo un plan activo a partir de ese objetivo.",
        ])
        return "\n".join(lines)

    title = str(plan.get("title") or plan.get("name") or "Plan activo").strip()
    today_focus = str(plan.get("today_focus") or plan.get("today_session") or "").strip()
    status = str(plan.get("status") or "active").strip()
    race = plan.get("target_race") or goals.get("target_race") or "objetivo definido"
    race_date = _format_iso_date_es(plan.get("target_race_date") or goals.get("target_race_date")) or "fecha por definir"

    lines = [
        "## 🧭 Resumen",
        f"Sí, tienes un plan activo: {title}.",
        "",
        "## 📋 Detalle del plan",
        f"- Estado: {status}",
        f"- Objetivo: {race}",
        f"- Fecha objetivo: {race_date}",
    ]
    if today_focus:
        lines.append(f"- Sesión sugerida hoy: {today_focus}")
    lines.extend([
        "",
        "## ✅ Siguiente paso",
        "Si quieres, adapto la sesión de hoy según tu recuperación actual.",
    ])
    return "\n".join(lines)


def _is_personal_records_intent(user_message: str) -> bool:
    """Detecta intención de consultar récords personales de running."""
    text = (user_message or "").strip().lower()
    if not text:
        return False
    markers = [
        "record personal",
        "records personales",
        "récord personal",
        "mejores registros",
        "personal records",
        "pr de",
        "mejores marcas",
        "marcas personales",
    ]
    return any(marker in text for marker in markers)


def _is_personal_records_followup_intent(user_message: str, history: list[dict]) -> bool:
    """Detecta follow-up tipo "en qué distancias son esas marcas"."""
    text = (user_message or "").strip().lower()
    if not text:
        return False

    followup_markers = [
        "esas marcas",
        "que distancias",
        "en que distancias",
        "qué distancias",
        "de que distancia",
        "de qué distancia",
    ]
    if not any(marker in text for marker in followup_markers):
        return False

    recent_assistant = [
        (msg.get("content") or "").lower()
        for msg in (history or [])[-6:]
        if msg.get("role") == "assistant"
    ]
    return any("mejores registros personales" in content for content in recent_assistant)


def _detect_personal_records_sport_intent(user_message: str, history: list[dict] | None = None) -> str:
    """Detecta el deporte objetivo para consulta de PRs: running o cycling."""
    text = (user_message or "").strip().lower()
    cycling_markers = ["ciclismo", "ciclista", "bici", "bike", "cycling"]
    running_markers = ["running", "correr", "carrera", "marat", "10k", "5k"]

    if any(marker in text for marker in cycling_markers):
        return "cycling"
    if any(marker in text for marker in running_markers):
        return "running"

    recent_assistant = [
        (msg.get("content") or "").lower()
        for msg in (history or [])[-6:]
        if msg.get("role") == "assistant"
    ]
    if any("registros personales en ciclismo" in content for content in recent_assistant):
        return "cycling"
    if any("registros personales en running" in content for content in recent_assistant):
        return "running"

    return "running"


def _is_no_access_reply(text: str) -> bool:
    """Detecta respuestas genéricas de falta de acceso a datos."""
    raw = (text or "").strip().lower()
    if not raw:
        return False
    markers = [
        "no tengo acceso",
        "no dispongo de acceso",
        "no puedo acceder",
    ]
    return any(marker in raw for marker in markers)


def _build_personal_records_markdown(compact_records: str, preferred_sport: str = "running") -> str:
    """Renderiza récords personales en markdown legible para el usuario."""
    data = _try_parse_json(compact_records)
    if not isinstance(data, list) or not data:
        return "No encontré récords personales en Garmin Connect para este usuario."

    rows: list[tuple[str, str, str]] = []
    running_type_ids = {1, 2, 3, 4, 5, 6, 7}
    cycling_type_ids = {8, 9, 11}

    for item in data:
        if not isinstance(item, dict):
            continue
        categoria = (
            item.get("categoria")
            or item.get("tipo")
            or item.get("record_type")
            or "Registro"
        )
        valor = (
            item.get("valor")
            or item.get("tiempo")
            or item.get("distancia")
            or item.get("elevacion")
            or item.get("pasos")
            or item.get("racha")
            or item.get("value")
            or "n/d"
        )
        type_id = item.get("type_id") if item.get("type_id") is not None else item.get("typeId")

        deporte = str(item.get("deporte") or "").lower()
        categoria_lower = str(categoria).lower()
        if isinstance(type_id, int):
            is_running = type_id in running_type_ids
            is_cycling = type_id in cycling_type_ids
        else:
            is_running = (
                "run" in deporte
                or "carrera" in deporte
                or "marathon" in categoria_lower
                or "5k" in categoria_lower
                or "10k" in categoria_lower
                or "longest run" in categoria_lower
            )
            is_cycling = (
                "cycl" in deporte
                or "bike" in deporte
                or "ride" in categoria_lower
                or "cycling" in categoria_lower
            )

        sport = "running" if is_running else "cycling" if is_cycling else "other"
        rows.append((str(categoria), str(valor), sport))

    selected: list[tuple[str, str, str]]
    if preferred_sport == "cycling":
        selected = [r for r in rows if r[2] == "cycling"]
    elif preferred_sport == "running":
        selected = [r for r in rows if r[2] == "running"]
    else:
        selected = rows
    selected = selected[:10]

    if not selected:
        if preferred_sport == "cycling":
            return "No encontré récords personales de ciclismo en Garmin Connect para este usuario."
        if preferred_sport == "running":
            return "No encontré récords personales de running en Garmin Connect para este usuario."
        return "No encontré récords personales en Garmin Connect para este usuario."

    sport_label = "ciclismo" if preferred_sport == "cycling" else "running"

    lines = [
        f"## Tus mejores registros personales en {sport_label}",
        "",
        "| Distancia / récord | Marca |",
        "|---|---|",
    ]
    for categoria, valor, _ in selected:
        lines.append(f"| {categoria} | {valor} |")

    return "\n".join(lines)


def _has_goal_in_profile(profile: dict) -> bool:
    """Comprueba si el perfil ya contiene un objetivo útil para planificar."""
    goals = (profile or {}).get("goals", {})
    return bool(
        goals.get("target_race")
        or goals.get("target_race_date")
        or goals.get("target_time")
        or goals.get("weekly_training_hours")
    )


def _normalize_storage_plan_row(row: dict) -> dict | None:
    """Normaliza una fila de training_plan (DB) al formato usado por el agente."""
    if not isinstance(row, dict):
        return None

    plan_data = row.get("plan_data")
    merged: dict = dict(plan_data) if isinstance(plan_data, dict) else {}
    status = str(row.get("status") or merged.get("status") or "").strip().lower()

    merged.update(
        {
            "id": row.get("id") or merged.get("id"),
            "title": row.get("title") or merged.get("title") or merged.get("name") or "Plan activo",
            "description": row.get("description") or merged.get("description") or "",
            "objective": row.get("objective") or merged.get("objective") or "",
            "difficulty": row.get("difficulty") or merged.get("difficulty") or "moderate",
            "duration_weeks": row.get("duration_weeks") if row.get("duration_weeks") is not None else merged.get("duration_weeks"),
            "status": status or "active",
            "source": row.get("source") or merged.get("source") or "agent",
            "active": (status == "active"),
        }
    )
    return merged


def _get_active_training_plan(profile: dict) -> dict | None:
    """Devuelve el plan activo del atleta; prioriza DB y usa perfil como fallback."""
    try:
        db_row = _storage.get_active_training_plan()
        db_plan = _normalize_storage_plan_row(db_row)
        if db_plan:
            plan_id = db_plan.get("id")
            if plan_id:
                try:
                    sessions = _storage.list_training_plan_sessions(str(plan_id))
                    if sessions:
                        db_plan["sessions"] = sessions
                except Exception:
                    pass
            return db_plan
    except Exception:
        pass

    plan = (profile or {}).get("training_plan")
    if not isinstance(plan, dict):
        return None

    status = str(plan.get("status") or "").strip().lower()
    active_flag = plan.get("active")
    is_active = bool(active_flag) if isinstance(active_flag, bool) else status in {
        "active", "assigned", "current", "in_progress"
    }
    if not is_active:
        return None
    return plan


def _build_startup_plan_recommendation(plan: dict) -> str:
    """Construye la recomendación inicial cuando existe plan activo."""
    title = str(plan.get("title") or plan.get("name") or "plan activo").strip()
    today_focus = str(plan.get("today_focus") or plan.get("today_session") or "").strip()
    if today_focus:
        return f"Tienes plan activo ({title}). Sesión sugerida hoy: {today_focus}. ¿Quieres que la ajuste con tu estado actual?"
    return f"Tienes plan activo ({title}). ¿Quieres que adapte la sesión de hoy a ese plan?"


def _build_goal_plan_fallback(profile: dict) -> str:
    """Genera una planificación base útil usando el objetivo guardado en el perfil."""
    goals = (profile or {}).get("goals", {})
    health = (profile or {}).get("health", {})

    race = goals.get("target_race") or "tu evento objetivo"
    race_date = goals.get("target_race_date") or "fecha por confirmar"
    target_time = goals.get("target_time") or "tiempo por definir"
    weekly_hours = goals.get("weekly_training_hours") or "8-10"
    injuries = ", ".join(health.get("injuries", [])) if health.get("injuries") else "ninguna relevante"

    return (
        "## Planificación Inicial para tu Objetivo\n\n"
        f"- Evento objetivo: {race}\n"
        f"- Fecha objetivo: {race_date}\n"
        f"- Tiempo objetivo: {target_time}\n"
        f"- Horas semanales estimadas: {weekly_hours}\n"
        f"- Condiciones de salud declaradas: {injuries}\n\n"
        "### Estructura semanal propuesta (base)\n"
        "- Lunes: descanso o movilidad + fuerza 30-40 min\n"
        "- Martes: calidad (intervalos/umbral)\n"
        "- Miércoles: rodaje Z2 suave\n"
        "- Jueves: calidad controlada (tempo o cuestas)\n"
        "- Viernes: descanso activo\n"
        "- Sábado: tirada larga progresiva\n"
        "- Domingo: rodaje de recuperación + técnica\n\n"
        "### Próximos pasos\n"
        "- En la siguiente interacción ajustaré paces, volúmenes y progresión según tus datos Garmin recientes "
        "(carga, HRV, sueño y entrenamientos)."
    )


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _wants_new_plan_intent(user_message: str) -> bool:
    text = (user_message or "").strip().lower()
    if not text:
        return False
    markers = [
        "nuevo plan",
        "nuevo ciclo",
        "desde cero",
        "plan nuevo",
        "crear otro plan",
    ]
    return any(marker in text for marker in markers)


def _apply_trail_overrides(sessions: list[dict], has_injuries: bool) -> list[dict]:
    """Enriquece las sesiones con tipos y notas específicos de trail running.

    Modifica en los dicts existentes: session_type, exercises y notes según el rol
    de cada sesión en la semana. No altera duraciones ni intensidades.
    """
    quality_intensity_note = "(RPE conservado por lesión)" if has_injuries else ""

    for s in sessions:
        day = s.get("day_index", 0)
        stype = str(s.get("session_type") or "").lower()

        if stype == "strength":
            s["session_type"] = "strength_trail"
            s["exercises"] = [
                "fuerza excéntrica cuádriceps (sentadillas búlgaras)",
                "isométricos de sóleo y gemelo",
                "hip thrust + trabajo glúteo medio",
                "core antirotacional",
                "movilidad cadera/tobillo",
            ]
            s["notes"] = (
                "Calentamiento 10'. Enfoque en tren inferior para subida/bajada de trail. "
                "Enfriamiento 5' con estiramientos fascia plantar y sóleo. "
                "Hidratación 500 ml. Sin impacto en rodilla si lesión activa."
            )

        elif stype == "running_quality" and day in (2, 4):
            if day == 2:
                s["session_type"] = "trail_hills"
                s["exercises"] = [
                    "cuestas largas 4-6x3-4 min Z4",
                    "bajadas técnicas controladas Z2 (no frenar con el talón)",
                    "técnica de subida con bastones si aplica",
                ]
                s["notes"] = (
                    f"Calentamiento 15' en llano/pendiente suave. "
                    f"Cuestas con desnivel 6-10%. Bajadas controlando impacto. "
                    f"Enfriamiento 10'. {quality_intensity_note} "
                    f"Nutrición pre-sesión. Hidratación 600-800 ml."
                )
            else:
                s["session_type"] = "trail_tempo"
                s["exercises"] = [
                    "tempo continuo Z3-Z4 en terreno variado",
                    "secciones de terreno técnico a ritmo controlado",
                    "economía de carrera en bajada",
                ]
                s["notes"] = (
                    f"Calentamiento 15'. Tempo en terreno mixto (mezcla llano + cuesta suave). "
                    f"Enfriamiento 10'. {quality_intensity_note} "
                    f"Hidratación 500-750 ml. Practica alimentación en movimiento."
                )

        elif stype == "running_z2":
            s["session_type"] = "trail_z2"
            s["exercises"] = [
                "rodaje continuo Z2 en terreno variado",
                "movilidad de cadera en parada breve",
            ]
            s["notes"] = (
                "Calentamiento 10'. Prioriza terreno blando (tierra/hierba) para reducir impacto. "
                "Desnivel acumulado suave (±150 m si es posible). "
                "Enfriamiento 5-10' + estiramientos suaves. Hidratación 500 ml."
            )

        elif stype == "long_run":
            s["session_type"] = "trail_long"
            s["exercises"] = [
                "tirada larga progresiva en terreno de montaña",
                "subidas a potencia constante (RPE, no ritmo)",
                "bajadas técnicas con cadencia alta",
                "alimentación y estrategia de avituallamiento en carrera",
            ]
            dur_h = round((s.get("duration_min") or 90) / 60, 1)
            s["notes"] = (
                f"Salida de {dur_h}h en terreno de trail. "
                f"Objetivo: acumular desnivel positivo (+400-800 m según capacidad). "
                f"Ritmo conversacional Z2. Practica tu estrategia real de avituallamiento: "
                f"carbohidratos 30-60 g/h, hidratación 400-600 ml/h. "
                f"Lleva bastones si el recorrido lo requiere."
            )

        elif stype == "recovery":
            s["session_type"] = "trail_recovery"
            s["exercises"] = [
                "rodaje muy suave en terreno blando",
                "movilidad y estiramientos de fascia plantar, cuádriceps y glúteo",
            ]
            s["notes"] = (
                "Ritmo completamente libre, sin HR objetivo. "
                "Terreno llano o bajada muy suave. "
                "Enfriamiento con rodillo de espuma. Hidratación 400-600 ml."
            )

    return sessions


def _generate_structured_plan_payload(
    profile: dict,
    user_message: str,
    base_plan: dict | None = None,
) -> tuple[dict, list[dict]]:
    """Genera un plan estructurado y sesiones semanales listas para persistir."""
    goals = (profile or {}).get("goals", {})
    health = (profile or {}).get("health", {})

    race = str(goals.get("target_race") or "objetivo de rendimiento").strip()
    race_date = str(goals.get("target_race_date") or "").strip()
    target_time = str(goals.get("target_time") or "").strip()
    weekly_hours = _safe_float(goals.get("weekly_training_hours"), 8.0)
    weekly_hours = min(24.0, max(3.0, weekly_hours))

    duration_weeks = 8
    if race_date:
        try:
            days_to_race = (date.fromisoformat(race_date) - date.today()).days
            duration_weeks = min(16, max(4, int(days_to_race / 7)))
        except Exception:
            duration_weeks = 8

    injuries = list((health or {}).get("injuries") or [])
    has_injuries = bool(injuries)
    injuries_label = ", ".join(injuries[:2]) if injuries else ""

    difficulty = "moderate"
    difficulty_reason = ""
    if has_injuries:
        difficulty = "easy"
        difficulty_reason = f"Dificultad reducida a 'easy' por lesión activa: {injuries_label}."
    elif weekly_hours >= 10:
        difficulty = "hard"
        difficulty_reason = f"Dificultad 'hard' por disponibilidad semanal alta ({weekly_hours}h)."
    else:
        difficulty_reason = f"Dificultad 'moderate' estándar."

    weekly_minutes = int(round(weekly_hours * 60))
    # Distribución por bloques (debe sumar 100)
    ratios = {
        "strength": 10,
        "quality_1": 18,
        "easy": 16,
        "quality_2": 18,
        "rest": 0,
        "long": 28,
        "recovery": 10,
    }

    def _dur(key: str) -> int:
        if key == "rest":
            return 0
        return max(25, int(round((weekly_minutes * ratios[key]) / 100)))

    long_run_min = max(_dur("long"), 70)
    easy_intensity = "RPE 3-4"
    quality_intensity = "RPE 7-8" if not has_injuries else "RPE 5-6"

    sessions = [
        {
            "week_index": 1,
            "day_index": 1,
            "session_type": "strength",
            "duration_min": _dur("strength"),
            "intensity": "RPE 4-5",
            "exercises": ["movilidad cadera/tobillo", "fuerza general", "core"],
            "notes": "Calentamiento 10'. Parte principal de fuerza funcional. Enfriamiento 5'. Hidratación 500-750 ml.",
        },
        {
            "week_index": 1,
            "day_index": 2,
            "session_type": "running_quality",
            "duration_min": _dur("quality_1"),
            "intensity": quality_intensity,
            "exercises": ["intervalos/umbral", "técnica de carrera"],
            "notes": "Calentamiento 15'. Parte principal de calidad por RPE. Enfriamiento 10'. Nutrición pre-sesión + hidratación 500-750 ml.",
        },
        {
            "week_index": 1,
            "day_index": 3,
            "session_type": "running_z2",
            "duration_min": _dur("easy"),
            "intensity": easy_intensity,
            "exercises": ["rodaje continuo", "movilidad breve"],
            "notes": "Calentamiento 10'. Parte principal en Z2. Enfriamiento 5-10'. Hidratación 500 ml.",
        },
        {
            "week_index": 1,
            "day_index": 4,
            "session_type": "running_quality",
            "duration_min": _dur("quality_2"),
            "intensity": quality_intensity,
            "exercises": ["tempo/cuetas", "economía de carrera"],
            "notes": "Calentamiento 15'. Parte principal controlada por RPE. Enfriamiento 10'. Hidratación 500-750 ml.",
        },
        {
            "week_index": 1,
            "day_index": 5,
            "session_type": "rest",
            "duration_min": 0,
            "intensity": "RPE 1-2",
            "exercises": ["descanso activo opcional"],
            "notes": "Recuperación activa opcional: caminar/movilidad 20-30'.",
        },
        {
            "week_index": 1,
            "day_index": 6,
            "session_type": "long_run",
            "duration_min": long_run_min,
            "intensity": "RPE 4-5" if not has_injuries else "RPE 3-4",
            "exercises": ["tirada larga progresiva"],
            "notes": "Calentamiento 10-15'. Parte principal continua. Enfriamiento 10'. Nutrición 30-60 g CH/h e hidratación 500-800 ml/h.",
        },
        {
            "week_index": 1,
            "day_index": 7,
            "session_type": "recovery",
            "duration_min": _dur("recovery"),
            "intensity": "RPE 2-3",
            "exercises": ["rodaje suave", "movilidad"],
            "notes": "Calentamiento suave. Parte principal muy ligera. Enfriamiento corto. Hidratación 400-600 ml.",
        },
    ]

    objective_text = f"Preparación para {race}"
    if target_time:
        objective_text += f" con objetivo de {target_time}"

    sport_primary = str((goals or {}).get("primary") or "running").strip().lower()
    is_trail = "trail" in sport_primary

    if is_trail:
        sessions = _apply_trail_overrides(sessions, has_injuries=has_injuries)

    base_description = "Plan estructurado generado por el coach a partir de objetivos y perfil del atleta."
    if difficulty_reason:
        plan_description = f"{base_description} {difficulty_reason}"
    else:
        plan_description = base_description

    plan = {
        "title": f"Plan hacia {race}",
        "description": plan_description,
        "objective": objective_text,
        "difficulty": difficulty,
        "duration_weeks": duration_weeks,
        "status": "active",
        "source": "agent_structured_plan",
        "plan_data": {
            "target_race": race,
            "target_race_date": race_date,
            "target_time": target_time,
            "weekly_training_hours": weekly_hours,
            "injuries": injuries,
            "difficulty_reason": difficulty_reason,
            "today_focus": "Sesión de calidad o ajuste por recuperación",
            "generation_note": (user_message or "")[:240],
            "base_plan_id": (base_plan or {}).get("id"),
        },
    }
    return plan, sessions


def _validate_structured_plan(plan: dict, sessions: list[dict], profile: dict) -> list[str]:
    """Valida la coherencia básica del plan estructurado antes de persistir."""
    errors: list[str] = []
    if not isinstance(plan, dict):
        return ["Plan inválido: formato no soportado."]

    if not str(plan.get("title") or "").strip():
        errors.append("El plan no tiene título.")
    if not str(plan.get("objective") or "").strip():
        errors.append("El plan no tiene objetivo definido.")
    if int(plan.get("duration_weeks") or 0) <= 0:
        errors.append("La duración del plan debe ser mayor que 0 semanas.")
    if not sessions:
        errors.append("El plan no contiene sesiones.")

    weekly_minutes = 0
    for session in sessions:
        day_index = int(session.get("day_index") or 0)
        if day_index < 1 or day_index > 7:
            errors.append("Hay sesiones con día fuera de rango (1-7).")
            break

        duration = int(session.get("duration_min") or 0)
        session_type = str(session.get("session_type") or "").strip().lower()
        if session_type != "rest" and duration <= 0:
            errors.append("Hay sesiones activas con duración no válida.")
            break
        weekly_minutes += max(0, duration)

    goals = (profile or {}).get("goals", {})
    expected_weekly_hours = _safe_float(goals.get("weekly_training_hours"), 8.0)
    expected_weekly_min = int(max(120, expected_weekly_hours * 60))
    if weekly_minutes > int(expected_weekly_min * 1.35):
        errors.append("La carga semanal propuesta excede claramente las horas semanales objetivo.")

    return errors


def _summarize_plan_changes(
    previous_plan: dict | None,
    new_plan: dict,
    previous_sessions: list[dict] | None,
    new_sessions: list[dict],
) -> str:
    """Resume diferencias entre plan previo y nuevo para trazabilidad funcional."""
    if not previous_plan:
        return "Se creó un plan nuevo y se activó como plan principal."

    changes: list[str] = []
    if (previous_plan.get("duration_weeks") or 0) != (new_plan.get("duration_weeks") or 0):
        changes.append(
            f"Duración: {previous_plan.get('duration_weeks', 0)} -> {new_plan.get('duration_weeks', 0)} semanas"
        )
    if str(previous_plan.get("difficulty") or "") != str(new_plan.get("difficulty") or ""):
        changes.append(f"Dificultad: {previous_plan.get('difficulty', 'n/d')} -> {new_plan.get('difficulty', 'n/d')}")

    prev_sessions_count = len(previous_sessions or [])
    new_sessions_count = len(new_sessions or [])
    if prev_sessions_count != new_sessions_count:
        changes.append(f"Sesiones semanales: {prev_sessions_count} -> {new_sessions_count}")

    prev_total = sum(int((s or {}).get("duration_min") or 0) for s in (previous_sessions or []))
    new_total = sum(int((s or {}).get("duration_min") or 0) for s in (new_sessions or []))
    if prev_total != new_total:
        changes.append(f"Volumen semanal estimado: {prev_total} -> {new_total} min")

    if not changes:
        return "Se registró una nueva versión sin cambios estructurales relevantes."
    return "\n".join(f"- {item}" for item in changes)


def _build_structured_plan_markdown(
    plan: dict,
    sessions: list[dict],
    change_summary: str,
) -> str:
    """Construye respuesta funcional del plan con estructura accionable."""
    title = str(plan.get("title") or "Plan de entrenamiento").strip()
    objective = str(plan.get("objective") or "Objetivo no especificado").strip()
    difficulty = str(plan.get("difficulty") or "moderate").strip()
    duration_weeks = int(plan.get("duration_weeks") or 0)
    plan_id = str(plan.get("id") or "").strip()

    day_names = {
        1: "Lunes", 2: "Martes", 3: "Miércoles", 4: "Jueves", 5: "Viernes", 6: "Sábado", 7: "Domingo"
    }

    lines = [
        "## 🧭 Resumen",
        f"Plan activo: {title}",
        f"Objetivo: {objective}",
        f"Duración: {duration_weeks} semanas · Dificultad: {difficulty}",
    ]

    if plan_id:
        lines.append(f"ID del plan: {plan_id}")

    lines.extend([
        "",
        "## 📅 Semana tipo (estructura)",
    ])

    for s in sorted(sessions, key=lambda x: (int(x.get("week_index") or 1), int(x.get("day_index") or 1))):
        day = day_names.get(int(s.get("day_index") or 0), f"Día {s.get('day_index', '?')}")
        session_type = str(s.get("session_type") or "sesión")
        duration = int(s.get("duration_min") or 0)
        intensity = str(s.get("intensity") or "RPE n/d")
        lines.append(f"- {day}: {session_type} · {duration} min · {intensity}")

    lines.extend([
        "",
        "## 🔄 Cambios de versión",
        change_summary,
        "",
        "## ✅ Próximo paso",
        "Usa `/plan listar` para revisar planes, `/plan ver <plan_id>` para detalle y `/plan activar <plan_id>` para cambiar el activo.",
    ])

    return "\n".join(lines)


def _normalize_trend_date_range(tool_name: str, arguments: dict) -> dict:
    """Ajusta rangos de fechas para herramientas trend según límites MCP."""
    if not isinstance(arguments, dict):
        return {}

    max_days_by_tool = {
        "get_training_load_trend": 90,
        "get_vo2max_trend": 90,
        "get_hrv_trend": 30,
    }
    max_days = max_days_by_tool.get(tool_name)
    if not max_days:
        return arguments

    args = dict(arguments)
    today = date.today()

    start_key = "start_date" if "start_date" in args else "startDate" if "startDate" in args else None
    end_key = "end_date" if "end_date" in args else "endDate" if "endDate" in args else None
    if not start_key and not end_key:
        return args

    def _to_date(value: Any) -> date | None:
        if not isinstance(value, str):
            return None
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None

    end_date = _to_date(args.get(end_key)) if end_key else None
    start_date = _to_date(args.get(start_key)) if start_key else None

    if end_date is None or end_date > today:
        end_date = today
    if start_date is None or start_date > end_date:
        start_date = end_date - timedelta(days=max_days)

    if (end_date - start_date).days > max_days:
        start_date = end_date - timedelta(days=max_days)

    if start_key:
        args[start_key] = start_date.isoformat()
    if end_key:
        args[end_key] = end_date.isoformat()
    return args


# ─── Cliente Gemini (SDK oficial google-genai, soporta claves AQ.) ────────────

def _get_field(obj, key):
    """Accede a un campo tanto si obj es dict como si es objeto."""
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


class _GFnCall:
    __slots__ = ("name", "arguments")

    def __init__(self, name: str, arguments: str):
        self.name = name
        self.arguments = arguments


class _GToolCall:
    __slots__ = ("id", "function", "type")

    def __init__(self, call_id: str, fn: _GFnCall):
        self.id = call_id
        self.function = fn
        self.type = "function"


class _GMessage:
    __slots__ = ("role", "content", "tool_calls")

    def __init__(self, role: str, content, tool_calls=None):
        self.role = role
        self.content = content
        self.tool_calls = tool_calls


class _GChoice:
    __slots__ = ("message", "finish_reason")

    def __init__(self, message: _GMessage, finish_reason: str):
        self.message = message
        self.finish_reason = finish_reason


class _GUsage:
    __slots__ = ("prompt_tokens", "completion_tokens", "total_tokens")

    def __init__(self, prompt_tokens: int, completion_tokens: int, total_tokens: int):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = total_tokens


class _GResponse:
    __slots__ = ("choices", "usage")

    def __init__(self, choices, usage=None):
        self.choices = choices
        self.usage = usage


_GEMINI_SCHEMA_ALLOWED = {"type", "description", "properties", "required", "enum", "items", "nullable", "format"}


def _clean_schema_for_gemini(schema: dict) -> dict:
    """Limpia recursivamente un JSON Schema para que sea compatible con Gemini SDK.
    El SDK solo acepta: type, description, properties, required, enum, items, nullable, format.
    Todo lo demás (exclusiveMinimum, additionalProperties, $schema, etc.) causa ValidationError.
    """
    clean: dict = {}
    for k, v in schema.items():
        if k not in _GEMINI_SCHEMA_ALLOWED:
            continue
        if k == "properties" and isinstance(v, dict):
            clean[k] = {pk: _clean_schema_for_gemini(pv) if isinstance(pv, dict) else pv
                        for pk, pv in v.items()}
        elif k == "items" and isinstance(v, dict):
            clean[k] = _clean_schema_for_gemini(v)
        else:
            clean[k] = v
    return clean


class _GeminiCompletions:
    def __init__(self, api_key: str):
        from google import genai as _g
        from google.genai import types as _t
        self._T = _t
        self._api_key = api_key
        self._client = _g.Client(api_key=api_key)

    async def create(self, *, model, messages, tools=None, tool_choice=None, **_kw):
        T = self._T
        system_instruction = None
        contents = []
        id_to_name: dict[str, str] = {}

        for msg in messages:
            role = _get_field(msg, "role")
            content_text = _get_field(msg, "content") or ""

            if role == "system":
                system_instruction = content_text

            elif role == "user":
                contents.append(T.Content(
                    role="user",
                    parts=[T.Part(text=content_text)]
                ))

            elif role == "tool":
                tc_id = _get_field(msg, "tool_call_id") or ""
                fn_name = id_to_name.get(tc_id, "unknown_tool")
                contents.append(T.Content(
                    role="user",
                    parts=[T.Part(function_response=T.FunctionResponse(
                        name=fn_name,
                        response={"output": content_text},
                    ))]
                ))

            elif role in ("assistant", "model"):
                tcs = _get_field(msg, "tool_calls")
                if tcs:
                    parts = []
                    for tc in tcs:
                        if isinstance(tc, dict):
                            tc_id = tc.get("id", "")
                            fn_d = tc.get("function", {})
                            fn_name = fn_d.get("name", "") if isinstance(fn_d, dict) else getattr(fn_d, "name", "")
                            fn_args_raw = fn_d.get("arguments", "{}") if isinstance(fn_d, dict) else getattr(fn_d, "arguments", "{}")
                        else:
                            tc_id = getattr(tc, "id", "")
                            fn_obj = getattr(tc, "function", None)
                            fn_name = getattr(fn_obj, "name", "") if fn_obj else ""
                            fn_args_raw = getattr(fn_obj, "arguments", "{}") if fn_obj else "{}"
                        try:
                            fn_args = json.loads(fn_args_raw) if isinstance(fn_args_raw, str) else (fn_args_raw or {})
                        except (json.JSONDecodeError, TypeError):
                            fn_args = {}
                        id_to_name[tc_id] = fn_name
                        parts.append(T.Part(function_call=T.FunctionCall(
                            name=fn_name,
                            args=fn_args or {},
                        )))
                    contents.append(T.Content(role="model", parts=parts))
                elif content_text:
                    contents.append(T.Content(
                        role="model",
                        parts=[T.Part(text=content_text)]
                    ))

        cfg_kwargs: dict = {}
        if system_instruction:
            cfg_kwargs["system_instruction"] = system_instruction
        if tools:
            fn_decls = []
            for t in tools:
                fn = t["function"] if isinstance(t, dict) else t
                params = fn.get("parameters") or {"type": "object", "properties": {}}
                params = _clean_schema_for_gemini(params)
                fn_decls.append(T.FunctionDeclaration(
                    name=fn["name"],
                    description=fn.get("description", ""),
                    parameters=params,
                ))
            cfg_kwargs["tools"] = [T.Tool(function_declarations=fn_decls)]
            cfg_kwargs["tool_config"] = T.ToolConfig(
                function_calling_config=T.FunctionCallingConfig(mode="AUTO")
            )

        attempts = 8
        delay = 2.0
        for attempt in range(attempts):
            try:
                response = await self._client.aio.models.generate_content(
                    model=model,
                    contents=contents,
                    config=T.GenerateContentConfig(**cfg_kwargs) if cfg_kwargs else None,
                )
                break
            except Exception as e:
                err_msg = str(e)
                
                # Detectar limite de cuota de la cuenta/API key (RESOURCE_EXHAUSTED / quota exceeded)
                is_quota_exhausted = "RESOURCE_EXHAUSTED" in err_msg or (
                    "quota" in err_msg.lower() and "rate" not in err_msg.lower()
                )
                if is_quota_exhausted:
                    # Guardar que la clave se ha quedado sin cuota hoy para mostrarlo coherentemente al inicio
                    mark_gemini_quota_exhausted(self._api_key)
                    raise Exception(
                        f"La API Key de Gemini ha agotado tu cuota diaria o mensual gratuita (429 RESOURCE_EXHAUSTED).\n"
                        f"Detalle de Google: '{err_msg}'.\n"
                        f"Por favor, revisa tus límites en Google AI Studio (https://aistudio.google.com) o genera otra clave gratuita."
                    ) from e
                
                # 503 (Unavailable) o 429 (Rate limit por RPM) son comunes; reintentar con backoff
                is_transient = "503" in err_msg or "429" in err_msg or "UNAVAILABLE" in err_msg
                if is_transient and attempt < attempts - 1:
                    current_delay = delay
                    if "Please retry in" in err_msg:
                        try:
                            # Intentar extraer los segundos para esperar exactamente lo que pide
                            parts = err_msg.split("Please retry in")
                            sec_str = parts[1].strip().split("s")[0].strip()
                            current_delay = float(sec_str) + 1.0
                        except Exception:
                            pass
                    log.debug(f"Gemini ocupado ({e}). Reintentando en {current_delay:.1f}s...")
                    await asyncio.sleep(current_delay)
                    delay *= 2
                else:
                    raise

        return self._parse(response)

    def _parse(self, response) -> _GResponse:
        candidate = response.candidates[0]
        parts = candidate.content.parts

        fn_calls = [
            p.function_call for p in parts
            if getattr(p, "function_call", None) and getattr(p.function_call, "name", None)
        ]
        if fn_calls:
            tool_calls = [
                _GToolCall(
                    call_id=f"gcall_{i}",
                    fn=_GFnCall(
                        name=fc.name,
                        arguments=json.dumps(dict(fc.args) if fc.args else {}),
                    ),
                )
                for i, fc in enumerate(fn_calls)
            ]
            msg = _GMessage(role="assistant", content=None, tool_calls=tool_calls)
        else:
            text = "".join(getattr(p, "text", "") or "" for p in parts)
            msg = _GMessage(role="assistant", content=text, tool_calls=None)

        usage = None
        if hasattr(response, "usage_metadata") and response.usage_metadata:
            meta = response.usage_metadata
            p_tokens = getattr(meta, "prompt_token_count", 0) or 0
            c_tokens = getattr(meta, "candidates_token_count", 0) or 0
            t_tokens = getattr(meta, "total_token_count", 0) or 0
            usage = _GUsage(prompt_tokens=p_tokens, completion_tokens=c_tokens, total_tokens=t_tokens)

        return _GResponse(choices=[_GChoice(message=msg, finish_reason="stop")], usage=usage)


class _GeminiChat:
    def __init__(self, api_key: str):
        self.completions = _GeminiCompletions(api_key)


class _GeminiClient:
    def __init__(self, api_key: str):
        self.chat = _GeminiChat(api_key)


# Palabras clave de fecha que algunos LLMs envían en lugar de fechas ISO
_TODAY_KEYWORDS = {"hoy", "today", "今日", "今天", "ahora", "now", "current", "actual", "este dia"}
_YESTERDAY_KEYWORDS = {"ayer", "yesterday", "昨日", "昨天"}
_MONTHS_ES = {
    "enero": 1,
    "febrero": 2,
    "marzo": 3,
    "abril": 4,
    "mayo": 5,
    "junio": 6,
    "julio": 7,
    "agosto": 8,
    "septiembre": 9,
    "setiembre": 9,
    "octubre": 10,
    "noviembre": 11,
    "diciembre": 12,
}


def _normalize_date_args(arguments: dict) -> dict:
    """Normaliza parámetros de fecha de las llamadas a herramientas.

    Convierte palabras clave de fecha al formato ISO YYYY-MM-DD que requiere
    la API de Garmin Connect. Previene HTTP 404 cuando el LLM pasa 'hoy',
    'ayer', 'today', etc. como valor de fecha en lugar de la cadena ISO.
    """
    DATE_FIELDS = {"date", "startDate", "endDate", "start_date", "end_date"}
    today = date.today()
    yesterday = today - timedelta(days=1)

    result = {}
    for key, value in arguments.items():
        if key in DATE_FIELDS and isinstance(value, str):
            v_lower = value.strip().lower()
            if v_lower in _TODAY_KEYWORDS:
                result[key] = today.isoformat()
            elif v_lower in _YESTERDAY_KEYWORDS:
                result[key] = yesterday.isoformat()
            else:
                result[key] = value
        else:
            result[key] = value
    return result


def _is_no_data_result(raw_result: str | None) -> bool:
    """Detecta respuestas de herramientas que indican ausencia de datos."""
    if not raw_result:
        return True
    text = raw_result.strip().lower()
    return (
        "no" in text
        and "data" in text
        and ("found" in text or "available" in text)
    )


async def _build_recovery_fallback_snapshot(
    mcp_session: ClientSession,
    preferred_date_iso: str | None,
) -> str | None:
    """Construye un snapshot de recuperación cuando no hay training_readiness.

    Intenta primero la fecha solicitada, y si no hay datos, prueba hoy y ayer.
    """
    dates_to_try: list[str] = []
    if preferred_date_iso:
        dates_to_try.append(preferred_date_iso)
    today_iso = date.today().isoformat()
    yesterday_iso = (date.today() - timedelta(days=1)).isoformat()
    for candidate in (today_iso, yesterday_iso):
        if candidate not in dates_to_try:
            dates_to_try.append(candidate)

    tools = [
        "get_body_battery",
        "get_hrv_data",
        "get_sleep_summary",
        "get_stress_summary",
        "get_rhr_day",
    ]

    snapshot: dict[str, dict] = {}
    for tool_name in tools:
        for date_iso in dates_to_try:
            try:
                args = (
                    {"start_date": date_iso, "end_date": date_iso}
                    if tool_name == "get_body_battery"
                    else {"date": date_iso}
                )
                raw = await call_tool(mcp_session, tool_name, args)
            except Exception:
                continue

            if _is_no_data_result(raw):
                continue

            compact = _compact_tool_result(raw, tool_name)
            try:
                parsed_data = json.loads(compact)
            except (TypeError, json.JSONDecodeError):
                # Algunos endpoints devuelven texto plano; guardarlo también es útil
                # para que el LLM no pierda contexto y evitar romper el flujo.
                parsed_data = {"raw": compact}

            snapshot[tool_name] = {
                "date": date_iso,
                "data": parsed_data,
            }
            break

    if not snapshot:
        return None

    payload = {
        "fallback_reason": "training_readiness_unavailable",
        "summary": "Se usa un snapshot alternativo de recuperación (body battery, HRV, sueño, estrés, RHR).",
        "snapshot": snapshot,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _extract_iso_date_from_text(value: str) -> str | None:
    """Extrae una fecha ISO YYYY-MM-DD desde texto libre en español/inglés."""
    if not isinstance(value, str):
        return None

    text = value.strip().lower()
    if not text:
        return None

    # Coincidencia exacta (el texto completo es la palabra clave)
    if text in _TODAY_KEYWORDS:
        return date.today().isoformat()
    if text in _YESTERDAY_KEYWORDS:
        return (date.today() - timedelta(days=1)).isoformat()

    # Palabra clave como token dentro de un mensaje más largo
    # ej: "Analiza mi actividad de ayer" → "ayer" está en el texto
    _words = set(re.split(r"\W+", text))
    if _words & _TODAY_KEYWORDS:
        return date.today().isoformat()
    if _words & _YESTERDAY_KEYWORDS:
        return (date.today() - timedelta(days=1)).isoformat()

    # yyyy-mm-dd
    m_iso = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", text)
    if m_iso:
        try:
            return date.fromisoformat(m_iso.group(1)).isoformat()
        except ValueError:
            pass

    # dd/mm/yyyy o dd-mm-yyyy
    m_slash = re.search(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b", text)
    if m_slash:
        d, mth, y = int(m_slash.group(1)), int(m_slash.group(2)), int(m_slash.group(3))
        try:
            return date(y, mth, d).isoformat()
        except ValueError:
            pass

    # 2 de julio de 2026 / 2 julio 2026 / 2 de julio
    m_month = re.search(
        r"\b(\d{1,2})\s*(?:de\s+)?([a-záéíóúñ]+)\s*(?:de\s*)?(\d{4})?\b",
        text,
        flags=re.IGNORECASE,
    )
    if m_month:
        d = int(m_month.group(1))
        month_name = (
            m_month.group(2)
            .replace("á", "a")
            .replace("é", "e")
            .replace("í", "i")
            .replace("ó", "o")
            .replace("ú", "u")
        )
        mth = _MONTHS_ES.get(month_name)
        if mth:
            y = int(m_month.group(3)) if m_month.group(3) else date.today().year
            try:
                return date(y, mth, d).isoformat()
            except ValueError:
                return None

    return None


def _extract_activity_date_iso(activity: dict) -> str | None:
    """Obtiene la fecha ISO de una actividad Garmin a partir de sus campos de inicio.
    Soporta:
    - ISO strings: '2026-07-02T08:30:00' o '2026-07-02 08:30:00'
    - Solo fecha: '2026-07-02'
    - Epoch en milisegundos (int o string): 1751414400000
    - Epoch en segundos (int o string): 1751414400
    """
    if not isinstance(activity, dict):
        return None

    for key in ("startTimeLocal", "startTimeGMT", "startTimeUTC", "startTime", "start_time",
                "calendarDate", "beginTimestamp", "activitySummary"):
        value = activity.get(key)
        if value is None:
            continue

        # String con fecha ISO o similar
        if isinstance(value, str):
            s = value.strip()
            if len(s) >= 10:
                date_str = s[:10].replace(" ", "-")  # '2026 07 02' -> '2026-07-02'
                try:
                    return date.fromisoformat(date_str).isoformat()
                except ValueError:
                    pass
            # Epoch como string
            if s.isdigit() and len(s) >= 10:
                try:
                    ts = int(s)
                    if ts > 10_000_000_000:   # milisegundos
                        ts //= 1000
                    return datetime.utcfromtimestamp(ts).date().isoformat()
                except (ValueError, OSError, OverflowError):
                    pass

        # Epoch numérico
        if isinstance(value, (int, float)) and value > 0:
            try:
                ts = int(value)
                if ts > 10_000_000_000:   # milisegundos
                    ts //= 1000
                return datetime.utcfromtimestamp(ts).date().isoformat()
            except (ValueError, OSError, OverflowError):
                pass

    return None


def _parse_activities_response(raw: str | None) -> tuple[list[dict], bool, int]:
    """Parsea la respuesta de get_activities en (activities, has_more, next_start).
    Soporta tanto array JSON directo como objeto {activities: [...]}
    """
    if not raw or not raw.strip():
        return [], False, 0
    stripped = raw.strip()
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        return [], False, 0

    # Formato lista directa: [{...}, {...}]
    if isinstance(data, list):
        activities = [a for a in data if isinstance(a, dict)]
        log.debug(f"get_activities -> lista directa con {len(activities)} actividades")
        return activities, False, 0

    # Formato objeto: {"activities": [...], "has_more": ..., "next_start": ...}
    if isinstance(data, dict):
        # Algunos servidores MCP devuelven las actividades bajo distintas claves
        activities = data.get("activities") or data.get("activityList") or data.get("list") or []
        if not isinstance(activities, list):
            activities = []
        activities = [a for a in activities if isinstance(a, dict)]
        has_more = bool(data.get("has_more") or data.get("hasMore"))
        next_start = int(data.get("next_start") or data.get("nextStart") or 0)
        log.debug(f"get_activities -> objeto con {len(activities)} actividades, has_more={has_more}")
        return activities, has_more, next_start

    return [], False, 0


async def _find_activity_id_by_date(mcp_session: ClientSession, target_date_iso: str) -> int | None:
    """Busca en actividades recientes el activity_id correspondiente a una fecha ISO."""
    start = 0
    limit = 100
    max_pages = 30  # hasta 3000 actividades para cubrir historiales amplios

    for page_num in range(max_pages):
        raw = await call_tool(mcp_session, "get_activities", {"start": str(start), "limit": str(limit)})
        activities, has_more, next_start = _parse_activities_response(raw)

        if page_num == 0 and activities:
            sample = activities[0]
            # Debug exhaustivo: muestra TODOS los keys y los valores de fecha para diagnóstico
            all_keys = list(sample.keys())
            log.debug(f"Primera actividad keys: {all_keys}")
            date_fields = {k: sample.get(k) for k in all_keys if any(x in k.lower() for x in ("time", "date", "start", "timestamp", "calendar"))}
            act_id_debug = sample.get("activityId") or sample.get("id") or sample.get("activity_id")
            log.debug(f"activityId={act_id_debug} campos_fecha={date_fields}")

        for activity in activities:
            act_date = _extract_activity_date_iso(activity)
            act_id = activity.get("activityId") or activity.get("activity_id") or activity.get("id")
            if act_date:
                log.debug(f"Comparando actividad {act_id}: fecha_extraida={act_date} vs target={target_date_iso}")
            if act_date != target_date_iso:
                continue
            activity_id = activity.get("activityId") or activity.get("activity_id") or activity.get("id")
            try:
                return int(activity_id)
            except (TypeError, ValueError):
                continue

        if not has_more:
            break
        new_start = next_start if next_start > start else start + limit
        if new_start <= start:  # paginación rota: el servidor no avanza
            break
        start = new_start

    return None


async def _find_activity_id_by_name(mcp_session: ClientSession, name_hint: str) -> int | None:
    """Busca activity_id por nombre aproximado en actividades recientes."""
    hint = (name_hint or "").strip().lower()
    if not hint:
        return None

    stop_tokens = {
        "analiza", "analizar", "mi", "mis", "del", "de", "la", "el", "los", "las",
        "por", "para", "con", "una", "uno", "competicion", "competición", "actividad",
        "carrera", "quiero", "que", "hice", "hacer", "sobre",
    }
    hint_tokens = [t for t in _tokenize_for_kb(hint) if t not in stop_tokens]

    start = 0
    limit = 100
    max_pages = 30  # hasta 3000 actividades para cubrir historiales amplios

    for _ in range(max_pages):
        raw = await call_tool(mcp_session, "get_activities", {"start": str(start), "limit": str(limit)})
        activities, has_more, next_start_val = _parse_activities_response(raw)

        best_id = None
        best_score = -1
        for activity in activities:
            if not isinstance(activity, dict):
                continue
            name = str(activity.get("name", "")).strip().lower()
            if not name:
                continue

            score = -1
            # Coincidencias más fuertes primero
            if name == hint:
                score = 100
            elif name.startswith(hint):
                score = 90
            elif hint in name:
                score = 80
            elif all(tok in name for tok in hint.split() if tok):
                score = 70
            else:
                # Fallback robusto para texto libre del usuario:
                # puntuar por solape de tokens relevantes (ignorando ruido).
                if hint_tokens:
                    overlap = sum(1 for tok in hint_tokens if tok in name)
                    if overlap >= 2:
                        score = 60 + min(overlap * 5, 20)

            if score > best_score:
                activity_id = activity.get("activityId") or activity.get("activity_id") or activity.get("id")
                try:
                    best_id = int(activity_id)
                    best_score = score
                except (TypeError, ValueError):
                    continue

        if best_id is not None and best_score >= 70:
            return best_id

        if not has_more:
            break
        start = next_start_val if next_start_val > start else start + limit

    return None


def _find_hr_zones_in_json(data: Any) -> list[dict] | None:
    """Busca recursivamente datos de zonas de FC en cualquier nivel del JSON.
    
    Detecta arrays con objetos que tengan secsInZone > 0 y zoneNumber.
    Cubre el caso donde Garmin devuelve los datos en campos anidados.
    """
    if isinstance(data, list):
        # Comprobar si esta lista ES la lista de zonas
        zone_like = [
            x for x in data
            if isinstance(x, dict) and (
                x.get("zoneNumber") is not None or x.get("zone_number") is not None
            )
        ]
        if zone_like and len(zone_like) >= 3:
            return zone_like
        # Buscar en los elementos de la lista
        for item in data:
            result = _find_hr_zones_in_json(item)
            if result:
                return result

    elif isinstance(data, dict):
        # Revisar primero las claves más probables
        for key in (
            "heartRateTimeInZone", "heartRateZones", "hrTimeInZones",
            "timeInHeartRateZones", "heartRateTimeInZones", "hrZones",
            "zones", "hr_zones", "timeInZone", "timeInZones",
        ):
            val = data.get(key)
            if isinstance(val, list) and len(val) >= 3:
                result = _find_hr_zones_in_json(val)
                if result:
                    return result
        # Búsqueda en profundidad en todos los valores
        for val in data.values():
            if isinstance(val, (dict, list)):
                result = _find_hr_zones_in_json(val)
                if result:
                    return result

    return None


def _parse_hr_zones_list(raw: str | None) -> list[dict] | None:
    """Parsea la respuesta de get_activity_hr_zones en una lista normalizada de zonas."""
    if not raw or not raw.strip():
        return None
    stripped = raw.strip()
    if stripped in ("null", "[]", "{}", "(sin datos)"):
        return None
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        return None

    # Búsqueda recursiva: encontrar la lista de zonas dondequiera que esté
    zones_raw = _find_hr_zones_in_json(data)
    if not zones_raw:
        return None

    # Normalizar cada zona a un dict homogéneo
    normalized: list[dict] = []
    for z in zones_raw:
        if not isinstance(z, dict):
            continue

        zone_num = (z.get("zoneNumber") or z.get("zone_number")
                    or z.get("zone") or z.get("zoneNum") or 0)
        try:
            zone_num = int(zone_num)
        except (TypeError, ValueError):
            zone_num = 0

        # Tiempo en zona (segundos) — varios nombres posibles
        secs = (z.get("secsInZone") or z.get("secs_in_zone")
                or z.get("timeInZone") or z.get("time_in_zone")
                or z.get("seconds") or z.get("durationSeconds") or 0)
        try:
            secs = float(secs)
        except (TypeError, ValueError):
            secs = 0.0

        # Porcentaje directo (cuando no hay segundos disponibles)
        pct_direct = z.get("percentInZone") or z.get("percent_in_zone") or z.get("percentage")
        try:
            pct_direct = float(pct_direct) if pct_direct is not None else None
        except (TypeError, ValueError):
            pct_direct = None

        # Límites de FC de la zona — zoneLow/zoneHigh o minHeartRateIn/maxHeartRateIn
        lo = (z.get("minHeartRateIn") or z.get("min_heart_rate_in")
              or z.get("zoneLow") or z.get("zone_low")
              or z.get("zoneLowBoundary") or z.get("zone_low_boundary")
              or z.get("minHr") or "?")
        hi = (z.get("maxHeartRateIn") or z.get("max_heart_rate_in")
              or z.get("zoneHigh") or z.get("zone_high") or z.get("maxHr") or "?")

        zone_name = (z.get("zoneName") or z.get("zone_name")
                     or z.get("name") or f"Z{zone_num}")

        if secs > 0 or pct_direct is not None:
            normalized.append({
                "zoneNumber": zone_num,
                "secsInZone": secs,
                "pctDirect": pct_direct,  # porcentaje directo si está disponible
                "minHeartRateIn": lo,
                "maxHeartRateIn": hi,
                "zoneName": zone_name,
            })

    return normalized if normalized else None


def _build_activity_analysis_block(
    activity_raw: str,
    body_battery_raw: str | None = None,
    sleep_raw: str | None = None,
    hrv_raw: str | None = None,
    training_load_raw: str | None = None,
    ftp: float | None = None,
    hr_zones_raw: str | None = None,
) -> str:
    """Construye un bloque de análisis pre-computado en Python para inyectar al LLM.

    Calcula métricas derivadas (ritmo, zonas FC, hidratación, carga) directamente
    en Python para que el LLM solo aporte interpretación y coaching, no cálculos.
    """
    lines: list[str] = []

    # ── Parsear actividad ──────────────────────────────────────────────────
    try:
        act = json.loads(activity_raw) if activity_raw else {}
    except Exception:
        act = {}

    name     = act.get("name") or act.get("activityName") or "Actividad"
    act_type = act.get("type") or act.get("activityType") or ""
    dur_s_raw = (act.get("duration") or act.get("duration_seconds")
                 or act.get("movingDuration") or act.get("moving_duration_seconds"))
    dist_m_raw = act.get("distance") or act.get("distance_meters")
    avg_hr   = act.get("avgHr") or act.get("avg_hr_bpm") or act.get("averageHR")
    max_hr   = act.get("maxHr") or act.get("max_hr_bpm") or act.get("maxHR")
    min_hr   = act.get("minHr") or act.get("min_hr_bpm") or act.get("minHR")
    calories = act.get("calories") or act.get("activeKilocalories") or act.get("activeCalories")
    elev_gain = act.get("elevationGain") or act.get("elevation_gain_meters") or act.get("totalAscent")
    elev_loss = act.get("elevationLoss") or act.get("elevation_loss_meters") or act.get("totalDescent")
    train_effect = act.get("trainingEffect") or act.get("aerobicTrainingEffect")
    train_load   = act.get("activityTrainingLoad") or act.get("trainingLoadScore")

    # ── Conversiones base ─────────────────────────────────────────────────
    try:
        dur_s = float(dur_s_raw) if dur_s_raw is not None else None
    except (ValueError, TypeError):
        dur_s = None
    try:
        dist_km = float(dist_m_raw) / 1000 if dist_m_raw is not None else None
    except (ValueError, TypeError):
        dist_km = None
    try:
        avg_hr_f = float(avg_hr) if avg_hr is not None else None
        max_hr_f = float(max_hr) if max_hr is not None else None
    except (ValueError, TypeError):
        avg_hr_f = max_hr_f = None

    # ── Sección 1: Resumen básico ──────────────────────────────────────────
    lines.append("=== RESUMEN DE ACTIVIDAD (calculado) ===")
    lines.append(f"Nombre: {name}")
    if act_type:
        lines.append(f"Tipo: {act_type}")
    if dur_s:
        lines.append(f"Duracion: {_seconds_to_hhmmss(dur_s)}")
        lines.append(f"Duracion total: {dur_s:.0f} segundos ({dur_s/3600:.2f} horas)")
    if dist_km:
        lines.append(f"Distancia: {dist_km:.2f} km")
    if dur_s and dist_km and dist_km > 0:
        if _is_cycling_activity(act_type):
            speed_kmh = dist_km / (dur_s / 3600)
            lines.append(f"Velocidad media: {speed_kmh:.1f} km/h")
            # Velocidad máxima (Garmin devuelve avgSpeed/maxSpeed en m/s)
            max_spd_raw = act.get("maxSpeed") or act.get("max_speed_ms")
            if max_spd_raw is not None:
                try:
                    lines.append(f"Velocidad maxima: {float(max_spd_raw) * 3.6:.1f} km/h")
                except (ValueError, TypeError):
                    pass
        else:
            pace_s = dur_s / dist_km
            lines.append(f"Ritmo medio: {int(pace_s//60)}:{int(pace_s%60):02d} min/km")
    if avg_hr_f:
        lines.append(f"FC media: {avg_hr_f:.0f} bpm")
    if max_hr_f:
        lines.append(f"FC maxima: {max_hr_f:.0f} bpm")
    if min_hr is not None:
        lines.append(f"FC minima: {min_hr} bpm")
    if elev_gain:
        lines.append(f"Desnivel positivo: {float(elev_gain):.0f} m")
    if elev_loss:
        lines.append(f"Desnivel negativo: {float(elev_loss):.0f} m")
    if calories:
        lines.append(f"Calorias: {float(calories):.0f} kcal")
    # TSS o hrTSS calculado para este entrenamiento
    _tss_val, _tss_lbl = _estimate_session_tss(act, ftp=ftp)
    if _tss_val > 0:
        lines.append(f"{_tss_lbl}: {_tss_val:.1f}")

    # ── Sección 2: Zonas de FC ─────────────────────────────────────────────
    # Prioridad 1: datos reales del dispositivo (get_activity_hr_zones)
    # Prioridad 2: estimación gaussiana (fallback, solo cuando no hay datos reales)
    _zones_shown = False
    _zones_parsed = _parse_hr_zones_list(hr_zones_raw)
    if _zones_parsed:
        # Calcular total de segundos para los porcentajes
        _total_secs = sum(float(z.get("secsInZone") or 0) for z in _zones_parsed)
        # Si no hay segundos pero hay porcentajes directos, usarlos
        _has_pct_direct = all(z.get("pctDirect") is not None for z in _zones_parsed)
        if _total_secs > 0 or _has_pct_direct:
            lines.append("")
            lines.append("=== ZONAS DE FRECUENCIA CARDIACA (datos reales Garmin — Tiempo en Zonas) ===")
            if avg_hr_f and max_hr_f:
                lines.append(f"FCmax: {max_hr_f:.0f} bpm | FC media: {avg_hr_f:.0f} bpm")
            for z in sorted(_zones_parsed, key=lambda x: int(x.get("zoneNumber") or 0)):
                _z_secs = float(z.get("secsInZone") or 0)
                _z_pct_d = z.get("pctDirect")
                if _total_secs > 0:
                    _z_pct = _z_secs / _total_secs * 100
                elif _z_pct_d is not None:
                    _z_pct = float(_z_pct_d)
                    _z_secs = (_z_pct / 100.0 * (dur_s or 0))
                else:
                    continue
                _z_mins = _z_secs / 60
                _z_num = int(z.get("zoneNumber") or 0)
                _z_lo = z.get("minHeartRateIn") or "?"
                _z_hi = z.get("maxHeartRateIn") or "?"
                _z_name = z.get("zoneName") or f"Z{_z_num}"
                _hr_range = f"{_z_lo}–{_z_hi} bpm" if _z_lo != "?" and _z_hi != "?" else ""
                _suffix = f" ({_hr_range})" if _hr_range else ""
                lines.append(f"  {_z_name} (Z{_z_num}){_suffix}: {_z_pct:.1f}%  (~{_z_mins:.0f} min)")
            _zones_shown = True

    if not _zones_shown and avg_hr_f and max_hr_f and dur_s:
        lines.append("")
        lines.append("=== ZONAS DE FRECUENCIA CARDIACA (estimacion gaussiana — aproximada) ===")
        lines.append(f"AVISO: sin datos reales de zonas. Estimación basada en FC media y FCmax, puede diferir de las zonas reales configuradas en Garmin.")
        lines.append(f"FCmax observada: {max_hr_f:.0f} bpm | FC media: {avg_hr_f:.0f} bpm")
        sigma = 0.10 * max_hr_f
        zone_defs = [
            ("Z1 Recuperacion     (<60% FC)", 0.00 * max_hr_f, 0.60 * max_hr_f),
            ("Z2 Base aerobica (60-70% FC)",  0.60 * max_hr_f, 0.70 * max_hr_f),
            ("Z3 Umbral aerobico (70-80%FC)", 0.70 * max_hr_f, 0.80 * max_hr_f),
            ("Z4 Umbral anaer.  (80-90% FC)", 0.80 * max_hr_f, 0.90 * max_hr_f),
            ("Z5 VO2max          (>90% FC)",  0.90 * max_hr_f, 2.00 * max_hr_f),
        ]
        def ncdf(x, mu, s):
            return 0.5 * (1 + math.erf((x - mu) / (s * math.sqrt(2))))
        raw_pcts = []
        for _, lo, hi in zone_defs:
            p = ncdf(hi, avg_hr_f, sigma) - ncdf(lo, avg_hr_f, sigma)
            raw_pcts.append(max(p, 0))
        total_p = sum(raw_pcts) or 1.0
        for i, (zname, _, _) in enumerate(zone_defs):
            pct = raw_pcts[i] / total_p * 100
            mins = dur_s * raw_pcts[i] / total_p / 60
            lines.append(f"  {zname}: {pct:.1f}%  (~{mins:.0f} min)")

    # ── Sección 3: Efecto de entrenamiento y carga ────────────────────────
    # Extraer también efecto anaeróbico y label para formatearlos en Python
    anaer_effect  = act.get("anaerobicTrainingEffect") or act.get("anaerobic_training_effect")
    effect_label  = (act.get("activityTrainingEffectLabel") or act.get("trainingEffectLabel")
                     or act.get("training_effect_label"))
    _effect_labels_es = {
        "AEROBIC_BASE":   "construccion base aerobica",
        "RECOVERY":       "recuperacion",
        "TEMPO":          "mejora de ritmo/tempo",
        "THRESHOLD":      "trabajo de umbral",
        "OVERSTRESSING":  "sobrecarga (excesivo)",
        "NO_EFFECT":      "sin efecto significativo",
    }

    if train_effect or train_load:
        lines.append("")
        lines.append("=== CARGA Y EFECTO DE ENTRENAMIENTO ===")
        te_labels = {1: "recuperacion", 2: "mantenimiento", 3: "mejora", 4: "alto impacto", 5: "sobreextension/pico"}
        if train_effect is not None:
            te = float(train_effect)
            label = te_labels.get(min(int(te), 5), "")
            lines.append(f"Training Effect aerobico: {te:.1f}/5.0 ({label})")
        if anaer_effect is not None:
            lines.append(f"Training Effect anaerobico: {float(anaer_effect):.1f}/5.0")
        if effect_label:
            friendly = _effect_labels_es.get(str(effect_label), str(effect_label).replace("_", " ").lower())
            lines.append(f"Tipo de entrenamiento: {friendly}")
        if train_load is not None:
            lines.append(f"Carga de entrenamiento: {float(train_load):.1f}")
            tl = float(train_load)
            if tl > 300:
                lines.append("  -> Carga MUY ALTA (>300): tipica de ultras o sesiones de maximo esfuerzo")
            elif tl > 150:
                lines.append("  -> Carga ALTA (150-300): sesion exigente, requiere varios dias de recuperacion")
            else:
                lines.append("  -> Carga moderada")

    # ── Sección 4: Hidratación estimada ───────────────────────────────────
    if dur_s:
        lines.append("")
        lines.append("=== HIDRATACION ESTIMADA ===")
        dur_h = dur_s / 3600
        low  = round(dur_h * 0.5, 1)
        high = round(dur_h * 0.8, 1)
        hot  = round(dur_h * 1.0, 1)
        lines.append(f"Duracion {dur_h:.1f}h -> minimo {low}-{high}L (condiciones normales)")
        lines.append(f"Con calor/altitud -> hasta {hot}L")
        if dist_km and dist_km > 30:
            lines.append("  -> Ultra: añadir electrolitos cada 45-60 min ademas de agua")

    # ── Sección 5: Recuperacion pre-actividad ────────────────────────────
    if body_battery_raw and body_battery_raw != "(sin datos)":
        # El body_battery_raw viene como "BODY BATTERY del YYYY-MM-DD:\n[json]"
        # Parsear el JSON y formatear los campos útiles
        try:
            bb_json_str = body_battery_raw.split("\n", 1)[1].strip() if "\n" in body_battery_raw else body_battery_raw
            bb_data_list = json.loads(bb_json_str)
            if isinstance(bb_data_list, list) and bb_data_list:
                bb = bb_data_list[0]
            elif isinstance(bb_data_list, dict):
                bb = bb_data_list
            else:
                bb = {}
            charged = bb.get("charged") or bb.get("bodyBatteryCharged")
            drained  = bb.get("drained") or bb.get("bodyBatteryDrained")
            highest  = bb.get("highestBodyBattery") or bb.get("highest")
            lowest   = bb.get("lowestBodyBattery") or bb.get("lowest")
            lines.append("")
            lines.append("=== BODY BATTERY (dia de la actividad) ===")
            if highest is not None and lowest is not None:
                lines.append(f"Maximo del dia: {int(highest)} | Minimo del dia: {int(lowest)}")
            if charged is not None:
                lines.append(f"Recargado: +{int(charged)} puntos")
            if drained is not None:
                lines.append(f"Drenado: -{int(drained)} puntos")
            if charged is not None and drained is not None:
                net = int(charged) - int(drained)
                lines.append(f"Balance neto: {net:+d} puntos {'(deficit esperado en una ultra)' if net < -30 else ''}")
        except Exception:
            lines.append("")
            lines.append("=== BODY BATTERY (dia de la actividad) ===")
            lines.append(body_battery_raw[:200])

    if sleep_raw and sleep_raw != "(sin datos)":
        # El sleep_raw viene como "SUENO noche previa (YYYY-MM-DD):\n{json}"
        # Parsear dailySleepDTO y mostrar solo métricas útiles
        try:
            sleep_json_str = sleep_raw.split("\n", 1)[1].strip() if "\n" in sleep_raw else sleep_raw
            sd = json.loads(sleep_json_str)
            dto = sd.get("dailySleepDTO") or sd if isinstance(sd, dict) else {}
            sleep_secs  = dto.get("sleepTimeSeconds", 0)
            deep_secs   = dto.get("deepSleepSeconds", 0)
            light_secs  = dto.get("lightSleepSeconds", 0)
            rem_secs    = dto.get("remSleepSeconds", 0)
            wake_secs   = dto.get("wakeSeconds", 0)
            score       = dto.get("sleepScore")
            quality_map = {1: "Pobre", 2: "Regular", 3: "Buena", 4: "Excelente"}
            quality_num = dto.get("sleepQuality")
            quality_str = quality_map.get(int(quality_num), str(quality_num)) if quality_num else None
            def fmt_mins(s):
                h, m = int(s) // 3600, (int(s) % 3600) // 60
                return f"{h}h {m:02d}min" if h else f"{m}min"
            lines.append("")
            lines.append("=== SUENO NOCHE PREVIA ===")
            lines.append(f"Duracion total: {fmt_mins(sleep_secs)}")
            if deep_secs:
                lines.append(f"Sueno profundo: {fmt_mins(deep_secs)}")
            if light_secs:
                lines.append(f"Sueno ligero: {fmt_mins(light_secs)}")
            if rem_secs:
                lines.append(f"REM: {fmt_mins(rem_secs)}")
            if wake_secs:
                lines.append(f"Despertares: {fmt_mins(wake_secs)}")
            if score:
                lines.append(f"Puntuacion Garmin: {score}/100")
            if quality_str:
                lines.append(f"Calidad: {quality_str}")
        except Exception:
            lines.append("")
            lines.append("=== SUENO NOCHE PREVIA ===")
            lines.append(sleep_raw[:300])

    if hrv_raw and hrv_raw != "(sin datos)":
        try:
            hrv_json_str = hrv_raw.split("\n", 1)[1].strip() if "\n" in hrv_raw else hrv_raw
            hd = json.loads(hrv_json_str)
            if isinstance(hd, dict):
                avg_hrv  = hd.get("last_night_avg_hrv_ms") or hd.get("avgHrv") or hd.get("averageHrv")
                high_hrv = hd.get("last_night_5min_high_hrv_ms") or hd.get("highHrv")
                lines.append("")
                lines.append("=== HRV DIA ACTIVIDAD ===")
                if avg_hrv:
                    lines.append(f"HRV promedio noche: {avg_hrv} ms")
                if high_hrv:
                    lines.append(f"HRV maximo 5min: {high_hrv} ms")
        except Exception:
            lines.append("")
            lines.append("=== HRV DIA ACTIVIDAD ===")
            lines.append(hrv_raw[:200])

    # ── Recuperacion recomendada post-ultra ──────────────────────────────
    if train_load is not None or (dur_s and dur_s > 10800):
        lines.append("")
        lines.append("=== RECUPERACION RECOMENDADA ===")
        tl_val = float(train_load) if train_load is not None else 0
        dur_h2 = (dur_s or 0) / 3600
        if tl_val > 300 or dur_h2 > 8:
            lines.append("Carga extrema (ultra/maratón+): 10-14 días sin impacto, 3-4 semanas hasta intensidad")
        elif tl_val > 150 or dur_h2 > 3:
            lines.append("Carga alta: 3-5 días recuperacion activa, evitar intensidad 1 semana")
        else:
            lines.append("Carga media: 1-2 días recuperacion, retomar progresivamente")

    return "\n".join(lines)


async def _normalize_get_activity_args(
    mcp_session: ClientSession,
    arguments: dict,
    user_message: str | None = None,
) -> dict:
    """Normaliza argumentos de get_activity.

    Acepta activity_id numérico o fechas en lenguaje natural/ISO y resuelve el
    ID automáticamente consultando get_activities cuando sea necesario.
    """
    if not isinstance(arguments, dict):
        arguments = {}

    args = dict(arguments)
    candidate = args.get("activity_id")
    if candidate is None:
        candidate = args.get("activityId")
    if candidate is None:
        candidate = args.get("id")
    if candidate is None:
        candidate = args.get("date")
    if candidate is None and isinstance(user_message, str) and user_message.strip():
        candidate = user_message.strip()

    # ID ya numérico
    if isinstance(candidate, (int, float)):
        return {"activity_id": int(candidate)}
    if isinstance(candidate, str) and candidate.strip().isdigit():
        return {"activity_id": int(candidate.strip())}

    # Intentar resolver fecha -> activity_id
    if isinstance(candidate, str):
        target_date = _extract_iso_date_from_text(candidate)
        if target_date:
            resolved_id = await _find_activity_id_by_date(mcp_session, target_date)
            if resolved_id is not None:
                return {"activity_id": resolved_id}
            # Si el usuario pidió una fecha concreta y no hay actividad ese día,
            # no caer a matching por nombre para evitar seleccionar otro entrenamiento.
            return {}
        # Si no es fecha, intentar resolver por nombre de actividad
        resolved_id = await _find_activity_id_by_name(mcp_session, candidate)
        if resolved_id is not None:
            return {"activity_id": resolved_id}

    # Fallback: mantener nombre esperado por la tool solo para valores numéricos.
    # Evita enviar texto libre al backend (fallo: invalid literal for int()).
    if "activity_id" in args:
        v = args["activity_id"]
        if isinstance(v, (int, float)) or (isinstance(v, str) and v.strip().isdigit()):
            return {"activity_id": int(v)}
        return {}
    if "activityId" in args:
        v = args["activityId"]
        if isinstance(v, (int, float)) or (isinstance(v, str) and v.strip().isdigit()):
            return {"activity_id": int(v)}
        return {}
    if "id" in args:
        v = args["id"]
        if isinstance(v, (int, float)) or (isinstance(v, str) and v.strip().isdigit()):
            return {"activity_id": int(v)}
        return {}
    return {}


async def _resolve_activity_id_from_query(mcp_session: ClientSession, user_message: str) -> int | None:
    """Resuelve un activity_id directamente desde la consulta del usuario."""
    if not isinstance(user_message, str) or not user_message.strip():
        return None

    target_date = _extract_iso_date_from_text(user_message)
    if target_date:
        by_date = await _find_activity_id_by_date(mcp_session, target_date)
        if by_date is not None:
            return by_date
        return None

    by_name = await _find_activity_id_by_name(mcp_session, user_message)
    return by_name


async def _build_activity_candidates_payload(mcp_session: ClientSession, user_message: str) -> str:
    """Devuelve candidatos de actividades para ayudar al modelo a recuperar activity_id."""
    target_date = _extract_iso_date_from_text(user_message) if isinstance(user_message, str) else None
    collected: list[dict] = []
    start = 0
    limit = 100
    max_pages = 20
    try:
        for _ in range(max_pages):
            raw = await call_tool(mcp_session, "get_activities", {"start": str(start), "limit": str(limit)})
            page_activities, has_more, next_start_val = _parse_activities_response(raw)
            collected.extend(page_activities)
            if not has_more:
                break
            start = next_start_val if next_start_val > start else start + limit
    except Exception as exc:
        payload = {
            "error": "missing_activity_id",
            "message": "No se pudo recuperar listado de actividades para resolver activity_id.",
            "detail": str(exc),
        }
        return json.dumps(payload, ensure_ascii=False)

    activities = collected
    if target_date:
        date_matches = [a for a in activities if _extract_activity_date_iso(a) == target_date]
        if not date_matches:
            # No hay actividad en esa fecha exacta: informar claramente sin mostrar otras fechas
            payload = {
                "error": "no_activity_on_date",
                "target_date": target_date,
                "message": (
                    f"No se encontró ninguna actividad registrada el {target_date} en Garmin Connect. "
                    "Informa al usuario que no hay actividad para esa fecha y pregúntale si quiere "
                    "ver las actividades más recientes disponibles."
                ),
            }
            return json.dumps(payload, ensure_ascii=False)
        activities = date_matches

    compact_candidates = []
    for activity in activities[:20]:
        if not isinstance(activity, dict):
            continue
        activity_id = activity.get("activityId") or activity.get("activity_id") or activity.get("id")
        try:
            activity_id = int(activity_id)
        except (TypeError, ValueError):
            continue
        compact_candidates.append(
            {
                "activity_id": activity_id,
                "date": _extract_activity_date_iso(activity) or "",
                "name": str(activity.get("name") or activity.get("activityName") or "Actividad").strip(),
            }
        )

    payload = {
        "error": "missing_activity_id",
        "query": user_message,
        "target_date": target_date,
        "hint": "Selecciona una actividad de la lista y vuelve a llamar get_activity con activity_id.",
        "candidates": compact_candidates,
    }
    return json.dumps(payload, ensure_ascii=False)


# ─── Herramientas internas Kairos (kairos_*) ─────────────────────────────────
# Estas tools operan sobre datos ya almacenados en el perfil (load_metrics.series)
# o sobre actividades Garmin MCP, y se procesan en Python puro sin llamar al LLM.

def _kairos_load_trends(profile: dict, metric: str, weeks_back: int = 8) -> str:
    """Devuelve la serie temporal de una métrica de carga/fatiga desde el perfil."""
    valid = {"tss", "atl", "ctl", "tsb"}
    metric = str(metric or "tsb").strip().lower()
    if metric not in valid:
        return json.dumps({"error": f"Métrica '{metric}' no válida. Opciones: {sorted(valid)}"}, ensure_ascii=False)
    series = (profile.get("load_metrics") or {}).get("series") or []
    if not series:
        return json.dumps({"error": "Sin datos históricos de carga/fatiga. Ejecuta una sesión para que el sistema los calcule.", "n": 0}, ensure_ascii=False)
    weeks_back = max(1, min(int(weeks_back), 52))
    cutoff = (date.today() - timedelta(days=weeks_back * 7)).isoformat()
    filtered = [r for r in series if str(r.get("date") or "") >= cutoff]
    if not filtered:
        return json.dumps({"error": f"Sin datos en las últimas {weeks_back} semanas.", "n": 0}, ensure_ascii=False)
    points = [{"date": r["date"], "value": round(float(r.get(metric, 0.0)), 1)} for r in filtered if r.get("date")]
    today = date.today()
    # Semanas naturales lunes→domingo (no ventanas deslizantes)
    _week_mon = today - timedelta(days=today.weekday())  # lunes de esta semana
    weekly = []
    for w in range(weeks_back - 1, -1, -1):
        mon = _week_mon - timedelta(weeks=w)
        sun = mon + timedelta(days=6)
        wpts = [p for p in points if mon.isoformat() <= p["date"] <= sun.isoformat()]
        if wpts:
            agg = round(sum(p["value"] for p in wpts), 1) if metric == "tss" else round(wpts[-1]["value"], 1)
            weekly.append({"week": f"{mon.strftime('%d/%m')}–{sun.strftime('%d/%m')}", "value": agg})
    return json.dumps({
        "metric": metric, "n_days": len(points), "weeks_back": weeks_back,
        "latest": points[-1] if points else None,
        "daily": points[-14:], "weekly": weekly,
        "nota": "Fuente: series TSS/ATL/CTL/TSB calculadas desde actividades Garmin y almacenadas en perfil.",
    }, ensure_ascii=False, separators=(",", ":"))


def _kairos_correlate(profile: dict, metric_a: str, metric_b: str, weeks_back: int = 8) -> str:
    """Calcula la correlación de Pearson entre dos métricas de carga/fatiga."""
    valid = {"tss", "atl", "ctl", "tsb"}
    metric_a = str(metric_a or "tss").strip().lower()
    metric_b = str(metric_b or "tsb").strip().lower()
    if metric_a not in valid:
        return json.dumps({"error": f"Métrica A '{metric_a}' no válida."}, ensure_ascii=False)
    if metric_b not in valid:
        return json.dumps({"error": f"Métrica B '{metric_b}' no válida."}, ensure_ascii=False)
    if metric_a == metric_b:
        return json.dumps({"error": "Las dos métricas deben ser distintas."}, ensure_ascii=False)
    series = (profile.get("load_metrics") or {}).get("series") or []
    if not series:
        return json.dumps({"error": "Sin datos históricos de carga/fatiga.", "n": 0}, ensure_ascii=False)
    weeks_back = max(2, min(int(weeks_back), 52))
    cutoff = (date.today() - timedelta(days=weeks_back * 7)).isoformat()
    filtered = [r for r in series if str(r.get("date") or "") >= cutoff]
    n = len(filtered)
    if n < 7:
        return json.dumps({"error": f"Datos insuficientes ({n} días). Necesitas ≥7 días de historial.", "n": n}, ensure_ascii=False)
    vals_a = [float(r.get(metric_a, 0.0)) for r in filtered]
    vals_b = [float(r.get(metric_b, 0.0)) for r in filtered]
    mean_a = sum(vals_a) / n
    mean_b = sum(vals_b) / n
    num = sum((a - mean_a) * (b - mean_b) for a, b in zip(vals_a, vals_b))
    denom_a = (sum((a - mean_a) ** 2 for a in vals_a)) ** 0.5
    denom_b = (sum((b - mean_b) ** 2 for b in vals_b)) ** 0.5
    if denom_a < 1e-9 or denom_b < 1e-9:
        return json.dumps({"error": "Una métrica no tiene variación suficiente.", "n": n}, ensure_ascii=False)
    r = max(-1.0, min(1.0, num / (denom_a * denom_b)))
    abs_r = abs(r)
    strength = "fuerte" if abs_r >= 0.7 else ("moderada" if abs_r >= 0.4 else ("débil" if abs_r >= 0.2 else "sin correlación significativa"))
    direction = "positiva" if r > 0 else "negativa"
    return json.dumps({
        "metric_a": metric_a, "metric_b": metric_b, "n_days": n, "weeks_back": weeks_back,
        "pearson_r": round(r, 3), "strength": strength, "direction": direction,
        "interpretation": f"Correlación {strength} {direction} (r={r:.3f}, N={n} días). Cuando {metric_a} sube, {metric_b} tiende a {'subir' if r > 0 else 'bajar'}.",
        "nota": f"Basado en {n} días ({weeks_back} semanas). {'Representativo.' if n >= 21 else 'Pocos datos, tomar con cautela.'}",
    }, ensure_ascii=False, separators=(",", ":"))


async def _kairos_weekly_sport_breakdown(mcp_session, weeks_back: int = 4, sport_type: str = "") -> str:
    """Agrega actividades por deporte en las últimas N semanas."""
    weeks_back = max(1, min(int(weeks_back), 12))
    end_date = date.today()
    start_date = end_date - timedelta(days=weeks_back * 7)
    collected: list[dict] = []
    start_idx = 0
    limit = 100
    for _ in range(10):
        try:
            raw = await call_tool(mcp_session, "get_activities", {"start": str(start_idx), "limit": str(limit)})
            activities, has_more, next_start = _parse_activities_response(raw)
        except Exception:
            break
        past_window = False
        for act in activities:
            act_date_iso = _extract_activity_date_iso(act)
            if not act_date_iso:
                continue
            try:
                act_d = date.fromisoformat(act_date_iso)
            except ValueError:
                continue
            if act_d < start_date:
                past_window = True
                break
            if act_d <= end_date:
                collected.append(act)
        if not has_more or past_window:
            break
        start_idx = next_start if next_start > start_idx else start_idx + limit
    sport_filter = str(sport_type or "").strip().lower()
    breakdown: dict[str, dict] = {}
    for act in collected:
        sport = act.get("activityType") or act.get("type") or "Otro"
        if isinstance(sport, dict):
            sport = sport.get("typeKey") or "Otro"
        sport = str(sport).strip()
        sport_key = sport.replace("_", " ").capitalize()
        if sport_filter and sport_filter not in sport.lower():
            continue
        dur_s = float(act.get("duration") or act.get("movingDuration") or 0.0)
        dist_m = float(act.get("distance") or 0.0)
        if sport_key not in breakdown:
            breakdown[sport_key] = {"count": 0, "duration_h": 0.0, "distance_km": 0.0}
        breakdown[sport_key]["count"] += 1
        breakdown[sport_key]["duration_h"] += dur_s / 3600
        breakdown[sport_key]["distance_km"] += dist_m / 1000
    for k in breakdown:
        breakdown[k]["duration_h"] = round(breakdown[k]["duration_h"], 1)
        breakdown[k]["distance_km"] = round(breakdown[k]["distance_km"], 2)
    total_acts = sum(v["count"] for v in breakdown.values())
    total_hours = round(sum(v["duration_h"] for v in breakdown.values()), 1)
    return json.dumps({
        "period": f"{start_date.strftime('%d/%m/%Y')} – {end_date.strftime('%d/%m/%Y')}",
        "weeks": weeks_back, "total_activities": total_acts, "total_hours": total_hours,
        "by_sport": breakdown,
        "nota": f"Basado en {total_acts} actividades en las últimas {weeks_back} semanas.",
    }, ensure_ascii=False, separators=(",", ":"))


_KAIROS_INTERNAL_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "kairos_load_trends",
            "description": (
                "Devuelve la serie diaria y semanal de TSS, ATL, CTL o TSB calculados desde el perfil. "
                "ÚSALA como PRIMERA opción para CUALQUIER pregunta sobre carga, fatiga o forma: "
                "'¿cuál fue mi TSS ayer?', '¿cuánto TSS llevo esta semana?', '¿cómo está mi ATL/CTL/TSB?', "
                "'evolución de carga', '¿estoy en sobreentrenamiento?'. "
                "IMPORTANTE: los endpoints de actividades Garmin NO devuelven TSS — esta tool es la única fuente."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "metric": {"type": "string", "enum": ["tss", "atl", "ctl", "tsb"], "description": "tss=carga sesión, atl=fatiga aguda, ctl=fitness crónico, tsb=forma"},
                    "weeks_back": {"type": "integer", "description": "Semanas hacia atrás (1–52, por defecto 8)"},
                },
                "required": ["metric"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "kairos_correlate",
            "description": "Calcula la correlación de Pearson entre dos métricas de carga/fatiga (TSS, ATL, CTL, TSB). Úsalo para preguntas como '¿correlaciona mi carga con mi forma?'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "metric_a": {"type": "string", "enum": ["tss", "atl", "ctl", "tsb"], "description": "Primera métrica"},
                    "metric_b": {"type": "string", "enum": ["tss", "atl", "ctl", "tsb"], "description": "Segunda métrica (distinta de metric_a)"},
                    "weeks_back": {"type": "integer", "description": "Semanas de historial (2–52, por defecto 8)"},
                },
                "required": ["metric_a", "metric_b"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "kairos_weekly_sport_breakdown",
            "description": "Devuelve el desglose de actividades por deporte (sesiones, horas, km) en las últimas N semanas. Úsalo para preguntas sobre distribución de carga entre disciplinas.",
            "parameters": {
                "type": "object",
                "properties": {
                    "weeks_back": {"type": "integer", "description": "Semanas hacia atrás (1–12, por defecto 4)"},
                    "sport_type": {"type": "string", "description": "Filtrar por deporte (ej: 'running', 'cycling'). Vacío = todos."},
                },
                "required": [],
            },
        },
    },
]


# ─── Helpers para cálculo incremental de carga/fatiga ─────────────────────────

async def _fetch_activities_for_load_calc(
    mcp_session: ClientSession,
    start_date_iso: str,
    end_date_iso: str,
) -> list[dict]:
    """Obtiene actividades en el rango [start_date, end_date] usando paginación de get_activities.

    Usa el mismo mecanismo paginado que _find_activity_id_by_date (probado y funcional).
    Para cuando start_date esté lejos en el tiempo pagina hacia atrás hasta encontrar
    actividades más antiguas que start_date.
    """
    from datetime import date as _date
    try:
        start_d = _date.fromisoformat(start_date_iso)
        end_d   = _date.fromisoformat(end_date_iso)
    except Exception:
        return []

    result: list[dict] = []
    seen_ids: set = set()
    start_idx = 0
    limit = 100
    max_pages = 50   # hasta 5000 actividades — suficiente para historiales de 120 días

    for _ in range(max_pages):
        raw = await call_tool(mcp_session, "get_activities", {"start": str(start_idx), "limit": str(limit)})
        activities, has_more, next_start = _parse_activities_response(raw)

        if not activities:
            break

        stop_early = False
        for act in activities:
            if not isinstance(act, dict):
                continue
            act_id = act.get("activityId") or act.get("id") or act.get("activity_id")
            if act_id is not None and act_id in seen_ids:
                continue
            if act_id is not None:
                seen_ids.add(act_id)

            d_iso = _extract_activity_date_iso(act)
            if not d_iso:
                continue
            try:
                d_obj = _date.fromisoformat(d_iso)
            except Exception:
                continue

            if d_obj > end_d:
                continue   # más reciente que el rango, seguir paginando
            if d_obj < start_d:
                stop_early = True
                break      # más antigua que el inicio, no hay más relevantes
            result.append(act)

        if stop_early or not has_more:
            break
        new_start = next_start if next_start > start_idx else start_idx + limit
        if new_start <= start_idx:
            break
        start_idx = new_start

    log.info(
        "_fetch_activities_for_load_calc: %d actividades obtenidas [%s → %s]",
        len(result), start_date_iso, end_date_iso,
    )
    # Diagnóstico de campos (DEBUG): útil para verificar compatibilidad con garmin-mcp
    if result:
        sample = result[0]
        tss_fields  = {k: sample[k] for k in sample if any(x in k.lower() for x in ("load","tss","training","effect","stress"))}
        dur_fields  = {k: sample[k] for k in sample if any(x in k.lower() for x in ("duration","elapsed","moving"))}
        hr_fields   = {k: sample[k] for k in sample if any(x in k.lower() for x in ("hr","heart"))}
        log.debug("sample activity keys: %s", list(sample.keys())[:30])
        log.debug("sample tss_fields=%s  dur_fields=%s  hr_fields=%s", tss_fields, dur_fields, hr_fields)
    return result


def _build_load_fatigue_dict_from_series(series: list[dict], model_cfg: dict) -> dict | None:
    """Construye el dict de carga/fatiga completo a partir de una serie ya calculada.

    Equivale al bloque final de _compute_load_fatigue_metrics pero reutiliza
    la serie persistida en DB en lugar de recalcularla.
    """
    if not series:
        return None

    latest = series[-1]
    last_28 = series[-28:] if len(series) >= 28 else series[:]
    last_42 = series[-42:] if len(series) >= 42 else series[:]
    atl_values = [float(x["atl"]) for x in last_28]
    tsb_values = [float(x["tsb"]) for x in last_28]

    weekly_tss_values: list[float] = []
    for idx in range(0, len(last_42), 7):
        chunk = last_42[idx:idx + 7]
        if chunk:
            weekly_tss_values.append(round(sum(float(x["tss"]) for x in chunk), 1))
    # Semana actual: lunes de esta semana → hoy (no los últimos 7 días del array)
    _today_s = date.today()
    _week_start_iso = (_today_s - timedelta(days=_today_s.weekday())).isoformat()
    current_week_tss = round(
        sum(float(x["tss"]) for x in series if (x.get("date") or "") >= _week_start_iso),
        1,
    )

    tsb_low  = round(_percentile(tsb_values, float(model_cfg.get("tsb_low_pct") or 0.20), default=-10.0), 1)
    tsb_high = round(_percentile(tsb_values, float(model_cfg.get("tsb_high_pct") or 0.80), default=5.0), 1)
    atl_high = round(_percentile(atl_values, float(model_cfg.get("atl_high_pct") or 0.80), default=max(50.0, float(latest["atl"]))), 1)
    weekly_target = round(_percentile(weekly_tss_values, float(model_cfg.get("weekly_target_pct") or 0.55), default=current_week_tss), 1)
    weekly_high   = round(_percentile(weekly_tss_values, float(model_cfg.get("weekly_high_pct") or 0.85), default=max(current_week_tss, weekly_target * 1.15)), 1)

    days_with_load = sum(1 for x in series if float(x.get("tss") or 0.0) > 0)
    _MIN_DAYS = 21
    warming_up = days_with_load < _MIN_DAYS

    tsb_now = float(latest["tsb"])
    atl_now = float(latest["atl"])
    tsb_abs_floor = float(model_cfg.get("tsb_abs_floor") or -30.0)
    abs_overload = tsb_now <= tsb_abs_floor
    sustained_overload = len(series) >= 7 and all(float(x["tsb"]) <= tsb_low for x in series[-7:])
    fatigue_high = (tsb_now < tsb_low) or (atl_now > atl_high)
    available_for_quality = (tsb_now >= tsb_low) and (tsb_now <= max(tsb_high, tsb_low + 4.0)) and not fatigue_high

    if abs_overload or sustained_overload or (current_week_tss > weekly_high and tsb_now < tsb_low):
        status = "overload"; action = "sobrecarga sostenida"
        recommendation = "Activa semana de descarga (−30% a −40% de volumen) y elimina calidad intensa 3-5 dias."
    elif fatigue_high:
        status = "fatigue_high"; action = "fatiga alta"
        recommendation = "Reduce intensidad/volumen hoy y prioriza recuperación activa, sueño e hidratación."
    elif available_for_quality:
        status = "ready"; action = "buena disponibilidad"
        recommendation = "Puedes mantener sesión de calidad o progresión controlada según plan."
    else:
        status = "neutral"; action = "carga estable"
        recommendation = "Mantén carga aeróbica controlada y reevalúa mañana con HRV/sueño/estrés."

    return {
        "model": {
            "name": "tp-inspired-ewma",
            "sport": str((model_cfg.get("_sport") or "running")),
            "atl_tau_days": int(model_cfg.get("atl_tau_days") or 7),
            "ctl_tau_days": int(model_cfg.get("ctl_tau_days") or 42),
        },
        "latest": latest,
        "series": series[-120:],
        "weekly": {"current_tss": current_week_tss, "target_tss": weekly_target, "high_tss": weekly_high},
        "ranges": {"tsb_low": tsb_low, "tsb_high": tsb_high, "atl_high": atl_high, "tsb_abs_floor": tsb_abs_floor},
        "warming_up": warming_up,
        "warming_up_days_remaining": max(0, _MIN_DAYS - days_with_load),
        "days_with_load": days_with_load,
        "flags": {
            "fatigue_high": fatigue_high, "sustained_overload": sustained_overload,
            "abs_overload": abs_overload, "available_for_quality": available_for_quality,
            "warming_up": warming_up,
        },
        "status": status,
        "action": action,
        "recommendation": recommendation,
    }


class TrainerAgent:
    """
    Agente entrenador personal que usa OpenAI + Garmin MCP.
    Mantiene historial de conversación y llama herramientas de Garmin
    automáticamente según lo que necesite para responder al usuario.
    """

    def __init__(self, mcp_session: ClientSession, provider: str = "vpn"):
        self.mcp_session = mcp_session
        self.set_provider(provider)
        # GitHub Models (vpn) tiene limite de ~8000 tokens en el request;
        # usamos el prompt compacto para dejar espacio a tools + contexto.
        self.system_prompt = _load_system_prompt(compact=(provider == "vpn"))
        self.user_profile = _load_user_profile()
        self.conversation_history: list[dict] = []
        self.tools_schema: list[dict] = []
        self.mcp_read_only = _is_mcp_read_only_enabled()
        self.knowledge_chunks, self.knowledge_sources = _load_athlete_knowledge_chunks(
            os.environ.get("ATHLETE_KB_PATHS", "")
        )
        stored_kb = (_storage.load_athlete_knowledge() or "").strip()
        if stored_kb:
            self.knowledge_chunks.append({"source": "db:athlete_knowledge", "text": stored_kb[:4000]})
            if "db:athlete_knowledge" not in self.knowledge_sources:
                self.knowledge_sources.append("db:athlete_knowledge")
        
        # Variables para tracking de tokens de la sesión
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0

    def set_provider(self, provider: str) -> None:
        """Configura o cambia el proveedor de LLM actual."""
        self.provider = provider
        if provider == "vpn":
            # GitHub Models — requiere VPN con Zscaler (usa truststore para el certificado corporativo)
            ssl_ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            http_client = httpx.AsyncClient(verify=ssl_ctx)
            self.client = AsyncOpenAI(
                base_url="https://models.inference.ai.azure.com",
                api_key=os.environ["GITHUB_TOKEN"],
                http_client=http_client,
            )
            self.model = os.environ.get("GITHUB_MODEL", "gpt-4o-mini")
            self._api_key = os.environ["GITHUB_TOKEN"]
        elif provider == "groq":
            # Groq — gratuito, sin VPN, 100k tokens/día
            # Registro en https://console.groq.com
            self.client = AsyncOpenAI(
                base_url="https://api.groq.com/openai/v1",
                api_key=os.environ["GROQ_API_KEY"],
            )
            self.model = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
            self._api_key = os.environ["GROQ_API_KEY"]
        elif provider == "gemini":
            # API nativa de Gemini con x-goog-api-key (soporta claves AQ.)
            _gemini_key = os.environ["GEMINI_API_KEY"]
            self.client = _GeminiClient(api_key=_gemini_key)
            self.model = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
            self._api_key = _gemini_key
        elif provider == "mistral":
            # Mistral La Plateforme — API compatible OpenAI, capa gratuita generosa
            # Registro en https://console.mistral.ai
            self.client = AsyncOpenAI(
                base_url="https://api.mistral.ai/v1",
                api_key=os.environ["MISTRAL_API_KEY"],
            )
            self.model = os.environ.get("MISTRAL_MODEL", "mistral-small-latest")
            self._api_key = os.environ["MISTRAL_API_KEY"]
        elif provider == "cerebras":
            # Cerebras — inferencia ultrarrápida, API compatible OpenAI, capa gratuita
            # Registro en https://cloud.cerebras.ai
            self.client = AsyncOpenAI(
                base_url="https://api.cerebras.ai/v1",
                api_key=os.environ["CEREBRAS_API_KEY"],
            )
            self.model = os.environ.get("CEREBRAS_MODEL", "llama-3.3-70b")
            self._api_key = os.environ["CEREBRAS_API_KEY"]
        elif provider == "nvidia":
            # NVIDIA NIM — API compatible con OpenAI
            # Docs: https://build.nvidia.com/explore/discover
            self.client = AsyncOpenAI(
                base_url="https://integrate.api.nvidia.com/v1",
                api_key=os.environ["NVIDIA_API_KEY"],
            )
            self.model = os.environ.get("NVIDIA_MODEL", "meta/llama-3.1-70b-instruct")
            self._api_key = os.environ["NVIDIA_API_KEY"]
        else:
            raise ValueError(f"Proveedor desconocido: '{provider}'. Opciones válidas: 'vpn', 'groq', 'gemini', 'mistral', 'cerebras', 'nvidia'.")
        
        # Variables para tracking de tokens de la sesión
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0

    async def initialize(self) -> None:
        """Carga las herramientas disponibles del MCP y restaura el historial reciente."""
        tools = await list_available_tools(self.mcp_session)
        if self.mcp_read_only:
            tools = [
                tool for tool in tools
                if not _is_write_mcp_tool((tool or {}).get("name", ""))
            ]
        self.tools_schema = _build_tools_schema(tools)
        # Añadir herramientas internas Kairos (no requieren MCP)
        self.tools_schema.extend(_KAIROS_INTERNAL_TOOLS)

        # Restaurar los últimos 6 mensajes del historial persistido
        ctx = _load_session_context()
        for entry in ctx.get("history", [])[-6:]:
            role = entry.get("role")
            content = entry.get("content", "")
            if role in ("user", "assistant") and content:
                self.conversation_history.append({"role": role, "content": content})

    async def fetch_garmin_personal_data(self) -> dict:
        """
        Obtiene datos personales del usuario directamente desde Garmin Connect.
        Estructura real de get_user_profile:
          { "userData": { "gender", "weight"(g), "height"(cm), "birthDate" }, ... }
        El nombre no está disponible en este endpoint.
        """
        result = {}
        today = date.today().isoformat()

        # --- get_user_profile ---
        try:
            raw = await call_tool(self.mcp_session, "get_user_profile", {})
            data = json.loads(raw) if raw and raw.strip().startswith("{") else {}
            if isinstance(data, dict):
                ud = data.get("userData", {})

                # Edad calculada desde birthDate (YYYY-MM-DD)
                birth = ud.get("birthDate") or data.get("birthDate")
                if birth:
                    try:
                        born = date.fromisoformat(str(birth))
                        today_d = date.today()
                        age = today_d.year - born.year - (
                            (today_d.month, today_d.day) < (born.month, born.day)
                        )
                        if 5 < age < 120:
                            result["age"] = age
                    except (ValueError, TypeError):
                        pass

                # Género
                gender = ud.get("gender") or data.get("gender", "")
                if gender:
                    result["gender"] = "hombre" if "MALE" in str(gender).upper() else "mujer"

                # Altura en cm
                height = ud.get("height") or data.get("height")
                if height:
                    try:
                        h = float(height)
                        if h > 50:
                            result["height_cm"] = int(round(h))
                    except (ValueError, TypeError):
                        pass

                # Peso: Garmin lo devuelve en gramos (ej: 67000.0 = 67 kg)
                weight = ud.get("weight") or data.get("weight")
                if weight:
                    try:
                        w = float(weight)
                        result["weight_kg"] = round(w / 1000, 1) if w > 300 else round(w, 1)
                    except (ValueError, TypeError):
                        pass
        except Exception:
            pass

        # --- get_body_composition (peso más reciente si get_user_profile no lo devolvió) ---
        if "weight_kg" not in result:
            try:
                raw = await call_tool(
                    self.mcp_session,
                    "get_body_composition",
                    {"start_date": today, "end_date": today},
                )
                data = json.loads(raw) if raw and raw.strip().startswith(("{", "[")) else {}
                if isinstance(data, list) and data:
                    data = data[0]
                if isinstance(data, dict):
                    weight = data.get("weight") or data.get("weightKg") or data.get("value")
                    if weight:
                        try:
                            w = float(weight)
                            result["weight_kg"] = round(w / 1000, 1) if w > 300 else round(w, 1)
                        except (ValueError, TypeError):
                            pass
            except Exception:
                pass

        return result

    async def compute_and_persist_load_metrics(self, force_full_recalc: bool = False) -> None:
        """Calcula TSS/ATL/CTL/TSB de forma incremental y los persiste en load_metrics_daily.

        Flujo:
        1. Lee la serie existente de DB (últimos 120 días).
        2. Si ya está al día, recarga el perfil y sale.
        3. Obtiene actividades de Garmin solo para los días nuevos (incremental).
        4. Calcula TSS día a día y re-corre el EWMA sembrando desde el último registro.
        5. Persiste las nuevas filas en DB (upsert).
        6. Actualiza self.user_profile["load_metrics"] con la serie completa.
        """
        from datetime import date as _date, timedelta

        today = _date.today()
        full_window_days = 120
        full_start = today - timedelta(days=full_window_days)

        # 1. Datos existentes en DB
        existing_series = _storage.get_load_metrics_series(days=full_window_days)
        last_date_str   = _storage.get_load_metrics_last_date()

        if not force_full_recalc and last_date_str:
            try:
                last_d = _date.fromisoformat(last_date_str)
            except Exception:
                last_d = None
            if last_d and last_d >= today:
                # Auto-detectar serie corrupta: si tenemos suficientes días en DB pero
                # todos los CTL son 0, los datos fueron guardados con el bug del TSS=0.
                # En ese caso forzamos recálculo completo.
                stale_zeros = (
                    len(existing_series) > 5
                    and all(float(row.get("ctl") or 0) <= 0.01 for row in existing_series)
                )
                saved_formula_v = int(
                    (self.user_profile.get("load_metrics") or {}).get("formula_version") or 0
                )
                formula_changed = saved_formula_v != _TSS_FORMULA_VERSION
                if stale_zeros or formula_changed:
                    reason = "CTL=0" if stale_zeros else f"fórmula v{saved_formula_v}→v{_TSS_FORMULA_VERSION}"
                    log.info(
                        "compute_load: recalculando serie completa (%s)",
                        reason,
                    )
                    fetch_from = full_start.isoformat()
                else:
                    log.info("compute_load: ya actualizado (último=%s) — recargando perfil", last_date_str)
                    self._apply_series_to_profile(existing_series, today)
                    return
            else:
                # Reprocessar desde el último día guardado (no last+1) para capturar
                # actividades que llegaron después de la última ejecución del mismo día.
                fetch_from = (last_d if last_d else full_start).isoformat()
        else:
            fetch_from = full_start.isoformat()
            log.info("compute_load: cálculo completo desde %s", fetch_from)

        log.info("compute_load: fetch incremental desde %s", fetch_from)

        # 2. FTP de ciclismo: intentar desde perfil cacheado o desde Garmin
        cycling_ftp: float | None = None
        try:
            cycling_ftp = float(
                (self.user_profile.get("performance") or {}).get("cycling_ftp") or 0
            ) or None
        except (ValueError, TypeError):
            cycling_ftp = None
        if not cycling_ftp:
            try:
                raw_ftp = await call_tool(self.mcp_session, "get_cycling_ftp", {})
                if raw_ftp and raw_ftp.strip():
                    ftp_data = json.loads(raw_ftp) if raw_ftp.strip()[0] in ("{", "[") else {}
                    if isinstance(ftp_data, list) and ftp_data:
                        ftp_data = ftp_data[0]
                    if isinstance(ftp_data, dict):
                        ftp_val = (
                            ftp_data.get("cyclingFtp")
                            or ftp_data.get("ftp")
                            or ftp_data.get("functionalThresholdPower")
                            or ftp_data.get("functional_threshold_power")
                        )
                        if ftp_val:
                            cycling_ftp = round(float(ftp_val), 1)
            except Exception:
                pass
        if cycling_ftp:
            log.info("compute_load: FTP ciclismo=%.0f W (usado para TSS por potencia)", cycling_ftp)
            # Cachear en perfil para el próximo arranque
            perf = self.user_profile.setdefault("performance", {})
            perf["cycling_ftp"] = cycling_ftp
            perf["cycling_ftp_date"] = today.isoformat()
        else:
            log.info("compute_load: FTP ciclismo no disponible — usando estimación por FC")

        # 3. Obtener actividades nuevas usando la función paginada probada
        new_activities = await _fetch_activities_for_load_calc(
            self.mcp_session, fetch_from, today.isoformat()
        )

        # 3b. Enriquecer actividades recientes (últimos 14 días) con detalle de get_activity
        # para obtener trainingStressScore (TSS nativo por potenciómetro) y campos de potencia.
        # Esto garantiza que Priority 1 use el TSS de Garmin cuando está disponible.
        _ENRICH_DAYS = 14
        _enrich_cutoff = (today - timedelta(days=_ENRICH_DAYS)).isoformat()
        _recent = [a for a in new_activities
                   if (_extract_activity_date_iso(a) or "") >= _enrich_cutoff]
        if _recent:
            log.info(
                "compute_load: enriqueciendo %d actividades recientes con detalle (trainingStressScore/potencia)",
                len(_recent),
            )
            for _act in _recent:
                _act_id = _act.get("id") or _act.get("activityId")
                if not _act_id:
                    continue
                try:
                    _raw_d = await call_tool(
                        self.mcp_session, "get_activity", {"activityId": str(_act_id)}
                    )
                    if _raw_d and _raw_d.strip():
                        _detail = json.loads(_raw_d) if _raw_d.strip()[0] in ("{", "[") else {}
                        if isinstance(_detail, dict):
                            for _k in (
                                "trainingStressScore",
                                "normalizedPower", "normalizedPowerWatts",
                                "avgPower", "averagePower", "avg_power_watts",
                                "activityTrainingLoad",
                            ):
                                if _detail.get(_k) is not None:
                                    _act[_k] = _detail[_k]
                except Exception:
                    pass

        # 4. TSS por día para las actividades nuevas
        tss_by_day:   dict[str, float] = {}
        count_by_day: dict[str, int]   = {}
        for act in new_activities:
            d_iso = _extract_activity_date_iso(act)
            if not d_iso:
                continue
            tss, _ = _estimate_session_tss(act, ftp=cycling_ftp)
            if tss > 0:
                tss_by_day[d_iso]   = tss_by_day.get(d_iso, 0.0) + tss
                count_by_day[d_iso] = count_by_day.get(d_iso, 0) + 1

        log.info("compute_load: %d días con TSS desde %s (actividades=%d)",
                 len(tss_by_day), fetch_from, len(new_activities))

        # 6. Configuración de tau por deporte
        model_cfg = _resolve_sport_model_cfg(self.user_profile)
        tau_atl   = max(3,  min(int(round(float(model_cfg.get("atl_tau_days") or 7))), 14))
        tau_ctl   = max(21, min(int(round(float(model_cfg.get("ctl_tau_days") or 42))), 90))
        alpha_atl = 1.0 / float(tau_atl)
        alpha_ctl = 1.0 / float(tau_ctl)

        # 7. Semilla: último valor de ATL/CTL en DB antes del rango a calcular
        atl_prev = 0.0
        ctl_prev = 0.0
        if existing_series:
            for row in sorted(existing_series, key=lambda x: x["date"], reverse=True):
                if row["date"] < fetch_from:
                    atl_prev = float(row.get("atl") or 0.0)
                    ctl_prev = float(row.get("ctl") or 0.0)
                    log.info("compute_load: semilla ATL=%.1f CTL=%.1f desde %s", atl_prev, ctl_prev, row["date"])
                    break

        # 8. EWMA día a día para el rango nuevo
        new_rows: list[dict] = []
        day_cursor = _date.fromisoformat(fetch_from)
        while day_cursor <= today:
            d_iso = day_cursor.isoformat()
            tss   = max(0.0, float(tss_by_day.get(d_iso, 0.0)))
            atl   = atl_prev + (tss - atl_prev) * alpha_atl
            ctl   = ctl_prev + (tss - ctl_prev) * alpha_ctl
            tsb   = ctl - atl
            new_rows.append({
                "date": d_iso,
                "tss":  round(tss, 2),
                "atl":  round(atl, 2),
                "ctl":  round(ctl, 2),
                "tsb":  round(tsb, 2),
                "activities_count": count_by_day.get(d_iso, 0),
            })
            atl_prev = atl
            ctl_prev = ctl
            day_cursor += timedelta(days=1)

        # 9. Persistir en DB
        _storage.upsert_load_metrics_series(new_rows)

        # 10. Recargar serie completa de DB y actualizar perfil
        full_series = _storage.get_load_metrics_series(days=full_window_days)
        self._apply_series_to_profile(full_series, today)
        log.info("compute_load: serie de %d días lista (hoy: TSS=%.1f ATL=%.1f CTL=%.1f TSB=%.1f)",
                 len(full_series),
                 float((full_series[-1] if full_series else {}).get("tss", 0)),
                 float((full_series[-1] if full_series else {}).get("atl", 0)),
                 float((full_series[-1] if full_series else {}).get("ctl", 0)),
                 float((full_series[-1] if full_series else {}).get("tsb", 0)))

    def _apply_series_to_profile(self, series: list[dict], today) -> None:
        """Actualiza self.user_profile["load_metrics"] con la serie dada y la guarda."""
        if not isinstance(self.user_profile, dict):
            return
        model_cfg = _resolve_sport_model_cfg(self.user_profile)
        model_cfg["_sport"] = str(
            ((self.user_profile.get("goals") or {}).get("primary") or "running")
        ).strip().lower()
        load_fatigue = _build_load_fatigue_dict_from_series(series, model_cfg)
        if not load_fatigue:
            return
        self.user_profile["load_metrics"] = {
            "model":           load_fatigue.get("model") or {},
            "last":            {**(load_fatigue.get("latest") or {}), "date": today.isoformat()},
            "ranges":          load_fatigue.get("ranges") or {},
            "weekly":          load_fatigue.get("weekly") or {},
            "series":          load_fatigue.get("series") or [],
            "formula_version": _TSS_FORMULA_VERSION,
            "updated_at":      datetime.now().isoformat(timespec="seconds"),
        }
        try:
            _save_user_profile(self.user_profile)
        except Exception:
            pass

    async def collect_startup_snapshot_48h(self) -> dict:
        """Recoge un snapshot operativo de 48h para briefing de arranque."""
        today_iso = date.today().isoformat()
        yesterday_iso = (date.today() - timedelta(days=1)).isoformat()

        async def _tool_json(tool_name: str, args: dict) -> Any:
            try:
                raw = await call_tool(self.mcp_session, tool_name, args)
            except Exception:
                return None
            parsed_raw = _try_parse_json(raw)
            if parsed_raw is not None:
                return parsed_raw
            compact = _compact_tool_result(raw, tool_name)
            parsed = _try_parse_json(compact)
            return parsed if parsed is not None else compact

        body_today = await _tool_json(
            "get_body_battery",
            {"start_date": today_iso, "end_date": today_iso},
        )
        body_yday = await _tool_json(
            "get_body_battery",
            {"start_date": yesterday_iso, "end_date": yesterday_iso},
        )
        hrv_today = await _tool_json("get_hrv_data", {"date": today_iso})
        hrv_yday = await _tool_json("get_hrv_data", {"date": yesterday_iso})
        sleep_today = await _tool_json("get_sleep_summary", {"date": today_iso})
        sleep_yday = await _tool_json("get_sleep_summary", {"date": yesterday_iso})
        load_trend = await _tool_json(
            "get_training_load_trend",
            {
                "start_date": (date.today() - timedelta(days=56)).isoformat(),
                "end_date": today_iso,
            },
        )

        # ── Actividades recientes (48h) para el briefing proactivo ─────────────
        # Solo necesitamos las últimas actividades para saber qué entrenó ayer/hoy.
        activities_raw = await _tool_json("get_activities", {"start": "0", "limit": "12"})
        activities_recent = _extract_activities_list(activities_raw)
        recent_trainings: list[dict] = []
        for activity in activities_recent:
            if not _is_activity_in_last_48h(activity):
                continue
            start_local = str(activity.get("startTimeLocal") or "")
            day = start_local.split("T", 1)[0] if "T" in start_local else ""
            recent_trainings.append(
                {
                    "date": day,
                    "name": activity.get("name") or activity.get("activityName") or activity.get("activityType") or "Actividad",
                    "activity_id": activity.get("activityId") or activity.get("id"),
                }
            )

        # ── Actividades históricas por rango de fechas para el modelo TSS/ATL/CTL ──
        # El modelo EWMA necesita TODOS los entrenamientos del período de cálculo,
        # independientemente del número total. Un atleta que doble sesiones tendría
        # 2 actividades/día → limit=N actividades no garantiza cobertura temporal.
        # Usamos get_activities_by_date con el mismo rango que days_window.
        load_window_days = 56
        load_start_iso = (date.today() - timedelta(days=load_window_days)).isoformat()
        activities_for_load: list[dict] = []
        _load_debug: str = "ok"
        try:
            hist_raw = await _tool_json(
                "get_activities_by_date",
                {
                    "start_date": load_start_iso,
                    "end_date": today_iso,
                    "page": 0,
                    "page_size": 200,
                },
            )
            if isinstance(hist_raw, dict):
                page_acts = _extract_activities_list(hist_raw.get("activities") or hist_raw)
                activities_for_load.extend(page_acts)
            elif isinstance(hist_raw, list):
                activities_for_load.extend(_extract_activities_list(hist_raw))
            elif isinstance(hist_raw, str):
                # get_activities_by_date devolvió cadena — intentar parseo manual
                try:
                    parsed = json.loads(hist_raw)
                    if isinstance(parsed, list):
                        activities_for_load.extend(_extract_activities_list(parsed))
                    elif isinstance(parsed, dict):
                        activities_for_load.extend(_extract_activities_list(parsed.get("activities") or parsed))
                except Exception:
                    pass
        except Exception as _e:
            _load_debug = f"excepcion get_activities_by_date: {_e}"
            log.warning("collect_startup: get_activities_by_date falló: %s", _e)

        # Fallback: si get_activities_by_date no retornó actividades, intentar get_activities con mayor límite
        if not activities_for_load:
            _load_debug = "get_activities_by_date sin datos — usando fallback get_activities(100)"
            log.info("collect_startup: get_activities_by_date sin datos, fallback a get_activities(100)")
            try:
                fallback_raw = await _tool_json("get_activities", {"start": "0", "limit": "100"})
                activities_for_load = _extract_activities_list(fallback_raw)
                if activities_for_load:
                    log.info("collect_startup: fallback ok — %d actividades obtenidas", len(activities_for_load))
                    _load_debug = f"fallback ok: {len(activities_for_load)} actividades via get_activities"
                else:
                    _load_debug = "sin actividades en fallback — usuario nuevo o sin datos en Garmin"
                    log.info("collect_startup: fallback también vacío — usuario nuevo o sin datos")
            except Exception as _e2:
                _load_debug = f"fallback también falló: {_e2}"
                log.warning("collect_startup: fallback get_activities falló: %s", _e2)
                activities_for_load = list(activities_recent)
        else:
            log.info("collect_startup: %d actividades obtenidas via get_activities_by_date", len(activities_for_load))

        body_summary = (
            f"hoy={_format_body_battery_day(body_today, today_iso)} · "
            f"ayer={_format_body_battery_day(body_yday, yesterday_iso)}"
        )
        hrv_summary = (
            f"hoy={_format_hrv_day(hrv_today, today_iso)} · "
            f"ayer={_format_hrv_day(hrv_yday, yesterday_iso)}"
        )
        sleep_summary = (
            f"hoy={_format_sleep_day(sleep_today, today_iso)} · "
            f"ayer={_format_sleep_day(sleep_yday, yesterday_iso)}"
        )

        load_fatigue = _compute_load_fatigue_metrics(
            activities=activities_for_load,
            trend_payload=load_trend,
            profile=getattr(self, "user_profile", {}) if hasattr(self, "user_profile") else {},
            days_window=load_window_days,
        )

        # ── Fallback: usar métricas precalculadas del perfil (de compute_and_persist) ──
        # compute_and_persist_load_metrics se ejecuta antes en main.py y deja
        # user_profile["load_metrics"] actualizado. Si la descarga de actividades
        # falla aquí, leemos esa caché en lugar de mostrar zeros.
        if load_fatigue is None:
            cached_lm = (getattr(self, "user_profile", None) or {}).get("load_metrics") or {}
            cached_series = cached_lm.get("series") or []
            if cached_series:
                model_cfg = _resolve_sport_model_cfg(getattr(self, "user_profile", {}))
                model_cfg["_sport"] = str(
                    ((self.user_profile.get("goals") or {}).get("primary") or "running")
                ).strip().lower()
                load_fatigue = _build_load_fatigue_dict_from_series(cached_series, model_cfg)
                if load_fatigue:
                    _load_debug = "usando métricas cacheadas de DB (cálculo incremental previo)"
                    log.info("collect_startup: cargadas métricas cacheadas (%d días)", len(cached_series))

        if isinstance(getattr(self, "user_profile", None), dict) and load_fatigue:
            self.user_profile["load_metrics"] = {
                "model": load_fatigue.get("model") or {},
                "last": {
                    **(load_fatigue.get("latest") or {}),
                    "date": (load_fatigue.get("latest") or {}).get("date") or today_iso,
                },
                "ranges": load_fatigue.get("ranges") or {},
                "weekly": load_fatigue.get("weekly") or {},
                "series": load_fatigue.get("series") or [],
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            }
            try:
                _save_user_profile(self.user_profile)
            except Exception:
                pass

        return {
            "window_hours": 48,
            "dates": {"today": today_iso, "yesterday": yesterday_iso},
            "body_battery": {"today": body_today, "yesterday": body_yday, "summary": body_summary},
            "hrv": {"today": hrv_today, "yesterday": hrv_yday, "summary": hrv_summary},
            "sleep": {"today": sleep_today, "yesterday": sleep_yday, "summary": sleep_summary},
            "load_fatigue": load_fatigue or {},
            "load_debug": _load_debug,
            "trainings": recent_trainings[:5],
        }

    async def build_startup_status_markdown(self, profile_changes: list[str] | None = None) -> str:
        """Construye el mensaje proactivo mostrado al arrancar la sesion."""
        snapshot = await self.collect_startup_snapshot_48h()
        snapshot["profile_changes"] = profile_changes or []
        active_plan = _get_active_training_plan(self.user_profile)
        snapshot["plan_assigned"] = bool(active_plan)
        if active_plan:
            snapshot["plan_recommendation"] = _build_startup_plan_recommendation(active_plan)
        return _build_proactive_status_markdown(snapshot)

    async def build_onboarding_mcp_enrichment(self) -> dict:
        """Obtiene datos MCP utiles para enriquecer la base inicial del atleta."""
        personal = await self.fetch_garmin_personal_data()
        startup = await self.collect_startup_snapshot_48h()
        return {
            "personal": personal,
            "startup_48h": startup,
        }

    def get_daily_usage_info(self) -> dict:
        """Devuelve información sobre el uso diario de tokens del proveedor actual."""
        api_key = getattr(self, "_api_key", "")
        today_usage = get_gemini_daily_usage(api_key) if api_key else 0
        is_exhausted = _storage.is_gemini_quota_exhausted(api_key) if api_key else False

        # Límites diarios de tokens definidos por defecto o por estimación razonable
        limits = {
            "gemini": 1_000_000,
            "groq": 100_000,
            "vpn": 100_000,         # GitHub Models
            "mistral": 10_000_000,  # Capa gratuita muy generosa
            "cerebras": 1_000_000,
            "nvidia": 1_000_000      # Límite por defecto, NVIDIA usa rate limits
        }
        limit = limits.get(self.provider, 1_000_000)

        return {
            "today_usage":     today_usage,
            "limit":           limit,
            "remaining":       max(0, limit - today_usage),
            "has_key":         bool(api_key),
            "quota_exhausted": is_exhausted or (today_usage >= limit if limit else False),
        }

    def get_gemini_daily_info(self) -> dict:
        """Devuelve información sobre el uso diario de tokens de Gemini (mantenido por compatibilidad)."""
        return self.get_daily_usage_info()

    def _build_system_prompt(self) -> str:
        """Construye el system prompt incluyendo la fecha actual y el perfil del usuario."""
        today_str = date.today().isoformat()
        yesterday_str = (date.today() - timedelta(days=1)).isoformat()
        date_context = (
            f"\n\n## Fecha actual\n"
            f"- Hoy es: **{today_str}** (formato ISO YYYY-MM-DD)\n"
            f"- Ayer fue: **{yesterday_str}**\n"
            f"- OBLIGATORIO: cuando pases fechas como parámetros a herramientas, SIEMPRE usa formato ISO YYYY-MM-DD exacto (ej: `{today_str}`). "
            f"NUNCA uses palabras como 'hoy', 'ayer', 'today', 'yesterday' ni caracteres de otros idiomas en parámetros de herramientas.\n"
        )
        profile_context = ""
        p = self.user_profile.get("personal", {})
        g = self.user_profile.get("goals", {})
        h = self.user_profile.get("health", {})
        if p or g or h:
            lines = []
            if p.get("name"):          lines.append(f"- Nombre: {p['name']}")
            if p.get("age"):           lines.append(f"- Edad: {p['age']} años")
            if p.get("gender"):        lines.append(f"- Género: {p['gender']}")
            if p.get("weight_kg"):     lines.append(f"- Peso: {p['weight_kg']} kg")
            if p.get("height_cm"):     lines.append(f"- Altura: {p['height_cm']} cm")
            if g.get("primary"):       lines.append(f"- Deporte principal: {g['primary']}")
            if g.get("weekly_training_hours"): lines.append(f"- Horas de entrenamiento/semana: {g['weekly_training_hours']}")
            if g.get("target_race"):   lines.append(f"- Carrera/evento objetivo: {g['target_race']}")
            if g.get("target_race_date"): lines.append(f"- Fecha del evento: {g['target_race_date']}")
            if g.get("target_time"):   lines.append(f"- Tiempo objetivo: {g['target_time']}")
            injuries = h.get("injuries", [])
            if injuries:               lines.append(f"- Lesiones/condiciones: {', '.join(injuries)}")
            if h.get("notes"):         lines.append(f"- Notas de salud: {h['notes']}")
            if lines:
                profile_context = "\n\n## Perfil del usuario\n" + "\n".join(lines) + "\n"

        # Incluir resúmenes de sesiones anteriores para memoria a largo plazo
        memory_context = ""
        summaries = _load_session_summaries()
        if summaries:
            recent = summaries[-3:]  # últimas 3 sesiones
            _MAX_SUMMARY = 350  # caracteres máximos por resumen
            lines = "\n".join(
                f"- **{s['date']}**: {s['summary'][:_MAX_SUMMARY]}{'…' if len(s['summary']) > _MAX_SUMMARY else ''}"
                for s in recent
            )
            memory_context = (
                f"\n\n## Memoria de sesiones anteriores\n"
                f"Estas son las conversaciones previas resumidas. Úsalas como contexto para dar continuidad:\n"
                f"{lines}\n"
            )

        kb_context = ""
        if self.knowledge_sources:
            kb_files = ", ".join(self.knowledge_sources)
            kb_context = (
                f"\n\n## Base de conocimiento del atleta\n"
                f"- Fuentes disponibles: {kb_files}\n"
                f"- Usa esta base como prioridad junto al Perfil del usuario.\n"
                f"- Para responder, combina esta base con datos reales de Garmin obtenidos por herramientas.\n"
            )

        return self.system_prompt + date_context + profile_context + memory_context + kb_context

    async def _handle_internal_tool(self, tool_name: str, arguments: dict) -> str:
        """Despacha herramientas internas kairos_* sin llamar al servidor MCP."""
        args = arguments if isinstance(arguments, dict) else {}
        if tool_name == "kairos_load_trends":
            return _kairos_load_trends(
                self.user_profile,
                metric=str(args.get("metric") or "tsb"),
                weeks_back=int(args.get("weeks_back") or 8),
            )
        elif tool_name == "kairos_correlate":
            return _kairos_correlate(
                self.user_profile,
                metric_a=str(args.get("metric_a") or "tss"),
                metric_b=str(args.get("metric_b") or "tsb"),
                weeks_back=int(args.get("weeks_back") or 8),
            )
        elif tool_name == "kairos_weekly_sport_breakdown":
            return await _kairos_weekly_sport_breakdown(
                self.mcp_session,
                weeks_back=int(args.get("weeks_back") or 4),
                sport_type=str(args.get("sport_type") or ""),
            )
        else:
            return json.dumps({"error": f"Herramienta interna '{tool_name}' no reconocida."}, ensure_ascii=False)

    def _build_messages(self, user_message: str) -> list[dict]:
        """Construye el array de mensajes para la llamada al LLM.
        Limita el historial a los últimos 6 turnos (3 pares user/assistant)
        para mantener el contexto razonable sin consumir tokens innecesarios.
        """
        messages = [{"role": "system", "content": self._build_system_prompt()}]
        rag_context = _build_athlete_knowledge_context(user_message, self.knowledge_chunks)
        if rag_context:
            messages.append({"role": "system", "content": rag_context})
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

        # Ruta determinista para estado de plan: evita alucinaciones del LLM
        # cuando la pregunta es "¿tengo plan?" o "¿cuál es mi plan?".
        if _is_plan_status_intent(user_message):
            assistant_reply = _build_training_plan_status_markdown(self.user_profile)
            self.conversation_history.append({"role": "user", "content": user_message})
            self.conversation_history.append({"role": "assistant", "content": assistant_reply})
            _save_history_entry("user", user_message)
            _save_history_entry("assistant", assistant_reply)
            return assistant_reply

        # Ruta funcional de planificación: generación/actualización estructurada,
        # persistida y versionada en DB sin depender del LLM.
        if _is_planning_intent(user_message) and _has_goal_in_profile(self.user_profile):
            try:
                previous_plan_row = None
                previous_plan = None
                previous_sessions: list[dict] = []
                try:
                    previous_plan_row = _storage.get_active_training_plan()
                    previous_plan = _normalize_storage_plan_row(previous_plan_row)
                    if previous_plan and previous_plan.get("id"):
                        previous_sessions = _storage.list_training_plan_sessions(str(previous_plan.get("id")))
                except Exception:
                    previous_plan = _get_active_training_plan(self.user_profile)
                    previous_sessions = []

                new_plan, new_sessions = _generate_structured_plan_payload(
                    self.user_profile,
                    user_message,
                    base_plan=previous_plan,
                )
                validation_errors = _validate_structured_plan(new_plan, new_sessions, self.user_profile)
                if validation_errors:
                    assistant_reply = (
                        "## ⚠️ No pude persistir el plan propuesto\n\n"
                        + "\n".join(f"- {err}" for err in validation_errors)
                        + "\n\nAjusta perfil/objetivo con `/perfil editar objetivo` y lo regenero."
                    )
                else:
                    wants_new = _wants_new_plan_intent(user_message)
                    if previous_plan and previous_plan.get("id") and not wants_new:
                        persisted = _storage.update_training_plan(
                            str(previous_plan.get("id")),
                            {
                                "title": new_plan.get("title"),
                                "description": new_plan.get("description"),
                                "objective": new_plan.get("objective"),
                                "difficulty": new_plan.get("difficulty"),
                                "duration_weeks": new_plan.get("duration_weeks"),
                                "status": "active",
                                "source": "agent_structured_plan",
                                "plan_data": dict(new_plan.get("plan_data") or {}),
                            },
                            sessions=new_sessions,
                            change_reason="agent_structured_adjustment",
                        )
                        persisted_plan = _normalize_storage_plan_row(persisted) or new_plan
                    else:
                        persisted = _storage.create_training_plan(
                            new_plan,
                            sessions=new_sessions,
                            change_reason="agent_structured_creation",
                        )
                        persisted_plan = _normalize_storage_plan_row(persisted) or new_plan

                    change_summary = _summarize_plan_changes(
                        previous_plan,
                        persisted_plan,
                        previous_sessions,
                        new_sessions,
                    )
                    assistant_reply = _build_structured_plan_markdown(
                        persisted_plan,
                        new_sessions,
                        change_summary,
                    )

                    # Espejo backward-compatible en perfil.
                    persisted_plan.setdefault("target_race", (new_plan.get("plan_data") or {}).get("target_race"))
                    persisted_plan.setdefault("target_race_date", (new_plan.get("plan_data") or {}).get("target_race_date"))
                    self.user_profile["training_plan"] = persisted_plan
                    _save_user_profile(self.user_profile)

                self.conversation_history.append({"role": "user", "content": user_message})
                self.conversation_history.append({"role": "assistant", "content": assistant_reply})
                _save_history_entry("user", user_message)
                _save_history_entry("assistant", assistant_reply)
                return assistant_reply
            except Exception:
                # Fallback conservador al flujo anterior
                pass

        # Ruta directa para récords personales: evita respuestas de "sin acceso"
        # y asegura que se entreguen distancia + marca desde la primera respuesta.
        force_personal_records = (
            _is_personal_records_intent(user_message)
            or _is_personal_records_followup_intent(user_message, self.conversation_history)
        )
        if force_personal_records:
            try:
                available_tool_names = {
                    (item.get("function") or {}).get("name")
                    for item in (self.tools_schema or [])
                    if isinstance(item, dict)
                }
                records_tool = None
                if "get_personal_record" in available_tool_names:
                    records_tool = "get_personal_record"
                elif "get_personal_records" in available_tool_names:
                    records_tool = "get_personal_records"

                if records_tool:
                    records_raw = await call_tool(self.mcp_session, records_tool, {})
                    records_compact = _compact_tool_result(records_raw, records_tool)
                    if records_compact and records_compact != "(sin datos)" and not _is_no_data_result(records_raw):
                        records_sport = _detect_personal_records_sport_intent(user_message, self.conversation_history)
                        assistant_reply = _build_personal_records_markdown(records_compact, preferred_sport=records_sport)
                        self.conversation_history.append({"role": "user", "content": user_message})
                        self.conversation_history.append({"role": "assistant", "content": assistant_reply})
                        _save_history_entry("user", user_message)
                        _save_history_entry("assistant", assistant_reply)
                        return assistant_reply
            except Exception:
                pass

        # Pre-fetch proactivo: si el usuario menciona una fecha explícita,
        # resolver y cargar la actividad + contexto completo ANTES del bucle LLM.
        user_date = _extract_iso_date_from_text(user_message)
        if user_date:
            # Intento 1: get_activities_by_date para la fecha exacta (más fiable)
            pre_id = None
            try:
                _raw_date_acts = await call_tool(
                    self.mcp_session, "get_activities_by_date",
                    {"startdate": user_date, "enddate": user_date},
                )
                _acts_day = _extract_activities_list(_raw_date_acts)
                if _acts_day:
                    _first = _acts_day[0]
                    pre_id = (_first.get("id") or _first.get("activityId")
                              or _first.get("activity_id"))
                    if pre_id:
                        pre_id = int(pre_id)
            except Exception as _e:
                log.debug("pre_fetch get_activities_by_date fallback: %s", _e)

            # Intento 2: paginación por fecha si el anterior falló
            if pre_id is None:
                pre_id = await _find_activity_id_by_date(self.mcp_session, user_date)

            log.info("pre_fetch: user_date=%s pre_id=%s", user_date, pre_id)
            if pre_id is not None:
                raw_pre = await call_tool(self.mcp_session, "get_activity", {"activity_id": pre_id})
                pre_data = _compact_tool_result(raw_pre, "get_activity")
                context_parts = [f"ACTIVIDAD (activityId={pre_id}, fecha={user_date}):\n{pre_data}"]

                # Body battery del día de la actividad (requiere start_date + end_date)
                try:
                    raw_bb = await call_tool(self.mcp_session, "get_body_battery", {
                        "start_date": user_date,
                        "end_date": user_date,
                    })
                    bb_data = _compact_tool_result(raw_bb, "get_body_battery")
                    log.debug(f"body_battery({user_date}): {bb_data[:120] if bb_data else 'None'}")
                    if bb_data and bb_data != "(sin datos)":
                        context_parts.append(f"BODY BATTERY del {user_date}:\n{bb_data}")
                except Exception as e:
                    log.debug(f"body_battery error: {e}")

                # Sueño de la noche previa (recuperación pre-actividad)
                try:
                    night_before = (date.fromisoformat(user_date) - timedelta(days=1)).isoformat()
                    raw_sleep = await call_tool(self.mcp_session, "get_sleep_data", {"date": night_before})
                    sleep_data = _compact_tool_result(raw_sleep, "get_sleep_data")
                    log.debug(f"sleep({night_before}): {sleep_data[:120] if sleep_data else 'None'}")
                    if sleep_data and sleep_data != "(sin datos)":
                        context_parts.append(f"SUENO noche previa ({night_before}):\n{sleep_data}")
                except Exception as e:
                    log.debug(f"sleep error: {e}")

                # HRV del día de la actividad
                try:
                    raw_hrv = await call_tool(self.mcp_session, "get_hrv_data", {"date": user_date})
                    hrv_data = _compact_tool_result(raw_hrv, "get_hrv_data")
                    log.debug(f"hrv({user_date}): {hrv_data[:80] if hrv_data else 'None'}")
                    if hrv_data and hrv_data != "(sin datos)":
                        context_parts.append(f"HRV del {user_date}:\n{hrv_data}")
                except Exception as e:
                    log.debug(f"hrv error: {e}")

                # Carga de entrenamiento — prueba con rango de 4 semanas
                try:
                    tl_end   = date.today().isoformat()
                    tl_start = (date.today() - timedelta(weeks=4)).isoformat()
                    raw_tl = await call_tool(self.mcp_session, "get_training_load_trend", {
                        "start_date": tl_start,
                        "end_date": tl_end,
                    })
                    tl_data = _compact_tool_result(raw_tl, "get_training_load_trend")
                    log.debug(f"training_load: {tl_data[:80] if tl_data else 'None'}")
                    if tl_data and tl_data != "(sin datos)":
                        context_parts.append(f"CARGA DE ENTRENAMIENTO:\n{tl_data}")
                except Exception as e:
                    log.debug(f"training_load error: {e}")

                # ── Zonas reales de FC ──────────────────────────────────────────────
                # Estrategia 1: buscar en el raw de get_activity (ya disponible, sin llamada extra)
                raw_hr_zones = None
                try:
                    _act_data = json.loads(raw_pre) if raw_pre else {}
                    _zones_in_act = _find_hr_zones_in_json(_act_data)
                    if _zones_in_act:
                        raw_hr_zones = json.dumps(_zones_in_act)
                        log.info("hr_zones: encontradas %d zonas en get_activity", len(_zones_in_act))
                    else:
                        log.info("hr_zones: get_activity no contiene datos de zonas (requiere llamada específica)")
                except Exception:
                    pass

                # Estrategia 2: llamar get_activity_hr_zones (herramienta específica)
                if not raw_hr_zones:
                    for _param in ({"activity_id": pre_id}, {"activityId": pre_id}, {"id": pre_id}):
                        try:
                            _raw = await call_tool(self.mcp_session, "get_activity_hr_zones", _param)
                            log.info("get_activity_hr_zones(%s): %s", _param, (_raw or "")[:200])
                            if not _raw or "Unknown tool" in _raw or "unknown tool" in _raw.lower():
                                log.info("hr_zones: get_activity_hr_zones no disponible en este servidor MCP")
                                break  # no reintentar con otros params si la herramienta no existe
                            if _raw.strip() not in ("null", "[]", "{}", "(sin datos)", ""):
                                _parsed = _parse_hr_zones_list(_raw)
                                if _parsed:
                                    raw_hr_zones = _raw
                                    log.info("hr_zones: %d zonas via get_activity_hr_zones(%s)", len(_parsed), list(_param.keys())[0])
                                    break
                        except Exception as _e:
                            log.info("get_activity_hr_zones(%s) error: %s", list(_param.keys())[0], _e)
                            break

                # Estrategia 3: get_activity_hr_in_timezones (nombre real en garminconnect / garmin-mcp)
                if not raw_hr_zones:
                    for _param in ({"activity_id": pre_id}, {"activityId": pre_id}):
                        try:
                            _raw = await call_tool(self.mcp_session, "get_activity_hr_in_timezones", _param)
                            log.info("get_activity_hr_in_timezones(%s): %s", _param, (_raw or "")[:200])
                            if not _raw or "Unknown tool" in _raw or "unknown tool" in _raw.lower():
                                log.info("hr_zones: get_activity_hr_in_timezones no disponible en este servidor MCP")
                                break  # no reintentar con otros params si la herramienta no existe
                            if _raw.strip() not in ("null", "[]", "{}", "(sin datos)", ""):
                                _parsed = _parse_hr_zones_list(_raw)
                                if _parsed:
                                    raw_hr_zones = _raw
                                    log.info("hr_zones: %d zonas via get_activity_hr_in_timezones", len(_parsed))
                                    break
                        except Exception:
                            break

                if raw_hr_zones:
                    context_parts.append(f"ZONAS FC (datos reales):\n{raw_hr_zones}")
                else:
                    log.info("hr_zones: NO se encontraron datos reales de zonas — usando estimación gaussiana")

                # Si tenemos zonas reales, actualizar pre_data para reemplazar estimación gaussiana
                _zones_for_predata = _parse_hr_zones_list(raw_hr_zones)
                if _zones_for_predata:
                    try:
                        _pd = json.loads(pre_data)
                        if isinstance(_pd, dict):
                            _pd.pop("zonas_fc_estimadas", None)
                            _pd.pop("nota_zonas", None)
                            _total_z = sum(float(z.get("secsInZone") or 0) for z in _zones_for_predata)
                            if _total_z > 0:
                                _zr = {}
                                for _z in sorted(_zones_for_predata, key=lambda x: int(x.get("zoneNumber") or 0)):
                                    _zn = int(_z.get("zoneNumber") or 0)
                                    _zs = float(_z.get("secsInZone") or 0)
                                    _pct = round(_zs / _total_z * 100, 1)
                                    _lo = _z.get("minHeartRateIn") or "?"
                                    _hi = _z.get("maxHeartRateIn") or "?"
                                    _zname = _z.get("zoneName") or f"Z{_zn}"
                                    _zr[f"Z{_zn}_{_zname}_{_lo}-{_hi}bpm"] = f"{_pct:.1f}% (~{int(_zs/60)} min)"
                                _pd["zonas_fc_reales_garmin"] = _zr
                                _pd["nota_zonas"] = "Zonas reales de Garmin (Tiempo en Zonas del dispositivo)."
                            pre_data = json.dumps(_pd, ensure_ascii=False, separators=(",", ":"))
                            context_parts[0] = f"ACTIVIDAD (activityId={pre_id}, fecha={user_date}):\n{pre_data}"
                            log.info("pre_data: zonas_fc_reales_garmin inyectadas (%d zonas)", len(_zones_for_predata))
                    except Exception as _ze:
                        log.debug("pre_data zone update error: %s", _ze)

                # Construir bloque de análisis pre-computado en Python
                analysis_block = _build_activity_analysis_block(
                    activity_raw=raw_pre,
                    body_battery_raw=next((p for p in context_parts[1:] if "BODY BATTERY" in p), None),
                    sleep_raw=next((p for p in context_parts if "SUENO" in p), None),
                    hrv_raw=next((p for p in context_parts if "HRV" in p), None),
                    training_load_raw=next((p for p in context_parts if "CARGA" in p), None),
                    ftp=float((self.user_profile.get("performance") or {}).get("cycling_ftp") or 0) or None,
                    hr_zones_raw=raw_hr_zones,
                )

                # Eliminar del array de mensajes cualquier respuesta previa del asistente
                # que sea un análisis de actividad — evita copiar floats crudos y formato viejo.
                # Incluye tanto los headers del bloque pre-computado como el texto libre
                # que el LLM genera, para limpiar también respuestas de sesiones anteriores.
                _ANALYSIS_MARKERS = (
                    # Headers del bloque pre-computado
                    "Resumen ejecutivo", "zonas de FC", "Distribución por zonas",
                    "Plan de recuperación", "Efecto de entrenamiento",
                    "Recomendaciones para la próxima", "Training load:",
                    "Body battery:", "Hidratación recomendada",
                    # Texto libre típico del LLM en español (respuestas de sesiones anteriores)
                    "velocidad media fue", "velocidad máxima fue",
                    "ritmo medio fue", "frecuencia cardíaca media fue",
                    "efecto de entrenamiento de", "zonas de frecuencia cardíaca",
                    "carga de entrenamiento (TSS)", "elevación ganada fue",
                    "tiempo de recuperación de",
                    # Identificadores de la actividad concreta
                    user_date, str(pre_id),
                )
                messages = [
                    msg for msg in messages
                    if not (
                        msg.get("role") == "assistant"
                        and any(m in (msg.get("content") or "") for m in _ANALYSIS_MARKERS)
                    )
                ]

                # Pre-computar texto de zonas para forzar salida exacta al LLM
                _zones_direct_text = None
                if _zones_for_predata:
                    _total_zd = sum(float(z.get("secsInZone") or 0) for z in _zones_for_predata)
                    if _total_zd > 0:
                        _zdlines = []
                        for _z in sorted(_zones_for_predata, key=lambda x: int(x.get("zoneNumber") or 0)):
                            _zn = int(_z.get("zoneNumber") or 0)
                            _zs = float(_z.get("secsInZone") or 0)
                            _pct = round(_zs / _total_zd * 100, 1)
                            _mins = int(_zs / 60)
                            _zname = _z.get("zoneName") or f"Z{_zn}"
                            _lo_d = _z.get("minHeartRateIn") or "?"
                            _hi_d = _z.get("maxHeartRateIn") or "?"
                            _rng = f" ({_lo_d}-{_hi_d} bpm)" if _lo_d != "?" and _hi_d != "?" else ""
                            _zdlines.append(f"• Z{_zn} {_zname}{_rng}: {_pct}% (~{_mins} min)")
                        _zones_direct_text = "\n".join(_zdlines)

                _zones_override = (
                    f"\nZONAS FC REALES GARMIN — USA ESTOS VALORES EXACTOS:\n{_zones_direct_text}\n"
                    "OBLIGA: copia estas lineas en '## 💓 Distribucion por zonas de FC'. "
                    "PROHIBIDO calcular, estimar o usar zonas_fc_estimadas.\n"
                    if _zones_direct_text else ""
                )

                messages.insert(len(messages) - 1, {
                    "role": "system",
                    "content": (
                        f"ANALISIS PRE-COMPUTADO DE LA ACTIVIDAD DEL {user_date}:\n\n"
                        f"{analysis_block}\n\n"
                        f"{_zones_override}"
                        "INSTRUCCION OBLIGATORIA: Usa SOLO los datos de los bloques === de arriba.\n"
                        "Estructura la respuesta en Markdown con estas secciones (una por linea):\n\n"
                        "## \U0001f4ca Resumen ejecutivo\n"
                        "## \U0001f493 Distribucion por zonas de FC\n"
                        "## \u26a1 Efecto de entrenamiento y carga\n"
                        "## \U0001f4a7 Hidratacion recomendada\n"
                        "## \U0001f6cc Estado pre-carrera (body battery y sueno si disponibles)\n"
                        "## \U0001f504 Plan de recuperacion post-actividad\n"
                        "## \U0001f3af Recomendaciones para la proxima edicion\n\n"
                        "FORMATO: Cada item de lista en su propia linea con '- ' o '* '. "
                        "NUNCA pongas varios items en la misma linea. "
                        "PROHIBIDO: velocidad en m/s, duracion en segundos, floats crudos. "
                        "USA los valores calculados: "
                        "ciclismo \u2192 km/h; running \u2192 min/km; duracion \u2192 HH:MM:SS."
                    ),
                })
            else:
                log.debug(f"Pre-fetch {user_date}: no se encontro actividad")

        _MAX_TOOL_ITER = 15
        iteration = 0
        while True:
            iteration += 1
            if iteration > _MAX_TOOL_ITER:
                log.debug(f"Límite de {_MAX_TOOL_ITER} iteraciones de herramientas alcanzado. Abortando.")
                assistant_reply = "[Lo siento, la consulta requirió demasiadas llamadas a herramientas. Por favor, reformula tu pregunta de forma más concreta.]"
                self.conversation_history.append({"role": "user", "content": user_message})
                self.conversation_history.append({"role": "assistant", "content": assistant_reply})
                _save_history_entry("user", user_message)
                _save_history_entry("assistant", assistant_reply)
                return assistant_reply
            try:
                response = await self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    tools=self.tools_schema if self.tools_schema else None,
                    tool_choice="auto" if self.tools_schema else None,
                )
            except Exception as api_exc:
                err_str = str(api_exc)
                
                # Detectar si la clave ha agotado recursos o cuota y marcarlo en la BBDD
                is_quota_exhausted = (
                    "RESOURCE_EXHAUSTED" in err_str
                    or "quota_exceeded" in err_str
                    or "insufficient_quota" in err_str
                    or "limit_exceeded" in err_str
                    or ("quota" in err_str.lower() and "rate" not in err_str.lower())
                )
                if is_quota_exhausted and getattr(self, "_api_key", None):
                    mark_gemini_quota_exhausted(self._api_key)
                
                if "413" in err_str or "tokens_limit_reached" in err_str or "Request body too large" in err_str:
                    msg = (
                        "La consulta es demasiado extensa para el modelo actual (límite de tokens del proveedor).\n\n"
                        "Prueba con una de estas opciones:\n"
                        "- Haz una pregunta más específica y acotada (ej: *¿Cómo estoy hoy?* en lugar de *analiza 8 semanas*)\n"
                        "- Divide el análisis en pasos: primero métricas de hoy, luego tendencias, luego plan\n"
                        "- Si no estás en red corporativa, reinicia el agente o usa /modelo para cambiar a un modelo con contexto más grande (como Gemini)"
                    )
                    self.conversation_history.append({"role": "user", "content": user_message})
                    self.conversation_history.append({"role": "assistant", "content": msg})
                    _save_history_entry("user", user_message)
                    _save_history_entry("assistant", msg)
                    return msg
                raise

            # Track and log token usage
            if getattr(response, "usage", None):
                u = response.usage
                p_toks = getattr(u, "prompt_tokens", 0) or 0
                c_toks = getattr(u, "completion_tokens", 0) or 0
                self.total_prompt_tokens += p_toks
                self.total_completion_tokens += c_toks
                total_step_tokens = p_toks + c_toks
                log.debug(f"Tokens - Entrada: {p_toks} | Salida: {c_toks} | Total paso: {total_step_tokens}")
                if getattr(self, "_api_key", None):
                    update_gemini_daily_usage(self._api_key, total_step_tokens)

            message = response.choices[0].message

            # Debug: muestra si el modelo llama herramientas
            if message.tool_calls:
                tool_names = [tc.function.name for tc in message.tool_calls]
                log.debug(f"Iteracion {iteration}: llamando tools -> {tool_names}")
            else:
                log.debug(f"Iteración {iteration}: respuesta directa (sin tool calls)")
                log.debug(f"finish_reason: {response.choices[0].finish_reason}")

            # Si el modelo quiere llamar herramientas de Garmin
            if message.tool_calls:
                messages.append(message)

                for tool_call in message.tool_calls:
                    tool_name = tool_call.function.name
                    # Compatibilidad: algunas guías/prompts antiguos usan plural.
                    if tool_name == "get_personal_records":
                        tool_name = "get_personal_record"
                    try:
                        arguments = json.loads(tool_call.function.arguments)
                    except json.JSONDecodeError:
                        arguments = {}
                    # json.loads("null") devuelve None → las tools MCP
                    # esperan un objeto, no null → convertir a {}
                    if arguments is None:
                        arguments = {}
                    # Normalizar fechas: convertir palabras como 'hoy'/'ayer' a ISO
                    arguments = _normalize_date_args(arguments)
                    if tool_name == "get_activity":
                        # Si la pregunta contiene una fecha explícita, SIEMPRE resolver por fecha,
                        # ignorando el activity_id que haya propuesto el modelo (puede ser de
                        # conversaciones anteriores o alucinado).
                        user_date = _extract_iso_date_from_text(user_message)
                        if user_date:
                            resolved_id = await _find_activity_id_by_date(self.mcp_session, user_date)
                            if resolved_id is not None:
                                log.debug(f"Fecha explicita {user_date} -> resolviendo a activity_id={resolved_id} (modelo propuso {arguments.get('activity_id', 'nada')})")
                                arguments = {"activity_id": resolved_id}
                            else:
                                log.debug(f"Fecha explicita {user_date} -> no se encontro actividad ese dia")
                                arguments = {}
                        else:
                            arguments = await _normalize_get_activity_args(
                                self.mcp_session,
                                arguments,
                                user_message=user_message,
                            )
                            if not (isinstance(arguments, dict) and arguments.get("activity_id")):
                                resolved_id = await _resolve_activity_id_from_query(self.mcp_session, user_message)
                                if resolved_id is not None:
                                    arguments = {"activity_id": resolved_id}
                    arguments = _normalize_trend_date_range(tool_name, arguments)

                    if self.mcp_read_only and _is_write_mcp_tool(tool_name):
                        log.debug(f"Bloqueada tool de escritura por MCP_READ_ONLY: {tool_name}")
                        raw_result = _build_mcp_read_only_block_message(tool_name)
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": raw_result,
                        })
                        continue

                    log.debug(f"Ejecutando: {tool_name}({arguments})")
                    if tool_name == "get_activity" and not (isinstance(arguments, dict) and arguments.get("activity_id")):
                        raw_result = await _build_activity_candidates_payload(self.mcp_session, user_message)
                    elif tool_name.startswith("kairos_"):
                        raw_result = await self._handle_internal_tool(tool_name, arguments)
                    else:
                        raw_result = await call_tool(
                            self.mcp_session, tool_name, arguments
                        )

                    # Si no hay training_readiness, enriquecer contexto con métricas de recuperación
                    if (
                        tool_name in {"get_training_readiness", "get_morning_training_readiness"}
                        and _is_no_data_result(raw_result)
                    ):
                        requested_date = arguments.get("date") if isinstance(arguments, dict) else None
                        fallback_snapshot = await _build_recovery_fallback_snapshot(
                            self.mcp_session,
                            requested_date if isinstance(requested_date, str) else None,
                        )
                        if fallback_snapshot:
                            log.debug("Training readiness sin datos; usando snapshot alternativo de recuperación")
                            raw_result = fallback_snapshot

                    tool_result = _compact_tool_result(raw_result, tool_name)
                    log.debug(f"Resultado ({len(raw_result or '')} -> {len(tool_result)} chars): {tool_result[:150]}")

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": tool_result,
                    })

                # Continúa el loop para que el modelo procese los resultados
                continue

            # Respuesta final del agente
            assistant_reply = message.content or ""

            # Fallback para récords personales cuando el modelo responde
            # con "sin acceso" pese a tener herramientas MCP activas.
            if _is_no_access_reply(assistant_reply) and (
                _is_personal_records_intent(user_message)
                or _is_personal_records_followup_intent(user_message, self.conversation_history)
            ):
                try:
                    available_tool_names = {
                        (item.get("function") or {}).get("name")
                        for item in (self.tools_schema or [])
                        if isinstance(item, dict)
                    }
                    records_tool = None
                    if "get_personal_record" in available_tool_names:
                        records_tool = "get_personal_record"
                    elif "get_personal_records" in available_tool_names:
                        records_tool = "get_personal_records"

                    if records_tool:
                        records_raw = await call_tool(self.mcp_session, records_tool, {})
                        records_compact = _compact_tool_result(records_raw, records_tool)
                        if records_compact and records_compact != "(sin datos)" and not _is_no_data_result(records_raw):
                            records_sport = _detect_personal_records_sport_intent(user_message, self.conversation_history)
                            assistant_reply = _build_personal_records_markdown(records_compact, preferred_sport=records_sport)
                except Exception:
                    pass

            # Fallback anti-respuesta genérica: si ya existe objetivo en perfil,
            # devolver una planificación base en lugar de pedir contexto redundante.
            if (
                _is_generic_needs_more_info_reply(assistant_reply)
                and _is_planning_intent(user_message)
                and _has_goal_in_profile(self.user_profile)
            ):
                assistant_reply = _build_goal_plan_fallback(self.user_profile)
                # Persistir un plan activo mínimo como fuente de verdad en DB
                # y mantener compatibilidad hacia atrás en perfil.
                try:
                    goals = (self.user_profile or {}).get("goals", {})
                    target_race = goals.get("target_race") or "objetivo"
                    target_date = goals.get("target_race_date") or "fecha por definir"
                    created = _storage.create_training_plan(
                        {
                            "title": f"Plan hacia {target_race}",
                            "description": "Plan inicial autogenerado por fallback desde objetivo del atleta.",
                            "objective": str(target_race),
                            "difficulty": "moderate",
                            "duration_weeks": 0,
                            "status": "active",
                            "source": "agent_goal_fallback",
                            "plan_data": {
                                "target_race": target_race,
                                "target_race_date": target_date,
                                "created_at": date.today().isoformat(),
                            },
                        },
                        sessions=None,
                        change_reason="auto_fallback_from_goal",
                    )
                    db_plan = _normalize_storage_plan_row(created)
                    if db_plan:
                        db_plan.setdefault("target_race", target_race)
                        db_plan.setdefault("target_race_date", target_date)
                        self.user_profile["training_plan"] = db_plan
                    else:
                        self.user_profile["training_plan"] = {
                            "active": True,
                            "status": "active",
                            "source": "agent_goal_fallback",
                            "title": f"Plan hacia {target_race}",
                            "target_race": target_race,
                            "target_race_date": target_date,
                            "created_at": date.today().isoformat(),
                        }
                    _save_user_profile(self.user_profile)
                except Exception:
                    pass

            # Guardar en historial de conversación
            self.conversation_history.append({"role": "user", "content": user_message})
            self.conversation_history.append({"role": "assistant", "content": assistant_reply})

            # Guardar en memoria persistente
            _save_history_entry("user", user_message)
            _save_history_entry("assistant", assistant_reply)

            return assistant_reply

    async def generate_session_summary(self) -> str:
        """Genera un resumen compacto de la sesión actual usando el LLM."""
        if not self.conversation_history:
            return ""
        # Tomar los últimos 30 mensajes para el resumen (evitar contexto excesivo)
        history_text = "\n".join(
            f"{msg['role'].upper()}: {msg['content'][:600]}"
            for msg in self.conversation_history[-30:]
            if msg.get("content")
        )
        summary_prompt = (
            "Resume en MÁXIMO 250 palabras los puntos clave de esta sesión de entrenamiento. "
            "Incluye: métricas destacadas (HRV, VO₂max, sueño, estrés…), hallazgos importantes, "
            "recomendaciones dadas al deportista, y cualquier dato personal relevante que deba "
            "recordarse en futuras sesiones. Sé conciso y factual, sin saludos ni introducciones.\n\n"
            f"CONVERSACIÓN:\n{history_text}"
        )
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": summary_prompt}],
            )
            # Track and update token usage for OpenAI keys
            if getattr(response, "usage", None) and getattr(self, "_api_key", None):
                u = response.usage
                p_toks = getattr(u, "prompt_tokens", 0) or 0
                c_toks = getattr(u, "completion_tokens", 0) or 0
                update_gemini_daily_usage(self._api_key, p_toks + c_toks)
            return (response.choices[0].message.content or "").strip()
        except Exception:
            # Fallback: resumen básico con los temas del usuario
            topics = [
                msg["content"][:80]
                for msg in self.conversation_history
                if msg.get("role") == "user" and msg.get("content")
            ]
            return f"Temas tratados: {' | '.join(topics[:5])}"

    def save_session_summary(self, summary: str) -> None:
        """Persiste el resumen de sesión en disco."""
        if summary:
            _persist_session_summary(summary)

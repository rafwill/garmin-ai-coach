"""
trainer_agent.py
Agente entrenador personal que combina OpenAI con las herramientas
de Garmin Connect a través del servidor MCP.
"""

import os
import logging
import ssl
import json
import asyncio
import hashlib
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
import truststore
from openai import AsyncOpenAI
from mcp import ClientSession

from agent.mcp_client import list_available_tools, call_tool
from agent import storage as _storage


PROMPTS_DIR = Path(__file__).parent.parent / "prompts"
MEMORY_DIR  = Path(__file__).parent.parent / "memory"  # mantenido por compatibilidad


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
# existentes en main.py y los tests. Toda la lógica (Supabase + fallback JSON)
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


def _save_session_context(ctx: dict) -> None:
    """Guarda el contexto de sesiones."""
    _storage.save_session_context(ctx)


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


def _compact_personal_records(data: list) -> str:
    """Convierte los récords personales de Garmin a un formato compacto y legible.
    Transforma el campo `value` (unidades raw de Garmin: segundos para tiempos, metros para distancias/alturas, pasos, días)
    al formato adecuado directamente en Python, facilitando la interpretación por el LLM.
    """
    results = []
    for record in data:
        if not isinstance(record, dict):
            continue
        type_id = record.get("typeId")
        value = record.get("value")
        meta = _PR_METADATA.get(type_id)
        
        if meta:
            tipo_name = meta["tipo"]
            unidad = meta["unidad"]
        else:
            tipo_name = f"typeId={type_id}"
            unidad = "valor"
            
        entry: dict = {
            "actividad": record.get("activityName", ""),
            "tipo": tipo_name,
            "deporte": record.get("activityType", ""),
        }
        
        if value is not None:
            try:
                v_float = float(value)
                if unidad == "tiempo":
                    entry["tiempo"] = _seconds_to_hhmmss(v_float)
                elif unidad == "distancia_km":
                    entry["distancia"] = f"{v_float / 1000:.2f} km"
                elif unidad == "distancia_m_y_km":
                    if v_float >= 1000:
                        entry["distancia"] = f"{v_float / 1000:.2f} km"
                    else:
                        entry["distancia"] = f"{v_float:.0f} m"
                elif unidad == "elevacion_m":
                    entry["elevacion"] = f"{v_float:.1f} m"
                elif unidad == "pasos":
                    entry["pasos"] = f"{int(round(v_float)):,}"
                elif unidad == "dias":
                    entry["racha"] = f"{int(round(v_float))} días"
                else:
                    entry["valor"] = value
            except (ValueError, TypeError):
                entry["valor"] = value
                
        results.append(entry)
    return json.dumps(results, ensure_ascii=False, separators=(",", ":"))


def _compact_tool_result(raw: str | None, tool_name: str = "") -> str:
    """
    Compacta el resultado de una herramienta para que quepa en el contexto.
    - get_personal_records: conversión específica de segundos a HH:MM:SS.
    - Arrays JSON: conserva hasta 8 elementos y elimina campos metadata.
    - Strings demasiado largos: trunca a _MAX_TOOL_RESULT_CHARS.
    """
    if not raw:
        return "(sin datos)"
    try:
        data = json.loads(raw)
        # Procesado específico para récords personales
        if tool_name == "get_personal_records" and isinstance(data, list):
            return _compact_personal_records(data)
        # Añadir campos normalizados útiles para análisis de actividades
        if tool_name == "get_activity" and isinstance(data, dict):
            duration = data.get("duration") or data.get("movingDuration")
            distance = data.get("distance")
            try:
                if duration is not None:
                    data["duration_hhmmss"] = _seconds_to_hhmmss(float(duration))
            except (ValueError, TypeError):
                pass
            try:
                if distance is not None:
                    data["distance_km"] = round(float(distance) / 1000, 2)
            except (ValueError, TypeError):
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
                    print(f"  [debug] Gemini ocupado ({e}). Reintentando en {current_delay:.1f}s...")
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
                raw = await call_tool(mcp_session, tool_name, {"date": date_iso})
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

    if text in _TODAY_KEYWORDS:
        return date.today().isoformat()
    if text in _YESTERDAY_KEYWORDS:
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
    """Obtiene la fecha ISO de una actividad Garmin a partir de sus campos de inicio."""
    if not isinstance(activity, dict):
        return None

    for key in ("startTimeLocal", "startTimeGMT", "startTimeUTC", "startTime", "calendarDate"):
        value = activity.get(key)
        if isinstance(value, str) and len(value) >= 10:
            date_str = value[:10]
            try:
                return date.fromisoformat(date_str).isoformat()
            except ValueError:
                continue
    return None


async def _find_activity_id_by_date(mcp_session: ClientSession, target_date_iso: str) -> int | None:
    """Busca en actividades recientes el activity_id correspondiente a una fecha ISO."""
    start = 0
    limit = 100
    max_pages = 6  # hasta 600 actividades recientes

    for _ in range(max_pages):
        raw = await call_tool(mcp_session, "get_activities", {"start": str(start), "limit": str(limit)})
        data = json.loads(raw) if raw and raw.strip().startswith("{") else {}
        activities = data.get("activities", []) if isinstance(data, dict) else []

        for activity in activities:
            if _extract_activity_date_iso(activity) != target_date_iso:
                continue

            activity_id = activity.get("activityId") or activity.get("activity_id") or activity.get("id")
            try:
                return int(activity_id)
            except (TypeError, ValueError):
                continue

        has_more = bool(data.get("has_more")) if isinstance(data, dict) else False
        if not has_more:
            break
        start = int(data.get("next_start", start + limit))

    return None


async def _find_activity_id_by_name(mcp_session: ClientSession, name_hint: str) -> int | None:
    """Busca activity_id por nombre aproximado en actividades recientes."""
    hint = (name_hint or "").strip().lower()
    if not hint:
        return None

    start = 0
    limit = 100
    max_pages = 6  # hasta 600 actividades recientes

    for _ in range(max_pages):
        raw = await call_tool(mcp_session, "get_activities", {"start": str(start), "limit": str(limit)})
        data = json.loads(raw) if raw and raw.strip().startswith("{") else {}
        activities = data.get("activities", []) if isinstance(data, dict) else []

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

            if score > best_score:
                activity_id = activity.get("activityId") or activity.get("activity_id") or activity.get("id")
                try:
                    best_id = int(activity_id)
                    best_score = score
                except (TypeError, ValueError):
                    continue

        if best_id is not None and best_score >= 70:
            return best_id

        has_more = bool(data.get("has_more")) if isinstance(data, dict) else False
        if not has_more:
            break
        start = int(data.get("next_start", start + limit))

    return None


async def _normalize_get_activity_args(mcp_session: ClientSession, arguments: dict) -> dict:
    """Normaliza argumentos de get_activity.

    Acepta activity_id numérico o fechas en lenguaje natural/ISO y resuelve el
    ID automáticamente consultando get_activities cuando sea necesario.
    """
    if not isinstance(arguments, dict):
        return {}

    args = dict(arguments)
    candidate = args.get("activity_id")
    if candidate is None:
        candidate = args.get("activityId")
    if candidate is None:
        candidate = args.get("id")
    if candidate is None:
        candidate = args.get("date")

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
    return args


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
        self.tools_schema = _build_tools_schema(tools)

        # Restaurar los últimos 6 mensajes del historial (de session_context.json)
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
                raw = await call_tool(self.mcp_session, "get_body_composition", {"date": today})
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

        return self.system_prompt + date_context + profile_context + memory_context

    def _build_messages(self, user_message: str) -> list[dict]:
        """Construye el array de mensajes para la llamada al LLM.
        Limita el historial a los últimos 6 turnos (3 pares user/assistant)
        para mantener el contexto razonable sin consumir tokens innecesarios.
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

        _MAX_TOOL_ITER = 15
        iteration = 0
        while True:
            iteration += 1
            if iteration > _MAX_TOOL_ITER:
                print(f"  [debug] Límite de {_MAX_TOOL_ITER} iteraciones de herramientas alcanzado. Abortando.")
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
                    from agent.storage import mark_gemini_quota_exhausted
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
                print(f"  [debug] Tokens - Entrada: {p_toks} | Salida: {c_toks} | Total paso: {total_step_tokens}")
                if getattr(self, "_api_key", None):
                    update_gemini_daily_usage(self._api_key, total_step_tokens)

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
                    # json.loads("null") devuelve None → las tools MCP
                    # esperan un objeto, no null → convertir a {}
                    if arguments is None:
                        arguments = {}
                    # Normalizar fechas: convertir palabras como 'hoy'/'ayer' a ISO
                    arguments = _normalize_date_args(arguments)
                    if tool_name == "get_activity":
                        arguments = await _normalize_get_activity_args(self.mcp_session, arguments)

                    print(f"  [debug] Ejecutando: {tool_name}({arguments})")
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
                            print("  [debug] Training readiness sin datos; usando snapshot alternativo de recuperación")
                            raw_result = fallback_snapshot

                    tool_result = _compact_tool_result(raw_result, tool_name)
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

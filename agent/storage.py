"""
agent/storage.py
Capa de persistencia unificada para GarminCoach.

Usa Supabase (PostgreSQL en la nube) si SUPABASE_URL + SUPABASE_ANON_KEY
están configurados en .env. Siempre escribe también en ficheros JSON locales
como copia de seguridad offline.

Si Supabase no está configurado o falla en una lectura, los ficheros JSON son
la fuente de verdad (comportamiento idéntico a antes de esta migración).

Tablas Supabase necesarias:
    user_profile    — perfil personal, objetivos y salud del deportista
    session_context — historial de conversación y resúmenes de sesiones
    gemini_usage    — uso diario de tokens por API key (clave hasheada)

Ver supabase/schema.sql para el DDL completo.
"""

import hashlib
import json
import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

# ─── Rutas de ficheros locales ────────────────────────────────────────────────
# Se usan siempre como backup, independientemente de si Supabase está activo.
_MEMORY_DIR   = Path(__file__).parent.parent / "memory"
_PROFILE_FILE = _MEMORY_DIR / "user_profile.json"
_CONTEXT_FILE = _MEMORY_DIR / "session_context.json"
_GEMINI_FILE  = _MEMORY_DIR / "gemini_daily_usage.json"

_MEMORY_DIR.mkdir(parents=True, exist_ok=True)


# ─── Detección de Zscaler (proxy SSL corporativo) ────────────────────────────

_zscaler_cache: bool | None = None  # None = no comprobado todavía


def is_zscaler_network() -> bool:
    """
    Detecta si el tráfico sale a través de Zscaler (proxy SSL corporativo).
    Dos firmas posibles:
      1. Zscaler bloquea la URL → respuesta 4xx con "Zscaler" en el cuerpo HTML.
      2. Zscaler intercepta SSL sin bloquear → error "CERTIFICATE_VERIFY_FAILED"
         porque inyecta su propio certificado raíz (no confiado por Python).
    El resultado se cachea para no repetir la sonda en cada llamada.
    """
    global _zscaler_cache
    if _zscaler_cache is not None:
        return _zscaler_cache
    try:
        with httpx.Client(timeout=4.0, follow_redirects=False, verify=True) as client:
            resp = client.get(
                "https://generativelanguage.googleapis.com/v1beta/models",
                headers={"x-goog-api-key": "probe"},
            )
            body = resp.text or ""
            _zscaler_cache = "Zscaler" in body or "zscaler" in body.lower()
    except Exception as e:
        err = str(e)
        # Firma 1: nombre de Zscaler en el mensaje de error
        # Firma 2: Python no puede verificar el certificado porque Zscaler
        #          inyecta su propia CA raíz (no instalada en el bundle de Python)
        _zscaler_cache = (
            "Zscaler" in err
            or "zscaler" in err.lower()
            or "CERTIFICATE_VERIFY_FAILED" in err
            or "unable to get local issuer certificate" in err
        )
    log.debug("[storage] Zscaler detectado: %s", _zscaler_cache)
    return _zscaler_cache


# ─── Cliente Supabase (singleton, inicialización lazy) ───────────────────────

_sb       = None   # instancia del cliente supabase
_sb_ready = False  # True después del primer intento (evita reintentos en cada llamada)


def _supabase():
    """Devuelve el cliente Supabase si está configurado, o None en caso contrario."""
    global _sb, _sb_ready
    if _sb_ready:
        return _sb
    _sb_ready = True

    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_ANON_KEY", "").strip()

    # Detectar placeholder del .env.example
    if not url or not key or "xxx" in url:
        return None

    try:
        if is_zscaler_network():
            # Zscaler intercepta el SSL corporativo — usar el almacén de
            # certificados del sistema (truststore) para que httpx confíe
            import truststore
            truststore.inject_into_ssl()
            log.debug("[storage] Supabase: truststore activado (Zscaler detectado)")
        from supabase import create_client
        _sb = create_client(url, key)
        log.info("[storage] Supabase conectado: %s", url)
    except Exception as exc:
        log.warning("[storage] Supabase no disponible, usando ficheros locales: %s", exc)
    return _sb


def _garmin_uid() -> str:
    """Identificador único del usuario basado en el email de Garmin (SHA-256[:16])."""
    email = os.environ.get("GARMIN_EMAIL", "").strip().lower()
    return hashlib.sha256(email.encode()).hexdigest()[:16] if email else "unknown"


# ─── Helpers de fichero ───────────────────────────────────────────────────────

def _read_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return default


def _write_json(path: Path, data) -> None:
    _MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        log.warning("[storage] No se pudo escribir %s: %s", path.name, exc)


# ─── user_profile ─────────────────────────────────────────────────────────────

def load_user_profile() -> dict:
    """Carga el perfil del usuario (datos personales, objetivos, salud)."""
    sb = _supabase()
    if sb:
        try:
            uid = _garmin_uid()
            res = sb.table("user_profile").select("data").eq("garmin_user_id", uid).execute()
            if res.data:
                return res.data[0].get("data") or {}
        except Exception as exc:
            log.warning("[storage] load_user_profile Supabase error: %s. Fallback a fichero.", exc)
    return _read_json(_PROFILE_FILE, {})


def save_user_profile(profile: dict) -> None:
    """Guarda el perfil del usuario. Escribe en fichero siempre; también en Supabase si disponible."""
    _write_json(_PROFILE_FILE, profile)
    sb = _supabase()
    if sb:
        try:
            sb.table("user_profile").upsert({
                "garmin_user_id": _garmin_uid(),
                "data":           profile,
            }).execute()
        except Exception as exc:
            log.warning("[storage] save_user_profile Supabase error: %s.", exc)


# ─── session_context ──────────────────────────────────────────────────────────

def load_session_context() -> dict:
    """Carga el contexto de sesiones (historial de mensajes y resúmenes)."""
    sb = _supabase()
    if sb:
        try:
            uid = _garmin_uid()
            res = sb.table("session_context") \
                    .select("history,session_summaries") \
                    .eq("garmin_user_id", uid) \
                    .execute()
            if res.data:
                row = res.data[0]
                return {
                    "history":           row.get("history") or [],
                    "session_summaries": row.get("session_summaries") or [],
                }
            return {"history": [], "session_summaries": []}
        except Exception as exc:
            log.warning("[storage] load_session_context Supabase error: %s. Fallback a fichero.", exc)
    return _read_json(_CONTEXT_FILE, {"history": [], "session_summaries": []})


def save_session_context(ctx: dict) -> None:
    """Guarda el contexto de sesiones. Escribe en fichero siempre; también en Supabase si disponible."""
    _write_json(_CONTEXT_FILE, ctx)
    sb = _supabase()
    if sb:
        try:
            sb.table("session_context").upsert({
                "garmin_user_id":    _garmin_uid(),
                "history":           ctx.get("history", []),
                "session_summaries": ctx.get("session_summaries", []),
            }).execute()
        except Exception as exc:
            log.warning("[storage] save_session_context Supabase error: %s.", exc)


def save_history_entry(role: str, content: str) -> None:
    """Añade una entrada al historial de conversación persistente."""
    ctx = load_session_context()
    ctx.setdefault("history", []).append({"role": role, "content": content})
    ctx["history"] = ctx["history"][-50:]  # últimas 50 entradas
    save_session_context(ctx)


def load_session_summaries() -> list[dict]:
    """Carga los resúmenes de sesiones anteriores."""
    return load_session_context().get("session_summaries", [])


def persist_session_summary(summary: str) -> None:
    """Guarda el resumen de la sesión actual en el contexto persistente."""
    ctx = load_session_context()
    summaries = ctx.get("session_summaries", [])
    summaries.append({"date": date.today().isoformat(), "summary": summary[:600]})
    ctx["session_summaries"] = summaries[-10:]  # últimos 10 resúmenes
    save_session_context(ctx)


# ─── gemini_usage ─────────────────────────────────────────────────────────────

def _key_hash(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12]


def get_gemini_daily_usage(api_key: str) -> int:
    """Obtiene los tokens consumidos hoy para una API key específica."""
    if not api_key:
        return 0
    kh    = _key_hash(api_key)
    today = date.today().isoformat()
    sb = _supabase()
    if sb:
        try:
            res = sb.table("gemini_usage") \
                    .select("tokens") \
                    .eq("key_hash", kh) \
                    .eq("usage_date", today) \
                    .execute()
            if res.data:
                return res.data[0].get("tokens", 0)
            return 0
        except Exception as exc:
            log.warning("[storage] get_gemini_daily_usage Supabase error: %s. Fallback a fichero.", exc)
    # Fallback a fichero
    data     = _read_json(_GEMINI_FILE, {})
    day_data = data.get(kh, {}).get(today, 0)
    return day_data.get("tokens", 0) if isinstance(day_data, dict) else day_data


def update_gemini_daily_usage(api_key: str, tokens: int) -> int:
    """Actualiza y devuelve los tokens acumulados hoy para una API key específica."""
    if not api_key or tokens <= 0:
        return get_gemini_daily_usage(api_key)
    kh        = _key_hash(api_key)
    today     = date.today().isoformat()
    new_total = get_gemini_daily_usage(api_key) + tokens
    sb = _supabase()
    if sb:
        try:
            sb.table("gemini_usage").upsert({
                "key_hash":   kh,
                "usage_date": today,
                "tokens":     new_total,
            }).execute()
        except Exception as exc:
            log.warning("[storage] update_gemini_daily_usage Supabase error: %s.", exc)
    _update_gemini_file(api_key, new_total, quota_exhausted=False)
    return new_total


def mark_gemini_quota_exhausted(api_key: str) -> None:
    """Marca la API key como agotada por cuota para el día de hoy."""
    if not api_key:
        return
    kh     = _key_hash(api_key)
    today  = date.today().isoformat()
    tokens = max(get_gemini_daily_usage(api_key), 1_000_000)
    sb = _supabase()
    if sb:
        try:
            sb.table("gemini_usage").upsert({
                "key_hash":        kh,
                "usage_date":      today,
                "tokens":          tokens,
                "quota_exhausted": True,
            }).execute()
        except Exception as exc:
            log.warning("[storage] mark_gemini_quota_exhausted Supabase error: %s.", exc)
    _update_gemini_file(api_key, tokens, quota_exhausted=True)


def is_gemini_quota_exhausted(api_key: str) -> bool:
    """Devuelve True si la API key está marcada como agotada por cuota hoy."""
    if not api_key:
        return False
    kh    = _key_hash(api_key)
    today = date.today().isoformat()
    sb = _supabase()
    if sb:
        try:
            res = sb.table("gemini_usage") \
                    .select("quota_exhausted") \
                    .eq("key_hash", kh) \
                    .eq("usage_date", today) \
                    .execute()
            if res.data:
                return bool(res.data[0].get("quota_exhausted", False))
            return False
        except Exception as exc:
            log.warning("[storage] is_gemini_quota_exhausted Supabase error: %s.", exc)
    # Fallback a fichero
    data     = _read_json(_GEMINI_FILE, {})
    day_data = data.get(kh, {}).get(today, {})
    return bool(day_data.get("quota_exhausted", False)) if isinstance(day_data, dict) else False


def _update_gemini_file(api_key: str, tokens: int, quota_exhausted: bool) -> None:
    """Actualiza el fichero local de uso de Gemini."""
    kh        = _key_hash(api_key)
    today_str = date.today().isoformat()
    data      = _read_json(_GEMINI_FILE, {})
    data.setdefault(kh, {})[today_str] = {"tokens": tokens, "quota_exhausted": quota_exhausted}
    # Limpiar entradas con más de 30 días de antigüedad
    try:
        cutoff = (datetime.now() - timedelta(days=30)).date().isoformat()
        for kh2 in list(data.keys()):
            for d_str in list(data.get(kh2, {}).keys()):
                if d_str < cutoff:
                    del data[kh2][d_str]
    except Exception:
        pass
    _write_json(_GEMINI_FILE, data)

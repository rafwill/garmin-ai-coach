"""
agent/storage.py
Capa de persistencia multiusuario (DB-first) para GarminCoach.

Este módulo usa Supabase como fuente de verdad para:
- app_user (autenticación local de la app)
- user_profile
- session_context
- athlete_knowledge
- gemini_usage
"""

import hashlib
import hmac
import logging
import os
from datetime import date
from uuid import uuid4

import httpx

log = logging.getLogger(__name__)


_zscaler_cache: bool | None = None
_sb = None
_sb_ready = False
_active_user_id: str | None = None
_active_username: str | None = None


def set_active_user(user_id: str | None, username: str | None = None) -> None:
    """Define el usuario activo para escopar lecturas/escrituras."""
    global _active_user_id, _active_username
    _active_user_id = (user_id or "").strip() or None
    _active_username = (username or "").strip().lower() or None


def get_active_user() -> dict:
    """Devuelve metadatos del usuario activo en esta ejecución."""
    return {
        "user_id": _active_user_id,
        "username": _active_username,
    }


def _require_active_user_id() -> str:
    if not _active_user_id:
        raise RuntimeError("No hay usuario activo en sesión")
    return _active_user_id


def is_zscaler_network() -> bool:
    """Detecta si el tráfico sale por proxy SSL corporativo Zscaler."""
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
            _zscaler_cache = "zscaler" in body.lower()
    except Exception as exc:
        err = str(exc).lower()
        _zscaler_cache = (
            "zscaler" in err
            or "certificate_verify_failed" in err
            or "unable to get local issuer certificate" in err
        )

    return _zscaler_cache


def _supabase():
    """Devuelve el cliente Supabase si está configurado, o None."""
    global _sb, _sb_ready
    if _sb_ready:
        return _sb
    _sb_ready = True

    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_ANON_KEY", "").strip()

    if not url or not key or "xxx" in url.lower():
        return None

    try:
        if is_zscaler_network():
            import truststore

            truststore.inject_into_ssl()
        from supabase import create_client

        _sb = create_client(url, key)
        return _sb
    except Exception as exc:
        log.warning("[storage] Supabase no disponible: %s", exc)
        return None


def _require_supabase():
    sb = _supabase()
    if not sb:
        raise RuntimeError("Supabase no está configurado o no es accesible")
    return sb


def _normalize_username(username: str) -> str:
    return (username or "").strip().lower()


def _sanitize_credentials_for_storage(credentials: dict | None) -> dict:
    """Elimina secretos que no deben persistirse en BBDD."""
    creds = dict(credentials or {})
    creds.pop("garmin_password", None)
    return creds


def _pbkdf2_hash(password: str, salt_hex: str | None = None) -> str:
    salt = bytes.fromhex(salt_hex) if salt_hex else os.urandom(16)
    iterations = 120_000
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        algo, _iters, salt_hex, _digest_hex = stored.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        candidate = _pbkdf2_hash(password, salt_hex=salt_hex)
        return hmac.compare_digest(candidate, stored)
    except Exception:
        return False


def register_app_user(username: str, password: str, credentials: dict | None = None) -> dict:
    """Crea un usuario de app y guarda password hasheada en Supabase."""
    uname = _normalize_username(username)
    if not uname or len(uname) < 3:
        return {"ok": False, "user_id": None, "error": "Usuario inválido"}
    if not password or len(password) < 6:
        return {"ok": False, "user_id": None, "error": "Password demasiado corta (mínimo 6)"}

    user_id = uuid4().hex
    password_hash = _pbkdf2_hash(password)
    creds = _sanitize_credentials_for_storage(credentials)

    try:
        sb = _require_supabase()
        existing = sb.table("app_user").select("id").eq("username", uname).limit(1).execute()
        if existing.data:
            return {"ok": False, "user_id": None, "error": "El usuario ya existe"}

        sb.table("app_user").insert(
            {
                "id": user_id,
                "username": uname,
                "password_hash": password_hash,
                "credentials": creds,
            }
        ).execute()
        return {"ok": True, "user_id": user_id, "error": None}
    except Exception as exc:
        return {"ok": False, "user_id": None, "error": str(exc)}


def authenticate_app_user(username: str, password: str) -> dict:
    """Valida usuario/password contra Supabase."""
    uname = _normalize_username(username)
    if not uname or not password:
        return {"ok": False, "user_id": None, "credentials": {}, "error": "Credenciales incompletas"}

    try:
        sb = _require_supabase()
        res = sb.table("app_user").select("id,password_hash,credentials").eq("username", uname).limit(1).execute()
        if not res.data:
            return {"ok": False, "user_id": None, "credentials": {}, "error": "Usuario no encontrado"}

        row = res.data[0]
        if not _verify_password(password, row.get("password_hash", "")):
            return {"ok": False, "user_id": None, "credentials": {}, "error": "Password incorrecta"}

        return {
            "ok": True,
            "user_id": row.get("id"),
            "credentials": row.get("credentials") or {},
            "error": None,
        }
    except Exception as exc:
        return {"ok": False, "user_id": None, "credentials": {}, "error": str(exc)}


def update_user_credentials(credentials: dict) -> None:
    """Actualiza credenciales auxiliares del usuario activo."""
    uid = _require_active_user_id()
    sb = _require_supabase()
    sb.table("app_user").update({"credentials": _sanitize_credentials_for_storage(credentials)}).eq("id", uid).execute()


def load_user_profile() -> dict:
    uid = _require_active_user_id()
    sb = _require_supabase()
    res = sb.table("user_profile").select("data").eq("app_user_id", uid).limit(1).execute()
    if not res.data:
        return {}
    return res.data[0].get("data") or {}


def save_user_profile(profile: dict) -> None:
    uid = _require_active_user_id()
    sb = _require_supabase()
    sb.table("user_profile").upsert({"app_user_id": uid, "data": profile or {}}).execute()


def load_session_context() -> dict:
    uid = _require_active_user_id()
    sb = _require_supabase()
    res = (
        sb.table("session_context")
        .select("history,session_summaries")
        .eq("app_user_id", uid)
        .limit(1)
        .execute()
    )
    if not res.data:
        return {"history": [], "session_summaries": []}
    row = res.data[0]
    return {
        "history": row.get("history") or [],
        "session_summaries": row.get("session_summaries") or [],
    }


def save_session_context(ctx: dict) -> None:
    uid = _require_active_user_id()
    sb = _require_supabase()
    sb.table("session_context").upsert(
        {
            "app_user_id": uid,
            "history": ctx.get("history", []),
            "session_summaries": ctx.get("session_summaries", []),
        }
    ).execute()


def save_history_entry(role: str, content: str) -> None:
    ctx = load_session_context()
    ctx.setdefault("history", []).append({"role": role, "content": content})
    ctx["history"] = ctx["history"][-50:]
    save_session_context(ctx)


def load_session_summaries() -> list[dict]:
    return load_session_context().get("session_summaries", [])


def persist_session_summary(summary: str) -> None:
    ctx = load_session_context()
    summaries = ctx.get("session_summaries", [])
    summaries.append({"date": date.today().isoformat(), "summary": (summary or "")[:600]})
    ctx["session_summaries"] = summaries[-10:]
    save_session_context(ctx)


def load_athlete_knowledge() -> str:
    uid = _require_active_user_id()
    sb = _require_supabase()
    res = sb.table("athlete_knowledge").select("content").eq("app_user_id", uid).limit(1).execute()
    if not res.data:
        return ""
    return (res.data[0].get("content") or "").strip()


def save_athlete_knowledge(content: str) -> None:
    uid = _require_active_user_id()
    sb = _require_supabase()
    sb.table("athlete_knowledge").upsert({"app_user_id": uid, "content": (content or "").strip()}).execute()


def _key_hash(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12]


def get_gemini_daily_usage(api_key: str) -> int:
    if not api_key:
        return 0
    uid = _require_active_user_id()
    sb = _require_supabase()
    kh = _key_hash(api_key)
    today = date.today().isoformat()
    res = (
        sb.table("gemini_usage")
        .select("tokens")
        .eq("app_user_id", uid)
        .eq("key_hash", kh)
        .eq("usage_date", today)
        .limit(1)
        .execute()
    )
    if not res.data:
        return 0
    return int(res.data[0].get("tokens", 0) or 0)


def update_gemini_daily_usage(api_key: str, tokens: int) -> int:
    if not api_key or tokens <= 0:
        return get_gemini_daily_usage(api_key)
    uid = _require_active_user_id()
    sb = _require_supabase()
    kh = _key_hash(api_key)
    today = date.today().isoformat()
    new_total = get_gemini_daily_usage(api_key) + tokens
    sb.table("gemini_usage").upsert(
        {
            "app_user_id": uid,
            "key_hash": kh,
            "usage_date": today,
            "tokens": new_total,
        }
    ).execute()
    return new_total


def mark_gemini_quota_exhausted(api_key: str) -> None:
    if not api_key:
        return
    uid = _require_active_user_id()
    sb = _require_supabase()
    kh = _key_hash(api_key)
    today = date.today().isoformat()
    tokens = max(get_gemini_daily_usage(api_key), 1_000_000)
    sb.table("gemini_usage").upsert(
        {
            "app_user_id": uid,
            "key_hash": kh,
            "usage_date": today,
            "tokens": tokens,
            "quota_exhausted": True,
        }
    ).execute()


def is_gemini_quota_exhausted(api_key: str) -> bool:
    if not api_key:
        return False
    uid = _require_active_user_id()
    sb = _require_supabase()
    kh = _key_hash(api_key)
    today = date.today().isoformat()
    res = (
        sb.table("gemini_usage")
        .select("quota_exhausted")
        .eq("app_user_id", uid)
        .eq("key_hash", kh)
        .eq("usage_date", today)
        .limit(1)
        .execute()
    )
    if not res.data:
        return False
    return bool(res.data[0].get("quota_exhausted", False))


def check_supabase_connection() -> dict:
    """Comprueba conectividad con Supabase haciendo una query real."""
    global _sb, _sb_ready

    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_ANON_KEY", "").strip()

    if not url or not key or "xxx" in url.lower():
        return {"configured": False, "connected": False, "error": None}

    _sb_ready = False
    _sb = None
    sb = _supabase()
    if sb is None:
        return {"configured": True, "connected": False, "error": "No se pudo crear el cliente Supabase"}

    try:
        sb.table("app_user").select("id").limit(1).execute()
        return {"configured": True, "connected": True, "error": None}
    except Exception as exc:
        return {"configured": True, "connected": False, "error": str(exc)}

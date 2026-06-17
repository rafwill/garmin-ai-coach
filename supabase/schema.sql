-- =============================================================================
-- GarminCoach — Supabase Schema
-- =============================================================================
-- Ejecutar en: Supabase Dashboard → SQL Editor → New query → Run
--
-- Crea las 3 tablas necesarias para la persistencia del agente y desactiva
-- RLS (esta es una aplicación personal de un único usuario con anon key).
-- =============================================================================


-- ─── user_profile ─────────────────────────────────────────────────────────────
-- Almacena el perfil del deportista: datos personales, objetivos y salud.
-- La clave es el garmin_user_id (SHA-256[:16] del GARMIN_EMAIL del .env).

create table if not exists user_profile (
    garmin_user_id text        primary key,
    data           jsonb       not null default '{}',
    updated_at     timestamptz not null default now()
);

-- Desactivar RLS (app personal, acceso con anon key desde un solo dispositivo)
alter table user_profile disable row level security;


-- ─── session_context ──────────────────────────────────────────────────────────
-- Almacena el historial de conversación (últimas 50 entradas) y los resúmenes
-- de sesiones anteriores (últimos 10).

create table if not exists session_context (
    garmin_user_id    text        primary key,
    history           jsonb       not null default '[]',
    session_summaries jsonb       not null default '[]',
    updated_at        timestamptz not null default now()
);

alter table session_context disable row level security;


-- ─── gemini_usage ─────────────────────────────────────────────────────────────
-- Registra el consumo diario de tokens de Gemini por API key (hasheada con
-- SHA-256[:12]). Se usa para respetar el límite gratuito de ~1M tokens/día.

create table if not exists gemini_usage (
    key_hash        text        not null,
    usage_date      date        not null,
    tokens          integer     not null default 0,
    quota_exhausted boolean     not null default false,
    updated_at      timestamptz not null default now(),
    primary key (key_hash, usage_date)
);

alter table gemini_usage disable row level security;


-- ─── Trigger: updated_at automático ──────────────────────────────────────────

create or replace function _set_updated_at()
returns trigger
language plpgsql as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

-- user_profile
drop trigger if exists trg_user_profile_updated_at on user_profile;
create trigger trg_user_profile_updated_at
    before update on user_profile
    for each row execute function _set_updated_at();

-- session_context
drop trigger if exists trg_session_context_updated_at on session_context;
create trigger trg_session_context_updated_at
    before update on session_context
    for each row execute function _set_updated_at();

-- gemini_usage
drop trigger if exists trg_gemini_usage_updated_at on gemini_usage;
create trigger trg_gemini_usage_updated_at
    before update on gemini_usage
    for each row execute function _set_updated_at();

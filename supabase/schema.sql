-- =============================================================================
-- Kairos Coach — Supabase Schema (instalacion limpia)
-- =============================================================================
-- Ejecutar en: Supabase Dashboard -> SQL Editor -> New query -> Run.
--
-- Este script crea el esquema multiusuario desde cero para un proyecto nuevo.
-- No incluye migraciones legacy ni scripts de limpieza.
-- =============================================================================

begin;
set local lock_timeout = '5s';
set local statement_timeout = '120s';

-- ─── Trigger helper ─────────────────────────────────────────────────────────
create or replace function _set_updated_at()
returns trigger
language plpgsql as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

-- ─── app_user ───────────────────────────────────────────────────────────────
create table if not exists app_user (
    id            text        primary key,
    username      text        not null unique,
    password_hash text        not null,
    credentials   jsonb       not null default '{}',
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now()
);

alter table app_user disable row level security;

-- ─── user_profile ───────────────────────────────────────────────────────────
create table if not exists user_profile (
    app_user_id text        primary key references app_user(id) on delete cascade,
    data        jsonb       not null default '{}',
    updated_at  timestamptz not null default now()
);

alter table user_profile disable row level security;

-- ─── session_context ────────────────────────────────────────────────────────
create table if not exists session_context (
    app_user_id       text        primary key references app_user(id) on delete cascade,
    history           jsonb       not null default '[]',
    session_summaries jsonb       not null default '[]',
    updated_at        timestamptz not null default now()
);

alter table session_context disable row level security;

-- ─── athlete_knowledge ──────────────────────────────────────────────────────
create table if not exists athlete_knowledge (
    app_user_id text        primary key references app_user(id) on delete cascade,
    content     text        not null default '',
    updated_at  timestamptz not null default now()
);

alter table athlete_knowledge disable row level security;

-- ─── gemini_usage ───────────────────────────────────────────────────────────
create table if not exists gemini_usage (
    app_user_id     text        not null references app_user(id) on delete cascade,
    key_hash        text        not null,
    usage_date      date        not null,
    tokens          integer     not null default 0,
    quota_exhausted boolean     not null default false,
    updated_at      timestamptz not null default now(),
    primary key (app_user_id, key_hash, usage_date)
);

alter table gemini_usage disable row level security;

-- ─── training_plan (fuente de verdad de planes) ────────────────────────────
create table if not exists training_plan (
    id             text        primary key,
    app_user_id    text        not null references app_user(id) on delete cascade,
    title          text        not null,
    description    text        not null default '',
    objective      text        not null default '',
    difficulty     text        not null default 'moderate',
    duration_weeks integer     not null default 0,
    status         text        not null default 'draft' check (status in ('draft', 'active', 'inactive', 'archived')),
    source         text        not null default 'agent',
    plan_data      jsonb       not null default '{}',
    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now()
);

alter table training_plan disable row level security;

create index if not exists idx_training_plan_user_created
    on training_plan (app_user_id, created_at desc);

create unique index if not exists uq_training_plan_single_active
    on training_plan (app_user_id)
    where status = 'active';

-- ─── training_plan_session ─────────────────────────────────────────────────
create table if not exists training_plan_session (
    id           text        primary key,
    plan_id      text        not null references training_plan(id) on delete cascade,
    week_index   integer     not null default 1,
    day_index    integer     not null default 1,
    session_type text        not null default 'running',
    duration_min integer,
    intensity    text,
    exercises    jsonb       not null default '[]',
    notes        text        not null default '',
    created_at   timestamptz not null default now(),
    updated_at   timestamptz not null default now()
);

alter table training_plan_session disable row level security;

create index if not exists idx_training_plan_session_plan
    on training_plan_session (plan_id, week_index, day_index);

-- ─── training_plan_version ────────────────────────────────────────────────
create table if not exists training_plan_version (
    id             text        primary key,
    plan_id        text        not null references training_plan(id) on delete cascade,
    version_number integer     not null,
    snapshot       jsonb       not null default '{}',
    change_reason  text        not null default '',
    created_at     timestamptz not null default now(),
    unique (plan_id, version_number)
);

alter table training_plan_version disable row level security;

create index if not exists idx_training_plan_version_plan
    on training_plan_version (plan_id, version_number desc);

-- ─── Triggers updated_at ────────────────────────────────────────────────────
drop trigger if exists trg_user_profile_updated_at on user_profile;
create trigger trg_user_profile_updated_at
    before update on user_profile
    for each row execute function _set_updated_at();

drop trigger if exists trg_session_context_updated_at on session_context;
create trigger trg_session_context_updated_at
    before update on session_context
    for each row execute function _set_updated_at();

drop trigger if exists trg_athlete_knowledge_updated_at on athlete_knowledge;
create trigger trg_athlete_knowledge_updated_at
    before update on athlete_knowledge
    for each row execute function _set_updated_at();

drop trigger if exists trg_app_user_updated_at on app_user;
create trigger trg_app_user_updated_at
    before update on app_user
    for each row execute function _set_updated_at();

drop trigger if exists trg_gemini_usage_updated_at on gemini_usage;
create trigger trg_gemini_usage_updated_at
    before update on gemini_usage
    for each row execute function _set_updated_at();

drop trigger if exists trg_training_plan_updated_at on training_plan;
create trigger trg_training_plan_updated_at
    before update on training_plan
    for each row execute function _set_updated_at();

drop trigger if exists trg_training_plan_session_updated_at on training_plan_session;
create trigger trg_training_plan_session_updated_at
    before update on training_plan_session
    for each row execute function _set_updated_at();

commit;

-- =============================================================================
-- Migración 001: tabla load_metrics_daily
-- Ejecutar en Supabase Dashboard → SQL Editor → New query → Run
-- =============================================================================

begin;

-- ─── load_metrics_daily ──────────────────────────────────────────────────────
-- Almacena TSS/ATL/CTL/TSB calculados por día para cada usuario.
-- Permite cálculo incremental: solo recalculamos desde el último registro.
create table if not exists load_metrics_daily (
    app_user_id      text        not null references app_user(id) on delete cascade,
    metric_date      date        not null,
    tss              numeric(10,2) not null default 0,
    atl              numeric(10,2) not null default 0,
    ctl              numeric(10,2) not null default 0,
    tsb              numeric(10,2) not null default 0,
    activities_count integer     not null default 0,
    updated_at       timestamptz not null default now(),
    primary key (app_user_id, metric_date)
);

alter table load_metrics_daily disable row level security;

create index if not exists idx_load_metrics_daily_user_date
    on load_metrics_daily (app_user_id, metric_date desc);

drop trigger if exists trg_load_metrics_daily_updated_at on load_metrics_daily;
create trigger trg_load_metrics_daily_updated_at
    before update on load_metrics_daily
    for each row execute function _set_updated_at();

commit;

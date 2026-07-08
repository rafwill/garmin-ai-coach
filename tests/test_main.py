"""
tests/test_main.py
Suite de tests unitarios para las funciones puras de main.py.

Cubre:
  - _validate_date
  - _validate_time
  - _validate_hours
  - _is_first_time
"""

from datetime import date, timedelta
from unittest.mock import patch

import pytest

from agent.main import (
    _auto_select_provider,
    _build_enriched_athlete_knowledge,
    _ensure_garmin_credentials,
    _format_coach_markdown,
    _is_first_time,
    _parse_plan_command,
    _validate_date,
    _validate_hours,
    _validate_time,
)


# ─── _validate_date ───────────────────────────────────────────────────────────

class TestValidateDate:
    def test_valid_future_date(self):
        future = (date.today() + timedelta(days=30)).isoformat()
        ok, err = _validate_date(future)
        assert ok
        assert err == ""

    def test_wrong_format_slash(self):
        ok, err = _validate_date("17/06/2026")
        assert not ok
        assert "YYYY-MM-DD" in err

    def test_wrong_format_no_separator(self):
        ok, err = _validate_date("20260617")
        assert not ok

    def test_past_date_rejected(self):
        ok, err = _validate_date("2020-01-01")
        assert not ok
        assert "futura" in err

    def test_today_rejected(self):
        ok, err = _validate_date(date.today().isoformat())
        assert not ok
        assert "futura" in err

    def test_invalid_month_13(self):
        ok, err = _validate_date("2027-13-01")
        assert not ok
        assert "inválida" in err

    def test_invalid_day_32(self):
        ok, err = _validate_date("2027-01-32")
        assert not ok
        assert "inválida" in err

    def test_far_future_valid(self):
        ok, err = _validate_date("2099-12-31")
        assert ok

    def test_empty_string_rejected(self):
        ok, err = _validate_date("")
        assert not ok


# ─── _validate_time ───────────────────────────────────────────────────────────

class TestValidateTime:
    def test_valid_h_mm_ss(self):
        ok, err = _validate_time("9:30:00")
        assert ok
        assert err == ""

    def test_valid_hh_mm_ss(self):
        ok, err = _validate_time("13:45:30")
        assert ok
        assert err == ""

    def test_invalid_format_letters(self):
        ok, err = _validate_time("1h30m")
        assert not ok

    def test_invalid_format_no_seconds(self):
        ok, err = _validate_time("01:30")
        assert not ok

    def test_minutes_over_59(self):
        ok, err = _validate_time("1:60:00")
        assert not ok
        assert "Minutos" in err

    def test_seconds_over_59(self):
        ok, err = _validate_time("1:00:60")
        assert not ok
        assert "Minutos" in err  # mensaje incluye "Minutos y segundos"

    def test_boundary_minutes_59_valid(self):
        ok, _ = _validate_time("1:59:59")
        assert ok

    def test_three_digit_hours_valid(self):
        ok, err = _validate_time("99:59:59")
        assert ok

    def test_hours_over_99_rejected(self):
        ok, err = _validate_time("100:00:00")
        assert not ok
        assert "Horas" in err

    def test_zero_time_valid(self):
        ok, _ = _validate_time("0:00:00")
        assert ok


# ─── _validate_hours ─────────────────────────────────────────────────────────

class TestValidateHours:
    def test_valid_integer(self):
        ok, err = _validate_hours("10")
        assert ok
        assert err == ""

    def test_valid_decimal_dot(self):
        ok, err = _validate_hours("10.5")
        assert ok

    def test_valid_decimal_comma(self):
        # La función acepta coma como separador decimal
        ok, err = _validate_hours("10,5")
        assert ok

    def test_too_low(self):
        ok, err = _validate_hours("0.1")
        assert not ok
        assert "mínimo" in err

    def test_too_high(self):
        ok, err = _validate_hours("41")
        assert not ok
        assert "máximo" in err

    def test_not_a_number(self):
        ok, err = _validate_hours("mucho")
        assert not ok
        assert "número" in err

    def test_boundary_min_valid(self):
        ok, _ = _validate_hours("0.5")
        assert ok

    def test_boundary_max_valid(self):
        ok, _ = _validate_hours("40")
        assert ok

    def test_negative_rejected(self):
        ok, err = _validate_hours("-5")
        assert not ok
        assert "mínimo" in err

    def test_empty_string_rejected(self):
        ok, err = _validate_hours("")
        assert not ok
        assert "número" in err


# ─── _is_first_time ──────────────────────────────────────────────────────────

class TestIsFirstTime:
    """_is_first_time() depende solo de setup_complete en el perfil."""

    def test_no_profile_returns_true(self):
        with patch("agent.main._load_user_profile", return_value={}):
            assert _is_first_time() is True

    def test_setup_complete_false_returns_true(self):
        with patch("agent.main._load_user_profile", return_value={"setup_complete": False}):
            assert _is_first_time() is True

    def test_setup_complete_true_returns_false(self):
        with patch("agent.main._load_user_profile", return_value={"setup_complete": True}):
            assert _is_first_time() is False

    def test_ignores_legacy_garmin_uid_field(self):
        with patch("agent.main._load_user_profile", return_value={
            "setup_complete": True,
            "garmin_user_id": "legacy-value",
        }):
            assert _is_first_time() is False


# ─── _format_coach_markdown ────────────────────────────────────────────────

class TestFormatCoachMarkdown:
    def test_keeps_existing_markdown(self):
        md = "## Resumen\n\n| Métrica | Valor |\n|---|---|\n| HRV | 56 |"
        out = _format_coach_markdown(md)
        assert out == md

    def test_wraps_single_plain_line(self):
        out = _format_coach_markdown("Hoy estás recuperado y listo para entrenar.")
        assert out.startswith("## 🧭 Resumen del Coach")
        assert "Hoy estás recuperado" in out

    def test_converts_plain_multiline_to_bullets(self):
        out = _format_coach_markdown("Readiness alta\nBody Battery 82\nSueño sólido")
        assert out.startswith("## 🧭 Resumen del Coach")
        assert "- Readiness alta" in out
        assert "- Body Battery 82" in out
        assert "- Sueño sólido" in out


class TestParsePlanCommand:
    def test_default_help(self):
        action, arg = _parse_plan_command("/plan")
        assert action == "help"
        assert arg is None

    def test_parse_list(self):
        action, arg = _parse_plan_command("/plan listar")
        assert action == "list"
        assert arg is None

    def test_parse_view_with_id(self):
        action, arg = _parse_plan_command("/plan ver plan-123")
        assert action == "view"
        assert arg == "plan-123"

    def test_parse_activate_with_id(self):
        action, arg = _parse_plan_command("/plan activar abc")
        assert action == "activate"
        assert arg == "abc"

    def test_parse_create(self):
        action, arg = _parse_plan_command("/plan crear")
        assert action == "create"
        assert arg is None


# ─── _build_enriched_athlete_knowledge ─────────────────────────────────────

class TestBuildEnrichedAthleteKnowledge:
    def test_includes_profile_and_mcp_sections(self):
        profile = {
            "personal": {"name": "Rafa", "height_cm": 178, "weight_kg": 70.2},
            "goals": {"primary": "trail running", "target_race": "PDA", "target_time": "09:59:00"},
            "health": {"injuries": ["DT1"], "notes": "control glucemia"},
        }
        enrichment = {
            "personal": {"age": 39, "gender": "hombre", "weight_kg": 70.1},
            "startup_48h": {
                "body_battery": {"summary": "hoy=ok · ayer=ok"},
                "hrv": {"summary": "hoy=ok · ayer=no"},
                "sleep": {"summary": "hoy=ok · ayer=ok"},
                "trainings": [{"date": "2026-07-06", "name": "Rodaje suave"}],
            },
        }

        out = _build_enriched_athlete_knowledge(profile, enrichment)

        assert "Base de Conocimiento del Atleta" in out
        assert "Enriquecimiento MCP (arranque)" in out
        assert "Estado de las ultimas 48h" in out
        assert "Rodaje suave" in out
        assert "```json" in out


# ─── _ensure_garmin_credentials ────────────────────────────────────────────

# ─── _ensure_garmin_credentials ────────────────────────────────────────────

class TestEnsureGarminCredentials:
    def test_uses_app_password_directly_sets_env(self):
        """Con email ya guardado, app_password se pone en GARMIN_PASSWORD sin prompt."""
        creds = {"garmin_email": "runner@example.com"}
        captured_env = {}
        with patch("agent.main.update_user_credentials"), \
             patch("agent.main.encrypt_password", return_value="ENC"), \
             patch("agent.main.Prompt.ask") as prompt_mock, \
             patch.dict("os.environ", {}, clear=True):
            out = _ensure_garmin_credentials(creds, app_password="app-secret")
            # Capturar dentro del contexto donde los env vars están activos
            import os as _os
            captured_env["pw"] = _os.environ.get("GARMIN_PASSWORD")
            captured_env["email"] = _os.environ.get("GARMIN_EMAIL")

        assert captured_env["pw"] == "app-secret"
        assert captured_env["email"] == "runner@example.com"
        assert prompt_mock.call_count == 0

    def test_prompts_email_when_missing(self):
        """Sin email guardado, pide email por prompt y actualiza credenciales."""
        creds = {}
        with patch("agent.main.update_user_credentials") as upd_mock, \
             patch("agent.main.encrypt_password", return_value="ENC"), \
             patch("agent.main.Prompt.ask", return_value="nuevo@example.com") as prompt_mock, \
             patch.dict("os.environ", {}, clear=True):
            out = _ensure_garmin_credentials(creds, app_password="secret")

        assert out["garmin_email"] == "nuevo@example.com"
        prompt_mock.assert_called_once()
        upd_mock.assert_called_once()


# ─── _auto_select_provider ──────────────────────────────────────────────────

class TestAutoSelectProvider:
    def test_vpn_detected_uses_menu_with_on_vpn_true(self):
        with patch("agent.main._detect_zscaler", return_value=True), \
             patch("agent.main._select_provider_menu", return_value="vpn") as menu_mock, \
             patch("agent.main.console.print"):
            provider = _auto_select_provider()

        assert provider == "vpn"
        menu_mock.assert_called_once_with(on_vpn=True)

    def test_no_vpn_uses_menu_with_on_vpn_false(self):
        with patch("agent.main._detect_zscaler", return_value=False), \
             patch("agent.main._select_provider_menu", return_value="nvidia") as menu_mock, \
             patch("agent.main.console.print"):
            provider = _auto_select_provider()

        assert provider == "nvidia"
        menu_mock.assert_called_once_with(on_vpn=False)

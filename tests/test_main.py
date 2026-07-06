"""
tests/test_main.py
Suite de tests unitarios para las funciones puras de main.py.

Cubre:
  - _validate_date
  - _validate_time
  - _validate_hours
  - _garmin_user_id
  - _is_first_time
"""

import hashlib
import os
from datetime import date, timedelta
from unittest.mock import patch

import pytest

from agent.main import (
    _format_coach_markdown,
    _garmin_user_id,
    _is_first_time,
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


# ─── _garmin_user_id ─────────────────────────────────────────────────────────

class TestGarminUserId:
    def test_returns_unknown_without_email(self):
        with patch.dict(os.environ, {}, clear=True):
            uid = _garmin_user_id()
        assert uid == "unknown"

    def test_returns_hash_with_email(self):
        with patch.dict(os.environ, {"GARMIN_EMAIL": "test@example.com"}):
            uid = _garmin_user_id()
        expected = hashlib.sha256("test@example.com".encode()).hexdigest()[:16]
        assert uid == expected

    def test_email_is_lowercased(self):
        with patch.dict(os.environ, {"GARMIN_EMAIL": "Test@Example.COM"}):
            uid = _garmin_user_id()
        expected = hashlib.sha256("test@example.com".encode()).hexdigest()[:16]
        assert uid == expected

    def test_return_is_16_chars(self):
        with patch.dict(os.environ, {"GARMIN_EMAIL": "any@mail.com"}):
            uid = _garmin_user_id()
        assert len(uid) == 16

    def test_different_emails_give_different_ids(self):
        with patch.dict(os.environ, {"GARMIN_EMAIL": "a@x.com"}):
            uid_a = _garmin_user_id()
        with patch.dict(os.environ, {"GARMIN_EMAIL": "b@x.com"}):
            uid_b = _garmin_user_id()
        assert uid_a != uid_b


# ─── _is_first_time ──────────────────────────────────────────────────────────

class TestIsFirstTime:
    """
    _is_first_time() llama a _load_user_profile() (persistencia) y a
    _garmin_user_id() (env).  Mockeamos ambos para aislar la lógica.
    """

    def test_no_profile_returns_true(self):
        with patch("agent.main._load_user_profile", return_value={}):
            assert _is_first_time() is True

    def test_setup_complete_false_returns_true(self):
        with patch("agent.main._load_user_profile", return_value={"setup_complete": False}):
            assert _is_first_time() is True

    def test_setup_complete_same_uid_returns_false(self):
        uid = hashlib.sha256("user@test.com".encode()).hexdigest()[:16]
        with patch.dict(os.environ, {"GARMIN_EMAIL": "user@test.com"}):
            with patch("agent.main._load_user_profile", return_value={
                "setup_complete": True,
                "garmin_user_id": uid,
            }):
                assert _is_first_time() is False

    def test_setup_complete_different_uid_returns_true(self):
        with patch.dict(os.environ, {"GARMIN_EMAIL": "new@test.com"}):
            with patch("agent.main._load_user_profile", return_value={
                "setup_complete": True,
                "garmin_user_id": "olduid12",
            }):
                assert _is_first_time() is True

    def test_empty_stored_uid_not_account_change(self):
        """Sin garmin_user_id guardado, la condición de cambio de cuenta no se activa
        (stored_uid es falsy → la rama 'if stored_uid' no se toma)."""
        with patch.dict(os.environ, {"GARMIN_EMAIL": "any@test.com"}):
            with patch("agent.main._load_user_profile", return_value={
                "setup_complete": False,
                "garmin_user_id": "",
            }):
                # setup_complete=False → siempre True
                assert _is_first_time() is True


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

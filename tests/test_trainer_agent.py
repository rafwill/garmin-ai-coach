"""
tests/test_trainer_agent.py
Suite de tests unitarios para las funciones puras de trainer_agent.py.

Cubre:
  - _seconds_to_hhmmss
  - _normalize_date_args
  - _strip_garmin_object
  - _compact_tool_result / _compact_personal_records
  - _clean_schema_for_gemini
  - _GeminiCompletions._parse  (sin llamada real a la API)
"""

import json
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pytest

from agent.trainer_agent import (
    _clean_schema_for_gemini,
    _compact_personal_records,
    _compact_tool_result,
    _extract_iso_date_from_text,
    _GeminiCompletions,
    _normalize_get_activity_args,
    _normalize_date_args,
    _seconds_to_hhmmss,
    _strip_garmin_object,
)


# ─── _seconds_to_hhmmss ───────────────────────────────────────────────────────

class TestSecondsToHhmmss:
    def test_below_one_hour_returns_mmss(self):
        assert _seconds_to_hhmmss(90) == "01:30"

    def test_zero_returns_mmss(self):
        assert _seconds_to_hhmmss(0) == "00:00"

    def test_sub_minute(self):
        assert _seconds_to_hhmmss(45) == "00:45"

    def test_exactly_one_hour(self):
        assert _seconds_to_hhmmss(3600) == "01:00:00"

    def test_above_one_hour(self):
        assert _seconds_to_hhmmss(5400) == "01:30:00"

    def test_float_rounds_up(self):
        # 90.6 → 91 segundos → 01:31
        assert _seconds_to_hhmmss(90.6) == "01:31"

    def test_marathon_time(self):
        # 3h30m = 12600s
        assert _seconds_to_hhmmss(12600) == "03:30:00"


# ─── _normalize_date_args ─────────────────────────────────────────────────────

class TestNormalizeDateArgs:
    def test_hoy(self):
        today = date.today().isoformat()
        assert _normalize_date_args({"date": "hoy"})["date"] == today

    def test_today_english(self):
        today = date.today().isoformat()
        assert _normalize_date_args({"startDate": "today"})["startDate"] == today

    def test_ayer(self):
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        assert _normalize_date_args({"date": "ayer"})["date"] == yesterday

    def test_yesterday_english(self):
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        assert _normalize_date_args({"endDate": "yesterday"})["endDate"] == yesterday

    def test_iso_passthrough(self):
        result = _normalize_date_args({"date": "2026-06-01"})
        assert result["date"] == "2026-06-01"

    def test_non_date_field_never_replaced(self):
        # "activityId" no está en DATE_FIELDS → aunque el valor sea "hoy" no se toca
        result = _normalize_date_args({"activityId": "hoy"})
        assert result["activityId"] == "hoy"

    def test_keyword_case_insensitive(self):
        today = date.today().isoformat()
        result = _normalize_date_args({"date": "HOY"})
        assert result["date"] == today

    def test_empty_dict(self):
        assert _normalize_date_args({}) == {}

    def test_multiple_fields(self):
        today = date.today().isoformat()
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        result = _normalize_date_args({
            "startDate": "hoy",
            "endDate":   "ayer",
            "activityId": 123,
        })
        assert result["startDate"] == today
        assert result["endDate"]   == yesterday
        assert result["activityId"] == 123


# ─── _extract_iso_date_from_text ────────────────────────────────────────────

class TestExtractIsoDateFromText:
    def test_extracts_iso(self):
        assert _extract_iso_date_from_text("2026-07-02") == "2026-07-02"

    def test_extracts_spanish_month_with_year(self):
        assert _extract_iso_date_from_text("2 de julio de 2026") == "2026-07-02"

    def test_extracts_dd_mm_yyyy(self):
        assert _extract_iso_date_from_text("02/07/2026") == "2026-07-02"


# ─── _strip_garmin_object ─────────────────────────────────────────────────────

class TestStripGarminObject:
    def test_removes_strip_fields(self):
        obj = {
            "startTimeGMT": "2026-06-17T08:00:00",
            "distance": 10000,
        }
        result = _strip_garmin_object(obj)
        assert "startTimeGMT" not in result
        assert result["distance"] == 10000

    def test_activity_id_NOT_stripped(self):
        """activityId debe llegar al LLM para que pueda llamar get_activity después de get_activities."""
        obj = {"activityId": "abc123", "distance": 5000}
        result = _strip_garmin_object(obj)
        assert "activityId" in result, "activityId no debe estar en _GARMIN_STRIP_FIELDS"

    def test_removes_image_url_keys(self):
        obj = {"profileImageUrl": "http://cdn.example.com/img.png", "steps": 8000}
        result = _strip_garmin_object(obj)
        assert "profileImageUrl" not in result
        assert result["steps"] == 8000

    def test_simplifies_activity_type_dict(self):
        obj = {"activityType": {"typeKey": "running", "sortOrder": 1}}
        result = _strip_garmin_object(obj)
        assert result["activityType"] == "running"

    def test_simplifies_event_type_dict(self):
        obj = {"eventType": {"typeKey": "race", "sortOrder": 2}}
        result = _strip_garmin_object(obj)
        assert result["eventType"] == "race"

    def test_nested_strip(self):
        obj = {"heartRate": {"avg": 155, "userProfileId": 999}}
        result = _strip_garmin_object(obj)
        assert "userProfileId" not in result["heartRate"]
        assert result["heartRate"]["avg"] == 155

    def test_list_truncated_to_4(self):
        lst = [{"v": i} for i in range(10)]
        result = _strip_garmin_object(lst)
        assert len(result) == 4

    def test_scalar_passthrough(self):
        assert _strip_garmin_object(42)      == 42
        assert _strip_garmin_object("hello") == "hello"
        assert _strip_garmin_object(None)    is None

    def test_empty_values_removed(self):
        obj = {"distance": 5000, "nothing": {}, "empty_list": []}
        result = _strip_garmin_object(obj)
        assert "nothing"    not in result
        assert "empty_list" not in result
        assert result["distance"] == 5000


# ─── _compact_tool_result ─────────────────────────────────────────────────────

class TestCompactToolResult:
    def test_none_returns_sin_datos(self):
        assert _compact_tool_result(None) == "(sin datos)"

    def test_empty_string_returns_sin_datos(self):
        assert _compact_tool_result("") == "(sin datos)"

    def test_list_truncated_to_8(self):
        raw = json.dumps([{"v": i} for i in range(20)])
        result = _compact_tool_result(raw)
        assert len(json.loads(result)) == 8

    def test_non_json_passthrough(self):
        assert _compact_tool_result("respuesta de texto plano") == "respuesta de texto plano"

    def test_long_string_truncated(self):
        long_str = "x" * 5000
        result = _compact_tool_result(long_str)
        assert result.endswith("...(truncado)")
        assert len(result) <= 3015  # _MAX_TOOL_RESULT_CHARS + sufijo

    def test_long_json_truncated(self):
        # JSON válido pero muy grande
        raw = json.dumps([{"data": "a" * 500} for _ in range(20)])
        result = _compact_tool_result(raw)
        assert result.endswith("...(truncado)")

    def test_personal_records_dispatched(self):
        data = [{"typeId": 3, "value": 1200.0, "activityName": "5K race", "activityType": "running"}]
        result = _compact_tool_result(json.dumps(data), tool_name="get_personal_records")
        assert "20:00" in result  # 1200 s = 20 min
        assert "5K"    in result

    def test_dict_json_stripped(self):
        data = {"activityId": "abc", "startTimeGMT": "2026-01-01T07:00:00", "distance": 10000}
        result = _compact_tool_result(json.dumps(data))
        result_dict = json.loads(result)
        assert "startTimeGMT" not in result_dict
        assert result_dict["distance"] == 10000

    def test_get_activity_adds_normalized_fields(self):
        data = {"activityId": 123, "duration": 36612.18359375, "distance": 54428.41015625}
        result = _compact_tool_result(json.dumps(data), tool_name="get_activity")
        result_dict = json.loads(result)
        assert result_dict["duration_hhmmss"] == "10:10:12"
        assert result_dict["distance_km"] == 54.43


# ─── _compact_personal_records ────────────────────────────────────────────────

class TestCompactPersonalRecords:
    def test_5k_time_conversion(self):
        data = [{"typeId": 3, "value": 1200.0, "activityName": "5K", "activityType": "running"}]
        result = json.loads(_compact_personal_records(data))
        assert result[0]["tiempo"] == "20:00"
        assert result[0]["tipo"]   == "5K"

    def test_half_marathon_time(self):
        data = [{"typeId": 5, "value": 5400.0, "activityName": "HM", "activityType": "running"}]
        result = json.loads(_compact_personal_records(data))
        assert result[0]["tiempo"] == "01:30:00"

    def test_marathon_time(self):
        # 4 horas = 14400 s
        data = [{"typeId": 6, "value": 14400.0, "activityName": "Marathon", "activityType": "running"}]
        result = json.loads(_compact_personal_records(data))
        assert result[0]["tiempo"] == "04:00:00"

    def test_longest_run_km(self):
        data = [{"typeId": 7, "value": 42195.0, "activityName": "Long Run", "activityType": "running"}]
        result = json.loads(_compact_personal_records(data))
        assert "42.20 km" in result[0]["distancia"]

    def test_swim_short_returns_metres(self):
        # 400 m de natación → metros
        data = [{"typeId": 17, "value": 400.0, "activityName": "Swim", "activityType": "pool_swimming"}]
        result = json.loads(_compact_personal_records(data))
        assert "400 m" in result[0]["distancia"]

    def test_swim_long_returns_km(self):
        data = [{"typeId": 17, "value": 3800.0, "activityName": "Open water", "activityType": "open_water"}]
        result = json.loads(_compact_personal_records(data))
        assert "3.80 km" in result[0]["distancia"]

    def test_unknown_type_id(self):
        data = [{"typeId": 999, "value": 100, "activityName": "X", "activityType": "running"}]
        result = json.loads(_compact_personal_records(data))
        assert result[0]["tipo"] == "typeId=999"

    def test_skips_non_dict_entries(self):
        data = [
            {"typeId": 3, "value": 900.0, "activityName": "5K", "activityType": "running"},
            "bad_entry",
            None,
        ]
        result = json.loads(_compact_personal_records(data))
        assert len(result) == 1

    def test_daily_steps(self):
        data = [{"typeId": 12, "value": 25432, "activityName": "Day", "activityType": "steps"}]
        result = json.loads(_compact_personal_records(data))
        assert "pasos" in result[0]


# ─── _clean_schema_for_gemini ─────────────────────────────────────────────────

class TestCleanSchemaForGemini:
    def test_removes_exclusive_minimum(self):
        schema = {"type": "integer", "exclusiveMinimum": 0, "description": "Activity ID"}
        result = _clean_schema_for_gemini(schema)
        assert "exclusiveMinimum" not in result
        assert result["type"]        == "integer"
        assert result["description"] == "Activity ID"

    def test_removes_additional_properties(self):
        schema = {"type": "object", "additionalProperties": False, "properties": {"x": {"type": "string"}}}
        result = _clean_schema_for_gemini(schema)
        assert "additionalProperties" not in result
        assert "properties" in result

    def test_keeps_required(self):
        schema = {"type": "object", "required": ["date"], "properties": {}}
        result = _clean_schema_for_gemini(schema)
        assert result["required"] == ["date"]

    def test_keeps_enum(self):
        schema = {"type": "string", "enum": ["running", "cycling"]}
        result = _clean_schema_for_gemini(schema)
        assert result["enum"] == ["running", "cycling"]

    def test_recursive_properties_cleaned(self):
        schema = {
            "type": "object",
            "properties": {
                "activityId": {"type": "number", "exclusiveMinimum": 0}
            }
        }
        result = _clean_schema_for_gemini(schema)
        assert "exclusiveMinimum" not in result["properties"]["activityId"]
        assert result["properties"]["activityId"]["type"] == "number"

    def test_nested_items_cleaned(self):
        schema = {
            "type": "array",
            "items": {"type": "number", "exclusiveMinimum": 0, "description": "value"}
        }
        result = _clean_schema_for_gemini(schema)
        assert "exclusiveMinimum" not in result["items"]
        assert result["items"]["type"] == "number"

    def test_empty_schema(self):
        assert _clean_schema_for_gemini({}) == {}


# ─── _GeminiCompletions._parse ────────────────────────────────────────────────

class TestGeminiCompletionsParse:
    """
    Tests de la capa de parsing sin llamadas reales a la API.
    _parse() solo accede a response.candidates[0].content.parts
    y a response.usage_metadata — ambos se mockean con MagicMock.
    """

    def _make_gemini(self) -> _GeminiCompletions:
        """Instancia _GeminiCompletions omitiendo __init__ (que requiere google-genai)."""
        gc = object.__new__(_GeminiCompletions)
        gc._api_key = "fake-key"
        return gc

    def _make_response(self, parts, usage_metadata=None):
        content   = MagicMock()
        content.parts = parts
        candidate = MagicMock()
        candidate.content = content
        response  = MagicMock()
        response.candidates    = [candidate]
        response.usage_metadata = usage_metadata
        return response

    # --- respuesta de texto ---

    def test_text_response_sets_content(self):
        part = MagicMock(spec=["text", "function_call"])
        part.text           = "Hola, soy tu entrenador."
        part.function_call  = None
        response = self._make_response([part])

        result = self._make_gemini()._parse(response)

        msg = result.choices[0].message
        assert msg.content    == "Hola, soy tu entrenador."
        assert msg.tool_calls is None

    def test_multi_text_parts_concatenated(self):
        p1 = MagicMock(spec=["text", "function_call"]); p1.text = "Parte 1 "; p1.function_call = None
        p2 = MagicMock(spec=["text", "function_call"]); p2.text = "Parte 2";  p2.function_call = None
        response = self._make_response([p1, p2])

        result = self._make_gemini()._parse(response)
        assert result.choices[0].message.content == "Parte 1 Parte 2"

    # --- respuesta con function call ---

    def test_function_call_sets_tool_calls(self):
        fn_call      = MagicMock()
        fn_call.name = "get_daily_steps"
        fn_call.args = {"date": "2026-06-17"}

        part               = MagicMock()
        part.function_call = fn_call

        with patch("agent.trainer_agent.update_gemini_daily_usage"):
            response = self._make_response([part])
            result   = self._make_gemini()._parse(response)

        msg = result.choices[0].message
        assert msg.tool_calls is not None
        assert msg.tool_calls[0].function.name == "get_daily_steps"
        args = json.loads(msg.tool_calls[0].function.arguments)
        assert args["date"] == "2026-06-17"

    def test_function_call_id_generated(self):
        fn_call      = MagicMock()
        fn_call.name = "get_body_battery"
        fn_call.args = {}
        part               = MagicMock()
        part.function_call = fn_call

        with patch("agent.trainer_agent.update_gemini_daily_usage"):
            result = self._make_gemini()._parse(self._make_response([part]))

        assert result.choices[0].message.tool_calls[0].id.startswith("gcall_")

    # --- sin usage_metadata ---

    def test_usage_none_when_no_metadata(self):
        part = MagicMock(spec=["text", "function_call"])
        part.text          = "ok"
        part.function_call = None
        response = self._make_response([part], usage_metadata=None)

        result = self._make_gemini()._parse(response)
        assert result.usage is None


# ─── _normalize_get_activity_args ───────────────────────────────────────────

class TestNormalizeGetActivityArgs:
    @pytest.mark.asyncio
    async def test_keeps_numeric_activity_id(self):
        out = await _normalize_get_activity_args(MagicMock(), {"activity_id": "12345"})
        assert out == {"activity_id": 12345}

    @pytest.mark.asyncio
    async def test_resolves_spanish_date_to_activity_id(self):
        fake_response = json.dumps(
            {
                "start": 0,
                "limit": 100,
                "count": 2,
                "has_more": False,
                "next_start": 100,
                "activities": [
                    {"activityId": 111, "startTimeLocal": "2026-07-01T07:00:00.0"},
                    {"activityId": 222, "startTimeLocal": "2026-07-02T07:30:15.0"},
                ],
            }
        )
        with patch("agent.trainer_agent.call_tool", return_value=fake_response):
            out = await _normalize_get_activity_args(MagicMock(), {"activity_id": "2 de julio de 2026"})
        assert out == {"activity_id": 222}

    @pytest.mark.asyncio
    async def test_resolves_activity_name_hint_to_activity_id(self):
        fake_response = json.dumps(
            {
                "start": 0,
                "limit": 100,
                "count": 2,
                "has_more": False,
                "next_start": 100,
                "activities": [
                    {"activityId": 1001, "name": "Rodaje suave 8km", "startTimeLocal": "2026-07-03T07:00:00.0"},
                    {
                        "activityId": 2002,
                        "name": "Ultra Trail. Hoka Val d'Aran Pyrenees by UTMB PDA 2026",
                        "startTimeLocal": "2026-07-02T07:30:15.0",
                    },
                ],
            }
        )
        with patch("agent.trainer_agent.call_tool", return_value=fake_response):
            out = await _normalize_get_activity_args(MagicMock(), {"activity_id": "Ultra Trail. Hoka Val d"})
        assert out == {"activity_id": 2002}

    @pytest.mark.asyncio
    async def test_returns_empty_args_for_unresolved_text_activity_id(self):
        fake_response = json.dumps(
            {
                "start": 0,
                "limit": 100,
                "count": 1,
                "has_more": False,
                "next_start": 100,
                "activities": [
                    {"activityId": 3003, "name": "Paseo", "startTimeLocal": "2026-07-01T07:00:00.0"},
                ],
            }
        )
        with patch("agent.trainer_agent.call_tool", return_value=fake_response):
            out = await _normalize_get_activity_args(MagicMock(), {"activity_id": "actividad inventada"})
        assert out == {}

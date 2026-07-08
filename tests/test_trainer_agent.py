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
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.trainer_agent import (
    _build_training_plan_status_markdown,
    _build_tools_schema,
    _build_mcp_read_only_block_message,
    _build_personal_records_markdown,
    _build_startup_plan_recommendation,
    _build_activity_candidates_payload,
    _build_goal_plan_fallback,
    _build_athlete_knowledge_context,
    _build_proactive_status_markdown,
    _build_recovery_fallback_snapshot,
    _clean_schema_for_gemini,
    _compact_personal_records,
    _compact_tool_result,
    _extract_activities_list,
    _extract_iso_date_from_text,
    _generate_structured_plan_payload,
    _GeminiCompletions,
    _get_active_training_plan,
    _has_goal_in_profile,
    _detect_personal_records_sport_intent,
    _is_generic_needs_more_info_reply,
    _is_personal_records_followup_intent,
    _is_plan_status_intent,
    _is_planning_intent,
    _is_write_mcp_tool,
    _resolve_activity_id_from_query,
    _summarize_plan_changes,
    _is_activity_in_last_48h,
    _is_no_data_result,
    _normalize_trend_date_range,
    _normalize_get_activity_args,
    _normalize_date_args,
    _load_system_prompt,
    _load_athlete_knowledge_chunks,
    _resolve_kb_paths,
    _retrieve_athlete_knowledge,
    _seconds_to_hhmmss,
    _strip_garmin_object,
    _validate_structured_plan,
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


class TestSystemPromptDateFormatRules:
    def test_full_prompt_requires_global_spanish_date_format(self):
        prompt = _load_system_prompt(compact=False)
        assert "Formato de fecha obligatorio (España)" in prompt
        assert "Regla global de fechas (OBLIGATORIA)" in prompt
        assert "DD/MM/AAAA" in prompt
        assert "YYYY-MM-DD" in prompt

    def test_compact_prompt_requires_global_spanish_date_format(self):
        prompt = _load_system_prompt(compact=True)
        assert "Regla global de fechas (España)" in prompt
        assert "DD/MM/AAAA" in prompt
        assert "YYYY-MM-DD" in prompt


class TestSystemPromptPlanStatusRules:
    def test_full_prompt_includes_plan_status_intent_variants(self):
        prompt = _load_system_prompt(compact=False)
        assert "Consulta de estado del plan (OBLIGATORIO)" in prompt
        assert "que plan llevo esta semana?" in prompt
        assert "sigo con el plan?" in prompt
        assert "goals" in prompt and "training_plan" in prompt

    def test_compact_prompt_includes_plan_status_rules(self):
        prompt = _load_system_prompt(compact=True)
        assert "Estado del plan (OBLIGATORIO)" in prompt
        assert "Nunca inferir plan activo desde `goals`" in prompt
        assert "training_plan" in prompt


class TestSystemPromptPlanManagementRules:
    def test_full_prompt_includes_functional_plan_management(self):
        prompt = _load_system_prompt(compact=False)
        assert "Generacion y manejo funcional de planes (OBLIGATORIO)" in prompt
        assert "/plan crear" in prompt
        assert "/plan listar" in prompt
        assert "/plan ver <plan_id>" in prompt
        assert "/plan activar <plan_id>" in prompt
        assert "No afirmes que un plan quedó guardado/activado" in prompt

    def test_compact_prompt_includes_functional_plan_management(self):
        prompt = _load_system_prompt(compact=True)
        assert "Generación y manejo de planes (OBLIGATORIO)" in prompt
        assert "/plan crear" in prompt
        assert "/plan listar" in prompt
        assert "/plan ver <plan_id>" in prompt
        assert "/plan activar <plan_id>" in prompt
        assert "nueva versión" in prompt or "nueva version" in prompt


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

    def test_personal_record_singular_dispatched(self):
        data = [{"typeId": 3, "value": 1200.0, "activityName": "5K race", "activityType": "running"}]
        result = _compact_tool_result(json.dumps(data), tool_name="get_personal_record")
        assert "20:00" in result
        assert "5K" in result

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


# ─── Base de conocimiento del atleta (RAG) ──────────────────────────────────

class TestAthleteKnowledgeRag:
    def test_resolve_kb_paths_uses_defaults_when_env_empty(self, tmp_path: Path):
        paths = _resolve_kb_paths("", project_root=tmp_path)
        assert len(paths) >= 3
        assert str(paths[0]).startswith(str(tmp_path))

    def test_load_knowledge_chunks_from_txt_and_json(self, tmp_path: Path):
        txt_file = tmp_path / "athlete_notes.txt"
        txt_file.write_text("Objetivo: bajar de 10h en la PDA.\nFuerza de sóleo 2 veces/semana.", encoding="utf-8")

        json_file = tmp_path / "athlete_profile.json"
        json_file.write_text(
            json.dumps({"nutrition": {"during_long_run": "60-90g CH/h"}, "injuries": ["soleo"]}, ensure_ascii=False),
            encoding="utf-8",
        )

        env_paths = f"{txt_file},{json_file}"
        chunks, sources = _load_athlete_knowledge_chunks(env_paths, project_root=tmp_path, chunk_size=120)

        assert chunks, "Debe generar chunks desde archivos válidos"
        assert "athlete_notes.txt" in sources
        assert "athlete_profile.json" in sources
        joined = "\n".join(c["text"] for c in chunks)
        assert "bajar de 10h" in joined
        assert "during_long_run" in joined

    def test_retrieve_returns_most_relevant_chunks(self):
        chunks = [
            {"source": "a.txt", "text": "Trabajo de umbral y VO2max para 10K."},
            {"source": "b.txt", "text": "Diabetes tipo 1: controlar glucemia antes de tiradas largas."},
            {"source": "c.txt", "text": "Series en cuesta y técnica de bajada con bastones."},
        ]
        out = _retrieve_athlete_knowledge("Tengo diabetes y haré tirada larga", chunks, top_k=2)
        assert out
        assert out[0]["source"] == "b.txt"

    def test_build_knowledge_context_includes_header_and_source(self):
        chunks = [{"source": "kb.md", "text": "Objetivo principal: PDA sub10h."}]
        ctx = _build_athlete_knowledge_context("objetivo PDA", chunks)
        assert "Base de Conocimiento del atleta" in ctx
        assert "Fuente: kb.md" in ctx
        assert "PDA sub10h" in ctx


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

    def test_supports_snake_case_payload_shape(self):
        data = [
            {
                "record_type": "Fastest 10K",
                "type_id": 4,
                "value": "35:53",
                "raw_value": 2153.6,
            }
        ]
        result = json.loads(_compact_personal_records(data))
        assert result[0]["categoria"] == "10K"
        assert result[0]["valor"] == "35:53"

    def test_translates_unknown_record_type_to_spanish(self):
        data = [
            {
                "record_type": "Longest Ride",
                "value": "199.02 km",
            }
        ]
        result = json.loads(_compact_personal_records(data))
        assert result[0]["categoria"] == "Ciclismo más largo"


class TestPersonalRecordsMarkdown:
    def test_build_personal_records_markdown_shows_running_rows(self):
        compact = json.dumps(
            [
                {"categoria": "5K", "valor": "17:48", "type_id": 3},
                {"categoria": "Ciclismo más largo", "valor": "199.02 km", "type_id": 8},
            ]
        )
        out = _build_personal_records_markdown(compact)
        assert "mejores registros personales en running" in out.lower()
        assert "| 5K | 17:48 |" in out
        assert "Ciclismo más largo" not in out

    def test_personal_records_followup_intent_true_with_context(self):
        history = [{"role": "assistant", "content": "## Tus mejores registros personales en running"}]
        assert _is_personal_records_followup_intent("En que distancias son esas marcas?", history)

    def test_build_personal_records_markdown_cycling_does_not_return_running(self):
        compact = json.dumps(
            [
                {"categoria": "5K", "valor": "17:48", "type_id": 3},
                {"categoria": "Ciclismo más largo", "valor": "199.02 km", "type_id": 8},
                {"categoria": "40K ciclismo", "valor": "54:42", "type_id": 11},
            ]
        )
        out = _build_personal_records_markdown(compact, preferred_sport="cycling")
        assert "registros personales en ciclismo" in out.lower()
        assert "Ciclismo más largo" in out
        assert "40K ciclismo" in out
        assert "5K" not in out

    def test_detect_personal_records_sport_intent_cycling(self):
        assert _detect_personal_records_sport_intent("Y mis mejores marcas en ciclismo?", []) == "cycling"

    def test_detect_personal_records_sport_intent_from_followup_context(self):
        history = [{"role": "assistant", "content": "## Tus mejores registros personales en ciclismo"}]
        assert _detect_personal_records_sport_intent("En que distancias son esas marcas?", history) == "cycling"


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

    @pytest.mark.asyncio
    async def test_recovers_activity_id_from_user_message_date_when_args_empty(self):
        fake_response = json.dumps(
            {
                "start": 0,
                "limit": 100,
                "count": 2,
                "has_more": False,
                "next_start": 100,
                "activities": [
                    {"activityId": 111, "startTimeLocal": "2026-07-01T07:00:00.0"},
                    {"activityId": 222, "startTimeLocal": "2026-07-02T09:30:00.0"},
                ],
            }
        )
        with patch("agent.trainer_agent.call_tool", return_value=fake_response):
            out = await _normalize_get_activity_args(
                MagicMock(),
                {},
                user_message="Analiza mi competición del 2 de julio de 2026",
            )
        assert out == {"activity_id": 222}

    @pytest.mark.asyncio
    async def test_recovers_activity_id_from_user_message_name_when_args_empty(self):
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
            out = await _normalize_get_activity_args(
                MagicMock(),
                {},
                user_message="Analiza mi Ultra Trail. Hoka Val d",
            )
        assert out == {"activity_id": 2002}

    @pytest.mark.asyncio
    async def test_date_query_does_not_fallback_to_name_when_date_missing(self):
        fake_response = json.dumps(
            {
                "start": 0,
                "limit": 100,
                "count": 2,
                "has_more": False,
                "next_start": 100,
                "activities": [
                    {
                        "activityId": 4001,
                        "name": "Competición local 10K",
                        "startTimeLocal": "2026-07-04T08:00:00.0",
                    },
                    {
                        "activityId": 4002,
                        "name": "Senderismo",
                        "startTimeLocal": "2026-07-04T10:00:00.0",
                    },
                ],
            }
        )
        with patch("agent.trainer_agent.call_tool", return_value=fake_response):
            out = await _normalize_get_activity_args(
                MagicMock(),
                {},
                user_message="Analiza mi competición del 2 de julio de 2026",
            )
        assert out == {}


class TestResolveActivityIdFromQuery:
    @pytest.mark.asyncio
    async def test_resolves_from_query_date(self):
        fake_response = json.dumps(
            {
                "start": 0,
                "limit": 100,
                "count": 2,
                "has_more": False,
                "next_start": 100,
                "activities": [
                    {"activityId": 111, "startTimeLocal": "2026-07-01T07:00:00.0"},
                    {"activityId": 222, "startTimeLocal": "2026-07-02T07:30:00.0"},
                ],
            }
        )
        with patch("agent.trainer_agent.call_tool", return_value=fake_response):
            out = await _resolve_activity_id_from_query(
                MagicMock(),
                "Analiza mi competición del 2 de julio de 2026",
            )
        assert out == 222

    @pytest.mark.asyncio
    async def test_build_candidates_payload_returns_candidates(self):
        fake_response = json.dumps(
            {
                "activities": [
                    {
                        "activityId": 333,
                        "name": "Zara Speed Run 10k",
                        "startTimeLocal": "2026-07-02T08:00:00.0",
                    }
                ]
            }
        )
        with patch("agent.trainer_agent.call_tool", return_value=fake_response):
            raw = await _build_activity_candidates_payload(
                MagicMock(),
                "Analiza mi competición del 2 de julio de 2026",
            )

        parsed = json.loads(raw)
        assert parsed["error"] == "missing_activity_id"
        assert parsed["candidates"]

    @pytest.mark.asyncio
    async def test_date_query_strict_no_name_fallback(self):
        fake_response = json.dumps(
            {
                "activities": [
                    {
                        "activityId": 5001,
                        "name": "Competición 10K",
                        "startTimeLocal": "2026-07-04T08:00:00.0",
                    }
                ]
            }
        )
        with patch("agent.trainer_agent.call_tool", return_value=fake_response):
            out = await _resolve_activity_id_from_query(
                MagicMock(),
                "Analiza mi competición del 2 de julio de 2026",
            )
        assert out is None


# ─── Fallback de recuperación ───────────────────────────────────────────────

class TestRecoveryFallback:
    def test_is_no_data_result_true(self):
        assert _is_no_data_result("No training readiness data found for 2026-07-03")

    def test_is_no_data_result_false(self):
        assert not _is_no_data_result('{"value":42}')

    @pytest.mark.asyncio
    async def test_build_recovery_fallback_snapshot_returns_payload(self):
        async def _fake_call_tool(_session, tool_name, arguments):
            if (
                tool_name == "get_body_battery"
                and arguments.get("start_date") == "2026-07-03"
                and arguments.get("end_date") == "2026-07-03"
            ):
                return '{"charged":72,"drained":28}'
            return "No data found"

        with patch("agent.trainer_agent.call_tool", side_effect=_fake_call_tool):
            payload = await _build_recovery_fallback_snapshot(MagicMock(), "2026-07-03")

        assert payload is not None
        parsed = json.loads(payload)
        assert parsed["fallback_reason"] == "training_readiness_unavailable"
        assert "get_body_battery" in parsed["snapshot"]

    @pytest.mark.asyncio
    async def test_build_recovery_fallback_snapshot_handles_plain_text(self):
        async def _fake_call_tool(_session, tool_name, arguments):
            if (
                tool_name == "get_body_battery"
                and arguments.get("start_date") == "2026-07-03"
                and arguments.get("end_date") == "2026-07-03"
            ):
                return "Battery score: 58"
            return "No data found"

        with patch("agent.trainer_agent.call_tool", side_effect=_fake_call_tool):
            payload = await _build_recovery_fallback_snapshot(MagicMock(), "2026-07-03")

        assert payload is not None
        parsed = json.loads(payload)
        assert parsed["snapshot"]["get_body_battery"]["data"]["raw"] == "Battery score: 58"


# ─── Startup 48h proactivo ────────────────────────────────────────────────

class TestStartupProactive:
    def test_extract_activities_list_supports_dict_payload(self):
        payload = {"activities": [{"activityId": 1}, {"activityId": 2}]}
        out = _extract_activities_list(payload)
        assert len(out) == 2

    def test_is_activity_in_last_48h_true_for_recent_day(self):
        today = date.today().isoformat()
        activity = {"startTimeLocal": f"{today}T08:00:00.0"}
        assert _is_activity_in_last_48h(activity)

    def test_build_proactive_status_markdown_contains_sections(self):
        payload = {
            "profile_changes": ["peso", "altura"],
            "body_battery": {"summary": "hoy=ok · ayer=ok"},
            "hrv": {"summary": "hoy=no · ayer=ok"},
            "sleep": {"summary": "hoy=ok · ayer=no"},
            "trainings": [{"date": "2026-07-06", "name": "Trail suave"}],
        }
        out = _build_proactive_status_markdown(payload)
        assert "Estado Proactivo" in out
        assert "Perfil Garmin actualizado" in out
        assert "Trail suave" in out
        assert "No tienes plan asignado" in out

    def test_build_proactive_status_markdown_shows_plan_recommendation_when_assigned(self):
        payload = {
            "plan_assigned": True,
            "plan_recommendation": "Tienes un objetivo activo (10K). ¿Quieres que adapte la sesion de hoy a ese plan?",
            "body_battery": {"summary": "sin datos"},
            "hrv": {"summary": "sin datos"},
            "sleep": {"summary": "sin datos"},
            "trainings": [],
        }
        out = _build_proactive_status_markdown(payload)
        assert "Tienes un objetivo activo (10K)" in out

    @pytest.mark.asyncio
    async def test_collect_startup_snapshot_48h_collects_metrics(self):
        from agent.trainer_agent import TrainerAgent
        captured_calls: list[tuple[str, dict]] = []

        async def _fake_call_tool(_session, tool_name, _arguments):
            captured_calls.append((tool_name, dict(_arguments or {})))
            if tool_name == "get_activities":
                today = date.today().isoformat()
                return json.dumps({
                    "activities": [
                        {"activityId": 777, "name": "Rodaje", "startTimeLocal": f"{today}T07:30:00.0"}
                    ]
                })
            return json.dumps({"ok": True})

        agent = object.__new__(TrainerAgent)
        agent.mcp_session = MagicMock()

        with patch("agent.trainer_agent.call_tool", side_effect=_fake_call_tool):
            snapshot = await TrainerAgent.collect_startup_snapshot_48h(agent)

        assert snapshot["window_hours"] == 48
        assert snapshot["body_battery"]["summary"].startswith("hoy=")
        assert snapshot["trainings"]
        bb_calls = [args for name, args in captured_calls if name == "get_body_battery"]
        assert len(bb_calls) == 2
        assert all("start_date" in args and "end_date" in args for args in bb_calls)

    @pytest.mark.asyncio
    async def test_build_startup_status_markdown_uses_training_plan_not_goals(self):
        from agent.trainer_agent import TrainerAgent

        agent = object.__new__(TrainerAgent)
        agent.mcp_session = MagicMock()
        agent.user_profile = {
            "goals": {
                "target_race": "10K",
                "target_race_date": "2026-11-22",
            }
        }

        with patch.object(TrainerAgent, "collect_startup_snapshot_48h", return_value={"body_battery": {}, "hrv": {}, "sleep": {}, "trainings": []}):
            out = await TrainerAgent.build_startup_status_markdown(agent)

        assert "No tienes plan asignado" in out


# ─── Fallback de planificacion y rangos trend ─────────────────────────────

class TestPlanningFallbackAndRanges:
    def test_normalize_trend_date_range_clamps_future_end_date(self):
        out = _normalize_trend_date_range(
            "get_training_load_trend",
            {"start_date": "2026-07-07", "end_date": "2099-01-01"},
        )
        assert "start_date" in out and "end_date" in out
        assert out["end_date"] <= date.today().isoformat()

    def test_normalize_trend_date_range_enforces_max_window(self):
        out = _normalize_trend_date_range(
            "get_hrv_trend",
            {"start_date": "2020-01-01", "end_date": date.today().isoformat()},
        )
        s = date.fromisoformat(out["start_date"])
        e = date.fromisoformat(out["end_date"])
        assert (e - s).days <= 30

    def test_generic_needs_more_info_detection(self):
        txt = "Lo siento, pero no puedo crear una planificación para tu objetivo sin más información"
        assert _is_generic_needs_more_info_reply(txt)

    def test_planning_intent_detection_true(self):
        assert _is_planning_intent("¿puedes crearme una planificación para mi objetivo?")

    def test_planning_intent_detection_false_for_activity_analysis(self):
        assert not _is_planning_intent("Analiza mi entrenamiento del día 2 de julio de 2026")

    def test_plan_status_intent_true_for_have_plan_question(self):
        assert _is_plan_status_intent("Tengo algun plan asignado?")

    def test_plan_status_intent_false_for_plan_creation_request(self):
        assert not _is_plan_status_intent("Puedes planificarme la semana?")

    def test_plan_status_intent_false_for_plan_adjustment_request(self):
        assert not _is_plan_status_intent("Ajusta mi plan de esta semana")

    def test_has_goal_in_profile(self):
        profile = {"goals": {"target_race": "10k", "target_race_date": "2026-11-22"}}
        assert _has_goal_in_profile(profile)

    def test_get_active_training_plan_requires_plan_entity(self):
        profile = {"goals": {"target_race": "10k"}}
        assert _get_active_training_plan(profile) is None

    def test_get_active_training_plan_detects_active(self):
        profile = {"training_plan": {"active": True, "title": "Plan 10K", "status": "active"}}
        plan = _get_active_training_plan(profile)
        assert plan is not None
        assert plan["title"] == "Plan 10K"

    def test_get_active_training_plan_prefers_storage_source_of_truth(self):
        profile = {"training_plan": {"active": True, "title": "Plan local", "status": "active"}}
        with patch("agent.trainer_agent._storage.get_active_training_plan", return_value={
            "id": "plan-db-1",
            "title": "Plan DB",
            "status": "active",
            "source": "agent",
            "plan_data": {"target_race": "10K"},
        }), patch("agent.trainer_agent._storage.list_training_plan_sessions", return_value=[]):
            plan = _get_active_training_plan(profile)

        assert plan is not None
        assert plan["title"] == "Plan DB"
        assert plan["id"] == "plan-db-1"

    def test_get_active_training_plan_falls_back_to_profile_when_storage_unavailable(self):
        profile = {"training_plan": {"active": True, "title": "Plan local", "status": "active"}}
        with patch("agent.trainer_agent._storage.get_active_training_plan", side_effect=RuntimeError("db down")):
            plan = _get_active_training_plan(profile)

        assert plan is not None
        assert plan["title"] == "Plan local"

    def test_build_startup_plan_recommendation_includes_title(self):
        msg = _build_startup_plan_recommendation({"title": "Plan 10K", "active": True})
        assert "Plan 10K" in msg

    def test_build_goal_plan_fallback_contains_target(self):
        profile = {
            "goals": {
                "target_race": "Zara Speed Run 10k",
                "target_race_date": "2026-11-22",
                "target_time": "0:35:59",
            },
            "health": {"injuries": ["DT 1"]},
        }
        out = _build_goal_plan_fallback(profile)
        assert "Zara Speed Run 10k" in out
        assert "Estructura semanal propuesta" in out

    def test_build_training_plan_status_markdown_no_plan_is_explicit(self):
        profile = {
            "goals": {
                "target_race": "Trail 42K",
                "target_race_date": "2026-11-22",
                "target_time": "05:30:00",
                "weekly_training_hours": 10,
            }
        }
        out = _build_training_plan_status_markdown(profile)
        assert "No tienes plan asignado" in out
        assert "22/11/2026" in out

    def test_build_training_plan_status_markdown_active_plan_uses_plan_entity(self):
        profile = {
            "goals": {"target_race": "Trail 42K", "target_race_date": "2026-11-22"},
            "training_plan": {
                "active": True,
                "status": "active",
                "title": "Plan Trail 42K",
                "target_race": "Trail 42K",
                "target_race_date": "2026-11-22",
                "today_focus": "Rodaje Z2 50 min",
            },
        }
        out = _build_training_plan_status_markdown(profile)
        assert "Sí, tienes un plan activo: Plan Trail 42K." in out
        assert "22/11/2026" in out
        assert "Rodaje Z2 50 min" in out

    def test_generate_structured_plan_payload_returns_plan_and_sessions(self):
        profile = {
            "goals": {
                "target_race": "10K",
                "target_race_date": (date.today() + timedelta(days=56)).isoformat(),
                "weekly_training_hours": 7,
            },
            "health": {},
        }
        plan, sessions = _generate_structured_plan_payload(profile, "Planifícame para mi 10K")
        assert plan["title"].startswith("Plan hacia")
        assert plan["duration_weeks"] >= 4
        assert len(sessions) == 7

    def test_validate_structured_plan_flags_invalid_session_day(self):
        plan = {
            "title": "Plan",
            "objective": "Objetivo",
            "duration_weeks": 8,
        }
        sessions = [{"day_index": 9, "duration_min": 40, "session_type": "running_z2"}]
        errors = _validate_structured_plan(plan, sessions, {"goals": {"weekly_training_hours": 6}})
        assert any("día fuera de rango" in e for e in errors)

    def test_summarize_plan_changes_includes_duration_and_volume(self):
        previous_plan = {"duration_weeks": 8, "difficulty": "moderate"}
        new_plan = {"duration_weeks": 10, "difficulty": "hard"}
        previous_sessions = [{"duration_min": 40}, {"duration_min": 60}]
        new_sessions = [{"duration_min": 50}, {"duration_min": 70}]
        out = _summarize_plan_changes(previous_plan, new_plan, previous_sessions, new_sessions)
        assert "Duración" in out
        assert "Volumen semanal estimado" in out


class TestPlanStatusChatRoute:
    @pytest.mark.asyncio
    async def test_chat_plan_status_does_not_call_llm_and_is_consistent_with_profile(self):
        from agent.trainer_agent import TrainerAgent

        agent = object.__new__(TrainerAgent)
        agent.user_profile = {
            "goals": {
                "target_race": "Trail 42K",
                "target_race_date": "2026-11-22",
                "weekly_training_hours": 10,
            }
        }
        agent.conversation_history = []
        agent.tools_schema = []
        agent.mcp_session = MagicMock()
        agent._build_messages = lambda _msg: []
        agent.client = MagicMock()
        agent.client.chat = MagicMock()
        agent.client.chat.completions = MagicMock()
        agent.client.chat.completions.create = AsyncMock(side_effect=AssertionError("LLM should not be called"))

        with patch("agent.trainer_agent._save_history_entry"):
            out = await TrainerAgent.chat(agent, "Tengo algun plan?")

        assert "No tienes plan asignado" in out
        assert len(agent.conversation_history) == 2

    @pytest.mark.asyncio
    async def test_chat_planning_generates_structured_plan_without_llm(self):
        from agent.trainer_agent import TrainerAgent

        agent = object.__new__(TrainerAgent)
        agent.user_profile = {
            "goals": {
                "target_race": "10K",
                "target_race_date": "2026-11-22",
                "target_time": "00:45:00",
            },
            "health": {},
        }
        agent.conversation_history = []
        agent.tools_schema = []
        agent.mcp_session = MagicMock()
        agent.mcp_read_only = True
        agent.model = "test-model"
        agent._build_messages = lambda _msg: []
        agent.client = MagicMock()
        agent.client.chat = MagicMock()
        agent.client.chat.completions = MagicMock()
        agent.client.chat.completions.create = AsyncMock(side_effect=AssertionError("LLM should not be called"))

        with patch("agent.trainer_agent._storage.get_active_training_plan", return_value=None), patch("agent.trainer_agent._storage.create_training_plan", return_value={
            "id": "plan-db-1",
            "title": "Plan hacia 10K",
            "status": "active",
            "source": "agent_structured_plan",
            "duration_weeks": 8,
            "difficulty": "moderate",
            "plan_data": {
                "target_race": "10K",
                "target_race_date": "2026-11-22",
            },
        }) as mocked_create, patch("agent.trainer_agent._save_user_profile"), patch("agent.trainer_agent._save_history_entry"):
            out = await TrainerAgent.chat(agent, "Puedes planificarme la semana para mi 10K?")

        assert "Resumen" in out
        assert "Plan activo: Plan hacia 10K" in out
        mocked_create.assert_called_once()
        assert agent.user_profile["training_plan"]["id"] == "plan-db-1"

    @pytest.mark.asyncio
    async def test_chat_planning_updates_existing_plan_and_reports_changes(self):
        from agent.trainer_agent import TrainerAgent

        agent = object.__new__(TrainerAgent)
        agent.user_profile = {
            "goals": {
                "target_race": "10K",
                "target_race_date": "2026-11-22",
                "weekly_training_hours": 8,
            },
            "health": {},
        }
        agent.conversation_history = []
        agent.tools_schema = []
        agent.mcp_session = MagicMock()
        agent.mcp_read_only = True
        agent.model = "test-model"
        agent._build_messages = lambda _msg: []
        agent.client = MagicMock()
        agent.client.chat = MagicMock()
        agent.client.chat.completions = MagicMock()
        agent.client.chat.completions.create = AsyncMock(side_effect=AssertionError("LLM should not be called"))

        existing = {
            "id": "plan-db-1",
            "title": "Plan actual 10K",
            "objective": "Preparación 10K",
            "difficulty": "moderate",
            "duration_weeks": 8,
            "status": "active",
            "source": "agent_structured_plan",
            "plan_data": {"target_race": "10K", "target_race_date": "2026-11-22"},
        }

        with patch("agent.trainer_agent._storage.get_active_training_plan", return_value=existing), patch(
            "agent.trainer_agent._storage.list_training_plan_sessions",
            return_value=[
                {"duration_min": 40, "day_index": 1, "session_type": "running_z2"},
                {"duration_min": 50, "day_index": 2, "session_type": "running_quality"},
            ],
        ), patch("agent.trainer_agent._storage.update_training_plan", return_value={
            "id": "plan-db-1",
            "title": "Plan hacia 10K",
            "objective": "Preparación para 10K",
            "difficulty": "moderate",
            "duration_weeks": 10,
            "status": "active",
            "source": "agent_structured_plan",
            "plan_data": {"target_race": "10K", "target_race_date": "2026-11-22"},
        }) as mocked_update, patch("agent.trainer_agent._storage.create_training_plan") as mocked_create, patch(
            "agent.trainer_agent._save_user_profile"
        ), patch("agent.trainer_agent._save_history_entry"):
            out = await TrainerAgent.chat(agent, "Ajusta mi plan de esta semana")

        mocked_update.assert_called_once()
        mocked_create.assert_not_called()
        assert "Cambios de versión" in out or "Cambios de version" in out
        assert "Volumen semanal estimado" in out

    @pytest.mark.asyncio
    async def test_chat_planning_validation_error_returns_user_facing_message(self):
        from agent.trainer_agent import TrainerAgent

        agent = object.__new__(TrainerAgent)
        agent.user_profile = {
            "goals": {"target_race": "10K", "target_race_date": "2026-11-22"},
            "health": {},
        }
        agent.conversation_history = []
        agent.tools_schema = []
        agent.mcp_session = MagicMock()
        agent.mcp_read_only = True
        agent.model = "test-model"
        agent._build_messages = lambda _msg: []
        agent.client = MagicMock()
        agent.client.chat = MagicMock()
        agent.client.chat.completions = MagicMock()
        agent.client.chat.completions.create = AsyncMock(side_effect=AssertionError("LLM should not be called"))

        with patch("agent.trainer_agent._storage.get_active_training_plan", return_value=None), patch(
            "agent.trainer_agent._validate_structured_plan", return_value=["Error de validación de prueba"]
        ), patch("agent.trainer_agent._storage.create_training_plan") as mocked_create, patch(
            "agent.trainer_agent._storage.update_training_plan"
        ) as mocked_update, patch("agent.trainer_agent._save_history_entry"):
            out = await TrainerAgent.chat(agent, "Planifícame para mi 10K")

        mocked_create.assert_not_called()
        mocked_update.assert_not_called()
        assert "No pude persistir el plan propuesto" in out
        assert "Error de validación de prueba" in out


class TestMcpReadOnlyPolicy:
    def test_is_write_mcp_tool_detects_mutations(self):
        assert _is_write_mcp_tool("create_custom_food")
        assert _is_write_mcp_tool("update_custom_food")
        assert _is_write_mcp_tool("delete_workout")
        assert _is_write_mcp_tool("schedule_workout")
        assert _is_write_mcp_tool("upload_workout")

    def test_is_write_mcp_tool_allows_read_tools(self):
        assert not _is_write_mcp_tool("get_activity")
        assert not _is_write_mcp_tool("get_training_status")

    def test_read_only_block_message_has_expected_shape(self):
        payload = json.loads(_build_mcp_read_only_block_message("schedule_workout"))
        assert payload["error"] == "mcp_read_only_mode"
        assert payload["tool"] == "schedule_workout"

    def test_build_tools_schema_can_be_filtered_for_read_only(self):
        tools = [
            {
                "name": "get_activity",
                "description": "read",
                "input_schema": {"type": "object", "properties": {}},
            },
            {
                "name": "create_run_workout",
                "description": "write",
                "input_schema": {"type": "object", "properties": {}},
            },
        ]
        filtered = [t for t in tools if not _is_write_mcp_tool(t["name"])]
        schema = _build_tools_schema(filtered)
        names = {(item.get("function") or {}).get("name") for item in schema}
        assert "get_activity" in names
        assert "create_run_workout" not in names

    @pytest.mark.asyncio
    async def test_chat_blocks_write_tool_calls_in_read_only_mode(self):
        from agent.trainer_agent import TrainerAgent

        tool_call = MagicMock()
        tool_call.id = "tc_1"
        tool_call.function = MagicMock()
        tool_call.function.name = "schedule_workout"
        tool_call.function.arguments = "{}"

        msg_with_tool = MagicMock()
        msg_with_tool.tool_calls = [tool_call]
        msg_with_tool.content = ""

        msg_final = MagicMock()
        msg_final.tool_calls = None
        msg_final.content = "respuesta final"

        choice1 = MagicMock()
        choice1.message = msg_with_tool
        choice1.finish_reason = "tool_calls"

        choice2 = MagicMock()
        choice2.message = msg_final
        choice2.finish_reason = "stop"

        response1 = MagicMock()
        response1.choices = [choice1]
        response1.usage = None

        response2 = MagicMock()
        response2.choices = [choice2]
        response2.usage = None

        agent = object.__new__(TrainerAgent)
        agent.mcp_session = MagicMock()
        agent.user_profile = {}
        agent.conversation_history = []
        agent.tools_schema = [{
            "type": "function",
            "function": {
                "name": "schedule_workout",
                "description": "write",
                "parameters": {"type": "object", "properties": {}}
            }
        }]
        agent.mcp_read_only = True
        agent.client = MagicMock()
        agent.model = "test-model"
        agent.client.chat = MagicMock()
        agent.client.chat.completions = MagicMock()
        agent.client.chat.completions.create = AsyncMock(side_effect=[response1, response2])
        agent._build_messages = lambda _msg: []

        with patch("agent.trainer_agent.call_tool", new=AsyncMock(side_effect=AssertionError("Should not call MCP write tools"))):
            with patch("agent.trainer_agent._save_history_entry"):
                out = await TrainerAgent.chat(agent, "Programa un entrenamiento para mañana")

        assert out == "respuesta final"

"""
tests/test_storage.py
Tests de no-regresión para la capa de persistencia.
"""

from agent import storage


class _DummyResult:
    def __init__(self, data=None):
        self.data = data if data is not None else []


class _DummyTable:
    def __init__(self, parent, name: str):
        self.parent = parent
        self.name = name
        self._mode = None
        self._payload = None

    def select(self, *_args, **_kwargs):
        self._mode = "select"
        return self

    def eq(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def insert(self, payload):
        self._mode = "insert"
        self._payload = payload
        return self

    def update(self, payload):
        self._mode = "update"
        self._payload = payload
        return self

    def execute(self):
        if self._mode == "insert":
            self.parent.last_insert = self._payload
            return _DummyResult([])
        if self._mode == "update":
            self.parent.last_update = self._payload
            return _DummyResult([])
        return _DummyResult([])


class _DummySupabase:
    def __init__(self):
        self.last_insert = None
        self.last_update = None

    def table(self, name: str):
        return _DummyTable(self, name)


def test_sanitize_credentials_removes_garmin_password_without_mutating_input():
    original = {
        "garmin_email": "runner@example.com",
        "garmin_password": "super-secret",
        "other": "value",
    }

    cleaned = storage._sanitize_credentials_for_storage(original)

    assert "garmin_password" not in cleaned
    assert cleaned["garmin_email"] == "runner@example.com"
    assert cleaned["other"] == "value"
    # No debe mutar el diccionario original
    assert original["garmin_password"] == "super-secret"


def test_update_user_credentials_never_persists_garmin_password(monkeypatch):
    fake_sb = _DummySupabase()

    monkeypatch.setattr(storage, "_require_active_user_id", lambda: "user-1")
    monkeypatch.setattr(storage, "_require_supabase", lambda: fake_sb)

    storage.update_user_credentials(
        {
            "garmin_email": "runner@example.com",
            "garmin_password": "super-secret",
            "timezone": "Europe/Madrid",
        }
    )

    assert fake_sb.last_update is not None
    persisted_credentials = fake_sb.last_update["credentials"]
    assert "garmin_password" not in persisted_credentials
    assert persisted_credentials["garmin_email"] == "runner@example.com"
    assert persisted_credentials["timezone"] == "Europe/Madrid"


def test_register_app_user_never_persists_garmin_password(monkeypatch):
    fake_sb = _DummySupabase()
    monkeypatch.setattr(storage, "_require_supabase", lambda: fake_sb)

    result = storage.register_app_user(
        "runner",
        "123456",
        credentials={
            "garmin_email": "runner@example.com",
            "garmin_password": "super-secret",
        },
    )

    assert result["ok"] is True
    assert fake_sb.last_insert is not None
    persisted_credentials = fake_sb.last_insert["credentials"]
    assert "garmin_password" not in persisted_credentials
    assert persisted_credentials["garmin_email"] == "runner@example.com"


class _MemoryResult:
    def __init__(self, data=None):
        self.data = data if data is not None else []


class _MemoryTable:
    def __init__(self, parent, name: str):
        self.parent = parent
        self.name = name
        self._mode = None
        self._payload = None
        self._filters: list[tuple[str, str, object]] = []
        self._order: tuple[str, bool] | None = None
        self._limit = None

    def select(self, *_args, **_kwargs):
        self._mode = "select"
        return self

    def eq(self, field, value):
        self._filters.append(("eq", field, value))
        return self

    def neq(self, field, value):
        self._filters.append(("neq", field, value))
        return self

    def order(self, field, desc=False):
        self._order = (field, bool(desc))
        return self

    def limit(self, n):
        self._limit = int(n)
        return self

    def insert(self, payload):
        self._mode = "insert"
        self._payload = payload
        return self

    def update(self, payload):
        self._mode = "update"
        self._payload = payload
        return self

    def upsert(self, payload):
        self._mode = "upsert"
        self._payload = payload
        return self

    def delete(self):
        self._mode = "delete"
        return self

    def _rows(self):
        return self.parent.rows.setdefault(self.name, [])

    def _match(self, row):
        for op, field, value in self._filters:
            if op == "eq" and row.get(field) != value:
                return False
            if op == "neq" and row.get(field) == value:
                return False
        return True

    def _selected(self):
        items = [dict(r) for r in self._rows() if self._match(r)]
        if self._order:
            field, is_desc = self._order
            items = sorted(items, key=lambda x: x.get(field), reverse=is_desc)
        if self._limit is not None:
            items = items[: self._limit]
        return items

    def execute(self):
        rows = self._rows()

        if self._mode == "select":
            return _MemoryResult(self._selected())

        if self._mode == "insert":
            payload = self._payload
            if isinstance(payload, list):
                for item in payload:
                    rows.append(dict(item))
                return _MemoryResult([dict(x) for x in payload])
            rows.append(dict(payload))
            return _MemoryResult([dict(payload)])

        if self._mode == "update":
            updated = []
            for row in rows:
                if self._match(row):
                    row.update(dict(self._payload or {}))
                    updated.append(dict(row))
            return _MemoryResult(updated)

        if self._mode == "delete":
            kept = []
            deleted = []
            for row in rows:
                if self._match(row):
                    deleted.append(dict(row))
                else:
                    kept.append(row)
            self.parent.rows[self.name] = kept
            return _MemoryResult(deleted)

        if self._mode == "upsert":
            payload = dict(self._payload or {})
            if self.name == "training_plan_version":
                match = None
                for row in rows:
                    if row.get("plan_id") == payload.get("plan_id") and row.get("version_number") == payload.get("version_number"):
                        match = row
                        break
                if match is not None:
                    match.update(payload)
                else:
                    rows.append(payload)
            else:
                rows.append(payload)
            return _MemoryResult([payload])

        return _MemoryResult([])


class _MemorySupabase:
    def __init__(self):
        self.rows = {
            "training_plan": [],
            "training_plan_session": [],
            "training_plan_version": [],
        }

    def table(self, name: str):
        return _MemoryTable(self, name)


def test_create_training_plan_enforces_single_active_and_versions(monkeypatch):
    fake_sb = _MemorySupabase()
    fake_sb.rows["training_plan"].append(
        {
            "id": "old-plan",
            "app_user_id": "user-1",
            "title": "Plan anterior",
            "status": "active",
        }
    )

    monkeypatch.setattr(storage, "_require_active_user_id", lambda: "user-1")
    monkeypatch.setattr(storage, "_require_supabase", lambda: fake_sb)

    created = storage.create_training_plan(
        {
            "title": "Plan 10K",
            "description": "Base + específico",
            "objective": "Bajar de 45'",
            "difficulty": "moderate",
            "duration_weeks": 12,
            "status": "active",
        },
        sessions=[
            {
                "week_index": 1,
                "day_index": 2,
                "session_type": "running",
                "duration_min": 50,
                "intensity": "Z2",
                "exercises": ["rodaje"],
            }
        ],
    )

    plans = fake_sb.rows["training_plan"]
    active = [p for p in plans if p.get("app_user_id") == "user-1" and p.get("status") == "active"]
    assert len(active) == 1
    assert active[0]["id"] == created["id"]
    assert any(p.get("id") == "old-plan" and p.get("status") == "inactive" for p in plans)

    sessions = fake_sb.rows["training_plan_session"]
    assert len(sessions) == 1
    assert sessions[0]["plan_id"] == created["id"]

    versions = fake_sb.rows["training_plan_version"]
    assert len(versions) == 1
    assert versions[0]["plan_id"] == created["id"]
    assert versions[0]["version_number"] == 1


def test_update_training_plan_creates_new_version_per_edit(monkeypatch):
    fake_sb = _MemorySupabase()

    monkeypatch.setattr(storage, "_require_active_user_id", lambda: "user-1")
    monkeypatch.setattr(storage, "_require_supabase", lambda: fake_sb)

    created = storage.create_training_plan(
        {
            "title": "Plan Maratón",
            "status": "inactive",
        }
    )

    updated = storage.update_training_plan(
        created["id"],
        {"title": "Plan Maratón v2", "description": "Ajustado por fatiga"},
        change_reason="edit",
    )

    assert updated["title"] == "Plan Maratón v2"
    versions = [v for v in fake_sb.rows["training_plan_version"] if v.get("plan_id") == created["id"]]
    assert len(versions) == 2
    numbers = sorted(v.get("version_number") for v in versions)
    assert numbers == [1, 2]


def test_activate_training_plan_switches_active_plan_and_versions(monkeypatch):
    fake_sb = _MemorySupabase()

    monkeypatch.setattr(storage, "_require_active_user_id", lambda: "user-1")
    monkeypatch.setattr(storage, "_require_supabase", lambda: fake_sb)

    first = storage.create_training_plan({"title": "Plan A", "status": "active"})
    second = storage.create_training_plan({"title": "Plan B", "status": "inactive"})

    storage.activate_training_plan(second["id"])

    p1 = storage.get_training_plan(first["id"])
    p2 = storage.get_training_plan(second["id"])
    assert p1 is not None and p1["status"] == "inactive"
    assert p2 is not None and p2["status"] == "active"

    versions = [v for v in fake_sb.rows["training_plan_version"] if v.get("plan_id") == second["id"]]
    assert len(versions) == 2

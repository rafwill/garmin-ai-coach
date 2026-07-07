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

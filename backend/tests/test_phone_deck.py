from nightshift.engine.credentials import MemoryCredentialStore
from nightshift.engine.phone_deck import build_phone_deck


class FakeStore:
    def get_project(self, project_id):
        return {"name": "Example"}

    def list_plans(self, project_id):
        assert project_id == "example"
        return [{"id": "example-plan"}]

    def list_phases(self, plan_id):
        assert plan_id == "example-plan"
        return [
            {"title": "secret-value C:\\private\\file", "status": "parked", "prompt": "raw prompt"},
            *[{"title": f"Next {n}", "status": "pending"} for n in range(5)],
        ]


def test_phone_deck_limits_and_redacts_project_scoped_data():
    MemoryCredentialStore().set_secret("phone-test", "secret-value")
    deck = build_phone_deck(FakeStore(), "example")
    assert deck["state"] == "attention"
    assert len(deck["next"]) == 3
    assert "secret-value" not in str(deck)
    assert "private" not in str(deck)
    assert "raw prompt" not in str(deck)
    assert "[REDACTED_SECRET]" in str(deck)

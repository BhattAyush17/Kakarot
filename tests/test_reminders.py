from __future__ import annotations

from services import reminders


def test_reminder_is_idempotent_and_restart_safe(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KAKAROT_DATA_DIR", str(tmp_path))
    first = reminders.create(
        "Check the build", delay_seconds=60, operation_id="turn-1", now=1_000
    )
    duplicate = reminders.create(
        "Different retry text", delay_seconds=90, operation_id="turn-1", now=1_001
    )
    assert duplicate["id"] == first["id"]
    assert duplicate["message"] == "Check the build"
    assert reminders.enqueue_due(now=1_059) == 0
    assert reminders.enqueue_due(now=1_060) == 1

    pending = reminders.pending_notifications(now=1_060)
    assert len(pending) == 1
    reminders.mark_delivery(pending[0]["outbox_id"], delivered=True, now=1_061)
    completed = reminders.list_reminders(include_completed=True)
    assert completed[0]["state"] == "delivered"


def test_cancelled_reminder_never_enters_outbox(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KAKAROT_DATA_DIR", str(tmp_path))
    row = reminders.create("Cancel me", delay_seconds=1, now=100)
    assert reminders.cancel(row["id"])
    assert reminders.enqueue_due(now=200) == 0
    assert reminders.pending_notifications(now=200) == []

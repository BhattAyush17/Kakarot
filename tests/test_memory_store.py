from __future__ import annotations

from services import memory_store


def test_memory_survives_connections_and_private_filter(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KAKAROT_DATA_DIR", str(tmp_path))
    public = memory_store.remember("Project Atlas uses Python 3.12", project="Atlas")
    private = memory_store.remember(
        "Private Atlas access phrase", project="Atlas", private=True
    )

    found = memory_store.search("Atlas Python", project="Atlas")
    assert [row["id"] for row in found] == [public["id"]]
    assert private["id"] not in {row["id"] for row in found}

    local_found = memory_store.search(
        "Private Atlas", project="Atlas", include_private=True
    )
    assert [row["id"] for row in local_found] == [private["id"]]

    assert memory_store.forget(public["id"])
    assert memory_store.search("Python", project="Atlas") == []


def test_memory_validation_and_limits(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KAKAROT_DATA_DIR", str(tmp_path))
    try:
        memory_store.remember("")
    except ValueError as exc:
        assert "empty" in str(exc)
    else:
        raise AssertionError("empty memory should fail")

    row = memory_store.remember("Remember this", kind="Preference!!!")
    assert row["kind"] == "preference"
    assert memory_store.list_memories(limit=9999)[0]["id"] == row["id"]


def test_memory_duplicate_detection(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KAKAROT_DATA_DIR", str(tmp_path))
    first = memory_store.remember("Project Atlas uses Python 3.12")
    duplicate = memory_store.remember("project   atlas uses PYTHON 3.12")
    assert duplicate["id"] == first["id"]
    assert memory_store.list_memories(limit=10) == [first]


def test_memory_correction_supersedes_old(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KAKAROT_DATA_DIR", str(tmp_path))
    old = memory_store.remember("Project Atlas uses Python 3.11", project="Atlas")
    corrected = memory_store.correct(old["id"], "Project Atlas uses Python 3.12")

    assert corrected["supersedes_id"] == old["id"]
    assert corrected["project"] == "Atlas"
    assert corrected["source_uri"] == "correction"

    found = memory_store.search("Atlas Python", project="Atlas")
    assert [row["id"] for row in found] == [corrected["id"]]
    assert old["id"] not in {row["id"] for row in found}

    listed = {row["id"]: row for row in memory_store.list_memories()}
    assert listed[old["id"]]["superseded"] is True
    assert listed[corrected["id"]]["superseded"] is False

    exported = memory_store.export_memories()
    assert {row["id"] for row in exported} == {old["id"], corrected["id"]}

    try:
        memory_store.correct(99999, "Nothing to correct")
    except ValueError as exc:
        assert "not found" in str(exc)
    else:
        raise AssertionError("correcting a missing memory should fail")


def test_memory_retention_purge(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KAKAROT_DATA_DIR", str(tmp_path))
    expired = memory_store.remember(
        "Temporary observation note", expires_at=1_000
    )
    durable = memory_store.remember("Project Atlas uses Python 3.12")
    pinned = memory_store.remember("Pinned note", expires_at=1_000)
    import sqlite3

    with sqlite3.connect(memory_store.database_path()) as connection:
        connection.execute("UPDATE memories SET pinned = 1 WHERE id = ?", (pinned["id"],))

    purged = memory_store.purge_expired(now=2_000)
    assert purged == 1
    remaining = {row["id"] for row in memory_store.export_memories()}
    assert expired["id"] not in remaining
    assert durable["id"] in remaining
    assert pinned["id"] in remaining

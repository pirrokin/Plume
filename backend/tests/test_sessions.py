import copy
import os
import time

import pytest

from plume import config
from plume.store import DocumentError, Store


def _age(path, seconds):
    t = time.time() - seconds
    os.utime(path, (t, t))


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "sessions")


def test_closed_tab_is_deleted_after_grace(store, sample_pdf, monkeypatch):
    monkeypatch.setattr(config, "SESSION_CLOSE_GRACE_SECONDS", 10)
    doc = store.create(sample_pdf.read_bytes(), "a.pdf")
    root = store.root / doc.id
    store.close_soon(doc.id)
    assert store.sweep(force=True) == 0  # still within the grace period
    _age(root / "closing", 30)
    _age(root / "alive", 31)
    assert store.sweep(force=True) == 1
    assert not root.exists()
    with pytest.raises(DocumentError) as err:
        store.get(doc.id)
    assert err.value.status == 404


def test_reload_cancels_close(store, sample_pdf, monkeypatch):
    monkeypatch.setattr(config, "SESSION_CLOSE_GRACE_SECONDS", 10)
    doc = store.create(sample_pdf.read_bytes(), "a.pdf")
    root = store.root / doc.id
    store.close_soon(doc.id)
    _age(root / "closing", 30)
    assert store.ping(doc.id)  # the reloaded page sends a heartbeat
    assert store.sweep(force=True) == 0
    assert root.exists()


def test_idle_and_lifetime_expiry(store, sample_pdf, monkeypatch):
    monkeypatch.setattr(config, "SESSION_IDLE_MINUTES", 30)
    monkeypatch.setattr(config, "SESSION_MAX_HOURS", 12)
    idle = store.create(sample_pdf.read_bytes(), "idle.pdf")
    old = store.create(sample_pdf.read_bytes(), "old.pdf")
    fresh = store.create(sample_pdf.read_bytes(), "fresh.pdf")
    _age(store.root / idle.id / "alive", 31 * 60)
    _age(store.root / old.id / "created", 13 * 3600)
    assert store.sweep(force=True) == 2
    assert (store.root / fresh.id).exists()


def test_explicit_delete_removes_everything(store, sample_pdf):
    doc = store.create(sample_pdf.read_bytes(), "a.pdf")
    store.delete(doc.id)
    assert not (store.root / doc.id).exists()
    assert list(store.root.iterdir()) == []


def test_two_processes_share_a_document(store, sample_pdf):
    """A second server process sees the edits and the undo of the first one."""
    doc = store.create(sample_pdf.read_bytes(), "a.pdf")
    other = Store(store.root)  # another process: its own memory, same folder
    twin = other.get(doc.id)
    model = doc.public_model(0)
    block = next(b for b in model["blocks"] if b["runs"][0]["text"].startswith("Rapport"))
    runs = copy.deepcopy(block["runs"])
    runs[0]["text"] = "Rapport annuel 2027"
    box = {"x": block["x"], "y": block["baseline"], "width": block["width"], "wrap": False, "align": "left"}
    doc.edit(0, block["id"], doc.version, block["sig"], runs, box)
    twin = other.get(doc.id)
    assert twin.version == doc.version == 1
    assert "Rapport annuel 2027" in twin.doc[0].get_text()
    twin.undo()
    assert store.get(doc.id).version == 0
    assert "Rapport annuel 2025" in store.get(doc.id).doc[0].get_text()

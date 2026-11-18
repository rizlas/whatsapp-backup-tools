"""End-to-end --prune-missing through the real CLI. Synthesized data only."""

import hashlib
import os
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest
import wab_archiver.main as wa

N_MEDIA = 12  # one missing file = 8.3%, below the default 10% limit


def _snapshot(root: Path) -> dict[str, str]:
    snap = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            full = Path(dirpath) / name
            snap[str(full.relative_to(root))] = hashlib.md5(full.read_bytes()).hexdigest()
    return snap


def _make_msgstore(path: Path):
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT);
        CREATE TABLE jid_map (lid_row_id INTEGER, jid_row_id INTEGER);
        CREATE TABLE chat (_id INTEGER PRIMARY KEY, subject TEXT, jid_row_id INTEGER);
        CREATE TABLE message (
            _id INTEGER PRIMARY KEY, timestamp INTEGER, sender_jid_row_id INTEGER,
            from_me INTEGER, chat_row_id INTEGER, key_id TEXT, message_type INTEGER,
            text_data TEXT
        );
        CREATE TABLE message_media (
            _id INTEGER PRIMARY KEY, file_path TEXT, mime_type TEXT, chat_row_id INTEGER,
            message_row_id INTEGER, message_url TEXT, media_name TEXT
        );
        INSERT INTO jid VALUES (1, '390000000001');
        INSERT INTO chat VALUES (1, NULL, 1);
    """)
    for i in range(N_MEDIA):
        conn.execute(
            "INSERT INTO message VALUES (?, 1781092800000, 1, 0, 1, ?, 0, 'pic')",
            (i + 1, f"k{i}"),
        )
        conn.execute(
            "INSERT INTO message_media VALUES (?, ?, 'image/jpeg', 1, ?, NULL, NULL)",
            (i + 1, f"Media/WhatsApp Images/m{i}.jpg", i + 1),
        )
    conn.commit()
    conn.close()


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "phone"
    images = root / "Media" / "WhatsApp Images"
    images.mkdir(parents=True)
    for i in range(N_MEDIA):
        (images / f"m{i}.jpg").write_bytes(f"JPEG-{i}".encode() * 50)
    msgstore = tmp_path / "msgstore.db"
    _make_msgstore(msgstore)
    contacts = tmp_path / "contacts.csv"
    contacts.write_text("+390000000001,Alice\n", encoding="utf-8")
    out = tmp_path / "archive"
    base = [
        "wab-archiver",
        "archive",
        "--msgstore",
        str(msgstore),
        "--wa-root",
        str(root),
        "--contacts",
        str(contacts),
        "--output",
        str(out),
    ]
    received = out / "Contacts" / "Alice (00390000000001)" / "2026" / "Received"
    return root, images, out, received, base


def _run(argv):
    with patch("sys.argv", argv):
        wa.main()


class TestE2EPruneMissing:
    def test_phone_deletion_pruned_sources_untouched(self, setup):
        root, images, out, received, base = setup
        _run(base)
        assert all((received / f"m{i}.jpg").exists() for i in range(N_MEDIA))

        os.remove(images / "m0.jpg")  # deleted on the phone
        before = _snapshot(root)
        _run(base + ["--prune-missing"])

        assert not (received / "m0.jpg").exists()
        assert all((received / f"m{i}.jpg").exists() for i in range(1, N_MEDIA))
        assert _snapshot(root) == before

    def test_without_flag_nothing_is_pruned(self, setup):
        root, images, out, received, base = setup
        _run(base)
        os.remove(images / "m0.jpg")
        _run(base)
        assert (received / "m0.jpg").exists()

    def test_mass_disappearance_aborts_with_exit_2(self, setup):
        root, images, out, received, base = setup
        _run(base)
        for i in range(6):  # 50% of the sources vanish (e.g. half-synced phone)
            os.remove(images / f"m{i}.jpg")
        before = _snapshot(root)

        with pytest.raises(SystemExit) as exc:
            _run(base + ["--prune-missing"])

        assert exc.value.code == 2
        assert all((received / f"m{i}.jpg").exists() for i in range(N_MEDIA))
        assert _snapshot(root) == before

    def test_dry_run_prunes_nothing(self, setup):
        root, images, out, received, base = setup
        _run(base)
        os.remove(images / "m0.jpg")
        _run(base + ["--prune-missing", "--dry-run"])
        assert (received / "m0.jpg").exists()

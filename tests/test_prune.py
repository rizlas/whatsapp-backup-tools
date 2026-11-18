"""Tests for wab_archiver.prune (--prune-missing). Synthesized data only."""

import hashlib
import os
from unittest.mock import patch

import pytest
import wab_archiver.archive_db as arc
import wab_archiver.main as wa
from wab_archiver import prune

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _snapshot(root):
    """{relative path: md5} of every file under root, to prove it is untouched."""
    snap = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            full = os.path.join(dirpath, name)
            with open(full, "rb") as f:
                snap[os.path.relpath(full, root)] = hashlib.md5(f.read()).hexdigest()
    return snap


def _write(path, content=b"data"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


class Env:
    """An output archive plus one wa_root, both under tmp_path."""

    def __init__(self, tmp_path):
        self.out = tmp_path / "archive"
        self.root = tmp_path / "phone"
        self.out.mkdir()
        (self.root / "Media").mkdir(parents=True)
        self.conn = arc.open_archive_db(str(self.out))

    def source(self, original, content=b"data"):
        return _write(self.root.joinpath(*original.split("/")), content)

    def archived(self, original, archive_rel, content=b"data", write=True):
        arc.record_file_archived(
            self.conn.cursor(),
            original,
            hashlib.md5(content).digest(),
            archive_rel,
            len(content),
            0,
            None,
        )
        self.conn.commit()
        if write:
            return _write(self.out.joinpath(*archive_rel.split("/")), content)
        return self.out.joinpath(*archive_rel.split("/"))

    def rows(self, original):
        return self.conn.execute(
            "SELECT COUNT(*) FROM archive_copies WHERE original_path = ?", (original,)
        ).fetchone()[0]

    def run(self, logger, **kw):
        return prune.prune_missing(self.conn, str(self.out), [str(self.root)], logger, **kw)


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.conn.close()


def _fill_present(env, n):
    """n archived files whose sources still exist (keeps the missing ratio low)."""
    for i in range(n):
        orig = f"Media/WhatsApp Images/keep{i}.jpg"
        env.source(orig)
        env.archived(orig, f"Contacts/Alice/2024/Received/keep{i}.jpg")


# ---------------------------------------------------------------------------
# Behaviour
# ---------------------------------------------------------------------------


class TestPruneMissing:
    def test_present_sources_are_kept(self, env, logger):
        _fill_present(env, 3)
        before = _snapshot(env.root)
        stats = env.run(logger)
        assert stats == prune.PruneStats(3, 0, 0, False)
        assert (env.out / "Contacts/Alice/2024/Received/keep0.jpg").exists()
        assert _snapshot(env.root) == before

    def test_missing_source_copy_deleted(self, env, logger):
        _fill_present(env, 19)
        gone = env.archived("Media/WhatsApp Images/gone.jpg", "Contacts/Bob/2023/Sent/gone.jpg")
        before = _snapshot(env.root)

        stats = env.run(logger)

        assert stats.missing == 1 and stats.deleted == 1 and not stats.aborted
        assert not gone.exists()
        assert env.rows("Media/WhatsApp Images/gone.jpg") == 0
        assert _snapshot(env.root) == before

    def test_empty_folders_removed_up_to_contacts(self, env, logger):
        _fill_present(env, 19)
        env.archived("Media/WhatsApp Images/gone.jpg", "Contacts/Bob/2023/Sent/gone.jpg")
        env.run(logger)
        assert not (env.out / "Contacts" / "Bob").exists()
        assert (env.out / "Contacts").is_dir()

    def test_dry_run_deletes_nothing(self, env, logger):
        _fill_present(env, 19)
        gone = env.archived("Media/WhatsApp Images/gone.jpg", "Contacts/Bob/2023/Sent/gone.jpg")
        stats = env.run(logger, dry_run=True)
        assert stats.deleted == 1
        assert gone.exists()
        assert env.rows("Media/WhatsApp Images/gone.jpg") == 1

    def test_vcards_ignored(self, env, logger):
        _fill_present(env, 1)
        card = env.archived("vcard:123", "Contacts/Alice/2024/Received/card.vcf")
        stats = env.run(logger)
        assert stats.originals == 1
        assert card.exists()

    def test_source_in_second_root_counts_as_present(self, env, tmp_path, logger):
        second = tmp_path / "old"
        (second / "Media").mkdir(parents=True)
        _write(second / "Media" / "WhatsApp Images" / "old.jpg")
        kept = env.archived("Media/WhatsApp Images/old.jpg", "Contacts/Bob/2020/Sent/old.jpg")
        stats = prune.prune_missing(env.conn, str(env.out), [str(env.root), str(second)], logger)
        assert stats.missing == 0
        assert kept.exists()

    def test_shared_archive_file_kept_for_present_source(self, env, logger):
        _fill_present(env, 19)
        shared = "Contacts/Alice/2024/Received/shared.jpg"
        env.source("Media/WhatsApp Images/a.jpg")
        env.archived("Media/WhatsApp Images/a.jpg", shared)
        env.archived("Media/WhatsApp Images/b.jpg", shared)  # b is gone from the phone
        env.run(logger)
        assert (env.out / shared).exists()
        assert env.rows("Media/WhatsApp Images/b.jpg") == 0
        assert env.rows("Media/WhatsApp Images/a.jpg") == 1


# ---------------------------------------------------------------------------
# Safety: the source folders must never be touched
# ---------------------------------------------------------------------------


class TestPruneSafety:
    def test_ratio_above_limit_aborts(self, env, logger):
        _fill_present(env, 1)
        gone = env.archived("Media/WhatsApp Images/gone.jpg", "Contacts/Bob/2023/Sent/gone.jpg")
        stats = env.run(logger)  # 1 of 2 missing = 50% > 10%
        assert stats.aborted and stats.deleted == 0
        assert gone.exists()
        assert env.rows("Media/WhatsApp Images/gone.jpg") == 1

    def test_ratio_limit_is_configurable(self, env, logger):
        _fill_present(env, 1)
        gone = env.archived("Media/WhatsApp Images/gone.jpg", "Contacts/Bob/2023/Sent/gone.jpg")
        stats = env.run(logger, max_ratio=0.5)
        assert not stats.aborted
        assert not gone.exists()

    def test_wa_root_without_media_aborts(self, tmp_path, logger):
        out = tmp_path / "archive"
        out.mkdir()
        empty_root = tmp_path / "unmounted"
        empty_root.mkdir()
        conn = arc.open_archive_db(str(out))
        arc.record_file_archived(
            conn.cursor(), "Media/x.jpg", b"\x00", "Contacts/A/2024/Sent/x.jpg", 1, 0, None
        )
        conn.commit()
        _write(out / "Contacts/A/2024/Sent/x.jpg")
        stats = prune.prune_missing(conn, str(out), [str(empty_root)], logger, max_ratio=1)
        conn.close()
        assert stats.aborted
        assert (out / "Contacts/A/2024/Sent/x.jpg").exists()

    def test_overlapping_output_and_root_aborts(self, tmp_path, logger):
        root = tmp_path / "phone"
        (root / "Media").mkdir(parents=True)
        out = root / "archive"  # output inside the source folder
        out.mkdir()
        conn = arc.open_archive_db(str(out))
        stats = prune.prune_missing(conn, str(out), [str(root)], logger, max_ratio=1)
        conn.close()
        assert stats.aborted

    def test_no_wa_root_aborts(self, env, logger):
        stats = prune.prune_missing(env.conn, str(env.out), [], logger)
        assert stats.aborted

    @pytest.mark.parametrize(
        "bad_rel",
        [
            "../phone/Media/WhatsApp Images/orig.jpg",
            "Contacts/../../phone/Media/WhatsApp Images/orig.jpg",
            "Media/WhatsApp Images/orig.jpg",
            "wab-archiver.log",
        ],
    )
    def test_archive_path_outside_contacts_groups_refused(self, env, logger, bad_rel):
        _fill_present(env, 19)
        env.source("Media/WhatsApp Images/orig.jpg")
        env.archived("Media/WhatsApp Images/gone.jpg", bad_rel, write=False)
        _write(env.out / "wab-archiver.log", b"log")
        before = _snapshot(env.root)

        env.run(logger)

        assert _snapshot(env.root) == before
        assert (env.out / "wab-archiver.log").exists()
        # rows kept so the refused copy stays tracked
        assert env.rows("Media/WhatsApp Images/gone.jpg") == 1

    @pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
    def test_symlink_into_source_never_followed(self, env, logger):
        _fill_present(env, 19)
        original = env.source("Media/WhatsApp Images/orig.jpg", b"precious")
        link = env.out / "Contacts" / "Eve" / "2024" / "Sent"
        link.mkdir(parents=True)
        os.symlink(original.parent, link / "evil")
        env.archived(
            "Media/WhatsApp Images/gone.jpg", "Contacts/Eve/2024/Sent/evil/orig.jpg", write=False
        )
        before = _snapshot(env.root)

        env.run(logger)

        assert original.read_bytes() == b"precious"
        assert _snapshot(env.root) == before


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


class TestPruneCli:
    def test_flags_parsed(self, tmp_path):
        argv = ["wa", "-o", str(tmp_path), "--wa-root", "/wa", "--prune-missing"]
        with patch("sys.argv", argv + ["--prune-max-ratio", "0.25"]):
            args = wa.parse_args()
        assert args.prune_missing is True
        assert args.prune_max_ratio == 0.25

    def test_default_off(self, tmp_path):
        with patch("sys.argv", ["wa", "-o", str(tmp_path), "--wa-root", "/wa"]):
            args = wa.parse_args()
        assert args.prune_missing is False
        assert args.prune_max_ratio == prune.DEFAULT_MAX_RATIO

    def test_rejected_with_ios_backup(self, tmp_path):
        argv = ["wa", "-o", str(tmp_path), "--ios-backup", "/b", "--prune-missing"]
        with patch("sys.argv", argv), pytest.raises(SystemExit):
            wa.parse_args()

    @pytest.mark.parametrize("ratio", ["-0.1", "1.5"])
    def test_ratio_out_of_range_rejected(self, tmp_path, ratio):
        argv = ["wa", "-o", str(tmp_path), "--wa-root", "/wa", "--prune-missing"]
        with patch("sys.argv", argv + ["--prune-max-ratio", ratio]), pytest.raises(SystemExit):
            wa.parse_args()

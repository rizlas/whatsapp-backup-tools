"""
Prune archived media whose source file no longer exists in any wa_root.

Opt-in (``--prune-missing``): it makes the archive mirror the phone instead of
accumulating everything ever seen. Only archive copies inside the output folder
(``Contacts/`` and ``Groups/``) are ever deleted; the source folders are read
only, never written. Safety stops:

- output and any wa_root must not overlap;
- every wa_root must contain a ``Media`` folder (an empty or unmounted source
  would otherwise look like "everything was deleted");
- if the share of original files to prune exceeds ``max_ratio`` the prune is
  aborted, nothing is deleted, and the caller gets ``aborted=True``.
"""

import logging
import os
import sqlite3
from typing import NamedTuple

# Only these archive subtrees hold media copies that may be pruned.
PRUNABLE_TOP_DIRS = ("Contacts", "Groups")

DEFAULT_MAX_RATIO = 0.10


class PruneStats(NamedTuple):
    originals: int  # original files tracked (vCards excluded)
    missing: int  # originals no longer present in any wa_root
    deleted: int  # archive copies deleted (or that would be, in dry run)
    aborted: bool  # safety stop triggered, nothing deleted


def _real(path: str) -> str:
    return os.path.realpath(os.path.abspath(path))


def _is_within(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:  # different drives on Windows
        return False


def _source_exists(original_path: str, wa_roots: list[str]) -> bool:
    parts = original_path.split("/")
    return any(os.path.exists(os.path.join(root, *parts)) for root in wa_roots)


def _safe_archive_target(rel_path: str, output_root: str, wa_roots: list[str]) -> str | None:
    """Absolute path of an archive copy if it is safe to delete, else None."""
    parts = rel_path.split("/")
    if not parts or parts[0] not in PRUNABLE_TOP_DIRS or ".." in parts:
        return None
    target = _real(os.path.join(output_root, *parts))
    if not _is_within(target, os.path.join(output_root, parts[0])):
        return None
    if any(_is_within(target, root) for root in wa_roots):
        return None
    return target


def _remove_empty_parents(path: str, output_root: str):
    """Remove now-empty folders up to (not including) Contacts/ or Groups/."""
    stops = {os.path.join(output_root, d) for d in PRUNABLE_TOP_DIRS}
    parent = os.path.dirname(path)
    while _is_within(parent, output_root) and parent not in stops and parent != output_root:
        try:
            os.rmdir(parent)  # only succeeds if empty
        except OSError:
            break
        parent = os.path.dirname(parent)


def prune_missing(
    conn: sqlite3.Connection,
    output_root: str,
    wa_roots: list[str],
    logger: logging.Logger,
    dry_run: bool = False,
    max_ratio: float = DEFAULT_MAX_RATIO,
) -> PruneStats:
    output_root = _real(output_root)
    roots = [_real(r) for r in wa_roots]

    if not roots:
        logger.error("PRUNE skipped: no wa_root given.")
        return PruneStats(0, 0, 0, True)
    for root in roots:
        if _is_within(root, output_root) or _is_within(output_root, root):
            logger.error(f"PRUNE skipped: output and wa_root overlap ({root}).")
            return PruneStats(0, 0, 0, True)
        if not os.path.isdir(os.path.join(root, "Media")):
            logger.error(f"PRUNE skipped: no Media folder in wa_root {root}.")
            return PruneStats(0, 0, 0, True)

    copies: dict[str, list[str]] = {}
    for original_path, archive_path in conn.execute(
        "SELECT original_path, archive_path FROM archive_copies "
        "WHERE original_path NOT LIKE 'vcard:%' ORDER BY original_path, rowid"
    ):
        copies.setdefault(original_path, []).append(archive_path)

    missing = [o for o in copies if not _source_exists(o, roots)]
    total = len(copies)
    if not missing:
        logger.info(f"PRUNE: all {total} archived source file(s) still present.")
        return PruneStats(total, 0, 0, False)

    ratio = len(missing) / total
    if ratio > max_ratio:
        logger.error(
            f"PRUNE aborted: {len(missing)} of {total} source file(s) ({ratio:.1%}) are "
            f"missing, above the {max_ratio:.0%} safety limit. Nothing was deleted. "
            "Check that the wa_root is complete; raise --prune-max-ratio only if the "
            "deletion is intended."
        )
        return PruneStats(total, len(missing), 0, True)

    missing_set = set(missing)
    deleted = 0
    for original_path in missing:
        refused = False
        for rel_path in copies[original_path]:
            target = _safe_archive_target(rel_path, output_root, roots)
            if target is None:
                logger.warning(f"PRUNE refused (outside Contacts/Groups): {rel_path}")
                refused = True
                continue
            # Same archive file recorded for another, still present, source: keep it.
            sharers = {
                o
                for (o,) in conn.execute(
                    "SELECT original_path FROM archive_copies WHERE archive_path = ?",
                    (rel_path,),
                )
            }
            if sharers - missing_set:
                logger.debug(f"PRUNE kept (shared with a present source): {rel_path}")
                continue
            if dry_run:
                logger.info(f"[DRY RUN] Would prune: {rel_path}")
                deleted += 1
                continue
            try:
                if os.path.isfile(target):
                    os.remove(target)
                    deleted += 1
                    _remove_empty_parents(target, output_root)
                logger.info(f"PRUNED: {rel_path}")
            except OSError as e:
                logger.error(f"PRUNE error on {rel_path}: {e}")
                refused = True
        # Keep the DB rows if any copy was left behind, so a later run sees it again.
        if not dry_run and not refused:
            conn.execute("DELETE FROM archive_copies WHERE original_path = ?", (original_path,))
            conn.execute("DELETE FROM files WHERE original_path = ?", (original_path,))

    if not dry_run:
        conn.commit()

    label = "would prune" if dry_run else "pruned"
    logger.info(
        f"PRUNE: {len(missing)} of {total} source file(s) gone from wa_root, "
        f"{label} {deleted} archive cop(ies)."
    )
    return PruneStats(total, len(missing), deleted, False)

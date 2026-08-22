#!/usr/bin/env python3
"""Reset the Governor's accumulated state.

Wipes derived analysis data (evaluations, diagnostics, AI recommendations,
the action ledger, etc.) so everything regenerates cleanly with current
logic. ALWAYS backs up the data/ directory to a timestamped archive first,
and NEVER touches config/.

Usage:
    python scripts/reset_system.py                 # dry run (soft), shows what it would do
    python scripts/reset_system.py --mode soft --yes
    python scripts/reset_system.py --mode full --yes
    python scripts/reset_system.py --mode nuke --yes
    python scripts/reset_system.py --mode soft --yes --no-backup

Modes:
    soft  (default)  Wipe derived analysis only. KEEP imported inputs
                     (Ahrefs keyword queue, sitemap, page inventory,
                     content manager) and API caches. Re-run an evaluation
                     afterward for a clean slate — no re-importing needed.
    full             soft + wipe imported inputs (keyword queue, sitemap,
                     page inventory, content manager). Keep API caches.
    nuke             Wipe everything in data/ except .gitkeep. Config is
                     always preserved (it lives outside data/).
"""

import argparse
import sys
import tarfile
from datetime import datetime
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# Derived analysis state — the accumulated output that gets stale/wrong.
DERIVED = [
    "latest_evaluation.json",
    "diagnostic_results.json",
    "ai_recommendations.json",
    "action_ledger.json",
    "evaluation_history.json",
    "governance_state.json",
    "link_graph.json",
    "ai_visibility.json",
    "notifications.json",
    "job_state.json",
]

# Imported inputs — expensive to rebuild (re-import / re-crawl needed).
INPUTS = [
    "keyword_queue.json",
    "sitemap_types.json",
    "sitemap_images.json",
    "page_inventory.json",
    "content_manager.json",
]

# API response caches — clearing these just wastes quota on the next run.
CACHES = [
    "serp_cache.json",
    "crux_cache.json",
    "moz_cache.json",
    "ads_cache.json",
]


def targets_for(mode: str) -> list[str]:
    if mode == "soft":
        return list(DERIVED)
    if mode == "full":
        return DERIVED + INPUTS
    if mode == "nuke":
        # Everything present in data/ except the .gitkeep marker
        return sorted(
            p.name for p in DATA_DIR.iterdir()
            if p.is_file() and p.name != ".gitkeep"
        )
    raise ValueError(f"unknown mode: {mode}")


def make_backup() -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = DATA_DIR.parent / f"data-backup-{stamp}.tar.gz"
    with tarfile.open(backup_path, "w:gz") as tar:
        tar.add(DATA_DIR, arcname="data")
    return backup_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Reset Governor state (safe, backs up first).")
    parser.add_argument("--mode", choices=["soft", "full", "nuke"], default="soft")
    parser.add_argument("--yes", action="store_true", help="Actually delete (otherwise dry run).")
    parser.add_argument("--no-backup", action="store_true", help="Skip the safety backup.")
    args = parser.parse_args()

    if not DATA_DIR.exists():
        print(f"No data directory at {DATA_DIR} — nothing to reset.")
        return 0

    wanted = targets_for(args.mode)
    present = [name for name in wanted if (DATA_DIR / name).exists()]
    missing = [name for name in wanted if not (DATA_DIR / name).exists()]

    print(f"Reset mode: {args.mode.upper()}")
    print(f"Data dir:   {DATA_DIR}")
    print()
    print(f"Would delete ({len(present)} present):")
    for name in present:
        size = (DATA_DIR / name).stat().st_size
        print(f"  - {name}  ({size:,} bytes)")
    if missing:
        print(f"\nAlready absent ({len(missing)}): {', '.join(missing)}")

    kept = []
    if args.mode == "soft":
        kept = INPUTS + CACHES
    elif args.mode == "full":
        kept = CACHES
    if kept:
        kept_present = [k for k in kept if (DATA_DIR / k).exists()]
        if kept_present:
            print(f"\nPreserved: {', '.join(kept_present)}")
    print("\nAlways preserved: config/ (settings)")

    if not args.yes:
        print("\n[DRY RUN] Nothing deleted. Re-run with --yes to execute.")
        return 0

    if not present:
        print("\nNothing to delete.")
        return 0

    if not args.no_backup:
        backup = make_backup()
        print(f"\nBackup written: {backup}")

    deleted = 0
    for name in present:
        try:
            (DATA_DIR / name).unlink()
            deleted += 1
        except OSError as exc:
            print(f"  ! failed to delete {name}: {exc}")
    print(f"\nDeleted {deleted} file(s). Reset complete.")
    print("Next: restart the service and run a fresh evaluation "
          "(Admin -> Run Evaluation).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

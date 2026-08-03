"""
Super Clean
-----------
Scrubs business-list CSVs produced by business_finder.py BEFORE you use them:

- Removes bad/junk emails (sentry hashes, image files, placeholder addresses,
  "www."-domain garbage, vendor addresses, version strings, ...) from every
  company row.
- Regenerates the "Contact Email" column from the emails that survive.
- If a company has NO valid emails left — including single-email companies
  whose only email is junk — the row is kept ONLY when it still has a
  WhatsApp link (still a usable lead); otherwise it is removed from the list.

Validation reuses gmail_email_sender.is_valid_target_email, so the list is
cleaned with exactly the same rules the sender applies before sending.

USAGE:
    # Clean one file, write a cleaned copy next to it
    python super_clean.py Lists/it_companies_Berlin__Germany.csv

    # Clean every CSV in a directory
    python super_clean.py --csv-dir Lists/

    # Overwrite the originals instead of writing copies
    python super_clean.py --in-place Lists/it_companies_Berlin__Germany.csv

    # Dry-run: only show what would be removed, don't write anything
    python super_clean.py --dry-run Lists/

    # Stricter validation (also rejects single-letter locals, short domains,
    # hash-like locals, CSS-class-style locals such as "20background")
    python super_clean.py --strict Lists/
"""

# ─── Imports ─────────────────────────────────────────────────────────────────────
import os
import re
import csv
import sys
import glob
import argparse
from pathlib import Path

# Force UTF-8 output for Windows console (fixes UnicodeEncodeError)
if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Allow importing the validator from the same folder
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gmail_email_sender import is_valid_target_email, get_contact_email

# ─── Terminal-safe symbols (Windows cp1252 compat) ──────────────────────────────
_CHECK = "[OK]"
_CROSS = "[X]"
_WARN = "[!]"
_LINE = "-" * 60
_INFO = "[i]"

# Columns that can hold email data. The sender uses these same names.
EMAIL_COLUMNS = ("All Emails Found", "Contact Email", "emails")


def collect_row_emails(row: dict) -> list:
    """Gather every email mentioned anywhere in the row, deduped, order kept."""
    emails = []
    for col in EMAIL_COLUMNS:
        raw = (row.get(col) or "").strip()
        if not raw:
            continue
        # "All Emails Found" is ";"-separated; tolerate commas too
        for part in re.split(r"[;,]", raw):
            part = part.strip()
            if part:
                emails.append(part)

    seen = set()
    unique = []
    for e in emails:
        key = e.lower()
        if key not in seen:
            seen.add(key)
            unique.append(e)
    return unique


def clean_row(row: dict, strict: bool = False, drop_whatsapp_only: bool = False) -> tuple[dict, list, bool]:
    """
    Strip bad emails from one company row.

    Returns (cleaned_row, removed_emails, dropped).
    - removed_emails: list of emails that were deleted from the row.
    - dropped: True when the company has no valid emails left (and no
      WhatsApp link, unless drop_whatsapp_only is set) and the whole
      row should be removed from the list.
    """
    all_emails = collect_row_emails(row)

    kept = []
    removed = []
    for e in all_emails:
        if is_valid_target_email(e, strict=strict):
            kept.append(e)
        else:
            removed.append(e)

    # Nothing valid left. Keep the row if it still has a WhatsApp link (the
    # finder now fills quotas with WhatsApp-only rows) — those are still
    # usable leads. Only drop companies with no email AND no WhatsApp,
    # unless --drop-whatsapp-only was requested.
    if not kept:
        has_wa = bool((row.get("WhatsApp Link") or "").strip())
        if has_wa and not drop_whatsapp_only:
            return row, removed, False
        return row, removed, True

    # Rebuild the email columns from the survivors
    if "All Emails Found" in row:
        row["All Emails Found"] = "; ".join(kept)
    if "Contact Email" in row:
        # Pick the best surviving email (domain match + named prefix win)
        row["Contact Email"] = get_contact_email(row) or kept[0]
    if "emails" in row:
        row["emails"] = "; ".join(kept)

    return row, removed, False


def load_rows(filepath: str) -> tuple[list[dict], list]:
    """Load a CSV as a list of dicts (headers + values trimmed)."""
    with open(filepath, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = [h.strip() for h in (reader.fieldnames or [])]
        rows = []
        for raw in reader:
            rows.append({
                k.strip(): (v.strip() if v else "")
                for k, v in raw.items()
            })
    return rows, fieldnames


def has_email_columns(fieldnames: list) -> bool:
    return any(col in fieldnames for col in EMAIL_COLUMNS)


def find_csv_files(directory: str) -> list[str]:
    """All CSVs in a directory, sorted, excluding sent logs."""
    files = glob.glob(os.path.join(directory, "*.csv"))
    files = [f for f in files if "sent" not in os.path.basename(f).lower()]
    return sorted(files)


def process_file(
    filepath: str,
    strict: bool = False,
    in_place: bool = False,
    dry_run: bool = False,
    quiet: bool = False,
    drop_whatsapp_only: bool = False,
) -> tuple[int, int, int]:
    """
    Clean a single CSV. Returns (kept, dropped, removed_email_count).
    """
    rows, fieldnames = load_rows(filepath)
    if not rows:
        if not quiet:
            print(f"  {_WARN} '{os.path.basename(filepath)}' has no data rows.")
        return 0, 0, 0

    if not has_email_columns(fieldnames):
        if not quiet:
            print(f"  {_WARN} '{os.path.basename(filepath)}' has no email columns — skipped.")
        return 0, 0, 0

    kept_rows = []
    dropped = 0
    removed_count = 0
    removed_details = []  # (company, email) pairs for reporting

    for row in rows:
        company = row.get("Company Name") or row.get("name") or "Unknown"
        cleaned, removed, is_dropped = clean_row(
            row, strict=strict, drop_whatsapp_only=drop_whatsapp_only,
        )
        if is_dropped:
            dropped += 1
            for e in removed:
                removed_details.append((company, e))
            continue
        kept_rows.append(cleaned)
        removed_count += len(removed)
        for e in removed:
            removed_details.append((company, e))

    # ── Report ──
    if not quiet:
        print(f"  {os.path.basename(filepath)}:")
        print(f"    kept    : {len(kept_rows)} companies")
        print(f"    dropped : {dropped} companies (no valid emails left)")
        print(f"    removed : {removed_count} bad emails")
        if removed_details:
            for company, email in removed_details[:10]:
                print(f"      {_CROSS} {company} -> {email}")
            if len(removed_details) > 10:
                print(f"      ... and {len(removed_details) - 10} more")

    # ── Write / preview ──
    if dry_run:
        if not quiet:
            print(f"    {_INFO} dry-run — no file written.")
        return len(kept_rows), dropped, removed_count

    out_path = filepath if in_place else str(
        Path(filepath).with_name(f"super_clean_{Path(filepath).name}")
    )
    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(kept_rows)

    if not quiet:
        action = "overwrote" if in_place else "wrote"
        print(f"    {_CHECK} {action} {os.path.basename(out_path)}")
    return len(kept_rows), dropped, removed_count


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Remove bad emails from business lists and drop companies "
            "left with no valid email."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "EXAMPLES:\n"
            "  # Clean one file -> writes super_clean_<name>.csv next to it\n"
            "  python super_clean.py Lists/it_companies_Berlin__Germany.csv\n\n"
            "  # Clean every CSV in a directory\n"
            "  python super_clean.py --csv-dir Lists/\n\n"
            "  # Overwrite the original files\n"
            "  python super_clean.py --in-place Lists/\n\n"
            "  # Preview only (nothing written)\n"
            "  python super_clean.py --dry-run Lists/\n"
        ),
    )
    parser.add_argument("csv_file", nargs="?", help="Path to a single CSV list")
    parser.add_argument(
        "--csv-dir",
        help="Directory containing CSV lists to batch-clean (alternative to csv_file)",
        default="",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Stricter email validation (single-letter locals, short domains, "
             "hash-like / CSS-class locals)",
    )
    parser.add_argument(
        "--drop-whatsapp-only",
        action="store_true",
        help="Drop rows that have a WhatsApp link but no valid email "
             "(default: keep them as usable WhatsApp leads)",
    )
    parser.add_argument(
        "--in-place",
        action="store_true",
        help="Overwrite the original CSV(s) instead of writing a cleaned copy",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would be removed without writing any file",
    )
    parser.add_argument(
        "--quiet", "-q",
        action="store_true",
        help="Only print per-file summaries, not removed-email details",
    )
    args = parser.parse_args()

    # Determine the files to process
    files = []
    if args.csv_dir:
        if not os.path.isdir(args.csv_dir):
            print(f"{_CROSS} Directory not found: {args.csv_dir}")
            sys.exit(1)
        files = find_csv_files(args.csv_dir)
        if not files:
            print(f"{_CROSS} No CSV files found in '{args.csv_dir}'")
            sys.exit(1)
        print(f"{_INFO} Found {len(files)} CSV file(s) in '{args.csv_dir}'")
    else:
        if not args.csv_file:
            parser.error("You must specify a CSV file or --csv-dir")
        if not os.path.isfile(args.csv_file):
            print(f"{_CROSS} File not found: {args.csv_file}")
            sys.exit(1)
        files = [args.csv_file]

    print()
    print(_LINE)
    print("  SUPER CLEAN — email scrubber")
    print(f"  Files: {len(files)} CSV(s)")
    if args.strict:
        print("  Strict validation: ON")
    print(f"  Mode: {'DRY-RUN (nothing written)' if args.dry_run else ('IN-PLACE (overwrites originals)' if args.in_place else 'COPIES')}")
    print(_LINE)
    print()

    total_kept = 0
    total_dropped = 0
    total_removed = 0

    for path in files:
        kept, dropped, removed = process_file(
            path,
            strict=args.strict,
            in_place=args.in_place,
            dry_run=args.dry_run,
            quiet=args.quiet,
            drop_whatsapp_only=args.drop_whatsapp_only,
        )
        total_kept += kept
        total_dropped += dropped
        total_removed += removed
        print()

    print(_LINE)
    print("  FINAL SUMMARY")
    print(_LINE)
    print(f"  Companies kept  : {total_kept}")
    print(f"  Companies dropped: {total_dropped} (no valid emails)")
    print(f"  Bad emails removed: {total_removed}")
    if args.dry_run:
        print(f"  Nothing was written (dry-run).")
    print()


if __name__ == "__main__":
    main()

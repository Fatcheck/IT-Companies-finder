"""
Verify Emails
-------------
Checks every email in a business-list CSV for real deliverability BEFORE
you send, so you stop getting "Address not found" bounces:

  1. Format / junk check (reuses gmail_email_sender.is_valid_target_email)
  2. MX record check  — does the domain have a mail server at all?
  3. SMTP RCPT check  — does the mail server accept THIS mailbox?

Outputs:
  - verified_<name>.csv   : only rows whose email is deliverable
                            (default: valid + unknown are kept; --strict drops unknown)
  - <name>_report.csv     : every email with its status and the reason

Zero third-party dependencies (urllib + smtplib from the standard library),
so it runs on GitHub Actions without a pip install step.

USAGE:
    # Check a CSV, write verified_<name>.csv next to it
    python verify_emails.py businesses.csv

    # Custom output + drop "unknown" results too (fewest bounces, may lose some leads)
    python verify_emails.py businesses.csv --out verified.csv --strict

    # Faster / slower (default 20 workers)
    python verify_emails.py businesses.csv --workers 40

    # Skip the SMTP probe, only check MX records (much faster, less accurate)
    python verify_emails.py businesses.csv --mx-only
"""

# ─── Imports ─────────────────────────────────────────────────────────────────────
import os
import csv
import sys
import json
import time
import argparse
import smtplib
import socket
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from pathlib import Path

# Force UTF-8 output for Windows console
if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Allow importing helpers from the same folder
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gmail_email_sender import load_csv, get_contact_email, is_valid_target_email  # noqa: E402

# ─── Terminal-safe symbols ───────────────────────────────────────────────────────
_CHECK = "[OK]"
_CROSS = "[X]"
_WARN = "[!]"
_LINE = "-" * 60
_INFO = "[i]"

# Big providers block SMTP mailbox probing (they return 550 even for real
# addresses to stop enumeration). For these, MX presence is enough.
NO_SMTP_PROBE_DOMAINS = {
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com",
    "msn.com", "yahoo.com", "ymail.com", "aol.com", "icloud.com", "me.com",
    "protonmail.com", "proton.me", "zoho.com", "mail.com", "gmx.com",
    "hey.com", "fastmail.com", "outlook.fr", "hotmail.fr",
}

# DNS-over-HTTPS resolvers (try in order; any network is fine, incl. GitHub runners)
RESOLVERS = [
    "https://dns.google/resolve",
    "https://cloudflare-dns.com/dns-query",
]


def dns_query(domain: str, rtype: int) -> list | None:
    """
    DNS-over-HTTPS lookup for a record type (1=A, 15=MX, 28=AAAA).

    Returns the list of answer dicts, or [] for a definitive "no records"
    (NOERROR-empty or NXDOMAIN), or None when every resolver failed / errored.
    """
    for resolver in RESOLVERS:
        try:
            url = f"{resolver}?name={domain}&type={rtype}"
            req = urllib.request.Request(url, headers={"Accept": "application/dns-json"})
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read().decode("utf-8"))
            status = data.get("Status")
            if status == 0:
                return data.get("Answer") or []
            if status == 3:  # NXDOMAIN — domain does not exist
                return []
            # SERVFAIL / REFUSED / other — not definitive, treat as failed
            return None
        except Exception:
            continue
    return None


@lru_cache(maxsize=8192)
def lookup_domain(domain: str) -> tuple[bool | None, str | None, bool | None]:
    """
    One combined lookup: (has_mx, mx_host, has_addr). Cached per domain so a
    400+ email list with many same-domain addresses only queries DNS once per
    domain instead of once per email.

    - has_mx: True when MX records exist, False when the domain has none,
      None when the lookup failed.
    - mx_host: the lowest-preference MX hostname (or None).
    - has_addr: True/False/None whether the domain has an A/AAAA record — used
      for the RFC 5321 fallback where mail goes to the address record when no
      MX exists. None means the check itself failed (not "no records").
    """
    mx_answers = dns_query(domain, 15)
    if mx_answers is None:
        return None, None, None

    mxs = []
    for a in mx_answers:
        if a.get("type") == 15:  # only count real MX answers
            parts = a.get("data", "").split()
            # RFC 7505: a null MX ("0 .") means the domain refuses mail
            if len(parts) >= 2 and parts[1].strip(".") != "":
                mxs.append((int(parts[0]), parts[1].rstrip(".")))
    if mxs:
        mxs.sort()
        return True, mxs[0][1], None

    # No MX records: per RFC 5321 mail may still be delivered via A/AAAA.
    a_answers = dns_query(domain, 1)
    if a_answers is None:
        return False, None, None
    has_addr = any(a.get("type") == 1 for a in a_answers)
    if not has_addr:
        aaaa_answers = dns_query(domain, 28)
        if aaaa_answers is not None:
            has_addr = any(a.get("type") == 28 for a in aaaa_answers)
    return False, None, has_addr


def smtp_mailbox_check(email: str, mx_host: str, timeout: int = 8) -> bool | None:
    """
    Ask the domain's mail server whether this mailbox exists.
    True = accepted, False = rejected (550 etc.), None = couldn't tell.
    """
    smtp = None
    try:
        smtp = smtplib.SMTP(timeout=timeout)
        smtp.connect(mx_host, 25)
        try:
            smtp.ehlo("localhost")
        except smtplib.SMTPHeloError:
            try:
                smtp.helo("localhost")
            except Exception:
                return None
        # Null sender (bounce-style) — some servers reject; then we can't tell.
        try:
            code, _ = smtp.mail("<>")
        except smtplib.SMTPException:
            return None
        if code and code >= 400:
            return None
        code, _msg = smtp.rcpt(email)
        if code in (250, 251, 252):
            return True
        if code and 500 <= code < 600:
            return False
        return None
    except (socket.timeout, OSError, smtplib.SMTPException):
        return None
    finally:
        if smtp is not None:
            try:
                smtp.quit()
            except Exception:
                pass


# ─── Verification ────────────────────────────────────────────────────────────────

def verify_one(email: str, mx_only: bool, timeout: int) -> tuple[str, str]:
    """Return (status, reason). Status in {valid, invalid, unknown}."""
    if not is_valid_target_email(email, strict=True):
        return "invalid", "fails format / junk checks"

    domain = email.split("@", 1)[1].lower()

    has_mx, mx_host, has_addr = lookup_domain(domain)
    if has_mx is None:
        return "unknown", "DNS lookup failed (network / resolver)"
    if not has_mx:
        if has_addr:
            # RFC 5321: no MX but the domain resolves — mail may go to the A
            # record, so don't claim it's undeliverable.
            return "unknown", "no MX record but domain resolves (A-record fallback)"
        if has_addr is None:
            return "unknown", "no MX record; could not confirm A/AAAA record"
        return "invalid", "domain cannot receive mail (no MX, no A/AAAA record)"

    # Provider domains: MX is enough (they block probing)
    if domain in NO_SMTP_PROBE_DOMAINS:
        return "valid", "known provider with MX records"

    if mx_only:
        return "valid", "domain has MX records (SMTP probe skipped)"

    result = smtp_mailbox_check(email, mx_host, timeout=timeout)
    if result is True:
        return "valid", f"mailbox accepted by {mx_host}"
    if result is False:
        return "invalid", f"mailbox rejected by {mx_host} (550)"
    return "unknown", f"mail server {mx_host} did not confirm (blocked / timeout)"


# ─── Main ────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Check email deliverability (MX + SMTP) before sending.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "EXAMPLES:\\n"
            "  python verify_emails.py businesses.csv\\n"
            "  python verify_emails.py businesses.csv --strict --workers 40\\n"
        ),
    )
    parser.add_argument("csv_file", help="Path to the CSV with contacts")
    parser.add_argument(
        "--out", default="",
        help="Output CSV for verified rows (default: verified_<name>.csv)",
    )
    parser.add_argument(
        "--workers", type=int, default=20,
        help="Parallel verification workers (default: 20)",
    )
    parser.add_argument(
        "--timeout", type=int, default=8,
        help="SMTP connection timeout in seconds (default: 8)",
    )
    parser.add_argument(
        "--mx-only", action="store_true",
        help="Only check MX records, skip the SMTP mailbox probe (faster, less accurate)",
    )
    parser.add_argument(
        "--strict", action="store_true",
        help="Drop 'unknown' results too (fewest bounces, may lose some leads)",
    )
    parser.add_argument(
        "--quiet", "-q", action="store_true",
        help="Suppress per-row progress output",
    )
    args = parser.parse_args()

    if not os.path.isfile(args.csv_file):
        print(f"{_CROSS} File not found: {args.csv_file}")
        sys.exit(1)

    src = Path(args.csv_file)
    out_path = args.out or str(src.with_name(f"verified_{src.name}"))
    report_path = str(src.with_name(f"{src.stem}_report.csv"))

    rows = load_csv(str(src))
    if not rows:
        print(f"{_CROSS} No data rows in {args.csv_file}")
        sys.exit(1)

    # Collect (row_index, email) pairs; skip rows without a usable email.
    targets = []
    for i, row in enumerate(rows):
        email = get_contact_email(row)
        if email:
            targets.append((i, email))
    print(f"{_INFO} Loaded {len(rows)} row(s), {len(targets)} with an email to verify.\n")

    if not targets:
        print(f"{_CROSS} No verifiable emails found in CSV.")
        sys.exit(1)

    print(_LINE)
    print("  EMAIL VERIFIER — deliverability check")
    print(f"  Emails : {len(targets)}")
    print(f"  Workers: {args.workers}")
    print(f"  MX only: {'YES' if args.mx_only else 'no (SMTP probe on)'}")
    print(f"  Strict : {'YES (drop unknown)' if args.strict else 'no (keep unknown)'}")
    print(_LINE)
    print()

    # Deduplicate emails for verification, keep mapping back to rows.
    unique_emails = []
    seen = set()
    for _i, email in targets:
        key = email.lower()
        if key not in seen:
            seen.add(key)
            unique_emails.append(email)

    start = time.time()
    done = 0
    results = {}

    def _work(email: str):
        return email, verify_one(email, args.mx_only, args.timeout)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(_work, e): e for e in unique_emails}
        for fut in as_completed(futures):
            email, result = fut.result()
            results[email.lower()] = result
            done += 1
            if not args.quiet and (done % 25 == 0 or done == len(unique_emails)):
                print(f"  {_INFO} verified {done}/{len(unique_emails)} "
                      f"({time.time() - start:.0f}s)", end="\r")

    elapsed = time.time() - start

    # Map results back to rows
    kept_rows = []
    report_rows = []
    counts = {"valid": 0, "invalid": 0, "unknown": 0}

    for idx, email in targets:
        status, reason = results.get(email.lower(), ("unknown", "not verified"))
        counts[status] = counts.get(status, 0) + 1
        company = rows[idx].get("Company Name") or rows[idx].get("name") or ""
        report_rows.append({
            "Email": email,
            "Company": company,
            "Status": status,
            "Reason": reason,
        })
        keep = status == "valid" or (status == "unknown" and not args.strict)
        if keep:
            kept_rows.append(rows[idx])

    # Write verified CSV (same headers)
    fieldnames = list(rows[0].keys())
    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(kept_rows)

    # Write report CSV
    rep_fieldnames = ["Email", "Company", "Status", "Reason"]
    with open(report_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=rep_fieldnames)
        writer.writeheader()
        writer.writerows(report_rows)

    print(f"\r{' ' * 60}\r", end="")
    print()
    print(_LINE)
    print(f"  {_CHECK} Verification complete in {elapsed:.0f}s")
    print(f"  Valid   : {counts['valid']}")
    print(f"  Invalid : {counts['invalid']}  (removed — would have bounced)")
    print(f"  Unknown : {counts['unknown']}  ({'removed' if args.strict else 'kept'} — server would not confirm)")
    print(f"  Kept    : {len(kept_rows)} / {len(rows)} rows")
    print(f"  Verified CSV : {out_path}")
    print(f"  Report CSV   : {report_path}")
    print(_LINE)
    print()
    if counts["invalid"]:
        print(f"  {_WARN} {counts['invalid']} address(es) were dropped because they")
        print(f"      cannot receive mail. See the report CSV for details.\n")


if __name__ == "__main__":
    main()

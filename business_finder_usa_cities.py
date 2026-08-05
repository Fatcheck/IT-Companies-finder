"""
Business Finder — USA Cities (multi-city)
-----------------------------------------
Runs the single-city business finder (business_finder.py) over a WHOLE LIST
of USA cities in one go. This script is deliberately separate from the
single-city script (business_finder.py) and has its own GitHub Actions
workflow (.github/workflows/scrape_usa_cities.yml).

Key behaviour:
- `--limit 500` means 500 companies PER CITY, not 500 total — every city in
  the list gets its own CSV with up to `limit` companies that have emails.
- One CSV per city, named businesses_{niche}_{city}__{state}.csv, exactly like
  the single-city script so super_clean.py and the email sender can be reused
  unchanged on the results.

USAGE:
    python business_finder_usa_cities.py --limit 500 "IT"
    python business_finder_usa_cities.py --limit 300 --niche "software"
    python business_finder_usa_cities.py --limit 100 --cities "Austin, Texas; Seattle, Washington" "IT"
    python business_finder_usa_cities.py --limit 50 --start-city 5 --max-cities 10 "IT"

OPTIONS:
    --limit N          Companies to find PER CITY (default: 500)
    --niche X          Business niche (default: "IT"). Also accepted as the
                       first positional argument (keeps the single-city habit).
    --cities LIST      Semicolon-separated override list of cities, e.g.
                       "Austin, Texas; Dallas, Texas" (default: USA_CITIES below)
    --start-city N     Skip the first N cities in the list (resume support)
    --max-cities N     Only process the first N cities (0 = all)
    --workers N        Parallel website workers (pass-through to business_finder)
    --pages N          Pages checked per site (pass-through)
    --pool N           Candidate pool multiplier (pass-through)
    -h, --help         Show this help

EXAMPLES OF THE CITY LIST:
    Edit USA_CITIES below to add/remove cities. Each entry is "City, State"
    so Nominatim geocodes unambiguously ("New York, New York" != "New York
    State"). A ~60-city default list of major US metros is pre-filled.
"""

# ─── Imports ─────────────────────────────────────────────────────────────────────
import os
import re
import sys
import time

# Allow importing the shared finder from the same folder.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import business_finder  # noqa: E402

# ─── Terminal-safe symbols (Windows cp1252 compat) ──────────────────────────────
_CHECK = "[OK]"
_CROSS = "[X]"
_WARN = "[!]"
_ARROW = "->"
_LINE = "-" * 60
_INFO = "[i]"

# ─── USA CITIES ──────────────────────────────────────────────────────────────────
# "City, State" pairs. 60 major US metros — every city gets its own CSV.
USA_CITIES = [
    "New York, New York",
    "Los Angeles, California",
    "Chicago, Illinois",
    "Houston, Texas",
    "Phoenix, Arizona",
    "Philadelphia, Pennsylvania",
    "San Antonio, Texas",
    "San Diego, California",
    "Dallas, Texas",
    "San Jose, California",
    "Austin, Texas",
    "Jacksonville, Florida",
    "Fort Worth, Texas",
    "Columbus, Ohio",
    "Charlotte, North Carolina",
    "San Francisco, California",
    "Indianapolis, Indiana",
    "Seattle, Washington",
    "Denver, Colorado",
    "Washington, District of Columbia",
    "Boston, Massachusetts",
    "Nashville, Tennessee",
    "Detroit, Michigan",
    "Portland, Oregon",
    "Las Vegas, Nevada",
    "Memphis, Tennessee",
    "Louisville, Kentucky",
    "Baltimore, Maryland",
    "Milwaukee, Wisconsin",
    "Albuquerque, New Mexico",
    "Tucson, Arizona",
    "Fresno, California",
    "Sacramento, California",
    "Kansas City, Missouri",
    "Mesa, Arizona",
    "Atlanta, Georgia",
    "Omaha, Nebraska",
    "Colorado Springs, Colorado",
    "Raleigh, North Carolina",
    "Miami, Florida",
    "Long Beach, California",
    "Virginia Beach, Virginia",
    "Oakland, California",
    "Minneapolis, Minnesota",
    "Tulsa, Oklahoma",
    "Tampa, Florida",
    "Arlington, Texas",
    "New Orleans, Louisiana",
    "Wichita, Kansas",
    "Cleveland, Ohio",
    "Bakersfield, California",
    "Aurora, Colorado",
    "Anaheim, California",
    "Honolulu, Hawaii",
    "Santa Ana, California",
    "Riverside, California",
    "Corpus Christi, Texas",
    "Lexington, Kentucky",
    "Stockton, California",
    "St. Louis, Missouri",
]


def parse_city_list(text: str) -> list:
    """Parse a ';' separated city list like 'Austin, Texas; Dallas, Texas'."""
    cities = []
    for chunk in text.split(";"):
        chunk = chunk.strip().strip("\"'")
        if chunk:
            cities.append(chunk)
    return cities


def print_help() -> None:
    print("Business Finder — USA Cities (multi-city)")
    print()
    print("Runs the business finder for a WHOLE LIST of USA cities in one go.")
    print("--limit 500 means 500 companies PER CITY (one CSV per city).")
    print()
    print("Usage:")
    print("  python business_finder_usa_cities.py --limit 500 \"IT\"")
    print("  python business_finder_usa_cities.py --limit 300 --niche \"software\"")
    print("  python business_finder_usa_cities.py --limit 100 --cities \"Austin, Texas; Seattle, Washington\" \"IT\"")
    print()
    print("Options:")
    print("  --limit N       Companies to find PER CITY (default: 500)")
    print("  --niche X       Business niche (default: \"IT\")")
    print("  --cities LIST   ';' separated override list of cities")
    print("  --start-city N  Skip the first N cities (resume support)")
    print("  --max-cities N  Only process the first N cities (0 = all)")
    print("  --workers N     Parallel website workers (default: 30)")
    print("  --pages N       Pages checked per site (default: 15)")
    print("  --pool N        Candidate pool multiplier (default: 4)")
    print("  -h, --help      Show this help")


def main() -> None:
    args = sys.argv[1:]
    if not args or "-h" in args or "--help" in args:
        print_help()
        sys.exit(0)

    limit = 500
    niche = None
    cities_override = None
    start_city = 0
    max_cities = 0
    workers = None
    pages = None
    pool = None

    i = 0
    positionals = []
    while i < len(args):
        arg = args[i]
        if arg == "--limit":
            if i + 1 >= len(args):
                print(f"{_CROSS} --limit requires a number argument")
                sys.exit(1)
            try:
                limit = int(args[i + 1])
            except ValueError:
                print(f"{_CROSS} --limit requires a number, got '{args[i + 1]}'")
                sys.exit(1)
            if limit < 1:
                print(f"{_CROSS} --limit must be a positive number")
                sys.exit(1)
            i += 2
        elif arg == "--niche":
            if i + 1 >= len(args):
                print(f"{_CROSS} --niche requires a value")
                sys.exit(1)
            niche = args[i + 1]
            i += 2
        elif arg == "--cities":
            if i + 1 >= len(args):
                print(f"{_CROSS} --cities requires a value")
                sys.exit(1)
            cities_override = args[i + 1]
            i += 2
        elif arg == "--start-city":
            if i + 1 >= len(args):
                print(f"{_CROSS} --start-city requires a number")
                sys.exit(1)
            try:
                start_city = int(args[i + 1])
            except ValueError:
                print(f"{_CROSS} --start-city requires a number, got '{args[i + 1]}'")
                sys.exit(1)
            i += 2
        elif arg == "--max-cities":
            if i + 1 >= len(args):
                print(f"{_CROSS} --max-cities requires a number")
                sys.exit(1)
            try:
                max_cities = int(args[i + 1])
            except ValueError:
                print(f"{_CROSS} --max-cities requires a number, got '{args[i + 1]}'")
                sys.exit(1)
            i += 2
        elif arg == "--workers":
            if i + 1 >= len(args):
                print(f"{_CROSS} --workers requires a number")
                sys.exit(1)
            workers = args[i + 1]
            i += 2
        elif arg == "--pages":
            if i + 1 >= len(args):
                print(f"{_CROSS} --pages requires a number")
                sys.exit(1)
            pages = args[i + 1]
            i += 2
        elif arg == "--pool":
            if i + 1 >= len(args):
                print(f"{_CROSS} --pool requires a number")
                sys.exit(1)
            pool = args[i + 1]
            i += 2
        elif arg.startswith("-"):
            print(f"{_CROSS} Unknown option: {arg}")
            print_help()
            sys.exit(1)
        else:
            positionals.append(arg)
            i += 1

    # Niche: --niche flag wins, else the first positional (keeps the
    # single-city habit: `python business_finder_usa_cities.py --limit 500 IT`).
    if niche is None and positionals:
        niche = positionals[0]
    if not niche:
        niche = "IT"
    if len(positionals) > 1:
        print(f"{_WARN} Extra positional arguments ignored: {positionals[1:]}")

    # City list: --cities override wins, else the built-in USA_CITIES.
    if cities_override:
        cities = parse_city_list(cities_override)
        if not cities:
            print(f"{_CROSS} --cities gave no usable cities: '{cities_override}'")
            sys.exit(1)
    else:
        cities = list(USA_CITIES)

    # Apply --start-city / --max-cities slicing.
    if start_city > 0:
        print(f"{_INFO} Skipping the first {start_city} cities (--start-city).")
        cities = cities[start_city:]
    if max_cities > 0:
        print(f"{_INFO} Processing at most {max_cities} cities (--max-cities).")
        cities = cities[:max_cities]

    if not cities:
        print(f"{_CROSS} No cities left to process.")
        sys.exit(1)

    # Pass-through tuning knobs — set BEFORE the finder reads its env vars.
    if workers:
        os.environ["MAX_WORKERS"] = workers
        business_finder.MAX_WORKERS = int(workers)
    if pages:
        os.environ["MAX_PAGES_PER_SITE"] = pages
        business_finder.MAX_PAGES_PER_SITE = int(pages)
    if pool:
        os.environ["POOL_MULTIPLIER"] = pool
        business_finder.POOL_MULTIPLIER = int(pool)

    print(_LINE)
    print(f"  Business Finder — USA Cities (multi-city)")
    print(f"  Niche:    {niche}")
    print(f"  Limit:    {limit} companies PER CITY")
    print(f"  Cities:   {len(cities)}")
    print(f"{_LINE}\n")

    csv_files = []
    total_start = time.time()

    for idx, city in enumerate(cities, 1):
        city_start = time.time()
        print()
        print(_LINE)
        print(f"  City {idx}/{len(cities)}: {city}")
        print(_LINE)

        # The single-city pipeline for THIS city — writes one CSV per city.
        try:
            csv_file = business_finder.run_location(niche, city, limit)
        except Exception as e:  # noqa: BLE001 — keep going to the next city
            print(f"\n{_CROSS} City '{city}' failed: {e}")
            continue

        if csv_file and os.path.exists(csv_file):
            csv_files.append(csv_file)
            elapsed = time.time() - city_start
            print(f"\n{_CHECK} Finished {city} in {elapsed:.0f}s -> {csv_file}")

        # Be polite between cities (Nominatim/Overpass rate limits).
        time.sleep(1.0)

    # ─── Summary ─────────────────────────────────────────────────────────────
    print()
    print(_LINE)
    print(f"  {_CHECK} DONE - Processed {len(cities)} cities")
    print(_LINE)
    print(f"  Total run time: {time.time() - total_start:.0f}s")
    print(f"  CSV files written: {len(csv_files)}")
    for cf in csv_files:
        print(f"    -> {cf}")

    if not csv_files:
        print(f"\n{_CROSS} No CSV files were produced — check the errors above.")
        sys.exit(1)

    print(f"\n  To import into Google Sheets:")
    print(f"    1. Open sheets.google.com and create a new spreadsheet")
    print(f"    2. File > Import > Upload > select each CSV file")
    print(f"  Each city has its own CSV — use them directly with the Send Emails workflow.")


if __name__ == "__main__":
    main()

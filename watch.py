#!/usr/bin/env python3
"""Watch a Cinemark showing for seat openings and newly added dates.

Everything Cinemark serves is plain server-rendered HTML, so a sweep is just:
fetch the theater page (all sellable dates), fetch each date's page (showtime
ids for the configured movie), fetch each showtime's seat map, and diff seat
availability against the previous sweep. What to watch and which seats qualify
comes from config.toml.

State persists in state.json. Alerts append to alerts.log and are forwarded to
an executable ./notify-hook (if present) as: notify-hook TITLE MESSAGE.

Usage:
  python3 watch.py --once             # single sweep (what the CI cron runs)
  python3 watch.py                    # loop forever
  python3 watch.py --report           # print availability from state; no network
  python3 watch.py --dates 2026-08-08 # restrict a sweep (debugging)
"""
from __future__ import annotations

import argparse
import gzip
import json
import logging
import os
import random
import re
import subprocess
import time
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).parent
STATE_FILE = HERE / "state.json"
ALERT_LOG = HERE / "alerts.log"
LOG_FILE = HERE / "watch.log"

_log = logging.getLogger("cinemark")
_log.setLevel(logging.INFO)
_fmt = logging.Formatter("[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
_sh = logging.StreamHandler()
_sh.setFormatter(_fmt)
_log.addHandler(_sh)
_fh = logging.FileHandler(LOG_FILE, mode="a", delay=True)
_fh.setFormatter(_fmt)
_log.addHandler(_fh)

_cfg = tomllib.loads((HERE / "config.toml").read_text())
TARGET, FILTERS, PACING = _cfg["target"], _cfg["filters"], _cfg.get("pacing", {})

THEATER = TARGET["theater"]
MOVIE_ID = str(TARGET["movie_id"])
MOVIE_NAME = TARGET.get("movie_name", f"movie {MOVIE_ID}")
TZ = ZoneInfo(TARGET.get("timezone", "UTC"))
EXCLUDED_ROWS = set(FILTERS.get("excluded_rows", []))
EXCLUDED_COLS = set(FILTERS.get("excluded_columns", []))
IGNORED_DATES = set(FILTERS.get("ignored_dates", []))
EARLIEST = FILTERS.get("earliest_showtime", "00:00")
LATEST = FILTERS.get("latest_showtime", "23:59")
PARTY_SIZE = int(FILTERS.get("party_size", 1))
REQUEST_GAP = float(PACING.get("request_gap_seconds", 8))
DATE_SCAN_EVERY = int(PACING.get("date_scan_every", 3))
POLL_MINUTES = float(PACING.get("poll_minutes", 5))

BASE = "https://www.cinemark.com"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
BACKOFF_SCHEDULE = [120, 300, 900]

DATE_VALUE = re.compile(r'data-datevalue="(\d{4}-\d{2}-\d{2})"')
SHOWTIME_LINK = re.compile(
    r'/TicketSeatMap/\?TheaterId=(\d+)&(?:amp;)?ShowtimeId=(\d+)&(?:amp;)?'
    r'CinemarkMovieId=' + MOVIE_ID + r'&(?:amp;)?Showtime=([\d\-T:]+)'
)
# info="F,12,5,9,635630" = row letter, seat number, physical row, column, showtime
AVAILABLE_SEAT = re.compile(
    r'<button[^>]*class="seatAvailable seatBlock"[^>]*info="([A-Z]+),(\d+),\d+,(\d+),'
)


@dataclass
class Seat:
    row: str
    number: int
    col: int

    @property
    def label(self) -> str:
        return f"{self.row}{self.number}"


def _short(url: str) -> str:
    return url.removeprefix(BASE) or "/"


def fetch(url: str) -> str:
    _log.debug("GET %s", _short(url))
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Encoding": "gzip",
    })
    retry_after = 0
    for attempt, backoff in enumerate([0, *BACKOFF_SCHEDULE]):
        if backoff:
            wait = max(backoff, retry_after)
            _log.warning("backing off %ss (Retry-After: %s, schedule: %s)",
                         wait, retry_after or "none", backoff)
            time.sleep(wait)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
            time.sleep(REQUEST_GAP + REQUEST_GAP / 2 * random.random())
            return body.decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code not in (429, 403, 500, 502, 503):
                raise
            try:
                raw = e.headers.get("Retry-After", "0")
                retry_after = min(int(raw), 1800)
                _log.debug("server Retry-After: %s (capped to %s)", raw, retry_after)
            except ValueError:
                retry_after = 0
        except (urllib.error.URLError, TimeoutError):
            pass  # transient network hiccup: retry on the same schedule
    raise RuntimeError(f"gave up fetching {url} after {len(BACKOFF_SCHEDULE)} backoffs")


def notify(title: str, message: str) -> None:
    _log.info("ALERT: %s: %s", title, message)
    with ALERT_LOG.open("a") as f:
        f.write(f"{datetime.now().isoformat()}  {title}: {message}\n")
    hook = HERE / "notify-hook"
    if hook.exists() and os.access(hook, os.X_OK):
        try:
            subprocess.run([str(hook), title, message], capture_output=True, timeout=30)
        except Exception as e:  # noqa: BLE001: alerting must never kill the sweep
            _log.warning("notify-hook failed: %s", e)


def load_state() -> dict:
    if STATE_FILE.exists():
        _log.debug("load state")
        return json.loads(STATE_FILE.read_text())
    _log.debug("no state file, starting fresh")
    return {"dates": {}, "seats": {}}


def save_state(state: dict) -> None:
    _log.debug("save state")
    STATE_FILE.write_text(json.dumps(state, indent=1, sort_keys=True))


def showtimes_for(date: str) -> tuple[str | None, dict[str, str]]:
    """Return (theater_id, {showtime_id: iso_start}) for the movie on a date."""
    html = fetch(f"{BASE}/theatres/{THEATER}?showDate={date}")
    links = SHOWTIME_LINK.findall(html)
    return (links[0][0] if links else None), {sid: iso for _tid, sid, iso in links}


def qualifying(iso: str) -> bool:
    return EARLIEST <= iso[11:16] <= LATEST


def available_seats(theater_id: str, showtime_id: str, iso: str) -> list[Seat]:
    url = (f"{BASE}/TicketSeatMap/?TheaterId={theater_id}&ShowtimeId={showtime_id}"
           f"&CinemarkMovieId={MOVIE_ID}&Showtime={iso}")
    html = fetch(url)
    if "seatBlock" not in html:
        _log.warning("seat map %s returned no seat markup (page changed?)", showtime_id)
        return []
    seats = [Seat(row, int(num), int(col))
             for row, num, col in AVAILABLE_SEAT.findall(html)
             if row not in EXCLUDED_ROWS and int(col) not in EXCLUDED_COLS]
    _log.debug("seat map %s: %s available", showtime_id, len(seats))
    return seats


def seat_blocks(seats: list[Seat]) -> list[list[Seat]]:
    """Group seats into runs of physically adjacent seats (consecutive columns)."""
    blocks = []
    for row in sorted({s.row for s in seats}):
        run: list[Seat] = []
        for s in sorted((s for s in seats if s.row == row), key=lambda s: s.col):
            if run and s.col != run[-1].col + 1:
                blocks.append(run)
                run = []
            run.append(s)
        blocks.append(run)
    return blocks


def fmt_block(block: list[Seat]) -> str:
    if len(block) == 1:
        return block[0].label
    numbers = sorted(s.number for s in block)
    return f"{block[0].row}{numbers[0]}-{block[0].row}{numbers[-1]}"


def fmt_time(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%-I:%M%p").lower()


def prune_past(state: dict) -> None:
    today = datetime.now(TZ).date().isoformat()
    for d in [d for d in state["dates"] if d < today]:
        for sid in state["dates"][d]["showtimes"]:
            state["seats"].pop(sid, None)
        del state["dates"][d]


def sweep(state: dict, scan_dates: bool, only_dates: list[str] | None) -> None:
    first_run = not state["dates"]
    cycle = state.get("cycle", 0)
    _log.info("sweep #%s starting — %s @ %s",
              cycle, MOVIE_NAME, THEATER.split("/")[-1])
    _log.debug("theater URL: %s/theatres/%s", BASE, THEATER)
    if IGNORED_DATES:
        _log.debug("ignoring dates: %s", ", ".join(sorted(IGNORED_DATES)))
    prune_past(state)

    if scan_dates or first_run or only_dates or "theater_id" not in state:
        strip = only_dates or DATE_VALUE.findall(fetch(f"{BASE}/theatres/{THEATER}"))
        for date in sorted(set(strip)):
            if date in IGNORED_DATES or state["dates"].get(date, {}).get("showtimes"):
                continue
            try:
                theater_id, shows = showtimes_for(date)
            except Exception as e:
                _log.warning("date probe %s failed: %s", date, e)
                continue
            if theater_id:
                state["theater_id"] = theater_id
            state["dates"][date] = {"showtimes": shows}
            if shows and not first_run:
                notify(f"New date on sale: {date}",
                       f"{MOVIE_NAME} added for {date}: "
                       + ", ".join(sorted(fmt_time(i) for i in shows.values())))
        tracked_dates = {d for d, v in state["dates"].items() if v["showtimes"]}
        total_showtimes = sum(len(state["dates"][d]["showtimes"]) for d in tracked_dates)
        _log.info("date scan: %s dates (%s to %s), %s showtimes",
                  len(tracked_dates), min(tracked_dates), max(tracked_dates),
                  total_showtimes)
        save_state(state)

    watch = [
        (date, sid, iso)
        for date, info in sorted(state["dates"].items())
        for sid, iso in sorted(info["showtimes"].items(), key=lambda kv: kv[1])
        if date not in IGNORED_DATES and qualifying(iso) and (not only_dates or date in only_dates)
    ]
    total = 0
    for i, (date, sid, iso) in enumerate(watch):
        try:
            seats = available_seats(state["theater_id"], sid, iso)
        except Exception as e:  # noqa: BLE001: skip this showtime, keep sweeping
            _log.warning("seat check %s %s failed: %s", date, fmt_time(iso), e)
            continue
        total += len(seats)
        prev = set(state["seats"].get(sid, []))
        fresh = {s.label for s in seats} - prev
        state["seats"][sid] = sorted(s.label for s in seats)
        if fresh:
            _log.debug("seat diff %s %s: %s fresh, %s total",
                       date, fmt_time(iso), len(fresh), len(seats))
        openings = [b for b in seat_blocks(seats)
                    if len(b) >= PARTY_SIZE and any(s.label in fresh for s in b)]
        if openings and not first_run:
            notify(f"Seats open {date} {fmt_time(iso)}",
                   f"{MOVIE_NAME}: " + ", ".join(fmt_block(b) for b in openings))
        if i % 10 == 9:
            save_state(state)
    date_range = f"{watch[0][0]}..{watch[-1][0]}" if watch else "none"
    _log.info("seat scan: %s showtimes over %s, %s qualifying seats",
              len(watch), date_range, total)
    if first_run:
        _log.info("first run: baseline recorded — no alerts fired")


def report(state: dict) -> None:
    print(f"\n{MOVIE_NAME} @ {THEATER}")
    print(f"filters: rows {''.join(sorted(EXCLUDED_ROWS)) or 'none'} excluded, "
          f"shows {EARLIEST}-{LATEST}, party of {PARTY_SIZE}\n")
    tracked = {d: v for d, v in sorted(state["dates"].items()) if v["showtimes"]}
    if not tracked:
        print("no dates tracked yet: run a sweep first")
        return
    print(f"on sale: {min(tracked)} to {max(tracked)} ({len(tracked)} dates)\n")
    empty = True
    for d, info in tracked.items():
        for sid, iso in sorted(info["showtimes"].items(), key=lambda kv: kv[1]):
            seats = state["seats"].get(sid, [])
            if qualifying(iso) and seats:
                empty = False
                print(f"  {d} {fmt_time(iso):>8}  {len(seats):>3} seats: "
                      f"{', '.join(seats[:14])}{'...' if len(seats) > 14 else ''}")
    if empty:
        print("no qualifying seats right now: the watcher alerts when one opens")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="single sweep, then exit")
    ap.add_argument("-v", "--verbose", action="store_true", help="verbose output")
    ap.add_argument("--dates", nargs="*", help="restrict to specific YYYY-MM-DD dates")
    ap.add_argument("--report", action="store_true",
                    help="print availability from state.json and exit (no network)")
    args = ap.parse_args()
    if args.verbose:
        _log.setLevel(logging.DEBUG)

    if args.report:
        report(load_state())
        return

    while True:
        state = load_state()
        cycle = state.get("cycle", 0)
        try:
            sweep(state, scan_dates=(cycle % DATE_SCAN_EVERY == 0), only_dates=args.dates)
        except Exception as e:
            _log.error("sweep #%s ERROR: %s", cycle, e)
        state["cycle"] = cycle + 1
        save_state(state)
        if args.once:
            _log.info("sweep #%s complete", cycle)
            return
        _log.info("sweep #%s complete — next in %s min", cycle, int(POLL_MINUTES))
        time.sleep(POLL_MINUTES * 60)


if __name__ == "__main__":
    main()

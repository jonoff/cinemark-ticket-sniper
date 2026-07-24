# Cinemark ticket sniper

Push notifications via [ntfy.sh](https://ntfy.sh) when seats open up at a
sold-out Cinemark showing. (This fork replaces the original GitHub Issues
email alerts with ntfy.sh push notifications.)

Point it at any Cinemark theater and movie, say which rows, columns, and
showtimes you would accept, and it sends you a push alert when a matching seat
frees up or when new dates go on sale. Runs on GitHub Actions or locally.

Built to catch cancellations for The Odyssey in IMAX 70mm, which sold out
weeks ahead at every theater that can project it. Good seats reappear all the
time. Someone returns two tickets, a hold expires, and the seats go to whoever
happens to be looking.

## Setup

Create a topic at [ntfy.sh](https://ntfy.sh), install the app on your phone.
Copy `config.toml.example` to `config.toml` and fill in your theater, movie,
seat preferences, and ntfy topic.

Then choose how to run:

### GitHub Actions (fork)

1. Fork this repo. Keep the fork public: public repos get unlimited free
   Actions minutes.
2. Enable workflows on your fork. On your fork's GitHub page, click the
   **Actions** tab, then click the green **I understand my workflows, go ahead
   and enable them** button (or **Enable all workflows**). This is required
   because GitHub disables Actions by default on forked repos.
3. If the `watch` workflow still shows as **Disabled** in the left sidebar,
   click on it and click the **Enable workflow** button.
4. Run the `watch` workflow once by hand (**Actions → watch → Run workflow**).
   The first sweep records a quiet baseline and alerts start with the second.

### Local

```bash
git clone <url>
cd cinemark-ticket-sniper
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python3 watch.py
```

## Config

Everything lives in `config.toml`:

| key | meaning |
|---|---|
| `theater` | slug from the theater page URL: `cinemark.com/theatres/<slug>` |
| `movie_id` | numeric id for the movie (finding it: below) |
| `movie_name` | only used in alert text |
| `timezone` | the theater's IANA timezone, e.g. `America/Chicago` |
| `topic` | ntfy.sh topic name for push notifications |
| `excluded_rows` | rows you refuse, e.g. `["A", "B", "C", "D"]` |
| `excluded_columns` | seat columns to ignore, e.g. `[1, 27]` for edge seats |
| `ignored_dates` | dates to skip entirely, e.g. `["2026-08-15"]` |
| `earliest_showtime` / `latest_showtime` | accept window, 24h `HH:MM`, theater-local |
| `party_size` | alert only when this many adjacent seats open together |
| `samples_per_sweep` | showtimes sampled per sweep (weighted by cancellation likelihood); `0` = all |

To find `movie_id`: open your theater's page on cinemark.com, right-click any
showtime of your movie, and copy the link. It looks like
`/TicketSeatMap/?TheaterId=...&CinemarkMovieId=104867&...` and that number
is it.

## How it works

Cinemark's site is server-rendered, so dates, showtimes, and seat maps are all
plain HTML. On each run it fetches the seat map of every showing that passes
your filters, diffs availability against the previous run (state is a local
JSON snapshot saved to `state.json`), and on any newly opened seat or newly
listed date it posts to your ntfy.sh topic, which pushes the alert to your
phone. The scanner paces itself to about six requests a minute because Cinemark
rate-limits around 60-70 requests per ten minutes, occasionaly forcing a 30 min timeout.
The logic picks a weighted sampling of dates/times by default to prefer more frequent checks of closer show times. This can be disabled to force a complete sweep every run for all matching showtimes.

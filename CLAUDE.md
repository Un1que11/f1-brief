# F1 Brief — a digest before each race weekend

## Goal
About 2 days before the first practice session, send Misha a short digest in Russian via Telegram: what has changed since the last race, overall and for his favourites, and what to expect this weekend. Runs locally on a MacBook.

## Favourites (in `config.toml`)
- Team: McLaren
- Drivers: George Russell, Esteban Ocon
- For Ocon, also track news about his future in F1 (contract, moves).

## Stack
- Python 3.12+, uv, ruff, pytest
- Data: FastF1 (results, laps, telemetry), Jolpica-F1 API (the successor to Ergast: calendar, standings), OpenF1 (when needed), RSS from official and major F1 outlets for news, Open-Meteo for the weather forecast at the track
- Text generation: headless Claude Code (`claude -p`) receives **only** the collected JSON of facts and writes the digest from it
- Delivery: Telegram Bot API (token and chat_id in `.env`, never committed)
- Schedule: launchd, runs every day; the script itself decides whether it is time to send (2 days before FP1, once per round)

## Digest structure
1. Round, track, session schedule in Warsaw time
2. What has changed since the last race: standings, penalties, regulations, upgrades
3. McLaren: form over the last 3–5 races, pace, qualifying, upgrades
4. Russell and Ocon: results, comparison with their teammate, news
5. The track: its character, the favourites' past results there
6. Weather
7. "What to expect": 3–5 points, each explaining what the prediction is based on

## Rules for autonomous work
1. Each session takes **one** task: the first open one in `BACKLOG.md`.
2. Branch `feat/<id>-<slug>`, and a PR describing what was done and how to verify it.
3. `ruff check`, `ruff format --check` and `pytest` must pass before the PR is opened.
4. No network requests in tests: recorded fixtures only.
5. No made-up facts: everything in the digest must trace back to collected data. The generation prompt requires this explicitly, and every point has a source.
6. Never commit tokens or `.env`.
7. If information is missing, mark the task `BLOCKED: <question>` and move on to the next one.
8. Tick the task `[x]` in BACKLOG in the same PR.

## Work modes: day and night
- **Day (manual, Misha starts it):** heavy backlog tasks, one at a time, following the rules above.
- **Night (scheduled task):** light work only:
  1. Review open PRs: leave a comment with bugs found, risks and missing tests.
  2. Bug fixes: if an open PR has failing checks or the review found an obvious bug, fix it with a commit on the same branch.
  3. If there are no open PRs, or they are fine, take the first open task marked `[light]` in BACKLOG.md.
  4. Do not start heavy tasks (without `[light]`) at night. If there are no light tasks, do nothing and report that briefly.
- Misha sets the `[light]` tag himself; at night, never add or remove it.

## Language
Commits, branches, PR descriptions, code comments and reviews: English only.

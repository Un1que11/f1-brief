# Backlog — F1 Brief

## Phase 1 — skeleton
- [x] F1. Project with uv, ruff, pytest, `src/f1brief/` layout, README.
- [x] F2. [light] `config.toml`: favourites (teams: McLaren, Haas; drivers: Russell, Ocon, Bearman — as lists), digest language, how many days before FP1 to send, time zone Europe/Warsaw.
- [x] F3. [light] GitHub Actions: ruff + pytest.

## Phase 2 — collecting facts (each collector returns a typed model; tests on fixtures)
- [x] F4. Calendar and the next round (Jolpica), session schedule in Warsaw time.
- [x] F5. Driver and constructor standings + changes since the previous round.
- [x] F6. McLaren form: race and qualifying positions over the last N rounds.
- [x] F7. Russell and Ocon form + comparison with their teammate (qualifying, race).
- [x] F8. Pace from FastF1: median pace on long runs in the last race, favourites vs the leaders.
- [x] F9. The favourites' history at this track (last 3 seasons).
- [x] F10. News from RSS: filter by favourites and key topics (upgrades, penalties, regulations, contracts); a separate "Ocon's future" filter.
- [x] F11. Weather forecast at the track for the weekend's days (Open-Meteo).

## Phase 3 — digest
- [x] F12. Assemble all facts into one JSON with sources.
- [x] F13. Text generation via `claude -p` from a prompt template; a test that the prompt contains only facts from the JSON.
- [x] F14. Telegram formatting (MarkdownV2, escaping, splitting at the message length limit).
- [x] F15. Sending via the Bot API + a `--dry-run` mode (print to the console).

## Phase 4 — automation
- [x] F16. "Is it time to send" logic + protection against sending twice (state in SQLite).
- [x] F17. [light] launchd plist + installation instructions.
- [x] F18. Off-season: instead of digests, a short monthly summary (driver moves, testing, favourites' news).
- [x] F19. Optional: a short message after qualifying — the favourites' grid positions and what they mean for the race.

## Phase 5 — favourites as config + hot stories (see "Hot stories" in CLAUDE.md)
- [x] F20. Favourites from `config.toml` everywhere: replace the hardcoded McLaren/Russell/Ocon constants in form, drivers, pace, track_history, news and cli with config lists; add Haas and Bearman. Teammate comparison must work when two favourites are teammates (Ocon and Bearman at Haas): show the head-to-head once, not twice. Depends on F2.
- [x] F21. Stories store: SQLite schema and repository for stories, story_entities, story_updates, subscriptions (as in CLAUDE.md), with migrations and tests.
- [x] F22. Story detection: pick hot items from the F10 news, send only new items plus existing stories to `claude -p`, parse its JSON (attach / new story / ignore), reject any story or update without a source URL. Tests with fixture news and a stubbed `claude -p`.
- [x] F23. Interactive Telegram bot: long-polling `getUpdates` listener (separate process under launchd KeepAlive), inline buttons "Следить" / "Отписаться" / "Следить за Bearman, Haas", commands /stories, /story, /follow, /unfollow, /mute, /archive. Only accept updates from the configured chat_id. Tests on recorded update payloads.
- [x] F24. Story alerts: send a short message when a followed story gets an update; max 3 per day, quiet hours 23:00–08:00 Warsaw with a morning queue; every alert has an "Отписаться" button.
- [x] F25. Digest section "Твои сюжеты": followed stories' updates since the last digest go into the facts JSON with sources; the prompt explains why each matters this weekend (e.g. teammate head-to-head for a driver whose seat is in play).
- [x] F26. Story lifecycle: status changes (rumor → developing → confirmed/denied → resolved) only from sourced updates; one "no updates for 60 days, unfollow?" message per followed story.
- [x] F27. Seed story, created through the normal pipeline (not hardcoded): "Bearman to Ferrari in 2028 in place of Hamilton?" (status rumor), entities Bearman, Haas, Ferrari, Hamilton, followed by default. Sources to start from: https://www.gpfans.com/en/f1-news/1089724/new-lewis-hamilton-ferrari-contract-could-force-f1-star-to-leave-team/ and https://www.f1oversteer.com/news/ferrari-will-have-a-77-million-problem-if-they-replace-lewis-hamilton-with-oliver-bearman/ — check they still resolve; if not, mark BLOCKED and ask Misha for a link.
- [x] F28. Optional: a static HTML archive page generated locally (timeline per story, filters followed / all / resolved), opened from the `/archive` reply.

- [x] F36. [light] LaunchAgent for the bot: `f1brief launchd --job bot` writes a second plist (`local.f1brief.bot`) that starts `f1brief bot` at login with `KeepAlive` (restart on crash, throttle so a bad token does not loop fast), same PATH/workdir handling and log dir as the `run` plist (`bot.log`, `bot.err.log`). `--job run` stays the default. README: install, check, uninstall, and a note that only one listener may poll. Tests: the generated plist for both jobs.

## Phase 5b — Docker
- [x] F37. Docker image: `Dockerfile` on `python:3.12-slim` with uv (dependencies in a cached layer from `uv.lock`), Node + Claude Code CLI for `claude -p`, tzdata with `TZ=Europe/Warsaw`, non-root user, `.dockerignore` (.venv, .env, state.db, caches). No secrets or state in the image. Builds for `linux/arm64` and `linux/amd64`. Claude Code in a container cannot use the Mac's Keychain login: find the supported headless auth (e.g. a long-lived token from `claude setup-token` passed as an environment variable; check the current Claude Code docs, don't guess), document it, and fail with a clear message when it is missing.
- [ ] F38. `compose.yaml`:
  - `bot`: `f1brief bot`, `restart: unless-stopped` (one listener only: never scale it).
  - `runner`: `f1brief run` every hour via supercronic in the same image.
  - Data: named volumes `f1-state` (state.db, shared by both services) and `f1-cache` (FastF1 cache); `config.toml` bind-mounted (the bot's "Follow <names>" button writes to it, so not read-only) and `.env` via `env_file`.
  - Memory and CPU: `mem_limit` / `mem_reservation`, `cpus` and `pids_limit` per service (start with bot 128m, runner 1g because FastF1/pandas loads sessions; measure with `docker stats` and record the numbers in the PR).
  - Networks: one `egress` network for outbound calls only (Telegram, Jolpica, FastF1, RSS, Open-Meteo, Anthropic); no ports published.
  - Hardening: `read_only: true` with `tmpfs` /tmp and writable volumes only where needed, `cap_drop: [ALL]`, `no-new-privileges`, json-file log rotation.
  Depends on F37.
- [ ] F39. Easy start: `./setup.sh --docker` (checks Docker is running, does the same `.env` / Telegram / chat id steps, `docker compose up -d --build`, sends the test message, seeds the stories inside the container) and `./setup.sh --docker status|logs|down`. Switching to Docker unloads the launchd agents and switching back stops the containers, so two bot listeners never poll at once. README section "Docker". Depends on F38.

## Phase 6 — languages and accounts (approved 2026-10-20: tasks in order)
- [x] F29. Digest language: `ru`, `en`, `pl` for the existing global `[digest] language` (default `ru`). Prompt templates per language for the digest, the off-season summary and the post-qualifying message, all with the same facts-only and sources rules; a test that every language's template has the same sections and rules. Per-language name forms for drivers and teams (this covers the `name_ru` idea). Update CLAUDE.md where it says "in Russian".
- [ ] F30. Bot UI and alert texts from a message catalog (ru / en / pl), using the global language until F34; dates, times and numbers formatted per locale (24-hour times, decimal comma for ru / pl); a test that every key exists in all three languages. Update "Bot commands (Russian UI)" in CLAUDE.md.
- [ ] F31. Stories readable in any language: replace `title_ru` / `summary_ru` / `what_changed_ru` with language-neutral storage plus per-language texts, generated on demand by `claude -p` from the sourced update only and cached; migration for existing rows; the source URL stays the single source of truth. Depends on F29.
- [ ] F32. Users instead of a single recipient:
  - SQLite `users` (Telegram user id, chat_id, name, language, time zone, status active / blocked, created_at) and per-user settings (favourites, days before FP1, alert cap, quiet hours); `config.toml` keeps the defaults.
  - Add `user_id` to `subscriptions`, `story_alerts` and the send logs (`sends`, `qualifying_sends`, `summaries`), and migrate the existing rows to Misha's account, which is created from the current config and `.env` chat_id.
  - The "Следить за …" button writes to the pressing user's favourites instead of `config.toml`.
  - The per-user time zone replaces the hardcoded Europe/Warsaw in the session schedule, the send window and the quiet hours, with a test for a non-Warsaw user (e.g. America/New_York) across a DST change.
  - Update CLAUDE.md (goal, favourites, hot stories) to describe per-user behaviour.
- [ ] F33. Sign-up and sign-in through the bot (the Telegram account is the identity, no passwords):
  - `/start <invite code>` creates an account from a one-time code made by the admin with `/invite`. Codes expire after 7 days and are stored hashed. Without a valid code the bot answers politely and stores nothing.
  - `/start` is accepted only in private chats, so a group can never become a user.
  - The admin is Misha's Telegram user id from `.env`.
  - Users manage their own `/language`, `/favourites` and `/settings`.
  - `/delete_me` deletes the account and all its rows (subscriptions, alert queue, send logs), with a test that nothing is left behind.
  - Depends on F30, F32.
- [ ] F34. Per-user feeds:
  - The send window, digest, off-season summary, post-qualifying message, story subscriptions, alerts, the "Твои сюжеты" section and the 60-day "unfollow?" offer all run per user, with that user's favourites, language and time zone.
  - Facts are collected once per round and reused.
  - Story detection still runs once per run, over the union of all active users' favourites and followed entities.
  - The shared part of the digest is generated once per (language, favourites) combination. The per-user parts (the stories section, the schedule in the user's time zone, and news since that user's last digest) are built per user.
  - A test that one user's subscriptions, stories and settings never leak into another user's messages.
  - Depends on F29, F30, F31, F32.
- [ ] F35. Access control and limits:
  - Every bot handler and callback checks that the sender is an active user (replacing the single chat_id check).
  - Per-user command rate limits; admin commands `/users` and `/block`; a configurable cap on the number of users.
  - The README explains the cost: one `claude -p` call per (language, favourites) combination for the shared digest, plus the per-user parts and per-language story texts.
  - Depends on F33.

## Ideas (proposed by night job)
- Circuit facts as a cited section of the facts JSON (length, laps, corners, DRS zones, from a checked-in file with its source). Reason: digest section 5 asks for the track's character, but the JSON has nothing on it, so the generator can only say it is unknown.
- After generation, check that every digest point cites a source name that appears in the facts JSON, and refuse to send a digest that fails. Reason: the prompt requires sources (F13), but nothing verifies that the model followed it.
- Strip tracking parameters (`utm_*`) from news links when parsing feeds. Reason: Motorsport.com and Autosport links carry `?utm_source=RSS&...&utm_content=www|uk`, so the same article can arrive under two links (missed by dedup and by the story detection's checked list) and story sources are stored with tracking junk.
- (S) Failure alert in Telegram: when `run` is inside a send window but cannot deliver (calendar, facts or `claude -p` failure, Telegram refusal), or a round's FP1 starts with no digest sent, send one short plain message with the reason, at most once per round. Reason: under launchd errors only reach stderr, so a missed digest is currently silent and Misha finds out only when nothing arrives.
- (S) `f1brief doctor`: a read-only check of every source the digest uses (Jolpica, OpenF1, FastF1, each RSS feed, Open-Meteo), `claude` on PATH and the Telegram token via `getMe`, printing OK or the error per item. Reason: feeds and APIs change without notice, and today the first sign is a digest full of `missing` sections.
- (M) Sprint results in the favourites' form: add sprint finishes and points (Jolpica's separate sprint endpoint) to form and teammate comparison, labelled apart from Grand Prix results. Reason: `form.py` counts Grand Prix only, so on sprint weekends the form and points understate the favourites, while the schedule already knows `is_sprint_weekend`.
- (M) Last-race stewards' decisions for the favourites: from the OpenF1 race_control rows `pace` already fetches, keep the messages naming a favourite's car (incidents noted, investigations, penalties, deleted lap times) as a cited `incidents` list in the facts JSON. Reason: digest section 2 asks for penalties, but today they reach it only through RSS headlines, which rarely cover a 5-second penalty or a "no further investigation".
- (S) News window from the previous race instead of a fixed 7 days: `facts.collect` takes news since the last completed race (falling back to `NEWS_DAYS` when unknown). Reason: section 2 is "what changed since the last race", but with `NEWS_DAYS = 7` a three-week gap loses its first two weeks and a back-to-back repeats the previous digest's news.
- (S) Pre-computed championship gaps for the favourites in `standings` facts: points to the car ahead, to the car behind and to the leader. Reason: prompt rule 5 leaves the model to subtract points itself, and code arithmetic with a unit test is safer than trusting it on every run.
- (S) Keep each sent digest's facts JSON and text in the state SQLite next to the `sends` row. Reason: the send log records only status and message count, so when a digest says something odd there is no way to see what facts it was written from.
- (S) Last race's tyre strategy for the favourites: from the OpenF1 `stints` rows `pace` already fetches, list each favourite's stints (compound, first and last lap) and number of stops as a cited part of the `pace` facts. Reason: `pace.py` parses `compound` and then drops it, so the digest reports a long-run median without saying on which tyre or strategy it was set.
- (S) Qualifying gap to pole for the favourite teams: per round, the best Q1/Q2/Q3 time of each favourite driver against pole, in seconds and percent, computed in code with a unit test (needs the round's full qualifying from Jolpica, since `form` fetches only the constructor's own rows). Reason: positions alone hide whether a P11 was 0.1 s or 1 s off, and prompt rule 5 leaves the model to subtract lap-time strings itself.
- (S) Session-time correction after sending: when a session that was `start_warsaw: null` (time to be confirmed) in the sent digest gets a time, or a sent session's time changes in Jolpica, send one short plain message with the new Warsaw times, at most once per change. Reason: the digest goes out two days before FP1 and is never updated, so a TBC or moved start stays wrong in the only message Misha reads.
- (S) Length guard before sending: if the formatted digest needs more than a configured number of Telegram messages, ask `claude -p` once to shorten the same text from the same facts, and log the size either way. Reason: the prompt asks for "a few minutes" of reading, but nothing checks it, and `telegram_format` silently splits an overlong digest into as many messages as it takes.
- (S) Scheduled story detection: `run` calls `detect-stories` (at most a few times a day, e.g. when the last detection is over 6 hours old, recorded in the state file) before it sends the story alerts. Reason: detection is only run by hand today, so F24 alerts and the F25 digest section see no new updates unless Misha remembers to run it, and each detection is one `claude -p` call, so running it every hour would be wasteful.
- (S) Quiet hours for the scheduled messages too: the digest, the off-season summary and the post-qualifying message are held during 23:00–08:00 Warsaw (or sent with `disable_notification`), as long as their window is still open. Reason: the digest window opens at Warsaw midnight and launchd runs `run` hourly, so the digest and the monthly summary normally arrive around 00:00–01:00, and after an evening or night qualifying in the Americas the post-qualifying message lands in the night as well; today only story alerts respect the quiet hours.
- (S) Championship maths for the favourites: from the remaining rounds in the calendar (sprint weekends included) compute the maximum points still available and, for each favourite driver and team, whether they are still mathematically in the fight for the title or for the place ahead, and whether the leader can clinch this weekend, as a cited `standings` fact with a unit test. Reason: late in the season this is the question the digest should answer, and prompt rule 5 leaves the model to work out such sums itself.
- (S) `/next` bot command: the next round's name, circuit and session schedule in Warsaw time (with "time to be confirmed" where Jolpica has no time), from the same `schedule` code the digest uses. Reason: the schedule reaches Misha only once, two days before FP1, inside a long digest; between digests the bot already answers story commands but cannot say when the next session starts.
- (M) Prediction check: keep the "What to expect" points of each sent digest and, in the next digest, add a short cited look back at how they turned out against that round's results from Jolpica. Reason: section 8 makes predictions every weekend, but nothing ever checks them, so there is no way to tell whether they are worth reading; builds on the idea of keeping each sent digest in the state SQLite.
- (S) Post-race message for the favourites: after the race, one short message with each favourite's finishing position, points scored, championship position change and the teammate result, from Jolpica and with the same once-per-round send log as F19. Reason: the race result otherwise reaches Misha only in the next digest, two days before the next FP1, which can be three or more weeks later.
- (M) Neutralisation history at the track: from the OpenF1 race_control rows of this circuit's past editions (where OpenF1 has them, 2023 onwards), count safety cars, virtual safety cars and red flags per race as a cited fact for section 6. Reason: "What to expect" talks about strategy, and SC/VSC likelihood is a factor the model can only guess today, while `pace` already parses race_control messages for the last race.
- (S) Rotating backup of `state.db`: `run` makes at most one copy a day with SQLite's online backup API and keeps the last N copies (N in `config.toml`). Reason: stories, subscriptions, the alert queue and the send logs live only in that file, so a corrupt or deleted file loses the archive and lets `run` send a round's digest again.

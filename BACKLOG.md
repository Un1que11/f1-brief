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
- [ ] F24. Story alerts: send a short message when a followed story gets an update; max 3 per day, quiet hours 23:00–08:00 Warsaw with a morning queue; every alert has an "Отписаться" button.
- [ ] F25. Digest section "Твои сюжеты": followed stories' updates since the last digest go into the facts JSON with sources; the prompt explains why each matters this weekend (e.g. teammate head-to-head for a driver whose seat is in play).
- [ ] F26. Story lifecycle: status changes (rumor → developing → confirmed/denied → resolved) only from sourced updates; one "no updates for 60 days, unfollow?" message per followed story.
- [ ] F27. Seed story, created through the normal pipeline (not hardcoded): "Bearman to Ferrari in 2028 in place of Hamilton?" (status rumor), entities Bearman, Haas, Ferrari, Hamilton, followed by default. Sources to start from: https://www.gpfans.com/en/f1-news/1089724/new-lewis-hamilton-ferrari-contract-could-force-f1-star-to-leave-team/ and https://www.f1oversteer.com/news/ferrari-will-have-a-77-million-problem-if-they-replace-lewis-hamilton-with-oliver-bearman/ — check they still resolve; if not, mark BLOCKED and ask Misha for a link.
- [ ] F28. Optional: a static HTML archive page generated locally (timeline per story, filters followed / all / resolved), opened from the `/archive` reply.

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
- (S) Russian name forms in `config.toml` (optional `name_ru` per favourite team and driver), passed to the prompt as the names to use. Reason: the digest is in Russian, and without a given form the model's transliteration ("Расселл"/"Рассел", "Берман"/"Бирман") can differ between digests.
- (S) Keep each sent digest's facts JSON and text in the state SQLite next to the `sends` row. Reason: the send log records only status and message count, so when a digest says something odd there is no way to see what facts it was written from.
- (S) Last race's tyre strategy for the favourites: from the OpenF1 `stints` rows `pace` already fetches, list each favourite's stints (compound, first and last lap) and number of stops as a cited part of the `pace` facts. Reason: `pace.py` parses `compound` and then drops it, so the digest reports a long-run median without saying on which tyre or strategy it was set.
- (S) Qualifying gap to pole for the favourite teams: per round, the best Q1/Q2/Q3 time of each favourite driver against pole, in seconds and percent, computed in code with a unit test (needs the round's full qualifying from Jolpica, since `form` fetches only the constructor's own rows). Reason: positions alone hide whether a P11 was 0.1 s or 1 s off, and prompt rule 5 leaves the model to subtract lap-time strings itself.
- (S) Session-time correction after sending: when a session that was `start_warsaw: null` (time to be confirmed) in the sent digest gets a time, or a sent session's time changes in Jolpica, send one short plain message with the new Warsaw times, at most once per change. Reason: the digest goes out two days before FP1 and is never updated, so a TBC or moved start stays wrong in the only message Misha reads.
- (S) Length guard before sending: if the formatted digest needs more than a configured number of Telegram messages, ask `claude -p` once to shorten the same text from the same facts, and log the size either way. Reason: the prompt asks for "a few minutes" of reading, but nothing checks it, and `telegram_format` silently splits an overlong digest into as many messages as it takes.

# Backlog — F1 Brief

## Phase 1 — skeleton
- [x] F1. Project with uv, ruff, pytest, `src/f1brief/` layout, README.
- [x] F2. [light] `config.toml`: favourites (teams: McLaren, Haas; drivers: Russell, Ocon, Bearman — as lists), digest language, how many days before FP1 to send, time zone Europe/Warsaw.
- [ ] F3. [light] GitHub Actions: ruff + pytest.

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
- [ ] F17. [light] launchd plist + installation instructions.
- [x] F18. Off-season: instead of digests, a short monthly summary (driver moves, testing, favourites' news).
- [x] F19. Optional: a short message after qualifying — the favourites' grid positions and what they mean for the race.

## Phase 5 — favourites as config + hot stories (see "Hot stories" in CLAUDE.md)
- [ ] F20. Favourites from `config.toml` everywhere: replace the hardcoded McLaren/Russell/Ocon constants in form, drivers, pace, track_history, news and cli with config lists; add Haas and Bearman. Teammate comparison must work when two favourites are teammates (Ocon and Bearman at Haas): show the head-to-head once, not twice. Depends on F2.
- [x] F21. Stories store: SQLite schema and repository for stories, story_entities, story_updates, subscriptions (as in CLAUDE.md), with migrations and tests.
- [x] F22. Story detection: pick hot items from the F10 news, send only new items plus existing stories to `claude -p`, parse its JSON (attach / new story / ignore), reject any story or update without a source URL. Tests with fixture news and a stubbed `claude -p`.
- [ ] F23. Interactive Telegram bot: long-polling `getUpdates` listener (separate process under launchd KeepAlive), inline buttons "Следить" / "Отписаться" / "Следить за Bearman, Haas", commands /stories, /story, /follow, /unfollow, /mute, /archive. Only accept updates from the configured chat_id. Tests on recorded update payloads.
- [ ] F24. Story alerts: send a short message when a followed story gets an update; max 3 per day, quiet hours 23:00–08:00 Warsaw with a morning queue; every alert has an "Отписаться" button.
- [ ] F25. Digest section "Твои сюжеты": followed stories' updates since the last digest go into the facts JSON with sources; the prompt explains why each matters this weekend (e.g. teammate head-to-head for a driver whose seat is in play).
- [ ] F26. Story lifecycle: status changes (rumor → developing → confirmed/denied → resolved) only from sourced updates; one "no updates for 60 days, unfollow?" message per followed story.
- [ ] F27. Seed story, created through the normal pipeline (not hardcoded): "Bearman to Ferrari in 2028 in place of Hamilton?" (status rumor), entities Bearman, Haas, Ferrari, Hamilton, followed by default. Sources to start from: https://www.gpfans.com/en/f1-news/1089724/new-lewis-hamilton-ferrari-contract-could-force-f1-star-to-leave-team/ and https://www.f1oversteer.com/news/ferrari-will-have-a-77-million-problem-if-they-replace-lewis-hamilton-with-oliver-bearman/ — check they still resolve; if not, mark BLOCKED and ask Misha for a link.
- [ ] F28. Optional: a static HTML archive page generated locally (timeline per story, filters followed / all / resolved), opened from the `/archive` reply.

## Ideas (proposed by night job)
- Circuit facts as a cited section of the facts JSON (length, laps, corners, DRS zones, from a checked-in file with its source). Reason: digest section 5 asks for the track's character, but the JSON has nothing on it, so the generator can only say it is unknown.
- After generation, check that every digest point cites a source name that appears in the facts JSON, and refuse to send a digest that fails. Reason: the prompt requires sources (F13), but nothing verifies that the model followed it.

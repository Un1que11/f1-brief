# Backlog — F1 Brief

## Phase 1 — skeleton
- [x] F1. Project with uv, ruff, pytest, `src/f1brief/` layout, README.
- [ ] F2. [light] `config.toml`: favourites, digest language, how many days before FP1 to send, time zone Europe/Warsaw.
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
- [ ] F14. Telegram formatting (MarkdownV2, escaping, splitting at the message length limit).
- [ ] F15. Sending via the Bot API + a `--dry-run` mode (print to the console).

## Phase 4 — automation
- [ ] F16. "Is it time to send" logic + protection against sending twice (state in SQLite).
- [ ] F17. [light] launchd plist + installation instructions.
- [ ] F18. Off-season: instead of digests, a short monthly summary (driver moves, testing, favourites' news).
- [ ] F19. Optional: a short message after qualifying — the favourites' grid positions and what they mean for the race.

## Ideas (proposed by night job)
- Circuit facts as a cited section of the facts JSON (length, laps, corners, DRS zones, from a checked-in file with its source). Reason: digest section 5 asks for the track's character, but the JSON has nothing on it, so the generator can only say it is unknown.
- After generation, check that every digest point cites a source name that appears in the facts JSON, and refuse to send a digest that fails. Reason: the prompt requires sources (F13), but nothing verifies that the model followed it.

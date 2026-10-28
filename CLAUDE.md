# F1 Brief — a digest before each race weekend

## Goal
About 2 days before the first practice session, send Misha a short digest via Telegram, in the language set by `[digest] language` in `config.toml` (`ru`, `en` or `pl`; default `ru`): what has changed since the last race, overall and for his favourites, and what to expect this weekend. Runs locally on a MacBook.

## Favourites (in `config.toml`)
- Teams: McLaren, Haas
- Drivers: George Russell, Esteban Ocon, Oliver Bearman
- For Ocon, also track news about his future in F1 (contract, moves).
- Favourites are a list in config, never hardcoded: any number of teams and drivers. Every collector (form, teammate comparison, track history, pace, news) iterates over them.
- A favourite may set its name form per language (`names = { ru = "..." }`); texts in that language always use it.

## Hot stories (subscribable storylines)
A **story** is a developing storyline that spans weeks or months, e.g. "Bearman may replace Hamilton at Ferrari in 2028, so his 2027 teammate battle matters". Stories are kept in an archive and Misha can follow or unfollow each one.
- **Model (SQLite):** `stories` (id, slug, title, summary, language, status `rumor | developing | confirmed | denied | resolved`, created_at, last_update_at), `story_entities` (story_id, kind `driver | team | person`, ref — Jolpica id where one exists), `story_updates` (story_id, published_at, headline, url, source, what_changed, language), `subscriptions` (story_id, active, followed_at, unfollowed_at, muted_until). Texts are stored in the language detection wrote them in (Russian); other languages are generated on demand by `claude -p` from the stored story and sourced update only, and cached in `story_texts` / `update_texts`; the source URL is never generated.
- **Detection:** hot items are news about favourites or followed entities that touch contracts, seats, transfers, team principals, regulations or penalties. Each run, `claude -p` gets only the new items plus the list of existing stories and returns JSON: attach the item to a story, propose a new story, or ignore. Every story and every update must carry the source URL of the article it came from; no update without a source. Rumours are labelled as rumours.
- **Following a story** subscribes to that story only. The bot then *offers* (buttons, never automatic) to also follow its key entities, e.g. Bearman and Haas, which adds them to the favourites.
- **Unfollowing must be one tap:** every story message carries an inline "Отписаться" button; `/unfollow <id>` and `/mute <id> <days>` also work. Unfollowing keeps the story in the archive.
- **Bot (UI in `[digest] language` — `ru`, `en` or `pl` — until per-user languages in F34; buttons everywhere; every text in the message catalog `src/f1brief/messages/<language>.toml`, with the same keys in every language, composed by `src/f1brief/bot_texts.py`; labels below are the Russian ones):** `/start` explains the bot and when the next digest comes, then shows the main menu (stories, archive, next race, favourites, help); any non-command text gets the menu and every reply has «← Меню». Stories open from buttons (timeline with sources, «Следить» / «Отписаться» / «Без уведомлений на 7 дней»). Commands: `/stories` (followed and recent), `/story <id>` (timeline with sources), `/follow <id>`, `/unfollow <id>`, `/mute <id> <days>`, `/archive` (all, including resolved; the reply also carries a static HTML archive page with filters followed / all / resolved), `/next`, `/favourites` (remove buttons), `/help`. Callback data and command names are language-independent.
- **Alerts:** a new update on a followed story sends a short message right away, with at most 3 alerts a day and quiet hours 23:00–08:00 Warsaw (queued until morning).
- **Digest:** a section "Твои сюжеты" covers each followed story: what changed since the last digest and why it matters this weekend (e.g. Bearman vs his teammate in qualifying and race this season).
- **Lifecycle:** a story whose outcome is officially announced becomes `confirmed`/`denied`, then `resolved`. A followed story with no update for 60 days triggers one message offering to unfollow.

## Stack
- Python 3.12+, uv, ruff, pytest
- Data: FastF1 (results, laps, telemetry), Jolpica-F1 API (the successor to Ergast: calendar, standings), OpenF1 (when needed), RSS from official and major F1 outlets for news, Open-Meteo for the weather forecast at the track
- Text generation: headless Claude Code (`claude -p`) receives **only** the collected JSON of facts and writes the digest from it
- Delivery: Telegram Bot API (token and chat_id in `.env`, never committed)
- Schedule: launchd, runs every day; the script itself decides whether it is time to send (2 days before FP1, once per round)

## Digest structure
1. Round, track, session schedule in Warsaw time
2. What has changed since the last race: standings, penalties, regulations, upgrades
3. Favourite teams (McLaren, Haas): form over the last 3–5 races, pace, qualifying, upgrades
4. Favourite drivers (Russell, Ocon, Bearman): results, comparison with their teammate, news
5. "Твои сюжеты" ("Your stories" in `en`, "Twoje wątki" in `pl`): updates on followed stories since the last digest and why they matter this weekend
6. The track: its character, the favourites' past results there
7. Weather
8. "What to expect": 3–5 points, each explaining what the prediction is based on

## Rules for autonomous work
1. Each session takes **one** task: the first open one in `BACKLOG.md`.
2. Branch `feat/<id>-<slug>`, and a PR describing what was done and how to verify it.
3. `ruff check`, `ruff format --check` and `pytest` must pass before the PR is opened.
4. No network requests in tests: recorded fixtures only.
5. No made-up facts: everything in the digest must trace back to collected data. The generation prompt requires this explicitly, and every point has a source.
   Every language has its own prompt templates (`src/f1brief/prompts/<language>/`) with the same rules and sections; a change to one is made in all of them.
6. Never commit tokens or `.env`.
7. If information is missing, mark the task `BLOCKED: <question>` and move on to the next one.
8. Tick the task `[x]` in BACKLOG in the same PR.

## Work modes: day and night
- **Day (manual, Misha starts it):** heavy backlog tasks, one at a time, following the rules above.
- **Night (three scheduled tasks, in this order):**
  1. **Review & refactor:** review every open PR (bugs, risks, missing tests, deviations from these rules; never approve or merge) and fix failing checks or obvious small bugs with a commit on the same branch; large rewrites are described in the review, not done. If no open PR needs fixing, self-review `main`: a real bug goes on `fix/<slug>` with a test that fails without the fix; otherwise at most one small `refactor/<slug>` PR (no behaviour change, tests green). It never starts backlog tasks.
  2. **Implement:** takes the first open task in BACKLOG.md that is not `BLOCKED`, light or heavy, following the day rules (branch `feat/<id>-<slug>`, tests, checks green, tick `[x]` in the same PR). It skips a task that already has an open PR or depends on work in an unmerged PR. A heavy task that cannot be finished cleanly goes up as a draft PR listing what is left. Missing information: mark the task `BLOCKED: <question>` and take the next one.
  3. **Backlog ideas:** proposes 1–5 ideas, each with a one-line reason, only through a PR (`docs/backlog-ideas-<YYYY-MM-DD>`) that adds them to the `## Ideas (proposed by night job)` section at the end of BACKLOG.md. Ideas are never tagged `[light]`, never inserted into the numbered phases and never duplicate existing tasks or ideas; Misha moves them into the backlog himself. At most one ideas PR is open at a time: new ideas are added to it, or wait until it is merged or closed. It changes no code.
- **Limits:** each task opens at most one new PR per night. Implement opens no new PR while three or more of its PRs are waiting for Misha.
- Misha sets the `[light]` tag himself; at night, never add or remove it.

## Language
Commits, branches, PR descriptions, code comments and reviews: English only.
Digest texts (digest, off-season summary, message after qualifying) are in the configured `[digest] language`. The bot UI, story alerts, the offer to unfollow a quiet story and the archive page use the same language, from the message catalog (F30); a new text is added to every `messages/<language>.toml`.

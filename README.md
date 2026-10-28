# f1-brief

A pre-race-weekend F1 digest delivered to Telegram. About two days before FP1 it collects
facts (calendar, standings, form, pace, weather, news) and turns them into a short digest.
It runs locally on macOS. See `CLAUDE.md` for the full spec and `BACKLOG.md` for the roadmap.

## How it works

1. **Collect facts.** Calendar and standings from Jolpica-F1, sessions and pace from OpenF1,
   track weather from Open-Meteo, news from RSS feeds of major F1 outlets.
2. **Write the digest.** A headless LLM run gets only the collected facts as JSON and writes the
   digest in Russian, English or Polish. Every claim must trace back to a collected fact and a
   source; prompt templates per language are kept in sync by a test.
3. **Track stories.** New items about favourite drivers and teams (contracts, seats, transfers,
   penalties) are attached to long-running storylines or open new ones, each update with its
   source URL. Users follow a story with one tap and get rate-limited alerts with quiet hours.
4. **Deliver.** A Telegram bot with inline buttons sends the digest about two days before FP1, a
   short message after qualifying, and story alerts. launchd runs it hourly; a Docker image is
   also available.

Python 3.12, uv, SQLite, pytest (760+ tests, no network in tests), ruff, GitHub Actions.

## Quick start (macOS)

```bash
git clone https://github.com/Un1que11/f1-brief && cd f1-brief
./setup.sh
```

`setup.sh` does everything and is safe to re-run (e.g. after `git pull`): installs `uv` if it is
missing, checks Claude Code, runs `uv sync`, sets up the Telegram bot in `.env` (asks for the
token, checks it, and finds your chat id after you send the bot any message), sends a test
message, seeds the first stories, and installs two launchd agents: `local.f1brief.run` (hourly:
digest, message after qualifying, story alerts) and `local.f1brief.bot` (the always-on bot for
`/start`, its menu and the story buttons).

```bash
./setup.sh status      # are both agents running
./setup.sh uninstall   # stop and remove both agents (state.db and .env are kept)
```

## Requirements

- [uv](https://docs.astral.sh/uv/) (it installs Python 3.12+ for you if needed)

## Setup

```bash
uv sync
```

## Settings

`config.toml` in the working directory holds the settings: favourite teams and drivers (as
lists of Jolpica ids with display names and news keywords), the digest language, how many days
before FP1 the digest goes out, and the time zone. It has no secrets; the Telegram token and
chat id stay in `.env`. Without the file the built-in defaults (identical to the checked-in
file) apply; a file with a typo or a bad value is an error, not ignored. Use another file with
`uv run f1brief --config PATH <command>` and check what is in effect with `uv run f1brief config`.

`digest.language` is the language of the digest, the off-season summary and the message after
qualifying: `ru` (the default), `en` or `pl`. Each language has its own prompt templates in
`src/f1brief/prompts/<language>/`; they are word for word the same apart from the output
language, and a test fails if their rules or sections drift apart. The bot's buttons and
replies, the story alerts and the archive page use the same language (F30): their texts live in
`src/f1brief/messages/<language>.toml`, one file per language with the same keys (a test checks
it), with dates, 24-hour times, numbers (decimal comma for `ru` / `pl`) and plurals per language.
A favourite may give its name form per language, e.g.
`names = { ru = "Джордж Расселл" }`: the digest then always uses that form in that language
(otherwise the model picks one, which can differ between digests). News matching still uses
`name` and `aliases`.

`due` and `run` take their `--days-before` default from `digest.days_before_fp1`. Every
collector iterates over the favourites lists: team form for each favourite team, driver form
with the teammate comparison for each favourite driver, pace, track history, news tags and story
detection. When two favourite drivers are teammates (Ocon and Bearman at Haas), their
head-to-head is shown once, under the one listed first. News about the future in F1 (contract,
moves) is flagged for the drivers with `track_future = true`.

## Run

```bash
uv run f1brief --help
uv run f1brief config            # the settings in effect (config.toml or the defaults)
uv run f1brief next              # next race weekend, session times in Europe/Warsaw
uv run f1brief next --season 2027
uv run f1brief standings         # standings + changes since the previous round
uv run f1brief form --last 5     # the favourite teams' race/qualifying form, last 5 rounds
uv run f1brief drivers           # the favourite drivers' form vs their teammates
uv run f1brief pace              # long-run pace in the last race: favourites vs leaders
uv run f1brief track             # favourites at the next race's circuit, last 3 editions
uv run f1brief news --days 7     # news about the favourites and key topics (RSS)
uv run f1brief weather           # forecast at the circuit for the next race weekend
uv run f1brief facts --output facts.json   # every fact above as one JSON, with sources
uv run f1brief digest --output digest.json # the digest as a card (JSON), written by `claude -p`
uv run f1brief digest --facts facts.json --prompt-only   # the exact prompt, without claude
uv run f1brief format --input digest.json  # the card as Telegram HTML messages
uv run f1brief send --input digest.json --dry-run   # what would be sent, without sending
uv run f1brief send --input digest.json    # send the digest to the Telegram chat
uv run f1brief preview --kind digest       # a sample digest|quali|alert|summary, printed
uv run f1brief preview --kind alert --send # the same, sent to the admin chat to see the look
uv run f1brief due                         # is it time to send the next round's digest?
uv run f1brief run --dry-run               # the daily job, printing instead of sending
uv run f1brief run                         # the daily job: when due, write and send, once
uv run f1brief forget --season 2026 --round 18   # allow `run` to send that round again
uv run f1brief summary --prompt-only       # the off-season monthly summary's prompt, today
uv run f1brief summary                     # the monthly summary card (never sent)
uv run f1brief forget --month 2027-01      # allow `run` to send that month's summary again
uv run f1brief quali --round 17 --prompt-only   # the prompt of the message after qualifying
uv run f1brief quali                       # that message's card for this weekend (never sent)
uv run f1brief forget --season 2026 --round 18 --qualifying   # allow it to be sent again
uv run f1brief detect-stories --dry-run   # what claude makes of the new hot news items
uv run f1brief seed-stories --dry-run     # what claude makes of the articles in story_seeds.toml
uv run f1brief alerts --dry-run           # the story alerts due now (not sent)
uv run f1brief archive-page               # the story archive as a static HTML page
uv run f1brief launchd                    # the macOS LaunchAgent plist for `run` (printed)
```

`facts` is what the digest is generated from. Each section lists the exact requests it was
built from under `sources`; a section that could not be collected is `null` and its reason is
in `missing`.

`digest` needs [Claude Code](https://docs.claude.com/en/docs/claude-code) (`claude`) on PATH and
logged in. The prompt is `src/f1brief/prompts/<language>/digest.md` with the facts JSON inserted and
nothing else; `claude` runs without tools, MCP servers or user settings, so the digest can only use
those facts. `--model` picks the model (default: claude's own), `--facts` reuses a saved
document instead of collecting again.

Claude answers with structured JSON, not prose: sections of short items, each with the sources
it came from (`name` and `url`, copied from the facts). Before anything is saved or sent, a
source that does not appear in the facts document is dropped, and so is an item left without
one, unless it only says that something is unknown; each drop is a warning on stderr. Code adds
what it knows better than the model: the weekend's dates and the session schedule in Warsaw
time. The result is a **card**, which `digest`, `summary` and `quali` print (or save with
`--output`) as JSON.

`format` turns a saved card into the messages that will be sent to Telegram, in Telegram HTML
(`parse_mode=HTML`; names and headlines are escaped, link previews are off): a header line
(`🏁 <b>…</b>` and the dates), the schedule as an aligned `<pre>` table, each section under a
bold heading with one emoji, items as `•` bullets with the key number in bold and the sources
as short linked labels at the end (`<a href="…">Jolpica-F1</a>`). A long section (usually the
news) is folded into an expandable quote. Messages are split between sections, then items,
then words, never inside a tag or an entity, so none is longer than 4096 characters
(`--limit` to try a smaller one), and a heading always stays with its first item. It reads
stdin by default, so `f1brief digest | f1brief format` works too.

`preview --kind digest|quali|alert|summary` prints a sample of each message from the sample
cards in `src/f1brief/samples/` (synthetic text with example links), rendered the way `run`
renders a real one; `--send` sends it to the admin chat (`TELEGRAM_CHAT_ID`, the only chat for
now), to check the look in Telegram itself.

`send` renders a saved card the same way and sends each message to the chat through the
Telegram Bot API, in order, stopping at the first one Telegram refuses. It needs the bot token
and the chat id: set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in the environment, or copy
`.env.example` to `.env` (git-ignored) and fill it in; `--env-file` points somewhere else.
`--dry-run` prints the messages instead and needs neither. The token is never printed: it is
redacted from every error.

`run` is what the daily schedule calls. It sends the digest for a round once, on its first
run inside the send window: from midnight Warsaw time two days before FP1 (its Warsaw date;
`--days-before` changes the two) until FP1 starts. Outside the window, or when the round was
already sent, it says why and exits 0 without collecting anything. Otherwise it checks the
Telegram settings, collects the facts, writes the digest with `claude -p` and sends it.
`due` shows the same decision and does nothing else.

Sent rounds are kept in `state.db` (SQLite, git-ignored; `--state` to use another file). A run
claims the round right before sending, so two runs at once cannot both send. If nothing reached
the chat (Telegram refused the first message, or there was no connection), the claim is dropped
and the next run tries again. If some messages were, or may have been, delivered (a failure
after the first message, a timeout, a crash), the round is never resent automatically: check
the chat, then `forget` the round to allow another send. A round whose FP1 has started without
a digest is skipped. `run --dry-run` prints the messages instead and records nothing (it does
run `claude -p`).

In the off-season (before the Warsaw date of the year's first FP1, or after its last race)
`run` sends a short monthly summary instead: driver moves, testing and launches, news about
the favourites, and when the next season starts. It goes out on the first run in the first
7 days of a month, unless a race weekend starts within 14 days or the season ended less than
14 days ago, and once per month like a round's digest (`forget --month` allows another send).
It covers the news since the first day of the previous month. RSS feeds only carry their
latest items, so every off-season run also keeps the relevant news it fetched in `state.db`,
and the summary reads it back. The prompt is `src/f1brief/prompts/<language>/summary.md`, with the same
rules as the digest's. `summary` writes one on any day without sending it (`--prompt-only`
for the prompt); it reads the archive but adds nothing to it.

During a race weekend `run` also sends a short message after qualifying: the favourites'
positions, the comparison with their teammates and what that means for the race. It is due
from two hours after the qualifying start until the race starts, once per round (`forget
--qualifying` allows another send), with the same claim and partial rules as the digest. While
Jolpica has not published the classification yet, the run sends nothing and the next one tries
again. The facts are the qualifying classification, the favourite drivers' recent form (grid
against finish) and the news since FP1; Jolpica has no starting grid before the race, so grid
penalties come only from the news. The prompt is `src/f1brief/prompts/<language>/qualifying.md`. `quali`
writes the message for any round without sending it (`--round`, `--season`; `--driver` and
`--team` to try other favourites; `--prompt-only` for the prompt).

Hot stories (storylines Misha can follow, see CLAUDE.md) are kept in `state.db` too, in the
tables of `src/f1brief/stories.py`: each story with the drivers, teams and people it is about,
its timeline of updates (every one with the URL of its source) and whether it is followed or
muted. `detect-stories` fills them (F22): it takes the hot news items of the last 7 days
(`--days`), those about a favourite or an entity of a followed story that touch contracts,
seats, transfers, team principals, regulations or penalties, and sends the ones it has not
checked before, with the open stories, to `claude -p` (prompt: `src/f1brief/prompts/stories.md`).
For each item claude attaches it to a story, starts a new one or ignores it. The source of every
story and update is the item's own URL, headline and outlet, never claude's; a decision naming
no item sent is rejected. Answered items are not sent again. `--dry-run` shows the decisions
and stores nothing, `--prompt-only` prints the prompt without running claude.

Seed stories (F27): `story_seeds.toml` lists articles to start stories from, each described as
a feed would carry it (link, headline, outlet, date, a short summary). `f1brief seed-stories`
sends the ones no story has an update from yet through the same detection as `detect-stories`
(same prompt, same checks, same storing; no news feed is read), so claude decides the story's
title, summary, status and entities, and every update keeps the article's URL as its source. A
seed with `follow = true` (the default) then follows the story it started or joined, in the same
transaction. A seed claude ignored is sent again by the next run; one that is in a story is never
sent again, so a story unfollowed later stays unfollowed. The checked-in seeds are the two
articles about Bearman replacing Hamilton at Ferrari; run `uv run f1brief seed-stories` once
(`--dry-run` to see claude's decision first, `--prompt-only` for the prompt).

Story alerts (F24): when a followed story gets an update (recorded after it was followed),
`f1brief alerts` sends a short card: `🔔` and the story's title in bold, its status chip
(`🟡 слух`, `🟠 развивается`, `🟢 подтверждено`, `🔴 опровергнуто`), one line per update with
what changed and the source as a linked label, and the buttons «Хронология» and «Отписаться»;
a story's waiting updates go in one message. At most 3 alerts per Warsaw calendar day and none
from 23:00 to 08:00 Warsaw: the rest wait for a later run, the oldest first. Updates of a muted
story, and updates that have waited more than 3 days, are skipped (the digest covers them).
`run` sends the due alerts at the end of every run, so under launchd they go out within the hour
and the night's queue with the first run after 08:00. `alerts --dry-run` prints them and records
nothing.

Story lifecycle (F26): a story moves rumor → developing → confirmed or denied → resolved, only
with a sourced update and only forward (steps may be skipped up to confirmed or denied; only a
confirmed or denied story becomes resolved). Each update records the status it set, and
`/story` shows it next to the source. When claude gives an attached item an earlier status, the
update is stored without it and `detect-stories` reports "status not changed"; a new story never
starts as resolved. A followed story with no update for 60 days (counted from its latest update,
or from when it was followed if later) gets one message offering to unfollow, with the
"Отписаться" button; a later update starts a new 60 days. `alerts` (and so `run`) sends these
offers after the alerts, never in the quiet hours or while the story is muted, and they do not
count towards the 3 alerts a day.

The digest's "Твои сюжеты" section (F25): the facts' `stories` section lists every followed
story (muted ones too) with its title, summary, status and entities, and the updates recorded
since the last digest went out (the latest row in `sends`; 14 days back when no digest has been
sent yet), each with its headline, outlet and link, which are also the section's `sources`.
Entities that are favourites are marked, so the prompt can tie a story to that driver's or
team's facts in the same document (e.g. a teammate head-to-head for a driver whose seat is in
play). With no followed story the digest leaves the section out. `facts`, `digest` and `run`
read it from `--state`.

`f1brief bot` (F23, F40) is the Telegram bot: a long-polling listener that runs until stopped
(meant for its own launchd job with `KeepAlive`). It answers only the chat in
`TELEGRAM_CHAT_ID`; an update from any other chat gets no answer and is logged to stderr once
per chat (`ignored chat <id>; TELEGRAM_CHAT_ID is <id>`), so a wrong chat id shows up in
`bot.err.log`. UI in `digest.language`, buttons everywhere (the labels below are the Russian
ones):

- `/start` says in a few lines what the bot sends and when the next digest comes (Warsaw
  time), then shows the main menu: «📰 Сюжеты», «🗂 Архив», «🏁 Следующая гонка», «⭐ Избранное»,
  «❓ Помощь». Any text that is not a command gets the menu, and every reply has «← Меню».
- «📰 Сюжеты» (`/stories`) lists the followed and recent stories, each as a button that opens
  its timeline with sources and the buttons «Следить», «Отписаться» (one tap) and «Без
  уведомлений на 7 дней». Once a story is followed, the bot offers "Следить за <names>" for its
  drivers and teams that are not favourites yet; pressing it appends them to `config.toml`
  (people without a Jolpica id are skipped).
- «🏁 Следующая гонка» (`/next`): the round, circuit and session times in Warsaw, and when the
  digest for it comes (or when it was sent). The calendar is read from Jolpica at most once an
  hour.
- «⭐ Избранное» (`/favourites`): the favourite teams and drivers from `config.toml`, each with
  a «✕» button that removes it from the file (comments and other entries are kept).
- The typed commands keep working: `/story <id>`, `/follow <id>`, `/unfollow <id>`,
  `/mute <id> <days>`, `/archive` (all, resolved included), `/help`.

On start the listener registers its commands with `setMyCommands`, so they appear under the
"/" button in Telegram. Every text lives in the message catalog `src/f1brief/messages/`. Only one listener per
bot can poll: a second one, or a webhook on the bot, stops it with an error.

```bash
uv run f1brief bot --check                 # the bot's username, webhook, configured chat id
uv run f1brief bot --dry-run --updates tests/fixtures/telegram/updates.json --chat-id 424242
uv run f1brief bot                         # the listener (Ctrl-C to stop)
```

`--check` asks Telegram for the bot's username and whether a webhook is set (a webhook blocks
polling), prints them with the configured chat id and exits: the first thing to run when the
bot stays silent. `--dry-run` answers saved updates (a `getUpdates` answer or a JSON list) and
prints the replies and buttons: nothing is sent, the story changes are rolled back and
`config.toml` is not written. It needs no token; the chat to accept is `--chat-id` or
`TELEGRAM_CHAT_ID`.

The archive is also a static HTML page (F28): every story with its timeline, each update
linking to its source, and three filters, "Слежу" (followed now), "Все" and "Завершённые". It
opens on the followed stories, or on all of them when none is followed. The page has no script
(filters are plain CSS) and loads nothing from elsewhere, so it works opened as a file. The
bot's `/archive` reply sends it as an HTML file (`f1-archive-<date>.html`) after the list; tap
it in Telegram to open it. To make it locally:

```bash
uv run f1brief archive-page                 # writes archive.html (git-ignored)
open archive.html
uv run f1brief archive-page --output - > /tmp/archive.html   # or to stdout
```

`python -m f1brief` works too, from inside the virtual environment.

To keep the bot running in the background (starts at login, restarted if it stops, at most every
30 s so a bad token cannot loop fast), install its own LaunchAgent (F36):

```bash
uv run f1brief launchd --job bot --output ~/Library/LaunchAgents/local.f1brief.bot.plist
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/local.f1brief.bot.plist 2>/dev/null || true
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.f1brief.bot.plist
```

Logs go to `~/Library/Logs/f1brief/bot.log` and `bot.err.log`. Stop it before running
`f1brief bot` by hand (`launchctl bootout gui/$(id -u)/local.f1brief.bot`): only one listener
may poll.

## Schedule on the Mac (launchd)

`run` is meant to be started by launchd every hour: it decides by itself whether anything is
due and otherwise exits within seconds. Hourly rather than daily, because the message after
qualifying is only due between two hours after qualifying starts and the race start. A run
missed while the Mac sleeps is made up once when it wakes; nothing runs while it is off.

Before installing, check by hand from the checkout that everything works: `uv sync`,
`.env` filled in (see `send` above), `claude` logged in, then `uv run f1brief due` and
`uv run f1brief run --dry-run`.

Install (from the checkout):

```bash
uv run f1brief launchd --output ~/Library/LaunchAgents/local.f1brief.run.plist
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/local.f1brief.run.plist 2>/dev/null || true
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.f1brief.run.plist
launchctl print gui/$(id -u)/local.f1brief.run | head -20   # loaded? last exit code?
```

`f1brief launchd` writes the plist for this checkout (`--workdir` for another one) and creates
the log directory; without `--output` it only prints the plist. launchd starts jobs with a bare
PATH and `/` as the working directory, so the plist holds the full path of `uv`, a PATH with
the directories of `uv` and `claude` (found on your PATH now; `--uv` and `--claude` to give
them) plus Homebrew's, and the checkout as the working directory, where `config.toml`, `.env`
and `state.db` are read. It holds no secrets. If `claude` was installed with npm, `node` must
be in one of those directories too. `--interval MINUTES` changes the hourly schedule.

The job runs once right after `bootstrap`. Its output goes to `~/Library/Logs/f1brief/run.log`
and errors to `run.err.log` there (`--log-dir` for another place); `tail -f` them, or start a
run now with `launchctl kickstart gui/$(id -u)/local.f1brief.run`. After moving the checkout
or reinstalling `uv` or `claude`, write the plist again and repeat the `bootout`/`bootstrap`
lines. To uninstall:

```bash
launchctl bootout gui/$(id -u)/local.f1brief.run
rm ~/Library/LaunchAgents/local.f1brief.run.plist
```

## Docker image

`Dockerfile` builds one image for every command: `python:3.12-slim` with the locked
dependencies (`uv sync --locked`, in their own cached layer), the Claude Code CLI (a pinned
version, `CLAUDE_CODE_VERSION`; Node is used only in a build stage to install its native
binary), the `Europe/Warsaw` time zone, and a non-root user `f1brief`. The entrypoint is
`f1brief`, the working directory `/data`, where `config.toml`, `.env` and `state.db` are read
as in the checkout. Nothing secret or stateful is in the image: `.dockerignore` lets in only
`pyproject.toml`, `uv.lock`, `README.md`, `story_seeds.toml` (copied to
`/app/story_seeds.toml`) and `src/`.

```bash
docker build -t f1brief .                                       # for this Mac's platform
docker buildx build --platform linux/arm64,linux/amd64 -t f1brief .   # both platforms
```

Claude Code in a container cannot use the Mac's Keychain login. Its supported headless login
is a long-lived (one-year) OAuth token for your Claude subscription: run `claude setup-token`
once on the Mac, approve in the browser, and put the printed token in `.env` as
`CLAUDE_CODE_OAUTH_TOKEN=...` (an `ANTHROPIC_API_KEY` from the Claude Console works too, billed
per use). The token can only make model requests. Pass `.env` to the container as its
environment, so both the token and the Telegram settings reach it:

```bash
docker run --rm --env-file .env -v "$PWD/config.toml:/data/config.toml:ro" \
  -v f1-state:/data f1brief run --dry-run
```

`--env-file` takes `KEY=value` lines literally, so write values without quotes. Inside the
image (`F1BRIEF_CONTAINER=1`) every command that calls `claude -p` stops before starting it,
with a message naming `CLAUDE_CODE_OAUTH_TOKEN`, when neither a token, an API key nor a login
stored in `$CLAUDE_CONFIG_DIR` (`/home/f1brief/.claude`) is there. Claude Code's auto-updater is
off; to update it, change `CLAUDE_CODE_VERSION` and rebuild. The `Docker` workflow
(`.github/workflows/docker.yml`) builds both platforms and smoke-tests the amd64 image on every
pull request that touches the image's inputs; it pushes nothing and uses no secrets. The compose
setup with both services is F38.

## Checks

These must all pass before a PR is opened:

```bash
uv run ruff check
uv run ruff format --check
uv run pytest
```

GitHub Actions (`.github/workflows/ci.yml`) runs the same three checks, after
`uv sync --locked`, on every pull request and every push to `main`. The job uses no secrets.

## Layout

```
src/f1brief/   application package
tests/         pytest suite (no network: recorded fixtures only)
tests/fixtures/  recorded API responses (e.g. jolpica/schedule_2026.json)
.github/workflows/ci.yml  CI: ruff + pytest
.github/workflows/docker.yml  CI: build and smoke-test the Docker image
Dockerfile, .dockerignore  the container image (F37)
```

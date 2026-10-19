# f1-brief

A pre-race-weekend F1 digest delivered to Telegram. About two days before FP1 it collects
facts (calendar, standings, form, pace, weather, news) and turns them into a short digest.
It runs locally on macOS. See `CLAUDE.md` for the full spec and `BACKLOG.md` for the roadmap.

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
uv run f1brief digest                      # the Russian digest text, written by `claude -p`
uv run f1brief digest --facts facts.json --prompt-only   # the exact prompt, without claude
uv run f1brief format --input digest.txt   # the digest as Telegram MarkdownV2 messages
uv run f1brief send --input digest.txt --dry-run   # what would be sent, without sending
uv run f1brief send --input digest.txt     # send the digest to the Telegram chat
uv run f1brief due                         # is it time to send the next round's digest?
uv run f1brief run --dry-run               # the daily job, printing instead of sending
uv run f1brief run                         # the daily job: when due, write and send, once
uv run f1brief forget --season 2026 --round 18   # allow `run` to send that round again
uv run f1brief summary --prompt-only       # the off-season monthly summary's prompt, today
uv run f1brief summary                     # the monthly summary text (never sent)
uv run f1brief forget --month 2027-01      # allow `run` to send that month's summary again
uv run f1brief quali --round 17 --prompt-only   # the prompt of the message after qualifying
uv run f1brief quali                       # that message for the current weekend (never sent)
uv run f1brief forget --season 2026 --round 18 --qualifying   # allow it to be sent again
uv run f1brief detect-stories --dry-run   # what claude makes of the new hot news items
uv run f1brief alerts --dry-run           # the story alerts due now (not sent)
uv run f1brief launchd                    # the macOS LaunchAgent plist for `run` (printed)
```

`facts` is what the digest is generated from. Each section lists the exact requests it was
built from under `sources`; a section that could not be collected is `null` and its reason is
in `missing`.

`digest` needs [Claude Code](https://docs.claude.com/en/docs/claude-code) (`claude`) on PATH and
logged in. The prompt is `src/f1brief/digest_prompt.md` with the facts JSON inserted and nothing
else; `claude` runs without tools, MCP servers or user settings, so the digest can only use
those facts. `--model` picks the model (default: claude's own), `--facts` reuses a saved
document instead of collecting again.

`format` turns a digest text into the messages that will be sent to Telegram: MarkdownV2 with
every special character escaped and the section headings in bold, split at paragraph, line or
word boundaries so that no message is longer than 4096 characters (`--limit` to try a smaller
one). It reads stdin by default, so `f1brief digest | f1brief format` works too.

`send` formats a digest text the same way and sends each message to the chat through the
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
and the summary reads it back. The prompt is `src/f1brief/summary_prompt.md`, with the same
rules as the digest's. `summary` writes one on any day without sending it (`--prompt-only`
for the prompt); it reads the archive but adds nothing to it.

During a race weekend `run` also sends a short message after qualifying: the favourites'
positions, the comparison with their teammates and what that means for the race. It is due
from two hours after the qualifying start until the race starts, once per round (`forget
--qualifying` allows another send), with the same claim and partial rules as the digest. While
Jolpica has not published the classification yet, the run sends nothing and the next one tries
again. The facts are the qualifying classification, the favourite drivers' recent form (grid
against finish) and the news since FP1; Jolpica has no starting grid before the race, so grid
penalties come only from the news. The prompt is `src/f1brief/qualifying_prompt.md`. `quali`
writes the message for any round without sending it (`--round`, `--season`; `--driver` and
`--team` to try other favourites; `--prompt-only` for the prompt).

Hot stories (storylines Misha can follow, see CLAUDE.md) are kept in `state.db` too, in the
tables of `src/f1brief/stories.py`: each story with the drivers, teams and people it is about,
its timeline of updates (every one with the URL of its source) and whether it is followed or
muted. `detect-stories` fills them (F22): it takes the hot news items of the last 7 days
(`--days`), those about a favourite or an entity of a followed story that touch contracts,
seats, transfers, team principals, regulations or penalties, and sends the ones it has not
checked before, with the open stories, to `claude -p` (prompt: `src/f1brief/stories_prompt.md`).
For each item claude attaches it to a story, starts a new one or ignores it. The source of every
story and update is the item's own URL, headline and outlet, never claude's; a decision naming
no item sent is rejected. Answered items are not sent again. `--dry-run` shows the decisions
and stores nothing, `--prompt-only` prints the prompt without running claude.

Story alerts (F24): when a followed story gets an update (recorded after it was followed),
`f1brief alerts` sends a short message with what changed, the source and an "Отписаться" button;
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

`f1brief bot` (F23) is the Telegram bot for the stories: a long-polling listener that runs until
stopped (meant for its own launchd job with `KeepAlive`). It answers only the chat in
`TELEGRAM_CHAT_ID`; updates from any other chat get no answer. Commands (Russian UI):
`/stories` (followed and recent), `/story <id>` (timeline with sources), `/follow <id>`,
`/unfollow <id>`, `/mute <id> <days>`, `/archive` (all, resolved included). Story messages
carry "Отписаться" (one tap) or "Следить" buttons. Once a story is followed, the bot offers
"Следить за <names>" for its drivers and teams that are not favourites yet; pressing it appends
them to `config.toml` (people without a Jolpica id are skipped). Only one listener per bot can
poll: a second one, or a webhook on the bot, stops it with an error.

```bash
uv run f1brief bot --dry-run --updates tests/fixtures/telegram/updates.json --chat-id 424242
uv run f1brief bot                         # the listener (Ctrl-C to stop)
```

`--dry-run` answers saved updates (a `getUpdates` answer or a JSON list) and prints the replies
and buttons: nothing is sent, the story changes are rolled back and `config.toml` is not
written. It needs no token; the chat to accept is `--chat-id` or `TELEGRAM_CHAT_ID`.

`python -m f1brief` works too, from inside the virtual environment.

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
```

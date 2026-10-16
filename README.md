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

## Run

```bash
uv run f1brief --help
uv run f1brief next              # next race weekend, session times in Europe/Warsaw
uv run f1brief next --season 2027
uv run f1brief standings         # standings + changes since the previous round
uv run f1brief form --last 5     # McLaren race/qualifying form over the last 5 rounds
uv run f1brief drivers           # Russell and Ocon form vs their teammates
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
muted. Nothing fills them yet; detection, the bot and alerts come next (F22–F24).

`python -m f1brief` works too, from inside the virtual environment.

## Checks

These must all pass before a PR is opened:

```bash
uv run ruff check
uv run ruff format --check
uv run pytest
```

## Layout

```
src/f1brief/   application package
tests/         pytest suite (no network: recorded fixtures only)
tests/fixtures/  recorded API responses (e.g. jolpica/schedule_2026.json)
```

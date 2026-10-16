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

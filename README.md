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
```

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

# fixtures/sources/

Recorded sample responses from the free, no-auth external APIs that
engine/sources/ reads: Sleeper, ESPN's public endpoints, and Open-Meteo.

These files are inputs to mocked tests only. Nothing in the test suite
makes a real network call; a test loads one of these files and feeds its
contents to a patched urlopen instead.

This directory is separate from fixtures/sample_league/, which is a frozen
fixture owned by engine/fixtures.py. Nothing here may be added to
fixtures/sample_league/, and that fixture's schema is not touched by
anything in this directory.

Each recorded response is trimmed down to a handful of representative
records, not a full capture of the real endpoint's response.

File names are owned one per source module (for example a Sleeper players
sample belongs to engine/sources/sleeper.py, a schedule sample to
engine/sources/schedule.py, and so on).

Some of these files have a top level JSON list rather than a JSON object,
matching what the real endpoint returns. Load them with json.loads, not
engine.common.load_json, since load_json requires a top level object and
would raise on a list.

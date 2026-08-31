# fixtures/phase4/

Offline fixture data for Phase 4 (gameday and inactive-player alerting) and
for the run wrappers that must work fully in `--fixtures --dry-run` mode
with zero network access. This directory holds two kinds of thing:

- `schedule.json`, a hand-built NFL schedule in the same shape
  `engine.sources.schedule.fetch_week_schedule` puts inside its envelope's
  `"data"` key, read through `engine.timing.load_fixture_schedule`.
- A set of sample draft email bodies used to exercise the wrappers that
  turn a run's JSON into a sent email. Those files are owned by a different
  chunk of this phase's build, so they are not enumerated here; look for
  them alongside this README once that chunk has landed.

## Frozen directories: do not add to these

`fixtures/sample_league/`, `fixtures/sources/`, and `fixtures/yahoo/` are
frozen fixture sets from earlier phases. Every module built before Phase 4
reads exactly the files and keys those directories already contain, so
nothing new is ever added there, no matter how convenient it would be to
drop a Phase 4 file alongside them. New Phase 4 fixture data lives here,
in its own directory, instead.

## Why schedule.json's kickoffs are exactly what they are

`schedule.json` uses fictional NFL team codes (ZPH, QRN, VXA, NVR, OKS, TDN,
WLK, BRV, STG, FLX, GLE, MTN) so it never collides with a real week's real
matchups. The six games and their kickoff times were chosen to make the
plan's verification commands produce a specific, checkable outcome:

- No game kicks off on 2026-09-13, so a gameday run for that date has
  nothing to do and must report `STATUS skipped no-games` rather than
  send anything.
- Five games kick off on 2026-09-14 (13:00Z, 13:00Z, 16:05Z, 17:25Z,
  20:20Z), so a gameday run for that date has real work to do and must
  produce a full lineup email.
- The fixture league's team `t1` starts nine players, whose NFL teams are
  ZPH, QRN, VXA, NVR, OKS, TDN, WLK, BRV and STG; the earliest of those
  teams' kickoffs is 2026-09-14T13:00:00Z.
- Of `t1`'s three bench-only teams (FLX, GLE, MTN), only GLE and MTN have
  no game on 2026-09-14: their game is 2026-09-15T00:15:00Z, the next UTC
  day, which is what makes `games_on_date` return five games for
  2026-09-14 rather than six. FLX does play on 2026-09-14 (20:20Z), and
  that game still appears in a starter-filtered list for `t1`, because its
  opponent STG is one of `t1`'s starter teams: filtering to starter teams
  filters games, not teams, so a bench-only team can legitimately show up
  inside a returned game. The earliest starter kickoff is 13:00:00Z
  either way.
- 2026-09-14T11:45:00Z sits exactly 75 minutes before the 13:00Z kickoff,
  the default inactive-alert window
  (`engine.timing.DEFAULT_INACTIVE_WINDOW_MINUTES`), so an inactive-player
  run at that instant falls exactly on the window's inclusive boundary and
  must still fire; a run any earlier must stay silent.

# fixtures/yahoo/

Recorded sample Yahoo Fantasy Sports API responses, in the shape
engine/yahoo_client.py actually sees them. That is: after yfpy 17.0.0
parses a raw Yahoo response into its own model objects, and those model
objects are converted back to plain JSON with
`json.loads(yfpy.utils.jsonify_data(obj))`.

## How these were made

These values were derived from yfpy 17.0.0's own model definitions
(models.py) and from its query.py docstring examples, not probed against
a live Yahoo account. Yahoo Fantasy Sports API access for this project
was still pending review when this directory was written, so no live
capture was possible. Every value here should be re-checked against a
real response the first time access works.

## Serialized shape versus raw Yahoo shape

yfpy's serialization is not identical to the raw JSON Yahoo sends. Two
differences matter here:

- List-valued attributes keep their single-key wrapper dicts through
  serialization. A Settings object's roster_positions serializes as a
  list of `{"roster_position": {...}}` entries, its stat lists as
  `{"stats": [{"stat": {...}}, ...]}`, a Roster's players as a list of
  `{"player": {...}}` entries, and a Matchup's teams as a list of
  `{"team": {...}}` entries. All of that survives here.
- Derived attributes such as Player.full_name and Player.bye do not
  survive serialization, because they are not raw Yahoo keys. Read
  `name.full` and `bye_weeks.week` instead; they are what is actually
  present in these files.

## eligible_positions: a deliberate exception

yfpy's Player.__init__ normalizes eligible_positions into a plain list
of strings before anything is serialized. A genuinely serialized Player
object always shows a list of strings, for example `["WR"]`, never a
bare string and never a `{"position": ...}` dict.

Two records in league_players.json break that rule on purpose. Record 4
(Amon-Ra St. Brown) carries eligible_positions as the raw wrapped dict
form `{"position": "WR"}`, and record 5 (Trey McBride) carries it as the
raw bare string form `"TE"`. Both are forms yfpy would normalize away
before you could ever see them in real serialized output. They are kept
here only so the parser in engine/yahoo_shapes.py is proven to handle a
raw payload or a future yfpy change, not because Yahoo or yfpy would ever
hand you a Player in that shape today. Do not read those two records as
examples of serialized output; every other player record in this
directory is.

## File by file

- league_metadata.json: a serialized League object. Top level is a JSON
  object.
- league_settings.json: a serialized Settings object, including
  roster_positions, stat_categories, and stat_modifiers. Top level is a
  JSON object.
- league_players.json: a list of 6 serialized Player objects, the shape
  `get_league_players` returns after conversion. Top level is a JSON
  list.
- free_agents.json: a list of 2 serialized Player objects, each also
  carrying ownership and percent_owned. Top level is a JSON list.
- team_roster_week.json: a serialized Roster object for one team in one
  week, with each player carrying a selected_position. Top level is a
  JSON object.
- league_matchups_week.json: a list of 2 serialized Matchup objects,
  covering two head-to-head pairings in the same week. Top level is a
  JSON list.

Load the list-topped files (league_players.json, free_agents.json,
league_matchups_week.json) with `json.loads` on the file text, not
`engine.common.load_json`, because `load_json` requires a top level JSON
object and would raise on a list.

## Boundaries

This directory is separate from fixtures/sample_league/, a frozen
fixture owned by engine/fixtures.py, and from fixtures/sources/, Phase
2's Sleeper, ESPN, and Open-Meteo samples. Nothing in this directory may
be added to either of those, and nothing in either of those belongs
here.

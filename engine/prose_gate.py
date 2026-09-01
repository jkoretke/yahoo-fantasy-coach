"""Validate a Claude drafted email body against the run's own brief JSON.

docs/plan.md's "determinism split" is enforced here: a Claude drafted
sentence is allowed into the outgoing email only when every player it
names is a player the brief actually knows about, and every start/sit or
claim/skip verdict it states agrees with the brief's own verdict for that
player. The one narrow exception is a flagged toss up (a lineup change or
waiver target the brief itself marked "toss_up": true): for those, and
only those, the draft may pick either side.

The name check is deliberately STRICT and simple, on purpose. The wrapper
that calls this gate always has a plain, fully deterministic fallback
email ready (rendered straight from the brief JSON, no prose at all), so a
false positive here only costs prose, never correctness. A player name is
only recognized when it appears as a run of two or more consecutive
capitalized words; a lone capitalized word is never treated as a player
name, and never raises a violation either. One consequence of the exact
token pattern this gate is built on: a trailing period attaches to its
own word rather than ending the match, so a sentence that ends on a
capitalized word (a name, an allowed word, a team name) immediately
followed by a sentence that starts with one merges into a single run
spanning both. A generated draft should end sentences on a lowercase
word to stay clear of this; every sentence in this repo's own fixture
drafts does exactly that.

The verdict check is equally conservative: it reads one sentence at a
time (split on . ! ? or a newline), and only asserts a verdict for a
sentence that contains a start/bench (or claim/skip) word from exactly
one of the two opposing word sets. A sentence naming both, or neither,
asserts nothing about any player named in it, because there is no way to
tell which word belongs to which player from word presence alone. The
one carve-out to that rule is a comparative phrase ("start X over Y",
"sit X instead of Y"): the single most natural English for a start/sit
call, and the exact form engine.email_render's own plain-text fallback
uses, so it is split at the comparative and each side is checked against
its own verdict rather than the sentence's one trigger word being
asserted for both players in it. Which side gets which verdict mirrors
the sentence's own trigger word ("start" puts the started player first,
"sit"/"bench" puts the benched player first), not a fixed before/after
assignment, since hardcoding the direction would silently accept a
"sit X instead of Y" draft that inverts the brief's actual call.

check_draft never raises: a malformed or missing section of brief is
read defensively throughout, and any draft string, including an empty
one or arbitrary decoded bytes, is valid input.

Public names: START_WORDS, BENCH_WORDS, CLAIM_WORDS, SKIP_WORDS,
ALLOWED_CAPITALIZED_WORDS, brief_player_names, brief_verdicts,
toss_up_player_ids, check_draft, format_violations.
"""
from __future__ import annotations

import re
from typing import Any

# Words that assert a start verdict for every brief player named in the
# same sentence, provided no BENCH_WORDS word also appears in it.
START_WORDS: frozenset[str] = frozenset({"start", "starting", "starts"})

# Words that assert a bench verdict for every brief player named in the
# same sentence, provided no START_WORDS word also appears in it.
BENCH_WORDS: frozenset[str] = frozenset(
    {"bench", "benching", "benches", "sit", "sitting", "sits"}
)

# Words that assert a waiver claim verdict for every waiver target named
# in the same sentence, provided no SKIP_WORDS word also appears in it.
CLAIM_WORDS: frozenset[str] = frozenset({"claim", "claiming", "claims"})

# Words that assert a waiver skip verdict for every waiver target named
# in the same sentence, provided no CLAIM_WORDS word also appears in it.
# "pass on" is a two word phrase, matched as a phrase, not a single token.
SKIP_WORDS: frozenset[str] = frozenset({"skip", "skipping", "pass on"})

_WEEKDAYS = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)

_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)

# The fixed set of capitalized words that are trimmed off each end of a
# matched capitalized run before it is judged an unknown player name. Per
# call, check_draft unions this with every roster slot name found in the
# brief's assignments and every individual word of the brief's team and
# manager names, since neither of those varies by brief the way this fixed
# set does not vary at all.
#
# Both case variants of a word actually emitted in text this gate checks
# are listed separately (exact string match, not case-insensitive): Start
# and START both appear here because engine.email_render's own start/sit
# line reads "START <name> over <name>" in full caps, exactly like STATUS
# already did for the line this gate itself strips.
ALLOWED_CAPITALIZED_WORDS: frozenset[str] = frozenset(
    _WEEKDAYS
    + _MONTHS
    + (
        "Week",
        "Start",
        "START",
        "Sit",
        "Bench",
        "BENCH",
        "Team",
        "League",
        "Waiver",
        "Claim",
        "Skip",
        "Drop",
        "Add",
        "Swap",
        "Consider",
        "Trade",
        "Send",
        "Receive",
        "Total",
        "Projected",
        "Recommended",
        "Points",
        "Bye",
        "Out",
        "Questionable",
        "Doubtful",
        "NFL",
        "Yahoo",
        "Sleeper",
        "ESPN",
        "FAAB",
        "STATUS",
    )
)

# A maximal run of two or more consecutive capitalized tokens. Written
# here with single backslashes, exactly as it must appear in a raw
# Python string.
_CAPITALIZED_RUN_RE = re.compile(r"\b[A-Z][A-Za-z'.-]*(?:\s+[A-Z][A-Za-z'.-]*)+\b")

# Sentences are split on runs of these three punctuation marks or a
# newline; a semicolon or a comma does not end a sentence for this gate.
_SENTENCE_SPLIT_RE = re.compile(r"[.!?\n]+")


def brief_player_names(brief: dict[str, Any]) -> dict[str, str]:
    """Return every player name in brief, lowercased, mapped to its player_id.

    Reads exactly these sources: optimal_lineup and current_lineup
    assignments (an unfilled unit has both player_id and name as None and
    is skipped), lineup_changes (both the start and sit side of each
    entry), waivers.targets (both the target player and the player it
    would drop), matchup.team and matchup.opponent assignments, every
    named side of matchup.slot_edges, and, when present, trades.ideas
    (both the send and the receive side of each idea). Any of these
    sections may be absent from brief; a missing section simply
    contributes no names.

    trades is not part of engine.brief.build_brief's own shape (a trade
    partner's roster reaches across every team in the league, which
    nothing else in brief ever names): engine.run_common.compose_email
    attaches it under brief["trades"], in engine.trades.trade_ideas's own
    return shape, only when a caller actually supplied trades data, so
    that a drafted mention of a trade partner's player is recognized
    rather than rejected as an unknown name.
    """
    names: dict[str, str] = {}

    def _add(player_id: Any, name: Any) -> None:
        if player_id is None or name is None:
            return
        names[str(name).lower()] = player_id

    for lineup_key in ("optimal_lineup", "current_lineup"):
        lineup = brief.get(lineup_key) or {}
        for assignment in lineup.get("assignments") or []:
            _add(assignment.get("player_id"), assignment.get("name"))

    for change in brief.get("lineup_changes") or []:
        _add(change.get("start_player_id"), change.get("start_name"))
        _add(change.get("sit_player_id"), change.get("sit_name"))

    waivers = brief.get("waivers") or {}
    for target in waivers.get("targets") or []:
        _add(target.get("player_id"), target.get("name"))
        _add(target.get("drop_player_id"), target.get("drop_player_name"))

    matchup = brief.get("matchup") or {}
    for side_key in ("team", "opponent"):
        side = matchup.get(side_key) or {}
        for assignment in side.get("assignments") or []:
            _add(assignment.get("player_id"), assignment.get("name"))

    for edge in matchup.get("slot_edges") or []:
        _add(edge.get("team_player_id"), edge.get("team_name"))
        _add(edge.get("opponent_player_id"), edge.get("opponent_name"))

    trades = brief.get("trades") or {}
    for idea in trades.get("ideas") or []:
        send = idea.get("send") or {}
        receive = idea.get("receive") or {}
        _add(send.get("player_id"), send.get("name"))
        _add(receive.get("player_id"), receive.get("name"))

    return names


def brief_verdicts(brief: dict[str, Any]) -> dict[str, str]:
    """Return player_id -> "start" or "bench" for every player the brief has an opinion on.

    Seeded from optimal_lineup's starter_ids and bench_ids, then
    overridden by lineup_changes (start_player_id -> start, sit_player_id
    -> bench), so a changed player's verdict always reflects the change
    rather than the pre-change optimal placement. A player named
    elsewhere in the brief but absent from optimal_lineup entirely (an
    opponent's player, for example) never appears in the returned dict,
    which is deliberate: such a player can never produce a
    verdict-conflict.
    """
    verdicts: dict[str, str] = {}

    optimal = brief.get("optimal_lineup") or {}
    for player_id in optimal.get("starter_ids") or []:
        verdicts[player_id] = "start"
    for player_id in optimal.get("bench_ids") or []:
        verdicts[player_id] = "bench"

    for change in brief.get("lineup_changes") or []:
        start_id = change.get("start_player_id")
        if start_id is not None:
            verdicts[start_id] = "start"
        sit_id = change.get("sit_player_id")
        if sit_id is not None:
            verdicts[sit_id] = "bench"

    return verdicts


def toss_up_player_ids(brief: dict[str, Any]) -> set[str]:
    """Return every player_id named inside a flagged lineup toss up.

    Only lineup_changes entries are read here (engine.lineup's toss up
    shape, "toss_up_options": [{"player_id", "name"}, ...]); a waiver
    toss up carries no player_id at all in its own toss_up_options
    (["claim", "skip"]) and is handled separately inside check_draft.
    """
    ids: set[str] = set()
    for change in brief.get("lineup_changes") or []:
        if not change.get("toss_up"):
            continue
        for option in change.get("toss_up_options") or []:
            if not isinstance(option, dict):
                continue
            player_id = option.get("player_id")
            if player_id is not None:
                ids.add(player_id)
    return ids


def _waiver_targets_by_id(brief: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return player_id -> its own waivers.targets entry, for lookup by id."""
    targets: dict[str, dict[str, Any]] = {}
    waivers = brief.get("waivers") or {}
    for target in waivers.get("targets") or []:
        player_id = target.get("player_id")
        if player_id is not None:
            targets[player_id] = target
    return targets


def _allowed_words_for_brief(brief: dict[str, Any]) -> set[str]:
    """Return ALLOWED_CAPITALIZED_WORDS unioned with this brief's own words.

    Adds every roster slot name found in the brief's assignments (both
    lineups and both sides of the matchup), plus every individual word of
    the team name and manager name (brief.team.name, brief.team.manager,
    matchup.team.team_name, matchup.opponent.team_name), so a team name
    like "Sample Squad Two" is never mistaken for an unknown player.
    """
    words: set[str] = set(ALLOWED_CAPITALIZED_WORDS)

    for lineup_key in ("optimal_lineup", "current_lineup"):
        lineup = brief.get(lineup_key) or {}
        for assignment in lineup.get("assignments") or []:
            slot = assignment.get("slot")
            if slot:
                words.add(slot)

    matchup = brief.get("matchup") or {}
    for side_key in ("team", "opponent"):
        side = matchup.get(side_key) or {}
        for assignment in side.get("assignments") or []:
            slot = assignment.get("slot")
            if slot:
                words.add(slot)
        team_name = side.get("team_name")
        if team_name:
            words.update(str(team_name).split())

    team = brief.get("team") or {}
    for key in ("name", "manager"):
        value = team.get(key)
        if value:
            words.update(str(value).split())

    return words


def _extract_names(
    text: str, player_names: dict[str, str], allowed_words: set[str]
) -> tuple[list[str], list[str]]:
    """Scan text for capitalized runs, trim allowed words, and classify each.

    Returns (known_player_ids, unknown_texts): known_player_ids in the
    order matched (one entry per matched run that resolves to a brief
    player, duplicates included), unknown_texts holding the offending,
    still two-or-more-token text of every run that does not.
    """
    known_ids: list[str] = []
    unknown_texts: list[str] = []

    for match in _CAPITALIZED_RUN_RE.finditer(text):
        tokens = match.group(0).split()
        start = 0
        end = len(tokens)
        while start < end and tokens[start] in allowed_words:
            start += 1
        while end > start and tokens[end - 1] in allowed_words:
            end -= 1
        trimmed = tokens[start:end]
        if len(trimmed) < 2:
            continue
        candidate = " ".join(trimmed)
        player_id = player_names.get(candidate.lower())
        if player_id is not None:
            known_ids.append(player_id)
        else:
            unknown_texts.append(candidate)

    return known_ids, unknown_texts


def _contains_any_word(lower_text: str, words: frozenset[str]) -> bool:
    """True when any word or phrase in words appears in lower_text on a word boundary."""
    return any(re.search(r"\b" + re.escape(word) + r"\b", lower_text) for word in words)


def _lineup_sentence_verdict(lower_sentence: str) -> str | None:
    has_start = _contains_any_word(lower_sentence, START_WORDS)
    has_bench = _contains_any_word(lower_sentence, BENCH_WORDS)
    if has_start and not has_bench:
        return "start"
    if has_bench and not has_start:
        return "bench"
    return None


def _waiver_sentence_verdict(lower_sentence: str) -> str | None:
    has_claim = _contains_any_word(lower_sentence, CLAIM_WORDS)
    has_skip = _contains_any_word(lower_sentence, SKIP_WORDS)
    if has_claim and not has_skip:
        return "claim"
    if has_skip and not has_claim:
        return "skip"
    return None


# A comparative phrase splitting one start/sit sentence into its two
# halves, e.g. "START Trace Winslow over Brix Duskin": the most natural
# English for a start/sit call, and the exact form engine.email_render's
# own format_changes uses, so the gate must read it correctly rather than
# reject its own deterministic fallback email.
_COMPARATIVE_RE = re.compile(r"\b(?:over|instead of|ahead of|rather than)\b", re.IGNORECASE)


def _split_on_comparative(sentence: str) -> tuple[str, str] | None:
    """Split sentence at its first comparative phrase, or return None.

    Returns (before, after) with the comparative phrase itself excluded
    from both halves, so a player named in each half can be extracted
    independently. Only the first comparative phrase in the sentence is
    used; a sentence naming three or more players across two comparatives
    is not this gate's concern (a generated draft is expected to make one
    start/sit call per sentence, per the docstring above).
    """
    match = _COMPARATIVE_RE.search(sentence)
    if match is None:
        return None
    return sentence[: match.start()], sentence[match.end() :]


def check_draft(draft: str, brief: dict[str, Any]) -> dict[str, Any]:
    """Validate draft (a Claude drafted email body) against brief.

    Returns {"ok": bool, "violations": [{"kind", "detail"}, ...],
    "named_players": [player_id, ...]}. ok is True exactly when
    violations is empty. named_players lists, in first-seen order with no
    duplicates, every player_id the draft names anywhere that the brief
    also recognizes.

    Two independent passes:

    1. Unknown player names: every maximal run of two or more consecutive
       capitalized tokens in the whole draft, after trimming allowed
       words from each end, that is still two or more tokens and whose
       lowercase form is not a key of brief_player_names(brief), is an
       "unknown-player" violation.

    2. Verdicts: the draft is split into sentences on [.!?\\n]+. A
       sentence that contains a start/bench (or claim/skip) word from
       exactly one of the two opposing sets asserts that verdict for
       every brief player named in that sentence; a sentence with both or
       neither asserts nothing, since word presence alone cannot say
       which word belongs to which player. The one exception is a
       start/bench sentence containing a comparative phrase ("over",
       "instead of", "ahead of", "rather than", see
       _split_on_comparative): "START X over Y" and "SIT X instead of Y"
       both name two players under one trigger word, so the two halves
       are checked against opposite verdicts instead of asserting the
       sentence's single trigger word for both. Which half gets which
       verdict mirrors the sentence's own trigger word rather than a
       fixed before/after assignment: a "start"-triggered sentence checks
       before against "start" and after against "bench" (the played
       player named first), while a "bench"-triggered sentence checks
       before against "bench" and after against "start" (the benched
       player named first). An asserted start/bench verdict that
       disagrees with brief_verdicts(brief) is a "verdict-conflict"
       violation, unless that player_id is in toss_up_player_ids(brief).
       An asserted claim/skip verdict for a waiver target that disagrees
       with that target's own "verdict" is an "unknown-waiver-verdict"
       violation, unless that target itself carries "toss_up": true.

    Never raises: every brief section is read with .get(...) fallbacks,
    and any draft string, including empty or non-sentence garbage text,
    is valid input.
    """
    if not isinstance(draft, str):
        draft = str(draft)

    player_names = brief_player_names(brief)
    verdicts = brief_verdicts(brief)
    toss_ups = toss_up_player_ids(brief)
    waiver_targets = _waiver_targets_by_id(brief)
    allowed_words = _allowed_words_for_brief(brief)

    violations: list[dict[str, str]] = []
    named_players: list[str] = []
    seen_named: set[str] = set()

    known_ids, unknown_texts = _extract_names(draft, player_names, allowed_words)
    for player_id in known_ids:
        if player_id not in seen_named:
            seen_named.add(player_id)
            named_players.append(player_id)
    for text in unknown_texts:
        violations.append({"kind": "unknown-player", "detail": f"unrecognized player name: {text!r}"})

    for sentence in _SENTENCE_SPLIT_RE.split(draft):
        if not sentence.strip():
            continue
        lower_sentence = sentence.lower()
        sentence_ids, _ = _extract_names(sentence, player_names, allowed_words)

        lineup_verdict = _lineup_sentence_verdict(lower_sentence)
        if lineup_verdict is not None:
            comparative = _split_on_comparative(sentence)
            if comparative is not None:
                # "START Trace Winslow over Brix Duskin": one sentence,
                # two players, one start word. Asserting lineup_verdict
                # for every name in the whole sentence would wrongly
                # accuse the benched side of a start verdict, so each
                # half of the comparative is checked against its own
                # side's verdict instead of the sentence's single
                # trigger word.
                before_text, after_text = comparative
                before_ids, _ = _extract_names(before_text, player_names, allowed_words)
                after_ids, _ = _extract_names(after_text, player_names, allowed_words)
                # Mirror off the sentence's OWN trigger word rather than
                # hardcoding before=start/after=bench: "START X over Y"
                # (lineup_verdict "start") puts the started player first,
                # but "SIT X instead of Y" (lineup_verdict "bench") puts
                # the benched player first, and hardcoding the direction
                # would silently accept a draft that inverts the brief's
                # actual call in that second form.
                if lineup_verdict == "start":
                    sides = ((before_ids, "start"), (after_ids, "bench"))
                else:
                    sides = ((before_ids, "bench"), (after_ids, "start"))
            else:
                sides = ((sentence_ids, lineup_verdict),)

            for ids, side_verdict in sides:
                for player_id in ids:
                    asserted_against = verdicts.get(player_id)
                    if (
                        asserted_against is not None
                        and asserted_against != side_verdict
                        and player_id not in toss_ups
                    ):
                        violations.append(
                            {
                                "kind": "verdict-conflict",
                                "detail": (
                                    f"draft says {side_verdict} for {player_id} "
                                    f"but the brief says {asserted_against}"
                                ),
                            }
                        )

        waiver_verdict = _waiver_sentence_verdict(lower_sentence)
        if waiver_verdict is not None:
            for player_id in sentence_ids:
                target = waiver_targets.get(player_id)
                if target is None:
                    continue
                # A flagged waiver toss up carries "toss_up_options":
                # ["claim", "skip"], not a player_id shape at all (unlike
                # a lineup toss up), so the carve-out here is decided by
                # the flag alone; either word is allowed for this player.
                if target.get("toss_up"):
                    continue
                brief_verdict = target.get("verdict")
                if brief_verdict is not None and brief_verdict != waiver_verdict:
                    violations.append(
                        {
                            "kind": "unknown-waiver-verdict",
                            "detail": (
                                f"draft says {waiver_verdict} for {player_id} "
                                f"but the brief says {brief_verdict}"
                            ),
                        }
                    )

    return {
        "ok": not violations,
        "violations": violations,
        "named_players": named_players,
    }


def format_violations(result: dict[str, Any]) -> str:
    """Return one short line per violation in result, for stderr logging."""
    lines = []
    for violation in result.get("violations") or []:
        lines.append(f"{violation.get('kind')}: {violation.get('detail')}")
    return "\n".join(lines)

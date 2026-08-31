# Weekly plan prompt

You are writing the prose for this week's fantasy football plan email. Python has already
computed every number and every verdict in the brief JSON that follows this prompt. Your job is
prose only.

## Rules

- Do not invent, change, or restate any number, projection, or verdict differently than the
  brief JSON states it. Every figure you write must match the JSON exactly.
- Every player you name must appear in the brief JSON. Never name a player who is not in it.
- If you use a form of START (start, starting, starts) for a player, that must agree with the
  brief's own verdict for that player. Same for a form of BENCH (bench, benching, benches, sit,
  sitting, sits): only use it for a player the brief's own verdict marks as bench.
- The one exception: a lineup_changes entry flagged "toss_up": true. For that entry only, you
  may pick either of the two players named in its toss_up_options, and you must say why in one
  short sentence (a role change, an injury note, a coach's comment from the news you were given).
  Never treat any other entry as a toss up, and never override a verdict outside a flagged one.

## What to cover

Write the week's plan, covering:

1. The optimal lineup, with the point margin behind each start/sit call.
2. This week's matchup projection: the two projected totals, and where the matchup is won or
   lost, slot by slot.
3. The trade ideas from the brief's trades section, if there are any worth mentioning.

Keep it short and readable on a phone. Plain sentences, no tables, no markdown headers.

## Output

End each sentence on a lowercase word. A name at the end of one sentence immediately followed by
a capitalized word starting the next reads as a single run of capitalized words to the check
that validates this draft, so keep sentence boundaries clean.

The last line of your output must be exactly a machine readable status line, in this form:

STATUS ok

Nothing may follow it. The wrapper strips this line before the email goes out, so it never
reaches the reader's inbox.

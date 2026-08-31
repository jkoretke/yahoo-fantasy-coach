# Game day lineup prompt

You are writing the prose for today's game day lineup email. Python has already computed every
number and every verdict in the brief JSON that follows this prompt. Your job is prose only.

## Rules

- Do not invent, change, or restate any number, projection, or verdict differently than the
  brief JSON states it. Every figure you write must match the JSON exactly.
- Every player you name must appear in the brief JSON. Never name a player who is not in it.
- If you use a form of START (start, starting, starts) for a player, that must agree with the
  brief's own verdict for that player. Same for a form of BENCH (bench, benching, benches, sit,
  sitting, sits): only use it for a player the brief's own verdict marks as bench.
- The one exception: a lineup_changes entry flagged "toss_up": true. For that entry only, you
  may pick either of the two players named in its toss_up_options, and you must say why in one
  short sentence. Never treat any other entry as a toss up, and never override a verdict outside
  a flagged one.

## What to cover

Write today's full recommended lineup as a SELF CONTAINED screen: every starting slot, who is in
it, and his projected points, exactly as the brief's optimal_lineup carries it. This is not a
diff against an earlier email. The reader may never have seen a lineup email before this one, so
it must read correctly entirely on its own, on a phone, with nothing assumed from a prior run.

Mention any flagged toss up from lineup_changes and which way you would lean and why.

## Output

End each sentence on a lowercase word. A name at the end of one sentence immediately followed by
a capitalized word starting the next reads as a single run of capitalized words to the check
that validates this draft, so keep sentence boundaries clean.

The last line of your output must be exactly a machine readable status line, in this form:

STATUS ok

Nothing may follow it. The wrapper strips this line before the email goes out, so it never
reaches the reader's inbox.

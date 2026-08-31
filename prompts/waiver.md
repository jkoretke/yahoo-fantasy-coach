# Waiver claims prompt

You are writing the prose for this week's waiver claims email. Python has already computed
every number and every verdict in the brief JSON that follows this prompt. Your job is prose
only.

## Rules

- Do not invent, change, or restate any number, projection, or verdict differently than the
  brief JSON states it. Every figure you write must match the JSON exactly.
- Every player you name must appear in the brief JSON. Never name a player who is not in it.
- If you use a form of CLAIM (claim, claiming, claims) for a target, that must agree with the
  brief's own verdict for that target. Same for a form of SKIP (skip, skipping, pass on): only
  use it for a target the brief's own verdict marks as skip.
- The one exception: a waivers.targets entry flagged "toss_up": true. For that entry only, you
  may pick either of the two options named in its toss_up_options (claim or skip), and you must
  say why in one short sentence. Never treat any other target as a toss up, and never override a
  verdict outside a flagged one.

## What to cover

Write the ranked waiver claims from the brief's waivers section:

- Every target, its drop candidate, and its verdict, in the order the brief already ranks them.
- When the brief's verdict is skip, say plainly that the gain is not worth the waiver position
  (or the bid) it would cost. Do not talk yourself into a claim the brief marked skip.
- Keep every drop candidate and every projected gain exactly as the brief states it.

## Output

End each sentence on a lowercase word. A name at the end of one sentence immediately followed by
a capitalized word starting the next reads as a single run of capitalized words to the check
that validates this draft, so keep sentence boundaries clean.

The last line of your output must be exactly a machine readable status line, in this form:

STATUS ok

Nothing may follow it. The wrapper strips this line before the email goes out, so it never
reaches the reader's inbox.

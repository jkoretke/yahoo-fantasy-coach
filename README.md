# yahoo-fantasy-coach

Reads your Yahoo Fantasy Football league (read-only) and computes weekly lineup, matchup, and
waiver recommendations. See `docs/plan.md` for the full design. This README currently covers
only the one-time Yahoo setup every user needs; the rest (install, running it, deploying it) is
written in Phase 5.

## Yahoo API setup (do this first, one time)

Yahoo's Fantasy Sports API requires two separate steps: creating a developer app, then applying
for Fantasy Sports data access on top of it. As of 2026-08-30, Yahoo no longer offers a "Fantasy
Sports" checkbox during app creation. This is a real, two-step process now, not a formality.

### 1. Create a Yahoo developer app

1. Go to [developer.yahoo.com/apps/create](https://developer.yahoo.com/apps/create/) and sign in
   with the Yahoo account tied to your fantasy league.
2. Fill out the form:
   - **Application Name**: anything you want, but it cannot contain the word "yahoo" (the form
     rejects it).
   - **Redirect URI(s)**: `https://localhost:8080`
   - **OAuth Client Type**: Confidential Client (this app needs a client secret, unlike a mobile
     or single-page app).
   - **API Permissions**: leave both checkboxes (OpenID Connect Permissions, TW Auction)
     unchecked. Neither applies here, and Fantasy Sports is not listed. That is expected, not a
     bug: Fantasy Sports access comes from the separate application in step 2 below.
3. Click **Create App**. You'll land on a page showing a **Client ID** and a **Client Secret**.
   You only need those two values. The **App ID** shown on the same page is a dashboard
   identifier and is not used anywhere in this project.
4. Save the two values somewhere only you can read:
   ```
   mkdir -p ~/.config/yahoo-fantasy-coach
   chmod 700 ~/.config/yahoo-fantasy-coach
   touch ~/.config/yahoo-fantasy-coach/secrets.env
   chmod 600 ~/.config/yahoo-fantasy-coach/secrets.env
   ```
   Then open `~/.config/yahoo-fantasy-coach/secrets.env` in a text editor and add:
   ```
   YAHOO_CLIENT_ID=<your Client ID>
   YAHOO_CLIENT_SECRET=<your Client Secret>
   ```
   Never commit this file or paste these values anywhere public.

### 2. Apply for Yahoo Fantasy Sports API access

Having a developer app is not enough on its own: every API call returns a 401 until Yahoo
approves your app for Fantasy Sports data specifically. This is reviewed by a person at Yahoo,
so there is no published turnaround time.

1. Go to [sports.yahoo.com/developer/access](https://sports.yahoo.com/developer/access/).
2. Fill out the application. A few fields worth knowing before you start:
   - **Business Title**: if you're doing this as an individual rather than through a company,
     something like "Independent Developer" is fine.
   - **Business Name & Address**: your own name and address is fine for an individual,
     personal-use application; you do not need to represent a company.
   - **Consumer-Facing Product or App Name**: a plain, human name for the project (for example
     "Yahoo Fantasy Coach"), not the repo's technical name.
   - **Website URL or App Store Details**: this field requires an actual, real URL. If you have
     nothing else live yet, your GitHub profile URL (`https://github.com/<your-username>`) is a
     real, honest answer.
   - **Client ID**: paste the Client ID from step 1. The form explicitly allows leaving this
     blank if you have not created a developer app yet, but if you already have, fill it in so
     the approval attaches to the right app.
   - **Describe Your Intended Use Case**: be specific and honest that this is read-only,
     personal or single-league use, computed by your own code, with any actual roster moves
     made by hand in the Yahoo app (the API grants no write access). Applications that are vague
     get closed without a response, per Yahoo's own note on the form.
   - **Expected Users**: pick the smallest option if this is just for your own league.
3. Submit. You'll see an on-page confirmation that the application was received. There is no
   further action on your end until Yahoo responds.

Nothing that touches Yahoo (`engine/yahoo_client.py` and anything built on it) can be used for
real until this application is approved. See `docs/plan.md`'s Phase 3 entry for what proceeds in
the meantime.

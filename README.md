# ES Agents lead finder

Finds small UK businesses from several free sources, merges them, pulls a real email from each business's own website, checks Companies House, and appends the new ones to the **Business Queue** tab of your outreach sheet. Runs on GitHub Actions every hour, every day.

## Setup (about 20 minutes)

1. **Repo.** Create a new GitHub repo and upload everything in this folder (keep the `.github/workflows` path).
2. **Google Sheet access.** In Google Cloud Console: create a project, enable the *Google Sheets API*, create a *service account*, and download its JSON key. Open your `ES_Agents_Outreach_Tracker` sheet and share it (Editor) with the service account's email address.
3. **Companies House key.** Register at the Companies House Developer Hub, create an application, and create a *REST* API key. Without this key every lead is marked `Review`, never `Pending`.
4. **Google Places key (optional).** Create an API key restricted to the Places API and set a billing budget/alert in Google Cloud. `GOOGLE_MAX_REQUESTS` (default 12 per run) caps how many searches each run makes.
5. **Repo secrets** (Settings > Secrets and variables > Actions):
   - `SHEET_ID` = the ID from your sheet's URL (the part between `/d/` and `/edit`)
   - `GOOGLE_SERVICE_ACCOUNT_JSON` = the full contents of the JSON key
   - `COMPANIES_HOUSE_API_KEY`
   - `GOOGLE_PLACES_API_KEY` (optional)
6. **Test.** Actions tab > *Lead finder* > *Run workflow* with *Dry run* ticked. Open the `lead-preview` artifact to see what it would have added (only uploaded while the repo is private, because it contains scraped emails). Then untick dry run for a real run.

## What it does with each business

- Only keeps businesses that have their own website (social pages and directories are ignored).
- **Emails are never guessed.** An address is used only if it is written on the business's own site (a `mailto:` link or visible text on the home page, or a contact/about page linked from it) or in its OpenStreetMap tags. Sites whose robots.txt disallows crawling are skipped.
- The address must be on the business's own domain, or a normal freemail address (list in `config.json`) written on the site. Agency, web-designer and directory addresses are rejected. The domain must have a real MX record. `noreply`, sentry, wixpress, example and image-filename lookalikes are dropped.
- **Suppression.** Any address or domain found anywhere in the `Outreach Tracker` or `Replies` tabs (contacted, bounced, opted out) is never added. Freemail domains are only suppressed address by address.
- Skips anything already in your `Outreach Tracker` or `Business Queue` tabs (by name, website and email).
- **Pending is strict.** Companies House must show an active private limited company, incorporated at least 2 years ago, filing micro-entity, small or total-exemption accounts, with a SIC code matching the trade (`sic` in `config.json`). Names containing plc, group, holdings, bank or council, and full/group/medium accounts, go to `Review - <reason>`, which your emailer task ignores (sole traders and partnerships need consent under PECR).
- Each lead records where its email was found (`Email Source`) and a short `Why` note.
- Adds missing columns (Website, Contact Email, Company Type, Source, Date Added, Email Source, Why) to the end of the Business Queue header. It never edits existing rows.

## Tuning

- `config.json`: trades and towns. Each run works through the next `COMBOS_PER_RUN` (trade, town) pairs and remembers its place in `state.json`.
- `MAX_NEW_PER_RUN` (40) and `MAX_SITES_PER_RUN` (120) cap volume and run time.

## Limits to know about

- OpenStreetMap coverage of small UK trades is patchy, and its public Overpass server asks commercial users to use their own or a paid server. Treat it as a bonus source, not the foundation.
- Google's terms restrict how long Places data can be stored. Check them before relying on that source long term.
- Cold email to sole traders/partnerships needs consent under PECR. Every email must clearly identify you and offer a way to opt out.

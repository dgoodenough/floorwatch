# FloorWatch

A live feed of the House and Senate floor in plain English. Every item is labeled as one of these:

- **REAL VOTE**: passage, confirmation, veto override, or agreeing to the other chamber's version
- **PROCEDURAL**: rules, the previous question, cloture, motions to table, recommit or waive
- **AMENDMENT**: a vote on changing the bill, not on the bill itself
- **ON THE FLOOR**: what they're debating right now
- **COMING UP**: postponed votes, cloture filings, what's scheduled next
- **ROUTINE**: prayer, the journal, quorum calls (hidden by default)

Bills show their plain-English names. Clicking one opens a page for that bill with its full history from Congress.gov (introduction, committee, floor votes, including the procedural rule votes that set it up), the CRS summary, links (Congress.gov, the bill text, GovTrack, CBO cost estimates, committee reports) and recent news headlines.

Each item has a one-line explanation, the official text, the vote tally with the R and D split, and a link to the roll call. A procedural vote on a House "rule" also names the bill it's actually about. The **Votes table** view has a sortable list of every recorded vote.

## Live site

The site is at **https://dgoodenough.github.io/floorwatch/**.

- `.github/workflows/update-feed.yml` runs every 5 minutes. Each run refreshes 4 times, a minute apart, and publishes `feed.json` to the `data` branch. GitHub sometimes delays scheduled runs, so the page is usually 1–5 minutes behind the floor.
- `.github/workflows/pages.yml` deploys `docs/index.html` to GitHub Pages. The page reads the newest `feed.json` from the `data` branch every 90 seconds.

## Run it locally instead

You need Python 3.9 or newer. There's nothing to install.

```bash
python3 floorwatch.py
# then open http://localhost:8765
```

Options:

| flag | what it does |
|---|---|
| `--host 0.0.0.0` | lets you open it from your phone on the same Wi-Fi at `http://<computer-ip>:8765` |
| `--every 60` | seconds between checks of the official feeds |
| `--days 3` | how many recent House legislative days to load into the votes table |
| `--date 2026-11-17` | replays a past day |

## Where the data comes from (all official, no keys needed)

- **House floor log**: `clerk.house.gov/floor/YYYYMMDD.xml`, the Clerk's timestamped floor actions, updated in near real time
- **House roll calls**: `clerk.house.gov/evs/YYYY/rollNNN.xml`, for the question, the bill title and the party split
- **Senate floor log**: `periodicalpress.senate.gov/on-the-floor`, the Periodical Press Gallery's running log. It has no timestamps, so those entries show a `~` and an approximate time.
- **Bill names and history**: GovInfo's public BILLSTATUS XML (`govinfo.gov/bulkdata/BILLSTATUS/...`), refreshed every 3 hours per bill
- **News**: a Google News search on the bill number and short title, for bills active in the last two weeks
- **Senate Daily Press Gallery log** (`dailypress.senate.gov`): timestamped floor log with wrap-up business (voice votes, unanimous consent). Used in place of the Periodical Press Gallery log when it's current.
- **Senate party floor staff**: Senate Democrats' daily schedule and wrap-up (`democrats.senate.gov/feed`) and the Senate Republican Cloakroom's Bluesky posts. These are labeled "via" on the page, and only facts not already covered by an official source are added.
- **Senate committee schedule**: `senate.gov/general/committee_schedules/hearings.xml`
- **Senate roll calls**: the `senate.gov` session vote list and per-vote XML, for the exact time, 60-vote thresholds and the party split

## Notes

- The labels come from pattern rules (`HOUSE_RULES` and `SENATE_RULES` in `floorwatch.py`). If something is labeled wrong, add or reorder a rule. The first match wins.
- If a source can't be reached or changes its format, a red note appears under the header and the rest keeps working.
- When running locally, cached roll calls and Senate first-seen times are stored in `floorwatch_data/`; on the live site they're stored in the `data` branch. Delete that folder or branch to reset.

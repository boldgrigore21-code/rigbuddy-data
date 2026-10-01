# Rig Buddy market data

This little project is the "middleman" for Rig Buddy's Oil & Rigs card.
Twice a day GitHub runs `fetch_data.py` for free. It pulls:

- WTI, Brent and Henry Hub natural gas spot prices from the U.S. EIA (needs your free EIA key)
- The weekly US rig count, Permian and New Mexico counts from Baker Hughes

and saves them to one small file, `docs/market.json`. GitHub Pages hosts that
file, and every copy of Rig Buddy reads it. The EIA key is stored as a GitHub
secret, so it is never inside the app or visible in this repo.

## One-time setup

1. **EIA key** – sign up at https://www.eia.gov/opendata/register.php. The key arrives by email.
2. **GitHub account** – free, at https://github.com/signup.
3. **New repository** – name it `rigbuddy-data`, set it to **Public** (free Pages needs public;
   only this code and the price file are in it, nothing private), and create it empty.
4. **Put these files in it** – either tell Claude the repo name and connect GitHub so Claude can
   push them, or use *Add file → Upload files* and drag in everything from this folder.
   Make sure `.github/workflows/update.yml` made it (Windows sometimes hides folders that start with a dot).
5. **Add the key** – repo *Settings → Secrets and variables → Actions → New repository secret*.
   Name: `EIA_API_KEY`. Value: your key.
6. **Turn on Pages** – *Settings → Pages → Build and deployment*: Source **Deploy from a branch**,
   Branch **main**, folder **/docs**, Save.
7. **First run** – *Actions* tab → *Update market data* → **Run workflow**. Takes about a minute.
8. **Check it** – open `https://YOUR-USERNAME.github.io/rigbuddy-data/market.json`.
   You should see prices and rig counts. Anything that failed is listed under `"errors"`.

Then send Claude that link so it can be set in the app.

## If something breaks

- GitHub emails you if a run fails completely.
- If only one source fails (say Baker Hughes changes their page), the file keeps the last good
  numbers for it and lists the problem under `"errors"`. The app keeps working with older numbers.
- Fixing it means changing `fetch_data.py` here. No app update is needed.
- GitHub pauses scheduled runs on repos with no activity for 60 days. The twice-daily commits
  count as activity, but if the Actions tab ever says the workflow is disabled, click **Enable**.

## Terms

Baker Hughes publishes the rig count for public use; the app credits "Source: Baker Hughes"
and "Source: U.S. EIA" under the card. Review Baker Hughes' site terms before the paid launch.

# arXiv daily keyword alert (quant-ph)

A tiny zero-cost setup that emails you new **quant-ph** papers each day, filtered
to your keywords. It runs entirely on GitHub Actions — no server, no PC left on.

**Flow:** GitHub Actions runs `arxiv_alert.py` daily → it queries the arXiv API
for the newest quant-ph submissions → keeps only papers matching `config.yaml` →
emails them to you → records their IDs in `seen.json` so you never get duplicates.

---

## What you'll do once (about 10 minutes)

### 1. Create the repository
1. Create a new **private** GitHub repo (e.g. `arxiv-alert`).
2. Upload these files, keeping the folder layout:
   ```
   arxiv_alert.py
   config.yaml
   requirements.txt
   seen.json
   .github/workflows/arxiv-daily.yml
   ```

### 2. Make a Gmail App Password (this is the only credential step)
Sending mail through Gmail needs an **App Password**, not your normal password.
1. Turn on 2-Step Verification on the Google account: https://myaccount.google.com/security
2. Go to https://myaccount.google.com/apppasswords
3. Create an app password (name it e.g. "arxiv alert"). Google shows a
   16-character code — copy it. You'll paste it into GitHub in the next step.

> Keep this code to yourself — paste it only into the GitHub secret below.
> It goes nowhere near the code in this repo.

### 3. Add repository secrets
In the repo: **Settings → Secrets and variables → Actions → New repository secret.**
Add these:

| Name        | Value                                            |
|-------------|--------------------------------------------------|
| `SMTP_USER` | the Gmail you send **from** (e.g. `xxx@gmail.com`) |
| `SMTP_PASS` | the 16-character **App Password** from step 2    |
| `MAIL_TO`   | where to deliver the digest (`xxx@gmail.com`) |

(`SMTP_USER` and `MAIL_TO` can be the same address — the account just emails itself.)

### 4. Turn on Actions and test
1. Open the **Actions** tab; if prompted, enable workflows for the repo.
2. Click **arXiv daily alert → Run workflow** to trigger it manually.
3. Check the run log and your inbox. If it says "No new matching papers today,"
   that just means nothing in the latest batch matched — try again after the next
   arXiv announcement, or loosen a keyword to confirm mail delivery works.

That's it. From then on it runs automatically every day.

---

## Tuning it

- **Keywords:** edit `config.yaml`. Rules are documented at the top of that file.
  Briefly: a paper matches a topic if **any** rule fires; a rule fires when
  **all** of its groups are present; **any** term in a group counts. Matching is
  case-insensitive substring, so `metrolog` catches metrology/metrological.
- **Schedule:** edit the `cron:` line in `.github/workflows/arxiv-daily.yml`.
  It's in **UTC**. The default `0 23 * * *` = 23:00 UTC = **07:00 Singapore** the
  next morning. (GitHub may delay scheduled runs a few minutes under load —
  harmless here thanks to dedup.)
- **Another category too:** change `category:` in `config.yaml`, or duplicate the
  repo for a second field. To scan multiple categories at once, tell me and I'll
  extend the script.
- **Too many / too few hits:** widen or tighten the term lists. Tighter rules
  (more AND-groups) = higher precision; broader OR-lists = higher recall.

## Run it locally (optional)
```bash
pip install -r requirements.txt
export SMTP_USER="xxx@gmail.com"
export SMTP_PASS="your-app-password"
export MAIL_TO="xxx@gmail.com"
python arxiv_alert.py
```

## Notes
- The first run may send a small burst (everything currently matching in the last
  couple of days). After that you only get genuinely new papers.
- `seen.json` is committed back automatically after each run — that's the memory
  of what's already been sent. Don't delete it unless you want a fresh start.

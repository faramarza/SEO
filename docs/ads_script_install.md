# Paid Search agent — install the Google Ads export (click-by-click)

Governor's **📣 Paid Search** tab reads your Google Ads data from a Google Sheet that
a small Google Ads Script fills in daily. This avoids needing a Google Ads API
developer token. The script is **read-only** against your ads account — it only reads
reports and writes rows to the Sheet; it never changes campaigns, bids, budgets, or
keywords.

You do this once. ~10 minutes.

---

## Step 1 — Create the Sheet (in your Google Drive)

1. Go to <https://sheets.google.com> signed in as **faramarza@gmail.com**.
2. Click **Blank spreadsheet**.
3. Name it something like **Governor Ads Export**.
4. Copy the full URL from the address bar — you'll paste it into the script next.
   It looks like `https://docs.google.com/spreadsheets/d/XXXXXXXXXXXXXXXXXXXX/edit`.
   The long middle part (`XXXX…`) is the **Sheet ID** — you'll also need it in Step 5.

You don't need to add tabs or headers — the script creates `SearchTerms` and
`Keywords` tabs itself.

---

## Step 2 — Add the script in Google Ads

1. Open **Google Ads** → top menu **Tools** (wrench icon) → **Bulk actions** → **Scripts**.
2. Click the blue **+** (New script).
3. Delete any starter code, then paste the entire contents of **`scripts/ads_export.js`**.
4. Near the top, replace `PASTE_YOUR_GOOGLE_SHEET_URL_HERE` with the Sheet URL from Step 1
   (keep the quotes).
5. Click **Save** (name it "Governor Paid Search Export").

---

## Step 3 — Authorize and test-run

1. Click **Authorize** (or **Preview**/**Run** — Google will prompt the first time).
2. Choose the **faramarza@gmail.com** account and **Allow** the permissions
   (it asks for Google Ads read + Sheets access).
3. Click **Run** (not just Preview). Wait for it to finish — the logs should say
   *"Governor Paid Search export complete"*.
4. Open your Sheet. You should now see two tabs, **SearchTerms** and **Keywords**,
   with a header row and data. If they're empty, you may simply have little recent
   search-term data — that's fine, the report handles it.

---

## Step 4 — Schedule it daily

1. Back on the Scripts page, find your script row.
2. Under **Frequency**, click to set a schedule → **Daily**, any early-morning hour.
3. Save. From now on the Sheet refreshes every day with the last 30 days.

---

## Step 5 — Let Governor read the Sheet

Governor reads the Sheet using the **same service account** it already uses for GA4 and
Search Console — so you just share the Sheet with that account's email and tell Governor
the Sheet ID.

**5a. Find the service-account email.** On the server:

```
grep client_email /var/www/seo/credentials.json
```

It looks like `something@your-project.iam.gserviceaccount.com`. Copy it.

**5b. Share the Sheet with it (read-only).**
1. In your Sheet, click **Share**.
2. Paste the service-account email.
3. Set its access to **Viewer** (read-only — important).
4. Untick "Notify people", click **Share**.

**5c. Tell Governor the Sheet ID.** Edit `config/defaults.json` on the server and set
the Sheet ID (the `XXXX…` from the URL in Step 1):

```json
"paid_search": {
  "ads_sheet": { "sheet_id": "XXXXXXXXXXXXXXXXXXXX", "search_terms_tab": "SearchTerms", "keywords_tab": "Keywords" }
}
```

**5d. Restart Governor:**

```
cd /var/www/seo && git pull && sudo systemctl restart seo
```

---

## Done

Open **📣 Paid Search** in Governor and click **↻ Refresh from the data**. You should
see the profit-per-campaign table, negatives, free-traffic overlaps, new keyword
candidates, device split, and headline ideas.

If the tab says *"Connect your Google Ads export first,"* the Sheet ID isn't set
(Step 5c). If a section is empty but others work, that input simply had no data for the
window — the report never invents numbers.

### Notes
- The script reads **conversion value as Google Ads reports it.** If your Ads
  conversion value includes sales tax, the report detects it (by comparing to GA4
  pre-tax revenue) and shows a banner, because your margins are on pre-tax revenue.
- Everything here is read-only. Governor suggests; you apply every change in Google Ads
  by hand.

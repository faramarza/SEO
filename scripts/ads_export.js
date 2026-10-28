/**
 * Alphabet Trains — Paid Search export for Governor  (READ-ONLY)
 * ---------------------------------------------------------------
 * Exports the last 30 days of Google Ads SEARCH TERMS and KEYWORD performance to a
 * Google Sheet that Governor reads. This script only READS your Google Ads data and
 * WRITES rows to the Sheet. It never changes campaigns, bids, budgets, keywords or
 * anything else in your account.
 *
 * ONE-TIME SETUP (see docs/ads_script_install.md for click-by-click steps):
 *   1. Create a blank Google Sheet in your Drive; copy its URL.
 *   2. Paste that URL into SHEET_URL below.
 *   3. In Google Ads: Tools -> Bulk actions -> Scripts -> New script, paste this in,
 *      Authorize, Run once, then schedule it DAILY.
 *   4. Share the Sheet (read-only / Viewer) with your Governor service-account email.
 *   5. Put the Sheet ID into config: paid_search.ads_sheet.sheet_id, restart Governor.
 *
 * The column HEADERS below are what Governor matches on (by name, case-insensitive),
 * so keep them as-is. Cost is written in your account currency (not micros).
 */

var SHEET_URL = 'PASTE_YOUR_GOOGLE_SHEET_URL_HERE';
var SEARCH_TERMS_TAB = 'SearchTerms';
var KEYWORDS_TAB = 'Keywords';
var DATE_RANGE = 'LAST_30_DAYS';

function main() {
  if (SHEET_URL.indexOf('PASTE_YOUR') === 0) {
    throw new Error('Set SHEET_URL to your Google Sheet URL first.');
  }
  var ss = SpreadsheetApp.openByUrl(SHEET_URL);

  writeReport(
    ss, SEARCH_TERMS_TAB,
    ['Search term', 'Campaign', 'Clicks', 'Cost', 'Conversions', 'Conv. value'],
    'SELECT search_term_view.search_term, campaign.name, metrics.clicks, ' +
    'metrics.cost_micros, metrics.conversions, metrics.conversions_value ' +
    'FROM search_term_view WHERE segments.date DURING ' + DATE_RANGE,
    function (row) {
      return [
        get(row, ['searchTermView', 'searchTerm']),
        get(row, ['campaign', 'name']),
        num(get(row, ['metrics', 'clicks'])),
        micros(get(row, ['metrics', 'costMicros'])),
        num(get(row, ['metrics', 'conversions'])),
        num(get(row, ['metrics', 'conversionsValue']))
      ];
    });

  writeReport(
    ss, KEYWORDS_TAB,
    ['Keyword', 'Match type', 'Campaign', 'Clicks', 'Cost', 'Conversions', 'Conv. value'],
    'SELECT ad_group_criterion.keyword.text, ad_group_criterion.keyword.match_type, ' +
    'campaign.name, metrics.clicks, metrics.cost_micros, metrics.conversions, ' +
    'metrics.conversions_value FROM keyword_view ' +
    'WHERE segments.date DURING ' + DATE_RANGE +
    " AND ad_group_criterion.status != 'REMOVED'",
    function (row) {
      return [
        get(row, ['adGroupCriterion', 'keyword', 'text']),
        get(row, ['adGroupCriterion', 'keyword', 'matchType']),
        get(row, ['campaign', 'name']),
        num(get(row, ['metrics', 'clicks'])),
        micros(get(row, ['metrics', 'costMicros'])),
        num(get(row, ['metrics', 'conversions'])),
        num(get(row, ['metrics', 'conversionsValue']))
      ];
    });

  Logger.log('Governor Paid Search export complete: ' + new Date());
}

/** Run a GAQL query and overwrite one tab with header + rows. */
function writeReport(ss, tabName, headers, gaql, mapRow) {
  var sheet = ss.getSheetByName(tabName) || ss.insertSheet(tabName);
  sheet.clearContents();
  var out = [headers];
  var it = AdsApp.search(gaql);
  while (it.hasNext()) {
    out.push(mapRow(it.next()));
  }
  sheet.getRange(1, 1, out.length, headers.length).setValues(out);
}

/** Safe nested lookup; returns '' if any level is missing. */
function get(obj, path) {
  var cur = obj;
  for (var i = 0; i < path.length; i++) {
    if (cur == null) return '';
    cur = cur[path[i]];
  }
  return cur == null ? '' : cur;
}

function num(v) { var n = Number(v); return isNaN(n) ? 0 : n; }
function micros(v) { return Math.round(num(v) / 1e6 * 100) / 100; }

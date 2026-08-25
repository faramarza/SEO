/**
 * Agentic Organic Growth Governor — Dashboard JavaScript
 */

// Utility functions
function formatCurrency(value) {
    return '$' + value.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function formatPercent(value) {
    return (value * 100).toFixed(1) + '%';
}

function formatDate(isoString) {
    if (!isoString) return 'Never';
    const date = new Date(isoString);
    return date.toLocaleDateString('en-US', {
        year: 'numeric',
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit'
    });
}

// API helper
async function api(endpoint, options = {}) {
    const res = await fetch('/api' + endpoint, {
        headers: {
            'Content-Type': 'application/json',
            ...options.headers
        },
        ...options
    });
    return res.json();
}

// Toast notifications
function showToast(message, type = 'info') {
    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    toast.textContent = message;
    document.body.appendChild(toast);

    setTimeout(() => {
        toast.classList.add('fade-out');
        setTimeout(() => toast.remove(), 300);
    }, 3000);
}

// Initialize on page load
document.addEventListener('DOMContentLoaded', () => {
    // Add any global initialization here
    console.log('Governor Dashboard initialized');
});

/* ── Generic clickable-header table sorting (two-level) ───────────────────
   Makes any data table (a <table> with <thead> and <tbody>) sortable by
   clicking its column headers — across ALL screens, via event delegation, so
   dynamically-rendered tables work too. Clicks build the sort IN ORDER: the
   first column clicked is the primary key, a second column click adds it as
   the tiebreaker, clicking a sorted column flips its direction, and clicking
   a third, different column starts a fresh sort (same scheme as Growth →
   Top movers; arrows show 1▼ / 2▲). Numeric columns sort high→low first,
   text A→Z. Opt out: data-nosort on the table or a <th>. Headers with their
   own onclick (e.g. Playbook's custom Striking sort) are left alone. */
(function () {
  function cellVal(td) {
    // A cell can carry an explicit sort key (data-sort) when its display text
    // isn't the thing to sort by (e.g. GEO's "C 57" grade pill sorts by 57).
    var ds = td && td.getAttribute && td.getAttribute('data-sort');
    var t = ds != null ? String(ds).trim() : (td ? (td.textContent || '').trim() : '');
    if (t === '') return { t: '', n: 0, num: false };
    var cleaned = t.replace(/[,$%]/g, '').replace(/\/mo\b/gi, '').replace(/[▲▼→].*/, '').trim();
    // Sort by the cell's LEADING number so unit suffixes ("68.6 clk", "0% → 5%",
    // "141 → 42") still sort numerically. The lookahead rejects date-like values
    // ("2026-08-15", "8/22/2026") so those keep sorting as text.
    var m = cleaned.match(/^[-+]?\d+(\.\d+)?(?![\d/-])/);
    return { t: t.toLowerCase(), n: m ? parseFloat(m[0]) : 0, num: !!m };
  }
  function colKeys(rows, idx) {
    var keys = rows.map(function (r) { return cellVal(r.cells[idx]); });
    var allNum = keys.every(function (k) { return k.num || k.t === ''; }) &&
                 keys.some(function (k) { return k.num; });
    return { keys: keys, num: allNum };
  }
  function sortTable(table, idx, th) {
    var tbody = table.tBodies[0];
    if (!tbody) return;
    var rows = Array.prototype.slice.call(tbody.rows).filter(function (r) { return r.cells.length > idx; });
    if (rows.length < 2) return;
    var stack;
    try { stack = JSON.parse(table.getAttribute('data-sort-stack') || '[]'); } catch (e) { stack = []; }
    if (!Array.isArray(stack)) stack = [];
    var hit = -1;
    for (var si = 0; si < stack.length; si++) if (stack[si].idx === idx) hit = si;
    if (hit >= 0) {
      stack[hit].dir = stack[hit].dir === 'asc' ? 'desc' : 'asc';
    } else if (stack.length < 2) {
      stack.push({ idx: idx, dir: colKeys(rows, idx).num ? 'desc' : 'asc' });
    } else {
      stack = [{ idx: idx, dir: colKeys(rows, idx).num ? 'desc' : 'asc' }];
    }
    var cols = stack.map(function (s) {
      var c = colKeys(rows, s.idx);
      return { keys: c.keys, num: c.num, mul: s.dir === 'asc' ? 1 : -1 };
    });
    var order = rows.map(function (r, i) { return i; });
    order.sort(function (a, b) {
      for (var ci = 0; ci < cols.length; ci++) {
        var c = cols[ci];
        var d = c.num ? (c.keys[a].n || 0) - (c.keys[b].n || 0)
                      : c.keys[a].t.localeCompare(c.keys[b].t);
        if (d) return c.mul * d;
      }
      return 0;
    });
    order.forEach(function (i) { tbody.appendChild(rows[i]); });
    table.setAttribute('data-sort-stack', JSON.stringify(stack));
    var ths = Array.prototype.slice.call(th.parentElement.children);
    ths.forEach(function (h) {
      var a = h.querySelector('.tbl-sort-arrow'); if (a) a.remove();
      if (!h.title && !h.hasAttribute('data-nosort') && h.textContent.trim()) {
        h.title = 'Click to sort. A second column click adds it as the tiebreaker; '
                + 'clicking a sorted column flips it; a third column starts a new sort.';
      }
    });
    var names = ths.map(function (h) { return (h.textContent || '').trim(); });
    stack.forEach(function (s, level) {
      var h = ths[s.idx];
      if (!h) return;
      var arrow = document.createElement('span');
      arrow.className = 'tbl-sort-arrow';
      arrow.textContent = ' ' + (stack.length > 1 ? (level + 1) : '') + (s.dir === 'asc' ? '▲' : '▼');
      arrow.style.opacity = level === 0 ? '0.75' : '0.5';
      h.appendChild(arrow);
    });
    // Spell the sort out above the table — numbered arrows alone read as
    // "broken" when the tiebreaker has no ties to act on.
    var cap = table.previousElementSibling;
    if (!cap || !cap.classList || !cap.classList.contains('tbl-sort-state')) {
      cap = document.createElement('div');
      cap.className = 'tbl-sort-state';
      cap.style.cssText = 'font-size:11px;color:var(--text-muted,#64748b);margin:6px 0 2px;';
      if (table.parentElement) table.parentElement.insertBefore(cap, table);
    }
    cap.textContent = 'Sorting: ' + stack.map(function (s, i) {
      return (i + 1) + '. ' + (names[s.idx] || 'column ' + (s.idx + 1)) + ' ' + (s.dir === 'asc' ? '▲' : '▼');
    }).join('  ·  ') + (stack.length > 1
      ? '  —  the 2nd column only orders rows the 1st column ties on'
      : '');
  }
  document.addEventListener('click', function (e) {
    var th = e.target.closest && e.target.closest('th');
    if (!th || th.hasAttribute('data-nosort') || th.getAttribute('onclick')) return;
    if (!th.closest('thead') || !th.textContent.trim()) return;
    var table = th.closest('table');
    if (!table || table.hasAttribute('data-nosort') || !table.tBodies || !table.tBodies[0]) return;
    var idx = Array.prototype.slice.call(th.parentElement.children).indexOf(th);
    if (idx >= 0) sortTable(table, idx, th);
  });
  var st = document.createElement('style');
  st.textContent = 'table thead th:not([data-nosort]){cursor:pointer;user-select:none;}.tbl-sort-arrow{font-size:.85em;}';
  (document.head || document.documentElement).appendChild(st);
})();

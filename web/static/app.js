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

/* ── Generic clickable-header table sorting ───────────────────────────────
   Makes any data table (a <table> with <thead> and <tbody>) sortable by
   clicking its column headers — across ALL screens, via event delegation, so
   dynamically-rendered tables work too. Click to sort; click again to flip.
   Numeric columns sort high→low first, text A→Z. Opt out: data-nosort on the
   table or a <th>. Headers with their own onclick (e.g. Playbook's custom
   Striking sort) are left alone. */
(function () {
  function cellVal(td) {
    var t = (td.textContent || '').trim();
    if (t === '') return { t: '', n: 0, num: false };
    var cleaned = t.replace(/[,$%]/g, '').replace(/\/mo\b/gi, '').replace(/[▲▼→].*/, '').trim();
    // Sort by the cell's LEADING number so unit suffixes ("68.6 clk", "0% → 5%",
    // "141 → 42") still sort numerically. The lookahead rejects date-like values
    // ("2026-08-15", "8/22/2026") so those keep sorting as text.
    var m = cleaned.match(/^[-+]?\d+(\.\d+)?(?![\d/-])/);
    return { t: t.toLowerCase(), n: m ? parseFloat(m[0]) : 0, num: !!m };
  }
  function sortTable(table, idx, th) {
    var tbody = table.tBodies[0];
    if (!tbody) return;
    var rows = Array.prototype.slice.call(tbody.rows).filter(function (r) { return r.cells.length > idx; });
    if (rows.length < 2) return;
    var keys = rows.map(function (r) { return cellVal(r.cells[idx]); });
    var allNum = keys.every(function (k) { return k.num || k.t === ''; }) &&
                 keys.some(function (k) { return k.num; });
    var dir = table.getAttribute('data-sort-col') === String(idx)
      ? (table.getAttribute('data-sort-dir') === 'asc' ? 'desc' : 'asc')
      : (allNum ? 'desc' : 'asc');
    var mul = dir === 'asc' ? 1 : -1;
    var order = rows.map(function (r, i) { return i; });
    order.sort(function (a, b) {
      return allNum ? mul * ((keys[a].n || 0) - (keys[b].n || 0))
                    : mul * keys[a].t.localeCompare(keys[b].t);
    });
    order.forEach(function (i) { tbody.appendChild(rows[i]); });
    table.setAttribute('data-sort-col', idx);
    table.setAttribute('data-sort-dir', dir);
    Array.prototype.slice.call(th.parentElement.children).forEach(function (h) {
      var a = h.querySelector('.tbl-sort-arrow'); if (a) a.remove();
    });
    var arrow = document.createElement('span');
    arrow.className = 'tbl-sort-arrow';
    arrow.textContent = dir === 'asc' ? ' ▲' : ' ▼';
    arrow.style.opacity = '0.65';
    th.appendChild(arrow);
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

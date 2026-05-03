/* Tiny client-side table sorter.
   Any <table class="sortable"> on the page becomes column-sortable: click a
   header to toggle ascending/descending sort by that column. Cells in <th>
   marked class="num" are sorted numerically; everything else lexicographically.
   Designed to be small and dependency-free. */
(function () {
  function init() {
    document.querySelectorAll('table.sortable').forEach(function (table) {
      var headers = table.querySelectorAll('thead th');
      headers.forEach(function (th) {
        th.style.cursor = 'pointer';
        th.addEventListener('click', function () {
          var headerRow = th.parentElement;
          var colIndex = Array.prototype.indexOf.call(headerRow.children, th);
          var tbody = table.querySelector('tbody');
          if (!tbody) return;
          var rows = Array.prototype.slice.call(tbody.querySelectorAll('tr'));
          var isNum = th.classList.contains('num');
          var asc = th.dataset.dir !== 'asc';
          headerRow.querySelectorAll('th').forEach(function (o) { delete o.dataset.dir; });
          th.dataset.dir = asc ? 'asc' : 'desc';
          rows.sort(function (a, b) {
            var av = (a.children[colIndex] || {}).innerText || '';
            var bv = (b.children[colIndex] || {}).innerText || '';
            av = av.trim(); bv = bv.trim();
            if (isNum) {
              av = parseFloat(av.replace(/[^0-9.\-]/g, '')) || 0;
              bv = parseFloat(bv.replace(/[^0-9.\-]/g, '')) || 0;
            }
            if (av > bv) return asc ? 1 : -1;
            if (av < bv) return asc ? -1 : 1;
            return 0;
          });
          rows.forEach(function (r) { tbody.appendChild(r); });
        });
      });
    });
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();

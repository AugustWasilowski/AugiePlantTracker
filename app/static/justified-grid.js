// justified-grid.js — Flickr/Immich-style justified rows.
//
// Looks for every <div class="justified-grid" data-photos="…json…"> on the
// page and lays it out. Re-runs on resize.
//
// Each row's photos share the same scaled height. Photos keep their natural
// aspect ratio (no cropping). The last underfilled row stays at target height
// and left-aligns instead of stretching to full width.
(function () {
  'use strict';

  const ROW_HEIGHT_DESKTOP = 180;
  const ROW_HEIGHT_MOBILE  = 120;
  const GAP = 4;
  const MAX_ASPECT = 2.4;
  const MIN_ASPECT = 0.5;
  const LAST_ROW_FILL_THRESHOLD = 0.7; // if last row natural width < 70% of container, don't stretch

  // Live-filter state. Empty string = pass all.
  let currentFilter = '';
  function passesFilter(p) {
    if (!currentFilter) return true;
    const hay = ((p.plantNick || '') + ' ' + (p.species || '') + ' ' + (p.common || '') + ' ' + (p.location || '')).toLowerCase();
    return hay.includes(currentFilter);
  }

  function clampAspect(a) {
    if (!isFinite(a) || a <= 0) return 1;
    return Math.max(MIN_ASPECT, Math.min(MAX_ASPECT, a));
  }

  function targetRowHeight(containerWidth) {
    return containerWidth < 520 ? ROW_HEIGHT_MOBILE : ROW_HEIGHT_DESKTOP;
  }

  function layoutRows(photos, containerWidth, target, gap) {
    const rows = [];
    let row = [];
    let rowAspectSum = 0;

    function flush(isLast) {
      if (!row.length) return;
      const totalGap = gap * (row.length - 1);
      const naturalWidth = rowAspectSum * target;
      let scale = (containerWidth - totalGap) / naturalWidth;
      let height = target * scale;
      if (isLast && naturalWidth + totalGap < containerWidth * LAST_ROW_FILL_THRESHOLD) {
        height = target; scale = 1;
      }
      rows.push({
        height: Math.round(height),
        items: row.map(it => ({
          photo: it.photo,
          width: Math.round(it.aspect * height),
          height: Math.round(height),
        })),
      });
      row = []; rowAspectSum = 0;
    }

    for (const p of photos) {
      const aspect = clampAspect(p.w / p.h);
      row.push({ photo: p, aspect });
      rowAspectSum += aspect;
      const naturalWidth = rowAspectSum * target;
      const totalGap = gap * (row.length - 1);
      if (naturalWidth + totalGap >= containerWidth) flush(false);
    }
    flush(true);
    return rows;
  }

  function renderGrid(container) {
    const all = JSON.parse(container.dataset.photos || '[]');
    const photos = all.filter(passesFilter);

    // Hide the group head sibling when the filter empties this group, so
    // we don't show "Living Room — 0 photos" with nothing under it.
    const head = container.previousElementSibling;
    const isHead = head && head.classList && head.classList.contains('gallery-group-head');

    if (!all.length) return;
    if (!photos.length) {
      container.textContent = '';
      container.style.display = 'none';
      if (isHead) head.style.display = 'none';
      return;
    }
    container.style.display = '';
    if (isHead) {
      head.style.display = '';
      const cnt = head.querySelector('.gallery-group-count');
      if (cnt) cnt.textContent = photos.length + ' photo' + (photos.length === 1 ? '' : 's');
    }

    const containerWidth = Math.floor(container.getBoundingClientRect().width);
    if (!containerWidth) return;
    const target = targetRowHeight(containerWidth);
    const rows = layoutRows(photos, containerWidth, target, GAP);

    // Clear and rebuild. Preserves <noscript> outside this innerHTML write.
    container.textContent = '';

    for (const row of rows) {
      const rowEl = document.createElement('div');
      rowEl.className = 'jg-row';
      rowEl.style.height = row.height + 'px';
      for (const item of row.items) {
        const a = document.createElement('a');
        a.className = 'jg-tile';
        a.href = '/plants/' + item.photo.plantId;
        a.style.width = item.width + 'px';
        a.style.height = item.height + 'px';
        a.setAttribute('aria-label', item.photo.plantNick || 'Photo');

        const img = document.createElement('img');
        img.src = item.photo.src;
        img.alt = '';
        img.loading = 'lazy';
        a.appendChild(img);

        const cap = document.createElement('div');
        cap.className = 'jg-caption';
        cap.innerHTML =
          '<span class="jg-nick">' + escapeHtml(item.photo.plantNick || '') + '</span>' +
          '<span class="jg-date">' + escapeHtml(item.photo.date || '') + '</span>';
        a.appendChild(cap);

        rowEl.appendChild(a);
      }
      container.appendChild(rowEl);
    }
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function renderAll() {
    let total = 0;
    document.querySelectorAll('.justified-grid').forEach(grid => {
      const all = JSON.parse(grid.dataset.photos || '[]');
      total += all.filter(passesFilter).length;
      renderGrid(grid);
    });
    // Sync the toolbar count next to the "Gallery" title.
    const countEl = document.querySelector('.gallery-count');
    if (countEl) countEl.textContent = total + ' photo' + (total === 1 ? '' : 's');
  }

  // Debounced resize.
  let resizeTimer = null;
  function onResize() {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(renderAll, 120);
  }

  // Wire the toolbar search input for live filtering. The form's Enter-to-
  // submit behavior still works for shareable URLs.
  function wireSearch() {
    const searchInput = document.querySelector('.gallery-search input[name="q"]');
    if (!searchInput) return;
    if (searchInput.value) currentFilter = searchInput.value.trim().toLowerCase();
    searchInput.addEventListener('input', () => {
      currentFilter = searchInput.value.trim().toLowerCase();
      renderAll();
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => { wireSearch(); renderAll(); });
  } else {
    wireSearch();
    renderAll();
  }
  window.addEventListener('resize', onResize);
})();

'use strict';

// Separate budgets for visible rows, the nearby rows on either side, and the rest.
class PreviewLoader {
  constructor() {
    this.cache = new Map();
    this.active = new Map();
    this.limits = {visible: 6, nearby: 6, background: 3};
    this.nearbyRows = 4;
    this.failed = new Set();
    this.warmed = new Set();
    this.elements = [];
    this.rows = [];
    this.scores = new Map();
    this.layoutDirty = true;
    this.paused = false;
    this.backgroundPaused = false;
    this.frame = null;
    document.addEventListener('scroll', event => {
      if (event.target?.classList?.contains('post-strip')) this.layoutDirty = true;
      this.schedule();
    }, {capture: true, passive: true});
    window.addEventListener('resize', () => {this.layoutDirty = true; this.schedule();}, {passive: true});
    document.addEventListener('visibilitychange', () => this.schedule());
    this.resizeObserver = new ResizeObserver(() => {this.layoutDirty = true; this.schedule();});
    this.resizeObserver.observe(document.querySelector('main'));
    this.resizeObserver.observe(document.querySelector('aside'));
  }

  refresh() {
    this.elements = [...document.querySelectorAll('img[data-preview]')];
    for (const img of this.elements) {
      const cached = this.cache.get(img.dataset.preview);
      if (cached) this.show(img, cached);
      else if (img.dataset.loaded !== img.dataset.preview) img.classList.add('preview-pending');
    }
    this.layoutDirty = true;
    this.schedule();
  }

  show(img, url) {
    if (img.getAttribute('src') !== url) img.src = url;
    img.dataset.loaded = img.dataset.preview;
    img.classList.remove('preview-pending');
    img.parentElement.classList.remove('preview-loading');
  }

  schedule() {
    if (this.frame === null) this.frame = requestAnimationFrame(() => {
      this.frame = null;
      this.pump();
    });
  }

  // Read layout only when cards or container sizes change, not on every fetch.
  measureRows() {
    const aside = document.querySelector('aside');
    const sideRect = aside.getBoundingClientRect();
    const groups = {main: new Map(), side: new Map()};
    for (const img of this.elements) {
      if (!img.isConnected || !img.getClientRects().length) continue;
      const r = img.getBoundingClientRect();
      const side = !!img.closest('aside');
      const top = side ? r.top - sideRect.top + aside.scrollTop : r.top + scrollY;
      const key = Math.round(top);
      const rows = side ? groups.side : groups.main;
      if (!rows.has(key)) rows.set(key, {top, bottom: top + r.height, elements: []});
      const clip = img.closest('.post-strip')?.getBoundingClientRect();
      rows.get(key).elements.push({img, horizontal: !clip || (r.right > clip.left && r.left < clip.right)});
    }
    this.rows = Object.entries(groups).map(([area, rows]) => ({area, rows: [...rows.values()].sort((a,b) => a.top-b.top)}));
    this.layoutDirty = false;
  }

  rankRows() {
    if (this.layoutDirty) this.measureRows();
    const aside = document.querySelector('aside'), sideRect = aside.getBoundingClientRect();
    const headerBottom = document.querySelector('header').getBoundingClientRect().bottom;
    this.scores.clear();
    for (const group of this.rows) {
      if (group.area === 'main' && !document.querySelector('#viewer').hidden) continue;
      const side = group.area === 'side';
      const offset = side ? sideRect.top - aside.scrollTop : -scrollY;
      const top = side ? Math.max(headerBottom, sideRect.top) : headerBottom;
      const bottom = side ? Math.min(innerHeight, sideRect.bottom) : innerHeight;
      const visible = group.rows.map((row,index) => ({row,index}))
        .filter(({row}) => top < bottom && row.bottom+offset > top && row.top+offset < bottom);
      // If the viewport is between day sections, take actual neighboring rows on each side.
      const first = visible.length ? visible[0].index : group.rows.findIndex(row => row.bottom+offset > top);
      const start = first < 0 ? group.rows.length : first;
      const last = visible.length ? visible[visible.length-1].index : start-1;
      group.rows.forEach((row,index) => {
        const isVisible = top < bottom && row.bottom+offset > top && row.top+offset < bottom;
        const priority = top >= bottom ? 2 : isVisible ? 0 : index >= start-this.nearbyRows && index <= last+this.nearbyRows ? 1 : 2;
        const distance = Math.max(top-row.bottom-offset, row.top+offset-bottom, 0);
        for (const {img,horizontal} of row.elements) {
          this.scores.set(img, {priority: horizontal ? priority : 2, distance});
        }
      });
    }
  }

  pump() {
    if (document.hidden || this.paused || this.largePending) return;
    this.rankRows();
    // Include in-flight images when deciding the current stage. Otherwise
    // prefetch would start as soon as visible images were merely requested.
    const pending = new Map();
    for (const [img, score] of this.scores) {
      const url = img.dataset.preview;
      if (img.dataset.loaded === url || this.failed.has(url)) continue;
      if (score.priority === 2 && (this.backgroundPaused || this.warmed.has(url))) continue;
      if (this.cache.has(url)) {
        if (score.priority < 2) this.show(img, this.cache.get(url));
        continue;
      }
      const previous = pending.get(url);
      if (!previous || score.priority < previous.priority ||
          (score.priority === previous.priority && score.distance < previous.distance)) {
        pending.set(url, {url, ...score});
      }
    }
    const candidates = [...pending.values()].sort((a,b) => a.priority-b.priority || a.distance-b.distance);
    if (!candidates.length) return;
    const stage = candidates[0].priority;
    const pool = ['visible','nearby','background'][stage];
    const inPool = [...this.active.values()].filter(value => value === pool).length;
    // Keep the browser's connection queue short. In particular, do not fill
    // it with speculative requests when a scroll reveals new visible rows.
    let available = Math.min(this.limits[pool] - inPool, 6 - this.active.size);
    for (const {url, priority} of candidates) {
      if (priority !== stage || available <= 0) break;
      if (this.active.has(url)) continue;
      available--;
      this.load(url, pool);
    }
  }

  pageStats() {
    const unique = urls => new Set([...urls].filter(url => url.startsWith('/photo/')).map(url => url.split('?')[0])).size;
    return {loaded: unique(this.warmed), retained: unique(this.cache.keys()), limit: 256};
  }

  async load(url, pool) {
    this.active.set(url, pool);
    if (pool !== 'background') for (const img of this.elements) if (img.dataset.preview === url) img.parentElement.classList.add('preview-loading');
    try {
      const response = await fetch(url, {
        // Reuse browser HTTP cache as well as our bounded in-page cache.
        cache: 'default',
        headers: {'X-Preview-Priority': pool},
        priority: pool === 'visible' ? 'high' : pool === 'nearby' ? 'auto' : 'low',
      });
      if (!response.ok) throw new Error('Preview unavailable');
      const objectURL = URL.createObjectURL(await response.blob());
      this.cache.set(url, objectURL);
      this.warmed.add(url);
      for (const img of this.elements) if (img.dataset.preview === url && (this.scores.get(img)?.priority ?? 2) < 2) this.show(img, objectURL);
      // Bound memory even after scrolling through thousands of files.
      while (this.cache.size > 256) {
        const oldest = this.cache.keys().next().value;
        URL.revokeObjectURL(this.cache.get(oldest));
        this.cache.delete(oldest);
      }
    } catch {
      this.failed.add(url);
      for (const img of this.elements) if (img.dataset.preview === url) {
        img.parentElement.classList.remove('preview-loading');
        img.parentElement.title = 'Превью недоступно. Нажмите «Обновить библиотеку», чтобы повторить.';
      }
    } finally {
      this.active.delete(url);
      this.schedule();
    }
  }
}
const previews = new PreviewLoader();

// Keep compressed large previews, decoding only the displayed image.
class LargePreviewLoader {
  constructor(onReady, onActivity = () => {}) {
    this.onReady = onReady;
    this.onActivity = onActivity;
    this.prefetchLimit = 3;
    this.cache = new Map();
    this.active = new Map();
    this.failed = new Set();
    this.urls = [];
    this.current = null;
    this.paused = false;
    this.backgroundPaused = false;
    document.addEventListener('visibilitychange', () => this.pump());
  }

  setWindow(current, neighbors) {
    this.current = current;
    this.urls = [...new Set([current, ...neighbors].filter(Boolean))].slice(0, 21);
    this.trim();
    if (current && this.cache.has(current)) this.onReady(current, this.cache.get(current));
    else if (current && this.failed.has(current)) this.onReady(current, null);
    this.pump();
  }

  close() {
    this.current = null;
    this.urls = [];
    this.onActivity(false);
  }

  trim() {
    for (const [url, blob] of this.cache) {
      if (!this.urls.includes(url)) {
        URL.revokeObjectURL(blob);
        this.cache.delete(url);
      }
    }
  }

  pump() {
    const pending = url => url && !this.cache.has(url) && !this.failed.has(url);
    // Give the viewer the next available connections before starting thumbnails.
    this.onActivity(Boolean(this.urls.length && (pending(this.current) ||
      (!this.backgroundPaused && this.urls.some(pending)))));
    if (this.paused || document.hidden || !this.urls.length) return;
    if (pending(this.current)) {
      if (!this.active.has(this.current) && ![...this.active.values()].includes('visible'))
        this.load(this.current, 'visible');
      return;
    }
    if (this.backgroundPaused) return;
    let available = this.prefetchLimit - [...this.active.values()].filter(value => value === 'background').length;
    for (const url of this.urls) {
      if (available <= 0) break;
      if (!pending(url) || this.active.has(url)) continue;
      available--;
      this.load(url, 'background');
    }
  }

  async load(url, priority) {
    this.active.set(url, priority);
    try {
      const response = await fetch(url, {cache: 'default',
        headers: {'X-Preview-Priority': priority}, priority: 'high'});
      if (!response.ok) throw new Error('Large preview unavailable');
      const blob = await response.blob();
      if (this.urls.includes(url)) {
        const objectURL = URL.createObjectURL(blob);
        this.cache.set(url, objectURL);
        if (url === this.current) this.onReady(url, objectURL);
      }
    } catch (_) {
      this.failed.add(url);
      if (url === this.current) this.onReady(url, null);
    } finally {
      this.active.delete(url);
      this.pump();
    }
  }
}

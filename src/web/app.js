'use strict';
const $ = s => document.querySelector(s);
const esc = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let photos = [], byId, state, token, tab = 'photos', current = null, viewerIds = [], visible = [];
let cacheStatsTimer = null;
let largeDisplayRequest = 0, largeLoadingTimer = null;
const decodedLargePhotos = new Map();
function decodeLargePhoto(blob) {
  let entry=decodedLargePhotos.get(blob);
  if (!entry) {
    const image=new Image();
    image.src=blob;
    entry={image, ready:false};
    entry.promise=image.decode().then(()=>{entry.ready=true;});
  }
  decodedLargePhotos.delete(blob);
  decodedLargePhotos.set(blob,entry);
  while(decodedLargePhotos.size>3) decodedLargePhotos.delete(decodedLargePhotos.keys().next().value);
  return entry;
}
function finishLargeLoading() {
  clearTimeout(largeLoadingTimer);
  $('#largeLoading').hidden=true;
  $('#largePhoto').classList.remove('large-loading');
}
let dirty = false, saving = null, saveTimer, toastTimer, blocked = false, dragId = null, exportId;
const currentPost = () => state.posts.find(p => p.id === state.active);
const photoURL = (id, large=false) => byId.get(id)?.missing ? '/missing.svg' : `/photo/${id}?v=${encodeURIComponent(byId.get(id)?.version || '2')}${large ? '&size=large' : ''}`;
const largePreviews = new LargePreviewLoader(async (url, blob) => {
  if (!current || byId.get(current)?.kind === 'video' || byId.get(current)?.missing || photoURL(current, true) !== url) return;
  const request=++largeDisplayRequest;
  const img=$('#largePhoto');
  // Keep the previous image blurred until the replacement is decoded, too.
  try {
    if (!blob) throw new Error('Preview unavailable');
    if (img.getAttribute('src') !== blob) {
      const decoded=decodeLargePhoto(blob);
      if (!decoded.ready) await decoded.promise;
    }
    if (request !== largeDisplayRequest) return;
    if (img.getAttribute('src') !== blob) img.src=blob;
    finishLargeLoading();
    img.hidden=false;
    img.classList.remove('large-loading');
    img.removeAttribute('aria-hidden');
    $('#largeLoading').hidden=true;
    $('#largeError').hidden=true;
  } catch (_) {
    if (request !== largeDisplayRequest) return;
    finishLargeLoading();
    img.hidden=true;
    img.classList.remove('large-loading');
    $('#largeLoading').hidden=true;
    $('#largeError').hidden=false;
  }
}, busy => {
  previews.largePending=busy;
  if (!busy) previews.schedule();
});
function prepareLargePreviews() {
  const neighbors=[], index=viewerIds.indexOf(current);
  const sides=[[],[]];
  for (const [side, step] of [[0,1],[1,-1]]) {
    for (let i=index+step; i>=0 && i<viewerIds.length && sides[side].length<10; i+=step) {
      const p=byId.get(viewerIds[i]);
      if (p && !p.missing && p.kind!=='video') sides[side].push(photoURL(p.id,true));
    }
  }
  for (let i=0;i<10;i++) for (const side of sides) if(side[i]) neighbors.push(side[i]);
  const p=byId.get(current);
  const url=p && !p.missing && p.kind!=='video' ? photoURL(current,true) : null;
  largePreviews.setWindow(url,neighbors);
}
function durationLabel(seconds) {
  if(!Number.isFinite(seconds)) return 'Видео';
  const n=Math.max(0,Math.round(seconds)), h=Math.floor(n/3600), m=Math.floor(n/60)%60, sec=String(n%60).padStart(2,'0');
  return h ? `${h}:${String(m).padStart(2,'0')}:${sec}` : `${m}:${sec}`;
}
function videoBadge(id) {const p=byId.get(id);if(p?.missing)return '<span class="missing-badge">Недоступен</span>';return p?.kind==='video'?`<span class="video-badge">▶ ${durationLabel(p.duration)}</span>`:'';}
const dayFormat = new Intl.DateTimeFormat('ru', {day:'numeric',month:'long',weekday:'long'});
function dateLabel(date) { if(!date)return 'Дата неизвестна';return dayFormat.format(new Date(date + 'T12:00:00')); }
function toast(message) { $('#toast').textContent=message; $('#toast').hidden=false; clearTimeout(toastTimer); toastTimer=setTimeout(()=>$('#toast').hidden=true,4500); }
async function api(path, data) {
  const response=await fetch(path, data === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json','X-Local-Token':token},body:JSON.stringify(data)});
  const result=await response.json();
  if(!response.ok) { const error=new Error(result.error || 'Ошибка сервера'); error.status=response.status; throw error; }
  return result;
}
function changed() {
  dirty=true; $('#saveStatus').textContent='Сохраняем…'; $('#saveStatus').classList.remove('error');
  clearTimeout(saveTimer); saveTimer=setTimeout(()=>flush().catch(()=>{}),350);
}
async function flush() {
  clearTimeout(saveTimer);
  if(blocked) throw new Error('Обновите страницу: проект изменён в другой вкладке');
  if(saving) { await saving; if(dirty) return flush(); return; }
  saving=(async()=>{
    while(dirty) {
      dirty=false;
      const snapshot=structuredClone(state);
      try { const result=await api('/api/state',snapshot); state.revision=result.revision; }
      catch(error) { dirty=true; if(error.status===409) blocked=true; $('#saveStatus').textContent='Не сохранено — нажмите для повтора'; $('#saveStatus').classList.add('error'); toast(error.message); throw error; }
    }
    $('#saveStatus').textContent='✓ Сохранено на диске'; $('#saveStatus').classList.remove('error');
  })();
  try { await saving; } finally { saving=null; }
}
function newPost() {
  const p={id:crypto.randomUUID(),title:`Пост ${state.posts.length+1}`,caption:'',photos:[]};
  state.posts.push(p); state.active=p.id; changed(); render();
}
function startNext() {
  if(!currentPost()?.photos.length) return toast('Добавьте хотя бы один файл в текущий пост');
  newPost(); toast('Новый пост готов к сборке');
}
function usedIn(id) { return state.posts.filter(p=>p.photos.includes(id)); }
function updateCounts() {
  $('#photoCount').textContent=photos.filter(p=>!p.missing).length;
  const missing=photos.filter(p=>p.missing).length;
  $('#libraryNotice').hidden=!missing;
  $('#libraryNotice').textContent=`Недоступных файлов: ${missing}. Верните файлы в исходную папку и нажмите «Обновить библиотеку» или уберите их из постов. Посты и подписи сохранены.`;
  $('#postCount').textContent=state.posts.filter(p=>p.photos.length).length;
  $('#hiddenCount').textContent=state.hidden.length;
  $('#stats').innerHTML=`<strong>${photos.length.toLocaleString('ru')}</strong>файлов в медиатеке`;
}
function render() { updateCounts(); renderSidebar(); renderContent(); if(current) renderViewer(); }
function renderContent() {
  document.querySelectorAll('[data-tab]').forEach(b=>b.classList.toggle('active',b.dataset.tab===tab));
  $('#gallery').hidden=tab==='posts'; $('#photoToolbar').hidden=tab==='posts'; $('#postList').hidden=tab!=='posts';
  if(tab==='posts') renderPosts(); else renderGallery();
}
function renderGallery() {
  const hidden=new Set(state.hidden), selected=new Set(currentPost()?.photos || []), day=$('#day').value;
  const used=new Map(); state.posts.forEach((p,n)=>p.photos.forEach(id=>{if(!used.has(id)) used.set(id,[]);used.get(id).push(n+1);}));
  visible=photos.filter(p=>(tab==='hidden'?hidden.has(p.id):!hidden.has(p.id)) && (!day || p.date.startsWith(day)) && (!$('#unused').checked || !used.has(p.id)));
  $('#visibleCount').textContent=`${visible.length} файлов`;
  let last=null, html='';
  visible.forEach(p=>{
    const date=p.date.slice(0,10);
    if(date!==last) { if(last!==null) html+='</div>'; html+=`<h3 class="day-title">${esc(dateLabel(date))}</h3><div class="grid">`;last=date; }
    html+=`<article class="photo ${selected.has(p.id)?'selected':''}" data-photo="${p.id}"><button class="open-photo" data-open="${p.id}" aria-label="Открыть ${esc(p.name)}"><img decoding="async" data-preview="${photoURL(p.id)}" alt="${esc(p.name)}">${videoBadge(p.id)}</button><button class="select-photo" ${p.missing&&!selected.has(p.id)?'disabled':''} data-pick="${p.id}" aria-label="Выбрать файл" aria-pressed="${selected.has(p.id)}">${selected.has(p.id)?currentPost().photos.indexOf(p.id)+1:'＋'}</button>${used.has(p.id)?`<span class="used-badge">Пост ${used.get(p.id).join(', ')}</span>`:''}<div class="photo-meta"><span>${p.date?p.date.slice(8,10)+'.'+p.date.slice(5,7)+' · '+p.date.slice(11,16):'Дата неизвестна'}</span><button class="hide-card" data-hide="${p.id}">${hidden.has(p.id)?'Вернуть':'Скрыть'}</button></div></article>`;
  });
  $('#gallery').innerHTML=html+(last!==null?'</div>':'<div class="empty"><strong>Здесь пока нет фото и видео</strong>Измените фильтры или верните скрытые кадры.</div>');
  previews.refresh();
}
function renderSidebar() {
  const p=currentPost();
  $('#postSelect').innerHTML=state.posts.map((x,n)=>`<option value="${x.id}">${n+1}. ${esc(x.title || 'Без названия')} · ${x.photos.length} файлов</option>`).join('');
  $('#postSelect').value=state.active;
  $('#title').value=p?.title || ''; $('#caption').value=p?.caption || '';
  $('#selectionCount').textContent=`${p?.photos.length || 0} / 10`;
  let html=(p?.photos || []).map((id,n)=>`<div class="mini" draggable="true" data-mini="${id}" title="Перетащите для перестановки. Alt + ← / → на миниатюре — переместить."><button class="mini-open" data-open="${id}" data-reorder="${id}" aria-label="Файл ${n+1}: ${esc(byId.get(id).name)}"><img data-preview="${photoURL(id)}" alt="" draggable="false">${videoBadge(id)}</button><span class="number">${n+1}</span><button class="remove" data-pick="${id}" aria-label="Убрать файл ${n+1}">×</button></div>`).join('');
  for(let i=p?.photos.length || 0;i<10;i++) html+='<div class="slot">＋</div>';
  $('#selection').innerHTML=html;
  previews.refresh();
  captionCount();
}
function captionCount() { const length=currentPost()?.caption.length || 0; $('#captionCount').textContent=`${length} / 1024`; $('#captionCount').classList.toggle('over',length>1024); $('#nextPost').disabled=!currentPost()?.photos.length; }
function renderPosts() {
  $('#postList').innerHTML=state.posts.map((p,n)=>`<article class="post-card ${p.id===state.active?'current':''}"><div class="post-card-head"><h3><span class="post-number">${String(n+1).padStart(2,'0')}</span>${esc(p.title || 'Без названия')}</h3><div class="post-actions"><button data-post-up="${p.id}" ${n===0?'disabled':''} aria-label="Переместить пост вверх">↑</button><button data-post-down="${p.id}" ${n===state.posts.length-1?'disabled':''} aria-label="Переместить пост вниз">↓</button><button data-edit="${p.id}">Открыть</button><button data-delete="${p.id}" aria-label="Удалить черновик">×</button></div></div><div class="post-strip">${p.photos.map(id=>`<button data-open="${id}"><img decoding="async" data-preview="${photoURL(id)}" alt="${esc(byId.get(id).name)}">${videoBadge(id)}</button>`).join('')}</div>${p.photos.length?'':'<div class="empty">Пока без файлов</div>'}<p class="post-caption">${esc(p.caption || 'Без подписи')}</p></article>`).join('');
  previews.refresh();
}
function pick(id) {
  if(blocked) return toast('Обновите страницу перед продолжением');
  const p=currentPost(); if(!p) return;
  const i=p.photos.indexOf(id);
  if(i>=0) p.photos.splice(i,1);
  else { if(byId.get(id)?.missing)return toast('Недоступный файл нельзя добавить в пост. Верните его и обновите библиотеку.'); if(p.photos.length>=10) return toast('В посте уже 10 файлов. Создайте следующий пост или уберите один кадр.'); p.photos.push(id); }
  changed(); render();
}
function hide(id) {
  const i=state.hidden.indexOf(id);
  if(i>=0) state.hidden.splice(i,1); else state.hidden.push(id);
  const wasCurrent=current===id, index=viewerIds.indexOf(id);
  if(wasCurrent) { viewerIds=viewerIds.filter(x=>x!==id); current=viewerIds[Math.min(index,viewerIds.length-1)] || null; if(!current) closeViewer(); }
  changed();render();
  toast(i>=0?'Файл возвращён в общую ленту':'Файл скрыт. Вернуть его можно во вкладке «Скрытые».');
}
function openViewer(id) {
  let ids=tab==='posts'?state.posts.flatMap(p=>p.photos):visible.map(p=>p.id);
  if(!ids.includes(id)) ids=currentPost()?.photos.includes(id)?currentPost().photos:photos.map(p=>p.id);
  viewerIds=[...new Set(ids)]; current=id;
  $('#viewer').hidden=false; renderViewer(); $('#closeViewer').focus({preventScroll:true});
}
function closeViewer() { finishLargeLoading(); ++largeDisplayRequest; largePreviews.close(); stopVideo(); const old=current; current=null; $('#viewer').hidden=true; previews.schedule(); const b=document.querySelector(`[data-photo="${old}"] .open-photo`); b?.focus({preventScroll:true}); }
function stopVideo() { const v=$('#largeVideo'); v.pause(); if(v.hasAttribute('src')) {v.removeAttribute('src');v.load();} }
function renderViewer() {
  const p=byId.get(current); if(!p) return;
  ++largeDisplayRequest;
  const img=$('#largePhoto');
  const video=$('#largeVideo'), isVideo=p.kind==='video'&&!p.missing;
  $('#missingMessage').hidden=!p.missing;
  $('#videoPanel').hidden=!isVideo;
  const showPrevious=!isVideo && !p.missing && img.complete && img.naturalWidth>0;
  img.hidden=!showPrevious;
  finishLargeLoading();
  img.setAttribute('aria-hidden','true');
  if (!isVideo && !p.missing) {
    const requested=current;
    // Cached images usually decode within a frame: avoid flashing the overlay.
    largeLoadingTimer=setTimeout(()=>{
      if (current!==requested || $('#viewer').hidden) return;
      img.classList.toggle('large-loading',showPrevious);
      $('#largeLoading').hidden=false;
    },30);
  }
  $('#largeError').hidden=true;
  if(isVideo) {
    $('#largeError').hidden=true;
    const videoURL='/video/'+current;
    if(video.getAttribute('src')!==videoURL) {
      stopVideo(); $('#videoError').hidden=true; video.hidden=false; video.poster=photoURL(current); video.src=videoURL;
    }
  } else {
    stopVideo();
  }
  if(p.missing){stopVideo();img.hidden=true;$('#largeError').hidden=true;}
  img.alt=p.name;
  prepareLargePreviews();
  $('#viewerName').textContent=p.name;
  const used=usedIn(current).map(x=>x.title || 'Без названия');
  $('#viewerInfo').textContent=`${dateLabel(p.date.slice(0,10))} · ${p.date.slice(11,16)}${isVideo?' · ▶ '+durationLabel(p.duration):''}${used.length?' · В постах: '+used.join(', '):''}${p.dateSource==='file'?' · Дата изменения файла':''}`;
  const i=viewerIds.indexOf(current); $('#viewerPosition').textContent=`${i+1} / ${viewerIds.length}`;
  $('#previous').disabled=i<=0; $('#following').disabled=i>=viewerIds.length-1;
  const chosen=currentPost().photos.includes(current);
  $('#pickPhoto').disabled=p.missing&&!chosen;
  $('#pickPhoto').textContent=chosen?'✓ Убрать из поста · Пробел':'＋ В текущий пост · Пробел';
  $('#pickPhoto').setAttribute('aria-pressed',chosen);
  $('#hidePhoto').textContent=state.hidden.includes(current)?'Вернуть · H':'Скрыть · H';
}
function move(delta) { const next=viewerIds[viewerIds.indexOf(current)+delta]; if(next){current=next;renderViewer();} }
function reorder(from,to) {
  const p=currentPost(), a=p.photos.indexOf(from), b=p.photos.indexOf(to);
  if(a<0||b<0||a===b)return; p.photos.splice(a,1);p.photos.splice(b,0,from);changed();render();
}
function movePost(id,delta) { const i=state.posts.findIndex(p=>p.id===id), j=i+delta; if(j<0||j>=state.posts.length)return; const [p]=state.posts.splice(i,1);state.posts.splice(j,0,p);changed();render(); }
function bind() {
  document.addEventListener('click',e=>{
    const b=e.target.closest('button');if(!b)return;
    if(b.dataset.open)openViewer(b.dataset.open);
    else if(b.dataset.pick)pick(b.dataset.pick);
    else if(b.dataset.hide)hide(b.dataset.hide);
    else if(b.dataset.tab){closeViewer();tab=b.dataset.tab;renderContent();}
    else if(b.dataset.edit){state.active=b.dataset.edit;changed();render();}
    else if(b.dataset.postUp)movePost(b.dataset.postUp,-1);
    else if(b.dataset.postDown)movePost(b.dataset.postDown,1);
    else if(b.dataset.delete){if(!confirm('Удалить черновик поста? Исходные файлы останутся на месте.'))return;state.posts=state.posts.filter(p=>p.id!==b.dataset.delete);if(state.active===b.dataset.delete)state.active=state.posts[0]?.id || null;if(!state.posts.length)newPost();else{changed();render();}}
  });
  $('#newPost').onclick=()=>{if(!currentPost()?.photos.length){toast('Текущий пост уже пустой — можно добавлять фото и видео');return;}newPost();};
  $('#nextPost').onclick=startNext;
  $('#postSelect').onchange=e=>{state.active=e.target.value;changed();render();};
  $('#title').oninput=e=>{currentPost().title=e.target.value;const option=$('#postSelect').selectedOptions[0];if(option)option.textContent=`${state.posts.indexOf(currentPost())+1}. ${e.target.value || 'Без названия'} · ${currentPost().photos.length} файлов`;changed();if(tab==='posts')renderPosts();};
  $('#caption').oninput=e=>{currentPost().caption=e.target.value;captionCount();changed();if(tab==='posts')renderPosts();};
  $('#day').onchange=renderGallery;$('#unused').onchange=renderGallery;
  $('#closeViewer').onclick=closeViewer;$('#previous').onclick=()=>move(-1);$('#following').onclick=()=>move(1);$('#pickPhoto').onclick=()=>pick(current);$('#hidePhoto').onclick=()=>hide(current);
  $('#openVideo').onclick=()=>api('/api/open-video',{id:current}).catch(e=>toast(e.message));
  $('#largeVideo').onerror=()=>{if(!$('#largeVideo').hasAttribute('src'))return;$('#videoError').hidden=false;$('#largeVideo').hidden=true;};
  $('#largePhoto').onerror=()=>{$('#largeError').hidden=false;$('#largePhoto').hidden=true;};
  document.addEventListener('keydown',e=>{
    if(e.target.matches('input,textarea,select,video')||$('#exportDialog').open||$('#cacheDialog').open)return;
    if(e.altKey&&e.target.dataset.reorder&&['ArrowLeft','ArrowRight'].includes(e.key)) {e.preventDefault();const id=e.target.dataset.reorder,p=currentPost(),i=p.photos.indexOf(id),to=p.photos[i+(e.key==='ArrowRight'?1:-1)];if(to){reorder(id,to);document.querySelector(`[data-reorder="${id}"]`)?.focus();}return;}
    if(!current||e.ctrlKey||e.metaKey||e.altKey)return;
    if(e.key==='Escape'){e.preventDefault();closeViewer();}
    if(e.key==='ArrowLeft'){e.preventDefault();move(-1);}
    if(e.key==='ArrowRight'){e.preventDefault();move(1);}
    if(e.code==='Space'){e.preventDefault();pick(current);}
    if(e.code==='KeyH'){e.preventDefault();hide(current);}
  });
  const sel=$('#selection');
  sel.addEventListener('dragstart',e=>{const mini=e.target.closest('[data-mini]');if(!mini)return;dragId=mini.dataset.mini;e.dataTransfer.setData('text/plain',dragId);e.dataTransfer.effectAllowed='move';});
  sel.addEventListener('dragover',e=>{const mini=e.target.closest('[data-mini]');if(mini){e.preventDefault();mini.classList.add('drag-over');}});
  sel.addEventListener('dragleave',e=>e.target.closest('[data-mini]')?.classList.remove('drag-over'));
  sel.addEventListener('drop',e=>{e.preventDefault();const mini=e.target.closest('[data-mini]');if(mini&&dragId)reorder(dragId,mini.dataset.mini);dragId=null;});
  sel.addEventListener('dragend',()=>{dragId=null;document.querySelectorAll('.drag-over').forEach(x=>x.classList.remove('drag-over'));});
  $('#saveStatus').onclick=()=>flush().catch(()=>{});
  $('#export').onclick=exportPosts;
  $('#cacheSettings').onclick=openCache;
  $('#closeCache').onclick=()=>$('#cacheDialog').close();
  $('#cacheDialog').addEventListener('close',()=>clearTimeout(cacheStatsTimer));
  $('#saveCacheLimit').onclick=()=>changeCache(false);
  $('#clearCache').onclick=()=>changeCache(true);
  $('#refreshLibrary').onclick=refreshLibrary;
  $('#closeExport').onclick=()=>$('#exportDialog').close();
  $('#reveal').onclick=()=>api('/api/reveal',{id:exportId}).catch(e=>toast(e.message));
  window.addEventListener('beforeunload',e=>{if(dirty||saving){e.preventDefault();e.returnValue='';}});
  document.addEventListener('visibilitychange',()=>{if(document.hidden&&dirty)flush().catch(()=>{});});
}
function formatBytes(bytes) {
  if(bytes < 1024)return `${bytes} Б`;
  if(bytes < 1024**2)return `${(bytes/1024).toFixed(1)} КБ`;
  return bytes >= 1024**3 ? `${(bytes/1024**3).toFixed(2)} ГБ` : `${(bytes/1024**2).toFixed(1)} МБ`;
}
function showCacheStats(stats, updateLimit=true) {
  $('#cacheStats').textContent=`Занято ${formatBytes(stats.bytes)} из ${formatBytes(stats.limit_bytes)} · файлов: ${stats.files}`;
  const groups=stats.groups || {};
  $('#cacheBreakdown').innerHTML=[['thumbnails','Миниатюры · 480 px'],['large','Крупные превью · 1800 px'],['metadata','Данные длительности видео']].map(([key,label])=>{
    const group=groups[key] || {files:0,bytes:0};
    return `<div class="cache-row"><span>${label}</span><strong>${group.files} · ${formatBytes(group.bytes)}</strong></div>`;
  }).join('');
  const page=previews.pageStats();
  $('#pageCacheStats').textContent=`Загружено этой страницей: ${page.loaded} миниатюр. В быстром кэше страницы: ${page.retained} из ${page.limit}.`;
  if(!updateLimit)return;
  const select=$('#cacheLimit');
  if(![...select.options].some(option=>Number(option.value)===stats.limit_bytes)) {
    const option=new Option(formatBytes(stats.limit_bytes),String(stats.limit_bytes));select.add(option);
  }
  select.value=String(stats.limit_bytes);
}
async function openCache() {
  $('#cacheDialog').showModal();$('#cacheStats').textContent='Загрузка…';$('#cacheBreakdown').replaceChildren();
  try {showCacheStats(await api('/api/cache'));} catch(e) {$('#cacheStats').textContent=e.message;}
  clearTimeout(cacheStatsTimer);
  if($('#cacheDialog').open)cacheStatsTimer=setTimeout(refreshCacheStats,1500);
}
async function refreshCacheStats() {
  if(!$('#cacheDialog').open)return;
  try {
    if(!$('#clearCache').disabled) {
      const stats=await api('/api/cache');
      if($('#cacheDialog').open&&!$('#clearCache').disabled)showCacheStats(stats,false);
    }
  } catch { /* Keep the last reading while the server restarts. */ }
  finally {if($('#cacheDialog').open)cacheStatsTimer=setTimeout(refreshCacheStats,1500);}
}
async function changeCache(clear) {
  $('#clearCache').disabled=true;$('#saveCacheLimit').disabled=true;$('#cacheLimit').disabled=true;
  previews.paused=true;largePreviews.paused=true;
  try {
    if(clear) {
      $('#cacheStats').textContent='Завершаем текущие загрузки и очищаем кэш…';
      while(previews.active.size || largePreviews.active.size) await new Promise(resolve=>setTimeout(resolve,100));
    }
    const result=await api(clear?'/api/cache/clear':'/api/cache/limit',clear?{}:{limit_bytes:Number($('#cacheLimit').value)});
    showCacheStats(result);
    if(clear) {
      previews.backgroundPaused=true;largePreviews.backgroundPaused=true;
      $('#cacheNote').textContent='Кэш очищен. Открытые картинки остались в памяти. Превью будут создаваться по мере просмотра; фоновая подготовка возобновится после перезагрузки страницы.';
    }
    toast(clear?'Дисковый кэш очищен':'Лимит кэша сохранён');
  } catch(e) {toast(e.message);$('#cacheStats').textContent=e.message;}
  finally {
    previews.paused=false;previews.schedule();largePreviews.paused=false;largePreviews.pump();
    $('#clearCache').disabled=false;$('#saveCacheLimit').disabled=false;$('#cacheLimit').disabled=false;
  }
}
async function exportPosts() {
  if(!state.posts.some(p=>p.photos.length))return toast('Сначала выберите фото или видео для поста');
  if(state.posts.some(p=>p.photos.length&&p.caption.length>1024))return toast('Сократите подписи до 1024 символов перед экспортом');
  $('#export').disabled=true;
  try {
    await flush();
    $('#exportDialog').showModal();$('#exportTitle').textContent='Собираем папки…';$('#exportDescription').textContent='Копируем оригинальные файлы в порядке постов.';$('#exportPath').textContent='';$('#reveal').hidden=true;$('#exportProgress').value=0;
    const result=await api('/api/export',{});exportId=result.id;
    for(;;) {
      const job=await api('/api/job/'+exportId);$('#exportProgress').max=job.total;$('#exportProgress').value=job.done;
      $('#exportDescription').textContent=`Скопировано ${job.done} из ${job.total} оригиналов`;
      if(job.status==='error')throw new Error(job.error);
      if(job.status==='done') {$('#exportTitle').textContent='Посты готовы к отправке';$('#exportDescription').textContent='В каждой папке — оригинальные фото и видео с номерами. «Подпись.txt» создаётся только для непустой подписи. Проверьте порядок кадров при загрузке в Telegram.';$('#exportPath').textContent=job.path;$('#reveal').hidden=false;break;}
      await new Promise(r=>setTimeout(r,500));
    }
  } catch(e) {toast(e.message);if($('#exportDialog').open){$('#exportTitle').textContent='Экспорт не завершён';$('#exportDescription').textContent=e.message;}}
  finally {$('#export').disabled=false;}
}
function updateDays() {
  const selected=$('#day').value;
  const days=[...new Set(photos.map(p=>p.date.slice(0,10)).filter(Boolean))];
  $('#day').innerHTML='<option value="">Все даты</option>'+days.map(d=>`<option value="${d}">${esc(dateLabel(d))}</option>`).join('');
  $('#day').value=days.includes(selected)?selected:'';
}
async function refreshLibrary() {
  const button=$('#refreshLibrary');button.disabled=true;button.textContent='Обновляем…';
  try {
    await flush();
    const previous=new Map(photos.map(p=>[p.id,p]));
    const data=await api('/api/refresh',{});
    photos=data.photos;byId=new Map(photos.map(p=>[p.id,p]));
    previews.failed.clear();updateDays();render();
    if(current) {
      const ids=tab==='posts'?state.posts.flatMap(p=>p.photos):visible.map(p=>p.id);
      viewerIds=[...new Set(ids.includes(current)?ids:[current,...ids])];renderViewer();
    }
    const added=photos.filter(p=>!p.missing&&!previous.has(p.id)).length;
    const restored=photos.filter(p=>!p.missing&&previous.get(p.id)?.missing).length;
    toast(`Библиотека обновлена. Новых: ${added}, восстановлено: ${restored}, недоступно: ${photos.filter(p=>p.missing).length}.`);
  } catch(e) {toast(e.message);}
  finally {button.disabled=false;button.textContent='↻ Обновить библиотеку';}
}
async function init() {
  try {
    const data=await api('/api/library');photos=data.photos;byId=new Map(photos.map(p=>[p.id,p]));state=data.state;token=data.token;
    $('#folder').textContent=data.folder;
    updateDays();
    bind();if(!state.posts.length)newPost();else{if(!currentPost()){state.active=state.posts[0].id;changed();}render();$('#saveStatus').textContent='✓ Сохранено на диске';}
  } catch(e){$('main').innerHTML=`<p class="fatal">Не удалось открыть библиотеку: ${esc(e.message)}<br>Перезапустите приложение и обновите страницу.</p>`;}
}
init();

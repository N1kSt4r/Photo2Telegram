'use strict';
const $ = s => document.querySelector(s);
const esc = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let photos = [], byId, state, token, tab = 'photos', current = null, viewerIds = [], visible = [];
let dirty = false, saving = null, saveTimer, toastTimer, blocked = false, dragId = null, exportId;
const currentPost = () => state.posts.find(p => p.id === state.active);
const photoURL = (id, large=false) => `/photo/${id}${large ? '?size=large' : ''}`;
function durationLabel(seconds) {
  if(!Number.isFinite(seconds)) return 'Видео';
  const n=Math.max(0,Math.round(seconds)), h=Math.floor(n/3600), m=Math.floor(n/60)%60, sec=String(n%60).padStart(2,'0');
  return h ? `${h}:${String(m).padStart(2,'0')}:${sec}` : `${m}:${sec}`;
}
function videoBadge(id) {const p=byId.get(id);return p?.kind==='video'?`<span class="video-badge">▶ ${durationLabel(p.duration)}</span>`:'';}
const dayFormat = new Intl.DateTimeFormat('ru', {day:'numeric',month:'long',weekday:'long'});
function dateLabel(date) { return dayFormat.format(new Date(date + 'T12:00:00')); }
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
  $('#photoCount').textContent=photos.length;
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
  let last='', html='';
  visible.forEach(p=>{
    const date=p.date.slice(0,10);
    if(date!==last) { if(last) html+='</div>'; html+=`<h3 class="day-title">${esc(dateLabel(date))}</h3><div class="grid">`;last=date; }
    html+=`<article class="photo ${selected.has(p.id)?'selected':''}" data-photo="${p.id}"><button class="open-photo" data-open="${p.id}" aria-label="Открыть ${esc(p.name)}"><img loading="lazy" decoding="async" src="${photoURL(p.id)}" alt="${esc(p.name)}">${videoBadge(p.id)}</button><button class="select-photo" data-pick="${p.id}" aria-label="Выбрать файл" aria-pressed="${selected.has(p.id)}">${selected.has(p.id)?currentPost().photos.indexOf(p.id)+1:'＋'}</button>${used.has(p.id)?`<span class="used-badge">Пост ${used.get(p.id).join(', ')}</span>`:''}<div class="photo-meta"><span>${p.date.slice(8,10)}.${p.date.slice(5,7)} · ${p.date.slice(11,16)}</span><button class="hide-card" data-hide="${p.id}">${hidden.has(p.id)?'Вернуть':'Скрыть'}</button></div></article>`;
  });
  $('#gallery').innerHTML=html+(last?'</div>':'<div class="empty"><strong>Здесь пока нет фото и видео</strong>Измените фильтры или верните скрытые кадры.</div>');
}
function renderSidebar() {
  const p=currentPost();
  $('#postSelect').innerHTML=state.posts.map((x,n)=>`<option value="${x.id}">${n+1}. ${esc(x.title || 'Без названия')} · ${x.photos.length} файлов</option>`).join('');
  $('#postSelect').value=state.active;
  $('#title').value=p?.title || ''; $('#caption').value=p?.caption || '';
  $('#selectionCount').textContent=`${p?.photos.length || 0} / 10`;
  let html=(p?.photos || []).map((id,n)=>`<div class="mini" draggable="true" data-mini="${id}" title="Перетащите для перестановки. Alt + ← / → на миниатюре — переместить."><button class="mini-open" data-open="${id}" data-reorder="${id}" aria-label="Файл ${n+1}: ${esc(byId.get(id).name)}"><img src="${photoURL(id)}" alt="" draggable="false">${videoBadge(id)}</button><span class="number">${n+1}</span><button class="remove" data-pick="${id}" aria-label="Убрать файл ${n+1}">×</button></div>`).join('');
  for(let i=p?.photos.length || 0;i<10;i++) html+='<div class="slot">＋</div>';
  $('#selection').innerHTML=html;
  captionCount();
}
function captionCount() { const length=currentPost()?.caption.length || 0; $('#captionCount').textContent=`${length} / 1024`; $('#captionCount').classList.toggle('over',length>1024); $('#nextPost').disabled=!currentPost()?.photos.length; }
function renderPosts() {
  $('#postList').innerHTML=state.posts.map((p,n)=>`<article class="post-card ${p.id===state.active?'current':''}"><div class="post-card-head"><h3><span class="post-number">${String(n+1).padStart(2,'0')}</span>${esc(p.title || 'Без названия')}</h3><div class="post-actions"><button data-post-up="${p.id}" ${n===0?'disabled':''} aria-label="Переместить пост вверх">↑</button><button data-post-down="${p.id}" ${n===state.posts.length-1?'disabled':''} aria-label="Переместить пост вниз">↓</button><button data-edit="${p.id}">Открыть</button><button data-delete="${p.id}" aria-label="Удалить черновик">×</button></div></div><div class="post-strip">${p.photos.map(id=>`<button data-open="${id}"><img loading="lazy" src="${photoURL(id)}" alt="${esc(byId.get(id).name)}">${videoBadge(id)}</button>`).join('')}</div>${p.photos.length?'':'<div class="empty">Пока без файлов</div>'}<p class="post-caption">${esc(p.caption || 'Без подписи')}</p></article>`).join('');
}
function pick(id) {
  if(blocked) return toast('Обновите страницу перед продолжением');
  const p=currentPost(); if(!p) return;
  const i=p.photos.indexOf(id);
  if(i>=0) p.photos.splice(i,1);
  else { if(p.photos.length>=10) return toast('В посте уже 10 файлов. Создайте следующий пост или уберите один кадр.'); p.photos.push(id); }
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
function closeViewer() { stopVideo(); const old=current; current=null; $('#viewer').hidden=true; const b=document.querySelector(`[data-photo="${old}"] .open-photo`); b?.focus({preventScroll:true}); }
function stopVideo() { const v=$('#largeVideo'); v.pause(); if(v.hasAttribute('src')) {v.removeAttribute('src');v.load();} }
function renderViewer() {
  const p=byId.get(current); if(!p) return;
  const img=$('#largePhoto'), url=photoURL(current,true);
  const video=$('#largeVideo'), isVideo=p.kind==='video';
  $('#videoPanel').hidden=!isVideo; img.hidden=isVideo;
  if(isVideo) {
    $('#largeError').hidden=true;
    const videoURL='/video/'+current;
    if(video.getAttribute('src')!==videoURL) {
      stopVideo(); $('#videoError').hidden=true; video.hidden=false; video.poster=photoURL(current); video.src=videoURL;
    }
  } else {
    stopVideo();
    if(img.getAttribute('src')!==url) { $('#largeError').hidden=true; img.src=url; }
  }
  img.alt=p.name;
  $('#viewerName').textContent=p.name;
  const used=usedIn(current).map(x=>x.title || 'Без названия');
  $('#viewerInfo').textContent=`${dateLabel(p.date.slice(0,10))} · ${p.date.slice(11,16)}${isVideo?' · ▶ '+durationLabel(p.duration):''}${used.length?' · В постах: '+used.join(', '):''}${p.dateSource==='file'?' · Дата изменения файла':''}`;
  const i=viewerIds.indexOf(current); $('#viewerPosition').textContent=`${i+1} / ${viewerIds.length}`;
  $('#previous').disabled=i<=0; $('#following').disabled=i>=viewerIds.length-1;
  const chosen=currentPost().photos.includes(current);
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
    if(e.target.matches('input,textarea,select,video')||$('#exportDialog').open)return;
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
  $('#closeExport').onclick=()=>$('#exportDialog').close();
  $('#reveal').onclick=()=>api('/api/reveal',{id:exportId}).catch(e=>toast(e.message));
  window.addEventListener('beforeunload',e=>{if(dirty||saving){e.preventDefault();e.returnValue='';}});
  document.addEventListener('visibilitychange',()=>{if(document.hidden&&dirty)flush().catch(()=>{});});
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
async function init() {
  try {
    const data=await api('/api/library');photos=data.photos;byId=new Map(photos.map(p=>[p.id,p]));state=data.state;token=data.token;
    $('#folder').textContent=data.folder;
    const days=[...new Set(photos.map(p=>p.date.slice(0,10)))];$('#day').innerHTML='<option value="">Все даты</option>'+days.map(d=>`<option value="${d}">${esc(dateLabel(d))}</option>`).join('');
    bind();if(!state.posts.length)newPost();else{if(!currentPost()){state.active=state.posts[0].id;changed();}render();$('#saveStatus').textContent='✓ Сохранено на диске';}
  } catch(e){$('main').innerHTML=`<p class="fatal">Не удалось открыть библиотеку: ${esc(e.message)}<br>Перезапустите приложение и обновите страницу.</p>`;}
}
init();

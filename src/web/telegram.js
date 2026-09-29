'use strict';
let telegramTimer=null, telegramJobId=null, telegramSettingsLoaded=false, telegramActionBusy=false, telegramLastData=null;
let telegramSelectionScope=null, telegramSelectionDefault=null;
const telegramSelection=new Map();
const telegramLabels={cached:'Готов в кэше',sending:'Отправляется',sent:'Отправлен',unknown:'Результат неизвестен',error:'Ошибка',retry:'Повтор разрешён'};
async function telegramAPI(path,data){
  const controller=new AbortController();
  const timer=setTimeout(()=>controller.abort(),path.endsWith('/check')?65000:15000);
  try{
    const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-Local-Token':token},body:JSON.stringify(data),signal:controller.signal});
    const result=await response.json();
    if(!response.ok){const error=new Error(result.error||'Ошибка сервера');error.status=response.status;throw error;}
    return result;
  }catch(error){
    if(error.name==='AbortError')throw new Error('Сервер долго не отвечает. Операция могла продолжиться на сервере; дождитесь обновления статуса.');
    throw error;
  }finally{clearTimeout(timer);}
}
function telegramError(error){
  $('#telegramNotice').textContent=error.status===409?'Сервер перезапущен или библиотека изменилась. Обновите страницу, чтобы продолжить.':error.message;
  $('#telegramReload').hidden=error.status!==409;
  $('#telegramNotice').scrollIntoView({block:'nearest'});
}
function telegramModeChanged(){
  $('#telegramEndpointLabel').hidden=$('#telegramMode').value!=='local';
  $('#telegramContainer').hidden=$('#telegramMode').value!=='local';
}
function telegramConfig(){return {mode:$('#telegramMode').value,endpoint:$('#telegramEndpoint').value,
  token:$('#telegramToken').value,channel:$('#telegramChannel').value,silent:$('#telegramSilent').checked};}
async function saveTelegram(){
  const data=await telegramAPI('/api/telegram/settings',telegramConfig());
  $('#telegramToken').value='';$('#telegramToken').placeholder=data.has_token?'Токен сохранён локально; пустое поле оставляет его прежним':'Токен от @BotFather';
  return data;
}
function shortTelegramError(file){
  let message=file.error||'';
  if(message.startsWith(file.name+':'))message=message.slice(file.name.length+1).trim();
  if(/превышает лимит|превышает.*МБ/i.test(message))return 'Превышает лимит';
  if(/исходный файл недоступен|исходник.*недоступен/i.test(message))return 'Исходный файл недоступен';
  if(/FFmpeg.*не смог|не смог.*MP4/i.test(message))return 'Не удалось преобразовать';
  if(/нужен FFmpeg/i.test(message))return 'Нужен FFmpeg';
  if(/изменился/i.test(message))return 'Исходный файл изменился';
  return message.split('. ')[0];
}
function telegramThumbnail(file,index,post){
  const original=(file.name.split('.').pop()||'').toUpperCase().replace('JPG','JPEG');
  const output=file.kind==='video'?'MP4':'JPEG';
  const format=original===output?output:`${original} → ${output}`;
  const photo=byId.get(file.id);
  const duration=file.kind==='video'&&photo?.duration?durationLabel(photo.duration):'';
  const fileState=file.status||(file.prepared_ref?'ready':'unprepared');
  const stateText={unprepared:'Не подготовлен',preparing:'Готовится…',error:'Ошибка'}[fileState]||'';
  const link=file.prepared_ref?`/telegram-file/${encodeURIComponent(file.prepared_ref)}?project=${encodeURIComponent(project)}`:null;
  return `<figure class="telegram-media ${stateText?'telegram-media-pending':''} ${fileState==='error'?'telegram-media-failed':''}" title="${esc(file.name)}${file.error?' · '+esc(file.error):''}${file.cached?' · Из кэша':''}${link?' · Открыть файл для отправки':''}">${!link&&post.status==='sent'&&post.link?`<a class="telegram-media-open" href="${esc(post.link)}" target="_blank" rel="noopener noreferrer">`:`<button class="telegram-media-open" data-output-post="${esc(post.id)}" data-output-index="${index}" aria-label="Открыть файл для отправки: ${esc(file.name)}">`}<img src="${esc(photo?photoURL(file.id):'/missing.svg')}" loading="lazy" decoding="async" alt="${esc(file.name)}"><span class="telegram-media-top">${esc(format)}${file.bytes==null?'':`<span class="telegram-media-size">${formatBytes(file.bytes)}</span>`}</span>${stateText?`<span class="telegram-media-state">${esc(stateText)}${fileState==='error'&&file.error?`<small>${esc(shortTelegramError(file))}</small>`:''}</span>`:''}<span class="telegram-media-number">${index+1}</span>${duration?`<span class="telegram-media-duration">▶ ${esc(duration)}</span>`:''}${!link&&post.status==='sent'&&post.link?'</a>':'</button>'}</figure>`;
}
function renderTelegram(data){
  if(telegramSelectionScope!==project){
    telegramSelectionScope=project;telegramSelection.clear();telegramSelectionDefault=null;
  }else{
    for(const input of document.querySelectorAll('[data-telegram-post]'))telegramSelection.set(input.dataset.telegramPost,input.checked);
  }
  telegramLastData=data;
  if(!telegramSettingsLoaded){
    const settings=data.settings;
    $('#telegramMode').value=settings.mode||'cloud';$('#telegramEndpoint').value=settings.endpoint&&settings.mode==='local'?settings.endpoint:'http://127.0.0.1:8081';
    $('#telegramChannel').value=settings.channel||'';$('#telegramSilent').checked=Boolean(settings.silent);
    $('#telegramToken').value='';$('#telegramToken').placeholder=settings.has_token?'Токен сохранён локально; пустое поле оставляет его прежним':'Токен от @BotFather';
    telegramModeChanged();telegramSettingsLoaded=true;
  }
  const job=data.job,busy=data.busy??Boolean(job&&['preparing','cancelling','ready','sending'].includes(job.status));
  telegramJobId=job?.id;
  $('#telegramConfig').disabled=Boolean(busy);
  $('#telegramPrepareCurrent').disabled=Boolean(busy);$('#telegramPrepareAll').disabled=Boolean(busy);
  $('#telegramJob').hidden=!job;
  if(job){
    const elapsed=job.status==='sending'&&job.transfer_started?Math.max(0,(Date.now()/1000-job.transfer_started)):0;
    const speed=job.upload_speed?` · ${formatBytes(job.upload_speed)}/с`:'';
    const resumable=job.status==='ready'||job.can_resume||job.items.some(p=>p.status==='cached');
    $('#telegramProgress').textContent=job.message;
    $('#telegramTransfer').textContent=job.upload_total?`Передано Bot API: ${formatBytes(job.uploaded)} / ${formatBytes(job.upload_total)}${speed}${job.upload_seconds?' · загрузка '+job.upload_seconds.toFixed(1)+' с':''}${elapsed?' · всего '+Math.floor(elapsed)+' с':''}`:'';
    const sentCount=job.items.filter(p=>p.status==='sent').length;
    const processedCount=job.items.filter(p=>['ready','cached','sent','sending','unknown'].includes(p.status)).length;
    const sending=['sending','done'].includes(job.status);
    $('#telegramProgressBar').max=Math.max(1,job.items.length);$('#telegramProgressBar').value=sending?sentCount:processedCount;
    $('#telegramProgressBar').setAttribute('aria-label',sending?'Отправлено постов':'Обработано постов');
    $('#telegramChannelStatus').textContent=job.channel_status==='checking'?'Канал: проверяется…':job.channel_status==='error'?`Канал: ${job.channel_error}. Повторите проверку подключения.`:job.channel_status==='ready'?'Канал: проверен':'';
    $('#telegramDestination').textContent=job.destination?`Канал: ${job.destination.channel} (${job.destination.chat_id}) · бот @${job.destination.bot} · ${job.mode==='local'?'локальный Bot API: '+job.endpoint:'обычный Bot API'}`:'';
    const hideSent=$('#telegramHideSent').checked;
    $('#telegramCounts').textContent=`${processedCount}/${job.items.length} обработано · ${sentCount}/${job.items.length} отправлено`;
    const review=JSON.stringify([job.id,job.status,job.items,hideSent,Boolean(busy)]);
    if($('#telegramReview').dataset.review!==review){
      $('#telegramReview').dataset.review=review;
      let blocked=false;
      const markup=job.items.map((post,n)=>{
        if(!['ready','cached','sent'].includes(post.status))blocked=true;
        if(hideSent&&post.status==='sent')return '';
        const selectable=resumable&&['ready','cached'].includes(post.status);
        const selected=telegramSelection.get(post.id)??telegramSelectionDefault??!blocked;
        return `<div class="telegram-post"><label class="telegram-post-heading">${selectable?`<input type="checkbox" data-telegram-post="${esc(post.id)}" ${selected?'checked':''} ${busy?'disabled':''}> `:''}<strong>${n+1}. ${post.link?`<a class="telegram-post-link" href="${esc(post.link)}" target="_blank" rel="noopener noreferrer">${esc(post.title||'Без названия')}</a>`:esc(post.title||'Без названия')} · ${esc({cached:'Готов в кэше',sending:'Отправляется',unknown:'Проверьте канал',unprepared:'Не подготовлен',preparing:'Готовится',ready:'Готов',error:'Ошибка',sent:'Уже отправлен'}[post.status]||post.status)}</strong></label>${post.error?`<p class="error">${esc(post.error)}</p>`:''}${post.caption?`<p>${esc(post.caption)}</p>`:''}<div class="telegram-media-grid">${post.files.map((file,index)=>telegramThumbnail(file,index,post)).join('')}</div>${post.resolve_key&&post.status==='unknown'?`<p>Проверьте канал и отметьте результат:</p><div class="telegram-actions"><button data-telegram-resolve="sent" data-key="${esc(post.resolve_key)}" ${busy?'disabled':''}>Пост есть в канале</button><button data-telegram-resolve="retry" data-key="${esc(post.resolve_key)}" ${busy?'disabled':''}>Проверил: поста нет</button></div>`:''}</div>`;
      }).join('');
      // Keep already decoded previews when statuses or sizes change.
      const images=new Map();
      for(const img of $('#telegramReview').querySelectorAll('img')){
        const key=img.getAttribute('src');
        if(!images.has(key))images.set(key,[]);
        images.get(key).push(img);
      }
      const template=document.createElement('template');template.innerHTML=markup||(hideSent&&job.items.length?'<div class="empty">Все посты в списке отправлены.<br><button data-show-sent>Показать отправленные</button></div>':'<div class="empty">Пока нет постов.</div>');
      for(const img of template.content.querySelectorAll('img')){
        const previous=images.get(img.getAttribute('src'))?.shift();
        if(previous)img.replaceWith(previous);
      }
      $('#telegramReview').replaceChildren(template.content);
    }
    $('#telegramPublish').hidden=!resumable;$('#telegramPublish').disabled=busy;
    updateTelegramSelectAll();
    $('#telegramPublishPrefix').hidden=!resumable;
    const firstBlocked=job.items.findIndex(p=>!['ready','cached','sent'].includes(p.status));
    $('#telegramPublishPrefix').disabled=busy||!job.items.slice(0,firstBlocked<0?job.items.length:firstBlocked).some(p=>['ready','cached'].includes(p.status));

    $('#telegramSnapshot').hidden=true;$('#telegramCancel').hidden=!busy;
    $('#telegramCancel').disabled=job.status==='cancelling';
    $('#telegramCancel').textContent=job.status==='cancelling'?'Останавливаем…':job.status==='sending'?'Остановить после текущего поста':'Остановить подготовку';
  }

  if(telegramContainerData&&$('#telegramMode').value==='local')renderTelegramContainer(telegramContainerData);
}
async function refreshTelegram(){
  clearTimeout(telegramTimer);
  if(!$('#telegramDialog').open||telegramActionBusy)return;
  try{const data=await telegramAPI('/api/telegram/status',{});if(!telegramActionBusy){renderTelegram(data);await refreshTelegramContainer();}}
  catch(e){telegramError(e);$('#telegramConfig').disabled=false;$('#telegramPrepareCurrent').disabled=false;$('#telegramPrepareAll').disabled=false;}
  finally{if($('#telegramDialog').open)telegramTimer=setTimeout(refreshTelegram,1000);}
}
async function telegramAction(action){
  if(telegramActionBusy)return;
  telegramActionBusy=true;
  clearTimeout(telegramTimer);
  $('#telegramNotice').textContent='';
  const buttons=[...document.querySelectorAll('#telegramDialog button:not(#closeTelegram)')];
  buttons.forEach(b=>b.disabled=true);
  try{await action();}
  catch(e){telegramError(e);}
  finally{telegramActionBusy=false;buttons.forEach(b=>b.disabled=false);if(telegramContainerData)renderTelegramContainer(telegramContainerData);await refreshTelegram();}
}
$('#telegramSettings').onclick=()=>{telegramSettingsLoaded=false;$('#telegramDialog').showModal();refreshTelegram();};
$('#telegramReload').onclick=()=>location.reload();
$('#closeTelegram').onclick=()=>$('#telegramDialog').close();
$('#telegramDialog').addEventListener('close',()=>clearTimeout(telegramTimer));
$('#telegramMode').onchange=()=>{telegramModeChanged();refreshTelegramContainer();};
$('#telegramSave').onclick=()=>telegramAction(async()=>{await saveTelegram();$('#telegramNotice').textContent='Настройки сохранены локально.';});
$('#telegramCheck').onclick=()=>telegramAction(async()=>{
  await saveTelegram();$('#telegramNotice').textContent='Проверяем подключение…';
  const result=await telegramAPI('/api/telegram/check',{});
  $('#telegramNotice').textContent=`Подключено: @${result.bot}. Канал «${result.channel}» (${result.chat_id}), публикация разрешена.`;
});
async function prepareTelegram(all){
  await telegramAction(async()=>{
    if(!all&&(!currentPost()||emptyPost(currentPost())))throw new Error('Пост пустой. Добавьте фото, видео или текст, затем повторите подготовку.');
    if(all&&!state.posts.some(post=>!emptyPost(post)))throw new Error('Нет постов для подготовки. Добавьте фото, видео или текст.');
    $('#telegramNotice').textContent='Сохраняем посты и запускаем подготовку…';
    await flush();
    await saveTelegram();await telegramAPI('/api/telegram/prepare',all?{}:{ids:[state.active]});
    $('#telegramNotice').textContent='';
  });
}
$('#telegramPrepareCurrent').onclick=()=>prepareTelegram(false);
$('#telegramPrepareAll').onclick=()=>prepareTelegram(true);
async function sendTelegramSelection(ids){
  if(!ids.length)throw new Error('Выберите хотя бы один готовый пост.');
  await flush();
  const direct=telegramJobId&&telegramLastData.job.channel_status==='ready'&&!telegramLastData.job.items.some(p=>ids.includes(p.id)&&p.status==='cached');
  await telegramAPI(direct?'/api/telegram/send':'/api/telegram/send-ready',direct?{id:telegramJobId,ids}:{ids});
}
$('#telegramPublish').onclick=()=>telegramAction(()=>sendTelegramSelection([...document.querySelectorAll('[data-telegram-post]:checked')].map(input=>input.dataset.telegramPost)));
$('#telegramPublishPrefix').onclick=()=>telegramAction(async()=>{
  const ids=[];
  for(const post of telegramLastData.job.items){
    if(!['ready','cached','sent'].includes(post.status))break;
    if(post.status!=='sent')ids.push(post.id);
  }
  await sendTelegramSelection(ids);
});
$('#telegramCancel').onclick=()=>telegramAction(async()=>{await telegramAPI('/api/telegram/cancel',{});});
function updateTelegramSelectAll(){
  const inputs=[...document.querySelectorAll('[data-telegram-post]')];
  const selected=inputs.filter(input=>input.checked).length;
  const all=$('#telegramSelectAll');
  all.checked=inputs.length>0&&selected===inputs.length;
  all.indeterminate=selected>0&&selected<inputs.length;
  all.disabled=!inputs.length||Boolean(telegramLastData?.busy)||telegramActionBusy;
}
$('#telegramSelectAll').onchange=()=>{
  telegramSelectionDefault=$('#telegramSelectAll').checked;
  for(const input of document.querySelectorAll('[data-telegram-post]')){
    input.checked=telegramSelectionDefault;
    telegramSelection.set(input.dataset.telegramPost,input.checked);
  }
  updateTelegramSelectAll();
};
$('#telegramReview').addEventListener('change',event=>{
  if(event.target.matches('[data-telegram-post]')){
    telegramSelection.set(event.target.dataset.telegramPost,event.target.checked);
    updateTelegramSelectAll();
  }
});
$('#telegramHideSent').onchange=()=>{if(telegramLastData)renderTelegram(telegramLastData);};
$('#telegramReview').onclick=e=>{
  if(e.target.closest('[data-show-sent]')){$('#telegramHideSent').checked=false;renderTelegram(telegramLastData);return;}
  const output=e.target.closest('[data-output-post]');
  if(output){openTelegramOutput(output.dataset.outputPost,Number(output.dataset.outputIndex));return;}
  const button=e.target.closest('[data-telegram-resolve]');if(!button)return;
  const resolution=button.dataset.telegramResolve;
  if(!confirm(resolution==='sent'?'Подтвердить, что пост опубликован в канале?':'Вы проверили канал и убедились, что поста нет? Будет разрешена повторная отправка.'))return;
  telegramAction(()=>telegramAPI('/api/telegram/resolve',{key:button.dataset.key,resolution}));
};

let telegramOutputPost=null,telegramOutputIndex=0,telegramOutputRequest=0;
function openTelegramOutput(postId,index){
  telegramOutputPost=telegramLastData.job.items.find(post=>post.id===postId);
  telegramOutputIndex=index;
  if(!$('#telegramOutput').open)$('#telegramOutput').showModal();
  renderTelegramOutput();
}
function renderTelegramOutput(){
  const request=++telegramOutputRequest, file=telegramOutputPost.files[telegramOutputIndex];
  const image=$('#telegramOutputImage'),video=$('#telegramOutputVideo'),background=$('#telegramOutputBackground');
  video.pause();video.removeAttribute('src');video.load();video.hidden=true;image.hidden=true;
  background.src=byId.has(file.id)?photoURL(file.id):'/missing.svg';background.hidden=false;
  $('#telegramOutputCaption').textContent=`${file.name} · ${telegramOutputIndex+1} / ${telegramOutputPost.files.length}`;
  $('#telegramOutputPrevious').disabled=telegramOutputIndex===0;
  $('#telegramOutputNext').disabled=telegramOutputIndex===telegramOutputPost.files.length-1;
  const notice=$('#telegramOutputNotice');notice.hidden=false;
  notice.textContent=file.prepared_ref?'Загружаем файл…':file.missing_output||file.bytes!=null?'Отправленный файл удалён':'Файл ещё не подготовлен';
  if(!file.prepared_ref)return;
  const url=`/telegram-file/${encodeURIComponent(file.prepared_ref)}?project=${encodeURIComponent(project)}`;
  const ready=()=>{if(request!==telegramOutputRequest)return;background.hidden=true;notice.hidden=true;};
  const failed=()=>{if(request!==telegramOutputRequest)return;image.hidden=true;video.hidden=true;background.hidden=false;notice.hidden=false;notice.textContent='Отправленный файл удалён';};
  if(file.kind==='video'){
    video.onloadeddata=()=>{if(request!==telegramOutputRequest)return;video.hidden=false;ready();};video.onerror=failed;video.src=url;
  }else{
    const decoded=new Image();decoded.onload=()=>{if(request!==telegramOutputRequest)return;image.src=url;image.hidden=false;ready();};decoded.onerror=failed;decoded.src=url;
  }
}
function moveTelegramOutput(delta){const index=telegramOutputIndex+delta;if(index>=0&&index<telegramOutputPost.files.length){telegramOutputIndex=index;renderTelegramOutput();}}
$('#telegramOutputPrevious').onclick=()=>moveTelegramOutput(-1);
$('#telegramOutputNext').onclick=()=>moveTelegramOutput(1);
$('#telegramOutputClose').onclick=()=>$('#telegramOutput').close();
$('#telegramOutput').addEventListener('close',()=>{++telegramOutputRequest;const video=$('#telegramOutputVideo');video.pause();video.removeAttribute('src');video.load();});
$('#telegramOutput').addEventListener('keydown',event=>{if(event.key==='ArrowLeft'){event.preventDefault();moveTelegramOutput(-1);}if(event.key==='ArrowRight'){event.preventDefault();moveTelegramOutput(1);}});

let telegramContainerData=null;
function renderTelegramContainer(data){
  telegramContainerData=data;
  $('#telegramContainerStatus').textContent=data.message;
  $('#telegramApiId').placeholder=data.credentials?.has_api_id?'api_id сохранён локально':'Число с my.telegram.org';
  $('#telegramApiHash').placeholder=data.credentials?.has_api_hash?'api_hash сохранён локально':'32 символа с my.telegram.org';
  const blocked=Boolean(data.busy||telegramLastData?.busy||telegramActionBusy);
  $('#telegramApiCredentials').disabled=blocked;
  $('#telegramContainerStart').disabled=blocked||(data.status==='running'&&!data.restart_required);
  $('#telegramContainerStart').textContent=data.restart_required?'Применить ключи и запустить Bot API':'Запустить Bot API';
  $('#telegramContainerStop').disabled=blocked||data.status==='stopped'||data.status==='checking';
  $('#telegramContainerHint').textContent=telegramLastData?.busy?'Управление доступно после завершения очереди Telegram.':data.restart_required?'Ключи сохранены. Применение перезапустит работающий контейнер.':'';
  if(data.busy){
    for(const id of ['telegramPrepareCurrent','telegramPrepareAll','telegramPublish','telegramPublishPrefix','telegramCheck'])$('#'+id).disabled=true;
  }
}
async function refreshTelegramContainer(){
  if($('#telegramMode').value!=='local')return;
  try{renderTelegramContainer(await telegramAPI('/api/telegram/container/status',{}));}
  catch(error){$('#telegramContainerStatus').textContent=error.message;}
}
for(const [id,action] of [['telegramContainerStart','start'],['telegramContainerStop','stop']]){
  $('#'+id).onclick=()=>telegramAction(async()=>{
    renderTelegramContainer(await telegramAPI('/api/telegram/container/action',{action}));
  });
}

$('#telegramApiSave').onclick=()=>telegramAction(async()=>{
  await telegramAPI('/api/telegram/container/credentials',{api_id:$('#telegramApiId').value,api_hash:$('#telegramApiHash').value});
  $('#telegramApiId').value='';$('#telegramApiHash').value='';
  $('#telegramNotice').textContent='Ключи Bot API сохранены локально.';
});

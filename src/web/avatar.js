/* Crop coordinates refer to the oriented image, never to a resized upload. */
function avatarBounds(width, height, cx, cy, zoom) {
  const side=Math.min(width,height)/zoom;
  cx=Math.max(side/2/width,Math.min(1-side/2/width,cx));
  cy=Math.max(side/2/height,Math.min(1-side/2/height,cy));
  return {cx,cy,side,x:cx*width-side/2,y:cy*height-side/2};
}
(() => {
  const dialog=$('#avatarDialog'), canvas=$('#avatarCrop'), small=$('#avatarSmall');
  let image=null, id=null, destination=null, cx=.5,cy=.5,zoom=1,drag=null,busy=false, complete=false, generation=0;
  const notice=(message,kind='')=>{$('#avatarNotice').textContent=message;$('#avatarNotice').dataset.kind=kind;};
  function controls() {
    $('#avatarInstall').disabled=busy||!image||!destination;
    $('#avatarZoom').disabled=busy||complete||!image;
    $('#avatarReset').disabled=busy||!image;
    $('#avatarInstall').textContent=complete?'Готово':busy?'Устанавливаем…':'Установить аватарку';
    $('#avatarClose').disabled=busy;
  }
  function draw() {
    if(!image)return;
    const b=avatarBounds(image.naturalWidth,image.naturalHeight,cx,cy,zoom);
    cx=b.cx;cy=b.cy;
    $('#avatarZoomValue').textContent=zoom.toFixed(1)+'×';
    const ctx=canvas.getContext('2d');
    ctx.clearRect(0,0,640,640);
    ctx.drawImage(image,b.x,b.y,b.side,b.side,0,0,640,640);
    const mini=small.getContext('2d');
    mini.clearRect(0,0,160,160);mini.drawImage(canvas,0,0,160,160);
    ctx.save();ctx.beginPath();ctx.rect(0,0,640,640);ctx.arc(320,320,319,0,Math.PI*2);
    ctx.fillStyle='rgba(0,0,0,.6)';ctx.fill('evenodd');ctx.restore();
    ctx.beginPath();ctx.arc(320,320,319,0,Math.PI*2);ctx.strokeStyle='rgba(255,255,255,.8)';ctx.lineWidth=2;ctx.stroke();
  }
  $('#makeAvatar').onclick=async()=>{
    const photo=byId.get(current);
    if(!photo||photo.missing||photo.kind==='video')return;
    const run=++generation;
    id=current;image=null;destination=null;cx=cy=.5;zoom=1;drag=null;complete=false;
    $('#avatarBot').textContent='';$('#avatarZoomValue').textContent='1.0×';
    $('#avatarInstall').removeAttribute('aria-label');
    $('#avatarZoom').value='1';$('#avatarChannel').textContent='Проверяем сохранённые настройки канала…';
    $('#avatarInstall').textContent='Установить аватарку';notice('Загружаем фото…');
    canvas.getContext('2d').clearRect(0,0,640,640);small.getContext('2d').clearRect(0,0,160,160);
    controls();dialog.showModal();
    const candidate=new Image();candidate.src=photoURL(id,true);
    const results=await Promise.allSettled([candidate.decode(),api('/api/telegram/check',{})]);
    if(run!==generation||!dialog.open)return;
    if(results[0].status==='fulfilled'){image=candidate;draw();}
    if(results[1].status==='fulfilled'){
      destination=results[1].value;
      $('#avatarChannel').textContent=destination.channel;
      $('#avatarBot').textContent=`Бот @${destination.bot}`;
      $('#avatarInstall').setAttribute('aria-label',`Установить аватарку в «${destination.channel}»`);
    }else $('#avatarChannel').textContent='Настройте бота и канал в разделе «Экспорт», затем откройте редактор заново.';
    notice(results[0].status==='rejected'?'Не удалось загрузить фотографию. Закройте редактор и попробуйте снова.':
      results[1].status==='rejected'?results[1].reason.message:'',results.some(r=>r.status==='rejected')?'error':'');
    controls();
  };
  $('#avatarZoom').oninput=e=>{zoom=Number(e.target.value);draw();};
  canvas.onpointerdown=e=>{
    if(!image||busy||complete)return;
    drag={x:e.clientX,y:e.clientY,cx,cy};canvas.setPointerCapture(e.pointerId);canvas.focus();
  };
  canvas.onpointermove=e=>{
    if(!drag||!image||busy||complete)return;
    const side=Math.min(image.naturalWidth,image.naturalHeight)/zoom;
    const scale=side/canvas.getBoundingClientRect().width;
    cx=drag.cx-(e.clientX-drag.x)*scale/image.naturalWidth;
    cy=drag.cy-(e.clientY-drag.y)*scale/image.naturalHeight;draw();
  };
  canvas.onpointerup=canvas.onpointercancel=canvas.onlostpointercapture=()=>{drag=null;};
  canvas.onkeydown=e=>{
    if(!image||busy||complete||!['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(e.key))return;
    e.preventDefault();const step=.02/zoom;
    if(e.key==='ArrowLeft')cx-=step;if(e.key==='ArrowRight')cx+=step;
    if(e.key==='ArrowUp')cy-=step;if(e.key==='ArrowDown')cy+=step;draw();
  };
  $('#avatarClose').onclick=()=>dialog.close();
  $('#avatarReset').onclick=()=>{
    if(busy||!image)return;
    cx=cy=.5;zoom=1;complete=false;$('#avatarZoom').value='1';notice('');draw();controls();
    $('#avatarInstall').setAttribute('aria-label',`Установить аватарку в «${destination?.channel || 'канал'}»`);
  };
  dialog.oncancel=e=>{if(busy)e.preventDefault();};
  dialog.onclose=()=>{++generation;image=null;drag=null;};
  $('#avatarInstall').onclick=async()=>{
    if(complete){dialog.close();return;}
    if(busy||!image||!destination)return;
    busy=true;controls();notice('Устанавливаем аватарку…');
    try {
      const result=await api('/api/telegram/avatar',{photo:id,crop:[cx,cy,zoom],chat_id:destination.chat_id,bot_id:destination.bot_id});
      notice(`Аватарка канала «${result.channel}» установлена.`, 'success');
      complete=true;$('#avatarInstall').removeAttribute('aria-label');
    }catch(error){notice(error.message,'error');}
    finally {busy=false;controls();}
  };
})();

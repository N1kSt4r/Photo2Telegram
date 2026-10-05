const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');

function setup(posts,active){
  const context=vm.createContext({
    document:{querySelector:()=>({textContent:'',classList:{remove(){}}})},
    LargePreviewLoader:class {},
    Intl, setTimeout(){return 1;},clearTimeout(){},
    crypto:{randomUUID:()=> 'new-draft'},
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../src/web/app.js'),'utf8').replace(/init\(\);\s*$/, ''),context);
  vm.runInContext(`state=${JSON.stringify({posts,active})};render=()=>{};toast=()=>{};`,context);
  return {run:code=>vm.runInContext(code,context),posts:()=>JSON.parse(vm.runInContext('JSON.stringify(state.posts)',context))};
}
const post=(id,caption='',photos=[])=>({id,title:'Название для себя',caption,photos});

test('storage polling clears recovered read errors but preserves cleanup messages',async()=>{
  const app=setup([post('current')],'current');
  app.run(`
    globalThis.notice={textContent:''};
    document.querySelector=()=>notice;
    showGeneratedStorage=()=>{};
    api=async()=>{throw Object.assign(new Error('Not found'),{status:404});};
  `);
  await app.run('refreshGeneratedStorage()');
  assert.match(app.run('notice.textContent'),/Перезапустите сервер/);
  app.run('api=async()=>({})');
  await app.run('refreshGeneratedStorage()');
  assert.equal(app.run('notice.textContent'),'');
  app.run("notice.textContent='Ошибка удаления'");
  await app.run('refreshGeneratedStorage()');
  assert.equal(app.run('notice.textContent'),'Ошибка удаления');
});

test('empty abandoned drafts are removed; text, media and active draft survive',()=>{
  const app=setup([post('photo','',['file']),post('empty',' \n '),post('text','Подпись'),post('active')],'active');
  app.run('changed()');
  assert.deepEqual(app.posts().map(p=>p.id),['photo','text','active']);
  app.run("state.active='photo';changed()");
  assert.deepEqual(app.posts().map(p=>p.id),['photo','text']);
});

test('new draft reuses empty current post and allows leaving a text-only post',()=>{
  const app=setup([post('current')],'current');
  app.run('newPost()');assert.equal(app.posts().length,1);
  app.run("currentPost().caption='Текст';startNext()");
  assert.deepEqual(app.posts().map(p=>p.id),['current','new-draft']);
  app.run("state.active='current';changed()");
  assert.deepEqual(app.posts().map(p=>p.id),['current']);
});

test('cleanup leaves all photo and caption content intact and is idempotent',()=>{
  const photo=post('photo','',['a','b']),text=post('text','  Текст  ');
  const app=setup([post('empty'),photo,text],'photo');
  assert.equal(app.run('removeEmptyPosts()'),true);
  assert.deepEqual(app.posts(),[photo,text]);
  assert.equal(app.run('removeEmptyPosts()'),false);
});

test('library refresh clears failures for thumbnails and large previews',async()=>{
  const app=setup([post('current')],'current');
  app.run(`
    globalThis.previews={failed:new Set(['thumbnail'])};
    largePreviews.failed=new Set(['large']);
    api=async()=>({photos:[]});flush=async()=>{};updateDays=()=>{};
  `);
  await app.run('refreshLibrary()');
  assert.equal(app.run('previews.failed.size'),0);
  assert.equal(app.run('largePreviews.failed.size'),0);
});

function videoViewer(){
  const app=setup([post('current')],'current');
  app.run(`
    globalThis.elements=new Map();
    function element(){return {hidden:false,textContent:'',value:0,attrs:{},events:{},focus(){},
      classList:{remove(){},toggle(){}},setAttribute(k,v){this.attrs[k]=String(v);},
      getAttribute(k){return this.attrs[k]??null;},hasAttribute(k){return k in this.attrs;},
      removeAttribute(k){delete this.attrs[k];},addEventListener(k,fn){this.events[k]=fn;}};}
    document.querySelector=selector=>{
      if(!elements.has(selector))elements.set(selector,element());
      return elements.get(selector);
    };
    globalThis.video=$('#largeVideo');
    Object.assign(video,{duration:120,currentTime:30,paused:true,ended:false,muted:false,error:null,loads:0,
      pause(){this.paused=true;this.events.pause?.();},
      async play(){this.paused=false;this.ended=false;this.events.play?.();},
      load(){this.loads++;this.currentTime=0;}});
    Object.defineProperty(video,'src',{set(value){this.attrs.src=value;}});
    current='clip';viewerIds=['clip','other'];project='test';state.hidden=[];
    byId=new Map(['clip','other'].map(id=>[id,{id,kind:'video',name:id+'.mp4',date:'2026-10-05T10:00:00',duration:120}]));
    prepareLargePreviews=()=>{};
    render=renderViewer;
    bindVideoControls();renderViewer();
  `);
  return app;
}

test('video controls play, pause, seek and handle unavailable duration',async()=>{
  const app=videoViewer();
  assert.equal(app.run("$('#videoTime').textContent"),'0:30 / 2:00');
  assert.equal(app.run("$('#videoSeek').value"),250);
  await app.run("$('#videoPlay').onclick()");
  assert.equal(app.run('video.paused'),false);
  assert.equal(app.run("$('#videoPlay').getAttribute('aria-label')"),'Пауза');
  await app.run("$('#videoPlay').onclick()");
  assert.equal(app.run('video.paused'),true);
  app.run("$('#videoSeek').value=750;$('#videoSeek').oninput()");
  assert.equal(app.run('video.currentTime'),90);
  assert.equal(app.run("$('#videoTime').textContent"),'1:30 / 2:00');
  app.run('video.duration=NaN;video.events.durationchange()');
  assert.equal(app.run("$('#videoSeek').disabled"),true);
  app.run("$('#videoSeek').value=500;$('#videoSeek').oninput()");
  assert.equal(app.run('video.currentTime'),90);
  app.run('video.duration=3661;video.currentTime=3600;video.events.timeupdate()');
  assert.equal(app.run("$('#videoTime').textContent"),'1:00:00 / 1:01:01');
  app.run('video.ended=true;video.events.ended()');
  assert.equal(app.run("$('#videoPlay').getAttribute('aria-label')"),'Воспроизвести');
  app.run('video.error={};video.events.error()');
  assert.equal(app.run("$('#videoPlay').disabled"),true);
  assert.equal(app.run("$('#videoSeek').disabled"),true);
});

test('adding/removing video and changing posts preserve playback and the export audio choice',async()=>{
  const app=videoViewer();
  app.run('toggleVideoAudio()');
  assert.equal(app.run('state.mutedVideos.clip'),true);
  assert.equal(app.run('video.muted'),true);
  await app.run('toggleVideoPlayback()');
  app.run("pick('clip');renderViewer()");
  assert.deepEqual(app.posts()[0].photos,['clip']);
  assert.equal(app.run('video.muted'),true);
  assert.equal(app.run('video.currentTime'),30);
  assert.equal(app.run('video.paused'),false);
  assert.equal(app.run('video.loads'),0);
  app.run("pick('clip');pick('clip');startNext()");
  assert.equal(app.run('video.muted'),true);
  assert.equal(app.run('video.paused'),false);
  assert.equal(app.run('video.currentTime'),30);
  app.run('move(1)');
  assert.equal(app.run('video.muted'),false);
  app.run('move(-1)');
  assert.equal(app.run('video.muted'),true);
  app.run('toggleVideoAudio()');
  assert.equal(app.run('video.muted'),false);
  assert.equal(app.run("$('#muteVideo').textContent"),'Со звуком');
  assert.equal(app.run('state.mutedVideos.clip'),undefined);
});

test('playback keyboard controls do not add to posts or navigate the gallery; Escape can close',()=>{
  const app=videoViewer();
  for(const key of [' ','ArrowLeft','ArrowRight','Home','End']){
    app.run(`globalThis.stopped=false;$('#videoControls').events.keydown({key:${JSON.stringify(key)},stopPropagation(){stopped=true;}})`);
    assert.equal(app.run('stopped'),true);
  }
  app.run("globalThis.closed=false;closeViewer=()=>{closed=true;};$('#videoControls').events.keydown({key:'Escape',preventDefault(){},stopPropagation(){}})");
  assert.equal(app.run('closed'),true);
});

function viewMemory(){
  const app=videoViewer();
  app.run(`
    globalThis.memory=new Map();
    globalThis.sessionStorage={getItem:key=>memory.get(key)??null,setItem:(key,value)=>memory.set(key,value)};
    globalThis.window={scrollY:620,scrollTo(x,y){this.scrollY=y;}};
    $('#day').options=[{value:''},{value:'2026-10-05'}];$('#day').value='2026-10-05';
    $('#unused').checked=true;$('aside').scrollTop=90;
    photos=[...byId.values()];visible=photos;
    largePreviews.close=()=>{};globalThis.previews={schedule(){}};
  `);
  return app;
}

test('reload restores tab, filters, gallery and sidebar scroll, and the open file, without video time',()=>{
  const app=viewMemory();
  app.run("tab='hidden';viewStateReady=true;saveViewState();globalThis.saved=readViewState()");
  assert.equal(app.run('saved.current'),'clip');
  assert.equal(app.run("Object.hasOwn(saved,'videoTime')"),false);
  app.run(`
    viewStateReady=false;current=null;$('#viewer').hidden=true;
    tab='photos';$('#day').value='';$('#unused').checked=false;
    window.scrollY=0;$('aside').scrollTop=0;
    applyViewFilters(saved);restoreViewState(saved);
  `);
  assert.equal(app.run('tab'),'hidden');
  assert.equal(app.run("$('#day').value"),'2026-10-05');
  assert.equal(app.run("$('#unused').checked"),true);
  assert.equal(app.run('current'),'clip');
  assert.equal(app.run("$('#viewer').hidden"),false);
  assert.equal(app.run('window.scrollY'),620);
  assert.equal(app.run("$('aside').scrollTop"),90);
  app.run('closeViewer()');
  assert.equal(app.run('readViewState().current'),null);
});

test('view snapshots are isolated by library and initial rendering cannot overwrite saved navigation',()=>{
  const app=viewMemory();
  app.run("memory.set('photo2telegram:view:test',JSON.stringify({version:1,current:'clip',tab:'posts'}));saveViewState()");
  assert.equal(app.run('readViewState().tab'),'posts');
  app.run("project='second'");
  assert.equal(app.run('readViewState()'),null);
  app.run("viewStateReady=true;tab='photos';saveViewState();project='test'");
  assert.equal(app.run('readViewState().tab'),'posts');
});

test('unavailable files, obsolete filters and broken storage do not prevent browsing',()=>{
  const app=viewMemory();
  app.run(`
    current=null;$('#viewer').hidden=true;
    globalThis.saved={version:1,current:'deleted',tab:'invalid',day:'2099-01-01',unused:'yes',scrollY:-100};
    applyViewFilters(saved);restoreViewState(saved);
  `);
  assert.equal(app.run('current'),null);
  assert.equal(app.run("$('#viewer').hidden"),true);
  assert.equal(app.run('tab'),'photos');
  assert.equal(app.run("$('#day').value"),'');
  assert.equal(app.run("$('#unused').checked"),false);
  assert.equal(app.run('window.scrollY'),0);
  app.run("memory.set('photo2telegram:view:test','not json')");
  assert.equal(app.run('readViewState()'),null);
  app.run("sessionStorage.getItem=()=>{throw new Error('Blocked');};sessionStorage.setItem=()=>{throw new Error('Quota');}");
  assert.equal(app.run('readViewState()'),null);
  assert.doesNotThrow(()=>app.run('saveViewState()'));
});

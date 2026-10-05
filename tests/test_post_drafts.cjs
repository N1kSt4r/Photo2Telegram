const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');

function setup(posts,active){
  const context=vm.createContext({
    document:{querySelector:()=>({textContent:'',classList:{remove(){}}})},
    LargePreviewLoader:class {},
    previews:{refresh(){},schedule(){}},
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
    function element(){return {hidden:false,textContent:'',value:0,attrs:{},events:{},focus(){},style:{setProperty(k,v){this[k]=v;}},clientWidth:800,clientHeight:500,
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

test('rotation applies to every occurrence, wraps after four turns and preserves playback and mute',async()=>{
  const app=videoViewer();
  app.run("state.posts.push({id:'second',title:'Second',caption:'',photos:['clip']});toggleVideoAudio()");
  await app.run('toggleVideoPlayback()');
  const posts=app.posts();
  app.run('rotateMedia(90)');
  assert.equal(app.run('state.rotations.clip'),90);
  assert.equal(app.run("$('#largeVideo').style.width"),'500px');
  assert.equal(app.run("$('#largeVideo').style.height"),'800px');
  assert.match(app.run("$('#largeVideo').style.transform"),/rotate\(90deg\)/);
  assert.equal(app.run("previewRotation('clip')"),90);
  assert.equal(app.run('video.currentTime'),30);
  assert.equal(app.run('video.muted'),true);
  assert.equal(app.run('video.paused'),false);
  assert.equal(app.run('video.loads'),0);
  app.run('rotateMedia(90);rotateMedia(90);rotateMedia(90)');
  assert.equal(app.run('state.rotations.clip'),undefined);
  assert.equal(app.run("$('#largeVideo').style.width"),'100%');
  app.run('rotateMedia(-90)');
  assert.equal(app.run('state.rotations.clip'),270);
  assert.deepEqual(app.posts(),posts);
  app.run("byId.get('clip').missing=true;rotateMedia(90)");
  assert.equal(app.run('state.rotations.clip'),270);
});

test('photo rotation fits portrait turns into the viewer and resets on the next file',()=>{
  const app=videoViewer();
  app.run("byId.get('clip').kind='photo';rotateMedia(90)");
  assert.equal(app.run("$('#largePhoto').style.width"),'500px');
  assert.equal(app.run("$('#largePhoto').style.height"),'800px');
  app.run('move(1)');
  assert.match(app.run("$('#largeVideo').style.transform"),/rotate\(0deg\)/);
  app.run('move(-1)');
  assert.match(app.run("$('#largePhoto').style.transform"),/rotate\(90deg\)/);
});

test('photo corrections persist across posts, keep video untouched, and reset independently',()=>{
  const app=videoViewer();
  app.run(`
    document.querySelectorAll=()=>[];document.getElementById=()=>({});
    byId.set('photo',{id:'photo',kind:'photo',name:'photo.jpg'});
    current='photo';correctionOpen=true;
    changeCorrection('brightness',20);changeCorrection('saturation',-100);
  `);
  assert.equal(app.run("JSON.stringify(photoCorrection('photo'))"),'{"brightness":20,"saturation":-100}');
  assert.equal(app.run("$('#postPanel').hidden"),true);
  assert.equal(app.run("$('#correctionPanel').hidden"),false);
  assert.equal(app.run("$('#correction-brightness-value').textContent"),'+20');
  app.run("changeCorrection('brightness',0)");
  assert.equal(app.run("photoCorrection('photo').saturation"),-100);
  app.run("current='clip';changeCorrection('brightness',50);renderCorrection()");
  assert.equal(app.run("state.corrections.clip"),undefined);
  assert.equal(app.run("$('#correctionPanel').hidden"),true);
  app.run("current='photo';renderCorrection();blocked=true;changeCorrection(null)");
  assert.equal(app.run("photoCorrection('photo').saturation"),-100);
  app.run("blocked=false;changeCorrection('brightness',101)");
  assert.equal(app.run("photoCorrection('photo').brightness"),0);
  app.run('changeCorrection(null)');
  assert.equal(app.run("state.corrections.photo"),undefined);
  assert.equal(app.run("$('#correctionReset').disabled"),true);
});

test('before/after only changes the displayed filter; sRGB matrix preserves neutral and grey',()=>{
  const app=videoViewer();
  app.run(`
    document.querySelectorAll=()=>[];document.getElementById=()=>({});
    byId.set('photo',{id:'photo',kind:'photo',name:'photo.jpg'});current='photo';
    state.corrections={photo:{brightness:20}};dirty=false;
    showCorrectionComparison(true);
  `);
  assert.equal(app.run("$('#largePhoto').style['--photo-correction']"),'brightness(1)');
  assert.equal(app.run("photoCorrection('photo').brightness"),20);
  assert.equal(app.run('dirty'),false);
  app.run('showCorrectionComparison(false)');
  assert.equal(app.run("$('#largePhoto').style['--photo-correction']"),'url(#correction-20-0-0)');
  assert.deepEqual(JSON.parse(app.run('JSON.stringify(correctionMatrix({}))')),[1,0,0,0,0,0,1,0,0,0,0,0,1,0,0,0,0,0,1,0]);
  assert.deepEqual(JSON.parse(app.run('JSON.stringify(correctionMatrix({saturation:-100}).slice(0,5))')),[.213,.715,.072,0,0]);
});

test('returning from correction refreshes thumbnail layout without refreshing on each slider move',()=>{
  const app=videoViewer();
  app.run(`
    byId.set('photo',{id:'photo',kind:'photo',name:'photo.jpg'});current='photo';
    globalThis.refreshes=0;previews.refresh=()=>refreshes++;
    correctionOpen=true;renderCorrection();
  `);
  assert.equal(app.run('refreshes'),1);
  app.run('renderCorrection()');assert.equal(app.run('refreshes'),1);
  app.run('correctionOpen=false;renderCorrection()');assert.equal(app.run('refreshes'),2);
  app.run('renderCorrection()');assert.equal(app.run('refreshes'),2);
});

test('reuse copies the last correction, reset preserves it, browsing and video cannot overwrite it',()=>{
  const app=videoViewer();
  app.run(`
    document.querySelectorAll=()=>[];document.getElementById=()=>({});
    for(const id of ['first','second'])byId.set(id,{id,kind:'photo',name:id+'.jpg'});
    current='first';changeCorrection('brightness',20);changeCorrection('contrast',15);changeCorrection('saturation',-30);
    current='second';renderCorrection();reuseLastCorrection();
  `);
  assert.equal(app.run("JSON.stringify(photoCorrection('second'))"),'{"brightness":20,"contrast":15,"saturation":-30}');
  assert.equal(app.run("state.corrections.second===state.lastCorrection"),false);
  app.run("changeCorrection('brightness',40)");
  assert.equal(app.run("photoCorrection('first').brightness"),20);
  assert.equal(app.run('state.lastCorrection.brightness'),40);
  app.run('changeCorrection(null)');assert.equal(app.run('state.lastCorrection.brightness'),40);
  app.run('reuseLastCorrection()');assert.equal(app.run("photoCorrection('second').brightness"),40);
  app.run("current='clip';reuseLastCorrection();changeCorrection('brightness',50)");
  assert.equal(app.run('state.corrections.clip'),undefined);
  assert.equal(app.run('state.lastCorrection.brightness'),40);
  app.run("current='second';blocked=true;changeCorrection(null)");
  assert.equal(app.run("photoCorrection('second').brightness"),40);
});

const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');

function setup(){
  const nodes=new Map(), requests=[], storage=new Map();
  const node=selector=>{
    if(!nodes.has(selector))nodes.set(selector,{
      value:'',checked:false,dataset:{},addEventListener(){},querySelectorAll(){return [];},
      setAttribute(){},scrollIntoView(){},replaceChildren(content){this.markup=content.markup;},showModal(){this.open=true;},
    });
    return nodes.get(selector);
  };
  const context=vm.createContext({
    $:node,project:'test',byId:new Map(),token:'test-token',exportRunning:false,
    localStorage:{getItem:key=>storage.get(key),setItem:(key,value)=>storage.set(key,value)},
    esc:String,formatBytes:String,clearTimeout(){},setTimeout(){},
    flush:async()=>{},
    document:{querySelectorAll:()=>[],createElement:()=>({
      content:{querySelectorAll:()=>[]},set innerHTML(value){this.content.markup=value;},
    })},
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../src/web/telegram.js'),'utf8'),context);
  context.request=async(url,data)=>{requests.push({url,data});return {};};
  vm.runInContext('telegramAPI=request',context);
  const settings={mode:'cloud',channel:'@saved_channel',silent:false,has_token:true};
  node('#telegramMode').value='cloud';
  const post={id:'one',title:'One',caption:'',status:'ready',files:[]};
  const render=(overrides={})=>{
    context.data={settings,busy:false,job:{id:'queue',status:'ready',channel_status:'ready',items:[{...post,...overrides}],message:''}};
    vm.runInContext('renderTelegram(data)',context);
  };
  render();
  return {node,requests,render,run:code=>vm.runInContext(code,context)};
}

test('unsaved destination, credentials and delivery options cannot publish',async()=>{
  for(const [selector,property,value] of [
    ['#telegramChannel','value','@different_channel'],
    ['#telegramToken','value','54321:new-token'],
    ['#telegramMode','value','local'],
    ['#telegramSilent','checked',true],
  ]){
    const app=setup();app.node(selector)[property]=value;
    await assert.rejects(app.run("sendTelegramSelection(['one'])"),/Настройки отправки изменены/);
    assert.equal(app.requests.length,0);
  }
});

test('unchanged checked settings allow publishing a ready selection',async()=>{
  const app=setup();
  await app.run("sendTelegramSelection(['one'])");
  assert.equal(app.requests[0].url,'/api/telegram/send');
  assert.deepEqual(JSON.parse(JSON.stringify(app.requests[0].data)),{id:'queue',ids:['one']});
});

test('settings changed while saving drafts also prevent publishing',async()=>{
  const app=setup();
  app.run('flush=()=>new Promise(resolve=>{globalThis.saved=resolve;})');
  const sending=app.run("sendTelegramSelection(['one'])");
  app.node('#telegramChannel').value='@changed_while_saving';
  app.run('saved()');
  await assert.rejects(sending,/Настройки отправки изменены/);
  assert.equal(app.requests.length,0);
});

test('unknown results show both manual resolution controls',()=>{
  const app=setup();
  app.render({status:'unknown',resolve_key:'journal-key',error:'Connection lost'});
  const markup=app.node('#telegramReview').markup;
  assert.match(markup,/data-telegram-resolve="sent"/);
  assert.match(markup,/data-telegram-resolve="retry"/);
});

test('export defaults to directories and remembers the choice per library',()=>{
  const app=setup();app.run('refreshTelegram=()=>{};refreshTelegramContainer=()=>{}');
  app.run('openExportDialog()');
  assert.equal(app.node('#telegramMode').value,'directories');
  assert.equal(app.node('#telegramBody').hidden,true);
  assert.equal(app.node('#directoryExport').hidden,false);
  app.node('#telegramMode').value='local';app.node('#telegramMode').onchange();
  assert.equal(app.node('#telegramBody').hidden,false);
  assert.equal(app.node('#directoryExport').hidden,true);
  assert.equal(app.node('#telegramContainer').hidden,false);
  app.run('openExportDialog()');assert.equal(app.node('#telegramMode').value,'local');
  app.run("project='another';openExportDialog()");assert.equal(app.node('#telegramMode').value,'directories');
  app.run("project='test';openExportDialog()");assert.equal(app.node('#telegramMode').value,'local');
  app.node('#telegramMode').value='cloud';app.node('#telegramMode').onchange();
  assert.equal(app.node('#telegramContainer').hidden,true);
  assert.equal(app.node('#telegramEndpointLabel').hidden,true);
});

test('loading saved bot settings does not replace the chosen export method',()=>{
  const app=setup();app.run('refreshTelegram=()=>{};openExportDialog()');
  app.render();
  assert.equal(app.node('#telegramMode').value,'directories');
  assert.equal(app.node('#telegramBody').hidden,true);
});

test('connection details survive polling without preparation and hide after changing channel',()=>{
  const app=setup();
  app.run("telegramConnection={bot:'test_bot',channel:'My channel',chat_id:-100123};renderTelegramConnection(null)");
  app.render();
  assert.match(app.node('#telegramDestination').textContent,/@test_bot\nКанал: My channel \(-100123\)/);
  app.run("telegramConnectionError='Connection failed'");app.render();
  assert.equal(app.node('#telegramChannelStatus').textContent,'Connection failed');
  app.node('#telegramChannel').value='@other';app.render();
  assert.equal(app.node('#telegramDestination').textContent,'');
});

test('channel discovery shows code and applies a found channel only once',()=>{
  const app=setup();
  app.run("data.discovery={id:'pair',status:'waiting',code:'abcdefghijklmnop',bot:'bot',expires_at:Date.now()/1000+300,message:'Wait'};data.busy=true;renderTelegram(data)");
  assert.equal(app.node('#telegramDiscoveryCode').value,'abcdefghijklmnop');
  assert.equal(app.node('#telegramDiscoveryCodeRow').hidden,false);
  assert.equal(app.node('#telegramConfig').disabled,true);
  assert.equal(app.node('#telegramDiscoveryCancel').hidden,false);
  app.run("data.settings.channel='-100123';data.busy=false;data.discovery={id:'pair',status:'connected',message:'Удалите сообщение вручную',destination:{chat_id:-100123,channel:'Found',bot:'bot'}};renderTelegram(data)");
  assert.equal(app.node('#telegramChannel').value,'-100123');
  assert.equal(app.node('#telegramDiscoveryCodeRow').hidden,true);
  assert.equal(app.node('#telegramDiscoveryCancel').hidden,true);
  assert.match(app.node('#telegramDiscoveryMessage').textContent,/вручную/);
  app.node('#telegramChannel').value='@manual';app.run('renderTelegram(data)');
  assert.equal(app.node('#telegramChannel').value,'@manual');
});

test('sent thumbnails use the recorded rotation; older records default to no rotation',()=>{
  const app=setup();
  app.run("previewRotation=(id,angle)=>angle;photoURL=id=>'/photo/'+id;byId.set('photo',{kind:'photo'});");
  const markup=app.run("telegramThumbnail({id:'photo',name:'photo.jpg',kind:'photo',rotation:90},0,{id:'sent',status:'sent'})");
  assert.match(markup,/data-rotation="90"/);
  assert.doesNotMatch(markup,/style=/); // Inline styles are blocked by the server CSP.
  assert.match(app.run("telegramThumbnail({id:'photo',name:'photo.jpg',kind:'photo'},0,{id:'old',status:'sent'})"),/data-rotation="0"/);
});

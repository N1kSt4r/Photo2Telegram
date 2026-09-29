const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');

function setup(){
  const nodes=new Map(), requests=[];
  const node=selector=>{
    if(!nodes.has(selector))nodes.set(selector,{
      value:'',checked:false,dataset:{},addEventListener(){},querySelectorAll(){return [];},
      setAttribute(){},scrollIntoView(){},replaceChildren(content){this.markup=content.markup;},
    });
    return nodes.get(selector);
  };
  const context=vm.createContext({
    $:node,project:'test',byId:new Map(),token:'test-token',
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

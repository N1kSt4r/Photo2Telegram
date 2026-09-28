// Run with: node --test tests/test_preview_loader.cjs (no dependencies).
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const vm = require('node:vm');

function setup() {
  const context = vm.createContext({
    document: {hidden: false, addEventListener() {}, querySelector() {return {}; }},
    window: {addEventListener() {}},
    ResizeObserver: class {observe() {}},
    requestAnimationFrame() {return 1;},
    URL: {createObjectURL() {return 'blob:test';}, revokeObjectURL() {}},
  });
  vm.runInContext(readFileSync(join(__dirname,'../src/web/previews.js'),'utf8'),context);
  const loader = vm.runInContext('previews',context);
  loader.rankRows = () => {};
  const starts = [];
  loader.load = (url,pool) => {loader.active.set(url,pool); starts.push({url,pool});};
  function add(name,priority) {
    const img = {dataset:{preview:'/photo/'+name}, classList:{add(){},remove(){}},
      parentElement:{classList:{add(){},remove(){}}},getAttribute(){return null;},set src(value){this.source=value;}};
    loader.elements.push(img);
    loader.scores.set(img,{priority,distance:0});
    return img;
  }
  function finish(url) {
    loader.active.delete(url);
    loader.warmed.add(url);
    for(const img of loader.elements) if(img.dataset.preview===url)img.dataset.loaded=url;
  }
  return {context,loader,starts,add,finish};
}

test('visible requests must finish before nearby, nearby before background',()=>{
  const {loader,starts,add,finish}=setup();
  for(let i=0;i<8;i++)add('visible'+i,0);
  for(let i=0;i<4;i++)add('nearby'+i,1);
  for(let i=0;i<5;i++)add('background'+i,2);
  loader.pump();assert.equal(starts.length,6);assert(starts.every(x=>x.pool==='visible'));
  finish(starts[0].url);loader.pump();assert.equal(starts.length,7);
  for(const item of starts.slice(1,6))finish(item.url);
  loader.pump();assert.equal(starts.length,8);assert.equal(loader.active.size,2);
  loader.pump();assert.equal(starts.length,8);
  for(const item of starts.slice(6,8))finish(item.url);
  loader.pump();assert.equal(starts.length,12);assert(starts.slice(8).every(x=>x.pool==='nearby'));
  finish(starts[8].url);loader.pump();assert.equal(starts.length,12);
  for(const item of starts.slice(9,12))finish(item.url);
  loader.pump();assert.equal(starts.length,15);assert.equal(loader.active.size,3);
  assert(starts.slice(12).every(x=>x.pool==='background'));
});

test('scroll prioritizes new visible rows and never queues more than six requests',()=>{
  const {loader,starts,add,finish}=setup();
  const old=Array.from({length:6},(_,i)=>add('old'+i,0));
  const next=Array.from({length:6},(_,i)=>add('next'+i,1));
  loader.pump();
  old.forEach(img=>loader.scores.get(img).priority=2);
  next.forEach(img=>loader.scores.get(img).priority=0);
  loader.pump();assert.equal(starts.length,6);
  finish(starts[0].url);loader.pump();assert.equal(starts.length,7);
  assert.equal(starts[6].url,'/photo/next0');assert.equal(loader.active.size,6);
});

test('duplicate thumbnails are requested once at their highest priority',()=>{
  const {loader,starts,add}=setup();add('same',2);add('same',0);add('other',1);
  loader.pump();assert.deepEqual(starts,[{url:'/photo/same',pool:'visible'}]);
});

test('failed visible images do not block the next stage forever',()=>{
  const {loader,starts,add}=setup();add('broken',0);add('nearby',1);
  loader.failed.add('/photo/broken');loader.pump();assert.equal(starts[0].pool,'nearby');
});

test('default browser cache is used and page counts do not pretend to count disk files',async()=>{
  const {loader,context,add}=setup();const img=add('one?v=1',0);
  let options;
  context.fetch=async(url,opts)=>{options=opts;return {ok:true,blob:async()=>({})};};
  await vm.runInContext('PreviewLoader.prototype.load',context).call(loader,img.dataset.preview,'visible');
  assert.equal(options.cache,'default');assert.equal(options.priority,'high');
  assert.equal(loader.pageStats().loaded,1);assert.equal(loader.pageStats().retained,1);
  loader.cache.clear();assert.equal(loader.pageStats().loaded,1);assert.equal(loader.pageStats().retained,0);
});

test('large previews prioritize current, reuse cached neighbors and discard stale responses',async()=>{
  const {context}=setup();
  const requests=[],shown=[],revoked=[];
  context.fetch=(url,options)=>new Promise(resolve=>requests.push({url,options,resolve}));
  context.URL.createObjectURL=blob=>'blob:'+blob.url;
  context.URL.revokeObjectURL=url=>revoked.push(url);
  context.ready=(...args)=>shown.push(args);
  const loader=vm.runInContext('new LargePreviewLoader(ready)',context);
  const finish=async(url,ok=true)=>{
    requests.find(r=>r.url===url).resolve({ok,blob:async()=>({url})});
    await new Promise(resolve=>setImmediate(resolve));
  };
  loader.setWindow('a',['b','c']);
  assert.deepEqual(requests.map(r=>r.url),['a']);
  await finish('a');
  assert.deepEqual(requests.map(r=>r.url),['a','b','c']);
  loader.setWindow('c',['b','a']);
  assert.deepEqual(requests.map(r=>r.url),['a','b','c']);
  assert.equal(loader.active.size,2);
  await finish('b');
  assert.equal(shown.at(-1)[0],'a');
  await finish('c');
  assert.equal(shown.at(-1)[0],'c');
  loader.setWindow('b',['c','a']);
  assert.equal(shown.at(-1)[1],'blob:b');
  assert.equal(requests.length,3);
  loader.setWindow('d',['e']);
  assert.equal(loader.cache.size,0);
  assert.equal(revoked.length,3);
  loader.setWindow('f',[]);
  await finish('d');
  assert(!loader.cache.has('d'));
  assert.equal(requests.at(-1).url,'f');
  await finish('f',false);
  assert.deepEqual(shown.at(-1),['f',null]);
  assert(requests.every(r=>r.options.cache==='default'));
});

test('large prefetch pauses on close and for cache clearing',async()=>{
  const {context}=setup();const requests=[];
  context.fetch=url=>new Promise(resolve=>requests.push({url,resolve}));
  const loader=vm.runInContext('new LargePreviewLoader(()=>{})',context);
  loader.paused=true;loader.setWindow('a',['b']);assert.equal(requests.length,0);
  loader.paused=false;loader.pump();assert.equal(requests.length,1);
  loader.close();requests[0].resolve({ok:true,blob:async()=>({})});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(requests.length,1);assert.equal(loader.cache.size,0);
  loader.backgroundPaused=true;loader.cache.set('a','blob:a');
  loader.setWindow('a',['b']);assert.equal(requests.length,1);
});


test('large viewer gets three prefetch slots plus a reserved current slot',async()=>{
  const {context,loader:thumbs,add,starts}=setup();
  const requests=[];
  context.fetch=(url,options)=>new Promise(resolve=>requests.push({url,options,resolve}));
  context.activity=busy=>{thumbs.largePending=busy;};
  const loader=vm.runInContext('new LargePreviewLoader(()=>{},activity)',context);
  add('thumbnail',0);
  loader.cache.set('a','blob:a');
  loader.setWindow('a',['b','c','d','e','f']);
  assert.deepEqual(requests.map(r=>r.url),['b','c','d']);
  thumbs.pump();assert.equal(starts.length,0);
  loader.setWindow('x',['b','c','d','e']);
  assert.equal(requests.at(-1).url,'x');assert.equal(loader.active.size,4);
  requests.find(r=>r.url==='b').resolve({ok:true,blob:async()=>({})});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(requests.length,4); // current must finish before more speculation
  requests.find(r=>r.url==='x').resolve({ok:true,blob:async()=>({})});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(requests.at(-1).url,'e');
  for(const r of requests.filter(r=>['c','d','e'].includes(r.url)))r.resolve({ok:true,blob:async()=>({})});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(thumbs.largePending,false);
  thumbs.pump();assert.equal(starts.length,1);
  loader.setWindow('z',[]);assert.equal(thumbs.largePending,true);
  loader.close();assert.equal(thumbs.largePending,false);
  assert(requests.every(r=>r.options.priority==='high'));
});

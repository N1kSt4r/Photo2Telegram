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

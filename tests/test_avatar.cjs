const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm');
const code=fs.readFileSync('src/web/avatar.js','utf8');
const context={};vm.createContext(context);
vm.runInContext(code.slice(0,code.indexOf('(() =>')),context);
test('avatar crop stays square inside landscape and portrait at every zoom',()=>{
  for(const [w,h] of [[480,640],[1920,1080],[100,100]]){
    for(const zoom of [1,2,5])for(const x of [-1,.5,2])for(const y of [-1,.5,2]){
      const b=context.avatarBounds(w,h,x,y,zoom);
      assert.ok(b.x>=-1e-8&&b.y>=-1e-8);
      assert.ok(b.x+b.side<=w+1e-8&&b.y+b.side<=h+1e-8);
      assert.equal(b.side,Math.min(w,h)/zoom);
    }
  }
});
test('center and zoom retain the selected image coordinates until an edge is reached',()=>{
  const b=context.avatarBounds(400,200,.75,.5,1);
  assert.equal(b.x,200);assert.equal(b.y,0);assert.equal(b.side,200);
  const z=context.avatarBounds(400,200,b.cx,b.cy,2);
  assert.equal(z.cx,.75);assert.equal(z.cy,.5);assert.equal(z.side,100);
});

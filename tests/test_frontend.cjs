const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const {test} = require('node:test');
const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'panorama_viewer_template.html'), 'utf8');

// Run the actual script with small DOM/graphics substitutes. Browser layout is tested separately.
function harness(config = {scenes:[]}) {
  let active;
  const ids = new Map(), createdImages = [], frames = new Map(), revoked = [];
  let frameId = 0, drawCount = 0;
  class Node {
    constructor(tag='DIV') {
      this.tagName = tag.toUpperCase(); this.children=[]; this.style={}; this.dataset={}; this.events={};
      this.attributes={}; this.value=''; this.textContent=''; this.disabled=false; this.hidden=false;
      this.clientWidth=800; this.clientHeight=600; this.width=800; this.height=600;
      const classes = new Set();
      this.classList={add:x=>classes.add(x),remove:x=>classes.delete(x),contains:x=>classes.has(x),
        toggle:(x,on)=>on ? classes.add(x) : classes.delete(x)};
    }
    addEventListener(name,fn){(this.events[name] ||= []).push(fn);}
    emit(name,e={}){for(const f of this.events[name] || []) f({...e,currentTarget:this});}
    setAttribute(name,value){this.attributes[name]=value;}
    appendChild(n){this.children.push(n);return n;}
    replaceChildren(){this.children=[];}
    querySelectorAll(){return [];}
    focus(){active=this;}
    closest(){return ['INPUT','SELECT','BUTTON','TEXTAREA','A'].includes(this.tagName) ? this : null;}
    setPointerCapture(){}
    click(){if(!this.disabled)this.emit('click');}
    getContext(type){return type === '2d' ? ctx2d : gl;}
  }
  const ctx2d = {drawImage(){},clearRect(){},getImageData:(x,y,w,h)=>({width:w,height:h,data:new Uint8ClampedArray(w*h*4)}),
    createImageData:(w,h)=>({width:w,height:h,data:new Uint8ClampedArray(w*h*4)}),putImageData(){}};
  let lost=false, uploadError=0, uploadFail=false, textureLimit=8192;
  const gl = {MAX_TEXTURE_SIZE:3379,NO_ERROR:0,isContextLost:()=>lost,getParameter:()=>textureLimit,
    getError:()=>{const x=uploadError;uploadError=0;return x;},
    getProgramParameter:()=>true,getShaderParameter:()=>true, getAttribLocation:()=>0,getUniformLocation:()=>({}),
    createTexture:()=>({}),createBuffer:()=>({}),createProgram:()=>({}),createShader:()=>({}),
    texImage2D(){if(uploadFail)uploadError=1285;},drawArrays(){drawCount++;}};
  for(const method of ['bindBuffer','bufferData','useProgram','enableVertexAttribArray','vertexAttribPointer','uniform1i',
    'attachShader','linkProgram','shaderSource','compileShader','deleteShader','deleteProgram','deleteTexture','activeTexture','bindTexture',
    'pixelStorei','texParameteri','viewport','clearColor','clear','uniform1f']) gl[method]=()=>{};
  const stage = new Node();
  for(const match of html.matchAll(/<([\w-]+)[^>]*\bid="([^"]+)"[^>]*>/g)) {
    const n = new Node(match[1]); n.id = match[2]; if(match[0].includes('hidden'))n.classList.add('hidden'); ids.set(n.id,n);
  }
  const document = new Node();
  document.getElementById = id=>{assert.ok(ids.has(id), `missing DOM id: ${id}`); return ids.get(id);};
  document.querySelector = ()=>stage;
  document.createElement = tag=>new Node(tag);
  Object.defineProperty(document,'activeElement',{get:()=>active});
  document.hidden=false;
  class FakeImage {
    constructor(){createdImages.push(this);this.naturalWidth=1024;this.naturalHeight=512;this.width=1024;this.height=512;}
    complete=false;
  }
  const context = {document, Image:FakeImage,console,performance:{now:()=>100},devicePixelRatio:1,
    Float32Array,Uint8Array,DataView,TextDecoder,Blob,Math,Date,Map,Set,Number,String,URL:{revokeObjectURL:u=>revoked.push(u)},
    requestAnimationFrame:fn=>{frames.set(++frameId,fn);return frameId;},cancelAnimationFrame:id=>frames.delete(id),
    setTimeout:()=>1,clearTimeout(){},addEventListener(){},};
  context.window=context;
  let script = html.match(/<script>([\s\S]*?)<\/script>/)[1].replace('const CFG = __CONFIG__;',`const CFG = ${JSON.stringify(config)};`);
  script = script.replace('    })();', `globalThis.check = {state, scenes, loadScene, finiteHeading, viewHeadingDegrees, sceneHeading, readImageMetadata, validPanorama, renderFallback, upload, loop, requestRender};\n    })();`);
  vm.runInNewContext(script,context);
  return {api:context.check, desktop:context.panoramaDesktop, ids, document, stage, createdImages, frames, revoked, gl,
    get drawCount(){return drawCount;},
    finish(){createdImages.at(-1).onload();},
    setLost(value){lost=value;},setUploadFailure(value){uploadFail=value;},setTextureLimit(value){textureLimit=value;},
    flush(){const tasks=[...frames.values()];frames.clear();for(const task of tasks)task(200);}};
}
const scene=(id='a',heading=null)=>({id,label:id,source_name:id+'.jpg',image:id+'.jpg',thumbnail:id+'.jpg',heading_degrees:heading,optimized_width:1024,optimized_height:512});

test('empty configuration is actionable and has no initialization exception',()=>{
  const h=harness(); assert.match(h.ids.get('errorTitle').textContent,/添加/);assert.equal(h.ids.get('addSceneBtn').disabled,false);
  assert.equal(h.ids.get('loading').classList.contains('hidden'),true);
});
test('missing directions are unknown while numeric zero remains north',()=>{
  const {api}=harness();
  for(const value of [null,undefined,'', ' ',NaN,Infinity,false]) assert.equal(api.finiteHeading(value),null);
  assert.equal(api.viewHeadingDegrees(scene('x',0),0),0);
  assert.equal(api.viewHeadingDegrees(scene('x',null),90),null);
});
test('seam shift preserves the reported heading of the same original pixel',()=>{
  const {api}=harness();const s=scene('x',90);s.seam_shift_px=256;
  assert.equal(api.sceneHeading(s),0);
  assert.equal(api.viewHeadingDegrees(s,90),90); // original centre now 1/4 turn left
});
test('bad panorama ratio is rejected',()=>{
  const {api}=harness();assert.ok(api.validPanorama(14400,7200));assert.ok(!api.validPanorama(640,640));assert.ok(!api.validPanorama(32769,16));
});
test('failure followed by a good image clears the error and restores controls',()=>{
  const h=harness({scenes:[scene('a'),scene('bad')]});h.finish();
  h.api.loadScene(1);h.createdImages.at(-1).onerror();assert.equal(h.ids.get('error').classList.contains('hidden'),false);
  h.api.loadScene(0);h.finish();assert.equal(h.ids.get('error').classList.contains('hidden'),true);assert.equal(h.ids.get('autoBtn').disabled,false);
});
test('stale loading completion cannot replace the newer scene',()=>{
  const h=harness({scenes:[scene('a'),scene('b')]});const oldLoad=h.createdImages[0].onload;
  h.api.loadScene(1);h.finish();oldLoad();assert.equal(h.ids.get('sceneTitle').textContent,'b');
});
test('upload failure falls back for one image and next scene recovers',()=>{
  const h=harness({scenes:[scene('a'),scene('b')]});h.setUploadFailure(true);h.finish();assert.ok(h.stage.classList.contains('compat'));
  h.setUploadFailure(false);h.api.loadScene(1);h.finish();assert.equal(h.stage.classList.contains('compat'),false);
});
test('oversized valid panorama is scaled to the device texture limit',()=>{
  const h=harness();const size=h.api.upload({naturalWidth:14400,naturalHeight:7200});assert.equal(size.width,8192);assert.equal(size.height,4096);assert.ok(size.scaled);
});
test('keyboard controls leave sliders and buttons alone',()=>{
  const h=harness({scenes:[scene('a')]});h.finish();
  h.document.emit('keydown',{target:h.ids.get('zoom'),code:'ArrowRight'});assert.equal(h.api.state.yaw,0);
  h.document.emit('keydown',{target:h.ids.get('skyBtn'),code:'Space'});assert.equal(h.api.state.auto,false);
  let prevented=false;h.document.emit('keydown',{target:h.ids.get('gl'),code:'ArrowRight',preventDefault(){prevented=true;}});
  assert.ok(prevented);assert.notEqual(h.api.state.yaw,0);
});
test('two pointer pinch changes zoom, lifts cleanly and resumes one-pointer drag',()=>{
  const h=harness({scenes:[scene('a')]});h.finish();const canvas=h.ids.get('gl');
  const event=(id,x)=>({pointerId:id,clientX:x,clientY:100,pointerType:'touch',preventDefault(){}});
  canvas.emit('pointerdown',event(1,100));canvas.emit('pointerdown',event(2,200));canvas.emit('pointermove',event(2,300));
  assert.equal(h.api.state.fov,37.5);canvas.emit('pointerup',event(2,300));canvas.emit('pointermove',event(1,120));
  assert.notEqual(h.api.state.yaw,0);canvas.emit('pointerup',event(1,120));assert.equal(h.api.state.drag,false);
});
test('an idle frame stops rendering until input changes',()=>{
  const h=harness({scenes:[scene('a')]});h.finish();h.flush();const before=h.drawCount;assert.equal(h.frames.size,0);h.flush();assert.equal(h.drawCount,before);
});
test('context loss and restoration reload current scene without changing view',()=>{
  const h=harness({scenes:[scene('a')]});h.finish();h.api.state.yaw=.8;h.setLost(true);
  h.ids.get('gl').emit('webglcontextlost',{preventDefault(){}});assert.ok(h.stage.classList.contains('compat'));
  h.setLost(false);h.ids.get('gl').emit('webglcontextrestored');h.finish();assert.equal(h.stage.classList.contains('compat'),false);assert.equal(h.api.state.yaw,.8);
});
test('actual JPEG EXIF date is read without guessing filesystem time',async()=>{
  const h=harness();const bytes=fs.readFileSync(path.join(__dirname,'fixtures/exif-date.jpg'));
  const data=await h.api.readImageMetadata(new Blob([bytes]));assert.equal(data.capture_time,'2020-01-02 03:04:05');assert.equal(data.heading_degrees,null);
});
test('malformed metadata is tolerated and XMP zero heading is valid',async()=>{
  const {api}=harness();const data=await api.readImageMetadata(new Blob(['GPano:PoseHeadingDegrees="0" exif:DateTimeOriginal="2024-02-29T12:34:56"']));
  assert.equal(data.heading_degrees,0);assert.equal(data.capture_time,'2024-02-29 12:34:56');
  assert.equal((await api.readImageMetadata(new Blob([new Uint8Array([0xff,0xd8,0xff,0xe1,0,200])]))).heading_degrees,null);
});

test('desktop restores selected scene and its saved camera',()=>{
  const b={...scene('b'),saved_view:{yaw:.7,pitch:-.4,fov:38,manual_heading:0},manual_heading:0};
  const h=harness({desktop:true,desktop_current:'b',scenes:[scene('a'),b]});h.finish();
  assert.equal(h.api.state.scene,1);assert.equal(h.api.state.yaw,.7);assert.equal(h.api.state.fov,38);
  assert.equal(h.desktop.snapshot().views.b.manual_heading,0);
});

test('desktop switching scenes remembers each camera independently',()=>{
  const h=harness({desktop:true,scenes:[scene('a'),scene('b')]});h.finish();
  h.api.state.yaw=1.2;h.api.state.fov=44;h.api.loadScene(1);h.finish();
  h.api.state.pitch=-.9;h.api.loadScene(0);h.finish();
  assert.equal(h.api.state.yaw,1.2);assert.equal(h.api.state.fov,44);
  assert.equal(h.desktop.snapshot().views.b.pitch,-.9);
});

test('desktop snapshot sequence continues after a page reload',()=>{
  const h=harness({desktop:true,desktop_sequence:28,scenes:[scene('a')]});h.finish();
  assert.equal(h.desktop.snapshot().sequence,29);assert.equal(h.desktop.snapshot().sequence,30);
});

test('desktop accepts native file results and ignores duplicate scene entries',()=>{
  const h=harness({desktop:true,scenes:[scene('a')]});h.finish();
  h.desktop.receive({scenes:[scene('b'),scene('b')],selected:'b',recent:[],failures:[]});h.finish();
  assert.equal(h.api.scenes.length,2);assert.equal(h.api.state.scene,1);
});

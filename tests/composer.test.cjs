const {test}=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../web/app.js'),'utf8');
function harness(platform){
  const nodes=new Map(),messages=[],listeners={};
  const document={getElementById(id){if(!nodes.has(id))nodes.set(id,{value:'',disabled:false,selectionStart:0,selectionEnd:0,setRangeText(text,start,end){this.value=this.value.slice(0,start)+text+this.value.slice(end);this.selectionStart=this.selectionEnd=start+text.length;},dispatchEvent(e){this['on'+e.type]?.(e);},setAttribute(){}});return nodes.get(id);},addEventListener(type,fn){listeners[type]=fn;}};
  const window=platform==='mac'?{webkit:{messageHandlers:{hud:{postMessage:m=>messages.push(m)}}}}:{chrome:{webview:{postMessage:m=>messages.push(m),addEventListener:(name,fn)=>listeners.native=fn}}};
  const context=vm.createContext({window,document,setInterval(){},Event:class{constructor(type){this.type=type;}},console});
  vm.runInContext(source,context);
  vm.runInContext("agents=[{id:'fixture',online:true,agent_status:'idle',agent:'codex'}];selected='fixture';",context);
  const input=document.getElementById('prompt');
  function key(extra={}){let prevented=false;input.onkeydown({key:'Enter',preventDefault(){prevented=true;},...extra});return prevented;}
  function reset(text){input.value=text;input.selectionStart=1;input.selectionEnd=3;vm.runInContext('sending=false;renderHeader()',context);messages.length=0;}
  return {context,window,messages,input,key,reset,listeners};
}
for(const platform of ['mac','windows']){
  test(`${platform}: native bridge and Enter send exactly once`,()=>{
    const h=harness(platform);assert.equal(h.messages[0].op,'ready');h.reset('hello');
    assert.equal(h.key(),true);assert.equal(h.messages.filter(m=>m.op==='prompt').length,1);assert.equal(h.messages.at(-1).message,'hello');
    h.key();assert.equal(h.messages.filter(m=>m.op==='prompt').length,1);
  });
  test(`${platform}: modifier Enter replaces selection with newline, never sends`,()=>{
    const h=harness(platform);
    for(const modifier of ['ctrlKey','shiftKey','metaKey','altKey']){h.reset('abcd');assert.equal(h.key({[modifier]:true}),true);assert.equal(h.input.value,'a\nd');assert.equal(h.messages.length,0);}
  });
  test(`${platform}: IME and held Enter never submit`,()=>{
    const h=harness(platform);h.reset('hello');assert.equal(h.key({isComposing:true}),false);assert.equal(h.key({keyCode:229}),false);h.key({repeat:true});assert.equal(h.messages.length,0);
  });
}
test('Windows native events reach the shared receiver',()=>{const h=harness('windows');let got;h.window.receive=value=>got=value;h.listeners.native({data:{type:'notice',data:{message:'fixture'}}});assert.equal(got.data.message,'fixture');});
test('phone: ops go over HTTP after the event stream says hello, Return adds a line',()=>{
  const nodes=new Map(),posts=[],sources=[];
  const document={documentElement:{classList:{add(){}}},body:{classList:{add(){},remove(){}}},getElementById(id){if(!nodes.has(id))nodes.set(id,{value:'',disabled:false,selectionStart:0,selectionEnd:0,setRangeText(){},dispatchEvent(e){this['on'+e.type]?.(e);},setAttribute(){}});return nodes.get(id);},addEventListener(){},hidden:false};
  class EventSource{constructor(url){this.url=url;this.l={};sources.push(this);}addEventListener(t,f){this.l[t]=f;}}
  const fetch=(url,opts)=>{posts.push({url,opts});return Promise.resolve({status:204});};
  const context=vm.createContext({window:{},document,EventSource,fetch,location:{reload(){}},setInterval(){},Event:class{constructor(t){this.type=t;}},console});
  vm.runInContext(source,context);
  assert.equal(posts.length,0,'ready is queued until the stream is open');
  assert.equal(sources[0].url,'api/events');
  sources[0].l.hello({data:JSON.stringify({client:'c1'})});
  assert.equal(posts.length,1);assert.equal(posts[0].url,'api/op');
  assert.equal(posts[0].opts.headers['X-HUD-Client'],'c1');assert.equal(JSON.parse(posts[0].opts.body).op,'ready');
  vm.runInContext("agents=[{id:'a',online:true,agent_status:'idle',agent:'claude'}];selected='a';sending=false;",context);
  const input=document.getElementById('prompt');input.value='hi';let prevented=false;
  input.onkeydown({key:'Enter',preventDefault(){prevented=true;}});
  assert.equal(prevented,false);assert.equal(posts.filter(p=>JSON.parse(p.opts.body).op==='prompt').length,0);
});

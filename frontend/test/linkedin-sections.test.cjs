const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const Module = require('node:module');
const ts = require('typescript');
const React = require('react');
const {renderToStaticMarkup} = require('react-dom/server');
const frontend = path.resolve(__dirname,'..');
let states, refs, effects, stateIndex, refIndex, params;
const realResolve = Module._resolveFilename;
const realLoad = Module._load;
Module._resolveFilename = function(request,parent,...rest) {
  return realResolve.call(this,request.startsWith('@/') ? path.join(frontend,'src',request.slice(2)) : request,parent,...rest);
};
Module._load = function(request,parent,...rest) {
  if (request==='next/navigation') return {useRouter:()=>({push:()=>{}}),useSearchParams:()=>params};
  if (request==='react' && parent?.filename.endsWith('intelligence-page.tsx')) return {...React,
    useState(initial) {
      const index=stateIndex++;
      if (!(index in states)) states[index]=typeof initial==='function' ? initial() : initial;
      return [states[index],value=>{states[index]=typeof value==='function' ? value(states[index]) : value;}];
    },
    useRef(initial) {const index=refIndex++;return refs[index] || (refs[index]={current:initial});},
    useCallback:fn=>fn, useMemo:fn=>fn(), useEffect:fn=>{effects.push(fn);},
  };
  return realLoad.call(this,request,parent,...rest);
};
for (const ext of ['.ts','.tsx']) require.extensions[ext]=(mod,filename)=>mod._compile(ts.transpileModule(fs.readFileSync(filename,'utf8'),{
  fileName:filename,compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020,jsx:ts.JsxEmit.ReactJSX,esModuleInterop:true},
}).outputText,filename);
const {IntelligenceWorkspace}=require('../src/features/intelligence/components/intelligence-page.tsx');
const source=(id,url)=>({id,url,name:id,type:'LINKEDIN',status:'ACTIVE',collection_enabled:true,fetch_interval_minutes:1440});
const sources=[source('PrimexEU','https://linkedin.com/company/primexeu/'),source('Mendex','https://linkedin.com/company/mendex-ai/'),
  source('GA','https://linkedin.com/in/ganimete-arifaj-canolli-27641927a/'),
  source('Other profile','https://linkedin.com/in/other/'),source('Other company','https://linkedin.com/company/other/')];
const items=sources.map((s,index)=>({id:s.id,sourceId:s.id,sourceName:s.name,sourceType:'LINKEDIN',title:`Activity from ${s.id}`,url:null,
  originalText:null,publishedAt:'2026-10-20T08:00:00Z',createdAt:'2026-10-20T08:01:00Z',priority:'NORMAL',
  linkedin:{kind:index===4?'REPOST':'POST',authorName:s.name,reactionCount:null,commentCount:null,repostCount:null,comments:[]},repostGroups:[],
  analysis:{summary:'Stored activity',category:'NEWS',tags:[],deadline:null}}));

for (const [view,wanted,unwanted] of [
  ['primex-linkedin',['PrimexEU','Mendex','GA'],['Other profile','Other company']],
  ['repost-watch',['Other profile','Other company'],['PrimexEU','Mendex','GA']],
]) test(`${view} requests and renders only its assigned LinkedIn accounts`,async()=>{
  states=[];refs=[];effects=[];params=new URLSearchParams(view==='repost-watch'?'tab=repost-watch':'');
  const requests=[];
  const apiFetch=async url=>{
    requests.push(url);
    if (url==='/intelligence/linkedin-sources') return new Response(JSON.stringify(sources));
    if (url.startsWith('/intelligence/items?')) return new Response(JSON.stringify({items,hasLiveSources:true,emailConfigured:false,aiConfigured:false}));
    throw new Error(`Unexpected API request: ${url}`);
  };
  function render() {stateIndex=0;refIndex=0;effects=[];return renderToStaticMarkup(React.createElement(IntelligenceWorkspace,{view,user:null,apiFetch}));}
  render();effects[0]();
  for (let turn=0;turn<10 && states[4];turn++) await new Promise(setImmediate);
  const html=render();
  assert.equal(states[4],false,'feed finished loading');
  const feedRequests=requests.filter(url=>url.startsWith('/intelligence/items?'));
  assert.equal(feedRequests.length,1);
  const query=new URL(feedRequests[0],'http://test.local').searchParams;
  assert.deepEqual(query.getAll('source_ids'),wanted);
  assert.equal(query.has('repost_group'),false,'account membership must not depend on legacy CEO/company tags');
  for (const name of wanted) assert(html.includes(`Activity from ${name}`));
  for (const name of unwanted) assert(!html.includes(`Activity from ${name}`));
  assert(html.includes('LISTA GA'));
  assert(!html.includes('SMM LIST ACTIVITY'));
  if (view==='primex-linkedin') {
    assert(!html.includes('linkedin-account-filter'),'primary accounts must be visible outside the dropdown');
    const accountNav=html.match(/<nav aria-label="LinkedIn accounts"[^>]*>([\s\S]*?)<\/nav>/);
    assert(accountNav,'primary account navigation is visible');
    const links=[...accountNav[1].matchAll(/<a[^>]*href="([^"]+)"[^>]*>([^<]+)<\/a>/g)].map(m=>({href:m[1],label:m[2]}));
    assert.deepEqual(links,[
      {href:'/intelligence/primex-linkedin',label:'All accounts'},
      {href:'/intelligence/primex-linkedin?company=primex',label:'PrimexEU'},
      {href:'/intelligence/primex-linkedin?company=mendex',label:'Mendex'},
      {href:'/intelligence/primex-linkedin?company=ganimete',label:'GA'},
    ]);
  } else {
    assert(html.includes('linkedin-account-filter'),'LISTA GA retains its account dropdown');
    assert(!html.includes('aria-label="LinkedIn accounts"'));
  }
  assert(html.includes('>Posts</button>') && html.includes('>Reposts</button>'));
});

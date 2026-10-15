const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const ctx=vm.createContext({assert,URLSearchParams,location:{search:'',hash:'#logs'},document:{addEventListener(){}},
esc:v=>String(v??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;'),
filterValues:v=>v==='all'?null:Array.isArray(v)?v:[v],multiFilter:()=>'',heading:()=>'',stamp:v=>v||'',fmt:String});
vm.runInContext(fs.readFileSync(__dirname+'/logs.js','utf8'),ctx);
vm.runInContext(`
const query=new URLSearchParams(logsQuery({...logsSelection,strategy:['S-015','S-019'],severity:['error','warning']}));
assert.equal(query.get('strategy'),'["S-015","S-019"]');assert.equal(query.get('severity'),'["error","warning"]');
const row={event_id:'1',received_utc:'2026-10-15T10:00:00Z',host_id:'vps',terminal_id:'t',stream:'experts',account:'1',strategy:'S-019',strategy_source:'source',severity:'error',severity_source:'text',raw_line:'<img src=x onerror="alert(1)">'};
logsData={rows:[row],terminals:[],strategies:['S-019'],page:0,page_size:50,total:1,severity_counts:{error:1,warning:0},as_of:'now'};
const html=realLogs();assert(!html.includes('<img'));assert(html.includes('&lt;img'));assert(html.includes('log-row-error'));assert(html.includes('Ошибка · по тексту'));assert(html.includes('Только проблемы'));
assert.equal(logsStrategyLabel('__unassigned__'),'Без стратегии');
assert.equal(logsSeverityClass({severity:'<script>'}),'info');
`,ctx);
console.log('PASS: strategy/level multiselect serialization, raw log escaping, explicit inferred level, colour and unknown-strategy labels');

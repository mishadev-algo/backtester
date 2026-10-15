// Package only allowlisted assets and previously approved mapping decisions.
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const mapping=require('../mapping-model.js');
const [auditPath,snapshotPath,out]=process.argv.slice(2);
if(!out)throw Error('Usage: node deploy/package_live.js audit.json snapshot.json output-dir');
const audit=JSON.parse(fs.readFileSync(auditPath)),snapshot=JSON.parse(fs.readFileSync(snapshotPath));
const raw=new Map(snapshot.deals.map(d=>[mapping.key(d),d])),analysis=mapping.analyse(snapshot.deals),seed={};
for(const a of audit.rows.filter(a=>a.applied)){
 const k=mapping.key(a),d=raw.get(k),c=analysis.result.get(k),anchor=raw.get(c?.anchor);
 assert.ok(d&&c?.status==='candidate'&&anchor,'Approved deal must still have valid evidence');
 for(const f of ['magic','position_id','symbol','comment'])assert.equal(a[f],d[f]);
 assert.equal(a.source_strategy,d.strategy);assert.equal(a.target,c.target);assert.equal(a.anchor,c.anchor);assert.equal(a.method,c.method);
 seed[k]={target:c.target,fingerprint:JSON.stringify([d.magic,d.comment,d.position_id,d.symbol,d.entry,c.target,c.method,c.anchor,anchor.magic,anchor.comment,anchor.strategy,'v1'])};
}
fs.mkdirSync(out,{recursive:true,mode:0o700});
const files=['backtest_api.py','backtest_jobs.py','backtest_worker.py','backtests.js','backtests.css','index.html','real.js','real.css','journey.js','journey.css','mapping-model.js','mapping-ui.js','serve.py','serve_live.py','read_snapshot.sql','logs.js','logs_api.py','read_logs.sql','classify_logs.sql'];
for(const f of files){
 let text=fs.readFileSync(path.join(__dirname,'..',f),'utf8');
 if(f==='index.html')text=text.replace('<script src="real.js">','<script src="live-config.js"></script>\n<script src="real.js">');
 if(f==='real.js'){
  text=text.replace('ЛОКАЛЬНЫЙ ПРОТОТИП','LIVE · READ ONLY').replaceAll('Локальное представление','Рабочий дашборд').replaceAll('ATLAS · Локальный прототип / реальные данные VPS','ATLAS · Live / реальные данные VPS');
  text=text.replaceAll('VPS временно недоступен.','Не удалось прочитать данные сервера.').replaceAll('Проверьте SSH-соединение и нажмите «Обновить с VPS».','Повторите обновление данных.');
  text=text.replaceAll('Чтение PostgreSQL через SSH, в транзакции READ ONLY. Снимок хранится в памяти локального процесса. Обновление — по кнопке.','Чтение PostgreSQL на сервере в транзакции READ ONLY. Снимок обновляется при загрузке страницы и по кнопке.');
  text=text.replaceAll('Данные VPS в локальном интерфейсе','Данные live-дашборда');
  text=text.replace("document.querySelector('.top-actions').prepend(e)","document.querySelector('.top-actions').prepend(e);const legacy=document.createElement('a');legacy.className='btn';legacy.href='/legacy';legacy.textContent='Прежний дашборд';document.querySelector('.top-actions').prepend(legacy)");
 }
 if(f==='mapping-ui.js'){
  text=text.replace('mappingSaved={},mappingView=', 'mappingSaved=window.ATLAS_LIVE_CONFIG.mappings,mappingView=');
  text=text.replaceAll('Применено локально','Принятые привязки').replaceAll('LOCAL RECONCILIATION / MAGIC & COMMENTS','ПРИВЯЗКИ / MAGIC & COMMENTS');
 }
 fs.writeFileSync(path.join(out,f),text);
}
fs.writeFileSync(path.join(out,'live-config.js'),'window.ATLAS_LIVE_CONFIG = '+JSON.stringify({mappings:seed})+';\n');
fs.copyFileSync(path.join(__dirname,'atlas-workspace.service'),path.join(out,'atlas-workspace.service'));
fs.writeFileSync(path.join(out,'manifest.json'),JSON.stringify({created_at:new Date().toISOString(),approved_mappings:Object.keys(seed).length,source_snapshot:snapshot.fetched_at,files},null,2));
console.log(JSON.stringify({output:out,approvedMappings:Object.keys(seed).length,files:files.length}));

/* Pure, conservative attribution. Raw VPS strategy and monetary fields never change. */
(function(root){
 const key=d=>JSON.stringify([d.server,String(d.login),String(d.ticket)]);
 const pos=d=>JSON.stringify([d.server,String(d.login),String(d.position_id)]);
 const missing=s=>!s||s==='unmapped';
 const family=s=>(String(s||'').match(/^S-(\d{3})(?=[_\s:.\-]|$)/)||[])[1];
 const symbols={gold:new Set(['XAUUSD','XAUUSD.x']),nas:new Set(['NAS100','NAS100.x','NDX100','US100.cash','USTEC']),ger:new Set(['DAX40','DE40','GER40.cash','GER40.x','GER30'])};
 const source={
  s009:'30_validation/S-009/Sets/S-009-GER-Safe.set:26; S-009-NQ-FundNext.set:26; deployed/S-009_v1.3-delay.mq5:619–624',
  s009reverse:'30_validation/S-009/Sets/S-009-GER-Safe.set:26; deployed/S-009_v1.3-delay.mq5:690,695 — TryOpenStopReversal: ORB reverse short / long',
  s015:'30_validation/S-015/deployed/NAS100/S-015-1.3.9-SC-FC.set:49; XAUUSD/S-015-1.2.9-DC-FC.set:50; EA: ORB Buy / ORB Sell',
  s014:'30_validation/S-014/deployed/EURUSD/hikkake-eur-money.set:6; именованные сделки VPS с magic 415015',
  s019:'30_validation/S-019/S-019_v0.4.9.mq5; явный префикс S-019 в комментарии сделки'
 };
 function instrument(d){return symbols.gold.has(d.symbol)?'XAUUSD':symbols.nas.has(d.symbol)?'Nasdaq':symbols.ger.has(d.symbol)?'DAX':d.symbol}
 function label(f,d){return `S-${f} · ${instrument(d)} · версия не установлена`}
 function expected(d){const m=String(d.magic);if(m==='12345'&&(symbols.gold.has(d.symbol)||symbols.nas.has(d.symbol)))return '015';if(m==='505051'&&symbols.nas.has(d.symbol)||m==='505052'&&symbols.ger.has(d.symbol))return '009';if(m==='415015'&&['EURUSD','US500','US500.cash'].includes(d.symbol))return '014';return ''}
 function direct(d){
  const c=String(d.comment||'').trim(),f=family(c),exp=expected(d);
  if(f){if(exp&&exp!==f)return {conflict:true,reason:'Комментарий и ожидаемое семейство magic противоречат друг другу'};if(['009','014','015','019','036'].includes(f))return {family:f,method:'comment',reason:'Явный идентификатор S-'+f+' в комментарии',source:f==='019'?source.s019:'Явный комментарий сделки; существующие семейства стратегий',version:(c.match(/\bv(\d+(?:\.\d+)+)/)||[])[1]||null};return {conflict:true,reason:'Неизвестный явный идентификатор S-'+f+' требует проверки'}}
  if(![0,2].includes(d.entry))return null;
  if(exp==='015'&&/^ORB (Buy|Sell)$/.test(c))return {family:exp,method:'source',reason:'Magic 12345 + инструмент + ORB Buy/Sell совпадают с исходниками S-015',source:source.s015};
  if(exp==='009'&&/^ORB (long|short)$/.test(c))return {family:exp,method:'source',reason:'Magic + инструмент + ORB long/short совпадают с .set и исходником S-009',source:source.s009};
  if(exp==='009'&&/^ORB reverse (long|short)$/.test(c))return {family:exp,method:'source',reason:'Magic + инструмент + ORB reverse long/short подтверждены веткой TryOpenStopReversal в S-009',source:source.s009reverse};
  if(exp==='014'&&/^Hikkake (low|high) sweep/.test(c))return {family:exp,method:'source',reason:'Magic 415015 + инструмент + Hikkake согласуются с S-014',source:source.s014};
  return null;
 }
 function externalName(d){
  for(const text of [d.source_strategy,d.strategy,d.comment].map(v=>String(v||'').trim())){
   if(/^QQ(?:$|\[T\d+\/S\d+\])/.test(text))return 'QQ';
   if(/^(?:The[ _]+)?Gold[ _]+Reaper(?:$|[_ :\-])/.test(text))return 'Gold Reaper';
   if(/^(?:Golg|Gold)[ _-]*Grid(?=$|[ _:.\[\-]|v\d)/i.test(text))return 'GolgGrid';
  }
  return '';
 }
 function analyse(deals){
  const trades=deals.filter(d=>d.kind==='trade'),positions=new Map(),scope=new Map(),result=new Map();
  for(const d of trades){if(String(d.position_id)!=='0'&&d.position_id){let p=pos(d);if(!positions.has(p))positions.set(p,[]);positions.get(p).push(d)}const k=JSON.stringify([d.server,String(d.login),String(d.magic),d.symbol]);if(!scope.has(k))scope.set(k,[]);scope.get(k).push(d)}
  for(const d of trades){if(!missing(d.strategy))continue;const evidence=direct(d),siblings=positions.get(pos(d))||[d];
   const anchors=siblings.map(s=>({deal:s,evidence:!missing(s.strategy)&&family(s.strategy)?{family:family(s.strategy),method:'vps',source:'Именованная запись VPS',reason:'Существующая стратегия в VPS'}:direct(s)})).filter(a=>a.evidence);
   const families=new Set(anchors.map(a=>a.evidence.family).filter(Boolean));
   const scopeRows=scope.get(JSON.stringify([d.server,String(d.login),String(d.magic),d.symbol]))||[];
   const scopeFamilies=new Set(scopeRows.map(s=>!missing(s.strategy)?family(s.strategy):direct(s)?.family).filter(Boolean));
   const namedConflict=scopeRows.concat(siblings).some(s=>!missing(s.strategy)&&family(s.strategy)&&((direct(s)?.family&&direct(s).family!==family(s.strategy))||direct(s)?.conflict));
   if(evidence?.conflict||namedConflict||anchors.some(a=>a.evidence.conflict)||families.size>1||new Set(siblings.map(s=>s.symbol)).size>1||scopeFamilies.size>1){result.set(key(d),{status:'conflict',reason:evidence?.reason||'В позиции или в связке счёт + magic + символ обнаружены разные семейства'});continue}
   let anchor=evidence?.family?{deal:d,evidence}:anchors.find(a=>a.evidence.family);
   if(anchor){const a=anchor.evidence;result.set(key(d),{status:'candidate',family:a.family,target:label(a.family,d),method:anchor.deal===d?a.method:'position',reason:anchor.deal===d?a.reason:'Та же позиция, счёт и символ; семейство определено по опорной записи',source:a.source,anchor:key(anchor.deal),anchorTicket:String(anchor.deal.ticket),observedVersion:a.version||null,rule:'local-attribution-v1'});continue}
   const magic=String(d.magic);
   const scopeExternals=new Set(scopeRows.map(externalName).filter(Boolean));
   const ignoredEA=!scopeFamilies.size&&scopeExternals.size<=1&&(
    symbols.gold.has(d.symbol)&&magic==='1234'&&scopeRows.some(s=>/^QQ\[T\d+\/S\d+\]/.test(String(s.comment||'').trim()))?'QQ':
    symbols.gold.has(d.symbol)&&['8001','8005','8008','8002','8012','8009','8014','8015'].includes(magic)&&scopeRows.some(s=>/^The Gold Reaper_XAUUSD_[1-8]$/.test(String(s.comment||'').trim()))?'Gold Reaper':
    externalName(d)==='GolgGrid'||magic!=='0'&&scopeRows.some(s=>externalName(s)==='GolgGrid')?'GolgGrid':'');
   if(ignoredEA){result.set(key(d),{status:'ignored',excludedEA:ignoredEA,reason:ignoredEA+' · исключено из статистики и PnL локально',ignoredBy:'user',rule:'local-ignore-ea-v2'});continue}
   const c=String(d.comment||'');let hint='Нет опорной записи с достаточными признаками';
   if(/^The Gold Reaper_/.test(c)||/^80\d\d$/.test(String(d.magic)))hint='Gold Reaper: имя внешнего EA видно, соответствие вашему каталогу S-xxx неизвестно';
   else if(String(d.magic)==='1234')hint='QQ: комментарии QQ[T…/S…], соответствие вашему каталогу неизвестно';
   else if(/^(ORB (Buy|Sell))$/.test(c)||['12305','12347','12001','12302'].includes(String(d.magic)))hint=`Возможная S-015 · ${instrument(d)} · magic ${d.magic}: нет подтверждения в сохранённых параметрах и реестре`;
   else if(String(d.magic)==='0')hint='Magic 0: без опорной позиции нельзя отличить ручное исполнение от закрытия EA';
   result.set(key(d),{status:'unresolved',reason:hint});
  }
  const candidates=[...result.values()].filter(r=>r.status==='candidate');
  return {result,summary:{unmapped:result.size,candidates:candidates.length,conflicts:[...result.values()].filter(r=>r.status==='conflict').length,unresolved:[...result.values()].filter(r=>r.status==='unresolved').length,ignored:[...result.values()].filter(r=>r.status==='ignored').length,methods:candidates.reduce((m,r)=>(m[r.method]=(m[r.method]||0)+1,m),{})}};
 }
 function exclusions(deals,analysis=analyse(deals)){
  const result=new Map(),positions=new Map();
  for(const d of deals.filter(d=>d.kind==='trade')){
   const a=analysis.result.get(key(d)),name=externalName(d);
   if(a?.status==='ignored')result.set(key(d),{name:a.excludedEA,method:'confirmed-scope',rule:'local-statistics-exclusion-v1'});
   // Explicit external strategy labels are also excluded, unless S-xxx evidence conflicts.
   else if(!missing(d.strategy)&&name&&!family(d.strategy)&&!direct(d)?.family&&!direct(d)?.conflict)result.set(key(d),{name,method:'explicit-name',rule:'local-statistics-exclusion-v1'});
   if(d.position_id&&String(d.position_id)!=='0'){const k=pos(d);if(!positions.has(k))positions.set(k,[]);positions.get(k).push(d)}
  }
  for(const rows of positions.values()){
   const names=new Set(rows.map(d=>result.get(key(d))?.name).filter(Boolean));
   const conflict=names.size>1||new Set(rows.map(d=>d.symbol)).size>1||rows.some(d=>family(d.strategy)||direct(d)?.family||direct(d)?.conflict||analysis.result.get(key(d))?.status==='conflict');
   if(conflict){for(const d of rows)result.delete(key(d));continue}
   if(names.size===1)for(const d of rows)if(!result.has(key(d))&&(missing(d.strategy)||externalName(d)===[...names][0]))result.set(key(d),{name:[...names][0],method:'position',rule:'local-statistics-exclusion-v1'});
  }
  return result;
 }
 const api={key,pos,missing,family,direct,analyse,instrument,externalName,exclusions};
 if(typeof module!=='undefined'&&module.exports)module.exports=api;else root.AtlasMapping=api;
})(typeof globalThis!=='undefined'?globalThis:this);

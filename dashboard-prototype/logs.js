// Server-filtered logs; intentionally independent of portfolio/PnL filters.
let logsSelection={host:'all',terminal:'all',stream:'all',strategy:'all',severity:'all',q:'',start:'',end:'',page:0,legacy_terminal:''};
let logsData=null,logsBusy=false,logsError='',logsAttempted=false,logsRequest=0,logsApplied=null;
const logsLegacy=new URLSearchParams(location.search);
if(location.hash==='#logs'){
 for(const f of ['host','stream'])if(logsLegacy.get(f))logsSelection[f]=[logsLegacy.get(f)];
 logsSelection.q=(logsLegacy.get('q')||'').slice(0,200);logsSelection.legacy_terminal=logsLegacy.get('terminal')||'';
}
function ensureLogs(){if(!logsAttempted)fetchLogs()}
function logsSourceStrip(){return `<div class="source-strip ${logsError?'error':''}"><div><strong>Журнал VPS</strong><span>${logsData?stamp(logsData.fetched_at):'Чтение журнала…'}</span></div><button class="btn" data-action="logs-refresh" ${logsBusy?'disabled':''}>${icon('refresh')}${logsBusy?'Загрузка…':'Обновить логи'}</button></div>`}
function logsQuery(selection=logsSelection,before=''){
 const params=new URLSearchParams();for(const f of ['host','terminal','stream','strategy','severity'])params.set(f,JSON.stringify(filterValues(selection[f])));
 for(const f of ['q','start','end','page','legacy_terminal'])params.set(f,String(selection[f]||''));
 params.set('page',String(selection.page||0));if(before)params.set('before',before);return params.toString();
}
async function fetchLogs(paginate=false){
 const request=++logsRequest,selection={...logsSelection};logsBusy=true;logsError='';logsAttempted=true;
 if(dataMode==='real'&&state.page==='logs')render();
 try{
  const res=await fetch('/api/logs?'+logsQuery(selection,paginate?logsData?.as_of:''),{cache:'no-store'});
  if(!res.ok)throw Error(res.status===400?'Проверьте период и фильтры.':'Не удалось прочитать журнал. Попробуйте обновить ещё раз.');
  const data=await res.json();if(!Array.isArray(data.rows)||!Array.isArray(data.terminals))throw Error('Неверный ответ журнала.');
  if(request!==logsRequest)return;
  logsData=data;logsApplied=selection;
  if(selection.legacy_terminal){logsSelection.terminal=data.terminals.filter(t=>t.terminal_id===selection.legacy_terminal&&filterMatches(selection.host,t.host_id)).map(t=>t.key);logsSelection.legacy_terminal=''}
 }catch(e){if(request===logsRequest)logsError=e.message}
 finally{if(request===logsRequest){logsBusy=false;if(dataMode==='real'&&state.page==='logs')render()}}
}
function logsStrategyLabel(value){return value==='__unassigned__'?'Без стратегии':value}
function logsSeverityLabel(value){return ({error:'Ошибка',warning:'Предупреждение',info:'Информация'})[value]||'Информация'}
function logsSeverityClass(row){return ['error','warning'].includes(row.severity)?row.severity:'info'}
function logsSeverityBadge(row){return `<span class="log-level log-level-${logsSeverityClass(row)}" title="${esc(logsSeverityReason(row))}">${logsSeverityLabel(row.severity)}${row.severity_source==='text'?' · по тексту':''}</span>`}
function logsSeverityReason(row){return ({payload:'Уровень указан советником в EA_EVENT_V1',mt5:'Уровень указан в строке MT5',text:'Предположительный уровень по тексту сообщения',default:'Явных признаков ошибки или предупреждения нет'})[row.severity_source]||''}
function logsStrategyReason(row){return ({payload:'Стратегия указана в strategy_id',source:'Стратегия определена по имени советника',prefix:'Стратегия указана в начале сообщения',unknown:'В записи нет явного идентификатора стратегии'})[row.strategy_source]||''}
function logsMulti(field,label,allLabel,options){return multiFilter(field,label,allLabel,{options,selection:logsSelection[field],scope:'logs'})}
function realLogs(){
 const terminals=logsData?.terminals||[],hosts=[...new Set(terminals.map(t=>t.host_id))].sort();
 const filters=`<form id="logs-search-form" class="panel logs-filters"><div class="real-filters">${logsMulti('strategy','Стратегии','Все стратегии',(logsData?.strategies||[]).map(v=>[v,logsStrategyLabel(v)]))}${logsMulti('severity','Уровень','Все уровни',[['error','Ошибки'],['warning','Предупреждения'],['info','Информация']])}${logsMulti('host','VPS','Все VPS',hosts.map(h=>[h,h]))}${logsMulti('terminal','Счета / терминалы','Все счета',terminals.map(t=>[t.key,t.label+' · '+t.host_id]))}${logsMulti('stream','Источник','Все источники',[['journal','Journal'],['experts','Experts']])}<div><label for="logs-start">С даты · UTC</label><input id="logs-start" type="date" value="${esc(logsSelection.start)}"></div><div><label for="logs-end">По дату · UTC</label><input id="logs-end" type="date" value="${esc(logsSelection.end)}"></div></div><div class="logs-search-row"><div class="logs-search-input"><label for="logs-query">Текст сообщения</label><input id="logs-query" type="search" maxlength="200" placeholder="Текст, ticket, magic или имя советника…" value="${esc(logsSelection.q)}"></div><button type="submit" class="btn primary" ${logsBusy?'disabled':''}>Найти</button><button type="button" class="btn" data-action="logs-reset">Сбросить</button></div><p class="filter-help">Время получения сервером · UTC. Фильтры журнала независимы от фильтров торговли. Исключения EA из PnL не скрывают логи.</p></form>`;
 const headingHTML=heading('ДАННЫЕ / ЖУРНАЛ','Логи терминалов','Journal и Experts со всех VPS. Поиск по всей сохранённой истории.');
 const loading=logsBusy?'<div class="logs-loading" role="status">Обновляю журнал…</div>':'';
 const error=logsError?`<div class="notice" role="alert">${esc(logsError)}${logsData?' Ниже — предыдущая успешно загруженная выборка.':''} ${button('Повторить','logs-retry')}</div>`:'';
 if(!logsData)return headingHTML+filters+error+loading+(!logsBusy&&!logsError?empty('Загружаю журнал','Получаю последние события терминалов.'): '');
 const d=logsData,pages=Math.max(1,Math.ceil(d.total/d.page_size)),a=logsApplied;
 const summary=[a?.q?'Текст: '+a.q:'',a?.start||a?.end?(a.start||'Начало истории')+' → '+(a.end||'Последняя запись'):''].filter(Boolean).join(' · ');
 return headingHTML+filters+error+loading+`<div class="logs-problems"><button type="button" class="btn" data-action="logs-problems" ${logsBusy?'disabled':''}>Только проблемы</button><span class="log-level log-level-error">Ошибок: ${fmt(d.severity_counts?.error||0)}</span><span class="log-level log-level-warning">Предупреждений: ${fmt(d.severity_counts?.warning||0)}</span><small>По текущим фильтрам, до выбора уровня</small></div><div class="real-meta logs-meta" aria-live="polite"><span><b>${fmt(d.total)}</b> найдено</span><span>${d.rows.length} на странице · ${d.page+1} / ${pages}</span><span>Последнее событие: ${stamp(d.latest)}</span></div>${summary?`<p class="logs-applied">${esc(summary)}</p>`:''}<section class="panel logs-panel" aria-busy="${logsBusy}"><div class="panelhead"><div><h2>События терминалов</h2><p>От новых к старым · нажмите на сообщение для полного текста</p></div><span class="badge blue">Только чтение</span></div><div class="table-scroll"><table class="table logs-table"><thead><tr><th>ПОЛУЧЕНО · UTC</th><th>СЧЁТ / VPS</th><th>СТРАТЕГИЯ / УРОВЕНЬ</th><th>СООБЩЕНИЕ</th></tr></thead><tbody>${d.rows.map((r,i)=>`<tr class="log-row-${logsSeverityClass(r)}"><td class="logs-time">${esc(r.received_utc.slice(0,10))}<small>${esc(r.received_utc.slice(11,19))}</small></td><td class="logs-account">${esc(r.account||'Терминал '+r.terminal_id.slice(0,10))}<small>${esc(r.host_id)}</small></td><td class="log-classification"><strong title="${esc(logsStrategyReason(r))}">${esc(logsStrategyLabel(r.strategy))}</strong>${logsSeverityBadge(r)}<small>${esc(r.stream==='experts'?'Experts':r.stream==='journal'?'Journal':r.stream)}</small></td><td><button class="logs-message" data-action="logs-detail:${i}" aria-label="Открыть сообщение ${i+1}"><span>${esc(r.raw_line)}</span></button></td></tr>`).join('')||'<tr><td colspan="4"><div class="logs-empty"><strong>Ничего не найдено</strong><p>Измените текст, период или выбранные терминалы.</p></div></td></tr>'}</tbody></table></div><div class="real-pagination"><span>${d.total?'Записи '+(d.page*50+1)+'–'+(d.page*50+d.rows.length)+' из '+fmt(d.total):'Нет записей'}</span><div class="row"><button class="btn" data-action="logs-prev" ${logsBusy||d.page===0?'disabled':''}>← Назад</button><button class="btn" data-action="logs-next" ${logsBusy||(d.page+1)*50>=d.total?'disabled':''}>Далее →</button></div></div></section><p class="logs-footnote">При переходе между страницами выборка фиксируется на ${stamp(d.as_of)}. «Обновить логи» загружает новые события. Получение может быть позже времени строки MT5.</p><p class="logs-footnote">Стратегия определяется только по явному ID или имени советника. Общие события терминала доступны как «Без стратегии». Метка «по тексту» — предположение; исходный текст сохранён.</p>`;
}
function logsCaptureDraft(){for(const f of ['q','start','end']){const e=document.getElementById(f==='q'?'logs-query':'logs-'+f);if(e)logsSelection[f]=e.value.trim()}}
function logsAction(a){
 if(a==='logs-problems'){logsCaptureDraft();logsSelection.severity=['error','warning'];logsSelection.page=0;return fetchLogs()}
 if(a==='logs-refresh'||a==='logs-retry'){logsCaptureDraft();logsSelection.page=0;return fetchLogs()}
 if(a==='logs-reset'){logsSelection={host:'all',terminal:'all',stream:'all',strategy:'all',severity:'all',q:'',start:'',end:'',page:0,legacy_terminal:''};return fetchLogs()}
 if(a==='logs-next'||a==='logs-prev'){
  if(logsBusy||!logsData)return;logsSelection={...logsApplied,page:Math.max(0,logsData.page+(a==='logs-next'?1:-1))};return fetchLogs(true);
 }
 if(a.startsWith('logs-detail:')){
  const row=logsData?.rows[Number(a.split(':')[1])];if(!row)return;
  overlay(`<div class="eyebrow">${esc(row.stream.toUpperCase())} / ${esc(row.host_id)}</div><h1>Запись журнала</h1><p class="subtitle">${esc(row.account)} · ${stamp(row.received_utc)}</p><div class="logs-detail-labels"><strong title="${esc(logsStrategyReason(row))}">${esc(logsStrategyLabel(row.strategy))}</strong>${logsSeverityBadge(row)}</div><p class="logs-footnote">${esc(logsStrategyReason(row))}. ${esc(logsSeverityReason(row))}.</p><pre class="logs-full log-full-${logsSeverityClass(row)}">${esc(row.raw_line)}</pre><details class="data-explainer"><summary>Идентификаторы источника</summary><p>Терминал: ${esc(row.terminal_id)}</p><p>Event ID: ${esc(row.event_id)}</p></details>${button('Закрыть','close',true)}`,true);
  document.querySelector('.drawer')?.setAttribute('aria-label','Запись журнала');
 }
}
document.addEventListener('submit',e=>{if(e.target.id==='logs-search-form'){e.preventDefault();logsCaptureDraft();logsSelection.page=0;fetchLogs()}});

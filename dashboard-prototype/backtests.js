/* Real experiment workspace; imported backtests remain a separate library. */
const bt={catalog:null,jobs:[],error:'',loading:false,tab:'run',selected:null,timer:null,submitting:false,form:null};
const importedBacktests=realBacktests;
function btStrategy(){return bt.catalog?.strategies.find(s=>s.id===bt.form?.strategy)}
function btDefaults(){
 const stop=new Date(bt.catalog.dataset.last_timestamp);stop.setUTCDate(stop.getUTCDate()+1);const end=stop.toISOString().slice(0,10),d=new Date(end+'T00:00:00Z');d.setUTCDate(d.getUTCDate()-30);
 bt.form={mode:'backtest',strategy:'s019',start:d.toISOString().slice(0,10),end,initial_cash:100000,commission_bps:0,slippage_bps:0,spread_bps:0,warmup_days:180,parameters:JSON.stringify(bt.catalog.strategies[0].parameters,null,2)};
}
async function btRequest(path,options={}){
 const response=await fetch('/api/backtest/'+path,{cache:'no-store',...options});
 const body=await response.json();if(!response.ok)throw Error(body.error||'Не удалось выполнить запрос');return body;
}
async function btLoad(){
 if(bt.loading)return;bt.loading=true;
 try{const [cat,runs]=await Promise.all([btRequest('catalog'),btRequest('jobs')]);bt.catalog=cat;bt.jobs=runs.jobs;bt.error='';if(!bt.form&&cat.ready)btDefaults();}
 catch(e){bt.error=e.message}finally{bt.loading=false;if(dataMode==='real'&&state.page==='backtests'){
 const advanced=document.querySelector('.bt-advanced')?.open,focused=document.activeElement,param=focused?.dataset?.btParam,key=focused?.dataset?.btField,selection=['parameters','grid'].includes(key)?[focused.selectionStart,focused.selectionEnd]:null;
 render();if(advanced&&document.querySelector('.bt-advanced'))document.querySelector('.bt-advanced').open=true;if(key||param){const input=document.getElementById(param?'bt-param-'+param:'bt-'+key);input?.focus({preventScroll:true});if(selection)input?.setSelectionRange(...selection)}
 }}
}
function btField(key,label,type='number',extra=''){
 return `<div class="field"><label for="bt-${key}">${label}</label><input id="bt-${key}" data-bt-field="${key}" type="${type}" value="${esc(bt.form[key])}" ${extra}></div>`;
}
function btParameterFields(){
 let params;try{params=JSON.parse(bt.form.parameters)}catch{return '<p class="help">Исправьте JSON, чтобы редактировать отдельные параметры.</p>'}
 const fields=bt.form.strategy==='s019'?[['InpRiskMoney','Риск на сделку'],['InpADRPeriods','Период ADR'],['InpLongADRMultiplier','Long × ADR'],['InpShortADRMultiplier','Short × ADR'],['InpLimitEntryFromStop','Доля входа от стопа'],['InpTakeProfitRR','Take profit / риск'],['InpSlowEMAPeriod','Период Slow EMA'],['InpDailyPositionLimit','Открытий в день']]:[['fast','Быстрая SMA'],['slow','Медленная SMA']];
 return '<div class="fields">'+fields.map(([key,label])=>`<div class="field"><label for="bt-param-${key}">${label}</label><input id="bt-param-${key}" type="number" step="any" data-bt-param="${key}" value="${esc(params[key])}"></div>`).join('')+'</div>';
}
const btModeNames={backtest:'Обычный бэктест',optimization:'Оптимизация',walk_forward:'Walk-forward'};
function btResearchDefaults(mode){
 const f=bt.form;f.mode=mode;
 if(mode==='backtest')return;
 f.strategy='s019';f.spread_bps=0;
 const start=new Date(f.end+'T00:00:00Z');start.setUTCMonth(start.getUTCMonth()-(mode==='walk_forward'?30:12));
 f.start=start.toISOString().slice(0,10);
 if(f.start<bt.catalog.dataset.first_timestamp.slice(0,10))f.start=bt.catalog.dataset.first_timestamp.slice(0,10);
 const begin=Date.parse(f.start),span=Date.parse(f.end)-begin;
 Object.assign(f,{fit_end:new Date(begin+span*.6).toISOString().slice(0,10),validation_end:new Date(begin+span*.8).toISOString().slice(0,10),
 trials:12,shortlist:3,seed:20261015,min_fit_trades:30,min_validation_trades:10,fit_months:18,validation_months:6,test_months:6,
 grid:JSON.stringify(bt.catalog.research.grid,null,2)});
}
function btResearchFields(){
 const f=bt.form;if(f.mode==='backtest')return '';
 return `<section class="bt-research"><h3>${btModeNames[f.mode]} · отбор параметров</h3><p class="help">Исходные параметры всегда входят в поиск. Кандидаты проверяются на обучении, финалисты — на валидации с 3 seed. Тест не участвует в выборе. Риск, капитал и расходы фиксированы.</p><div class="fields">${f.mode==='optimization'?btField('fit_end','Конец обучения · UTC','date')+btField('validation_end','Конец валидации / начало теста · UTC','date'):btField('fit_months','Обучение, месяцев','number','min="1" max="36"')+btField('validation_months','Валидация, месяцев','number','min="1" max="12"')+btField('test_months','Тест и шаг окна, месяцев','number','min="1" max="12"')}${btField('trials','Кандидатов на окно, включая исходный','number','min="2" max="32"')}${btField('shortlist','Финалистов на валидации','number','min="1" max="5"')}${btField('min_fit_trades','Мин. закрытых сделок · обучение','number','min="1" max="1000"')}${btField('min_validation_trades','Мин. сделок · медиана валидации','number','min="1" max="1000"')}${btField('seed','Seed поиска','number','min="1" max="2147483645"')}</div><div class="field"><label for="bt-grid">Сетка поиска · JSON, до 8 значений на параметр</label><textarea id="bt-grid" data-bt-field="grid" rows="12" spellcheck="false">${esc(f.grid)}</textarea></div><p class="help">Критерий: прибыль / max(риск на сделку, денежная просадка). Нужны прибыль на обучении и положительная валидация в 2 из 3 seed. Если кандидатов нет, тестовое окно остаётся в кэше. Лимит расчёта — 2 часа; для первого запуска уменьшите число кандидатов.</p></section>`;
}
function btPayload(){
 const payload={};for(const key of ['mode','strategy','start','end','initial_cash','commission_bps','slippage_bps','spread_bps','warmup_days'])payload[key]=bt.form[key];
 payload.parameters=JSON.parse(bt.form.parameters);
 if(payload.mode!=='backtest'){
  payload.research={grid:JSON.parse(bt.form.grid)};
  for(const key of ['trials','shortlist','seed','min_fit_trades','min_validation_trades',...(payload.mode==='optimization'?['fit_end','validation_end']:['fit_months','validation_months','test_months'])])payload.research[key]=bt.form[key];
 }
 return payload;
}
function btComparison(job){
 const r=job.summary?.research;if(!r)return '';
 const num=v=>Number(v).toLocaleString('ru-RU',{maximumFractionDigits:2}),base='/api/backtest/jobs/'+job.id+'/files/';
 return `<section class="bt-comparison"><h3>Сравнение на одинаковом тестовом периоде</h3><p class="help">${esc(r.oos_start)} → ${esc(r.oos_end)} UTC · обучение и валидация исключены. Обычный бэктест рассчитан автоматически с исходными параметрами, тем же капиталом, расходами и seed.</p><div class="bt-table-wrap"><table><thead><tr><th>Вариант</th><th>Equity</th><th>Доходность</th><th>Макс. DD</th></tr></thead><tbody>${[[btModeNames[r.mode],r.selected],['Обычный бэктест',r.baseline]].map(([name,s])=>`<tr><td>${esc(name)}</td><td>${num(s.final_equity)}</td><td>${(s.total_return*100).toFixed(2)}%</td><td>${(s.max_drawdown*100).toFixed(2)}%</td></tr>`).join('')}</tbody></table></div><p>Разница equity: <b>${num(r.selected.final_equity-r.baseline.final_equity)}</b> · расчётов: ${r.simulations}</p>${r.cash_windows?`<div class="notice">Окон без прошедшего отбор кандидата: ${r.cash_windows}. В этих окнах — кэш, без новых сделок.</div>`:''}<h3>Тестовые окна и выбранные параметры</h3>${r.windows.map(w=>`<details><summary>Окно ${w.window+1} · ${esc(w.test_start)} → ${esc(w.test_end)} · PnL ${num(w.oos_profit)} · ${w.decision==='cash'?'кэш':'кандидат '+(w.selected_candidate+1)}</summary><p class="help">Обучение ${esc(w.fit_start)} → ${esc(w.validation_start)}; валидация → ${esc(w.test_start)}. Прошли обучение: ${w.fit_eligible}.</p>${w.parameters?`<pre>${esc(JSON.stringify(w.parameters,null,2))}</pre><button class="btn" data-action="bt-replay:${job.id}:${w.window}">Взять параметры для обычного бэктеста</button>`:''}<div class="bt-table-wrap"><table><tr><th>Кандидат</th><th>Score валидации</th><th>Отбор</th></tr>${w.ranking.map(x=>`<tr><td>${x.candidate+1}</td><td>${num(x.validation_score)}</td><td>${x.eligible?'Прошёл':'Не прошёл'}</td></tr>`).join('')}</table></div></details>`).join('')}<div class="row bt-downloads">${['baseline-report.html','trials.csv','selection-locks.json','comparison.json','protocol.json','baseline-fills.csv','baseline-equity.csv'].map(f=>`<a class="btn" target="_blank" rel="noopener" href="${base+f}">${esc(f==='baseline-report.html'?'Отчёт обычного бэктеста ↗':f)}</a>`).join('')}</div></section>`;
}
function btStatus(job){return {queued:'В очереди',running:'Расчёт',succeeded:'Готово',failed:'Ошибка',cancelled:'Отменён'}[job.status]||job.status}
function btResult(job){
 if(!job)return '<p class="help">После запуска здесь появятся ход расчёта и отчёт.</p>';
 const s=job.summary,base='/api/backtest/jobs/'+job.id+'/files/';
 return `<div class="panelhead"><div><h2>${esc(btModeNames[job.request.mode||'backtest'])} · ${esc(bt.catalog?.strategies.find(x=>x.id===job.request.strategy)?.name||job.request.strategy)}</h2><p>${esc(job.request.start)} → ${esc(job.request.end)} · UTC</p></div><span class="badge ${job.status==='failed'?'warn':''}">${btStatus(job)}</span></div><div class="bt-result"><p role="status">${esc(job.stage)}${job.cancel&&job.status==='running'?' · Отмена запрошена':''}</p>${['queued','running'].includes(job.status)?`<progress max="100" value="${job.progress}" aria-label="Прогресс расчёта"></progress><p class="help">${Math.floor(job.progress)}% · прогресс по этапам и обработанной истории. Можно закрыть страницу.</p><button class="btn" data-action="bt-cancel:${job.id}" ${job.cancel?'disabled':''}>Отменить расчёт</button>`:''}${job.error?`<div class="notice" role="alert">${esc(job.error)}</div>`:''}${s?`<div class="cards"><div class="card"><small>Конечная equity</small><div class="value">${Number(s.final_equity).toLocaleString('ru-RU',{maximumFractionDigits:2})}</div><small>${job.request.strategy==='s019'?esc(job.request.parameters.broker.account_currency):'Денежные единицы'}</small></div><div class="card"><small>Доходность</small><div class="value">${(s.total_return*100).toFixed(2)}%</div></div><div class="card"><small>Максимальная просадка</small><div class="value">${(s.max_drawdown*100).toFixed(2)}%</div></div><div class="card"><small>Исполнений</small><div class="value">${s.fill_count}</div><small>Входы и выходы отдельно</small></div></div><p class="help">Обработано: ${Number(s.ticks_processed??s.bars_processed).toLocaleString('ru-RU')} ${s.data_kind==='ticks'?'тиков':'баров'}. Открытый объём в конце: ${s.open_quantity}. Позиции оценены по рынку, без принудительного закрытия.</p><div class="row bt-downloads"><a class="btn primary" target="_blank" rel="noopener" href="${base}report.html">Открыть график и отчёт ↗</a>${['fills.csv','daily_pnl.csv','equity.csv','result.json'].map(f=>`<a class="btn" download href="${base+f}">${f}</a>`).join('')}</div>`:''}${btComparison(job)}${job.request.mode&&job.request.mode!=='backtest'?`<p class="help"><a href="${base}protocol.json" target="_blank">Протокол исследования</a> · <a href="${base}trials.jsonl" target="_blank">Журнал попыток</a></p>`:''}<details><summary>Параметры этого запуска</summary><pre>${esc(JSON.stringify(job.request,null,2))}</pre></details><small>ID: ${esc(job.id)} · ${esc(job.created)}</small></div>`;
}
realBacktests=function(){
 const tabs=`<div class="tabs"><button data-action="bt-tab:run" class="${bt.tab==='run'?'on':''}">Запуск и отчёты</button><button data-action="bt-tab:imported" class="${bt.tab==='imported'?'on':''}">Импортированные бэктесты</button></div>`;
 if(bt.tab==='imported')return tabs+(remote?importedBacktests():empty('Снимок VPS недоступен','Обновите данные для просмотра импортированной библиотеки.'));
 if(!bt.catalog&&!bt.loading&&!bt.error)setTimeout(btLoad,0);
 const header=heading('ИССЛЕДОВАНИЯ / РАСЧЁТ','Бэктестер','Расчёт на исторических котировках. Запуски и отчёты сохраняются на сервере.',button('Обновить','bt-refresh'));
 const error=bt.error?`<div class="notice" role="alert">${esc(bt.error)}</div>`:'';
 if(!bt.catalog?.ready)return tabs+header+error+empty(bt.loading?'Подготовка…':'Данные ещё не готовы','После подготовки тиковой истории здесь появится форма запуска.');
 clearTimeout(bt.timer);
 if(bt.jobs.some(j=>['queued','running'].includes(j.status)))bt.timer=setTimeout(()=>{if(dataMode==='real'&&state.page==='backtests'&&bt.tab==='run')btLoad()},2000);
 const selected=bt.jobs.find(j=>j.id===bt.selected)||bt.jobs[0],f=bt.form;
 return tabs+header+error+`<div class="bt-layout"><section class="panel bt-form"><h2>Новый эксперимент</h2><div class="field"><label for="bt-mode">Режим расчёта</label><select id="bt-mode" data-bt-field="mode">${Object.entries(btModeNames).map(([key,name])=>`<option value="${key}" ${f.mode===key?'selected':''}>${name}</option>`).join('')}</select></div><div class="field"><label for="bt-strategy">Стратегия</label><select id="bt-strategy" data-bt-field="strategy" ${f.mode!=='backtest'?'disabled':''}>${bt.catalog.strategies.map(s=>`<option value="${s.id}" ${f.strategy===s.id?'selected':''}>${esc(s.name)}</option>`).join('')}</select></div><p class="help">${esc(btStrategy().description)}</p><div class="notice">${esc(bt.catalog.dataset.spec.symbol)} · ${bt.catalog.dataset.rows.toLocaleString('ru-RU')} тиков<br>${esc(bt.catalog.dataset.first_timestamp)} → ${esc(bt.catalog.dataset.last_timestamp)}</div><div class="fields">${btField('start','Начало включительно · UTC','date')}${btField('end','Конец не включительно · UTC','date')}${btField('initial_cash','Начальный капитал','number','min="1" step="100"')}${btField('warmup_days','Прогрев, календарных дней','number','min="0" max="730" step="1"')}${btField('commission_bps','Комиссия, bps','number','min="0" step="0.01"')}${btField('slippage_bps','Проскальзывание, bps','number','min="0" step="0.01"')}${f.strategy==='sma'?btField('spread_bps','Спред, bps','number','min="0" step="0.01"'):''}</div><p class="help">1 bps = 0,01%. В S-019 используется исходный Bid/Ask-спред. Прогрев строит индикаторы на предшествующей истории без торговли.</p>${btParameterFields()}${btResearchFields()}<details class="bt-advanced"><summary>Все параметры и спецификация контракта · JSON</summary><div class="field"><label for="bt-parameters">Параметры стратегии · JSON</label><textarea id="bt-parameters" data-bt-field="parameters" spellcheck="false" rows="14">${esc(f.parameters)}</textarea></div></details><button class="btn primary" data-action="bt-submit" ${bt.submitting?'disabled':''}>${bt.submitting?'Добавляю в очередь…':'Запустить: '+btModeNames[f.mode]}</button><p class="help">Один расчёт одновременно. Торговые команды брокеру не отправляются.</p></section><div><section class="panel">${btResult(selected)}</section><section class="panel bt-history"><div class="panelhead"><h2>История запусков</h2><span>${bt.jobs.length}</span></div>${bt.jobs.length?bt.jobs.map(j=>`<button class="bt-history-row ${j.id===selected?.id?'selected':''}" data-action="bt-select:${j.id}"><span><b>${esc(btModeNames[j.request.mode||'backtest'])} · ${esc(j.request.strategy.toUpperCase())}</b> · ${esc(j.request.start)} → ${esc(j.request.end)}<small>${esc(j.created.slice(0,19).replace('T',' '))} UTC</small></span><span>${btStatus(j)}</span></button>`).join(''):'<p class="help">Пока нет запусков.</p>'}</section></div></div>`;
};
const beforeBacktestAction=action;
action=function(a){if(!a.startsWith('bt-'))return beforeBacktestAction(a);return btAction(a)};
async function btAction(a){
 if(a==='bt-refresh')return btLoad();
 if(a.startsWith('bt-tab:')){bt.tab=a.split(':')[1];render();if(bt.tab==='run')btLoad();return;}
 if(a.startsWith('bt-select:')){bt.selected=a.split(':')[1];return render();}
 try{
  if(a.startsWith('bt-cancel:')){await btRequest('jobs/'+a.split(':')[1]+'/cancel',{method:'POST'});return btLoad();}
  if(a.startsWith('bt-replay:')){
   const [,id,index]=a.split(':');const job=bt.jobs.find(j=>j.id===id),w=job.summary.research.windows.find(w=>w.window===Number(index));
   bt.form={...job.request,mode:'backtest',start:w.test_start,end:w.test_end,parameters:JSON.stringify(w.parameters,null,2)};bt.error='';render();document.getElementById('bt-mode')?.scrollIntoView({behavior:'smooth'});return;
  }
  if(a==='bt-submit'){
   if(bt.submitting)return;const payload=btPayload();
   bt.submitting=true;bt.error='';render();
   const job=await btRequest('jobs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
   bt.selected=job.id;await btLoad();
  }
 }catch(e){bt.error=e.message}finally{bt.submitting=false;render()}
}
document.addEventListener('input',e=>{const k=e.target.dataset.btField;if(!k||['strategy','mode'].includes(k))return;bt.form[k]=e.target.type==='number'?Number(e.target.value):e.target.value;});
document.addEventListener('change',e=>{if(e.target.dataset.btField==='strategy'){bt.form.strategy=e.target.value;bt.form.parameters=JSON.stringify(btStrategy().parameters,null,2);bt.form.spread_bps=bt.form.strategy==='sma'?2:0;render();}});

document.addEventListener('input',e=>{const key=e.target.dataset.btParam;if(!key)return;try{const p=JSON.parse(bt.form.parameters);p[key]=Number(e.target.value);bt.form.parameters=JSON.stringify(p,null,2);const json=document.getElementById('bt-parameters');if(json)json.value=bt.form.parameters}catch{}});

document.addEventListener('change',e=>{if(e.target.dataset.btField==='mode'){if(bt.form.strategy!=='s019'&&e.target.value!=='backtest')bt.form.parameters=JSON.stringify(bt.catalog.strategies.find(s=>s.id==='s019').parameters,null,2);btResearchDefaults(e.target.value);render();}});

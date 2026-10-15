const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const ctx=vm.createContext({assert,Date,clearTimeout,setTimeout,realBacktests:()=>'',action:()=>{},document:{addEventListener(){},getElementById(){return null}},render(){},esc:v=>String(v??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;')});
vm.runInContext(fs.readFileSync(__dirname+'/backtests.js','utf8'),ctx);
vm.runInContext(`
bt.catalog={dataset:{first_timestamp:'2021-10-14T00:00:00Z',last_timestamp:'2026-10-13T23:59:00Z'},strategies:[{id:'s019',parameters:{broker:{account_currency:'USD'},InpTakeProfitRR:2}}],research:{grid:{InpTakeProfitRR:[2,3]}}};
btDefaults();let p=btPayload();assert.equal(p.mode,'backtest');assert(!p.research);
btResearchDefaults('optimization');p=btPayload();assert(p.start<p.research.fit_end&&p.research.fit_end<p.research.validation_end&&p.research.validation_end<p.end);assert(!('grid' in p));assert.equal(p.research.trials,12);assert(btResearchFields().includes('Конец обучения'));
btResearchDefaults('walk_forward');p=btPayload();assert.equal(p.research.fit_months,18);assert(!('fit_end' in p.research));
const stats={final_equity:100100,initial_cash:100000,total_return:.001,max_drawdown:.01,fill_count:4};
const job={id:'abc',summary:{research:{mode:'walk_forward',selected:stats,baseline:stats,oos_start:'2026-01-01',oos_end:'2026-02-01',simulations:7,cash_windows:1,windows:[{window:0,decision:'cash',test_start:'2026-01-01',test_end:'2026-02-01',fit_start:'2025-01-01',validation_start:'2025-07-01',fit_eligible:0,ranking:[],oos_profit:0}]}}};
assert(btComparison(job).includes('Обычный бэктест'));assert(btComparison(job).includes('кэш'));assert(btComparison(job).includes('baseline-report.html'));
bt.form.mode='backtest';assert(!btPayload().research);assert(!btResearchFields());
`,ctx);
console.log('PASS research mode payloads, chronological defaults, baseline comparison and cash outcome');

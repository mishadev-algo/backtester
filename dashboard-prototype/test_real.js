const {readFileSync}=require('node:fs');
const vm=require('node:vm');
const assert=require('node:assert/strict');
const node={onclick(){}};
const ctx=vm.createContext({console,assert,Intl,Date,Number,Map,Set,JSON,
 localStorage:{getItem(){return 'demo'}},icons:{},navs:[],render(){},action(){},search(){},
 document:{getElementById(){return node},addEventListener(){}},
 esc:v=>String(v).replace(/</g,'&lt;'),state:{page:'overview'}});
vm.runInContext(readFileSync(__dirname+'/real.js','utf8'),ctx);
vm.runInContext(`
remote={accounts:[
 {server:'A',login:'1',currency:'USD',history_start_day:'2026-01-01',latest_snapshot_utc:'2026-01-05T00:00:00Z'},
 {server:'B',login:'1',currency:'EUR',history_start_day:'2026-01-01',latest_snapshot_utc:'2026-01-05T00:00:00Z'}
],deals:[],backtests:[{run_id:'run',currency:'USD',starting_capital:'1000',first_day:'2026-01-01',last_day:'2026-01-05'}],
backtest_days:[{run_id:'run',day:'2026-01-01',net_pnl:'100'},{run_id:'run',day:'2026-01-03',net_pnl:'-220'}]};
const sample={server:'A',login:'1',ticket:'1',kind:'trade',entry:0,type:0,strategy:'S-1',symbol:'EURUSD',time_utc:'2026-01-01T12:00:00Z',profit:'0',commission:'-2',swap:'0',fee:'-1'};
remote.deals=[sample,{...sample,ticket:'2',entry:1,time_utc:'2026-01-03T12:00:00Z',profit:'100',commission:'-2'},
{...sample,ticket:'3',kind:'cash',profit:'10000'}, {...sample,server:'B',profit:'99999'}];
assert.equal(selectedDeals().length,2,'Cash and other currencies excluded');
assert.equal(sum(selectedDeals(),net),94,'Entry costs plus exit costs retained');
assert.equal(groups(selectedDeals())[0].exits,1,'Do not count entry as closed trade');
assert.notEqual(dkey(sample),dkey({...sample,server:'B'}),'Ticket IDs scoped to broker/account');
rf.start='2026-01-02';assert.equal(selectedDeals().length,1,'Inclusive UTC day filter');rf.start='';
const stats=runStats(remote.backtests[0]);assert.equal(stats.pnl,-120);assert.equal(stats.dd,20,'DD from running peak, not starting capital');
rf.compareRun='run';rf.compareStrategy='S-1';rf.compareAccount='A|1';rf.compareSymbol='EURUSD';
let c=compareModel();assert.equal(c.bt,-120);assert.equal(c.live,94);assert.equal(c.rows.length,5,'All calendar days in shared period');
assert.equal(c.rows[1].btPresent,false);assert.equal(c.rows[1].livePresent,false,'Missing days are not recorded zeros');
assert.equal(c.costs,-6,'Entry and exit costs are included');
remote.backtest_days.push({run_id:'run',day:'2026-01-02',net_pnl:'0'},{run_id:'run',day:'2026-01-01',net_pnl:'5'});
c=compareModel();assert.equal(c.rows[1].btPresent,true,'Recorded zero stays distinguishable from missing');assert.equal(c.bt,-115);assert.equal(c.rows[0].bt,105,'Duplicate date aggregates reconcile with total');
remote.deals.reverse();c=compareModel();assert.equal(c.start,'2026-01-01','History start does not depend on row order');
rf.compareStart='2026-01-03';rf.compareEnd='2026-01-03';c=compareModel();assert.equal(c.rows.length,1);assert.equal(c.live,97,'Costs accounted on execution date; earlier entry not shifted');assert.equal(c.bt,-220);
rf.compareStart='2025-12-01';rf.compareEnd='2026-02-01';c=compareModel();assert.equal(c.clipped,true);assert.equal(c.rows.length,5);
rf.compareStart='2026-02-30';assert.equal(compareModel().code,'range','Invalid dates fail');
rf.compareStart='2026-01-05';rf.compareEnd='2026-01-02';assert.equal(compareModel().code,'range');
rf.compareStart='2027-01-01';rf.compareEnd='';assert.equal(compareModel().code,'range');
rf.compareStart='';rf.compareEnd='';
remote.accounts[0].history_start_day='2026-01-04';c=compareModel();assert.equal(c.start,'2026-01-01','Existing older trade history remains usable');assert.equal(c.olderHistory,true,'Older coverage is explicitly flagged');
remote.backtests[0].last_day='2025-12-31';assert.match(compareModel().reason,/Нет общего периода/);
remote.backtests[0].currency='EUR';assert.match(compareModel().reason,/Валюты/);
assert.match(actualChart([[]]),/Нет данных/);
console.log('PASS: currency separation, cash exclusion, cost reconciliation, scoped IDs, date filtering, peak drawdown, comparison overlap.');
`,ctx);
vm.runInContext(`
remote.accounts.push({server:'C',login:'2',currency:'USD'});
remote.deals.push({...sample,server:'C',login:'2',ticket:'4',strategy:'S-2',symbol:'XAUUSD',profit:'10'}, {...sample,ticket:'5',strategy:'S-3',symbol:'USDJPY',profit:'20'});
rf.account=['A|1','C|2'];rf.strategy=['S-1','S-2'];rf.symbol=['EURUSD','XAUUSD'];
assert.equal(selectedAccounts().length,2,'Account multi-select unions accounts, keeps currency separate');
assert.equal(selectedDeals().length,3,'OR within selections, AND across filter dimensions');
assert.equal(sum(selectedDeals(),net),101,'Multiple selections retain costs and do not duplicate records');
rf.symbol=['XAUUSD'];assert.equal(selectedDeals().length,1,'Symbol intersects selected strategies and accounts');
rf.strategy=[];assert.equal(selectedDeals().length,0,'Empty selection means no records, not all');
rf.strategy='S-1';rf.symbol='all';assert.equal(selectedDeals().length,2,'Single-value navigation remains compatible');
rf.account=[];assert.equal(selectedAccounts().length,0);assert.equal(selectedDeals().length,0);
rf.account='all';rf.strategy='all';assert.equal(selectedDeals().length,4,'Reset restores all currency-scoped trades');
assert.equal(filterOptions('account').length,2,'No other-currency account options');
assert.equal(filterOptions('strategy').length,3,'Options remain stable across other filter selections');
console.log('PASS: multi-select unions, intersections, empty selections, totals, reset, single-value navigation and currency-scoped options.');
`,ctx);
vm.runInContext(`
remote.deals.push({...sample,ticket:'excluded',strategy:'QQ',profit:'-999999',_excluded_ea:{name:'QQ'}});
assert.equal(selectedDeals().length,4,'Excluded external EA never enters normal statistics/CSV selection');
assert.equal(selectedDeals({includeExcluded:true}).length,5,'Audit retains source rows');
assert.equal(filterOptions('strategy').some(x=>x[0]==='QQ'),false,'External EA absent from normal filter options');
rf.compareStrategy='QQ';remote.backtests[0].currency='USD';remote.backtests[0].last_day='2026-01-05';
assert.equal(compareModel().code,'empty','Excluded records cannot enter comparisons even via a stale saved selection');
rf.account=['A|1'];rf.start='2026-01-02';assert.equal(selectedDeals({includeExcluded:true}).some(d=>d.ticket==='excluded'),false,'Exclusion audit observes the same dates');
rf.account='all';rf.start='';assert.equal(selectedDeals().length,4,'Reset does not cancel exclusion');
console.log('PASS: excluded PnL/CSV rows, audit scope, filter options and stale comparison prevention.');
`,ctx);
ctx.AtlasMapping=require('./mapping-model.js');
vm.runInContext(readFileSync(__dirname+'/mapping-ui.js','utf8'),ctx);
vm.runInContext(`
const exclusionFixture={...remote,deals:[{...sample,ticket:'qq1',strategy:'unmapped',symbol:'XAUUSD',magic:'1234',comment:'QQ[T1/S01]',position_id:'qq'}, {...sample,ticket:'own1',strategy:'S-019',symbol:'DE40',magic:'190300',comment:'S-019 v0.4 long limit',position_id:'own'}]};
const originalExclusionFixture=JSON.stringify(exclusionFixture);
for(const enabled of [true,false]){mappingEnabled=enabled;rebuildMappings(exclusionFixture);assert.equal(selectedDeals().length,1,'Exclusions survive attribution toggle');assert.equal(selectedDeals()[0].ticket,'own1')}
assert.equal(JSON.stringify(exclusionFixture),originalExclusionFixture,'Rebuilding does not alter the snapshot');
console.log('PASS: integration of source snapshot, local exclusions and attribution toggles.');
`,ctx);

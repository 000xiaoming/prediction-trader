"""Optional browser smoke test: DASHBOARD_CHROME_BINARY enables headless Chrome."""
# ruff: noqa: E501

import html
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

import pytest

from prediction_trader.indicator_page import INDICATOR_HTML
from prediction_trader.profiles import list_profiles


@pytest.mark.skipif(
    not os.environ.get("DASHBOARD_CHROME_BINARY"),
    reason="Set DASHBOARD_CHROME_BINARY for actual browser interaction checks",
)
def test_dashboard_switching_quotes_orders_and_retry_in_browser() -> None:
    profiles = [profile.as_dict() for profile in list_profiles()]
    fixtures = r"""
window.errors=[];window.addEventListener('error',event=>window.errors.push(event.message));
window.addEventListener('unhandledrejection',event=>window.errors.push(String(event.reason)));
window.setInterval=()=>0;
const profiles=PROFILES_FIXTURE;
const instruments=['binance_btc_spot','binance_btc_perpetual','binance_eth_spot','binance_eth_perpetual'];
let posts=[],postMode='success',delayedState=null,delayProfile=null,delayHistory=null,delayedHistory=null;
let quoteOffline=false,accountError=false,delayAccount=null,delayedAccount=null;
let previews=[],delayPreview=false,delayedPreview=null,previewError=false,historyQueries=[];
let activityQueries=[],activityError=false,activityEmpty=false,activityMismatch=false,delayActivity=false,delayedActivity=null;
const answer=(body,status=200)=>Promise.resolve({ok:status<400,status,json:async()=>body});
function quoteState(id){const perp=id.includes('perpetual'),event=id.startsWith('polymarket');return {mode:'paper',portfolio_id:'paper:'+id,max_quote_age_seconds:10,instrument:{instrument_id:id,kind:perp?'perpetual':event?'binary_event':'spot',symbol:id,title:event?'<img src=x onerror=alert(1)>':'' ,quote_currency:'USDT',minimum_quantity:'0.001',quantity_step:'0.001',minimum_notional:'5',maximum_leverage:perp?'3':'1'},quote:{instrument_id:id,portfolio_id:'paper:'+id,bid:'60000',ask:'60001',observed_at:new Date().toISOString(),source:'fixture'},account:null}}
function account(id){return {portfolio_id:'paper:'+id,currency:'USDT',cash:'999.99',equity:'1000',unrealized_pnl:'0.01',realized_pnl:'0',fees:'0.01',funding:'0',available_margin:'900',fill_count:1,positions:[{instrument_id:id,quantity:'0.001',average_price:'60001',leverage:'1'}]}}
function historyState(id,query){const older=query.get('before')==='2',funding=query.get('event_type')==='funding',sequences=funding?[3]:older?[1]:[3,2];return {mode:'paper',instrument_id:id,portfolio_id:'paper:'+id,next_before:older||funding?null:2,events:sequences.map(sequence=>({sequence,event_type:funding?'funding':'fill',observed_at:new Date().toISOString(),payload:{result:funding?'-0.0001':{instrument_id:id,client_order_id:id+'-<img src=x onerror=alert(1)>',side:'buy',quantity:'0.001',price:'60001.000000000001',fee:'0.01',realized_pnl:'0'}}}))}}
function activityState(id,query){const count=activityEmpty?0:120;return {mode:'paper',basis:'recorded_events',instrument_id:activityMismatch?'wrong':id,portfolio_id:'paper:'+id,since:query.get('since'),until:query.get('until'),event_count:count,events_by_type:{fill:count,mark:0,funding:0,settlement:0},instruments:activityEmpty?[]:[{instrument_id:id,quote_currency:'USDT',events_by_type:{fill:count},buy_quantity:'0.001',sell_quantity:'0',buy_notional:'60.001000000000000001',sell_notional:'0',fees:'0.000000000000000001',fill_realized_pnl:'0',funding_payments:'-0.00001',settlement_payouts:'0'}]}}
window.fetch=(url,options={})=>{const parsed=new URL(url,'http://localhost');if(options.method==='POST'){const payload=JSON.parse(options.body);if(parsed.pathname==='/api/paper/preview'){previews.push(payload);const data={mode:'paper',preview:true,persisted:false,fill:{...payload,price:'60001',fee:'0.01'},account_before:account(payload.instrument_id),account_after:{...account(payload.instrument_id),cash:'900'}};if(delayPreview){delayPreview=false;return new Promise(resolve=>delayedPreview=()=>resolve({ok:true,json:async()=>data}))}return previewError?answer({detail:'insufficient balance'},400):answer(data)}posts.push(payload);if(postMode==='network'){postMode='success';return Promise.reject(new TypeError('network disconnected'))}return answer({mode:'paper',fill:{...payload,price:'60001',fee:'0.01'},account:account(payload.instrument_id)})}if(parsed.pathname==='/api/profiles')return answer({active:profiles[0],enabled:profiles,profiles,switching:'view_only'});if(parsed.pathname==='/api/state'){const id=parsed.searchParams.get('profile'),data={status:'waiting_for_market',profile:profiles.find(p=>p.id===id),research:{status:'disabled'}};if(delayProfile===id){delayProfile=null;return new Promise(resolve=>delayedState=()=>resolve({ok:true,json:async()=>data}))}return answer(data)}if(parsed.pathname==='/api/reports'){const id=parsed.searchParams.get('profile');return answer({profile:id,report:{profile_id:id,as_of:new Date().toISOString(),metrics:{market_count:2,prediction_count:1,prediction_coverage:0.5,realized_pnl_usd:'0.25',max_realized_drawdown_usd:'0.10'},limitations:['fixture limitations']}})}if(parsed.pathname==='/api/trading/activity'){activityQueries.push(new URLSearchParams(parsed.search));const data=activityState(parsed.searchParams.get('instrument'),parsed.searchParams);if(delayActivity){delayActivity=false;return new Promise(resolve=>delayedActivity=()=>resolve({ok:true,json:async()=>data}))}return activityError?answer({detail:'Activity exceeds 10000 events; use a smaller time range'},400):answer(data)}if(parsed.pathname==='/api/trading/history'){historyQueries.push(new URLSearchParams(parsed.search));const id=parsed.searchParams.get('instrument'),data=historyState(id,parsed.searchParams);if(delayHistory===id){delayHistory=null;return new Promise(resolve=>delayedHistory=()=>resolve({ok:true,json:async()=>data}))}return answer(data)}if(parsed.pathname==='/api/instruments')return answer({instruments:instruments.map(instrument_id=>({instrument_id,label:instrument_id}))});if(parsed.pathname==='/api/trading/account'){const id=parsed.searchParams.get('instrument'),data={mode:'paper',instrument_id:id,portfolio_id:'paper:'+id,account:account(id),valuation:'last_recorded_quotes',last_quote_at:new Date(Date.now()-60000).toISOString()};if(delayAccount===id){delayAccount=null;return new Promise(resolve=>delayedAccount=()=>resolve({ok:true,json:async()=>data}))}return accountError?answer({detail:'account unavailable'},500):answer(data)}if(parsed.pathname==='/api/trading/state')return quoteOffline?answer({detail:'quote feed offline'},400):answer(quoteState(parsed.searchParams.get('instrument')));return answer({detail:'unknown'},404)};
try{localStorage.clear()}catch{}
""".replace("PROFILES_FIXTURE", json.dumps(profiles))
    checks = r"""
(async()=>{
function check(value,message){if(!value)throw new Error(message)}
async function waitFor(condition){for(let i=0;i<100;i++){if(condition())return;await new Promise(resolve=>setTimeout(resolve,5))}throw new Error('browser wait timed out')}
await waitFor(()=>selectedProfile&&selectedInstrument);
check(posts.length===0,'initialization submitted an order');
check($('calc').children.length>0,'manual calculator disappeared');
check($('profileSelect').options.length===3,'enabled profile menu missing');
delayProfile=profiles[0].id;const old=refreshAnalysis();await waitFor(()=>delayedState);
$('profileSelect').value=profiles[2].id;$('profileSelect').dispatchEvent(new Event('change'));
await waitFor(()=>selectedProfile===profiles[2].id&&activeProfile.id===profiles[2].id);
delayedState();await old;check(activeProfile.id===profiles[2].id,'old profile response overwrote selected profile');
await waitFor(()=>$('reportMetrics').children.length>0);
check($('reportStatus').textContent.includes('独立研究回放'),'profile report missing');
const now=new Date().toISOString();render({status:'ok',profile:profiles[2],research:{status:'disabled'},market:{question:'ETH Up/Down',start:now,end:new Date(Date.now()+120000).toISOString(),remaining_seconds:120,fee_rate:'0.06',fee_rate_source:'market_metadata'},chainlink:{window_seconds:60,current:'2000',start:'1999',freshness_seconds:0,opening_tolerance_seconds:2},quality:{issues:[]},model:{up_probability:'0.6',down_probability:'0.4',distance_usd:'1',momentum_15s:'0.001',volatility_60s:'0.003',expected_remaining_move_usd:'2'},outcomes:[{label:'UP',ask:'0.55',bid:'0.54',updated_at:now,book_updated_at:now,max_age_seconds:5,quote_status:'fresh'}]});
check($('probUp').textContent==='60.00%'&&$('probDown').textContent==='40.00%','probability display changed');
$('useQuote').click();check($('buy').value==='0.55','fresh quote did not populate calculator');
state.outcomes[0].ask='0.77';state.outcomes[0].updated_at=new Date(Date.now()-10000).toISOString();$('useQuote').click();check($('buy').value==='0.55','stale quote populated calculator');
$('useFee').click();check($('fee').value==='0.06','market fee calculator changed');
$('tradingTab').click();await waitFor(()=>tradingState);
check($('analysisPanel').hidden&&!$('tradingPanel').hidden,'trading tab failed');
check(posts.length===0,'view selection submitted an order');
await waitFor(()=>$('historyRows').children.length===2);
check(!$('historyExport').hidden&&new URL($('historyExport').href).searchParams.get('before')==='4','CSV page not anchored');
check($('historyRows').textContent.includes('<img')&&!$('historyRows').querySelector('img'),'history injected HTML');
check($('historyRows').textContent.includes('60001.000000000001'),'history lost decimal precision');
$('historySince').value='2026-10-01T00:00';$('historyUntil').value='2026-11-01T00:00';
await refreshHistory();
const expectedSince=new Date('2026-10-01T00:00').toISOString(),expectedUntil=new Date('2026-11-01T00:00').toISOString();
let exportQuery=new URL($('historyExport').href).searchParams;
check(exportQuery.get('since')===expectedSince&&exportQuery.get('until')===expectedUntil,'CSV did not retain the local time range');
check(historyQueries.at(-1).get('since')===expectedSince&&!historyQueries.at(-1).has('before'),'range query did not start at newest');
$('historyOlder').click();await waitFor(()=>$('historyRows').children.length===1);
check(historyQueries.at(-1).get('since')===expectedSince&&historyQueries.at(-1).get('until')===expectedUntil,'pagination lost time bounds');
const requestCount=historyQueries.length;
$('historyUntil').value='2026-09-01T00:00';await refreshHistory();
check(historyQueries.length===requestCount&&$('historyExport').hidden&&$('historyStatus').textContent.includes('晚于'),'invalid range was requested');
$('historyUntil').value='2026-11-01T00:00';
delayHistory=selectedInstrument;const oldRange=refreshHistory();await waitFor(()=>delayedHistory);
$('historySince').value='2026-10-02T00:00';$('historySince').dispatchEvent(new Event('input'));
delayedHistory();await oldRange;
check($('historyRows').children.length===0&&$('historyExport').hidden,'old response survived a time edit');
$('historyClearRange').click();await waitFor(()=>$('historyRows').children.length===2);
check(!historyQueries.at(-1).has('since')&&!historyQueries.at(-1).has('until')&&!historyQueries.at(-1).has('before'),'clear range kept filters or cursor');
check($('historyTimezone').textContent.includes(Intl.DateTimeFormat().resolvedOptions().timeZone),'browser timezone is missing');
$('historyOlder').click();await waitFor(()=>$('historyRows').children.length===1);
check($('historyOlder').disabled,'history did not stop at oldest page');
$('historyFilter').value='funding';$('historyFilter').dispatchEvent(new Event('change'));
await waitFor(()=>$('historyRows').textContent.includes('-0.0001'));
$('historyFilter').value='fill';$('historyFilter').dispatchEvent(new Event('change'));
await waitFor(()=>$('historyRows').children.length===2);
check(activityQueries.length===0,'activity was fetched without an explicit click');
$('historySince').value='2026-10-01T00:00';$('historyUntil').value='2026-11-01T00:00';
$('historyFilter').value='funding';
$('activityRefresh').click();await waitFor(()=>$('activityRows').children.length===1);
check(activityQueries.at(-1).get('since')===expectedSince&&activityQueries.at(-1).get('until')===expectedUntil&&!activityQueries.at(-1).has('event_type')&&!activityQueries.at(-1).has('before'),'activity query inherited page or event filters');
check($('activityStatus').textContent.includes('120')&&$('activityRows').textContent.includes('60.001000000000000001')&&$('activityRows').textContent.includes('0.000000000000000001'),'activity was truncated or rounded');
const savedActivity=$('activityRows').textContent;
await refreshHistory();check($('activityRows').textContent===savedActivity,'history refresh cleared matching activity');
activityError=true;await refreshActivity();
check($('activityTable').hidden&&$('activityRows').children.length===0&&$('activityStatus').textContent.includes('exceeds'),'failed activity displayed old totals');activityError=false;
activityEmpty=true;await refreshActivity();check($('activityTable').hidden&&$('activityStatus').textContent.includes('暂无'),'empty activity was not distinguished from errors');activityEmpty=false;
activityMismatch=true;await refreshActivity();check($('activityTable').hidden&&$('activityStatus').textContent.includes('不匹配'),'wrong account activity was accepted');activityMismatch=false;
const activityCount=activityQueries.length;
$('historyUntil').value='2026-09-01T00:00';await refreshActivity();
check(activityQueries.length===activityCount&&$('activityStatus').textContent.includes('晚于'),'invalid activity range was requested');
$('historyUntil').value='2026-11-01T00:00';delayActivity=true;
const oldActivity=refreshActivity();await waitFor(()=>delayedActivity);
$('historySince').value='2026-10-02T00:00';$('historySince').dispatchEvent(new Event('input'));
delayedActivity();await oldActivity;
check($('activityTable').hidden&&$('activityStatus').textContent.includes('重新统计'),'old activity survived date edit');
const activityInstrument=selectedInstrument;delayActivity=true;delayedActivity=null;
const switchedActivity=refreshActivity();await waitFor(()=>delayedActivity);
selectInstrument(instruments[1]);await waitFor(()=>tradingState?.instrument.instrument_id===instruments[1]);
delayedActivity();await switchedActivity;
check($('activityTable').hidden,'old activity crossed instruments');
selectInstrument(activityInstrument);await waitFor(()=>tradingState?.instrument.instrument_id===activityInstrument);
await refreshActivity();$('analysisTab').click();check($('activityTable').hidden,'activity survived panel switch');
$('tradingTab').click();await waitFor(()=>tradingState);
$('historyClearRange').click();$('historyFilter').value='fill';await refreshHistory();
check(posts.length===0,'activity reporting submitted an order');
$('orderLimitPrice').value='60001.000000000001';
await previewPaperOrder('buy');
check(previews[0].limit_price==='60001.000000000001','preview rounded the price protection');
$('orderLimitPrice').dispatchEvent(new Event('input'));
check($('previewResult').textContent.includes('重新预览'),'price edit kept an old preview');
await previewPaperOrder('buy');
check(previews.length===2&&posts.length===0&&$('previewResult').textContent.includes('未记账'),'preview submitted an order or failed to render');
check(!pendingOrder&&!storage.get(pendingKey()),'preview created a pending order');
delayPreview=true;const oldPreview=previewPaperOrder('buy');await waitFor(()=>delayedPreview);
$('orderQuantity').value='0.003';$('orderQuantity').dispatchEvent(new Event('input'));
delayedPreview();await oldPreview;check($('previewResult').textContent.includes('重新预览')&&!previewing,'old preview survived an input edit');
previewError=true;await previewPaperOrder('sell');check($('previewResult').textContent.includes('insufficient balance'),'preview error was hidden');previewError=false;
const previewInstrument=selectedInstrument;delayPreview=true;delayedPreview=null;const switchedPreview=previewPaperOrder('buy');await waitFor(()=>delayedPreview);
selectInstrument(instruments[1]);await waitFor(()=>tradingState?.instrument.instrument_id===instruments[1]);
delayedPreview();await switchedPreview;check(!$('previewResult').textContent.includes('预计'),'late preview crossed instruments');
selectInstrument(previewInstrument);await waitFor(()=>tradingState?.instrument.instrument_id===previewInstrument);
check(posts.length===0,'preview changed recorded orders');
check($('orderLimitPrice').value==='','price protection crossed instruments');
$('orderLimitPrice').value='0';const beforeInvalid=previews.length;await previewPaperOrder('buy');
check(previews.length===beforeInvalid&&$('previewResult').textContent.includes('大于零'),'invalid limit was submitted');
$('orderLimitPrice').value='';
delayHistory=selectedInstrument;const oldHistory=refreshHistory();await waitFor(()=>delayedHistory);

tradingState.quote.observed_at=new Date(Date.now()-11000).toISOString();updateOrderButtons();
check($('paperBuy').disabled&&$('previewBuy').disabled,'stale quote allowed an order or preview');
$('instrumentSelect').value=instruments[1];$('instrumentSelect').dispatchEvent(new Event('change'));await waitFor(()=>tradingState?.instrument.instrument_id===instruments[1]);
check(!$('leverageLabel').hidden&&!$('reduceLabel').hidden,'perpetual controls missing');
await waitFor(()=>$('historyRows').textContent.includes(instruments[1]));
delayedHistory();await oldHistory;
check($('historyRows').textContent.includes(instruments[1]),'old history response overwrote selected instrument');
check(posts.length===0,'history browsing submitted an order');

$('orderQuantity').value='0.002';$('orderLeverage').value='2';$('orderReduceOnly').checked=true;
$('orderLimitPrice').value='60002.000000000001';
await refreshActivity();
postMode='network';$('paperBuy').click();$('paperBuy').click();await waitFor(()=>!submitting&&pendingOrder);
check(posts.length===1,'double click created multiple orders');
check($('activityTable').hidden&&$('activityStatus').textContent.includes('重新统计'),'order left an obsolete activity summary');
check(posts[0].side==='buy'&&posts[0].quantity==='0.002'&&posts[0].leverage==='2'&&posts[0].reduce_only===true,'order fields incorrect');
check(!$('retryOrder').hidden&&$('paperBuy').disabled,'uncertain order has unsafe controls');
$('orderLimitPrice').value='99999';
$('retryOrder').click();await waitFor(()=>!submitting&&!pendingOrder);
check(posts.length===2&&posts[0].client_order_id===posts[1].client_order_id,'retry was not idempotent');
check(posts[0].limit_price==='60002.000000000001'&&posts[1].limit_price===posts[0].limit_price,'retry changed the original price protection');
check($('orderStatus').textContent.includes('模拟成交'),'fill did not render');
$('eventMarketId').value='not-numeric';$('loadEvent').click();check(selectedInstrument===instruments[1],'invalid Gamma ID accepted');
$('eventMarketId').value='12345';$('eventOutcome').value='no';$('loadEvent').click();await waitFor(()=>tradingState?.instrument.instrument_id==='polymarket_event_12345_no');
check($('leverageLabel').hidden&&$('reduceLabel').hidden,'spot/event displayed leverage controls');
check($('instrumentInfo').textContent.includes('<img')&&!$('instrumentInfo').querySelector('img'),'external event title injected HTML');
check(posts.length===2,'event selection submitted an order');
quoteOffline=true;await refreshTrading();
check($('paperCash').textContent==='$999.99'&&$('tradingFreshness').textContent.includes('账本最后记录'),'offline account was not restored from the ledger');
check($('paperBuy').disabled&&$('paperSell').disabled&&tradingState===null,'offline account enabled fresh orders');
await refreshActivity();check($('activityRows').children.length===1&&$('activityStatus').textContent.includes('120'),'quote outage blocked activity');
accountError=true;await refreshTrading();check($('tradingFreshness').textContent.includes('未刷新'),'account failure was hidden');accountError=false;
delayAccount=selectedInstrument;const oldAccount=refreshTrading();await waitFor(()=>delayedAccount);
selectInstrument(instruments[0]);await waitFor(()=>$('paperCurrency').textContent.includes('paper:'+instruments[0]));
delayedAccount();await oldAccount;
check($('paperCurrency').textContent.includes('paper:'+instruments[0]),'late account response crossed instruments');
check(posts.length===2,'offline fallback submitted an order');
quoteOffline=false;await refreshTrading();check(!$('paperBuy').disabled&&quoteIsFresh(),'quote recovery did not restore the controls');
await refreshActivity();
check(window.errors.length===0,'JavaScript errors: '+window.errors.join('; '));
document.body.dataset.browserTest='passed';document.body.dataset.checkCount='40';
})().catch(error=>{document.body.dataset.browserTest='failed';document.body.dataset.browserError=String(error.stack||error)});
"""
    page = INDICATOR_HTML.replace(
        "<script>", "<script>" + fixtures + "</script><script>", 1
    ).replace("</body>", "<script>" + checks + "</script></body>")
    browser = os.environ["DASHBOARD_CHROME_BINARY"]
    temp_root = os.environ.get("DASHBOARD_BROWSER_TMP")
    with tempfile.TemporaryDirectory(prefix="prediction-browser-", dir=temp_root) as folder:
        path = Path(folder) / "dashboard.html"
        path.write_text(page)
        browser_path = str(path)
        profile_path = str(Path(folder) / "chrome-profile")
        if browser.endswith(".exe") and browser_path.startswith("/mnt/"):
            browser_path = subprocess.check_output(["wslpath", "-w", browser_path], text=True).strip()
            profile_path = subprocess.check_output(["wslpath", "-w", profile_path], text=True).strip()
            url = "file:///" + browser_path.replace("\\", "/")
        else:
            url = path.as_uri()
        result = subprocess.run(
            [
                browser, "--headless=new", "--disable-gpu", "--disable-background-networking",
                "--no-first-run", "--no-default-browser-check", "--no-sandbox",
                f"--user-data-dir={profile_path}", "--virtual-time-budget=4000", "--dump-dom", url,
            ],
            capture_output=True, text=True, timeout=30,
        )
        error = re.search(r'data-browser-error="([^"]*)"', result.stdout)
        detail = html.unescape(error.group(1)) if error else result.stderr[-2000:]
        assert result.returncode == 0, detail
        assert 'data-browser-test="passed"' in result.stdout, detail or result.stdout[-3000:]

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
const answer=(body,status=200)=>Promise.resolve({ok:status<400,status,json:async()=>body});
function quoteState(id){const perp=id.includes('perpetual'),event=id.startsWith('polymarket');return {mode:'paper',portfolio_id:'paper:'+id,max_quote_age_seconds:10,instrument:{instrument_id:id,kind:perp?'perpetual':event?'binary_event':'spot',symbol:id,title:event?'<img src=x onerror=alert(1)>':'' ,quote_currency:'USDT',minimum_quantity:'0.001',quantity_step:'0.001',minimum_notional:'5',maximum_leverage:perp?'3':'1'},quote:{instrument_id:id,portfolio_id:'paper:'+id,bid:'60000',ask:'60001',observed_at:new Date().toISOString(),source:'fixture'},account:null}}
function account(id){return {portfolio_id:'paper:'+id,currency:'USDT',cash:'999.99',equity:'1000',unrealized_pnl:'0.01',realized_pnl:'0',fees:'0.01',funding:'0',available_margin:'900',fill_count:1,positions:[{instrument_id:id,quantity:'0.001',average_price:'60001',leverage:'1'}]}}
function historyState(id,query){const older=query.get('before')==='2',funding=query.get('event_type')==='funding',sequences=funding?[3]:older?[1]:[3,2];return {mode:'paper',instrument_id:id,portfolio_id:'paper:'+id,next_before:older||funding?null:2,events:sequences.map(sequence=>({sequence,event_type:funding?'funding':'fill',observed_at:new Date().toISOString(),payload:{result:funding?'-0.0001':{instrument_id:id,client_order_id:id+'-<img src=x onerror=alert(1)>',side:'buy',quantity:'0.001',price:'60001.000000000001',fee:'0.01',realized_pnl:'0'}}}))}}
window.fetch=(url,options={})=>{const parsed=new URL(url,'http://localhost');if(options.method==='POST'){const payload=JSON.parse(options.body);posts.push(payload);if(postMode==='network'){postMode='success';return Promise.reject(new TypeError('network disconnected'))}return answer({mode:'paper',fill:{...payload,price:'60001',fee:'0.01'},account:account(payload.instrument_id)})}if(parsed.pathname==='/api/profiles')return answer({active:profiles[0],enabled:profiles,profiles,switching:'view_only'});if(parsed.pathname==='/api/state'){const id=parsed.searchParams.get('profile'),data={status:'waiting_for_market',profile:profiles.find(p=>p.id===id),research:{status:'disabled'}};if(delayProfile===id){delayProfile=null;return new Promise(resolve=>delayedState=()=>resolve({ok:true,json:async()=>data}))}return answer(data)}if(parsed.pathname==='/api/reports'){const id=parsed.searchParams.get('profile');return answer({profile:id,report:{profile_id:id,as_of:new Date().toISOString(),metrics:{market_count:2,prediction_count:1,prediction_coverage:0.5,realized_pnl_usd:'0.25',max_realized_drawdown_usd:'0.10'},limitations:['fixture limitations']}})}if(parsed.pathname==='/api/trading/history'){const id=parsed.searchParams.get('instrument'),data=historyState(id,parsed.searchParams);if(delayHistory===id){delayHistory=null;return new Promise(resolve=>delayedHistory=()=>resolve({ok:true,json:async()=>data}))}return answer(data)}if(parsed.pathname==='/api/instruments')return answer({instruments:instruments.map(instrument_id=>({instrument_id,label:instrument_id}))});if(parsed.pathname==='/api/trading/state')return answer(quoteState(parsed.searchParams.get('instrument')));return answer({detail:'unknown'},404)};
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
$('historyOlder').click();await waitFor(()=>$('historyRows').children.length===1);
check($('historyOlder').disabled,'history did not stop at oldest page');
$('historyFilter').value='funding';$('historyFilter').dispatchEvent(new Event('change'));
await waitFor(()=>$('historyRows').textContent.includes('-0.0001'));
$('historyFilter').value='fill';$('historyFilter').dispatchEvent(new Event('change'));
await waitFor(()=>$('historyRows').children.length===2);
delayHistory=selectedInstrument;const oldHistory=refreshHistory();await waitFor(()=>delayedHistory);

tradingState.quote.observed_at=new Date(Date.now()-11000).toISOString();updateOrderButtons();
check($('paperBuy').disabled,'stale quote allowed an order');
$('instrumentSelect').value=instruments[1];$('instrumentSelect').dispatchEvent(new Event('change'));await waitFor(()=>tradingState?.instrument.instrument_id===instruments[1]);
check(!$('leverageLabel').hidden&&!$('reduceLabel').hidden,'perpetual controls missing');
await waitFor(()=>$('historyRows').textContent.includes(instruments[1]));
delayedHistory();await oldHistory;
check($('historyRows').textContent.includes(instruments[1]),'old history response overwrote selected instrument');
check(posts.length===0,'history browsing submitted an order');

$('orderQuantity').value='0.002';$('orderLeverage').value='2';$('orderReduceOnly').checked=true;
postMode='network';$('paperBuy').click();$('paperBuy').click();await waitFor(()=>!submitting&&pendingOrder);
check(posts.length===1,'double click created multiple orders');
check(posts[0].side==='buy'&&posts[0].quantity==='0.002'&&posts[0].leverage==='2'&&posts[0].reduce_only===true,'order fields incorrect');
check(!$('retryOrder').hidden&&$('paperBuy').disabled,'uncertain order has unsafe controls');
$('retryOrder').click();await waitFor(()=>!submitting&&!pendingOrder);
check(posts.length===2&&posts[0].client_order_id===posts[1].client_order_id,'retry was not idempotent');
check($('orderStatus').textContent.includes('模拟成交'),'fill did not render');
$('eventMarketId').value='not-numeric';$('loadEvent').click();check(selectedInstrument===instruments[1],'invalid Gamma ID accepted');
$('eventMarketId').value='12345';$('eventOutcome').value='no';$('loadEvent').click();await waitFor(()=>tradingState?.instrument.instrument_id==='polymarket_event_12345_no');
check($('leverageLabel').hidden&&$('reduceLabel').hidden,'spot/event displayed leverage controls');
check($('instrumentInfo').textContent.includes('<img')&&!$('instrumentInfo').querySelector('img'),'external event title injected HTML');
check(posts.length===2,'event selection submitted an order');
check(window.errors.length===0,'JavaScript errors: '+window.errors.join('; '));
document.body.dataset.browserTest='passed';document.body.dataset.checkCount='28';
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

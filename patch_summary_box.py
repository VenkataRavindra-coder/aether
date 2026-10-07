"""Adds the "Today in plain English" box to the dashboard. Run once from ~ on the VM:
    /home/ratelark/ratelark/.venv/bin/python3 patch_summary_box.py
Display-only: extends the read-only /api/safety route with the US proposal status and last
rebalance month, and adds one script to the page. No trading logic changes.
Changes nothing unless every anchor is found exactly once. Keeps .bak-summary copies."""
import pathlib
import shutil
import sys

HELPER = '''

def _us_proposal_info():
    """(proposal {id,status,date} or None, last_rebalance_month) for the active US mode."""
    import json
    from pathlib import Path
    try:
        st = json.loads((Path.home() / "aether_state.json").read_text())
    except Exception:
        return None, None
    u = st.get("us_live" if _flag("US_LIVE") else "us_paper") or {}
    p = u.get("live_proposal") or None
    prop = {k: p.get(k) for k in ("id", "status", "date")} if p else None
    return prop, u.get("last_rebalance_month")
'''

RET_OLD = '"live_spent_today_usd": spent, "last_backup_utc": last}'
RET_NEW = ('"live_spent_today_usd": spent, "last_backup_utc": last,\n'
           '            **dict(zip(("proposal", "last_rebalance_month"), _us_proposal_info()))}')

SCRIPT = r"""<script>
(function(){
function etParts(d){const f=new Intl.DateTimeFormat('en-US',{timeZone:'America/New_York',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',weekday:'short',hour12:false});const o={};f.formatToParts(d).forEach(p=>o[p.type]=p.value);return o;}
function etDate(d){const o=etParts(d);return o.year+'-'+o.month+'-'+o.day;}
function nextSession(now){const o=etParts(now);const h=+o.hour%24,m=+o.minute;let d=new Date(Date.UTC(+o.year,+o.month-1,+o.day,12));const wk=x=>x.getUTCDay()===0||x.getUTCDay()===6;if(h>15||(h===15&&m>=30)||wk(d)){do{d.setUTCDate(d.getUTCDate()+1)}while(wk(d))}return d.toLocaleDateString('en-GB',{weekday:'short',day:'numeric',month:'short',timeZone:'UTC'});}
function usd(x){return (x<0?'-$':'$')+Math.abs(x).toLocaleString('en-US',{maximumFractionDigits:0});}
function buildSummary(ov,sf,kill,now){
 const live=sf.us_live===true,ps=(ov&&ov.positions)||[],s=(ov&&ov.summary)||{};
 const us=ps.filter(p=>p.engine==='US ETFs'),cr=ps.filter(p=>p.engine!=='US ETFs');
 const usVal=us.reduce((a,p)=>a+(p.value||0),0),usPl=us.reduce((a,p)=>a+(p.upl||0),0);
 const today=etDate(now),month=today.slice(0,7),next=nextSession(now),pr=sf.proposal;
 let level='green',action='nothing',reb;
 if(sf.last_rebalance_month===month){const n=new Date(Date.UTC(+month.slice(0,4),+month.slice(5,7),1));reb='Rebalanced this month; next one early '+n.toLocaleDateString('en-GB',{month:'long',timeZone:'UTC'})+'.';}
 else if(pr&&pr.status==='PENDING'&&pr.date===today){level='orange';action='approve proposal '+pr.id+' on Telegram today (/approve_'+pr.id+'); it runs after 15:30 ET';reb='Monthly rebalance proposed today, waiting for your approval.';}
 else if(pr&&pr.status==='APPROVED'&&pr.date===today){reb='Rebalance approved today; it runs after 15:30 ET (~23:30 GST).';}
 else{level='orange';const stale=pr&&pr.date&&pr.date<today?' Proposal '+pr.id+' from '+pr.date+' has expired: do not approve it.':'';action='approve the new US rebalance proposal on Telegram on '+next+' after 15:30 ET (~23:30 GST)';reb='Monthly rebalance not done yet (last: '+(sf.last_rebalance_month||'never')+').'+stale;}
 if(kill===true){if(level==='green')level='orange';action=(action==='nothing'?'':action+'; ')+'crypto daily-loss stop is on (clears 04:00 GST)';}
 let bk='unknown';if(sf.last_backup_utc){const h=(now-Date.parse(sf.last_backup_utc))/3.6e6;bk=(h<1?'under 1':Math.round(h))+' h ago'+(h>26?' (LATE)':'');if(h>26){if(level==='green')level='orange';action=(action==='nothing'?'':action+'; ')+'check the nightly backup (journalctl -u aether-backup)';}}
 if(sf.halt===true){level='red';action='HALTED: all new entries blocked. Resume with: rm ~/AETHER_HALT';}
 const head={green:'\u{1F7E2}',orange:'\u{1F7E0}',red:'\u{1F534}'}[level]+' '+(live?'LIVE money':'Practice mode')+' \u00b7 '+(level==='green'?'All clear':level==='red'?'Stopped':'Needs your action')+(live?'':'. No real money at risk.');
 const usLine='US bot: '+(us.length?'holds '+us.map(p=>p.symbol).join(', ')+' ('+usd(usVal)+'), '+(usPl>=0?'up ':'down ')+usd(Math.abs(usPl))+'. ':'no holdings. ')+reb;
 const crLine='Crypto bot: '+(cr.length?'holds '+cr.map(p=>p.symbol).join(', ')+'.':'no position, watching BTC, ETH, SOL for a signal.');
 let plLine='';if(typeof s.total_pl==='number'){plLine='Since start '+usd(s.total_pl)+' = '+usd(s.realized||0)+' closed trades'+(cr.length===0&&(s.realized||0)<0?' (old crypto trades)':'')+' + '+usd(s.other||0)+' fees/other + '+usd(s.unrealized||0)+' open positions.';}
 const foot='Last backup: '+bk+' \u00b7 Live buys today: '+(sf.live_spent_today_usd==null?'unreadable':usd(sf.live_spent_today_usd))+' of '+usd(sf.live_budget_usd||100)+' \u00b7 Needs your action: '+action+'.';
 return {level:level,lines:[head,usLine,crLine,plLine,foot].filter(Boolean),proposal:pr,action:action};
}
window.buildSummary=buildSummary;
function mount(){let el=document.getElementById('plainSummary');if(el)return el;el=document.createElement('section');el.id='plainSummary';el.className='card';el.style.cssText='margin:0 0 14px 0;padding:14px 18px;line-height:1.7;font-size:14px;border-left:4px solid #2ea043';
 const t=[...document.querySelectorAll('div,span,h2,h3,p,small')].find(e=>e.children.length===0&&e.textContent.trim()==='Everything you own');
 if(t){const chain=[];for(let e=t;e&&e!==document.body;e=e.parentElement)chain.push(e);const maxW=Math.max(...chain.map(e=>e.offsetWidth||0));const row=chain.find(e=>(e.offsetWidth||0)>=0.9*maxW);if(row&&row.parentElement){row.parentElement.insertBefore(el,row);return el;}}
 document.body.prepend(el);return el;}
function tidyButtons(){[...document.querySelectorAll('button,a')].forEach(b=>{const x=b.textContent.trim();if(x==='Alpaca settings'){b.textContent='\u26A0 Alpaca keys';b.title='Changes API keys. Requires your password.';b.style.border='1px solid #d29922';b.style.color='#d29922';}if(x==='Pause animation'){b.textContent='Pause background motion';}});}
async function refreshSummary(){try{const [a,b]=await Promise.all([fetch('/api/overview',{credentials:'same-origin'}),fetch('/api/safety',{credentials:'same-origin'})]);if(!a.ok||!b.ok)return;const ov=await a.json(),sf=await b.json();
 const kill=(window.state&&window.state.strategy)?window.state.strategy.kill_switch:undefined;const r=buildSummary(ov,sf,kill,Date.now());const el=mount();
 el.style.borderLeftColor={green:'#2ea043',orange:'#d29922',red:'#f85149'}[r.level];
 el.innerHTML='<div style="font-weight:600;font-size:15px;margin-bottom:4px">'+r.lines[0]+'</div>'+r.lines.slice(1).map(l=>'<div>'+l+'</div>').join('');
 const ps=document.getElementById('proposalState'),pt=document.getElementById('proposalText');
 if(ps&&pt){const p=r.proposal;ps.textContent=p?p.status+(p.date?' \u00b7 '+p.date:''):'None';pt.textContent='Needs your action: '+r.action+'.';}
 tidyButtons();}catch(e){}}
if(typeof document!=='undefined'&&document.addEventListener){refreshSummary();setInterval(refreshSummary,30000);}
})();
</script>
"""


def need(text, old, name):
    n = text.count(old)
    if n != 1:
        sys.exit(f"ABORT: anchor in {name} found {n} times, nothing changed:\n  {old[:80]}")


py, html = pathlib.Path("dashboard_v2.py"), pathlib.Path("dashboard_v2.html")
p_s, h_s = py.read_text(), html.read_text()
if "_us_proposal_info" in p_s or "plainSummary" in h_s:
    sys.exit("already patched, nothing changed")
need(p_s, RET_OLD, "dashboard_v2.py (run patch_dashboard_safety.py first)")
need(h_s, "</body>", "dashboard_v2.html")
for f in (py, html):
    shutil.copyfile(f, f.with_name(f.name + ".bak-summary"))
py.write_text(p_s.replace(RET_OLD, RET_NEW, 1).rstrip("\n") + "\n" + HELPER)
html.write_text(h_s.replace("</body>", SCRIPT + "</body>", 1))
print("patched: summary box, real approval status, safer button labels")

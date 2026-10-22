"""Exercise DOM updates in Node with a minimal DOM stub; this is not a visual test."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.unit.test_progress_report import report_for


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is needed for the JS self-check")
def test_page_charts_updates_and_preserves_details_and_scroll():
    page = Path(__file__).parents[2] / "src/codex_watchtower/report/page.html"
    script = page.read_text().split("<script>")[1].split("</script>")[0]
    snapshot = {
        "version": 1,
        "session_id": "fixture",
        "report": report_for("e").model_dump(),
        "assessed_at": "2026-10-22T08:00:00Z",
        "last_event_at": "2026-10-22T08:00:00Z",
        "next_check_at": None,
        "last_turn_state": "between_turns",
        "process_state": "unverified",
        "report_is_current": True,
        "reported_goal": None,
        "report_through_at": "2026-10-22T08:00:00Z",
        "coverage": {
            "analyzed_sequence": 0,
            "latest_sequence": 0,
            "malformed_count": 0,
            "unknown_count": 0,
            "truncated_count": 0,
        },
        "evidence": [
            {
                "id": "e",
                "timestamp": "2026-10-22T08:00:00Z",
                "kind": "message",
                "summary": "<script>untrusted</script>",
            }
        ],
        "history": [
            {
                "version": 1,
                "at": "2026-10-22T08:00:00Z",
                "progress": {"low": 40, "high": 60},
                "scope_changed": False,
            }
        ],
        "error": None,
        "export_error": None,
        "analyzing": False,
    }
    harness = r"""
const assert=require('node:assert/strict');
class Element {
 constructor(tag){this.tag=tag;this.children=[];this.dataset={};this.open=false;this.handlers={};
   this.className='';this.classList={add:(v)=>this.className+=' '+v};}
 append(...nodes){this.children.push(...nodes);}
 setAttribute(k,v){this[k]=v;}
 addEventListener(name,callback){this.handlers[name]=callback;}
 replaceChildren(...nodes){this.children=nodes;}
 querySelectorAll(selector){let result=[];for(const c of this.children){
   if(selector==='details'&&c.tag==='details'||selector==='details[open]'&&c.tag==='details'&&c.open
     ||selector==='svg'&&c.tag==='svg')result.push(c);
   result.push(...c.querySelectorAll(selector));}return result;}
}
const elements={app:new Element('main'),connection:new Element('span'),
 freshness:new Element('span'),'initial-data':new Element('script')};
elements['initial-data'].textContent=JSON.stringify(SNAPSHOT);
for(const id of ['session-picker','session-form','session-search',
                 'session-results','session-search-status'])
  elements[id]=new Element('div');
global.document={getElementById:k=>elements[k],createElement:t=>new Element(t),
 createElementNS:(_,t)=>new Element(t)};
const scrolls=[];
global.window={scrollY:140,scrollTo:(x,y)=>{assert([0,140].includes(y));scrolls.push(y);}};
global.location={protocol:'file:'};global.setInterval=()=>{};
""".replace("SNAPSHOT", json.dumps(snapshot, ensure_ascii=False))
    assertions = r"""
assert.equal(app.querySelectorAll('svg').length,3);
function visibleText(n){return (n.textContent||'')+(n.tag==='details'&&!n.open
  ? n.children.filter(c=>c.tag==='summary'):n.children).map(visibleText).join(' ');}
assert(!visibleText(app).includes(initial.report.tasks[0].comment));
assert(!visibleText(app).includes(initial.report.progress_reason));
let detail=app.querySelectorAll('details')[0];detail.open=true;
const update=JSON.parse(JSON.stringify(initial));update.version=2;update.report.progress=null;
update.report.current_action=null;update.report.tasks[0].status='done';update.error='model timeout';
update.history.push({version:2,at:'2026-10-22T08:05:00Z',progress:null,scope_changed:true});
render(update);assert.equal(app.querySelectorAll('svg').length,2);
assert.equal(app.querySelectorAll('details[open]').length,1);
assert.equal(scrolls.at(-1),140);
assert(app.querySelectorAll('svg')[0].children.some(c=>c.tag==='line'));
let saved=app.children[0];render(update);assert.equal(app.children[0],saved);
render(initial);assert.equal(app.children[0],saved); // stale versions cannot replace a newer report
assert.equal(connection.textContent,undefined); // offline snapshots do not initiate SSE
const completed=JSON.parse(JSON.stringify(update));completed.version=3;
completed.reported_goal={status:'complete',at:'2026-10-22T08:05:00Z'};
completed.report_is_current=false;completed.coverage.latest_sequence=50000;
completed.report.progress={low:60,high:74};
completed.report.tasks[0].status='in_progress';render(completed);
assert(visibleText(app).includes('✓ Цель завершена'));
assert(visibleText(app).includes('Архивный срез'));
assert(visibleText(app).includes('Задачи архивного среза'));
assert(!visibleText(app).includes('▶ В работе')); // old tasks cannot masquerade as live work
assert(!visibleText(app).includes(initial.report.current_action.text));
function allText(n){return (n.textContent||'')+n.children.map(allText).join(' ');}
assert(!allText(app).includes('60–74%')); // no obsolete headline percentage, even under a cut
assert(!allText(app).includes('ETA'));
const history=app.querySelectorAll('details').find(d=>d.dataset.key==='completed-history');
assert(history && history.children[0].textContent.includes(time(completed.report_through_at)));
completed.version=4;completed.report_is_current=true;render(completed);
assert(visibleText(app).includes('Последняя оценка задач'));
assert(!visibleText(app).includes('▶ В работе'));
assert(!allText(app).includes('60–74%')); // applies even after analysis catches up
completed.version=5;completed.reported_goal.status='active';render(completed);
assert(visibleText(app).includes('60–74%')); // resumed goals regain their current estimate
(async()=>{
 const selected=JSON.parse(JSON.stringify(initial));selected.session_id='another-session';
 selected.selection_revision=1;selected.version=0;selected.report=null;
 const calls=[];
 global.fetch=async(url,options)=>{
   calls.push([url,options]);
   return {ok:true,json:async()=>options ? selected : [{key:'a'.repeat(24),
     title:'<script>untrusted</script>',workspace:'/home/user/mtg',session_id:'another-session',
     started_at:'2026-10-22T08:00:00Z'}]};
 };
 setupSessionPicker();assert.equal(elements['session-picker'].hidden,false);
 elements['session-search'].value='mtg заголовок';
 elements['session-form'].handlers.submit({preventDefault(){}});
 await new Promise(setImmediate);
 assert.equal(calls[0][0],'/sessions?q='+encodeURIComponent('mtg заголовок'));
 const button=elements['session-results'].children[0];
 assert.equal(button.children[0].textContent,'<script>untrusted</script>');
 await button.handlers.click();
 assert.equal(JSON.parse(calls[1][1].body).key,'a'.repeat(24));
 assert.equal(state.session_id,'another-session');assert.equal(lastVersion,0);
 assert.equal(elements['session-results'].hidden,true);assert.equal(scrolls.at(-1),0);
 const currentRoot=app.children[0];
 const late=JSON.parse(JSON.stringify(initial));late.version=999;late.selection_revision=0;
 render(late);assert.equal(app.children[0],currentRoot); // old SSE cannot undo selection
 assert.equal(state.session_id,'another-session');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    subprocess.run(
        ["node", "-"],
        input=harness + script + assertions,
        text=True,
        capture_output=True,
        check=True,
        timeout=10,
    )

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
 constructor(tag){this.tag=tag;this.children=[];this.dataset={};this.open=false;
   this.className='';this.classList={add:(v)=>this.className+=' '+v};}
 append(...nodes){this.children.push(...nodes);}
 setAttribute(k,v){this[k]=v;}
 replaceChildren(...nodes){this.children=nodes;}
 querySelectorAll(selector){let result=[];for(const c of this.children){
   if(selector==='details'&&c.tag==='details'||selector==='details[open]'&&c.tag==='details'&&c.open
     ||selector==='svg'&&c.tag==='svg')result.push(c);
   result.push(...c.querySelectorAll(selector));}return result;}
}
const elements={app:new Element('main'),connection:new Element('span'),
 freshness:new Element('span'),'initial-data':new Element('script')};
elements['initial-data'].textContent=JSON.stringify(SNAPSHOT);
global.document={getElementById:k=>elements[k],createElement:t=>new Element(t),
 createElementNS:(_,t)=>new Element(t)};
global.window={scrollY:140,scrollTo:(x,y)=>assert.equal(y,140)};
global.location={protocol:'file:'};global.setInterval=()=>{};
""".replace("SNAPSHOT", json.dumps(snapshot, ensure_ascii=False))
    assertions = r"""
assert.equal(app.querySelectorAll('svg').length,3);
let detail=app.querySelectorAll('details')[0];detail.open=true;
const update=JSON.parse(JSON.stringify(initial));update.version=2;update.report.progress=null;
update.report.current_action=null;update.report.tasks[0].status='done';update.error='model timeout';
update.history.push({version:2,at:'2026-10-22T08:05:00Z',progress:null,scope_changed:true});
render(update);assert.equal(app.querySelectorAll('svg').length,2);
assert.equal(app.querySelectorAll('details[open]').length,1);
assert(app.querySelectorAll('svg')[0].children.some(c=>c.tag==='line'));
let saved=app.children[0];render(update);assert.equal(app.children[0],saved);
render(initial);assert.equal(app.children[0],saved); // stale versions cannot replace a newer report
assert.equal(connection.textContent,undefined); // offline snapshots do not initiate SSE
"""
    subprocess.run(
        ["node", "-"],
        input=harness + script + assertions,
        text=True,
        capture_output=True,
        check=True,
        timeout=10,
    )

import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
const {gateReasons}=createRequire(import.meta.url)('../dist-electron/gate.cjs');
const ok=()=>({paused:false,snapshot:'snapshot-1',baseSnapshot:'base-1',baseIntact:true,check:{id:1,hash:'snapshot-1',passed:true,commandsMatch:true},review:{id:2,hash:'snapshot-1',baseHash:'base-1',authorModel:'writer',reviewerModel:'reviewer',approved:true},approval:{snapshot:'snapshot-1',baseHash:'base-1',checkId:1,reviewId:2}});
test('gate fails closed on missing or stale evidence',()=>{
 assert.deepEqual(gateReasons(ok()),[]);
 for(const change of [e=>e.paused=true,e=>e.baseIntact=false,e=>e.baseSnapshot='changed',e=>e.review.baseHash='old-base',e=>e.approval.baseHash='old-base',e=>e.check=undefined,e=>e.check.passed=false,e=>e.check.commandsMatch=false,e=>e.snapshot='changed',e=>e.review=undefined,e=>e.review.approved=false,e=>e.review.reviewerModel='writer',e=>e.review.authorModel=' ',e=>e.review.reviewerModel=' WRITER ',e=>e.approval=undefined,e=>e.approval.checkId=3,e=>e.approval.reviewId=3]){const e=ok();change(e);assert.notEqual(gateReasons(e).length,0)}
});

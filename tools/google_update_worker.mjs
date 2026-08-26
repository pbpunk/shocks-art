import fs from 'node:fs';
import path from 'node:path';
import { spawn } from 'node:child_process';
import {
  ROOT, POLL_SECONDS, STATUS_PATH, TERMINAL_STATES, ensureSchema, localRevision, nowIso,
  preflightDeploy, publishReceipt, publishState, readReceipt, readUpdateRows, updateUpdateRow,
  validateRequestId, normalizeRevision, atomicWriteJson,
} from './google_update_common.mjs';

const HELPER_PATH = path.join(ROOT,'tools','google_update_helper.mjs');
let stopping = false;

function statusPayload(status,lastRequestId='',lastError='') {
  let revision='unknown'; try{revision=localRevision()}catch{}
  return {process:'google_update_worker',pid:process.pid,status,heartbeatTimestamp:nowIso(),revision,lastRequestId,lastError};
}
function writeStatus(status,lastRequestId='',lastError='') { atomicWriteJson(STATUS_PATH,statusPayload(status,lastRequestId,lastError)); }
function normalized(row){const v=[...(row||[])];while(v.length<10)v.push('');return v.slice(0,10).map(x=>String(x??''));}
function launchHelper(requestId,expectedRevision,createdAt,requesterId){
  const child=spawn(process.execPath,[HELPER_PATH,'--request-id',requestId,'--expected-revision',expectedRevision,'--created-at',createdAt,'--requester-id',requesterId],{cwd:ROOT,detached:true,stdio:'ignore',windowsHide:true});
  child.unref(); return child.pid;
}
async function cycle(){
  await ensureSchema();
  for(const {rowNumber,row:raw} of await readUpdateRows()){
    const row=normalized(raw); if(!row[0]) continue;
    let requestId,expected;
    try{requestId=validateRequestId(row[0]);expected=normalizeRevision(row[2]);}
    catch(error){row[4]='rejected';row[6]=nowIso();row[8]='invalid_request';row[9]=error.message;await updateUpdateRow(rowNumber,row);continue;}
    const state=row[4].trim().toLowerCase();
    if(TERMINAL_STATES.has(state)) continue;
    const receipt=readReceipt(requestId);
    if(receipt && TERMINAL_STATES.has(String(receipt.state||''))){await publishReceipt(requestId,receipt);continue;}
    if(state==='launched'||state==='running') continue;
    const preflight=preflightDeploy(expected);
    if(preflight.decision==='already_running'){
      const done={requestId,createdAt:row[1],expectedRevision:expected,requesterId:row[3],state:'completed',launchedAt:'',finishedAt:nowIso(),runningRevision:preflight.currentRevision,outcome:'already_running',error:''};
      await publishReceipt(requestId,done); writeStatus('running',requestId); await publishState('running',requestId,''); return;
    }
    if(preflight.decision!=='launch'){
      const terminal=preflight.decision==='superseded'?'superseded':'rejected';
      const done={requestId,createdAt:row[1],expectedRevision:expected,requesterId:row[3],state:terminal,launchedAt:'',finishedAt:nowIso(),runningRevision:preflight.currentRevision||'',outcome:'preflight_rejected',error:preflight.reason||''};
      await publishReceipt(requestId,done); writeStatus('running',requestId); await publishState('running',requestId,''); return;
    }
    row[4]='launched';row[5]=nowIso();row[7]=preflight.currentRevision||'';row[8]='helper_launched';row[9]='';
    await updateUpdateRow(rowNumber,row);
    launchHelper(requestId,expected,row[1],row[3]);
    writeStatus('running',requestId); await publishState('running',requestId,''); return;
  }
  writeStatus('running'); await publishState('running','','');
}

async function main(){
  fs.mkdirSync(path.dirname(STATUS_PATH),{recursive:true}); writeStatus('starting');
  const stop=()=>{stopping=true}; process.on('SIGINT',stop); process.on('SIGTERM',stop);
  while(!stopping){
    try{await cycle();}
    catch(error){const msg=`${error?.name||'Error'}: ${error?.message||error}`;writeStatus('error','',msg);try{await publishState('error','',msg)}catch{};console.error(msg);}
    if(!stopping) await new Promise(resolve=>setTimeout(resolve,POLL_SECONDS*1000));
  }
  writeStatus('stopped');
}
main().catch(error=>{writeStatus('fatal','',String(error?.message||error));console.error(error.stack||error);process.exitCode=1;});

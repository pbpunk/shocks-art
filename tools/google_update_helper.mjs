import fs from 'node:fs';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import {
  LOG_DIR, ROOT, UPDATE_CMD, localRevision, nowIso, preflightDeploy, publishReceipt,
  validateRequestId, normalizeRevision, writeReceipt,
} from './google_update_common.mjs';

function arg(name){const i=process.argv.indexOf(`--${name}`);return i>=0?process.argv[i+1]:'';}
async function bestEffort(requestId,receipt){try{await publishReceipt(requestId,receipt)}catch(error){fs.mkdirSync(LOG_DIR,{recursive:true});fs.appendFileSync(path.join(LOG_DIR,'google-update-helper.err.log'),`${nowIso()} ${error.stack||error}\n`);}}
async function main(){
  const requestId=validateRequestId(arg('request-id'));
  const expected=normalizeRevision(arg('expected-revision'));
  const createdAt=arg('created-at'); const requesterId=arg('requester-id');
  const base={requestId,createdAt,expectedRevision:expected,requesterId};
  const preflight=preflightDeploy(expected);
  if(preflight.decision==='already_running'){
    const receipt={...base,state:'completed',launchedAt:'',finishedAt:nowIso(),runningRevision:preflight.currentRevision,outcome:'already_running',error:''};writeReceipt(requestId,receipt);await bestEffort(requestId,receipt);return;
  }
  if(preflight.decision!=='launch'){
    const receipt={...base,state:preflight.decision==='superseded'?'superseded':'rejected',launchedAt:'',finishedAt:nowIso(),runningRevision:preflight.currentRevision||'',outcome:'preflight_rejected',error:preflight.reason||''};writeReceipt(requestId,receipt);await bestEffort(requestId,receipt);return;
  }
  const launchedAt=nowIso(); const runningReceipt={...base,state:'running',launchedAt,finishedAt:'',runningRevision:preflight.currentRevision||'',outcome:'updater_running',error:''};writeReceipt(requestId,runningReceipt);
  fs.mkdirSync(LOG_DIR,{recursive:true}); const logFd=fs.openSync(path.join(LOG_DIR,'google-update-helper.log'),'a'); let receipt;
  try{
    fs.writeSync(logFd,`\n${nowIso()} request=${requestId} expected=${expected}\n`);
    const result=spawnSync('cmd.exe',['/d','/c',UPDATE_CMD],{cwd:ROOT,stdio:['ignore',logFd,logFd],windowsHide:true});
    const runningRevision=localRevision(); const ok=(result.status??1)===0 && runningRevision===expected;
    receipt={...base,state:ok?'completed':'failed',launchedAt,finishedAt:nowIso(),runningRevision,outcome:ok?'updated_exact':'updater_failed',error:ok?'':((result.status??1)!==0?`canonical updater exited with code ${result.status??1}`:'running revision does not equal requested revision')};
  }catch(error){let runningRevision='';try{runningRevision=localRevision()}catch{};receipt={...base,state:'failed',launchedAt,finishedAt:nowIso(),runningRevision,outcome:'helper_exception',error:`${error?.name||'Error'}: ${error?.message||error}`};}
  finally{fs.closeSync(logFd);}
  writeReceipt(requestId,receipt);await bestEffort(requestId,receipt);
}
main().catch(error=>{console.error(error.stack||error);process.exitCode=1;});

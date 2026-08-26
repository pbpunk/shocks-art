import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

export const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
export const UPDATE_CMD = path.join(ROOT, 'Update App.cmd');
export const SPREADSHEET_ID = process.env.SHOCKS_GOOGLE_SPREADSHEET_ID || '1hD8IqH_o1RJnVyxpAuhSmR-nB6DNwvK51xZIyn8IX_I';
export const CREDENTIALS_PATH = process.env.SHOCKS_GOOGLE_CREDENTIALS || 'F:\\JARVIS-secrets\\pancake-google-service-account.json';
export const POLL_SECONDS = Math.max(5, Number(process.env.SHOCKS_GOOGLE_UPDATE_POLL_SECONDS || 15));
export const STATUS_PATH = process.env.SHOCKS_GOOGLE_UPDATE_STATUS_PATH || path.join(ROOT, 'data', 'google_update_worker_status.json');
export const RECEIPT_DIR = process.env.SHOCKS_GOOGLE_UPDATE_RECEIPT_DIR || path.join(ROOT, 'data', 'google_update_receipts');
export const LOG_DIR = path.join(ROOT, 'data', 'logs');
export const TERMINAL_STATES = new Set(['completed', 'failed', 'superseded', 'rejected']);
export const UPDATES_HEADERS = ['request_id','created_at','expected_revision','requester_id','state','launched_at','finished_at','running_revision','outcome','error'];
let tokenCache = null;

export const nowIso = () => new Date().toISOString();
export function validateRequestId(value) {
  const v = String(value || '').trim();
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$/.test(v)) throw new Error('invalid request_id');
  return v;
}
export function normalizeRevision(value) {
  const v = String(value || '').trim().toLowerCase();
  if (!/^[0-9a-f]{40}$/.test(v)) throw new Error('expected_revision must be a full 40-character Git SHA');
  return v;
}
export function atomicWriteJson(filePath, payload) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  const tmp = `${filePath}.tmp`;
  fs.writeFileSync(tmp, `${JSON.stringify(payload, null, 2)}\n`, 'utf8');
  fs.renameSync(tmp, filePath);
}
export function receiptPath(requestId) { return path.join(RECEIPT_DIR, `${validateRequestId(requestId)}.json`); }
export function readReceipt(requestId) { try { return JSON.parse(fs.readFileSync(receiptPath(requestId), 'utf8')); } catch { return {}; } }
export function writeReceipt(requestId, payload) { atomicWriteJson(receiptPath(requestId), payload); }
function runGit(args, timeout = 120000) {
  const r = spawnSync('git', args, { cwd: ROOT, encoding: 'utf8', timeout, windowsHide: true });
  return { status: r.status ?? 1, stdout: String(r.stdout || '').trim(), stderr: String(r.stderr || '').trim() };
}
function gitOutput(args, timeout = 120000) {
  const r = runGit(args, timeout); if (r.status !== 0) throw new Error(r.stderr || r.stdout || `git ${args.join(' ')} failed`); return r.stdout;
}
export const localRevision = () => gitOutput(['rev-parse','HEAD'],10000).toLowerCase();
export function preflightDeploy(expectedRevision) {
  const expected = normalizeRevision(expectedRevision);
  const dirty = gitOutput(['status','--porcelain','--untracked-files=no'],10000);
  if (dirty) return { decision:'rejected', reason:'tracked local changes are present', expectedRevision:expected };
  const fetch = runGit(['fetch','origin','main','--prune']);
  if (fetch.status !== 0) throw new Error(fetch.stderr || fetch.stdout || 'git fetch failed');
  const currentRevision = localRevision();
  const originMainRevision = gitOutput(['rev-parse','origin/main'],10000).toLowerCase();
  if (originMainRevision !== expected) return { decision:'superseded', reason:'origin/main no longer equals requested revision', expectedRevision:expected, currentRevision, originMainRevision };
  if (currentRevision === expected) return { decision:'already_running', expectedRevision:expected, currentRevision, originMainRevision };
  const anc = runGit(['merge-base','--is-ancestor',currentRevision,expected],10000);
  if (anc.status !== 0) return { decision:'rejected', reason:'local HEAD is not an ancestor of requested origin/main', expectedRevision:expected, currentRevision, originMainRevision };
  return { decision:'launch', expectedRevision:expected, currentRevision, originMainRevision };
}
function base64url(value) { return Buffer.from(value).toString('base64url'); }
function loadServiceAccount() {
  if (!fs.existsSync(CREDENTIALS_PATH)) throw new Error(`Google service-account credentials not found: ${CREDENTIALS_PATH}`);
  const p = JSON.parse(fs.readFileSync(CREDENTIALS_PATH,'utf8'));
  if (!p.client_email || !p.private_key) throw new Error('Google service-account JSON is missing client_email/private_key');
  return p;
}
async function accessToken() {
  const now = Math.floor(Date.now()/1000);
  if (tokenCache && tokenCache.expiresAt > now + 60) return tokenCache.token;
  const sa = loadServiceAccount();
  const header = base64url(JSON.stringify({alg:'RS256',typ:'JWT'}));
  const claims = base64url(JSON.stringify({iss:sa.client_email,scope:'https://www.googleapis.com/auth/spreadsheets',aud:sa.token_uri || 'https://oauth2.googleapis.com/token',iat:now,exp:now+3600}));
  const unsigned = `${header}.${claims}`;
  const signature = crypto.sign('RSA-SHA256', Buffer.from(unsigned), sa.private_key).toString('base64url');
  const response = await fetch(sa.token_uri || 'https://oauth2.googleapis.com/token', { method:'POST', headers:{'content-type':'application/x-www-form-urlencoded'}, body:new URLSearchParams({grant_type:'urn:ietf:params:oauth:grant-type:jwt-bearer',assertion:`${unsigned}.${signature}`}) });
  const body = await response.json().catch(()=>({}));
  if (!response.ok || !body.access_token) throw new Error(`Google OAuth token request failed (${response.status})`);
  tokenCache = { token:body.access_token, expiresAt:now+Number(body.expires_in || 3600) }; return tokenCache.token;
}
async function googleJson(url,{method='GET',body}={}) {
  const token = await accessToken();
  const response = await fetch(url,{method,headers:{authorization:`Bearer ${token}`,...(body===undefined?{}:{'content-type':'application/json'})},body:body===undefined?undefined:JSON.stringify(body)});
  const text = await response.text(); let payload={}; if(text){try{payload=JSON.parse(text)}catch{payload={raw:text}}}
  if(!response.ok) throw new Error(payload?.error?.message || payload?.raw || `Google Sheets HTTP ${response.status}`);
  return payload;
}
const sheetsBase = () => `https://sheets.googleapis.com/v4/spreadsheets/${encodeURIComponent(SPREADSHEET_ID)}`;
export async function ensureSchema() {
  const meta = await googleJson(`${sheetsBase()}?fields=sheets.properties.title`);
  const titles = new Set((meta.sheets||[]).map(s=>String(s?.properties?.title||'')));
  if(!titles.has('Updates')) await googleJson(`${sheetsBase()}:batchUpdate`,{method:'POST',body:{requests:[{addSheet:{properties:{title:'Updates'}}}]}});
  await updateValues('Updates!A1:J1',[UPDATES_HEADERS]);
}
export async function getValues(range) { return (await googleJson(`${sheetsBase()}/values/${encodeURIComponent(range)}`)).values || []; }
export async function updateValues(range,values) { return googleJson(`${sheetsBase()}/values/${encodeURIComponent(range)}?valueInputOption=RAW`,{method:'PUT',body:{values}}); }
export async function readUpdateRows() { return (await getValues('Updates!A2:J1000')).map((row,index)=>({rowNumber:index+2,row})); }
export async function updateUpdateRow(rowNumber,values) { await updateValues(`Updates!A${rowNumber}:J${rowNumber}`,[values]); }
export async function publishState(status,lastRequestId='',lastError='') { await updateValues('State!G2:J2',[[nowIso(),status,lastRequestId,String(lastError||'').slice(0,1500)]]); }
export async function publishReceipt(requestId, receipt) {
  for (const {rowNumber,row} of await readUpdateRows()) {
    if(String(row?.[0]||'').trim()!==requestId) continue;
    await updateUpdateRow(rowNumber,[requestId,String(receipt.createdAt||row?.[1]||''),String(receipt.expectedRevision||row?.[2]||''),String(receipt.requesterId||row?.[3]||''),String(receipt.state||''),String(receipt.launchedAt||''),String(receipt.finishedAt||''),String(receipt.runningRevision||''),String(receipt.outcome||''),String(receipt.error||'').slice(0,1500)]);
    return true;
  }
  return false;
}

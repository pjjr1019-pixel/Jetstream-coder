import fs from 'node:fs/promises';import path from 'node:path';import {createHash} from 'node:crypto';
import {inspectCheckpoint} from './checkpoint';import {appendJournal,readJournal} from './checkpoint-journal';import {openExclusiveWrite} from './exclusive-write';
const hash=(b:Buffer)=>createHash('sha256').update(b).digest('hex');
/** Disposable-project prototype only; no application IPC is exposed. */
export async function runCheckpointTransaction(directory:string,options:{authorize:()=>Promise<boolean>;checks:()=>Promise<boolean>;afterWrite?:()=>Promise<void>}){
 const info=await inspectCheckpoint(directory);if(info.status!=='before'||(await readJournal(directory)).at(-1)!=='prepared')throw new Error('Checkpoint is not ready');const bytes=await fs.readFile(path.join(directory,'after.bin'));if(hash(bytes)!==info.record.afterHash)throw new Error('Checkpoint is corrupt');
 const lock=await openExclusiveWrite(path.join(info.record.project,info.record.relativePath),info.record.beforeHash);try{
  if(!await options.authorize())throw new Error('Approval or state changed');await appendJournal(directory,'applying');if(!await options.authorize())throw new Error('Approval or state changed; checkpoint requires inspection');
  await lock.commit(bytes);if(options.afterWrite)await options.afterWrite();await appendJournal(directory,'applied');await appendJournal(directory,'checking');let passed=false;try{passed=await options.checks()}catch{/* unknown results fail closed */}await appendJournal(directory,passed?'checks_passed':'checks_failed');return {state:passed?'checks_passed':'checks_failed'};
 }finally{await lock.cancel()}
}

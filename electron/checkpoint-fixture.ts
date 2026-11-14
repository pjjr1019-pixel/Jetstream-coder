import fs from 'node:fs/promises';import path from 'node:path';import {createHash} from 'node:crypto';
const hash=(data:Buffer)=>createHash('sha256').update(data).digest('hex');
import {inspectCheckpoint} from './checkpoint';import {exclusiveCompareWrite} from './exclusive-write';import {appendJournal,readJournal} from './checkpoint-journal';
/** Experimental only. Not wired into production Apply: path races and durable recovery remain unresolved. */
export async function applyCheckpointFixture(directory:string,afterWrite?:()=>Promise<void>){
 const info=await inspectCheckpoint(directory);if(info.status!=='before')throw new Error('Checkpoint target is not at the original version');
 if((await readJournal(directory)).at(-1)!=='prepared')throw new Error('Checkpoint is not ready');await appendJournal(directory,'applying');const after=await fs.readFile(path.join(directory,'after.bin'));if(hash(after)!==info.record.afterHash)throw new Error('Checkpoint is corrupt');await exclusiveCompareWrite(path.join(info.record.project,info.record.relativePath),info.record.beforeHash,after);if(afterWrite)await afterWrite();await appendJournal(directory,'applied');
 return inspectCheckpoint(directory);
}
/** Restore only the exact written bytes; all other states require manual inspection. */
export async function rollbackCheckpointFixture(directory:string){
 const info=await inspectCheckpoint(directory);if(info.status!=='after')throw new Error('Checkpoint target is not at the applied version');
 await appendJournal(directory,'reverting');const before=await fs.readFile(path.join(directory,'before.bin'));if(hash(before)!==info.record.beforeHash)throw new Error('Checkpoint is corrupt');await exclusiveCompareWrite(path.join(info.record.project,info.record.relativePath),info.record.afterHash,before);await appendJournal(directory,'reverted');
 return inspectCheckpoint(directory);
}

/** Classification only; never automatically overwrite a possibly edited file. */
export async function inspectRecoveryFixture(directory:string){const states=await readJournal(directory);const info=await inspectCheckpoint(directory);return {state:states.at(-1),fileStatus:info.status,needsInspection:states.at(-1)==='applying'||states.at(-1)==='reverting'||info.status==='conflict'}}

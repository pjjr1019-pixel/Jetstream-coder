import fs from 'node:fs/promises';
import path from 'node:path';
import {createHash,randomUUID} from 'node:crypto';
const digest=(data:Buffer)=>createHash('sha256').update(data).digest('hex');
export type Checkpoint={version:1;project:string;relativePath:string;beforeHash:string;afterHash:string;state:'prepared'};
/** Prepare evidence and a durable backup only. This module never writes the project. */
export async function prepareCheckpoint(project:string,relativePath:string,expectedHash:string,after:Buffer,checkpointRoot:string,checkpointId:string=randomUUID()){
 const root=await fs.realpath(project),store=await fs.realpath(checkpointRoot);const storeRel=path.relative(root,store);if(storeRel===''||(!storeRel.startsWith('..'+path.sep)&&storeRel!=='..'&&!path.isAbsolute(storeRel)))throw new Error('Checkpoint must be outside project');
 if(!relativePath||path.isAbsolute(relativePath))throw new Error('Invalid relative path');const target=path.resolve(root,relativePath),rel=path.relative(root,target);if(rel==='..'||rel.startsWith('..'+path.sep)||path.isAbsolute(rel))throw new Error('Path outside project');
 let parent=root;for(const part of rel.split(path.sep)){parent=path.join(parent,part);const info=await fs.lstat(parent);if(info.isSymbolicLink())throw new Error('Symbolic links are not supported')}
 const resolved=await fs.realpath(target);if(path.relative(root,resolved)!==rel)throw new Error('Target changed or is linked');const before=await fs.readFile(target);if(digest(before)!==expectedHash)throw new Error('Project conflict');
 if(!/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(checkpointId))throw new Error('Invalid checkpoint identifier');const dir=path.join(store,checkpointId);await fs.mkdir(dir);const writeDurable=async(name:string,data:Buffer)=>{const handle=await fs.open(path.join(dir,name),'wx');try{await handle.writeFile(data);await handle.sync()}finally{await handle.close()}};
 const record:Checkpoint={version:1,project:root,relativePath:rel,beforeHash:digest(before),afterHash:digest(after),state:'prepared'};
 await writeDurable('before.bin',before);await writeDurable('after.bin',after);await writeDurable('manifest.json',Buffer.from(JSON.stringify(record)));return {directory:dir,record};
}
/** Inspection only: unknown contents must never be overwritten automatically. */
export async function inspectCheckpoint(directory:string){
 const record=JSON.parse(await fs.readFile(path.join(directory,'manifest.json'),'utf8')) as Checkpoint;if(record.version!==1||record.state!=='prepared'||!record.project||!record.relativePath)throw new Error('Invalid checkpoint');
 const before=await fs.readFile(path.join(directory,'before.bin')),after=await fs.readFile(path.join(directory,'after.bin'));if(digest(before)!==record.beforeHash||digest(after)!==record.afterHash)throw new Error('Checkpoint is corrupt');
 const root=await fs.realpath(record.project),target=path.resolve(root,record.relativePath),rel=path.relative(root,target);if(!rel||rel==='..'||rel.startsWith('..'+path.sep)||path.isAbsolute(rel))throw new Error('Invalid checkpoint target');let current=root;for(const part of rel.split(path.sep)){current=path.join(current,part);if((await fs.lstat(current)).isSymbolicLink())throw new Error('Checkpoint target is linked')}
 const actual=digest(await fs.readFile(target));return {record,status:actual===record.beforeHash?'before':actual===record.afterHash?'after':'conflict'};
}

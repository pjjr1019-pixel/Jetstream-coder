import fs from 'node:fs/promises';
import path from 'node:path';
import {createHash,randomUUID} from 'node:crypto';
import {fingerprintTree} from './evidence';
export {fingerprintTree};
import {gateReasons,type GateEvidence} from './gate';

type FileEntry={name:string;hash:string;bytes:Buffer};
type Entry={name:string;before:string|null;after:string|null;backup:string|null};
type Journal={version:1;project:string;snapshot:string;state:'prepared'|'applying'|'applied'|'checks_failed'|'reverting'|'reverted';entries:Entry[]};
const digest=(data:Buffer)=>createHash('sha256').update(data).digest('hex');
async function tree(root:string):Promise<Map<string,FileEntry>>{
 const files=new Map<string,FileEntry>();let total=0;
 async function scan(dir:string){for(const item of await fs.readdir(dir,{withFileTypes:true})){
  const full=path.join(dir,item.name);if(item.isSymbolicLink())throw new Error('Symbolic links are not supported');
  if(item.isDirectory()){await scan(full);continue}if(!item.isFile())throw new Error('Unsupported file type');
  const stat=await fs.stat(full);total+=stat.size;if(total>256_000_000||files.size>=20000)throw new Error('Snapshot is too large');
  const bytes=await fs.readFile(full);const name=path.relative(root,full).split(path.sep).join('/');files.set(name,{name,hash:digest(bytes),bytes});
 }} await scan(root);return files;
}
async function safeTarget(root:string,name:string):Promise<string>{
 const parts=name.split('/');if(!name||parts.some(p=>!p||p==='.'||p==='..'||p.includes('\\')||p.includes(':')))throw new Error('Invalid relative path');
 let current=root;
 for(let i=0;i<parts.length;i++) {current=path.join(current,parts[i]);try{const stat=await fs.lstat(current);if(stat.isSymbolicLink())throw new Error('Symbolic links are not supported');if(i<parts.length-1&&!stat.isDirectory())throw new Error('Parent is not a directory');if(i===parts.length-1&&!stat.isFile())throw new Error('Target is not a regular file');}catch(e){if((e as NodeJS.ErrnoException).code==='ENOENT'&&i===parts.length-1)break;throw e}}
 return current;
}
async function currentHash(file:string):Promise<string|null>{try{return digest(await fs.readFile(file))}catch(e){if((e as NodeJS.ErrnoException).code==='ENOENT')return null;throw e}}
async function writeJournal(dir:string,j:Journal){const tmp=path.join(dir,'manifest.tmp');const out=path.join(dir,'manifest.json');await fs.writeFile(tmp,JSON.stringify(j,null,2));await fs.rename(tmp,out)}
async function replace(file:string,bytes:Buffer|null){if(bytes===null){await fs.unlink(file);return}const tmp=path.join(path.dirname(file),`.jetstream-${randomUUID()}.tmp`);try{await fs.writeFile(tmp,bytes,{flag:'wx'});await fs.rename(tmp,file)}catch(e){await fs.unlink(tmp).catch(()=>{});throw e}}

/** Prototype: never wired to production Apply. Callers must supply trusted gate evidence. */
export async function applyFixture(options:{project:string;base:string;scratch:string;checkpointRoot:string;evidence:GateEvidence;postCheck:()=>Promise<boolean>;afterStep?:(index:number)=>Promise<void>}){
 const {project,base,scratch,checkpointRoot,evidence}=options;
 const projectReal=await fs.realpath(project),checkpointReal=await fs.realpath(checkpointRoot);
 const relative=path.relative(projectReal,checkpointReal);if(relative===''||(!relative.startsWith('..'+path.sep)&&relative!=='..'&&!path.isAbsolute(relative)))throw new Error('Checkpoint must be outside the project');
 const actual=await fingerprintTree(scratch),actualBase=await fingerprintTree(base);if(actual!==evidence.snapshot||actualBase!==evidence.baseSnapshot||gateReasons(evidence).length)throw new Error('Gate evidence is missing or stale');
 const before=await tree(base),after=await tree(scratch);const names=[...new Set([...before.keys(),...after.keys()])].sort();const seen=new Set<string>();const entries:Entry[]=[];
 for(const name of names){const key=name.toLowerCase();if(seen.has(key))throw new Error('Case-colliding paths are not supported');seen.add(key);const b=before.get(name),a=after.get(name);if(b?.hash===a?.hash)continue;
  const target=await safeTarget(projectReal,name);if(await currentHash(target)!==(b?.hash??null))throw new Error(`Project conflict: ${name}`);
  entries.push({name,before:b?.hash??null,after:a?.hash??null,backup:b?`${entries.length}.bin`:null});
 }
 if(!entries.length)throw new Error('No changes to apply');
 const dir=path.join(checkpointReal,randomUUID());await fs.mkdir(dir);const j:Journal={version:1,project:projectReal,snapshot:actual,state:'prepared',entries};
 for(const entry of entries)if(entry.backup)await fs.writeFile(path.join(dir,entry.backup),before.get(entry.name)!.bytes,{flag:'wx'});
 await writeJournal(dir,j);
 if(await fingerprintTree(scratch)!==actual||await fingerprintTree(base)!==actualBase)throw new Error(`Scratch or baseline changed; checkpoint: ${dir}`);
 j.state='applying';await writeJournal(dir,j);
 for(let i=0;i<entries.length;i++){const entry=entries[i],target=await safeTarget(projectReal,entry.name);if(await currentHash(target)!==entry.before)throw new Error(`Project conflict during apply: ${entry.name}; checkpoint: ${dir}`);await replace(target,after.get(entry.name)?.bytes??null);if(options.afterStep)await options.afterStep(i)}
 j.state='applied';await writeJournal(dir,j);
 let passed=false;try{passed=await options.postCheck()}catch{/* An interrupted or failed check must never count as success. */}
 if(!passed){j.state='checks_failed';await writeJournal(dir,j)}
 return {checkpoint:dir,state:j.state};
}

/** Restore only when every affected file still equals its before or applied version. */
export async function rollbackFixture(dir:string){
 const j=JSON.parse(await fs.readFile(path.join(dir,'manifest.json'),'utf8')) as Journal;
 if(j.version!==1||j.state==='reverted')throw new Error('Invalid or already reverted checkpoint');
 const root=await fs.realpath(j.project);
 for(const e of j.entries){const file=await safeTarget(root,e.name),hash=await currentHash(file);if(hash!==e.before&&hash!==e.after)throw new Error(`Rollback conflict: ${e.name}`);if(e.backup){const bytes=await fs.readFile(path.join(dir,e.backup));if(digest(bytes)!==e.before)throw new Error('Checkpoint is corrupt')}}
 j.state='reverting';await writeJournal(dir,j);
 for(const e of [...j.entries].reverse()){const file=await safeTarget(root,e.name),hash=await currentHash(file);if(hash===e.before)continue;if(hash!==e.after)throw new Error(`Rollback conflict: ${e.name}`);await replace(file,e.backup?await fs.readFile(path.join(dir,e.backup)):null)}
 j.state='reverted';await writeJournal(dir,j);return true;
}

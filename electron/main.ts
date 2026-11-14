import {app, BrowserWindow, dialog, ipcMain, shell} from 'electron';
import {DatabaseSync} from 'node:sqlite';
import fs from 'node:fs/promises';
import {watch, type FSWatcher} from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
import {fingerprintTree,fingerprintManifest} from './evidence';
import {gateReasons} from './gate';
import ignore from 'ignore';
import * as pty from 'node-pty';
import ts from 'typescript';

const execFileAsync=promisify(execFile);
let terminal:pty.IPty|undefined; let terminalSession=0;
let win:BrowserWindow; let project=''; let db:DatabaseSync|undefined; let closeAllowed=false; let watcher:FSWatcher|undefined; let generation=0; let indexTimer:ReturnType<typeof setTimeout>|undefined;
const ignored=new Set(['.git','node_modules','dist','dist-electron','build','.jetstream-coder']);
const hash=(s:string|Buffer)=>crypto.createHash('sha256').update(s).digest('hex');
async function checked(name:string) {
  if(!project || !name || path.isAbsolute(name)) throw new Error('Invalid project path');
  const file=path.resolve(project,name); const rel=path.relative(project,file);
  if(rel.startsWith('..'+path.sep)||rel==='..'||path.isAbsolute(rel)) throw new Error('Path is outside the project');
  const real=await fs.realpath(file); const actual=path.relative(project,real);
  if(actual.startsWith('..'+path.sep)||actual==='..'||path.isAbsolute(actual)) throw new Error('Path is outside the project');
  return real;
}
async function files() {
  if(!project) return [];
  const result:string[]=[];
  type Rules={root:string;matcher:ReturnType<typeof ignore>};
  async function readRules(dir:string,name:string):Promise<Rules|undefined> {
    try {const file=path.join(dir,name);const stat=await fs.lstat(file);if(stat.isFile()&&!stat.isSymbolicLink()&&stat.size<1_000_000)return {root:dir,matcher:ignore().add(await fs.readFile(file,'utf8'))};}catch{/* Optional ignore file. */}
    return undefined;
  }
  async function walk(dir:string,parent:Rules[]) {
    const rules=[...parent];const git=await readRules(dir,'.gitignore');if(git)rules.push(git);if(dir===project){const custom=await readRules(dir,'.jetstreamignore');if(custom)rules.push(custom);const configured=String(db?.prepare("SELECT value FROM settings WHERE key='ignore_patterns'").get()?.value||'');if(configured)rules.push({root:dir,matcher:ignore().add(configured)});}
    const entries=await fs.readdir(dir,{withFileTypes:true});
    for(const e of entries){ if(ignored.has(e.name)||e.isSymbolicLink()||e.name==='.env'||e.name.startsWith('.env.')||e.name.toLowerCase().endsWith('.pem')||e.name.toLowerCase().endsWith('.key')) continue;
      const full=path.join(dir,e.name);let excluded=false;
      for(const rule of rules){const relative=path.relative(rule.root,full).split(path.sep).join('/')+(e.isDirectory()?'/':'');const match=rule.matcher.test(relative);if(match.ignored)excluded=true;else if(match.unignored)excluded=false;}
      if(excluded)continue; if(e.isDirectory()) await walk(full,rules); else if(e.isFile()) result.push(path.relative(project,full));
      if(result.length>20000) throw new Error('Project exceeds the 20,000-file scan limit');
    }
  }
  await walk(project,[]); return result.sort((a,b)=>a.localeCompare(b));
}
async function read(name:string) {
  const p=await checked(name); const stat=await fs.stat(p); if(!stat.isFile()||stat.size>2_000_000) throw new Error('File is not a regular file or exceeds 2 MB');
  const data=await fs.readFile(p); if(data.includes(0)) throw new Error('Binary files cannot be opened'); try { new TextDecoder('utf-8',{fatal:true}).decode(data); } catch { throw new Error('Only UTF-8 text files can be opened safely'); }
  return {content:data.toString('utf8'),fingerprint:hash(data)};
}
function indexSyntax(target:DatabaseSync,name:string,content:string){
  target.prepare('DELETE FROM symbols WHERE path=?').run(name);target.prepare('DELETE FROM calls WHERE path=?').run(name);
  if(!/\.[cm]?[jt]sx?$/i.test(name))return;
  const kind=name.endsWith('x')?(name.match(/\.tsx$/i)?ts.ScriptKind.TSX:ts.ScriptKind.JSX):name.match(/\.[cm]?ts$/i)?ts.ScriptKind.TS:ts.ScriptKind.JS;
  const source=ts.createSourceFile(name,content,ts.ScriptTarget.Latest,true,kind);
  const symbol=target.prepare('INSERT INTO symbols (path,name,kind,line) VALUES (?,?,?,?)');const call=target.prepare('INSERT INTO calls (path,caller,target,line) VALUES (?,?,?,?)');
  function visit(node:ts.Node,caller:string){
    let scope=caller;
    if((ts.isFunctionDeclaration(node)||ts.isClassDeclaration(node)||ts.isInterfaceDeclaration(node)||ts.isTypeAliasDeclaration(node)||ts.isMethodDeclaration(node))&&node.name){
      const title=node.name.getText(source);const label=ts.SyntaxKind[node.kind];symbol.run(name,title,label,source.getLineAndCharacterOfPosition(node.getStart(source)).line+1);if(ts.isFunctionDeclaration(node)||ts.isMethodDeclaration(node))scope=title;
    }
    if(ts.isCallExpression(node)){call.run(name,scope,node.expression.getText(source),source.getLineAndCharacterOfPosition(node.getStart(source)).line+1);}
    ts.forEachChild(node,n=>visit(n,scope));
  }
  visit(source,'<module>');
}
async function rebuildIndex(expected=generation) {
  const target=db; if(!target || !project) return;
  const names=await files(); if(expected!==generation) return;
  const seen=new Set<string>();
  for(const name of names) {
    if(expected!==generation) return;
    try { const data=await read(name); if(expected!==generation) return;
      const prior=target.prepare('SELECT fingerprint FROM file_index WHERE path=?').get(name)?.fingerprint;if(prior!==data.fingerprint){target.prepare('INSERT INTO file_index (path,content,fingerprint) VALUES (?,?,?) ON CONFLICT(path) DO UPDATE SET content=excluded.content,fingerprint=excluded.fingerprint').run(name,data.content,data.fingerprint);} const syntaxHash=target.prepare('SELECT fingerprint FROM syntax_indexed WHERE path=?').get(name)?.fingerprint;if(syntaxHash!==data.fingerprint){indexSyntax(target,name,data.content);target.prepare('INSERT INTO syntax_indexed(path,fingerprint) VALUES (?,?) ON CONFLICT(path) DO UPDATE SET fingerprint=excluded.fingerprint').run(name,data.fingerprint)} seen.add(name);
    } catch { /* Binary, oversized, changed, or unreadable files are omitted. */ }
  }
  if(expected!==generation) return;
  const rows=target.prepare('SELECT path FROM file_index').all() as {path:string}[];
  for(const row of rows) if(!seen.has(row.path)){target.prepare('DELETE FROM file_index WHERE path=?').run(row.path);target.prepare('DELETE FROM syntax_indexed WHERE path=?').run(row.path);target.prepare('DELETE FROM symbols WHERE path=?').run(row.path);target.prepare('DELETE FROM calls WHERE path=?').run(row.path);}
}
function startWatcher() {
  watcher?.close(); if(indexTimer)clearTimeout(indexTimer);
  try { watcher=watch(project,{recursive:true},()=>{
    if(indexTimer)clearTimeout(indexTimer);
    const expected=generation;
    indexTimer=setTimeout(()=>{if(expected!==generation)return; void rebuildIndex(expected).then(()=>{if(expected===generation && !win.isDestroyed())win.webContents.send('project:changed')}).catch(()=>{if(expected===generation && !win.isDestroyed())win.webContents.send('project:changed')});},600);
  }); } catch { watcher=undefined; }
}
function database() { if(!db) throw new Error('Open a project first'); return db; }
function transaction<T>(d:DatabaseSync,work:()=>T):T {d.exec('BEGIN IMMEDIATE');try{const result=work();d.exec('COMMIT');return result}catch(error){d.exec('ROLLBACK');throw error}}
function createWindow(){
 win=new BrowserWindow({width:1450,height:900,minWidth:850,minHeight:600,backgroundColor:'#11151d',webPreferences:{preload:path.join(__dirname,'preload.cjs'),contextIsolation:true,nodeIntegration:false,sandbox:true}});
 win.webContents.setWindowOpenHandler(()=>({action:'deny'}));
 win.webContents.on('will-navigate',e=>e.preventDefault());
 win.on('close',e=>{if(!closeAllowed){e.preventDefault();win.webContents.send('close:request')}});
 void win.loadFile(path.join(__dirname,'../dist/index.html'));
}
app.whenReady().then(()=>{
 ipcMain.handle('project:open',async()=>{
  const selection=await dialog.showOpenDialog(win,{properties:['openDirectory']}); if(selection.canceled||!selection.filePaths[0]) return null;
  const selected=await fs.realpath(selection.filePaths[0]);
  ++generation; watcher?.close(); if(indexTimer)clearTimeout(indexTimer); terminalSession++; if(terminal){terminal.kill(); terminal=undefined;win.webContents.send('terminal:exit',0)}
  const dataDir=path.join(app.getPath('userData'),'projects'); await fs.mkdir(dataDir,{recursive:true});
  const next=new DatabaseSync(path.join(dataDir,hash(selected)+'.sqlite'));
  next.exec("CREATE TABLE IF NOT EXISTS jobs (id INTEGER PRIMARY KEY, request TEXT NOT NULL, status TEXT NOT NULL, created TEXT NOT NULL); CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL); CREATE TABLE IF NOT EXISTS file_index (path TEXT PRIMARY KEY, content TEXT NOT NULL, fingerprint TEXT NOT NULL); CREATE TABLE IF NOT EXISTS job_events (id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL, created TEXT NOT NULL); CREATE TABLE IF NOT EXISTS pieces (job_id INTEGER PRIMARY KEY, base_path TEXT NOT NULL, scratch_path TEXT NOT NULL, manifest TEXT NOT NULL); CREATE TABLE IF NOT EXISTS syntax_indexed (path TEXT PRIMARY KEY, fingerprint TEXT NOT NULL); CREATE TABLE IF NOT EXISTS symbols (path TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL, line INTEGER NOT NULL); CREATE TABLE IF NOT EXISTS calls (path TEXT NOT NULL, caller TEXT NOT NULL, target TEXT NOT NULL, line INTEGER NOT NULL); CREATE TABLE IF NOT EXISTS reviews (id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL, snapshot TEXT NOT NULL, author_model TEXT NOT NULL, reviewer_model TEXT NOT NULL, approved INTEGER NOT NULL); CREATE TABLE IF NOT EXISTS approvals (id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL, snapshot TEXT NOT NULL, check_id INTEGER NOT NULL, review_id INTEGER NOT NULL); CREATE TABLE IF NOT EXISTS verification_runs (id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL, before_hash TEXT NOT NULL, after_hash TEXT NOT NULL, build_command TEXT NOT NULL, test_command TEXT NOT NULL, results TEXT NOT NULL, valid INTEGER NOT NULL, created TEXT NOT NULL)");
  for(const table of ['reviews','approvals']){const cols=next.prepare(`PRAGMA table_info(${table})`).all() as {name:string}[];if(!cols.some(c=>c.name==='base_hash'))next.exec(`ALTER TABLE ${table} ADD COLUMN base_hash TEXT NOT NULL DEFAULT ''`);}
  next.exec("UPDATE jobs SET status='verification interrupted; retry required' WHERE status='checks running; apply blocked'");
  db?.close();db=next;project=selected; await rebuildIndex(); startWatcher(); return project;
 });
 ipcMain.handle('project:ignore-get',()=>String(database().prepare("SELECT value FROM settings WHERE key='ignore_patterns'").get()?.value||''));
 ipcMain.handle('project:ignore-set',async(_e,patterns:string)=>{
   if(typeof patterns!=='string'||patterns.length>100000||patterns.includes('\0'))throw new Error('Invalid project ignore patterns');
   const d=database(),expected=generation;ignore().add(patterns);
   d.prepare("INSERT INTO settings(key,value) VALUES ('ignore_patterns',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value").run(patterns);
   await rebuildIndex(expected);if(expected!==generation)throw new Error('Project changed');if(!win.isDestroyed())win.webContents.send('project:changed');
 });
 ipcMain.handle('project:files',files);
 ipcMain.handle('project:create-file',async(_e,name:string)=>{
   if(typeof name!=='string'||!name||name.length>1000||path.isAbsolute(name))throw new Error('Enter a relative project file path');
   const parts=name.replace(/\\/g,'/').split('/');if(parts.some(x=>!x||x==='.'||x==='..'||/[<>:"|?*\x00-\x1f]/.test(x)||/[. ]$/.test(x)||/^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\.|$)/i.test(x)))throw new Error('Invalid file path');
   const root=project,expected=generation;if(!root)throw new Error('Open a project first');const parentName=parts.slice(0,-1).join(path.sep)||'.';const parent=await checked(parentName);const stat=await fs.stat(parent);if(!stat.isDirectory())throw new Error('Parent folder does not exist');
   if(expected!==generation)throw new Error('Project changed');const target=path.join(parent,parts.at(-1)!);const file=await fs.open(target,'wx');await file.close();return parts.join(path.sep);
 });
 ipcMain.handle('project:read',(_e,name:string)=>read(name));
 ipcMain.handle('project:save',async(_e,name:string,content:string,fingerprint:string)=>{
   if(typeof content!=='string'||content.length>2_000_000) throw new Error('Invalid file contents');
   const p=await checked(name); const current=await fs.readFile(p); if(hash(current)!==fingerprint) throw new Error('File changed on disk. Save stopped; reload or reconcile your edits.');
   await fs.writeFile(p,content,'utf8'); return {fingerprint:hash(Buffer.from(content,'utf8'))};
 });
 ipcMain.handle('project:search',async(_e,query:string)=>{
   if(typeof query!=='string'||!query.trim()||query.length>200) return [];
   await rebuildIndex(); const found:{file:string;line:number;text:string}[]=[]; const needle=query.toLowerCase();
   const rows=database().prepare('SELECT path,content FROM file_index ORDER BY path').all() as {path:string;content:string}[];
   for(const row of rows){const lines=row.content.split(/\r?\n/); for(let i=0;i<lines.length;i++){if(lines[i].toLowerCase().includes(needle)){found.push({file:row.path,line:i+1,text:lines[i].slice(0,240)});if(found.length>=200)return found;}}}
   return found;
 });
 ipcMain.handle('project:symbols',async(_e,query:string)=>{
   if(typeof query!=='string'||!query.trim()||query.length>200)return {symbols:[],calls:[]};await rebuildIndex();const q=`%${query.replace(/[\\%_]/g,'\\$&')}%`;
   const symbols=database().prepare("SELECT path,name,kind,line FROM symbols WHERE name LIKE ? ESCAPE '\\' LIMIT 100").all(q);
   const calls=database().prepare("SELECT path,caller,target,line FROM calls WHERE target LIKE ? ESCAPE '\\' LIMIT 100").all(q);
   return {symbols,calls};
 });
 ipcMain.handle('terminal:start',()=>{
   if(!project)throw new Error('Open a project first');
   if(terminal)throw new Error('A terminal session is already running');
   const session=++terminalSession;
   const instance=pty.spawn('powershell.exe',['-NoLogo'],{name:'xterm-256color',cols:100,rows:24,cwd:project,env:process.env as {[key:string]:string},useConpty:true});terminal=instance;
   instance.onData(data=>{if(session===terminalSession&&!win.isDestroyed())win.webContents.send('terminal:data',data)});
   instance.onExit(({exitCode})=>{if(session===terminalSession){terminal=undefined;if(!win.isDestroyed())win.webContents.send('terminal:exit',exitCode)}});
   return true;
 });
 ipcMain.handle('terminal:write',(_e,data:string)=>{if(typeof data!=='string'||data.length>32000)throw new Error('Invalid terminal input');if(!terminal)throw new Error('Terminal is not running');terminal.write(data)});
 ipcMain.handle('terminal:resize',(_e,cols:number,rows:number)=>{if(terminal&&Number.isInteger(cols)&&Number.isInteger(rows)&&cols>=2&&cols<=500&&rows>=2&&rows<=200)terminal.resize(cols,rows)});
 ipcMain.handle('terminal:stop',()=>{terminalSession++;terminal?.kill();terminal=undefined;return true});
 ipcMain.handle('terminal:run',async(_e,command:string)=>{
   if(!project)throw new Error('Open a project first');
   if(typeof command!=='string'||!command.trim()||command.length>4000)throw new Error('Enter a command under 4,000 characters');
   try {const result=await execFileAsync('powershell.exe',['-NoLogo','-NoProfile','-NonInteractive','-Command',command],{cwd:project,timeout:120000,maxBuffer:2_000_000,windowsHide:true});return {output:result.stdout+result.stderr,exit:0};}
   catch(e){const err=e as Error & {stdout?:string;stderr?:string;code?:number|string;killed?:boolean};return {output:(err.stdout||'')+(err.stderr||'')+(err.killed?'\nCommand timed out.':''),exit:typeof err.code==='number'?err.code:-1};}
 });
 ipcMain.handle('project:git-stage',async(_e,name:string,fingerprint:string)=>{
   if(typeof name!=='string'||!name||!project)throw new Error('Open a file in a project first');
   const root=project,expected=generation;const target=await checked(name);if(!(await files()).includes(name))throw new Error('Only visible project files can be staged');if(expected!==generation)throw new Error('Project changed');if(typeof fingerprint!=='string'||hash(await fs.readFile(target))!==fingerprint)throw new Error('File changed on disk; reload before staging');
   try{await execFileAsync('git',['--literal-pathspecs','add','--',name],{cwd:root,timeout:10000,maxBuffer:1_000_000,windowsHide:true});}
   catch(e){throw new Error(`Could not stage file: ${(e as Error).message}`)}
 });
 ipcMain.handle('project:git-unstage',async(_e,name:string)=>{
   if(typeof name!=='string'||!name||!project)throw new Error('Open a file in a project first');const root=project,expected=generation;
   await checked(name);if(expected!==generation)throw new Error('Project changed');
   let hasHead=true;try{await execFileAsync('git',['rev-parse','--verify','HEAD'],{cwd:root,timeout:10000,windowsHide:true})}catch{hasHead=false}
   if(expected!==generation)throw new Error('Project changed');
   const args=hasHead?['--literal-pathspecs','restore','--staged','--',name]:['--literal-pathspecs','rm','--cached','--',name];
   try{await execFileAsync('git',args,{cwd:root,timeout:10000,maxBuffer:1_000_000,windowsHide:true})}catch(e){throw new Error(`Could not unstage file: ${(e as Error).message}`)}
 });
 async function gitCommitPreview(){
   if(!project)throw new Error('Open a project first');const root=project,expected=generation;
   const run=(args:string[])=>execFileAsync('git',args,{cwd:root,timeout:10000,maxBuffer:2_000_000,windowsHide:true});
   const repo=(await run(['rev-parse','--show-toplevel'])).stdout.trim();if(path.resolve(repo).toLowerCase()!==root.toLowerCase())throw new Error('Open the repository root to commit');
   const tree=(await run(['write-tree'])).stdout.trim();const diff=(await run(['-c','core.pager=cat','diff','--cached','--no-ext-diff','--no-textconv','--'])).stdout;
   if(!diff.trim())throw new Error('No staged text changes to commit');let head='';try{head=(await run(['rev-parse','--verify','HEAD'])).stdout.trim()}catch{/* Initial commit. */}
   if(expected!==generation)throw new Error('Project changed');return {tree,diff,head};
 }
 ipcMain.handle('project:git-commit-preview',()=>gitCommitPreview());
 ipcMain.handle('project:git-commit',async(_e,message:string,tree:string,head:string)=>{
   if(typeof message!=='string'||!message.trim()||message.length>1000||/[\r\n\0]/.test(message))throw new Error('Enter a single-line commit message');
   const root=project,expected=generation;const current=await gitCommitPreview();if(expected!==generation||current.tree!==tree||current.head!==head)throw new Error('Staged changes or branch changed; review them again');
   try{const result=await execFileAsync('git',['commit','-m',message.trim()],{cwd:root,timeout:120000,maxBuffer:2_000_000,windowsHide:true,env:{...process.env,GIT_TERMINAL_PROMPT:'0'}});return result.stdout+result.stderr}
   catch(e){const error=e as Error & {stderr?:string;stdout?:string};throw new Error(`Commit failed: ${error.stderr||error.stdout||error.message}`)}
 });
 ipcMain.handle('project:git-diff',async()=>{if(!project)throw new Error('Open a project first');try{const result=await execFileAsync('git',['-c','core.pager=cat','diff','--no-ext-diff','--no-textconv','--'],{cwd:project,timeout:10000,maxBuffer:2_000_000,windowsHide:true});const staged=await execFileAsync('git',['-c','core.pager=cat','diff','--cached','--no-ext-diff','--no-textconv','--'],{cwd:project,timeout:10000,maxBuffer:2_000_000,windowsHide:true});return `STAGED CHANGES\n${staged.stdout||'(none)'}\nUNSTAGED CHANGES\n${result.stdout||'(none)'}`;}catch{throw new Error('Git diff unavailable for this folder')}});
 ipcMain.handle('project:git',async()=>{if(!project) return '';try{return (await execFileAsync('git',['status','--short','--branch'],{cwd:project,timeout:10000,maxBuffer:1_000_000})).stdout;}catch{return 'Git status unavailable for this folder.';}});
 ipcMain.handle('jobs:list',(_e,query:string='')=>({jobs:database().prepare('SELECT * FROM jobs WHERE instr(lower(request),lower(?))>0 OR instr(lower(status),lower(?))>0 ORDER BY id DESC').all(typeof query==='string'?query.slice(0,200):'',typeof query==='string'?query.slice(0,200):''),paused:database().prepare("SELECT value FROM settings WHERE key='paused'").get()?.value==='true'}));
 ipcMain.handle('jobs:add',(_e,request:string)=>{if(typeof request!=='string'||!request.trim()||request.length>10000)throw new Error('Enter a job under 10,000 characters');const d=database(); transaction(d,()=>{const result=d.prepare('INSERT INTO jobs (request,status,created) VALUES (?,?,?)').run(request.trim(),'blocked: coordinator unavailable',new Date().toISOString());d.prepare('INSERT INTO job_events (job_id,kind,detail,created) VALUES (?,?,?,?)').run(Number(result.lastInsertRowid),'requested',request.trim(),new Date().toISOString())});return true;});
 ipcMain.handle('jobs:cancel',(_e,id:number)=>{
   if(!Number.isSafeInteger(id)||id<1)throw new Error('Invalid job');const d=database();
   if(verifying.has(id)||scratchLocks.has(id))throw new Error('Wait for the current operation to finish before cancelling');
   transaction(d,()=>{const job=d.prepare('SELECT status FROM jobs WHERE id=?').get(id) as {status:string}|undefined;if(!job)throw new Error('Job not found');if(job.status==='cancelled')throw new Error('Job is already cancelled');d.prepare('UPDATE jobs SET status=? WHERE id=?').run('cancelled',id);d.prepare('UPDATE verification_runs SET valid=0 WHERE job_id=?').run(id);d.prepare('INSERT INTO job_events (job_id,kind,detail,created) VALUES (?,?,?,?)').run(id,'cancelled','Cancelled by user',new Date().toISOString())});
 });
 ipcMain.handle('jobs:requeue',(_e,id:number)=>{
   if(!Number.isSafeInteger(id)||id<1)throw new Error('Invalid job');const d=database();
   transaction(d,()=>{const job=d.prepare('SELECT status FROM jobs WHERE id=?').get(id) as {status:string}|undefined;if(!job||job.status!=='cancelled')throw new Error('Only cancelled jobs can be requeued');const piece=d.prepare('SELECT job_id FROM pieces WHERE job_id=?').get(id);const status=piece?'scratch ready; blocked: independent review unavailable':'blocked: coordinator unavailable';d.prepare('UPDATE jobs SET status=? WHERE id=?').run(status,id);d.prepare('INSERT INTO job_events (job_id,kind,detail,created) VALUES (?,?,?,?)').run(id,'requeued','Requeued by user; checks must be run again',new Date().toISOString())});
 });
 const scratchLocks=new Set<number>();
 ipcMain.handle('jobs:scratch',async(_e,id:number)=>{
   if(!Number.isSafeInteger(id)||id<1)throw new Error('Invalid job');
   const d=database(), expected=generation, root=project;
   const job=d.prepare('SELECT status FROM jobs WHERE id=?').get(id) as {status:string}|undefined;if(!job)throw new Error('Job not found');if(job.status==='cancelled')throw new Error('Job is cancelled');
   const existing=d.prepare('SELECT scratch_path,base_path FROM pieces WHERE job_id=?').get(id) as {scratch_path:string;base_path:string}|undefined;
   if(existing){try{const b=await fs.stat(existing.base_path),w=await fs.stat(existing.scratch_path);if(!b.isDirectory()||!w.isDirectory())throw new Error()}catch{throw new Error('Scratch workspace is missing or damaged; do not recreate it automatically')}return existing.scratch_path;}
   if(scratchLocks.has(id))throw new Error('Scratch workspace is already being prepared');
   scratchLocks.add(id);
   try {
     const destination=path.join(app.getPath('userData'),'scratch',hash(root),`${id}-${crypto.randomUUID()}`);
     const base=path.join(destination,'base'), scratch=path.join(destination,'work');
     await fs.mkdir(base,{recursive:true});await fs.mkdir(scratch,{recursive:true});
     const manifest:{path:string;fingerprint:string}[]=[]; let totalBytes=0;
     for(const name of await files()) {
       if(expected!==generation)throw new Error('Project changed while preparing scratch');
       const source=await checked(name);const stat=await fs.stat(source);totalBytes+=stat.size;if(totalBytes>256_000_000)throw new Error('Scratch snapshot exceeds the 256 MB limit');const bytes=await fs.readFile(source);
       for(const dest of [base,scratch]){const out=path.join(dest,name);await fs.mkdir(path.dirname(out),{recursive:true});await fs.writeFile(out,bytes,{flag:'wx'});}
       manifest.push({path:name,fingerprint:hash(bytes)});
     }
     if(expected!==generation)throw new Error('Project changed while preparing scratch');
     const finalNames=await files();const snapshotNames=manifest.map(x=>x.path).sort((a,b)=>a.localeCompare(b));if(finalNames.length!==snapshotNames.length||finalNames.some((n,i)=>n!==snapshotNames[i]))throw new Error('Project files changed while preparing scratch; retry');
     for(const item of manifest){if(expected!==generation||hash(await fs.readFile(await checked(item.path)))!==item.fingerprint)throw new Error('Project files changed while preparing scratch; retry')}
     transaction(d,()=>{d.prepare('INSERT INTO pieces (job_id,base_path,scratch_path,manifest) VALUES (?,?,?,?)').run(id,base,scratch,JSON.stringify(manifest));
     d.prepare('INSERT INTO job_events (job_id,kind,detail,created) VALUES (?,?,?,?)').run(id,'scratch_created',`Snapshot of ${manifest.length} files`,new Date().toISOString());
     d.prepare('UPDATE jobs SET status=? WHERE id=?').run('scratch ready; blocked: independent review unavailable',id)});
     return scratch;
   } finally {scratchLocks.delete(id)}
 });
 ipcMain.handle('jobs:open-scratch',async(_e,id:number)=>{
   if(!Number.isSafeInteger(id))throw new Error('Invalid job');const row=database().prepare('SELECT scratch_path FROM pieces WHERE job_id=?').get(id) as {scratch_path:string}|undefined;
   if(!row)throw new Error('Prepare scratch first');const error=await shell.openPath(row.scratch_path);if(error)throw new Error(error);
 });
 ipcMain.handle('jobs:diff',async(_e,id:number)=>{
   if(!Number.isSafeInteger(id))throw new Error('Invalid job');const row=database().prepare('SELECT base_path,scratch_path FROM pieces WHERE job_id=?').get(id) as {base_path:string;scratch_path:string}|undefined;
   if(!row)throw new Error('Prepare scratch first');
   try {return (await execFileAsync('git',['diff','--no-index','--no-ext-diff','--no-textconv','--',row.base_path,row.scratch_path],{timeout:15000,maxBuffer:2_000_000,windowsHide:true})).stdout || 'No changes in scratch.';}
   catch(e){const err=e as Error & {code?:number;stdout?:string};if(err.code===1)return err.stdout || 'Changes found but no text diff is available.';throw new Error('Could not generate diff: '+err.message);}
 });
 ipcMain.handle('jobs:conflicts',async(_e,id:number)=>{
   if(!Number.isSafeInteger(id))throw new Error('Invalid job');const expected=generation;
   const row=database().prepare('SELECT manifest,scratch_path FROM pieces WHERE job_id=?').get(id) as {manifest:string;scratch_path:string}|undefined;if(!row)throw new Error('Prepare scratch first');
   const manifest=JSON.parse(row.manifest) as {path:string;fingerprint:string}[];const conflicts:string[]=[];
   for(const item of manifest){if(expected!==generation)throw new Error('Project changed during conflict check');try{const file=await checked(item.path);if(hash(await fs.readFile(file))!==item.fingerprint)conflicts.push(item.path);}catch{conflicts.push(item.path)}}
   const baseline=new Set(manifest.map(x=>x.path));
   async function additions(dir:string){for(const ent of await fs.readdir(dir,{withFileTypes:true})){if(ent.isSymbolicLink())throw new Error('Scratch contains a symbolic link');const full=path.join(dir,ent.name);if(ent.isDirectory())await additions(full);else if(ent.isFile()){const rel=path.relative(row!.scratch_path,full);if(!baseline.has(rel)){try{await fs.lstat(path.join(project,rel));conflicts.push(rel)}catch(error){if((error as NodeJS.ErrnoException).code!=='ENOENT')throw error}}}}}
   await additions(row.scratch_path);return [...new Set(conflicts)].sort();
 });
 ipcMain.handle('verification:config',()=>{
   const d=database(); const get=(key:string)=>String(d.prepare('SELECT value FROM settings WHERE key=?').get(key)?.value||'');
   return {build:get('build_command'),test:get('test_command')};
 });
 ipcMain.handle('verification:configure',(_e,build:string,test:string)=>{
   if(typeof build!=='string'||typeof test!=='string'||build.length>4000||test.length>4000)throw new Error('Invalid verification commands');
   const d=database();transaction(d,()=>{const put=d.prepare('INSERT INTO settings (key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value');put.run('build_command',build.trim());put.run('test_command',test.trim());d.prepare('UPDATE verification_runs SET valid=0').run()});
 });
 const verifying=new Set<number>();
 ipcMain.handle('verification:run',async(_e,id:number)=>{
   if(!Number.isSafeInteger(id))throw new Error('Invalid job');const d=database(), expected=generation;
   if(d.prepare("SELECT value FROM settings WHERE key='paused'").get()?.value==='true')throw new Error('Team is paused');
   const job=d.prepare('SELECT status FROM jobs WHERE id=?').get(id) as {status:string}|undefined;if(!job||job.status==='cancelled')throw new Error('Job is unavailable or cancelled');
   const piece=d.prepare('SELECT scratch_path FROM pieces WHERE job_id=?').get(id) as {scratch_path:string}|undefined;
   if(!piece)throw new Error('Prepare scratch first');
   const get=(key:string)=>String(d.prepare('SELECT value FROM settings WHERE key=?').get(key)?.value||'');const build=get('build_command'),test=get('test_command');
   if(!build||!test)throw new Error('Configure both build and test commands before verification');
   if(verifying.has(id))throw new Error('Verification is already running');verifying.add(id);
   transaction(d,()=>{d.prepare('UPDATE verification_runs SET valid=0 WHERE job_id=?').run(id);d.prepare('UPDATE jobs SET status=? WHERE id=?').run('checks running; apply blocked',id)});
   d.prepare('INSERT INTO job_events (job_id,kind,detail,created) VALUES (?,?,?,?)').run(id,'verification_started','Build and test checks started',new Date().toISOString());
   try {
     const beforeHash=await fingerprintTree(piece.scratch_path);
     const results:{kind:string;command:string;exit:number;output:string}[]=[];
     for(const [kind,command] of [['build',build],['test',test]]) {
       if(expected!==generation)throw new Error('Project changed during verification');
       if(d.prepare("SELECT value FROM settings WHERE key='paused'").get()?.value==='true')throw new Error('Team paused during verification');
       let result:{kind:string;command:string;exit:number;output:string};
       try{const r=await execFileAsync('powershell.exe',['-NoLogo','-NoProfile','-NonInteractive','-Command',`$ErrorActionPreference = 'Stop'; & { ${command} }; if (-not $?) { exit 1 }; if ($null -ne $LASTEXITCODE) { exit $LASTEXITCODE }`],{cwd:piece.scratch_path,timeout:120000,maxBuffer:2_000_000,windowsHide:true});result={kind,command,exit:0,output:r.stdout+r.stderr};}
       catch(e){const err=e as Error & {stdout?:string;stderr?:string;code?:number;killed?:boolean};result={kind,command,exit:typeof err.code==='number'?err.code:-1,output:(err.stdout||'')+(err.stderr||'')+(err.killed?'\nTimed out.':'')};}
       if(expected!==generation)throw new Error('Project changed during verification');
       if(d.prepare("SELECT value FROM settings WHERE key='paused'").get()?.value==='true')throw new Error('Team paused during verification');
       if(get('build_command')!==build||get('test_command')!==test)throw new Error('Verification commands changed during the run');
       results.push(result);d.prepare('INSERT INTO job_events (job_id,kind,detail,created) VALUES (?,?,?,?)').run(id,kind,JSON.stringify(result),new Date().toISOString());
       if(result.exit!==0)break;
     }
     const afterHash=await fingerprintTree(piece.scratch_path);
     if(expected!==generation)throw new Error('Project changed during verification');
     const passed=results.length===2&&results.every(r=>r.exit===0)&&beforeHash===afterHash;
     d.prepare('INSERT INTO verification_runs (job_id,before_hash,after_hash,build_command,test_command,results,valid,created) VALUES (?,?,?,?,?,?,?,?)').run(id,beforeHash,afterHash,build,test,JSON.stringify(results),passed?1:0,new Date().toISOString());
     const status=passed?'checks passed for snapshot; blocked: independent review unavailable':beforeHash!==afterHash?'scratch changed during checks; verification invalid':'checks failed; apply blocked';
     d.prepare('UPDATE jobs SET status=? WHERE id=?').run(status,id);
     if(beforeHash!==afterHash)results.push({kind:'integrity',command:'',exit:-1,output:'Scratch changed during verification; results are invalid. Restore the intended files and rerun checks.'});
     return results;
   } catch(error){
     if(expected===generation){const message=error instanceof Error?error.message:String(error);d.prepare('UPDATE jobs SET status=? WHERE id=?').run('verification interrupted; retry required',id);d.prepare('INSERT INTO job_events (job_id,kind,detail,created) VALUES (?,?,?,?)').run(id,'verification_interrupted',message,new Date().toISOString());}
     throw error;
   } finally {verifying.delete(id)}
 });
 ipcMain.handle('jobs:gate',async(_e,id:number)=>{
   if(!Number.isSafeInteger(id)||id<1)throw new Error('Invalid job');const d=database(),expected=generation;
   const piece=d.prepare('SELECT scratch_path,base_path,manifest FROM pieces WHERE job_id=?').get(id) as {scratch_path:string;base_path:string;manifest:string}|undefined;if(!piece)throw new Error('Prepare scratch first');
   const snapshot=await fingerprintTree(piece.scratch_path);const baseSnapshot=await fingerprintTree(piece.base_path);const original=JSON.parse(piece.manifest) as {path:string;fingerprint:string}[];const originalHash=fingerprintManifest(original.map(x=>({name:x.path.split(path.sep).join('/'),hash:x.fingerprint}))); if(expected!==generation)throw new Error('Project changed during gate check');
   const run=d.prepare('SELECT * FROM verification_runs WHERE job_id=? ORDER BY id DESC LIMIT 1').get(id) as {id:number;after_hash:string;valid:number;build_command:string;test_command:string}|undefined;
   const get=(key:string)=>String(d.prepare('SELECT value FROM settings WHERE key=?').get(key)?.value||'');
   const review=d.prepare('SELECT * FROM reviews WHERE job_id=? ORDER BY id DESC LIMIT 1').get(id) as {id:number;snapshot:string;author_model:string;reviewer_model:string;approved:number;base_hash:string}|undefined;
   const approval=d.prepare('SELECT * FROM approvals WHERE job_id=? ORDER BY id DESC LIMIT 1').get(id) as {snapshot:string;check_id:number;review_id:number;base_hash:string}|undefined;
   const reasons=gateReasons({paused:get('paused')==='true',snapshot,baseSnapshot,baseIntact:baseSnapshot===originalHash,check:run&&{id:run.id,hash:run.after_hash,passed:run.valid===1,commandsMatch:!!get('build_command')&&!!get('test_command')&&run.build_command===get('build_command')&&run.test_command===get('test_command')},review:review&&{id:review.id,hash:review.snapshot,baseHash:review.base_hash,authorModel:review.author_model,reviewerModel:review.reviewer_model,approved:review.approved===1},approval:approval&&{snapshot:approval.snapshot,baseHash:approval.base_hash,checkId:approval.check_id,reviewId:approval.review_id}});
   return {snapshot,reasons,applyAvailable:false};
 });
 ipcMain.handle('jobs:history',(_e,id:number)=>{
   if(!Number.isSafeInteger(id)||id<1)throw new Error('Invalid job');
   return database().prepare('SELECT kind,detail,created FROM job_events WHERE job_id=? ORDER BY id').all(id);
 });
 ipcMain.handle('jobs:pause',(_e,paused:boolean)=>{database().prepare("INSERT INTO settings (key,value) VALUES ('paused',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value").run(String(!!paused));return true;});
 let localRequest:AbortController|undefined;
 ipcMain.handle('ollama:ask',async(_e,model:string,prompt:string)=>{
   if(typeof model!=='string'||!model||model.length>200||typeof prompt!=='string'||!prompt.trim()||prompt.length>12000)throw new Error('Choose a model and enter a prompt under 12,000 characters');
   if(localRequest)throw new Error('A local request is already running');
   const controller=new AbortController();localRequest=controller;const timeout=setTimeout(()=>controller.abort(),120000);
   try{
     const list=await fetch('http://127.0.0.1:11434/api/tags',{signal:controller.signal});if(!list.ok)throw new Error('Ollama is unavailable');
     const data=await list.json() as {models?:{name?:string}[]};if(!data.models?.some(x=>x.name===model))throw new Error('Selected model is not in the local model list');
     const response=await fetch('http://127.0.0.1:11434/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({model,messages:[{role:'user',content:prompt}],stream:false,think:false,keep_alive:0,options:{num_predict:512}}),signal:controller.signal});
     if(!response.ok)throw new Error(`Ollama returned HTTP ${response.status}`);
     const result=await response.json() as {model?:unknown;done?:unknown;message?:{content?:unknown}};
     if(result.done!==true||result.model!==model||typeof result.message?.content!=='string')throw new Error('Incomplete or unexpected Ollama response');
     return result.message.content;
   } finally {clearTimeout(timeout);localRequest=undefined;}
 });
 ipcMain.handle('ollama:cancel',()=>{localRequest?.abort()});
 ipcMain.handle('providers:list',async()=>{
   let local={name:'Ollama',status:'Unavailable',detail:'Local service did not respond. No local model work is enabled.'};
   try {const response=await fetch('http://127.0.0.1:11434/api/tags',{signal:AbortSignal.timeout(3000)});if(!response.ok)throw new Error('Service unavailable');
     const payload=await response.json() as {models?:{name?:unknown}[]};const names=Array.isArray(payload.models)?payload.models.map(x=>x.name).filter((x):x is string=>typeof x==='string').slice(0,30):[];
     local={name:'Ollama',status:'Connected',detail:names.length?`Models available: ${names.join(', ')}. Manual questions are available; job execution is not enabled.`:'Service reachable; no models reported. Job execution is not enabled.'};
   } catch{/* service is offline or inaccessible */}
   return [{name:'Muse / Jett',status:'Unavailable',detail:'No verified coordinator interface or usage data.'},{name:'GPT accounts',status:'Unavailable',detail:'No supported account integration verified.'},local];
 });
 ipcMain.on('close:confirm',()=>{closeAllowed=true;win.close()}); createWindow();
});
app.on('window-all-closed',()=>{++generation; watcher?.close(); if(indexTimer)clearTimeout(indexTimer); terminalSession++;terminal?.kill(); db?.close();app.quit()});


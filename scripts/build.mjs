import {readFileSync,writeFileSync,openSync,closeSync,unlinkSync} from 'node:fs';
import {spawnSync} from 'node:child_process';
import path from 'node:path';

const root=process.cwd();const versionPath=path.join(root,'BUILD_VERSION.json');const logPath=path.join(root,'CHANGELOG.md');const pendingPath=path.join(root,'CHANGELOG_PENDING.md');const lockPath=path.join(root,'.build-version.lock');
let lock;try{lock=openSync(lockPath,'wx')}catch{console.error('Another versioned build may be running; check .build-version.lock.');process.exit(1)}
const originalVersion=readFileSync(versionPath,'utf8');const originalLog=readFileSync(logPath,'utf8');const pending=readFileSync(pendingPath,'utf8');
let success=false;
try{
 const parsed=JSON.parse(originalVersion.replace(/^\uFEFF/,''));if(!Number.isSafeInteger(parsed.build)||parsed.build<0)throw new Error('Invalid build number');
 const label=`V0.${String(parsed.build).padStart(3,'0')}`;const notes=pending.trim();
 const heading='# Jetstream Coder changelog';if(!originalLog.replace(/^\uFEFF/,'').startsWith(heading))throw new Error('Unexpected changelog format');
 const clean=originalLog.replace(/^\uFEFF/,'');const marker=`## ${label}`;
 if(notes){const at=clean.indexOf(marker);if(at<0)throw new Error(`Missing changelog section ${label}`);const insert=at+marker.length;writeFileSync(logPath,clean.slice(0,insert)+'\n\n'+notes+clean.slice(insert));}
 const run=(script,args)=>{const result=spawnSync(process.execPath,[path.join(root,script),...args],{cwd:root,stdio:'inherit',env:process.env});if(result.error)throw result.error;if(result.status!==0)throw new Error(`${script} failed (${result.status??result.signal})`)};
 run('node_modules/vite/bin/vite.js',['build']);
 run('node_modules/esbuild/bin/esbuild',['electron/main.ts','electron/preload.ts','electron/gate.ts','electron/change-fixture.ts','--bundle','--platform=node','--format=cjs','--external:electron','--external:node-pty','--external:web-tree-sitter','--outdir=dist-electron','--out-extension:.js=.cjs']);
 run('node_modules/typescript/bin/tsc',['--noEmit']);
 writeFileSync(pendingPath,'');success=true;console.log(`Built ${label}`);
} catch(error){writeFileSync(versionPath,originalVersion);writeFileSync(logPath,originalLog);console.error(error);process.exitCode=1}
finally {closeSync(lock);unlinkSync(lockPath);if(!success)console.error('Build version and changelog restored; pending notes retained.')}

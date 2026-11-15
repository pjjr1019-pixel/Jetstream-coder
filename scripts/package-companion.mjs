import {cp,mkdir,readFile,writeFile,readdir,stat} from 'node:fs/promises';
import {createHash} from 'node:crypto';
import path from 'node:path';
import {execFileSync} from 'node:child_process';

// Builds a local development bundle only. Never installs or starts a service.
const root=process.cwd(),version=JSON.parse(await readFile('BUILD_VERSION.json','utf8'));
const nativeReport=JSON.parse(execFileSync('python',[path.join(root,'scripts/verify-android-native.py'),path.join(root,'integrations/deskhand/android/app/build/outputs/apk/debug/app-debug.apk')],{encoding:'utf8',windowsHide:true}));
const label=`V0.${String(version.build).padStart(3,'0')}`;
const out=path.join(root,'artifacts',`Jetstream-${label}-${Date.now()}`);await mkdir(out,{recursive:true});
const copy=async(from,to)=>cp(path.join(root,from),path.join(out,to),{recursive:true});
await copy('node_modules/electron/dist','Jetstream');
const app=path.join(out,'Jetstream/resources/app');await mkdir(app,{recursive:true});
for(const dir of ['dist','dist-electron'])await cp(path.join(root,dir),path.join(app,dir),{recursive:true});
await writeFile(path.join(app,'package.json'),JSON.stringify({name:'jetstream-coder',version:'0.1.0',main:'dist-electron/main.cjs'}));
for(const name of ['node-pty','web-tree-sitter','tree-sitter-wasms'])await cp(path.join(root,'node_modules',name),path.join(app,'node_modules',name),{recursive:true});
await cp(path.join(root,'TaskAgent'),path.join(app,'TaskAgent'),{recursive:true,filter:file=>!['agent_config.json','agent.log','__pycache__'].includes(path.basename(file))});
const deskhand = 'Jetstream/resources/app/Deskhand';
await copy('integrations/deskhand/windows/Deskhand.Windows.App/bin/Release/net8.0-windows',deskhand);
await copy('integrations/deskhand/android/app/build/outputs/apk/debug/app-debug.apk',`${deskhand}/Deskhand-Android-verification.apk`);
await copy('integrations/deskhand/source-manifest.json',`${deskhand}/source-manifest.json`);
await copy('integrations/deskhand/Start-Deskhand.ps1',`${deskhand}/Start-Deskhand.ps1`);
await copy('integrations/deskhand/README.md','README.md');
const hashes=[];
async function hashTree(dir){for(const e of await readdir(dir,{withFileTypes:true})){const file=path.join(dir,e.name);if(e.isDirectory())await hashTree(file);else if(e.isFile())hashes.push({path:path.relative(out,file).replaceAll('\\','/'),size:(await stat(file)).size,sha256:createHash('sha256').update(await readFile(file)).digest('hex')})}}
await writeFile(path.join(out,'android-native-alignment.json'),JSON.stringify(nativeReport,null,2));
const sourceCommit=execFileSync('git',['rev-parse','HEAD'],{encoding:'utf8',windowsHide:true}).trim();
const sourceDirty=Boolean(execFileSync('git',['status','--porcelain'],{encoding:'utf8',windowsHide:true}).trim());
await hashTree(out);await writeFile(path.join(out,'artifact-manifest.json'),JSON.stringify({version:label,kind:'development-verification-bundle',sourceCommit,sourceDirty,androidNativeAlignment:true,productionApply:false,phoneAcceptance:false,files:hashes},null,2));
console.log(`Created ${out}`);

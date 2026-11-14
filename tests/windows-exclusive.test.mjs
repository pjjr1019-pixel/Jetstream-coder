import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,writeFile,readFile,rm} from 'node:fs/promises';
import {spawn} from 'node:child_process';
import os from 'node:os';import path from 'node:path';

test('Windows exclusive access rejects competing opens and preserves later edits',{skip:process.platform!=='win32',timeout:20000},async()=>{
 const root=await mkdtemp(path.join(os.tmpdir(),'jetstream-exclusive-'));const file=path.join(root,'file.txt');await writeFile(file,'before');let proc;
 try{
  const script=`$p=$env:JETSTREAM_TEST_FILE; $s=[System.IO.File]::Open($p,[System.IO.FileMode]::Open,[System.IO.FileAccess]::ReadWrite,[System.IO.FileShare]::None); try { [Console]::Out.WriteLine('LOCKED'); [Console]::Out.Flush(); [void][Console]::In.ReadLine(); $s.Position=0; $reader=[System.IO.StreamReader]::new($s,[System.Text.Encoding]::UTF8,$false,1024,$true); $v=$reader.ReadToEnd(); if($v -ne 'before'){ throw 'conflict' }; $b=[System.Text.Encoding]::UTF8.GetBytes('after'); $s.Position=0; $s.SetLength(0); $s.Write($b,0,$b.Length); $s.Flush($true) } finally { $s.Dispose() }`;
  proc=spawn('powershell.exe',['-NoLogo','-NoProfile','-NonInteractive','-EncodedCommand',Buffer.from(script,'utf16le').toString('base64')],{env:{...process.env,JETSTREAM_TEST_FILE:file},windowsHide:true,stdio:['pipe','pipe','pipe']});let stderr='';proc.stderr.on('data',x=>stderr+=x);await new Promise((resolve,reject)=>{proc.stdout.on('data',x=>{if(String(x).includes('LOCKED'))resolve()});proc.once('exit',code=>reject(new Error(`Lock helper exited ${code}: ${stderr}`)))});
  await assert.rejects(writeFile(file,'competing edit'),error=>['EBUSY','EPERM','EACCES'].includes(error.code));proc.stdin.end('\n');const code=await new Promise(resolve=>proc.once('exit',resolve));assert.equal(code,0,stderr);assert.equal(await readFile(file,'utf8'),'after');await writeFile(file,'later edit');assert.equal(await readFile(file,'utf8'),'later edit');
 }finally{if(proc&&proc.exitCode===null)proc.kill();await rm(root,{recursive:true,force:true,maxRetries:3,retryDelay:200})}
});

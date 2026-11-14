import React,{useEffect,useRef,useState} from 'react';
import {Terminal} from '@xterm/xterm';
import {FitAddon} from '@xterm/addon-fit';
import '@xterm/xterm/css/xterm.css';

export function TerminalPanel({project,visible,onError}:{project:string;visible:boolean;onError:(s:string)=>void}) {
 const host=useRef<HTMLDivElement>(null);const term=useRef<Terminal|null>(null);const fit=useRef<FitAddon|null>(null);const runningRef=useRef(false);const [running,setRunning]=useState(false);
 useEffect(()=>{if(!host.current)return;const t=new Terminal({cursorBlink:true,scrollback:5000,fontSize:13,theme:{background:'#171c25'}});const f=new FitAddon();t.loadAddon(f);t.open(host.current);term.current=t;fit.current=f;
  const data=window.coder.onTerminalData(text=>t.write(text));const exit=window.coder.onTerminalExit(code=>{runningRef.current=false;setRunning(false);t.writeln(`\r\n[Terminal exited: ${code}]`)});
  const input=t.onData(text=>{if(runningRef.current)void window.coder.terminalWrite(text).catch(e=>onError(String(e)))});
  return()=>{data();exit();input.dispose();t.dispose();};
 },[]);
 useEffect(()=>{runningRef.current=false;setRunning(false);term.current?.clear()},[project]);
 useEffect(()=>{if(!visible)return;requestAnimationFrame(()=>{try{fit.current?.fit();const t=term.current;if(t&&runningRef.current)void window.coder.terminalResize(t.cols,t.rows)}catch{/* hidden or zero sized */}})},[visible]);
 async function start(){try{await window.coder.terminalStart();runningRef.current=true;setRunning(true);fit.current?.fit();const t=term.current;if(t){await window.coder.terminalResize(t.cols,t.rows);t.focus()}}catch(e){onError(String(e))}}
 async function stop(){try{await window.coder.terminalStop();runningRef.current=false;setRunning(false);term.current?.writeln('\r\n[Terminal stopped]')}catch(e){onError(String(e))}}
 return <div className="terminal-panel" style={{display:visible?'block':'none'}}><div className="terminal-toolbar"><span>PowerShell · Your commands run with your Windows permissions</span> <button disabled={!project||running} onClick={()=>void start()}>Start terminal</button> <button disabled={!running} onClick={()=>void stop()}>Stop terminal</button></div><div className="terminal-screen" ref={host}/></div>
}

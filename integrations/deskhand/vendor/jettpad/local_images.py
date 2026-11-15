"""Local image requests: bounded explicit intent, durable jobs, no new listener."""
from pathlib import Path
import json, os, re, subprocess, sys, threading, time, uuid

_PATTERN=re.compile(r"^(?:(?:hey\s+)?jett[, ]+)?(?:(?:can|could|would)\s+you\s+)?(?:please\s+)?(?:generate|create|make|draw)\s+(?:me\s+)?(?:an?\s+)?(?:image|picture|illustration|wallpaper|artwork)\s*(?:(?:of|showing|depicting)\s+|:\s*)(.+)$",re.I|re.S)
def image_prompt(text):
    text=text.strip()
    if text.lower().startswith('/image '): return text[7:].strip() or None
    m=_PATTERN.fullmatch(text)
    return m.group(1).strip() if m else None

class ImageWorker:
    def __init__(self,engine,stop):
        self.engine=engine;self.stop=stop
    def run(self):
        store=self.engine.store
        with store.lock,store.db:
            store.db.execute("update outbox set state='image_interrupted' where route='image' and state='image_running'")
        while not self.stop.is_set():
            row=None
            with store.lock,store.db:
                row=store.db.execute("select m.id,m.text,o.requested_model from messages m join outbox o on o.id=m.id where o.route='image' and o.state='queued' order by m.ts limit 1").fetchone()
                if row:store.db.execute("update outbox set state='image_running' where id=? and state='queued'",(row['id'],))
            if not row:
                self.stop.wait(.5);continue
            identity=row['id'];prompt=image_prompt(row['text']) or (row['text'] if row['requested_model']=='image:SSD-1B' else None)
            target=self.engine.documents/'Jett Images'/(identity+'.png')
            target.parent.mkdir(parents=True,exist_ok=True)
            runner=Path(__file__).resolve().parent/'image_generation_runner.py'
            python=Path(__file__).resolve().parent/'image-runtime/.venv/Scripts/python.exe'
            try:
                if not prompt:raise ValueError('Image prompt missing')
                if len(prompt)>4000:raise ValueError('Image prompt exceeds 4,000 characters')
                if not python.is_file():raise RuntimeError('Local image generator is not installed')
                if target.exists():raise RuntimeError('An image for this request already exists; refusing to overwrite')
                from model_priority import ModelPriority
                log=self.engine.data/'image-generation.log'
                with ModelPriority(self.engine.data).background(cancel=self.stop.is_set), log.open('a',encoding='utf-8') as diagnostic:
                    proc=subprocess.Popen([str(python),str(runner),str(target)],stdin=subprocess.PIPE,stdout=diagnostic,stderr=diagnostic,text=True,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                    proc.stdin.write(json.dumps({'prompt':prompt}));proc.stdin.close()
                    deadline=time.monotonic()+900
                    while proc.poll() is None and not self.stop.wait(.25) and time.monotonic()<deadline:pass
                    if proc.poll() is None:
                        proc.terminate()
                        try:proc.wait(1)
                        except subprocess.TimeoutExpired:proc.kill();proc.wait()
                        raise RuntimeError('Generation interrupted' if self.stop.is_set() else 'Generation timed out')
                    if proc.returncode!=0 or not target.is_file():raise RuntimeError('Image generator failed; see local diagnostics')
                msg='Local image generated: '+str(target)
                state='image_complete'
            except (OSError,ValueError,RuntimeError,subprocess.TimeoutExpired) as e:
                state='image_interrupted' if self.stop.is_set() else 'image_failed'
                msg=('Local image generation interrupted: ' if self.stop.is_set() else 'Local image generation failed: ')+str(e)
            with store.lock,store.db:
                store.db.execute('update outbox set state=? where id=?',(state,identity))
                store.put({'id':'image:'+identity,'ts':time.time(),'from':'system','text':msg})
            store.queue_changed.set()

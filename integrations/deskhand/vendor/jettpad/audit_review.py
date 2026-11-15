"""Private, read-only diagnostic review. Never dispatches or retries actions."""
from contextlib import closing
from pathlib import Path
import sqlite3


def load_review(folder, limit=300):
    path=Path(folder)/'pad-state.sqlite3'
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=3)) as db:
        db.row_factory=sqlite3.Row
        rows=db.execute("""SELECT m.id,m.ts,m.text,o.route,o.state,o.requested_model,o.codex_model,
              o.codex_effort,r.text AS reply,a.text AS receipt
            FROM messages m JOIN outbox o ON o.id=m.id
            LEFT JOIN messages r ON r.id='offline:'||m.id
            LEFT JOIN messages a ON a.id='action-status:'||m.id
            WHERE m.sender='phil' ORDER BY m.ts DESC,m.rowid DESC LIMIT ?""",(limit,)).fetchall()
        result=[]
        for row in rows:
            item=dict(row);identity=item['id']
            item['failure_category']=next((r[0] for r in db.execute("SELECT content FROM audit_events WHERE message_id=? AND kind='failure' ORDER BY seq DESC LIMIT 1",(identity,))),None)
            item['actual_model']=next((r[0] for r in db.execute("SELECT codex_model FROM audit_events WHERE message_id=? AND kind='resolved_model' ORDER BY seq DESC LIMIT 1",(identity,))),None)
            item['capability']=next((r[0] for r in db.execute("SELECT content FROM audit_events WHERE message_id=? AND kind='capability' ORDER BY seq DESC LIMIT 1",(identity,))),None)
            events=list(db.execute("SELECT delivery,recorded_at FROM audit_events WHERE message_id=? AND kind='delivery' ORDER BY seq",(identity,)))
            times={}
            for state,ts in events:times.setdefault(state,ts)
            started=next((times[k] for k in ('codex_running','selected_running','offline_running','browser_sending') if k in times),None)
            queued=times.get('queued');completed=next((times[k] for k in ('codex_complete','codex_failed','selected_complete','selected_failed','offline_complete','offline_failed','browser_complete') if k in times),None)
            item['queue_seconds']=round(started-queued,2) if started is not None and queued is not None and started>=queued else None
            item['processing_seconds']=round(completed-started,2) if started is not None and completed is not None and completed>=started else None
            job_events=list(db.execute("SELECT content,recorded_at FROM audit_events WHERE message_id=? AND kind IN ('message','message_update') ORDER BY seq",('action-status:'+identity,)))
            first_job=next((r[1] for r in job_events if any(': '+x in (r[0] or '') for x in ('submitted','running'))),None)
            last_job=next((r[1] for r in reversed(job_events) if any(': '+x in (r[0] or '') for x in ('completed','failed','cancelled'))),None)
            item['job_observed_seconds']=round(last_job-first_job,2) if first_job is not None and last_job is not None and last_job>=first_job else None
            link=db.execute('SELECT reply_id FROM audit_reply_links WHERE prompt_id=? LIMIT 1',(identity,)).fetchone()
            if link:
                answer=db.execute("SELECT text FROM messages WHERE id=? AND sender='jett'",(link[0],)).fetchone()
                if answer:item['reply']=answer[0]
            result.append(item)
        return result


def open_review(parent, folder):
    import tkinter as tk
    from tkinter import ttk
    from musebridge.pad_theme import apply_theme
    window=tk.Toplevel(parent);apply_theme(window);window.title('Private assistant review');window.geometry('850x650');window.transient(parent)
    ttk.Label(window,text='Local records · a successful reply is not proof a task succeeded',wraplength=780).pack(anchor='w',padx=12,pady=8)
    bar=ttk.Frame(window);bar.pack(fill='x',padx=12)
    filter_var=tk.StringVar(value='Failures and interrupted')
    filters=ttk.Combobox(bar,textvariable=filter_var,state='readonly',values=('Failures and interrupted','All requests'),width=27);filters.pack(side='left')
    listing=tk.Listbox(window,height=9,exportselection=False);listing.pack(fill='x',padx=12,pady=8)
    detail=tk.Text(window,wrap='word',height=15);detail.pack(fill='both',expand=True,padx=12,pady=8)
    items=[]
    def render(_=None):
        listing.delete(0,'end');items.clear()
        try:
            for row in load_review(folder):
                if filter_var.get()!='All requests' and not any(x in (row['state'] or '') for x in ('failed','interrupted','uncertain','cancel')):continue
                items.append(row);listing.insert('end',str(row['state'])+' · '+str(row['actual_model'] or row['codex_model'] or row['requested_model'] or row['route'] or 'unknown')+' · '+str(row['failure_category'] or 'reason unknown'))
        except (OSError,sqlite3.Error):listing.insert('end','Local history unavailable; try again.')
    def select(_=None):
        chosen=listing.curselection()
        if not chosen or chosen[0]>=len(items):return
        r=items[chosen[0]];detail.delete('1.0','end')
        detail.insert('end',f"Request ID: {r['id']}\nDelivery: {r['state']}\nModel: {r['actual_model'] or r['codex_model'] or r['requested_model'] or 'unknown'}\nFailure: {r['failure_category'] or 'unknown / not recorded'}\nQueue seconds: {r['queue_seconds']}\nProcessing seconds: {r['processing_seconds']}\nObserved PC-job seconds: {r['job_observed_seconds']}\nCapability: {r['capability'] or 'unknown / not recorded'}\n\nPrompt:\n{r['text']}\n\nReply:\n{r['reply'] or '(not recorded)'}\n\nPC receipt:\n{r['receipt'] or '(none recorded; task outcome unknown)'}")
    def recheck():
        chosen=listing.curselection()
        if not chosen or chosen[0]>=len(items):return
        identity=items[chosen[0]]['id']
        import threading,queue
        q=queue.Queue()
        def work():
            try:
                from action_status import query_turn,format_status
                rows=query_turn(folder,identity);q.put(format_status(rows) or 'No existing PC job recorded. No action was started.')
            except Exception:q.put('Could not check existing jobs. Outcome remains unknown. No action was started.')
        def done():
            if not window.winfo_exists():return
            try:message=q.get_nowait()
            except queue.Empty:window.after(100,done);return
            detail.insert('end','\n\nCurrent job check:\n'+message)
        threading.Thread(target=work,daemon=True).start();window.after(100,done)
    ttk.Button(bar,text='Refresh',command=render).pack(side='left',padx=8)
    ttk.Button(bar,text='Check existing PC job',command=recheck).pack(side='left')
    listing.bind('<<ListboxSelect>>',select);filters.bind('<<ComboboxSelected>>',render);render()

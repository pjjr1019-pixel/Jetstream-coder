"""Local JettPad Live command/search surface. All external requests stay drafts."""
import json
import tkinter as tk
from tkinter import ttk
import uuid

from musebridge.pad_theme import BASE,PANEL,TEXT,GOLD,MUTED
from ui_components import motion_enabled
from workspace import fuzzy_score,suggested_tags


class Registry:
    def __init__(self):self.commands={}
    def register(self,identity,label,callback,keywords=''):
        self.commands[identity]={'id':identity,'label':label,'callback':callback,'keywords':keywords}
    def search(self,query):
        values=[]
        for command in self.commands.values():
            score=max(fuzzy_score(query,command['label']),fuzzy_score(query,command['keywords']))
            if score:values.append((score,command))
        return [c for _,c in sorted(values,key=lambda value:value[0],reverse=True)][:8]


class CommandBar:
    def __init__(self,owner):
        self.owner=owner;self.workspace=owner.workspace;self.registry=owner.commands
        self.window=tk.Toplevel(owner.root);self.window.title('JettPad Live · Search and commands')
        self.window.geometry('680x520');self.window.minsize(460,360);self.window.configure(bg=BASE)
        self.query=tk.StringVar();self.kind=tk.StringVar(value='all');self.sender=tk.StringVar(value='all')
        self.generation=0;self.future=None;self.timer=None;self.results=[];self.undo=None;self.closed=False
        header=ttk.Frame(self.window,padding=14);header.pack(fill='x')
        ttk.Label(header,text='JettPad Live',style='Title.TLabel').pack(side='left')
        ttk.Label(header,text='Local search · Ctrl+K',style='Muted.TLabel').pack(side='right')
        self.entry=ttk.Entry(self.window,textvariable=self.query,takefocus=True);self.entry.pack(fill='x',padx=14,pady=(0,8))
        filters=ttk.Frame(self.window,padding=(14,0,14,8));filters.pack(fill='x')
        for label,var,values in [('Show',self.kind,('all','pinned','attention')),('From',self.sender,('all','phil','jett','offline','system'))]:
            ttk.Label(filters,text=label).pack(side='left',padx=(0,5))
            box=ttk.Combobox(filters,textvariable=var,values=values,state='readonly',width=12,takefocus=True)
            box.pack(side='left',padx=(0,14));box.bind('<<ComboboxSelected>>',lambda event:self.schedule())
        self.tree=ttk.Treeview(self.window,columns=('detail',),show='tree headings',selectmode='browse',takefocus=True)
        self.tree.heading('#0',text='Command or message');self.tree.heading('detail',text='Context')
        self.tree.column('#0',width=390);self.tree.column('detail',width=170);self.tree.pack(fill='both',expand=True,padx=14)
        self.tree.bind('<Return>',self.activate);self.tree.bind('<Double-1>',self.activate)
        buttons=ttk.Frame(self.window,padding=14);buttons.pack(fill='x')
        ttk.Button(buttons,text='Open',command=self.activate).pack(side='left')
        ttk.Button(buttons,text='Pin / tags…',command=self.organize).pack(side='left',padx=8)
        self.undo_button=ttk.Button(buttons,text='Undo organization',command=self.undo_last,state='disabled');self.undo_button.pack(side='left')
        ttk.Button(buttons,text='Close',command=self.close).pack(side='right')
        self.status=tk.StringVar(value='Search saved messages or choose a command. Nothing is sent automatically.')
        ttk.Label(self.window,textvariable=self.status,wraplength=620,padding=(14,0,14,12)).pack(fill='x')
        self.query.trace_add('write',lambda *_:self.schedule())
        self.recents=self.workspace.jobs.submit(self.workspace.recent_queries)
        self.entry.bind('<Down>',self.focus_results);self.entry.bind('<Return>',self.activate)
        self.entry.bind('<Control-k>',lambda event:self.focus())
        self.window.bind('<Escape>',lambda event:self.close());self.window.protocol('WM_DELETE_WINDOW',self.close)
        self.window.bind('<Control-k>',lambda event:self.focus())
        self.schedule();self.focus()
        if motion_enabled():
            try:self.window.attributes('-alpha',0);self._fade(0)
            except tk.TclError:pass

    def _fade(self,step):
        if self.closed:return
        self.window.attributes('-alpha',step/4)
        if step<4:self.window.after(40,lambda:self._fade(step+1))
    def focus(self):self.window.deiconify();self.window.lift();self.entry.focus_set();return 'break'
    def focus_results(self,event=None):
        if self.tree.get_children():self.tree.focus_set();self.tree.selection_set(self.tree.get_children()[0])
        return 'break'
    def schedule(self):
        self.generation+=1
        if self.timer:self.window.after_cancel(self.timer)
        self.timer=self.window.after(120,self.search)
    def search(self):
        self.timer=None;generation=self.generation;query=self.query.get().strip()
        self.results=[{'type':'command',**command} for command in self.registry.search(query)] if self.kind.get()=='all' and self.sender.get()=='all' else []
        self.render();self.status.set('Searching saved messages on this device…')
        if self.future:self.future.cancel()
        self.future=self.workspace.search(query,kind=self.kind.get(),sender=self.sender.get(),cancelled=lambda:self.closed or generation!=self.generation)
        self.poll(self.future,generation)
    def poll(self,future,generation):
        if self.closed or generation!=self.generation:return
        if not future.done():self.window.after(30,lambda:self.poll(future,generation));return
        try:rows=future.result()
        except Exception:self.status.set('Search is unavailable; saved messages and normal chat are preserved.');return
        if not self.query.get().strip() and self.recents.done():
            try:self.results.extend({'type':'recent','query':q} for q in self.recents.result()[:5])
            except Exception:pass
        self.results.extend({'type':'message',**row} for row in rows);self.render()
        indexing=' · Indexing older history' if not self.workspace.ready and not self.workspace.error else ''
        self.status.set((f'{len(rows)} messages · Enter opens · Pin/tags is reviewable'+indexing) if self.results else 'No results. Try fewer words or another filter.')
    def render(self):
        self.tree.delete(*self.tree.get_children())
        for index,row in enumerate(self.results):
            if row['type']=='command':label='› '+row['label'];detail='Application command'
            elif row['type']=='recent':label=row['query'];detail='Recent command/search'
            else:
                label=('★ ' if row['pinned'] else '')+row['preview'];detail=row['sender']+(' · '+', '.join(row['tags']) if row['tags'] else '')
            self.tree.insert('', 'end',iid=str(index),text=label,values=(detail,))
        if self.results:self.tree.selection_set('0')
    def selected(self):
        values=self.tree.selection()
        return self.results[int(values[0])] if values else None
    def activate(self,event=None):
        row=self.selected()
        if not row:return 'break'
        if row['type']=='recent':self.query.set(row['query'].lstrip('>'));return 'break'
        if row['type']=='command':
            self.workspace.jobs.submit(self.workspace.recent_queries,'>'+row['label'])
            row['callback']()
            if row['id'] not in ('pinned','attention'):self.close();return 'break'
        else:self.owner.open_message(row,self.query.get().strip())
        query=self.query.get().strip()
        if query:self.workspace.jobs.submit(self.workspace.recent_queries,query)
        return 'break'
    def organize(self):
        row=self.selected()
        if not row or row['type']!='message':self.status.set('Choose a saved message first.');return
        dialog=tk.Toplevel(self.window);dialog.title('Review message organization');dialog.configure(bg=BASE)
        pin=tk.BooleanVar(value=row['pinned']);tags=tk.StringVar(value=', '.join(row['tags']))
        ttk.Label(dialog,text='Local organization · original message stays unchanged',padding=12).pack()
        ttk.Checkbutton(dialog,text='Pin this message',variable=pin).pack(anchor='w',padx=12)
        ttk.Label(dialog,text='Tags, separated by commas',padding=(12,10,12,3)).pack(anchor='w')
        entry=ttk.Entry(dialog,textvariable=tags,width=48);entry.pack(padx=12,pady=5)
        hints=suggested_tags(row['text'])
        if hints:ttk.Label(dialog,text='Local suggestions: '+', '.join(hints),padding=12,wraplength=400).pack()
        def save():
            values=[value.strip() for value in tags.get().split(',') if value.strip()]
            future=self.workspace.organize(row['id'],pin.get(),values,row['meta_version'])
            dialog.destroy();self._mutation(future)
        ttk.Button(dialog,text='Save organization',command=save).pack(pady=12)
        ttk.Button(dialog,text='Cancel',command=dialog.destroy).pack(pady=(0,10));entry.focus_set()
    def _mutation(self,future,undoing=False):
        if self.closed:return
        if not future.done():self.window.after(30,lambda:self._mutation(future,undoing));return
        try:
            result=future.result();self.undo=None if undoing else result
            self.undo_button.configure(state='disabled' if undoing else 'normal')
            self.status.set('Previous organization restored.' if undoing else 'Organization saved locally. Undo is available.')
        except Exception as error:self.status.set(str(error));return
        self.schedule()
    def undo_last(self):
        if not self.undo:return
        receipt=self.undo;before=receipt['before'];after=receipt['after']
        self._mutation(self.workspace.organize(receipt['id'],before['pinned'],before['tags'],after['version']),True)
    def close(self):
        self.closed=True;self.generation+=1
        if self.future:self.future.cancel()
        if self.timer:self.window.after_cancel(self.timer)
        self.owner.palette=None;self.window.destroy()

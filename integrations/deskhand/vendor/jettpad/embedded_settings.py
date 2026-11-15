"""Staged local-only Jett Pad setup for the embedded phone connection."""
import os
from pathlib import Path
import threading
from urllib.parse import urlsplit
import webbrowser
import tkinter as tk
from tkinter import ttk, messagebox


def add_settings(tab,parent):
    from musebridge.lifecycle import local_request
    home=Path(os.environ['LOCALAPPDATA'])/'MuseBridge'
    frame=ttk.LabelFrame(tab,text='Built-in private phone connection',padding=10)
    frame.pack(fill='x',pady=8)
    ttk.Label(frame,text='On your phone, open Jett Settings → Set up built-in connection. '
              'Enter the phone identity and address shown there.',wraplength=600).pack(anchor='w')
    node=tk.StringVar();address=tk.StringVar();fingerprint=tk.StringVar();status=tk.StringVar(value='Not connected')
    login={'url':None};busy={'poll':False}
    for title,variable in (('Phone identity',node),('Phone private address',address)):
        ttk.Label(frame,text=title).pack(anchor='w')
        ttk.Entry(frame,textvariable=variable).pack(fill='x')
    ttk.Label(frame,text='Existing paired phone').pack(anchor='w')
    chooser=ttk.Combobox(frame,textvariable=fingerprint,state='readonly');chooser.pack(fill='x')
    ttk.Label(frame,textvariable=status,wraplength=600).pack(anchor='w',pady=5)

    def local(action,payload=None):
        result=local_request(home,action,payload,timeout=8)
        if not isinstance(result,dict) or result.get('ok') is not True or not isinstance(result.get('result'),dict):
            raise RuntimeError('PC connection service unavailable')
        return result['result']

    def background(work,done):
        def run():
            try:value=work();error=None
            except Exception as exc:value=None;error=str(exc)
            try:parent.after(0,lambda:done(value,error))
            except RuntimeError:pass
        threading.Thread(target=run,daemon=True,name='Jett built-in connection setup').start()

    def options_done(value,error):
        if error:status.set(error);return
        choices=value.get('paired_fingerprints',[])
        chooser.configure(values=choices)
        if len(choices)==1:fingerprint.set(choices[0])
    background(lambda:local('embedded_options_local'),options_done)

    def begin():
        binding={'version':1,'node':node.get().strip(),'address':address.get().strip(),'fingerprint':fingerprint.get()}
        if not messagebox.askyesno('Connect paired phone?',
            'Connect this existing paired phone to Jett’s built-in private network? '
            'Streaming stays off until you start it.',parent=parent):return
        status.set('Starting private connection…')
        background(lambda:local('embedded_begin_local',{'binding':binding,'local_approval':True}),
                   lambda value,error:status.set(error or 'Starting private connection…'))

    def open_login():
        url=login['url'];parts=urlsplit(url or '')
        if parts.scheme!='https' or parts.hostname!='login.tailscale.com' or parts.username or parts.password or parts.query or parts.fragment:
            status.set('Sign-in is not available.');return
        webbrowser.open(url)

    def stop():
        background(lambda:local('embedded_stop_local'),lambda value,error:status.set(error or 'Private connection stopped'))

    ttk.Button(frame,text='Connect paired phone',command=begin).pack(anchor='w',pady=3)
    ttk.Button(frame,text='Open PC sign-in',command=open_login).pack(anchor='w',pady=3)
    ttk.Button(frame,text='Stop built-in connection',command=stop).pack(anchor='w',pady=3)

    def update(value,error):
        busy['poll']=False
        if not frame.winfo_exists():return
        if error:status.set(error)
        else:
            login['url']=value.get('sign_in_url')
            state=value.get('state','unavailable');pc=value.get('address')
            status.set(('PC private address: '+pc+' · ' if pc else '')+state.replace('_',' '))
        frame.after(2000,refresh)
    def refresh():
        if not frame.winfo_exists() or busy['poll']:return
        busy['poll']=True;background(lambda:local('embedded_status_local'),update)
    frame.after(100,refresh)
    return frame

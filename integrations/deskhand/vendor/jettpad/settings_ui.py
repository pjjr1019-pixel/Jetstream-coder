"""Native PC connection/access settings inside Jett Pad."""
import json
from copy import deepcopy
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from bridge_control import merged_features, required_confirmations
from musebridge.pad_theme import apply_theme,bind_status,button_style
from ui_components import CategoryBook,FlowButtons,help_label,page_help


class SettingsWindow:
    def __init__(self, parent, control, browser=None,services=None,on_quit=None):
        self.control,self.state=control,{}
        self.window=tk.Toplevel(parent)
        apply_theme(self.window)
        self._timers=set()
        self.window.bind('<Destroy>',self.destroyed,add='+')
        self.window.title('Jett Pad Settings')
        self.window.geometry('780x700'); self.window.minsize(740,600)
        self.window.transient(parent)
        self.notice=tk.StringVar(value='Loading PC controls...')
        notice_label=ttk.Label(self.window,textvariable=self.notice,wraplength=600,padding=12)
        notice_label.pack(fill='x');bind_status(notice_label,self.notice)
        book=CategoryBook(self.window);self.book=book;book.pack(fill='both',expand=True,padx=16,pady=8)
        from companion_service import add_settings
        add_settings(book,self.window,services)
        self.tabs={}
        for title in ('Connection','Folders','Windows control','Access','Diagnostics'):
            tab=ttk.Frame(book,padding=14); book.add(tab,text=title); self.tabs[title]=tab
        if browser:
            muse=ttk.Frame(book,padding=14); book.insert(0,muse,text='Muse')
            ttk.Label(muse,text='Connection status',style='Field.TLabel').pack(anchor='w',pady=(12,0))
            browser_status=tk.StringVar()
            browser_label=ttk.Label(muse,textvariable=browser_status,wraplength=560)
            browser_label.pack(anchor='w',pady=12);bind_status(browser_label,browser_status)
            ttk.Label(muse,text='Normal replies use an invisible private Edge session.\nOnly Sign in to Muse opens a browser window.').pack(anchor='w',pady=12)
            self.buttons(muse,('Sign in to Muse',browser.sign_in),('Done signing in',browser.finish_sign_in))
            ttk.Label(muse,text='Advanced · uncertain delivery',style='Muted.TLabel').pack(anchor='w',pady=(16,0))
            def leave_uncertain():
                if messagebox.askyesno('Leave delivery unconfirmed',
                    'Check the last message in Muse first. Leave this request unconfirmed without resending it?',parent=self.window):
                    browser.leave_uncertain()
            self.buttons(muse,('Leave unconfirmed send',leave_uncertain))
            def refresh_browser():
                if self.window.winfo_exists():
                    browser_status.set(browser.status()); self.later(200,refresh_browser)
            refresh_browser()
        codex_tab=ttk.Frame(book,padding=14);book.add(codex_tab,text='Codex accounts')
        ttk.Label(codex_tab,text='Codex accounts',style='Field.TLabel').pack(anchor='w',pady=(8,4))
        ttk.Label(codex_tab,text='The current Codex sign-in remains available. Add separate accounts here; each opens a sign-in window when you choose Sign in. Your Pad conversation is shared across accounts.',wraplength=550).pack(anchor='w',pady=8)
        codex_list=tk.Listbox(codex_tab,height=5,exportselection=False);codex_list.pack(fill='x',pady=8)
        def refresh_codex_accounts():
            from codex_accounts import list_accounts
            codex_list.delete(0,'end')
            for account in list_accounts():codex_list.insert('end',account['name'])
        def add_codex_account():
            from codex_accounts import add_account
            name=simpledialog.askstring('Add Codex account','Give this account a name:',parent=self.window)
            if name is not None:
                try:add_account(name);refresh_codex_accounts();self.notice.set('Account added. Select it and choose Sign in.')
                except (ValueError,OSError) as error:messagebox.showerror('Codex account',str(error),parent=self.window)
        def sign_in_codex_account():
            from codex_accounts import list_accounts,login
            chosen=codex_list.curselection();accounts=list_accounts()
            if not chosen or chosen[0]>=len(accounts):self.notice.set('Select an account to sign in.');return
            try:login(accounts[chosen[0]]['id']);self.notice.set('Follow the sign-in instructions in the opened window.')
            except (ValueError,OSError,RuntimeError) as error:messagebox.showerror('Codex sign-in',str(error),parent=self.window)
        self.buttons(codex_tab,('Add account',add_codex_account),('Sign in',sign_in_codex_account));refresh_codex_accounts()
        voice=ttk.Frame(book,padding=14);book.add(voice,text='Voice')
        from pathlib import Path
        import os
        voice_root=Path(os.environ['LOCALAPPDATA'])/'JettVoice'
        voice_settings=voice_root/'settings.json'
        voice_muted=tk.BooleanVar(value=True)
        voice_status=tk.StringVar(value='Microphone and speech start muted.')
        voice_label=ttk.Label(voice,textvariable=voice_status,wraplength=560)
        voice_label.pack(anchor='w',pady=8);bind_status(voice_label,voice_status)
        def save_voice():
            from jettvoice.common import atomic,load_settings
            values=load_settings(voice_settings)
            values['muted']=voice_muted.get()
            atomic(voice_settings,values)
        ttk.Checkbutton(voice,text='Mute microphone and speech (Ctrl+Alt+Shift+M)',
                        variable=voice_muted,command=save_voice).pack(anchor='w',pady=12)
        voice_listening=tk.BooleanVar(value=True)
        def save_listening():
            from jettvoice.common import atomic,load_settings
            values=load_settings(voice_settings);values['listening_enabled']=voice_listening.get()
            atomic(voice_settings,values)
        ttk.Checkbutton(voice,text='Enable “Hey Jett” microphone when unmuted',
                        variable=voice_listening,command=save_listening).pack(anchor='w',pady=8)
        ttk.Label(voice,text='When unmuted: say “Hey Jett”, pause briefly, then speak.\n'
            'Wake detection and transcription stay on this PC. Only text goes to Muse.\n'
            'Speech uses Jett’s authenticated audio uploads; no local substitute voice.',wraplength=560).pack(anchor='w',pady=8)
        def refresh_voice():
            try:
                from jettvoice.common import load_settings
                values=load_settings(voice_settings);voice_muted.set(values['muted'])
                voice_listening.set(values['listening_enabled'])
                runtime=json.loads((voice_root/'runtime.json').read_text())
                voice_status.set(runtime.get('status','Audio status unavailable'))
            except (OSError,ValueError):
                voice_status.set('Audio service not running; microphone status unavailable.')
            if self.window.winfo_exists():self.later(500,refresh_voice)
        refresh_voice()
        if services:
            orb=ttk.Frame(book,padding=14);book.add(orb,text='Orb')
            ttk.Label(orb,text='The orb and chat belong to this application.\nDouble-click the orb to open chat; right-click for these settings.',wraplength=560).pack(anchor='w',pady=10)
            ttk.Label(orb,text='Look',style='Field.TLabel').pack(anchor='w')
            look=tk.StringVar(value='Ember Wisp' if services.orb.state().get('look')=='ember_wisp' else 'Jet')
            picker=ttk.Combobox(orb,textvariable=look,values=('Jet','Ember Wisp'),state='readonly')
            picker.pack(fill='x',pady=(0,8))
            picker.bind('<<ComboboxSelected>>',lambda event:services.configure_orb('look','ember_wisp' if look.get()=='Ember Wisp' else 'jet'))
            wisp_group=ttk.LabelFrame(orb,text='Ember Wisp',padding=10)
            wisp_group.pack(fill='x',pady=(0,10))
            wisp_values=services.orb.state()
            size=tk.StringVar(value=wisp_values.get('wisp_size','medium').title())
            ttk.Label(wisp_group,text='Flame size',style='Field.TLabel').pack(anchor='w')
            size_picker=ttk.Combobox(wisp_group,textvariable=size,values=('Small','Medium','Large'),state='readonly')
            size_picker.pack(fill='x',pady=(0,8))
            size_picker.bind('<<ComboboxSelected>>',lambda event:services.configure_orb('wisp_size',size.get().lower()))
            brightness=tk.DoubleVar(value=wisp_values.get('wisp_brightness',1.))
            ttk.Label(wisp_group,text='Flame brightness',style='Field.TLabel').pack(anchor='w')
            def save_brightness(event=None):services.configure_orb('wisp_brightness',round(brightness.get(),2))
            brightness_scale=ttk.Scale(wisp_group,from_=.5,to=1.5,variable=brightness)
            brightness_scale.pack(fill='x',pady=(0,8))
            brightness_scale.bind('<ButtonPress-1>',lambda event:brightness_scale.focus_set())
            brightness_scale.bind('<ButtonRelease-1>',save_brightness)
            brightness_scale.bind('<KeyRelease>',save_brightness)
            coverage=tk.DoubleVar(value=wisp_values.get('wisp_coverage',.8))
            ttk.Label(wisp_group,text='Flame coverage / contrast',style='Field.TLabel').pack(anchor='w')
            coverage_scale=ttk.Scale(wisp_group,from_=0,to=1,variable=coverage)
            coverage_scale.pack(fill='x',pady=(0,8))
            def save_coverage(event=None):services.configure_orb('wisp_coverage',round(coverage.get(),2))
            coverage_scale.bind('<ButtonPress-1>',lambda event:coverage_scale.focus_set())
            coverage_scale.bind('<ButtonRelease-1>',save_coverage);coverage_scale.bind('<KeyRelease>',save_coverage)
            ttk.Label(wisp_group,text='Higher coverage helps light or busy backgrounds. Glow stays separate; no desktop sampling.',wraplength=510,style='Muted.TLabel').pack(anchor='w',pady=(0,8))
            contrast=tk.BooleanVar(value=wisp_values.get('wisp_contrast',True))
            ttk.Checkbutton(wisp_group,text='Soft surrounding glow',variable=contrast,
                command=lambda:services.configure_orb('wisp_contrast',contrast.get())).pack(anchor='w')
            ttk.Label(wisp_group,text='Wisp only. A soft warm glow surrounds the flame; no border or desktop capture. Preview effects includes movement, hover and PC work, seven seconds each.',wraplength=510,style='Muted.TLabel').pack(anchor='w',pady=(8,0))
            appearance={}
            for key,label in (('roaming','Free movement'),('work','Subtle / work mode'),
                              ('reduced','Reduce motion'),('paused','Pause animation and movement'),('labels','Show activity label'),('quick_chat','Show orb chat'),('captions','Transcript captions')):
                var=tk.BooleanVar(value=services.orb.state()[key]);appearance[key]=var
                ttk.Checkbutton(orb,text=label,variable=var,
                    command=lambda k=key,v=var:services.configure_orb(k,v.get())).pack(anchor='w',pady=7)
            self.buttons(orb,('Show orb',services.start_orb),('Preview effects',services.preview),
                         ('Use position as home',lambda:services.orb.request('save_home')))
            ttk.Label(orb,text='Orb chat shares this conversation. Its visible messages may be seen by others and included in desktop streaming. STOP hides the activity label, but does not hide reply cards. Fullscreen hides automatic orb chat.',wraplength=560,style='Muted.TLabel').pack(anchor='w',pady=12)
            ttk.Label(orb,text='Preview effects are clearly labelled simulations.\nCtrl+Alt+Shift+H hides only the orb. Quit Jett closes the full application.',wraplength=560).pack(anchor='w',pady=16)
            def refresh_orb():
                values=services.orb.state()
                for key,var in appearance.items():var.set(values[key])
                if picker.focus_get()!=picker:look.set('Ember Wisp' if values.get('look')=='ember_wisp' else 'Jet')
                if size_picker.focus_get()!=size_picker:size.set(values.get('wisp_size','medium').title())
                if brightness_scale.focus_get()!=brightness_scale and not brightness_scale.instate(['pressed']):brightness.set(values.get('wisp_brightness',1.))
                contrast.set(values.get('wisp_contrast',True))
                if coverage_scale.focus_get()!=coverage_scale:coverage.set(values.get('wisp_coverage',.8))
                self.later(500,refresh_orb)
            refresh_orb()
        self.bind=tk.StringVar(); self.port=tk.StringVar()
        ttk.Label(self.tabs['Connection'],text='Address and port',style='Field.TLabel').pack(anchor='w',pady=(12,0))
        self.field(self.tabs['Connection'],'This PC\u2019s Tailscale IP',self.bind)
        self.field(self.tabs['Connection'],'Bridge port',self.port)
        self.buttons(self.tabs['Connection'],('Save connection',self.save_connection),
                     ('Start',lambda:self.action('start')),('Stop',lambda:self.action('stop')))
        self.buttons(self.tabs['Connection'],('Reconnect',lambda:self.action('reconnect')),
                     ('Check network',self.ping),('Copy connection details',self.copy_details))
        ttk.Label(self.tabs['Connection'],text='Muse stays signed in through Edge. Messages and answers use\n'
                  'the browser + file-action workflow, not another chat worker.',wraplength=570).pack(anchor='w',pady=14)
        folders=self.tabs['Folders']
        ttk.Label(folders,text='Approved folders',style='Field.TLabel').pack(anchor='w',pady=(12,8))
        self.scopes=ttk.Treeview(folders,columns=('path','access'),height=6,selectmode='browse')
        self.scopes.heading('#0',text='Name'); self.scopes.column('#0',width=90)
        self.scopes.heading('path',text='Folder'); self.scopes.column('path',width=300)
        self.scopes.heading('access',text='Access'); self.scopes.column('access',width=125)
        self.scopes.pack(fill='both',expand=True)
        horizontal=ttk.Scrollbar(folders,orient='horizontal',command=self.scopes.xview)
        horizontal.pack(fill='x');self.scopes.configure(xscrollcommand=horizontal.set)
        self.scopes.bind('<<TreeviewSelect>>',self.select_scope)
        self.scope_name=tk.StringVar(); self.scope_path=tk.StringVar()
        ttk.Label(folders,text='Selected folder',style='Field.TLabel').pack(anchor='w',pady=(16,0))
        self.field(folders,'Name',self.scope_name); self.field(folders,'Folder',self.scope_path)
        self.read=tk.BooleanVar(); self.write=tk.BooleanVar(); self.execute=tk.BooleanVar()
        row=ttk.Frame(folders); row.pack(fill='x')
        for label,var in (('Read',self.read),('Write',self.write),('Run commands',self.execute)):
            ttk.Checkbutton(row,text=label,variable=var).pack(side='left',padx=6)
        self.buttons(folders,('Browse',self.browse),('New',self.new_scope),
                     ('Save folder',self.save_scope),('Remove',self.remove_scope))
        windows=self.tabs['Windows control']
        self.apps=tk.StringVar(); self.screenshots=tk.BooleanVar(); self.desktop=tk.BooleanVar(); self.exec_enabled=tk.BooleanVar()
        ttk.Label(windows,text='Local approvals and control',style='Field.TLabel').pack(anchor='w',pady=(12,0))
        self.field(windows,'Approved applications (comma separated)',self.apps)
        for label,var in (('Allow images of ordinary app windows',self.screenshots),
                          ('Allow mouse, keyboard and accessible app controls',self.desktop),
                          ('Allow commands in approved execution folders',self.exec_enabled)):
            ttk.Checkbutton(windows,text=label,variable=var).pack(anchor='w',pady=7)
        self.buttons(windows,('Save Windows access',self.save_windows),
                     ('Allow control',lambda:self.action('desktop_control',{'enabled':True})),
                     ('Clear STOP',lambda:self.action('clear_stop')),
                     ('Take control back',lambda:self.action('desktop_control',{'enabled':False})))
        page_help(windows,'pin')
        self.buttons(windows,('Set remote-control PIN locally',self.set_remote_pin),
                     ('Revoke 24-hour authorization',lambda:self.action('revoke_day_authorization')))
        self.day_authorization=tk.StringVar(value='24-hour authorization: checking…')
        ttk.Label(windows,textvariable=self.day_authorization,wraplength=570).pack(anchor='w',pady=6)
        ttk.Label(windows,text='PIN unlock affects only approved see/desktop access, for at most 30 minutes.\n'
                  'One PIN unlock authorizes new sessions for 24 hours from that first unlock.\n'
                  'Lock/disconnect end the session. STOP/revoke end both. Set the PIN only here.',wraplength=570).pack(anchor='w',pady=6)
        ttk.Label(windows,text='Protected windows stay blocked. Local input during control latches STOP.\nClear STOP locally or use the locally-set remote PIN. Emergency stop: Ctrl+Alt+Shift+Esc.',
                  wraplength=570).pack(anchor='w',pady=20)
        access=self.tabs['Access']
        self.tier=tk.StringVar(); self.profile=tk.StringVar(); self.minutes=tk.StringVar(value='60')
        self.combo(access,'PC access',self.tier,('Off','Approved folders','Whole PC'))
        self.buttons(access,('Save PC access',lambda:self.configure({'tier':{'Off':0,'Approved folders':1,'Whole PC':2}[self.tier.get()]})))
        ttk.Label(access,text='Advanced · temporary restrictions',style='Field.TLabel').pack(anchor='w',pady=(16,0))
        self.combo(access,'Timed access restriction',self.profile,('Configured permissions','Read files only','Read and edit files','All configured assistance'))
        self.combo(access,'Minutes',self.minutes,('15','60','240'))
        self.buttons(access,('Apply timed access',self.apply_session),('Stop all Muse access',lambda:self.action('stop_all')))
        ttk.Label(access,text='Restrictions do not add new grants. Existing permissions and app approvals\n'
                  'remain authoritative.',wraplength=570).pack(anchor='w',pady=18)
        diagnostics=self.tabs['Diagnostics']
        self.details=tk.StringVar()
        ttk.Label(diagnostics,textvariable=self.details,wraplength=560,justify='left').pack(anchor='w',fill='x')
        self.buttons(diagnostics,('Refresh',self.refresh),('Restart background service',self.restart_service),
                     ('Save activity log',self.export_audit),
                     ('Review assistant failures',lambda:__import__('audit_review').open_review(self.window,services.data if services else __import__('pathlib').Path(__import__('os').environ['LOCALAPPDATA'])/'JettPad'))) 
        self.buttons(self.window,*([('Close',self.window.destroy),('Quit Jett',on_quit)] if on_quit else [('Close',self.window.destroy)]))
        book.finalize()
        self.refresh()
        self.poll_day_authorization()

    def set_remote_pin(self):
        from pathlib import Path
        import subprocess
        python=Path(__file__).resolve().parent/'backend/.venv/Scripts/pythonw.exe'
        subprocess.Popen([str(python),'-m','musebridge.pin_setup'],cwd=python.parents[3],
                         creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))

    def later(self,milliseconds,callback):
        def invoke():
            self._timers.discard(timer)
            if self.window.winfo_exists(): callback()
        timer=self.window.after(milliseconds,invoke)
        self._timers.add(timer)

    def destroyed(self,event):
        if event.widget is self.window:
            for timer in self._timers: self.window.after_cancel(timer)
            self._timers.clear()

    @staticmethod
    def field(parent,label,var):
        key='folders' if label=='Folder' else 'bridge' if label=='Bridge port' else 'stop' if label.startswith('Approved applications') else None
        help_label(parent,label,key)
        ttk.Entry(parent,textvariable=var).pack(fill='x')

    @staticmethod
    def combo(parent,label,var,values):
        help_label(parent,label,'tier' if label=='PC access' else 'session')
        ttk.Combobox(parent,textvariable=var,values=values,state='readonly').pack(fill='x')

    @staticmethod
    def buttons(parent,*items):
        return FlowButtons(parent,items,button_style)

    def run(self,function,done=None):
        self.notice.set('Working...')
        future=self.control.submit(function)
        def finish():
            if not self.window.winfo_exists(): return
            if not future.done(): self.later(80,finish); return
            try:
                result=future.result()
                if done: done(result)
                else: self.refresh()
            except Exception as exc: self.notice.set(str(exc)[:240])
        self.later(80,finish)

    def refresh(self): self.run(self.control.ensure_running,self.populate)

    def populate(self,state):
        self.state=state
        self.bind.set(state.get('bind','')); self.port.set(str(state.get('port',8765)))
        self.notice.set('Muse is connected' if state.get('connected') else 'Waiting for Muse' if state.get('enabled') else 'Connection is stopped')
        self.scopes.delete(*self.scopes.get_children())
        for name,scope in state.get('scopes',{}).items():
            self.scopes.insert('', 'end',iid=name,text=name,values=(scope['path'],', '.join(scope.get('permissions',[]))))
        features=state.get('features',{})
        self.apps.set(', '.join(state.get('desktop_apps',[])))
        self.screenshots.set(features.get('screenshots',False)); self.desktop.set(features.get('desktop_control',False))
        self.exec_enabled.set(features.get('exec',False))
        self.tier.set({0:'Off',1:'Approved folders',2:'Whole PC'}.get(state.get('tier',1)))
        profiles={None:'Configured permissions','files_read':'Read files only','files_edit':'Read and edit files','assistant':'All configured assistance'}
        self.profile.set(profiles.get(state.get('session',{}).get('profile'),'Configured permissions'))
        diagnostics=state.get('diagnostics',{}); heartbeat=diagnostics.get('heartbeat',{})
        self.show_day_authorization(state)
        self.details.set('Connection: '+self.notice.get()+'\n\nDesktop approval: '+('allowed' if features.get('desktop_control') else 'off')+
            '\nControl session: '+('active' if state.get('desktop_control_enabled') else 'inactive')+
            '\nSTOP: '+('latched — clear it locally to resume' if state.get('stop_latched') else 'clear')+
            '\nApproved folders: '+str(len(state.get('scopes',{})))+
            '\nRecent disconnects: '+str(len(diagnostics.get('recent_disconnects',[])))+
            '\nNetwork round trip: '+str(heartbeat.get('native_round_trip_ms','not checked'))+' ms'+
            '\n\nMessages use Jett Pad\u2019s browser connection. Replies arrive as matched PC files.')

    def show_day_authorization(self,state):
        expiry=state.get('day_authorization_expires_at')
        if state.get('day_authorized') and expiry:
            from datetime import datetime
            self.day_authorization.set('24-hour authorization: active until '+datetime.fromtimestamp(expiry).astimezone().strftime('%b %d, %H:%M %Z'))
        else:self.day_authorization.set('24-hour authorization: inactive — a PIN unlock is required')
    def poll_day_authorization(self):
        if not self.window.winfo_exists():return
        try:
            import time
            from musebridge.cli import default_home
            state=json.loads((default_home()/'day-authorization-status.json').read_text(encoding='utf-8'))
            if state.get('day_authorization_expires_at') and state['day_authorization_expires_at']<=time.time():
                state={'day_authorized':False,'day_authorization_expires_at':None}
            self.show_day_authorization(state)
        except Exception:self.day_authorization.set('24-hour authorization: status unavailable')
        self.later(500,self.poll_day_authorization)

    def configure(self,changes):
        if not self.state: self.notice.set('Wait for settings to load.'); return
        confirmations={}; top={}
        for key,phrase in required_confirmations(self.state,changes).items():
            typed=simpledialog.askstring('Confirm access','Type '+phrase+' to allow this change.',parent=self.window)
            if typed!=phrase: self.notice.set('Access was not changed.'); return
            (top if key=='confirmation' else confirmations)[key]=typed
        self.action('configure',{**changes,**top,'confirmations':confirmations})

    def action(self,name,payload=None): self.run(lambda:self.control.action(name,payload))

    def save_connection(self):
        try: port=int(self.port.get())
        except ValueError: self.notice.set('Enter a port number.'); return
        self.configure({'bind':self.bind.get().strip(),'port':port})

    def ping(self):
        self.run(lambda:self.control.action('ping_muse'),lambda value:self.notice.set('Network answered in '+str(round(value['round_trip_ms'],1))+' ms.'))

    def copy_details(self):
        def copied(value):
            self.window.clipboard_clear(); self.window.clipboard_append(json.dumps(value))
            self.notice.set('Private connection details copied. Paste them into Muse.')
        self.run(lambda:self.control.action('connection_details'),copied)

    def select_scope(self,event=None):
        selection=self.scopes.selection()
        if not selection: return
        name=selection[0]; scope=self.state.get('scopes',{}).get(name,{})
        self.scope_name.set(name); self.scope_path.set(scope.get('path',''))
        for permission,var in (('read',self.read),('write',self.write),('exec',self.execute)):
            var.set(permission in scope.get('permissions',[]))

    def new_scope(self):
        self.scopes.selection_remove(*self.scopes.selection())
        self.scope_name.set(''); self.scope_path.set(''); self.read.set(True); self.write.set(False); self.execute.set(False)

    def browse(self):
        path=filedialog.askdirectory(parent=self.window)
        if path: self.scope_path.set(path)

    def save_scope(self):
        name,path=self.scope_name.get().strip(),self.scope_path.get().strip()
        if not name or not path: self.notice.set('Enter a name and folder.'); return
        scopes=deepcopy(self.state.get('scopes',{}))
        permissions=[p for p,v in (('read',self.read),('write',self.write),('exec',self.execute)) if v.get()]
        scopes[name]={'path':path,'permissions':permissions}
        self.configure({'scopes':scopes})

    def remove_scope(self):
        name=self.scope_name.get()
        if name not in self.state.get('scopes',{}): return
        if messagebox.askyesno('Remove folder access','Remove access to '+name+'?',parent=self.window):
            scopes=deepcopy(self.state['scopes']); scopes.pop(name); self.configure({'scopes':scopes})

    def save_windows(self):
        apps=list(dict.fromkeys(value.strip() for value in self.apps.get().split(',') if value.strip()))
        features=merged_features(self.state,screenshots=self.screenshots.get(),desktop_control=self.desktop.get(),exec=self.exec_enabled.get())
        self.configure({'features':features,'desktop_apps':apps})

    def apply_session(self):
        profiles={'Configured permissions':None,'Read files only':'files_read','Read and edit files':'files_edit','All configured assistance':'assistant'}
        if self.profile.get() not in profiles:self.notice.set('Select a session profile.');return
        try:minutes=int(self.minutes.get())
        except ValueError:self.notice.set('Enter session minutes as a number.');return
        if minutes<=0:self.notice.set('Enter session minutes greater than zero.');return
        self.action('session',{'profile':profiles[self.profile.get()],'minutes':minutes})

    def restart_service(self):
        self.action('restart')

    def export_audit(self):
        path=filedialog.asksaveasfilename(parent=self.window,defaultextension='.json',filetypes=[('Activity log','*.json')])
        if not path: return
        def saved(value):
            from pathlib import Path
            Path(path).write_text(value['json'],encoding='utf-8'); self.notice.set('Activity log saved.')
        self.run(lambda:self.control.action('export_audit'),saved)

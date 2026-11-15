"""Presentation helpers only. No settings, network or action ownership."""
import ctypes,os,tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk
from musebridge.pad_theme import BASE,CARD,TEXT,GOLD

HELP={
 'folders':('Folder permissions','Read and Write apply only to this approved folder. Run commands enables the existing command execution grant inside approved execution folders; use it only for trusted projects.'),
 'tier':('PC access scope','Choose which existing access scope is available. Whole PC is broader than approved folders and keeps its existing confirmation requirement; this choice does not bypass protected files or STOP.'),
 'session':('Timed restrictions','Temporarily restrict the configured grants. Restrictions cannot create new permissions. The selected duration is enforced by the existing service.'),
 'pin':('PIN and 24-hour authorization','A successful PIN unlock creates a control session lasting at most 30 minutes. It also authorizes new sessions for 24 hours from the first unlock. Local STOP revokes access; protected windows stay blocked.'),
 'stop':('Local STOP','STOP interrupts control and streaming. A hard local STOP must be cleared on this PC; the phone cannot override it. Keep the visible control indicator open while using remote control.'),
 'phone':('Paired-phone streaming','Allows the already-paired phone to request streaming. Everything visible on the desktop is shared. Pairing, local STOP, network checks and existing control authorization still apply.'),
 'network':('Persistent network readiness','The existing verified setup uses exact Tailscale-only firewall rules and a pinned Sunshine build. Prepare is for missing or changed setup; it does not start sharing. Discovery verification requires a real diagnostic capture.'),
 'away':('Away access versus Away handoff','Away access keeps the PC awake on AC and recovers app services. The chat’s Away button instead hides the Pad and delegates the existing window-activation handoff. Neither bypasses Windows sign-in or grants control.'),
 'offline':('Offline actions','Allows only the existing locally approved offline action path. Local replies remain labelled separately from cloud Jett. Switching offline mode changes routing using the existing behavior; it does not expand permissions.'),
 'voice':('Local listening and speech','Mute controls microphone and speech together. When enabled and unmuted, wake detection and transcription run locally and only text goes to Muse. Speech playback still requires authenticated Jett uploads.'),
 'orb':('Orb presentation','Orb chat shares Pad history, with four reply previews and its own draft. STOP does not hide chat; fullscreen does. Show orb chat hides the panel immediately. Activity labels never show message text. Transcript captions are separate, optional message text. Effects preview is simulated; these controls grant no permissions.'),
 'muse':('Private Muse connection','The existing private browser stays hidden during normal chat. Sign in is the explicit visible-browser path. An uncertain send must be reviewed before resending to avoid a duplicate request.'),
 'bridge':('Bridge connection','The existing address and port connect PC capabilities over Tailscale. Changes can interrupt access. Chat continues to use its existing private-browser and reply-file route.'),
}

def connection_summary(value):
    lower=value.casefold()
    if lower.startswith('switching to offline'):return 'Switching to Offline Jett'
    if 'offline assistant unavailable' in lower:return 'Offline assistant unavailable'
    if lower.startswith('offline jett'):return 'Offline Jett · local replies'
    if lower.startswith('cloud jett'):return 'Cloud Jett · automatic routing'
    if any(w in lower for w in ('unavailable','unreachable','offline','failed','error','not ready','disconnected')):return 'Connection needs attention'
    if 'ready' in lower or 'connected' in lower:return 'Cloud Jett · connected'
    return value if len(value)<=64 else value[:61]+'…'

def motion_enabled():
    if os.name!='nt':return False
    try:
        enabled=ctypes.c_int();ok=ctypes.windll.user32.SystemParametersInfoW(0x1042,0,ctypes.byref(enabled),0)
        return bool(ok and enabled.value)
    except OSError:return False

class HelpTip:
    def __init__(self,widget,title,text):
        self.widget=widget;self.title=title;self.text=text;self.popup=None;self.timer=None
        widget.bind('<Enter>',self.schedule,add='+');widget.bind('<FocusIn>',self.schedule,add='+')
        for event in ('<Leave>','<FocusOut>','<Escape>','<Destroy>'):widget.bind(event,self.hide,add='+')
        widget.bind('<Button-1>',self.show,add='+');widget.bind('<Return>',self.show,add='+');widget.bind('<space>',self.show,add='+')
    def schedule(self,event=None):
        self.hide();self.timer=self.widget.after(300,self.show)
    def show(self,event=None):
        self.hide()
        if not self.widget.winfo_exists():return
        p=tk.Toplevel(self.widget);self.popup=p;p.withdraw();p.overrideredirect(True);p.attributes('-topmost',True)
        p.configure(bg=GOLD);body=tk.Frame(p,bg=CARD,padx=14,pady=12);body.pack(padx=1,pady=1)
        tk.Label(body,text=self.title,bg=CARD,fg=TEXT,font=('Segoe UI Semibold',10),wraplength=310,justify='left').pack(anchor='w')
        tk.Label(body,text=self.text,bg=CARD,fg=TEXT,font=('Segoe UI',10),wraplength=310,justify='left').pack(anchor='w',pady=(5,0))
        p.update_idletasks();x=self.widget.winfo_rootx();y=self.widget.winfo_rooty()+self.widget.winfo_height()+6
        x=max(0,min(x,p.winfo_screenwidth()-p.winfo_reqwidth()-8));y=max(0,min(y,p.winfo_screenheight()-p.winfo_reqheight()-8))
        p.geometry(f'+{x}+{y}');p.deiconify()
        if motion_enabled():
            try:p.attributes('-alpha',0);self.fade(p,0)
            except tk.TclError:pass
    def fade(self,p,step):
        if self.popup is not p:return
        try:p.attributes('-alpha',min(1,step/4))
        except tk.TclError:return
        if step<4:self.timer=self.widget.after(35,lambda:self.fade(p,step+1))
    def hide(self,event=None):
        if self.timer:
            try:self.widget.after_cancel(self.timer)
            except tk.TclError:pass
            self.timer=None
        if self.popup:
            try:self.popup.destroy()
            except tk.TclError:pass
            self.popup=None

def help_label(parent,text,key=None):
    row=ttk.Frame(parent);row.pack(fill='x',pady=(12,4))
    ttk.Label(row,text=text,style='Field.TLabel',wraplength=480).pack(side='left',fill='x',expand=True)
    if key:
        title,body=HELP[key];button=ttk.Button(row,text='ⓘ',width=3,style='Info.TButton',takefocus=True)
        button.pack(side='right',padx=(8,0));HelpTip(button,title,body)
    return row

class FlowButtons(ttk.Frame):
    def __init__(self,parent,items,style):
        super().__init__(parent);self.pack(fill='x',pady=(12,4));self.items=[];self.last_width=-1
        for label,command in items:
            b=ttk.Button(self,text=label,command=command,style=style(label));self.items.append(b)
        self.bind('<Configure>',self.reflow);self.after_idle(self.reflow)
    def reflow(self,event=None):
        width=self.winfo_width()
        if width<20 or width==self.last_width:return
        self.last_width=width;x=0;y=0;line=0
        for b in self.items:
            w=min(width,b.winfo_reqwidth());h=b.winfo_reqheight()
            if x and x+w>width:x=0;y+=line+8;line=0
            b.place(x=x,y=y,width=w,height=h);x+=w+8;line=max(line,h)
        self.configure(height=y+line)

class CategoryBook(ttk.Frame):
    """Small Notebook-compatible presentation adapter; original pages/callbacks retained."""
    def __init__(self,parent):
        super().__init__(parent);self.pages=[];self.current=None;self.reflow_timer=None
        self.columnconfigure(1,weight=1);self.rowconfigure(0,weight=1)
        self.nav=ttk.Treeview(self,show='tree',selectmode='browse',height=9,style='Navigation.Treeview',takefocus=True)
        nav_font=tkfont.Font(self,font=('Segoe UI',10))
        self.nav.column('#0',width=max(166,nav_font.measure('Phone & streaming')+30),minwidth=130,stretch=False);self.nav.grid(row=0,column=0,sticky='ns',padx=(0,12))
        holder=ttk.Frame(self);holder.grid(row=0,column=1,sticky='nsew');holder.columnconfigure(0,weight=1);holder.rowconfigure(0,weight=1)
        self.canvas=tk.Canvas(holder,bg=BASE,bd=0,highlightthickness=0);self.canvas.grid(row=0,column=0,sticky='nsew')
        self.scroll=ttk.Scrollbar(holder,orient='vertical',command=self.canvas.yview);self.scroll.grid(row=0,column=1,sticky='ns')
        self.canvas.configure(yscrollcommand=self.scroll.set);self.item=self.canvas.create_window((0,0),anchor='nw')
        self.nav.bind('<<TreeviewSelect>>',self.choose);self.nav.bind('<Return>',self.enter_page);self.nav.bind('<Right>',self.enter_page)
        self.canvas.bind('<Configure>',self.resize);self.bind('<Destroy>',self.cleanup,add='+')
        self.canvas.bind('<MouseWheel>',lambda e:self.canvas.yview_scroll(int(-e.delta/120),'units'))
    def add(self,widget,text):return self.insert(len(self.pages),widget,text=text)
    def insert(self,index,widget,text):
        key=str(widget);self.pages.insert(index,(key,widget,text));self.nav.insert('',index,iid=key,text=text)
        widget.bind('<Configure>',self.content_changed,add='+');widget.bind('<FocusIn>',self.focus_visible,add='+')
        key_help={'Muse':'muse','Phone & streaming':'phone','Connection':'bridge','Folders':'folders','Windows control':'stop','Access':'tier','Voice':'voice','Orb':'orb'}.get(text)
        help_label(widget,text,key_help)
        if len(self.pages)==1:self.nav.selection_set(key)
    def finalize(self):
        def bind_children(widget):
            widget.bind('<FocusIn>',self.focus_visible,add='+')
            if not isinstance(widget,(tk.Text,ttk.Treeview,ttk.Combobox)):
                widget.bind('<MouseWheel>',lambda e:self.canvas.yview_scroll(int(-e.delta/120),'units'),add='+')
            for child in widget.winfo_children():bind_children(child)
        for identity,page,text in self.pages:bind_children(page)
        top=self.winfo_toplevel()
        top.bind('<Control-Tab>',lambda e:self.cycle(1),add='+');top.bind('<Control-Shift-Tab>',lambda e:self.cycle(-1),add='+')
    def cycle(self,step):
        selected=self.nav.selection();keys=[k for k,w,t in self.pages]
        if keys:self.nav.selection_set(keys[(keys.index(selected[0])+step)%len(keys)] if selected else keys[0])
        return 'break'
    def choose(self,event=None):
        selection=self.nav.selection()
        if not selection:return
        key=selection[0];page=next(widget for identity,widget,text in self.pages if identity==key)
        self.current=page;self.canvas.itemconfigure(self.item,window=page);self.canvas.yview_moveto(0);self.resize()
    def enter_page(self,event=None):
        if self.current:
            target=self.current.tk_focusNext()
            if target:target.focus_set()
        return 'break'
    def resize(self,event=None):
        if self.current:
            width=max(180,self.canvas.winfo_width());self.canvas.itemconfigure(self.item,width=width)
            self.wrap(self.current,width-40);self.content_changed()
    def wrap(self,parent,width):
        for child in parent.winfo_children():
            if isinstance(child,ttk.Label) and int(float(child.cget('wraplength') or 0))>0:child.configure(wraplength=max(140,width))
            self.wrap(child,width)
    def content_changed(self,event=None):
        if self.reflow_timer:return
        self.reflow_timer=self.after_idle(self.update_region)
    def update_region(self):
        self.reflow_timer=None
        if not self.current:return
        self.canvas.configure(scrollregion=self.canvas.bbox('all'))
        if self.current.winfo_reqheight()>self.canvas.winfo_height()+2:self.scroll.grid()
        else:self.scroll.grid_remove()
    def focus_visible(self,event):
        if self.current and event.widget is not self.current:
            y=event.widget.winfo_rooty()-self.current.winfo_rooty();height=max(1,self.current.winfo_height());top=self.canvas.canvasy(0)
            if y<top or y+event.widget.winfo_height()>top+self.canvas.winfo_height():self.canvas.yview_moveto(max(0,(y-16)/height))
    def cleanup(self,event):
        if event.widget is self and self.reflow_timer:
            try:self.after_cancel(self.reflow_timer)
            except tk.TclError:pass

def page_help(parent,key):
    help_label(parent,HELP[key][0],key)

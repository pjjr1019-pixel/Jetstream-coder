"""Presentation only: Jett Pad's shared Tk/ttk black-and-gold palette."""
import os
import tkinter as tk
from tkinter import ttk

BASE='#0A0A0A'
PANEL='#141414'
CARD='#1A1A1A'
GOLD='#C9A227'
HIGHLIGHT='#D4AF37'
TEXT='#F5F0E6'
MUTED='#BCAF95'
DISABLED='#998F7A'
AMBER='#D7A95B'
ERROR='#C46C66'
BORDER='#39352D'
HOVER='#24211B'

def titlebar(window):
    """Style only this application's native Windows frame, never OS settings."""
    if os.name!='nt':return False
    try:
        import ctypes
        from ctypes import wintypes as w
        user=ctypes.WinDLL('user32',use_last_error=True)
        user.GetAncestor.argtypes=[w.HWND,w.UINT];user.GetAncestor.restype=w.HWND
        hwnd=user.GetAncestor(window.winfo_id(),2)
        dwm=ctypes.WinDLL('dwmapi');dwm.DwmSetWindowAttribute.argtypes=[w.HWND,w.DWORD,w.LPCVOID,w.DWORD]
        def color(value):
            r,g,b=(int(value[i:i+2],16) for i in (1,3,5));return r|(g<<8)|(b<<16)
        results=[]
        for attribute,value in ((20,1),(34,color(BORDER)),(35,color(BASE)),(36,color(TEXT))):
            item=w.DWORD(value)
            results.append(dwm.DwmSetWindowAttribute(hwnd,attribute,ctypes.byref(item),ctypes.sizeof(item))==0)
        return all(results)
    except (OSError,AttributeError,tk.TclError):return False

def apply_theme(window):
    root=window._root()
    if not getattr(root,'_jett_black_gold',False):
        root._jett_black_gold=True
        root.tk_setPalette(background=BASE,foreground=TEXT,activeBackground=CARD,activeForeground=TEXT,
                           selectBackground=GOLD,selectForeground=BASE,highlightBackground=BASE,highlightColor=GOLD)
        options={
            '*background':BASE,'*foreground':TEXT,'*insertBackground':TEXT,
            '*selectBackground':GOLD,'*selectForeground':BASE,'*highlightBackground':BASE,'*highlightColor':GOLD,
            '*activeBackground':CARD,'*activeForeground':TEXT,'*disabledForeground':DISABLED,
            '*Entry.background':PANEL,'*Text.background':PANEL,'*Listbox.background':PANEL,
            '*Button.background':BASE,'*Button.foreground':GOLD,
            '*Menu.background':CARD,'*Menu.foreground':GOLD,
            '*Scrollbar.background':CARD,'*Scrollbar.troughColor':BASE,'*Scrollbar.activeBackground':GOLD,
            '*TCombobox*Listbox.background':PANEL,'*TCombobox*Listbox.foreground':TEXT,
            '*TCombobox*Listbox.selectBackground':GOLD,'*TCombobox*Listbox.selectForeground':BASE,
        }
        for option,value in options.items():root.option_add(option,value,'interactive')
        style=ttk.Style(root);style.theme_use('clam')
        style.configure('.',background=BASE,foreground=TEXT,font=('Segoe UI',10),bordercolor=BORDER,
                        lightcolor=BASE,darkcolor=BASE,troughcolor=BASE,focuscolor=GOLD)
        style.map('.',background=[('disabled',BASE),('active',BASE)],foreground=[('disabled',DISABLED)],
                  selectbackground=[('!focus',GOLD)],selectforeground=[('!focus',BASE)])
        style.configure('TFrame',background=BASE)
        style.configure('Panel.TFrame',background=PANEL)
        style.configure('TLabel',background=BASE,foreground=TEXT)
        style.configure('Title.TLabel',foreground=GOLD,font=('Segoe UI Semibold',19))
        style.configure('Heading.TLabel',foreground=GOLD,font=('Segoe UI Semibold',13))
        style.configure('Field.TLabel',foreground=TEXT,font=('Segoe UI Semibold',10))
        style.configure('Muted.TLabel',foreground=MUTED)
        style.configure('TButton',background=PANEL,foreground=TEXT,bordercolor=BORDER,lightcolor=BORDER,darkcolor=BORDER,
                        borderwidth=1,relief='flat',padding=(12,8),focusthickness=2,focuscolor=HIGHLIGHT)
        style.map('TButton',background=[('disabled',CARD),('pressed',HOVER),('active',HOVER)],
                  foreground=[('disabled',DISABLED)],bordercolor=[('focus',HIGHLIGHT),('active',HIGHLIGHT)])
        style.configure('Info.TButton',foreground=GOLD,padding=(4,2),borderwidth=0)
        style.configure('Danger.TButton',foreground=ERROR,bordercolor=ERROR)
        style.map('Danger.TButton',foreground=[('disabled',DISABLED),('!disabled',ERROR)])
        style.configure('Primary.TButton',background=GOLD,foreground=BASE,lightcolor=GOLD,darkcolor=GOLD,focuscolor=BASE)
        style.map('Primary.TButton',background=[('disabled',CARD),('pressed',GOLD),('active',HIGHLIGHT)],
                  foreground=[('disabled',DISABLED),('!disabled',BASE)])
        for kind in ('TCheckbutton','TRadiobutton'):
            style.configure(kind,background=BASE,foreground=TEXT,indicatorbackground=CARD,indicatorforeground=GOLD,
                            upperbordercolor=GOLD,lowerbordercolor=GOLD)
            style.map(kind,background=[('active',BASE)],foreground=[('disabled',DISABLED)],
                      indicatorbackground=[('selected',GOLD),('!selected',CARD)],
                      indicatorforeground=[('selected',BASE),('!selected',GOLD)])
        for kind in ('TEntry','TCombobox','TSpinbox'):
            style.configure(kind,fieldbackground=PANEL,background=CARD,foreground=TEXT,insertcolor=TEXT,
                            bordercolor=BORDER,lightcolor=BORDER,darkcolor=BORDER,arrowcolor=GOLD,padding=8)
            style.map(kind,fieldbackground=[('disabled',CARD),('readonly',PANEL)],
                      foreground=[('disabled',DISABLED),('readonly',TEXT)],
                      selectbackground=[('!disabled',GOLD)],selectforeground=[('!disabled',BASE)],
                      bordercolor=[('focus',HIGHLIGHT),('!focus',BORDER)],
                      lightcolor=[('focus',GOLD),('!focus',BORDER)],darkcolor=[('focus',GOLD),('!focus',BORDER)],
                      background=[('active',CARD),('readonly',CARD)])
        style.configure('TNotebook',background=BASE,bordercolor=GOLD,tabmargins=(0,0,0,1))
        style.configure('TNotebook.Tab',background=PANEL,foreground=GOLD,padding=(12,8),bordercolor=GOLD,
                        lightcolor=PANEL,darkcolor=PANEL)
        style.map('TNotebook.Tab',background=[('selected',GOLD),('active',CARD)],
                  lightcolor=[('selected',GOLD),('!selected',PANEL)],darkcolor=[('selected',GOLD),('!selected',PANEL)],
                  foreground=[('selected',BASE),('disabled',DISABLED)])
        style.configure('Treeview',background=PANEL,fieldbackground=PANEL,foreground=TEXT,bordercolor=BORDER,rowheight=30)
        style.configure('Navigation.Treeview',background=BASE,fieldbackground=BASE,borderwidth=0,rowheight=42,font=('Segoe UI',10))
        style.map('Navigation.Treeview',background=[('selected',CARD)],foreground=[('selected',HIGHLIGHT)])
        style.map('Treeview',background=[('selected',GOLD)],foreground=[('selected',BASE),('disabled',DISABLED)],
                  bordercolor=[('focus',GOLD),('!focus',BORDER)],
                  lightcolor=[('focus',GOLD),('!focus',BORDER)],darkcolor=[('focus',GOLD),('!focus',BORDER)])
        style.configure('Treeview.Heading',background=CARD,foreground=GOLD,lightcolor=CARD,darkcolor=CARD,
                        bordercolor=GOLD,relief='flat',padding=6)
        style.map('Treeview.Heading',background=[('active',CARD),('pressed',PANEL)])
        for orientation in ('Vertical','Horizontal'):
            style.configure(orientation+'.TScrollbar',background=CARD,troughcolor=BASE,arrowcolor=GOLD,
                            bordercolor=BASE,lightcolor=CARD,darkcolor=CARD,gripcount=0)
            style.map(orientation+'.TScrollbar',background=[('active',GOLD),('pressed',GOLD)],
                      arrowcolor=[('active',BASE),('pressed',BASE)])
        style.configure('TSeparator',background=BORDER)
        style.configure('TLabelframe',background=BASE,bordercolor=GOLD)
        style.configure('TLabelframe.Label',background=BASE,foreground=GOLD)
        style.configure('TProgressbar',background=GOLD,troughcolor=CARD,bordercolor=BASE,lightcolor=GOLD,darkcolor=GOLD)
        # Classic Tk simpledialogs inherit the option database above. Their
        # native title bars receive the same styling when they are mapped.
        def mapped(event):
            if hasattr(event.widget,'winfo_toplevel') and event.widget.winfo_toplevel() is event.widget:titlebar(event.widget)
        root.bind_class('Toplevel','<Map>',mapped,add='+')
        root.bind('<Map>',mapped,add='+')
    window.configure(background=BASE)
    window.after_idle(lambda:titlebar(window))

def button_style(label):
    if label.startswith(('Remove','Revoke','Stop all','Quit')):return 'Danger.TButton'
    return 'Primary.TButton' if label.startswith(('Save','Apply','Done signing')) else 'TButton'

def status_color(value):
    value=value.casefold()
    if any(word in value for word in ('failed','error','could not','refused')):return ERROR
    if any(word in value for word in ('inactive','disabled','disconnected','not ready','not active','not connected')):return AMBER
    if any(word in value for word in ('waiting','unavailable','unreachable','checking','connecting','working','offline','stopped','latched')):return AMBER
    if any(word in value for word in ('connected','ready','active','new message')):return GOLD
    return TEXT

def bind_status(label,variable,*,active=True):
    def update(*_):
        color=status_color(variable.get())
        label.configure(foreground=TEXT if not active and color==GOLD else color)
    token=variable.trace_add('write',update);update()
    def cleanup(event):
        if event.widget is label:
            try:variable.trace_remove('write',token)
            except tk.TclError:pass
    label.bind('<Destroy>',cleanup,add='+')

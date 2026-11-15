"""Local-only operator dialog. PINs are never accepted in arguments or HTTP."""
import queue,threading

def main():
    import sys,tkinter as tk
    from tkinter import ttk
    from .cli import default_home
    from .remote_pin import PinStore
    from .pad_theme import apply_theme
    if len(sys.argv)!=1:raise SystemExit('PIN setup accepts no arguments; enter it locally in the window')
    root=tk.Tk();root.title('MuseBridge operator — remote-control PIN');root.resizable(False,False)
    apply_theme(root)
    panel=ttk.Frame(root,padding=20);panel.pack()
    ttk.Label(panel,text='Set or change your remote-control PIN',style='Heading.TLabel').pack(anchor='w')
    ttk.Label(panel,text='Enter it twice here on this PC. Use 6–128 letters or digits.\n'
              'This allows temporary remote see/desktop control only.\n'
              'Local STOP, protected windows and your existing approvals still apply.',wraplength=440).pack(anchor='w',pady=12)
    ttk.Label(panel,text='New PIN',style='Field.TLabel').pack(anchor='w');pin=ttk.Entry(panel,show='•',width=36);pin.pack(fill='x')
    ttk.Label(panel,text='Confirm PIN',style='Field.TLabel').pack(anchor='w',pady=(10,0));confirm=ttk.Entry(panel,show='•',width=36);confirm.pack(fill='x')
    notice=tk.StringVar(value='No PIN is sent through the bridge.');ttk.Label(panel,textvariable=notice,wraplength=440).pack(pady=12)
    results=queue.Queue()
    def save():
        value,confirmation=pin.get(),confirm.get()
        try:PinStore.validate(value)
        except ValueError as exc:notice.set(str(exc));return
        if value!=confirmation:notice.set('The PIN entries do not match.');return
        button.configure(state='disabled');notice.set('Saving securely on this PC…')
        def work():
            try:
                home=default_home();PinStore(home).set_local(value,confirmation)
                from .audit import AuditLog
                AuditLog(home/'audit.sqlite3').record('control.pin.set','local operator',True,'PIN configured locally')
                results.put(True)
            except Exception:results.put(False)
        threading.Thread(target=work,daemon=True).start()
    button=ttk.Button(panel,text='Save PIN locally',command=save,style='Primary.TButton');button.pack(side='left')
    ttk.Button(panel,text='Close',command=root.destroy).pack(side='right')
    def poll():
        try:ok=results.get_nowait()
        except queue.Empty:pass
        else:
            pin.delete(0,'end');confirm.delete(0,'end');button.configure(state='normal')
            notice.set('PIN saved locally. Ready for remote testing.' if ok else 'Could not save the PIN. Check local private-store access.')
        root.after(80,poll)
    pin.focus_set();root.after(80,poll);root.mainloop()

if __name__=='__main__':main()

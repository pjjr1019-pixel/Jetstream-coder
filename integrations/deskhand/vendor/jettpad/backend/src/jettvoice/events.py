"""Metadata and device facts only. Never captures pixels or infers temperatures."""
import uuid


class Events:
    def __init__(self, emit):
        self.emit = emit
        self.previous = None
        self.media_available = None

    def publish(self, event, **data):
        self.emit('desktop_event', contract='1.1', id=str(uuid.uuid4()), event=event, data=data)

    def poll(self):
        import psutil
        import win32gui
        import win32process
        from jettcrew.support import machine_facts
        windows = {}

        def collect(hwnd, _):
            if win32gui.IsWindowVisible(hwnd):
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                try:
                    process = psutil.Process(pid)
                    windows[str(hwnd)] = {'pid': pid, 'process_name': process.name(),
                                          'started': process.create_time()}
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
        win32gui.EnumWindows(collect, None)
        facts = machine_facts()
        fullscreen = facts['fullscreen']
        hwnd = win32gui.GetForegroundWindow()
        # Windows desktop shell covers the screen, but is not a fullscreen activity.
        if hwnd and win32gui.GetClassName(hwnd) in {'Progman', 'WorkerW', 'Shell_TrayWnd'}:
            fullscreen = False
        media = None
        try:
            from pycaw.pycaw import AudioUtilities
            media = any(session.State == 1 for session in AudioUtilities.GetAllSessions())
            self.media_available = True
        except Exception:
            self.media_available = False
        cpu_percent=psutil.cpu_percent()
        value = dict(windows=windows, fullscreen=fullscreen,
                     idle=(facts['idle_seconds'] >= 60) if facts['idle_seconds'] is not None else None,
                     media=media, cpu_high=cpu_percent >= 85)
        if self.previous is None:
            self.publish('sensors.available', thermal={'available': False, 'reason': 'No verified temperature provider'},
                         media={'available': self.media_available})
        else:
            old = self.previous
            for identity in windows.keys() - old['windows'].keys():
                self.publish('window.opened', window_id=identity, **windows[identity])
            for identity in old['windows'].keys() - windows.keys():
                self.publish('window.closed', window_id=identity, **old['windows'][identity])
            previous_processes = {(w['pid'], w['started']) for w in old['windows'].values()}
            for process in {(w['pid'], w['started'], w['process_name']) for w in windows.values()}:
                if process[:2] not in previous_processes:
                    self.publish('app.launched', pid=process[0], process_name=process[2])
            for key, yes, no in [('fullscreen', 'fullscreen.enter', 'fullscreen.exit'),
                                 ('idle', 'user.idle', 'user.return'),
                                 ('media', 'media.started', 'media.stopped'),
                                 ('cpu_high', 'cpu.pressure', 'cpu.normal')]:
                if value[key] is not None and value[key] != old[key]:
                    self.publish(yes if value[key] else no, available=True,
                                 **({'cpu_percent':cpu_percent,'threshold_percent':85} if key=='cpu_high' else {}))
        self.previous = value
        return value

"""Harmless Windows job/STOP-kernel-signal test; NEVER starts Sunshine."""
from pathlib import Path
import json,os,subprocess,sys,time,uuid
from .windows import StopSignal,SuspendedOwnedProcess,creation_time
from .launcher import WinLauncher

def case(kind):
    root=Path(__file__).resolve().parents[1]
    test=root/'guard-test';test.mkdir(exist_ok=True)
    # The random test signal is independent of real STOP. No grant or live state changes.
    event_name='Local\\JettPad.GuardProbe.'+str(uuid.uuid4())
    signal=StopSignal(event_name,create=True,initially_stopped=False)
    python=getattr(sys,'_base_executable',sys.executable)
    with (test/'child.log').open('ab',buffering=0) as log:
        process=SuspendedOwnedProcess([python,'-c','import time;time.sleep(60)'],root,log)
    publisher=None;identity=None
    if kind=='publisher_crash':
        publisher=subprocess.Popen([python,'-c','import time;time.sleep(60)'],creationflags=0x08000000)
        identity={'owner_pid':publisher.pid,'owner_created':creation_time(publisher._handle)}
    launcher=WinLauncher(root,event_name,test/'unused.log',test/'unused.conf',publisher_identity=identity)
    try:
        launcher.launch_suspended(process,lambda job:True,Path(python).resolve(),guard_test=True)
        if not process.alive():raise AssertionError('Test child did not start')
        started=time.perf_counter_ns()
        if kind=='kernel_stop_signal':signal.latch()
        elif kind=='hard_badge_crash':launcher.last_owned.guard.kill()
        elif kind=='publisher_crash':publisher.kill()
        else:raise AssertionError('Unknown harmless probe')
        process.process.wait(timeout=3)
        elapsed=(time.perf_counter_ns()-started)/1e6
        return {'case':kind,'exact_child_exit_ms':round(elapsed,3),'pass':True}
    finally:
        if launcher.last_owned:launcher.last_owned.terminate_now()
        else:process.terminate_now()
        signal.close()
        if publisher is not None and publisher.poll() is None:publisher.kill()
        if publisher is not None:publisher.wait(timeout=3)

if __name__=='__main__':
    root=Path(__file__).resolve().parents[1]
    previous=root/'guard-test-result.json'
    if previous.exists():
        history=root/'guard-test/history';history.mkdir(parents=True,exist_ok=True)
        (history/(str(time.time_ns())+'.json')).write_bytes(previous.read_bytes())
    rows=[case(kind) for kind in ('kernel_stop_signal','hard_badge_crash','publisher_crash')]
    (root/'guard-test-result.json').write_text(json.dumps({'test_only':True,'sunshine_started':False,
        'cases':rows,'clock':'perf_counter_ns','clock_resolution_seconds':time.get_clock_info('perf_counter').resolution,
        'actual_bridge_STOP_test':False,'result':'PASS: real suspended jobs; kernel STOP signal and hard badge crash each kill the exact sleeping child'},indent=2))

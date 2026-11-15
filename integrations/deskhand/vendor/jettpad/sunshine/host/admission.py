"""Local evidence gate. No fabricated receipts and no permission changes."""
from pathlib import Path
import argparse,hashlib,json,time
from .discovery import analyze

SOURCE_COMMIT='63d35f702ee9e362e43263742981836ec0710384'

def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def read(path):return json.loads(path.read_text(encoding='utf-8-sig'))

def evidence(root,snapshots=None):
    from .runtime import EXE_HASH,LOCAL,PHONE
    private=root/'private';binary=root/'vendor/Sunshine/sunshine.exe'
    source=read(root/'vendor/Sunshine/JETTPAD_BUILD.json')
    if source.get('source_commit')!=SOURCE_COMMIT or source.get('binary_sha256')!=EXE_HASH or digest(binary)!=EXE_HASH:
        raise ValueError('The discovery-disabled candidate does not match its pinned build manifest')
    if source.get('dns_sd_registration_disabled') is not True:raise ValueError('DNS-SD patch is not documented')
    capture=private/'discovery.pcapng';setup=snapshots['setup'] if snapshots else read(private/'admin-result.json')
    if setup.get('state')!='observation_complete':raise ValueError('Finish the local discovery observation first')
    start=setup.get('ready_ts',0);end=setup.get('finished_ts',0)
    if end-start<60:raise ValueError('At least 60 seconds of real observation is required')
    controls=setup.get('positive_controls',[])
    physical=setup.get('physical_nics',[])
    if isinstance(physical,dict):physical=[physical]
    if not physical:raise ValueError('No physical-interface evidence')
    for nic in physical:
        for phase in ('start','end'):
            if not any(c.get('nic')==nic['Name'] and c.get('phase')==phase and c.get('sent') is True for c in controls):
                raise ValueError('Missing physical-interface positive control')
    markers=[c['marker'] for c in controls]
    if not markers or any(not m.startswith('_jettcheck-') or len(m)>63 for m in markers):raise ValueError('Invalid positive-control identifiers')
    report=analyze(capture,markers)
    if any(not rows for rows in report['positive_controls'].values()):raise ValueError('Capture missed a positive control; no discovery conclusion is allowed')
    if not report['no_announcements']:raise ValueError('Sunshine discovery announcement observed; persistent readiness refused')
    observed=snapshots['listeners'] if snapshots else read(private/'observed-listeners.json')
    if not start<=observed.get('ts',0)<=end or observed.get('binary_sha256')!=EXE_HASH:
        raise ValueError('No matching live Sunshine listener proof inside this observation')
    from .policy import EndpointPolicy
    policy=EndpointPolicy(LOCAL,PHONE,'Tailscale',binary.resolve())
    sockets={tuple(s) for s in observed.get('sockets',[])}
    policy.validate_observed_sockets(sockets)
    if not {('tcp',policy.local,p) for p in (47984,47989,47990,48010)}.issubset(sockets):raise ValueError('Incomplete exact-address live listener evidence')
    return {'binary_sha256':EXE_HASH,'source_commit':SOURCE_COMMIT,'discovery_disabled':True,'positive_control_observed':True,
        'sunshine_announcements':0,'capture_sha256':digest(capture),'manifest_sha256':digest(root/'vendor/Sunshine/JETTPAD_BUILD.json'),
        'observation_seconds':end-start,'verified_ts':time.time(),'report':report}

def verify(root):
    saved=read(root/'private/persistent-admission.json');actual=evidence(root,read(root/'private/persistent-evidence.json'))
    for key in ('binary_sha256','source_commit','capture_sha256','manifest_sha256'):
        if saved.get(key)!=actual[key]:raise ValueError('Admission evidence changed; repeat local verification')
    return actual

def admit(root):
    from .runtime import atomic
    snapshots={'setup':read(root/'private/admin-result.json'),'listeners':read(root/'private/observed-listeners.json')}
    actual=evidence(root,snapshots)
    atomic(root/'private/persistent-evidence.json',snapshots)
    atomic(root/'private/persistent-admission.json',actual);return actual

def finalize(root):
    # The diagnostic setup already installed these exact rules in PersistentStore
    # with local Windows approval. Verify them; do not ask for elevation again or
    # widen/change a rule merely to retire the diagnostic dependency.
    from .runtime import Runtime,atomic,LOCAL,PHONE
    from .policy import EndpointPolicy
    proof=admit(root)
    runtime=Runtime.__new__(Runtime);runtime.root=root;runtime.private=root/'private'
    runtime.exe=root/'vendor/Sunshine/sunshine.exe'
    runtime.policy=EndpointPolicy(LOCAL,PHONE,'Tailscale',runtime.exe)
    runtime.persistent_network_ready()
    atomic(root/'private/admin-result.json',{'state':'persistent_ready','ready_ts':time.time(),'stream_started':False,
        'capture_sha256':proof['capture_sha256'],'rules':'Previously approved PersistentStore rules independently reverified'})
    return proof

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--verify',action='store_true');parser.add_argument('--admit',action='store_true');args=parser.parse_args()
    root=Path(__file__).resolve().parents[1]
    if args.verify:verify(root)
    elif args.admit:finalize(root)
    else:raise SystemExit('Choose --verify or --admit')

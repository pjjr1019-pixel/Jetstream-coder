"""Restricted paired-phone channel, isolated from the cloud bridge session."""
import asyncio, base64, hashlib, ipaddress, json, secrets, time, uuid
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from companion_data import PHONE, home, messages, paired_certificates, request, settings, stream_root

def transcript(client_nonce, server_nonce, fingerprint):
    return ('JettPad companion v1\n'+client_nonce+'\n'+server_nonce+'\n'+fingerprint).encode()
def verify(cert, signature, data):
    cert.public_key().verify(base64.b64decode(signature,validate=True),data,padding.PKCS1v15(),hashes.SHA256())

class CompanionChannel:
    def __init__(self,bridge,*,expected_phone=PHONE,stream_backend=None):
        from companion_data import atomic
        try: address=ipaddress.ip_address(expected_phone)
        except (ValueError,TypeError): raise ValueError('Invalid locally approved phone address') from None
        if address.version!=4 or address not in ipaddress.ip_network('100.64.0.0/10') or str(address)!=expected_phone:
            raise ValueError('Invalid locally approved phone address')
        # Caller must source this only from the operator-approved binding and
        # authenticate the actual embedded-node peer independently.
        self.expected_phone=expected_phone
        if stream_backend is not None and not all(callable(getattr(stream_backend,name,None)) for name in ('submit','status')):
            raise ValueError('Invalid embedded streaming backend')
        self.stream_backend=stream_backend
        self.bridge=bridge
        if not hasattr(bridge,'_companion_stop_generation'):bridge._companion_stop_generation=0
        if not hasattr(bridge,'_phone_viewing_barrier'):bridge._phone_viewing_barrier=0
        if not hasattr(bridge,'_companion_boot_id'):bridge._companion_boot_id=uuid.uuid4().hex
        if not hasattr(bridge,'phone_input'):
            from .phone_input import PhoneInput
            bridge.phone_input=PhoneInput(bridge)
            bridge.desktop.phone_input_busy=lambda:bridge.phone_input.active
        atomic(home()/'companion-bridge-boot.json',{'id':bridge._companion_boot_id})
    async def handle(self,socket):
        if not socket.remote_address or socket.remote_address[0]!=self.expected_phone:
            await socket.close(1008,'Unapproved phone');return
        workers=[]
        try:
            hello=json.loads(await asyncio.wait_for(socket.recv(),5))
            if hello.get('type')!='hello':raise ValueError('Pairing identity required')
            raw=base64.b64decode(hello['certificate'],validate=True)
            fingerprint=hashlib.sha256(raw).hexdigest()
            cert,registered=paired_certificates()[fingerprint]
            if raw!=registered:raise ValueError('Unregistered certificate')
            client_nonce=hello['nonce']
            if len(base64.b64decode(client_nonce,validate=True))!=32:raise ValueError('Invalid nonce')
            server_nonce=base64.b64encode(secrets.token_bytes(32)).decode()
            proof=transcript(client_nonce,server_nonce,fingerprint)
            key=serialization.load_pem_private_key((stream_root()/'private/host.key').read_bytes(),password=None)
            signature=key.sign(proof+b'\nserver',padding.PKCS1v15(),hashes.SHA256())
            await socket.send(json.dumps({'type':'challenge','nonce':server_nonce,'signature':base64.b64encode(signature).decode()}))
            auth=json.loads(await asyncio.wait_for(socket.recv(),10))
            if auth.get('type')!='proof':raise ValueError('Proof required')
            verify(cert,auth['signature'],proof+b'\nphone')
            await self.bridge._audit('companion.auth',ok=True,detail='paired phone')
            await socket.send(json.dumps({'type':'ready','permissions':settings()}))
            from .phone_pin import PinChallenge
            recent=[]; input_recent=[];unlock_challenge=PinChallenge()
            def identity_valid():
                # PIN verification may yield while the transport closes.
                from websockets.protocol import State
                return socket.state is State.OPEN and fingerprint in paired_certificates()
            from .companion_push import delta, revision, validate_known
            push_known={};push_event=asyncio.Event()
            async def push_chat():
                nonlocal push_known
                last_revision=object();fallback_at=0.
                while True:
                    await push_event.wait()
                    if not await asyncio.to_thread(identity_valid):return
                    if not (await asyncio.to_thread(settings))['chat']:
                        push_event.clear();push_known={}
                        await socket.send(json.dumps({'type':'chat.update','payload':{'messages':[],'subscribed':False}}))
                        continue
                    current=await asyncio.to_thread(revision,home())
                    # Revision lookup is local and cheap; no phone polling wait.
                    if current!=last_revision or (current is None and time.monotonic()>=fallback_at):
                        rows=await asyncio.to_thread(messages)
                        changed,next_known=delta(rows,push_known)
                        # Recheck after I/O so revocation cannot leak a new body.
                        if not await asyncio.to_thread(identity_valid):return
                        if (await asyncio.to_thread(settings))['chat']:
                            if changed:
                                await socket.send(json.dumps({'type':'chat.update','payload':{'messages':changed,'subscribed':True}}))
                            push_known=next_known
                            # Continue draining size-bounded pages even if no new
                            # DB write occurs before the next watcher iteration.
                            unsent,_=delta(rows,push_known)
                            last_revision=object() if unsent else current
                            fallback_at=time.monotonic()+1
                    await asyncio.sleep(.1)
            ordinary=asyncio.Queue(maxsize=16)
            stopping=asyncio.Queue(maxsize=8)
            async def process(item):
                nonlocal push_known
                value,received_at,admitted_generation=item
                identifier,kind,payload=value['id'],value['type'],value.get('payload',{})
                try:
                    if not await asyncio.to_thread(identity_valid):raise ValueError('Phone disconnected or unpaired')
                    if kind=='stream.start' and admitted_generation!=self.bridge._companion_stop_generation:
                        raise ValueError('Start cancelled by STOP; tap Start again')
                    if kind=='stream.start' and value['_admitted_stream_epoch']!=self.bridge.stream_stop.status()['epoch']:
                        raise ValueError('Start cancelled by STOP; tap Start again')
                    if kind=='input.status':result=await asyncio.to_thread(self.bridge.phone_input.status,identity_valid)
                    elif kind=='input.begin':result=await asyncio.to_thread(self.bridge.phone_input.begin,identity_valid,payload.get('frame_age_ms',9999)/1000)
                    elif kind=='input.events':result=await asyncio.to_thread(self.bridge.phone_input.events,payload.get('session'),payload.get('seq'),payload.get('events'),payload.get('frame_age_ms',9999)/1000)
                    elif kind=='input.end':result=await asyncio.to_thread(self.bridge.phone_input.end,'Phone gesture ended',payload.get('session',''))
                    elif kind=='authorization.status':
                        result=await asyncio.to_thread(self.bridge.remote_control.status)
                    elif kind=='viewing.metrics':
                        # Measurement only: never used as authority or readiness proof.
                        from companion_data import atomic
                        import math
                        names=('received_fps','decoded_fps','video_mbps','rejected_frames','frame_age_ms','retry_attempt','profile')
                        row={name:payload[name] for name in names if type(payload.get(name)) in (int,float) and math.isfinite(payload[name]) and 0<=payload[name]<=1e12}
                        state=payload.get('state')
                        if state not in ('STOPPED','STARTING','VIEWING','RECONNECTING','BACKGROUND_PAUSED','BLOCKED'):raise ValueError('Invalid measurement state')
                        row.update(ts=time.time(),state=state,source='paired phone decoder counters; not a server readiness test',packet_loss=None)
                        await asyncio.to_thread(atomic,home()/'phone-viewing-metrics.json',row)
                        result={'accepted':True}
                    elif kind=='authorization.challenge':
                        result=unlock_challenge.issue(self.bridge.remote_control.epoch)
                    elif kind=='authorization.unlock':
                        pin=None
                        try:
                            pin=unlock_challenge.open(key,payload,self.bridge.remote_control.epoch)
                            result=await self.bridge._unlock_authority(pin,origin='phone',identity_valid=identity_valid)
                        except Exception as error:
                            raise ValueError(getattr(error,'code','Unlock refused; check PIN, local STOP and connection')) from None
                        finally:pin=None;payload.clear()
                    elif kind=='authorization.resume':
                        result=await self.bridge._unlock_authority(day=True,origin='phone',identity_valid=identity_valid)
                    elif kind=='status':
                        def snapshot():
                            if self.stream_backend is not None:
                                state=self.stream_backend.status()
                            else:
                                try: state=json.loads((home()/'companion-stream.json').read_text())
                                except (OSError,ValueError):state={'phase':'off','reason':'PC streaming service unavailable'}
                            if time.time()-state.get('ts',0)>5:state={'phase':'off','reason':'PC streaming service unavailable'}
                            from viewing_access import authorized
                            return {'stream':state,'permissions':settings(),'authorization':self.bridge.remote_control.status(),'viewing_independent':authorized(fingerprint),'viewing_barrier':self.bridge._phone_viewing_barrier,
                                    'control_stopped':False,'bridge_boot_id':self.bridge._companion_boot_id}
                        result=await asyncio.to_thread(snapshot)
                    elif kind=='chat.organize':
                        from workspace_store import organize
                        def allowed():return identity_valid() and settings()['chat']
                        if not await asyncio.to_thread(allowed):raise ValueError('Enable phone chat locally in PC Settings')
                        result=await asyncio.to_thread(organize,home(),payload.get('id'),payload.get('pinned'),payload.get('tags'),payload.get('expected_version'),identifier,allowed)
                        await self.bridge._audit('companion.chat.organize',ok=True,detail='paired phone; content omitted')
                    elif kind in ('chat.sync','chat.subscribe'):
                        if not (await asyncio.to_thread(settings))['chat']:raise ValueError('Enable phone chat locally in PC Settings')
                        known=validate_known(payload.get('known',{}))
                        rows=await asyncio.to_thread(messages)
                        if not await asyncio.to_thread(identity_valid) or not (await asyncio.to_thread(settings))['chat']:
                            raise ValueError('Phone chat was revoked')
                        changed,next_known=delta(rows,known)
                        result={'messages':changed,'subscribed':kind=='chat.subscribe'}
                        if kind=='chat.subscribe':
                            push_known=next_known;push_event.set()
                    elif kind in ('chat.send','stream.start','stream.stop','stream.pause'):
                        if kind in ('stream.stop','stream.pause'):
                            from companion_data import atomic
                            await asyncio.to_thread(atomic,home()/'control-viewing-stop.json',{'ts':received_at,'reason':'Paired phone stop/pause'})
                        if kind=='chat.send':
                            if not (await asyncio.to_thread(settings))['chat']:raise ValueError('Enable phone chat locally in PC Settings')
                            if not isinstance(payload.get('text'),str) or not 1<=len(payload['text'].strip())<=24000:raise ValueError('Enter 1 to 24000 characters')
                        if kind=='stream.start':
                            if not (await asyncio.to_thread(settings))['remote_start']:raise ValueError('Enable phone stream starts locally in PC Settings')
                            from viewing_access import enabled,authorized
                            if await asyncio.to_thread(enabled) and not await asyncio.to_thread(authorized,fingerprint):raise ValueError('This phone is not approved for unattended viewing')
                            if await asyncio.to_thread(enabled) and payload.get('viewing_barrier')!=self.bridge._phone_viewing_barrier:raise ValueError('STOP cancelled the old viewing intent; refresh and tap Start again')
                            if payload.get('privacy_warning_accepted') is not True:raise ValueError('Acknowledge desktop visibility on the phone')
                            payload['_bridge_boot_id']=self.bridge._companion_boot_id
                            payload['_stream_stop_epoch']=value['_admitted_stream_epoch']
                        if kind.startswith('stream.') and self.stream_backend is not None:
                            # The embedded backend owns durable deduplication and
                            # the guarded capture/relay lifetime. Authentication,
                            # local permission and STOP admission above still apply.
                            result=await self.stream_backend.submit(identifier,kind,payload,fingerprint,received_at)
                        else:
                            await asyncio.to_thread(request,identifier,kind,payload,fingerprint,created=received_at)
                            result={'accepted':True}
                        await self.bridge._audit('companion.'+kind,ok=True,detail='paired phone; content omitted')
                    else:raise ValueError('Operation unavailable to companion')
                    await socket.send(json.dumps({'id':identifier,'type':'result','payload':result}))
                except ValueError as error:
                    await socket.send(json.dumps({'id':identifier,'type':'error','message':str(error)}))

            async def consume(queue):
                try:
                    while True:
                        item=await queue.get()
                        try:await process(item)
                        finally:queue.task_done()
                except Exception:
                    await socket.close(1008,'Companion operation failed; outcome may be unconfirmed')
            async def watch():
                try:await push_chat()
                except Exception:await socket.close(1008,'Chat subscription ended; reconnect to resynchronize')
            workers=[asyncio.create_task(consume(ordinary)),asyncio.create_task(consume(stopping)),asyncio.create_task(watch())]
            stop_recent=[]
            async for raw in socket:
                if not isinstance(raw,str) or len(raw)>40000:raise ValueError('Companion message too large')
                value=json.loads(raw)
                if not isinstance(value,dict):raise ValueError('Invalid request')
                identifier=value['id'];kind=value['type'];payload=value.get('payload',{})
                if str(uuid.UUID(identifier))!=identifier:raise ValueError('Invalid request ID')
                if not isinstance(kind,str) or not isinstance(payload,dict):raise ValueError('Invalid request')
                # No request, including STOP, bypasses current paired identity.
                if fingerprint not in await asyncio.to_thread(paired_certificates):raise ValueError('Phone was unpaired')
                received_at=time.time();now=time.monotonic()
                if kind=='stream.start':
                    lease=self.bridge.stream_stop.status()
                    if not lease.get('available') or type(lease.get('epoch')) is not int:
                        await socket.send(json.dumps({'id':identifier,'type':'error','message':'Stream STOP safety is unavailable; chat remains connected'}))
                        continue
                    value['_admitted_stream_epoch']=lease['epoch']
                if kind in ('stream.stop','stream.pause'):
                    # Signal/terminate BEFORE disk, queueing or audit waits.
                    self.bridge._companion_stop_generation+=1
                    if kind=='stream.stop':self.bridge._phone_viewing_barrier+=1
                    self.bridge.stream_stop.notify_stop('Paired phone Stop' if kind=='stream.stop' else 'Phone background pause')
                    stop_recent=[t for t in stop_recent if now-t<60]
                    if len(stop_recent)>=60:raise ValueError('Companion STOP rate limit')
                    stop_recent.append(now);queue=stopping
                elif kind.startswith('input.'):
                    input_recent=[t for t in input_recent if now-t<1]
                    if len(input_recent)>=60:
                        await socket.send(json.dumps({'id':identifier,'type':'error','message':'Phone input rate limit; request was not admitted'}))
                        continue
                    input_recent.append(now);queue=ordinary
                else:
                    recent=[t for t in recent if now-t<60]
                    if len(recent)>=120:
                        await socket.send(json.dumps({'id':identifier,'type':'error','message':'Companion rate limit; request was not admitted'}))
                        continue
                    recent.append(now);queue=ordinary
                try:queue.put_nowait((value,received_at,self.bridge._companion_stop_generation))
                except asyncio.QueueFull:
                    # Saturation refuses ordinary work without blocking the
                    # reader that must still receive a later authenticated STOP.
                    await socket.send(json.dumps({'id':identifier,'type':'error','message':'Companion queue busy; request was not admitted'}))
        except Exception:
            await socket.close(1008,'Companion connection ended; check pairing and PC settings')
        finally:
            for worker in workers:worker.cancel()
            if workers:await asyncio.gather(*workers,return_exceptions=True)
            if 'identity_valid' in locals():
                service=self.bridge.phone_input
                if service.session and service.session['owner'] is identity_valid:await asyncio.to_thread(service.end,'Phone disconnected')

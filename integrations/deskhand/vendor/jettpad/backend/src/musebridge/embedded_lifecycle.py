"""Local operator-owned lifecycle for the staged embedded connection.

This is not a bridge-client operation. The application must provide a manager
factory which verifies private storage, paired identity, STOP and firewall.
"""
import asyncio
import ipaddress
from urllib.parse import urlsplit
from peer_binding import validate


class EmbeddedLifecycle:
    def __init__(self, manager_factory, persist_binding):
        if not callable(manager_factory) or not callable(persist_binding):
            raise ValueError('Local connection setup unavailable')
        self._factory=manager_factory
        self._persist_binding=persist_binding
        self._task=None;self._manager=None;self._lock=asyncio.Lock()
        self._state='off';self._address=None;self._sign_in=None

    async def _login(self,url):
        value=urlsplit(url)
        if (value.scheme!='https' or value.hostname!='login.tailscale.com' or value.username or
                value.password or value.port or value.query or value.fragment or
                not value.path.startswith('/a/')):
            raise ValueError('Invalid local sign-in')
        self._sign_in=url;self._state='sign_in_required'

    async def _ready_address(self,address):
        ip=ipaddress.ip_address(address)
        if ip.version!=4 or ip not in ipaddress.ip_network('100.64.0.0/10'):
            raise ValueError('Invalid private connection address')
        self._address=str(ip);self._sign_in=None;self._state='waiting_for_phone'

    def status_local(self):
        return {'state':self._state,'address':self._address,'sign_in_url':self._sign_in}

    async def begin_local(self,binding,*,locally_approved):
        binding=validate(binding)
        if locally_approved is not True:raise PermissionError('Local approval required')
        async with self._lock:
            if self._task is not None:raise ValueError('Connection already started')
            manager=self._factory(binding,self._login,self._ready_address)
            self._manager=manager;self._state='starting';self._address=self._sign_in=None
            self._task=asyncio.create_task(self._run(manager,binding))
        return {'state':'starting'}

    async def _run(self,manager,binding):
        try:
            await manager.start(locally_approved=True)
            # Only persist after the child has independently checked the
            # tailnet peer and the existing certificate challenge completed.
            if not manager.transport.authorized():raise PermissionError('Phone pairing unavailable')
            await asyncio.to_thread(self._persist_binding,binding)
            if not manager.transport.authorized():raise PermissionError('Phone pairing unavailable')
            self._state='connected'
            await manager.transport.closed.wait()
            if self._state!='stopping':self._state='disconnected'
        except asyncio.CancelledError:
            raise
        except Exception as error:
            import logging
            logging.getLogger('musebridge.embedded').warning('built-in connection failed: %s',type(error).__name__)
            self._state='unavailable';self._sign_in=None
        finally:
            try:await manager.close()
            finally:
                if self._manager is manager:
                    self._manager=None;self._task=None

    async def stop_local(self):
        async with self._lock:
            task=self._task;manager=self._manager
            self._state='stopping';self._sign_in=None
            if manager is not None:await manager.close()
            if task is not None:
                task.cancel()
                await asyncio.gather(task,return_exceptions=True)
            self._task=self._manager=None;self._state='off';self._address=None
        return {'state':'off'}

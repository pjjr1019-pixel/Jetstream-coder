from __future__ import annotations
from dataclasses import dataclass
import ipaddress
from pathlib import Path

TAILNET = ipaddress.ip_network('100.64.0.0/10')
TCP_STREAM_PORTS = (47984,47989,48010)
UDP_STREAM_PORTS = (47998,47999,48000)
ADMIN_PORT = 47990
WARNING = ('Everything visible on the selected desktop is streamed, including Jett Pad/control '
           'and sensitive windows. Minimize sensitive windows before streaming.')

def address(value: str) -> str:
    parsed=ipaddress.ip_address(value)
    if parsed.version!=4 or parsed not in TAILNET or str(parsed)!=value:
        raise ValueError('A numeric Tailscale IPv4 address is required')
    return str(parsed)

def other_addresses(allowed:tuple[str,...]) -> list[str]:
    ranges=[ipaddress.ip_network('0.0.0.0/0')]
    for value in allowed:
        host=ipaddress.ip_network(address(value)+'/32')
        result=[]
        for network in ranges:
            result.extend(network.address_exclude(host) if host.subnet_of(network) else [network])
        ranges=result
    return [str(network) for network in ranges]+['::/0']

def firewall_address_ranges(allowed:tuple[str,...]) -> list[str]:
    """Equivalent explicit ranges, avoiding provider-specific CIDR rejection."""
    return [str(n.network_address) if n.num_addresses==1 else
            f'{n.network_address}-{n.broadcast_address}'
            for n in map(ipaddress.ip_network,other_addresses(allowed))]

@dataclass(frozen=True)
class EndpointPolicy:
    local: str
    phone: str
    interface: str
    executable: Path

    def __post_init__(self):
        address(self.local);address(self.phone)
        if self.local==self.phone:raise ValueError('Phone and PC must have different addresses')
        if not self.interface or any(c in self.interface for c in '\r\n\0'):
            raise ValueError('An exact Tailscale interface is required')
        if not self.executable.is_absolute():raise ValueError('A fixed absolute executable path is required')

    def expected_sockets(self) -> set[tuple[str,str,int]]:
        return {('tcp',self.local,p) for p in (*TCP_STREAM_PORTS,ADMIN_PORT)} | {('udp',self.local,p) for p in UDP_STREAM_PORTS}

    def validate_observed_sockets(self, sockets: set[tuple[str,str,int]]) -> None:
        # Missing sockets during startup are allowed; extra/wildcard/IPv6 sockets are not.
        if not sockets.issubset(self.expected_sockets()):
            raise ValueError('Unexpected Sunshine socket/address; stop the server')

    def firewall_plan(self) -> list[dict]:
        """Data only. Installation requires a separate local administrator action."""
        base={'Program':str(self.executable),'LocalAddress':self.local,'InterfaceAlias':self.interface,'Profile':'Any'}
        rules=[]
        for protocol,ports in (('TCP',TCP_STREAM_PORTS),('UDP',UDP_STREAM_PORTS)):
            rules.append({**base,'Name':'JettPad.Sunshine.Phone.'+protocol,'Direction':'Inbound','Action':'Allow',
                          'Protocol':protocol,'LocalPort':list(ports),'RemoteAddress':self.phone})
        rules.append({**base,'Name':'JettPad.Sunshine.Admin.Self','Direction':'Inbound','Action':'Allow',
                      'Protocol':'TCP','LocalPort':[ADMIN_PORT],'RemoteAddress':self.local})
        for direction in ('Inbound','Outbound'):
            rules.append({'Program':str(self.executable),'Name':'JettPad.Sunshine.OtherPeers.'+direction,
                          'Direction':direction,'Action':'Block','Profile':'Any','Protocol':'Any',
                          'RemoteAddress':firewall_address_ranges((self.local,self.phone))})
        rules.append({**base,'Name':'JettPad.Sunshine.Admin.NoPhone','Direction':'Inbound','Action':'Block',
                      'Protocol':'TCP','LocalPort':[ADMIN_PORT],'RemoteAddress':self.phone})
        # Outbound rules scope discovery suppression to the Sunshine executable.
        # Windows DNS-SD may emit as SYSTEM: these rules alone are NEVER proof of no leak.
        rules.append({'Program':str(self.executable),'Name':'JettPad.Sunshine.NoMDNS.Out',
                      'Direction':'Outbound','Action':'Block','Protocol':'UDP','RemotePort':[5353],'Profile':'Any'})
        rules.append({'Program':str(self.executable),'Name':'JettPad.Sunshine.NoMDNS.In',
                      'Direction':'Inbound','Action':'Block','Protocol':'UDP','LocalPort':[5353],'Profile':'Any'})
        return rules

def sunshine_configuration(policy: EndpointPolicy, private: Path) -> str:
    if not private.is_absolute():raise ValueError('Private configuration path must be absolute')
    def path(name):
        value=str(private/name).replace('\\','/')
        if any(c in value for c in '\r\n\0'):raise ValueError('Invalid configuration path')
        return value
    settings={
        'sunshine_name':'Jett Pad Desktop','address_family':'ipv4','bind_address':policy.local,'port':'47989',
        'upnp':'disabled','keyboard':'disabled','mouse':'disabled','controller':'disabled','native_pen_touch':'disabled',
        'stream_audio':'disabled','encoder':'nvenc','capture':'ddx','system_tray':'disabled',
        'dd_configuration_option':'disabled',
        'origin_web_ui_allowed':'lan', # Also verify PC-self classification at the live gate; never widen silently.
        'credentials_file':path('admin.json'),'file_state':path('sunshine-state.json'),
        'pkey':path('host.key'),'cert':path('host.crt'),'log_path':path('sunshine.log'),'file_apps':path('apps.json'),
    }
    return '\n'.join(f'{key} = {value}' for key,value in settings.items())+'\n'

"""Read packet payloads from captured NIC PCAPNG; no capture socket/listener."""
import struct
from pathlib import Path

def analyze(path:Path,markers=()):
    data=path.read_bytes();offset=0;endian='<';count=0;matches=[];interfaces=0
    controls={marker:[] for marker in markers}
    while offset<len(data):
        if offset+12>len(data):raise ValueError('Truncated packet capture')
        if data[offset:offset+4]==b'\x0a\x0d\x0d\x0a':
            magic=data[offset+8:offset+12]
            endian='<' if magic==b'\x4d\x3c\x2b\x1a' else '>' if magic==b'\x1a\x2b\x3c\x4d' else None
            if endian is None:raise ValueError('Invalid PCAPNG section')
        kind,length=struct.unpack_from(endian+'II',data,offset)
        if length<12 or length%4 or offset+length>len(data) or struct.unpack_from(endian+'I',data,offset+length-4)[0]!=length:
            raise ValueError('Incomplete packet capture block')
        if kind==1:interfaces+=1
        if kind==6:
            if length<32:raise ValueError('Invalid enhanced packet')
            interface,hi,lo,captured,original=struct.unpack_from(endian+'IIIII',data,offset+8)
            if 28+captured>length-4:raise ValueError('Truncated enhanced packet payload')
            payload=data[offset+28:offset+28+captured];count+=1
            # Do not depend on the sender being Sunshine: Windows DNS-SD may be SYSTEM-owned.
            lowered=payload.lower()
            for marker in controls:
                if marker.encode('ascii').lower() in lowered:controls[marker].append(interface)
            if b'_nvstream' in lowered or b'sunshine' in lowered:
                matches.append({'packet':count,'interface_id':interface,'timestamp_raw':(hi<<32)|lo})
        offset+=length
    if not interfaces:raise ValueError('No NIC capture interfaces recorded')
    return {'packet_count':count,'interface_count':interfaces,'sunshine_announcements':matches,'no_announcements':not matches,'positive_controls':controls,
            'scope':'Packet Monitor NIC UDP5353 capture; capture runtime/interface list must also be verified'}

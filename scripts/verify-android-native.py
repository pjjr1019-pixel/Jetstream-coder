"""Verify APK native LOAD segments and uncompressed ZIP entries on 16KB devices.
Based on https://developer.android.com/guide/practices/page-sizes.
Does not patch libraries or suppress Android compatibility warnings.
"""
import argparse
import json
import struct
import zipfile
from pathlib import Path

PAGE=16384

def inspect_elf(data):
    if data[:4] != b'\x7fELF' or data[5] not in (1,2):
        raise ValueError('Invalid ELF header')
    endian='<' if data[5]==1 else '>'
    bits=data[4]
    if bits==2:
        phoff=struct.unpack_from(endian+'Q',data,32)[0]
        size,count=struct.unpack_from(endian+'HH',data,54)
    elif bits==1:
        phoff=struct.unpack_from(endian+'I',data,28)[0]
        size,count=struct.unpack_from(endian+'HH',data,42)
    else:
        raise ValueError('Unsupported ELF class')
    loads=[]
    for index in range(count):
        at=phoff+index*size
        kind=struct.unpack_from(endian+'I',data,at)[0]
        if kind!=1:
            continue
        if bits==2:
            offset,address=struct.unpack_from(endian+'QQ',data,at+8)
            align=struct.unpack_from(endian+'Q',data,at+48)[0]
        else:
            offset,address=struct.unpack_from(endian+'II',data,at+4)
            align=struct.unpack_from(endian+'I',data,at+28)[0]
        loads.append({'alignment':align,'congruent':offset%PAGE==address%PAGE})
    if not loads:
        raise ValueError('No LOAD segments')
    return {'bits':64 if bits==2 else 32,'loads':loads,'compatible':all(p['alignment']>=PAGE and p['congruent'] for p in loads)}

def inspect_archive(file):
    rows=[]
    with zipfile.ZipFile(file) as archive,open(file,'rb') as raw:
        for item in archive.infolist():
            if not item.filename.endswith('.so'):
                continue
            result=inspect_elf(archive.read(item))
            raw.seek(item.header_offset)
            header=raw.read(30)
            name,extra=struct.unpack_from('<HH',header,26)
            offset=item.header_offset+30+name+extra
            # AAR entries are repacked by AGP. APK uncompressed native entries
            # must be aligned in the final signed artifact as well as in ELF.
            zip_ok=Path(file).suffix.lower()!='.apk' or item.compress_type!=zipfile.ZIP_STORED or offset%PAGE==0
            rows.append({'library':item.filename,**result,'zipAligned':zip_ok})
    if not rows:
        raise ValueError('No native libraries found')
    return rows

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('archive');args=parser.parse_args()
    rows=inspect_archive(args.archive)
    print(json.dumps({'artifact':args.archive,'libraries':rows},indent=2))
    raise SystemExit(0 if all(row['compatible'] and row['zipAligned'] for row in rows if row['bits']==64) else 1)

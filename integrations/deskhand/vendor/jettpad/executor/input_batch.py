"""Submit balanced down/up pairs together, avoiding held keys on helper kill."""
import ctypes
from ctypes import wintypes as w
from musebridge.windows import api

class KEY(ctypes.Structure):
    _fields_=[('vk',w.WORD),('scan',w.WORD),('flags',w.DWORD),('time',w.DWORD),('extra',ctypes.c_size_t)]
class MOUSE(ctypes.Structure):
    _fields_=[('x',w.LONG),('y',w.LONG),('data',w.DWORD),('flags',w.DWORD),('time',w.DWORD),('extra',ctypes.c_size_t)]
class DATA(ctypes.Union):_fields_=[('key',KEY),('mouse',MOUSE)]
class INPUT(ctypes.Structure):_fields_=[('kind',w.DWORD),('data',DATA)]

def send(items):
    u,_=api();u.SendInput.argtypes=[w.UINT,ctypes.POINTER(INPUT),ctypes.c_int];u.SendInput.restype=w.UINT
    values=(INPUT*len(items))(*items)
    if u.SendInput(len(items),values,ctypes.sizeof(INPUT))!=len(items):raise ValueError('Windows blocked input; outcome unverified')

def key(value):
    parts=(value if isinstance(value,list) else value.lower().split('+'))
    parts=[p.lower() for p in parts]
    if set(parts) in ({'alt','tab'},{'alt','escape'},{'ctrl','escape'}):raise ValueError('Use an explicit permitted window activation instead of global window switching')
    mapping=dict(enter=13,tab=9,escape=27,up=38,down=40,left=37,right=39,home=36,end=35,pageup=33,pagedown=34,backspace=8,delete=46,space=32,ctrl=17,alt=18,shift=16)
    mapping.update({'f'+str(i):111+i for i in range(1,13)})
    if not parts or len(parts)>4 or len(set(parts))!=len(parts) or sum(p in ('ctrl','alt','shift') for p in parts)>2:raise ValueError('Unsupported desktop key')
    codes=[]
    for p in parts:
        if p in mapping:codes.append(mapping[p])
        elif len(p)==1 and p.isascii() and p.isalnum():codes.append(ord(p.upper()))
        else:raise ValueError('Unsupported desktop key')
    def event(code,up=False):return INPUT(1,DATA(key=KEY(code,0,(2 if up else 0)|(1 if code in (33,34,35,36,37,38,39,40,45,46) else 0),0,0)))
    send([event(code) for code in codes]+[event(code,True) for code in reversed(codes)])

def click(x,y,button='left'):
    flags={'left':(2,4),'right':(8,16),'middle':(32,64)}
    if button not in flags:raise ValueError('Unsupported mouse button')
    u,_=api();left,top,width,height=[u.GetSystemMetrics(n) for n in (76,77,78,79)]
    if not left<=x<left+width or not top<=y<top+height:raise ValueError('Point outside desktop')
    def mouse(dx,dy,bits):return INPUT(0,DATA(mouse=MOUSE(dx,dy,0,bits,0,0)))
    send([mouse(round((x-left)*65535/max(1,width-1)),round((y-top)*65535/max(1,height-1)),0xC001),mouse(0,0,flags[button][0]),mouse(0,0,flags[button][1])])

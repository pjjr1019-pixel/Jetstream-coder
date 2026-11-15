"""One-frame, memory-only permitted-window compositor. Never captures a display DC.

Only explicit ordinary HWNDs are painted. Protected windows are never passed
to PrintWindow. Their rectangles are black in the final output.
"""
import base64,ctypes,io,json,math,struct,sys
from ctypes import wintypes as w
from PIL import Image,ImageDraw
from musebridge.windows import api
from musebridge.window_targets import read_window,desktop_protection
from musebridge.redaction import redact_text
import os


def reason(info,operators):
    value=info.get('protection_reason')
    return 'operator_window' if info['pid'] in operators else value if value!='unknown' else None


def snapshot(operators):
    if desktop_protection() is not None:raise ValueError('Interactive desktop unavailable')
    u,_=api();rows=[];failures=[]
    callback_type=ctypes.WINFUNCTYPE(w.BOOL,w.HWND,w.LPARAM)
    def visit(hwnd,unused):
        if len(rows)>=512:return False
        if u.IsWindowVisible(hwnd):
            try:
                info=read_window(hwnd)
                if info['state']!='minimized':
                    info['protected']=reason(info,operators);rows.append(info)
            except (ValueError,OSError):failures.append(hwnd)
        return True
    callback=callback_type(visit);u.EnumWindows.argtypes=[callback_type,w.LPARAM]
    u.IsWindowVisible.argtypes=[w.HWND];u.EnumWindows(callback,0)
    if failures or len(rows)>=512:raise ValueError('Window layout unavailable; frame withheld')
    return rows


def protected_signature(rows):
    return sorted((i['hwnd'],i['pid'],tuple(i['rect']),i['protected']) for i in rows if i['protected'])


def intersect(a,b):
    result=(max(a[0],b[0]),max(a[1],b[1]),min(a[2],b[2]),min(a[3],b[3]))
    return result if result[0]<result[2] and result[1]<result[3] else None


def compose(rows,bounds,capture,*,max_edge=1280,excluded=()):
    width,height=bounds[2]-bounds[0],bounds[3]-bounds[1]
    if width<=0 or height<=0 or width>32768 or height>32768:raise ValueError('Unsupported desktop bounds')
    scale=min(1.,max_edge/max(width,height));size=(max(1,round(width*scale)),max(1,round(height*scale)))
    canvas=Image.new('RGB',size,'black');covered=Image.new('1',size,0);selected=[];unavailable=[]
    def box(rect):
        return (max(0,math.floor((rect[0]-bounds[0])*scale)),max(0,math.floor((rect[1]-bounds[1])*scale)),
                min(size[0],math.ceil((rect[2]-bounds[0])*scale)),min(size[1],math.ceil((rect[3]-bounds[1])*scale)))
    # Top to bottom: draw only uncovered visible regions. No lower-window leak
    # when a foreground window is protected, unsupported or skipped.
    for info in rows:
        clip=intersect(bounds,info['rect'])
        if clip is None:continue
        area=box(clip)
        if area[0]>=area[2] or area[1]>=area[3]:continue
        if covered.crop(area).getextrema()==(1,1):continue
        masked=bool(info.get('protected')) or info['hwnd'] in excluded
        if info['hwnd'] in excluded:unavailable.append(info['hwnd'])
        if not masked:
            try:
                image=capture(info)
                if image.size!=(info['rect'][2]-info['rect'][0],info['rect'][3]-info['rect'][1]):raise ValueError('Window resized')
                crop=image.crop((clip[0]-info['rect'][0],clip[1]-info['rect'][1],clip[2]-info['rect'][0],clip[3]-info['rect'][1]))
                crop=crop.resize((area[2]-area[0],area[3]-area[1]),Image.Resampling.BILINEAR)
                visible=covered.crop(area).point(lambda v:0 if v else 255).convert('L')
                canvas.paste(crop,area[:2],visible);selected.append(info['hwnd'])
            except (ValueError,OSError):unavailable.append(info['hwnd'])
        ImageDraw.Draw(covered).rectangle((area[0],area[1],area[2]-1,area[3]-1),fill=1)
    # Redact ALL protected rectangles, including glass/backdrop edge cases.
    masks=[];draw=ImageDraw.Draw(canvas)
    for info in rows:
        if info.get('protected'):
            clip=intersect(bounds,info['rect'])
            if clip:
                area=box(clip);draw.rectangle(area,fill='black');masks.append(list(area))
    return canvas,selected,unavailable,masks


def capture_window(info):
    u,_=api();g=ctypes.WinDLL('gdi32',use_last_error=True)
    for name,args,result in (
        ('CreateCompatibleDC',[w.HDC],w.HDC),('CreateCompatibleBitmap',[w.HDC,ctypes.c_int,ctypes.c_int],w.HBITMAP),
        ('SelectObject',[w.HDC,w.HANDLE],w.HANDLE),('DeleteObject',[w.HANDLE],w.BOOL),('DeleteDC',[w.HDC],w.BOOL),
        ('GetDIBits',[w.HDC,w.HBITMAP,w.UINT,w.UINT,w.LPVOID,w.LPVOID,w.UINT],ctypes.c_int)):
        fn=getattr(g,name);fn.argtypes=args;fn.restype=result
    u.GetWindowDC.argtypes=[w.HWND];u.GetWindowDC.restype=w.HDC
    u.ReleaseDC.argtypes=[w.HWND,w.HDC];u.PrintWindow.argtypes=[w.HWND,w.HDC,w.UINT]
    width,height=info['rect'][2]-info['rect'][0],info['rect'][3]-info['rect'][1]
    if width<=0 or height<=0 or width*height>32_000_000:raise ValueError('Unsupported window size')
    dc=u.GetWindowDC(info['hwnd']);memory=bitmap=previous=None
    try:
        memory=g.CreateCompatibleDC(dc);bitmap=g.CreateCompatibleBitmap(dc,width,height)
        if not dc or not memory or not bitmap:raise ValueError('Capture allocation unavailable')
        previous=g.SelectObject(memory,bitmap)
        # Never fall back to screen BitBlt or foreground activation for MMC.
        if not u.PrintWindow(info['hwnd'],memory,2):raise ValueError('Window capture unavailable')
        g.SelectObject(memory,previous);previous=None
        header=struct.pack('<IiiHHIIiiII',40,width,-height,1,32,0,width*height*4,0,0,0,0)
        buffer=ctypes.create_string_buffer(width*height*4);bitmap_info=ctypes.create_string_buffer(header+b'\0'*12)
        if g.GetDIBits(memory,bitmap,0,height,buffer,bitmap_info,0)!=height:raise ValueError('Capture unavailable')
        image=Image.frombytes('RGB',(width,height),buffer.raw,'raw','BGRX')
        if image.getextrema()==((0,0),(0,0),(0,0)):raise ValueError('Application returned an empty frame')
        return image
    finally:
        if previous:g.SelectObject(memory,previous)
        if bitmap:g.DeleteObject(bitmap)
        if memory:g.DeleteDC(memory)
        if dc:u.ReleaseDC(info['hwnd'],dc)


def run(request):
    operators=set(request['operators']);before=snapshot(operators)
    by_handle={i['hwnd']:i for i in before};allowed={item['hwnd']:item['pid'] for item in request['allowed']}
    rows=[]
    for info in before:
        if not info['protected'] and info['hwnd'] in request.get('decorations',[]):continue
        if info['protected'] or info['hwnd'] in allowed:rows.append(info)
        elif request['mode']=='desktop':
            # A newly opened window is never replaced with pixels from beneath.
            info=dict(info,protected='unobserved_window');rows.append(info)
    if request['mode']=='window':
        target=by_handle.get(request['target'])
        if target is None or target['protected']:raise ValueError('Target unavailable or protected')
        bounds=target['rect'];rows=[target]+[i for i in rows if i['protected'] and i['hwnd']!=target['hwnd']]
    else:bounds=request['bounds']
    def capture(info):
        fresh=read_window(info['hwnd'])
        if fresh['pid']!=allowed.get(info['hwnd']) or reason(fresh,operators) or fresh['rect']!=info['rect']:
            raise ValueError('Window changed or protected')
        print(json.dumps({'event':'capture.source','target_hwnd':info['hwnd'],'process_name':os.path.basename(info['path']),'title':redact_text(info['title'])[:160],'target_protected':False}),flush=True)
        image=capture_window(fresh);after=read_window(info['hwnd'])
        if after['pid']!=fresh['pid'] or reason(after,operators) or after['rect']!=fresh['rect']:
            raise ValueError('Window changed or protected')
        return image
    image,selected,unavailable,masks=compose(rows,bounds,capture,max_edge=request['max_edge'],excluded=request.get('excluded',[]))
    after=snapshot(operators)
    if protected_signature(before)!=protected_signature(after):raise ValueError('Protected layout changed; frame withheld')
    # Refuse a whole composed frame if stacking/layout changed during capture;
    # ignored decorative Orb motion never participates in this comparison.
    def layout(items):
        return [(i['hwnd'],i['pid'],i['rect']) for i in items if i['protected'] or i['hwnd'] not in request.get('decorations',[])]
    if layout(before)!=layout(after):raise ValueError('Window layout changed; retrying live')
    output=io.BytesIO();image.save(output,format='JPEG',quality=request['quality'],subsampling=0)
    raw=output.getvalue()
    if len(raw)>512*1024:raise ValueError('Frame exceeds live budget')
    return dict(data_b64=base64.b64encode(raw).decode('ascii'),width=image.width,height=image.height,
                sources=selected,unavailable=unavailable,protected_regions=masks,bounds=bounds,
                all_unavailable=not selected)


if __name__=='__main__':
    try:result={'result':run(json.loads(sys.stdin.read(128*1024)))}
    except Exception as error:result={'error':str(error)[:200]}
    print(json.dumps(result),flush=True)


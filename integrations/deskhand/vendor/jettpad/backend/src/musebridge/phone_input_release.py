"""Release-only companion for an interrupted drag. No input-down operation."""
import sys
from pathlib import Path
# Standalone child processes do not inherit executor_link's sys.path setup.
sys.path.insert(0,str(Path(__file__).resolve().parents[3]))
def run(source=sys.stdin,out=sys.stdout,release=None):
    if release is None:
        from executor.input_batch import INPUT,DATA,MOUSE,send
        release=lambda:send([INPUT(0,DATA(mouse=MOUSE(0,0,0,4,0,0)))])
    armed=True
    print('ready',file=out,flush=True)
    try:
        for line in source:
            if line.strip()=='disarm':armed=False;break
            if line.strip()=='release':break
    finally:
        if armed:release()
if __name__=='__main__':run()

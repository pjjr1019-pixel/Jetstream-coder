"""One-shot encrypted phone PIN envelope. No persistent storage or logging."""
import base64,json,secrets,time
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

class PinChallenge:
    def __init__(self,clock=time.monotonic):self.clock=clock;self.pending=None
    def issue(self,epoch):
        self.pending=(secrets.token_urlsafe(32),self.clock()+30,epoch)
        return {'challenge':self.pending[0],'expires_in':30}
    def open(self,key,payload,epoch):
        current=self.pending;self.pending=None
        if not current or self.clock()>=current[1] or epoch!=current[2]:raise ValueError('Unlock challenge expired; try again')
        try:
            session=key.decrypt(base64.b64decode(payload['wrapped_key'],validate=True),padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),algorithm=hashes.SHA256(),label=b'JettPad PIN v1'))
            plain=AESGCM(session).decrypt(base64.b64decode(payload['iv'],validate=True),base64.b64decode(payload['sealed'],validate=True),b'JettPad PIN v1')
            value=json.loads(plain)
            if value.get('challenge')!=current[0]:raise ValueError('Invalid unlock challenge')
            pin=value.get('pin')
            if not isinstance(pin,str) or not pin.isascii() or not pin.isalnum() or not 6<=len(pin)<=128:raise ValueError('Invalid PIN format')
            return pin
        except Exception:raise ValueError('Invalid encrypted unlock request') from None
        finally:payload.clear()

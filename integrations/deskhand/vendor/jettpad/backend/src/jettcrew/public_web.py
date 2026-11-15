"""Optional public-page retrieval. IP-pinned requests; no cookies or auth."""
import http.client
import ipaddress
import socket
from html.parser import HTMLParser
from urllib.parse import urlsplit,urljoin


class Page(HTMLParser):
    def __init__(self):super().__init__();self.skip=0;self.text=[];self.links=[]
    def handle_starttag(self,tag,attrs):
        if tag in ('script','style','noscript'):self.skip+=1
        if tag=='a':
            href=dict(attrs).get('href')
            if href:self.links.append(href)
    def handle_endtag(self,tag):
        if tag in ('script','style','noscript'):self.skip=max(0,self.skip-1)
    def handle_data(self,text):
        if not self.skip and text.strip():self.text.append(text.strip())


def public_target(url):
    value=urlsplit(url)
    if value.scheme not in ('http','https') or not value.hostname or value.username or value.password:
        raise ValueError('Only unauthenticated public HTTP(S) URLs are supported')
    port=value.port or (443 if value.scheme=='https' else 80)
    if port not in (80,443):raise ValueError('Only public web ports 80/443 are allowed')
    addresses=socket.getaddrinfo(value.hostname,port,type=socket.SOCK_STREAM)
    ips=[row[4][0] for row in addresses]
    if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
        raise ValueError('Non-public web address refused')
    return value,port,ips[0]


def fetch(url):
    for _ in range(4):
        value,port,ip=public_target(url)
        cls=http.client.HTTPSConnection if value.scheme=='https' else http.client.HTTPConnection
        connection=cls(value.hostname,port,timeout=12)
        # Connect only to the public address already checked, preserving HTTPS SNI.
        connection._create_connection=lambda address,timeout,source_address=None:socket.create_connection((ip,port),timeout,source_address)
        try:
            connection.request('GET',(value.path or '/')+('?' + value.query if value.query else ''),
                headers={'User-Agent':'JettLocalCrew/0.1 public research','Accept':'text/html,text/plain','Accept-Encoding':'identity'})
            response=connection.getresponse()
            if response.status in (301,302,303,307,308):
                location=response.getheader('Location')
                if not location:raise ValueError('Redirect missing Location')
                url=urljoin(url,location);continue
            if response.status!=200:raise ValueError('Public page HTTP '+str(response.status)+'; no authentication/barrier workaround')
            if response.getheader('Content-Encoding','identity')!='identity':raise ValueError('Compressed page not supported')
            kind=response.getheader('Content-Type','').split(';')[0]
            if kind not in ('text/html','text/plain'):raise ValueError('Public page is not HTML/text')
            raw=response.read(512*1024+1)
            if len(raw)>512*1024:raise ValueError('Public page exceeds 512 KiB')
            text=raw.decode('utf-8',errors='replace')
            if kind=='text/plain':return url,text,[]
            page=Page();page.feed(text)
            return url,'\n'.join(page.text),[urljoin(url,link) for link in page.links]
        finally:connection.close()
    raise ValueError('Public page redirect limit exceeded')


def collect(urls,journal,task_id):
    docs=[];seen=set();pending=list(urls);discovered=0
    while pending and len(docs)<6:
        url=pending.pop(0)
        if url in seen:continue
        seen.add(url)
        final,text,links=fetch(url);docs.append((final,text[:48000]))
        journal('crew.web','Retrieved public page '+final,task_id)
        # At most two linked pages total, on the same host as a supplied page.
        if url in urls:
            for link in links:
                if discovered>=2:break
                parsed=urlsplit(link)
                if parsed.hostname==urlsplit(final).hostname and parsed.scheme in ('http','https') and link not in seen and link not in pending:
                    pending.append(link);discovered+=1
    return docs

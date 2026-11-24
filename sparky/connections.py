"""Bounded idle connection reuse; a lease is exclusive to one request."""
import http.client
import io
import select
import threading
import time
from urllib import request, error, parse

class IdlePool:
    def __init__(self,limit=4,ttl=45):
        self.limit=limit;self.ttl=ttl;self.idle=[];self.lock=threading.Lock()
    def take(self,key):
        now=time.monotonic();chosen=None
        with self.lock:
            kept=[]
            for saved,stamp,connection in self.idle:
                if now-stamp>self.ttl:connection.close()
                elif saved==key and chosen is None:chosen=connection
                else:kept.append((saved,stamp,connection))
            self.idle=kept
        return chosen
    def put(self,key,connection):
        with self.lock:
            self.idle.append((key,time.monotonic(),connection))
            if len(self.idle)>self.limit:self.idle.pop(0)[2].close()
    def clear(self):
        with self.lock:
            for _,_,connection in self.idle:connection.close()
            self.idle=[]
    def available(self,key):
        with self.lock:
            return sum(saved==key and time.monotonic()-stamp<=self.ttl for saved,stamp,_ in self.idle)

http_pool=IdlePool()
speech_pool=IdlePool(ttl=90)

def urlopen(req,timeout,context):
    """Reuse official HTTPS connections, including an existing HTTP CONNECT proxy."""
    url=parse.urlsplit(req.full_url)
    if url.scheme!='https' or url.netloc!='api.openai.com':
        return request.urlopen(req,timeout=timeout,context=context)
    proxy_url=request.getproxies().get('https','')
    proxy=parse.urlsplit(proxy_url if '://' in proxy_url else '//'+proxy_url)
    if proxy_url and (proxy.scheme not in ('','http') or proxy.username or proxy.password):
        return request.urlopen(req,timeout=timeout,context=context)
    if request.proxy_bypass(url.hostname):proxy_url=''
    key=(url.netloc,proxy_url,req.get_header('Authorization'))
    connection=http_pool.take(key)
    # An idle readable socket means unsolicited data or an upstream close.
    if connection and (not connection.sock or select.select([connection.sock],[],[],0)[0]):
        connection.close();connection=None
    if connection is None:
        if proxy_url:
            connection=http.client.HTTPSConnection(proxy.hostname,proxy.port or 80,timeout=timeout,context=context)
            connection.set_tunnel(url.hostname,443)
        else:connection=http.client.HTTPSConnection(url.netloc,timeout=timeout,context=context)
    else:
        connection.timeout=timeout
        connection.sock.settimeout(timeout)
    try:
        path=parse.urlunsplit(('', '',url.path,url.query,''))
        connection.request(req.get_method(),path,body=req.data,headers=dict(req.header_items()))
        response=connection.getresponse();data=response.read(131073)
        if len(data)>131072:raise ValueError('Upstream response too large')
        status,headers=response.status,response.headers
        if not response.will_close:http_pool.put(key,connection);connection=None
        if status>=400:raise error.HTTPError(req.full_url,status,'Upstream request failed',headers,io.BytesIO(data))
        return io.BytesIO(data)
    except http.client.HTTPException as exc:
        # Never retry an ambiguously submitted POST automatically.
        raise OSError('Upstream connection failed') from exc
    finally:
        if connection is not None:connection.close()

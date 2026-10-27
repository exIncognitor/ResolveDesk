"""Isolated upload/parser regression; never sends inputs to a live service."""
import asyncio
import tempfile
import threading

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest
from starlette.datastructures import UploadFile
from starlette.requests import Request
from starlette.staticfiles import StaticFiles

from resolvedesk.imports import MAX_IMPORT_BYTES,read_upload
from resolvedesk.store import Store
from resolvedesk.retrieval import HybridKnowledge
from resolvedesk.agent import DeskAgent
from resolvedesk.app import create_app
from test_resolvedesk import SUPPORT,client_with


def upload_request(body,*,content_type='multipart/form-data; boundary=boundary',length=None,chunk=4096):
    headers=[] if content_type is None else [(b'content-type',content_type.encode())]
    if length is not None:headers.append((b'content-length',str(length).encode()))
    position=0
    async def receive():
        nonlocal position
        data=body[position:position+chunk];position+=len(data)
        return {'type':'http.request','body':data,'more_body':position<len(body)}
    return Request({'type':'http','method':'POST','path':'/knowledge/upload','headers':headers},receive)


def multipart(data=b'hello',*,tail=b'--boundary--\r\n'):
    return b'--boundary\r\nContent-Disposition: form-data; name="file"; filename="a.md"\r\nContent-Type: text/markdown\r\n\r\n'+data+b'\r\n'+tail


def track_spools(monkeypatch,tmp_path,*,write_error=False):
    files=[];writes=[]
    class Tracked(tempfile.SpooledTemporaryFile):
        def __init__(self,*args,**kwargs):
            kwargs['dir']=tmp_path
            super().__init__(*args,**kwargs);files.append(self)
        def write(self,data):
            writes.append((threading.get_ident(),getattr(self,'_rolled',False),len(data)))
            if write_error:raise OSError('synthetic spool write failure')
            return super().write(data)
    monkeypatch.setattr('starlette.formparsers.SpooledTemporaryFile',Tracked)
    return files,writes


@pytest.mark.asyncio
@pytest.mark.parametrize('media',[None,'application/json','application/x-www-form-urlencoded','text/plain'])
async def test_other_media_is_rejected_before_read_or_form_parser(monkeypatch,media):
    touched=[]
    async def forbidden(self):
        touched.append(True)
        raise AssertionError('Unexpected body read')
        yield b''  # Keep the receive contract an async iterator in baseline probes.
    monkeypatch.setattr(Request,'stream',forbidden)
    with pytest.raises(HTTPException) as error:await read_upload(upload_request(b'a=1&b=2',content_type=media),SUPPORT)
    assert error.value.status_code==415 and not touched


@pytest.mark.asyncio
async def test_malformed_part_after_file_creation_closes_spool(monkeypatch,tmp_path):
    files,_=track_spools(monkeypatch,tmp_path)
    body=multipart(tail=b'--boundary\r\ninvalid header without colon\r\n\r\nx\r\n--boundary--\r\n')
    with pytest.raises(HTTPException) as error:await read_upload(upload_request(body),SUPPORT)
    assert error.value.status_code==400
    assert files and all(f.closed for f in files)


@pytest.mark.asyncio
@pytest.mark.parametrize('tail',[b'',b'--boundary\r\n'])
async def test_truncation_never_imports_partial_form_and_closes_spool(monkeypatch,tmp_path,tail):
    files,_=track_spools(monkeypatch,tmp_path)
    with pytest.raises(HTTPException) as error:await read_upload(upload_request(multipart(tail=tail)),SUPPORT)
    assert error.value.status_code==400
    assert files and all(f.closed for f in files)


@pytest.mark.asyncio
async def test_parse_stage_cancellation_propagates_and_closes_spool(monkeypatch,tmp_path):
    files,_=track_spools(monkeypatch,tmp_path)
    async def cancel(self,data):raise asyncio.CancelledError()
    monkeypatch.setattr(UploadFile,'write',cancel)
    with pytest.raises(asyncio.CancelledError):await read_upload(upload_request(multipart()),SUPPORT)
    assert files and all(f.closed for f in files)


@pytest.mark.asyncio
async def test_spool_io_failure_propagates_and_closes_spool(monkeypatch,tmp_path):
    files,_=track_spools(monkeypatch,tmp_path,write_error=True)
    with pytest.raises(OSError):await read_upload(upload_request(multipart()),SUPPORT)
    assert files and all(f.closed for f in files)


@pytest.mark.asyncio
async def test_rollover_write_uses_worker_thread_and_normal_upload_is_preserved(monkeypatch,tmp_path):
    files,writes=track_spools(monkeypatch,tmp_path)
    event_thread=threading.get_ident();size=1200*1024
    docs=await read_upload(upload_request(multipart(b'x'*size),chunk=65536),SUPPORT)
    assert len(docs)==1 and docs[0]['title']=='a' and len(docs[0]['content'])==size
    total=0;rollover_threads=[]
    for thread,rolled,count in writes:
        if not rolled and total+count>1024*1024:rollover_threads.append(thread)
        total+=count
    assert rollover_threads and all(t!=event_thread for t in rollover_threads)
    assert files and all(f.closed for f in files)


@pytest.mark.asyncio
@pytest.mark.parametrize('length',[None,-1,1])
async def test_actual_received_bytes_limit_does_not_trust_content_length(monkeypatch,tmp_path,length):
    files,_=track_spools(monkeypatch,tmp_path)
    with pytest.raises(HTTPException) as error:
        await read_upload(upload_request(b'x'*(MAX_IMPORT_BYTES+65537),length=length,chunk=65536),SUPPORT)
    assert error.value.status_code==413 and not files


def test_static_lookup_rejects_unc_and_absolute_paths_before_path_resolution(monkeypatch,tmp_path):
    static=StaticFiles(directory=tmp_path);resolved=[]
    def resolve(path):resolved.append(path);return str(tmp_path/'never-read')
    with monkeypatch.context() as context:
        context.setattr('starlette.staticfiles.os.path.realpath',resolve)
        results=[static.lookup_path(path) for path in ('\\\\untrusted.invalid\\share\\x','/outside/x')]
    assert results==[('',None),('',None)] and not resolved


def test_current_static_response_preserves_normal_and_range_requests(monkeypatch,tmp_path):
    import resolvedesk.app as app_module
    directory=tmp_path/'site'/'frontend'/'dist';assets=directory/'assets';assets.mkdir(parents=True)
    (directory/'index.html').write_text('0123456789abcdefghij',encoding='utf-8')
    (assets/'probe.js').write_text('0123456789abcdefghij',encoding='utf-8')
    monkeypatch.setattr(app_module,'ROOT',tmp_path/'site')
    store=Store(tmp_path/'range.sqlite3');kb=HybridKnowledge(store);agent=DeskAgent(store,kb,client_with())
    with TestClient(create_app(store,kb,agent,{})) as client:
        for path in ('/','/assets/probe.js'):
            assert client.get(path).text=='0123456789abcdefghij'
            single=client.get(path,headers={'Range':'bytes=2-5'})
            assert single.status_code==206 and single.text=='2345'
            multiple=client.get(path,headers={'Range':'bytes=0-1,8-9'})
            assert multiple.status_code==206 and 'multipart/byteranges' in (multiple.headers.get('content-type','')+' '+multiple.headers.get('content-range',''))
    store.close()

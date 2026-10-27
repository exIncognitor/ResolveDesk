"""Authenticated, bounded UTF-8/JSON/multipart knowledge imports."""
import hashlib
import json
from pathlib import PureWindowsPath

from fastapi import HTTPException
from pydantic import BaseModel,ConfigDict,Field,ValidationError
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException,MultiPartParser
from resolvedesk.knowledge_governance import KnowledgeMetadata

MAX_IMPORT_BYTES=10*1024*1024
MAX_IMPORT_DOCUMENTS=100


class CompleteMultipartParser(MultiPartParser):
    """Require a completed message, including when a final part was truncated."""
    completed=False

    def on_end(self):
        self.completed=True
        super().on_end()

    async def parse(self):
        form=await super().parse()
        if not self.completed:
            # Starlette 1.10 owns exception/cancellation cleanup. Its parser
            # finalizer does not reject truncation, which may leave a spool
            # outside FormData.items. Close that pinned ownership list as well.
            for spool in self._files_to_close_on_error:spool.close()
            raise MultiPartException('Incomplete multipart body')
        return form


class ImportDocument(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    document_id:str|None=Field(default=None,min_length=1,max_length=100)
    title:str=Field(min_length=1,max_length=200)
    content:str=Field(min_length=1,max_length=MAX_IMPORT_BYTES)
    visibility:str=Field(default='tenant',pattern='^(tenant|private)$')
    metadata:KnowledgeMetadata|None=None


class ImportBatch(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    documents:list[ImportDocument]=Field(min_length=1,max_length=MAX_IMPORT_DOCUMENTS)


async def bounded_body(request,limit):
    data=bytearray()
    async for part in request.stream():
        if len(data)+len(part)>limit:raise HTTPException(413,'Import exceeds the 10 MiB limit')
        data.extend(part)
    return bytes(data)


def documents(value,p):
    try:batch=ImportBatch.model_validate(value)
    except ValidationError:raise HTTPException(422,'Expected 1-100 documents with title, content and optional document_id/visibility') from None
    result=[]
    for item in batch.documents:
        doc=item.model_dump()
        if not item.title.strip() or not item.content.strip() or (item.document_id is not None and not item.document_id.strip()):raise HTTPException(422,'Document fields cannot be blank')
        if doc['document_id'] is None:
            identity=[p.tenant,p.user if doc['visibility']=='private' else '',doc['title'],doc['content']]
            doc['document_id']='import-'+hashlib.sha256(json.dumps(identity,ensure_ascii=False).encode()).hexdigest()[:32]
        result.append(doc)
    if len({d['document_id'] for d in result})!=len(result):raise HTTPException(422,'Duplicate document_id in one batch')
    return result


def json_value(raw):
    try:return json.loads(raw.decode('utf-8-sig'))
    except (UnicodeError,ValueError,RecursionError):raise HTTPException(422,'Invalid UTF-8 JSON') from None


async def read_batch(request,p):
    if request.headers.get('content-type','').split(';')[0].strip().lower()!='application/json':raise HTTPException(415,'Use application/json')
    raw=await bounded_body(request,MAX_IMPORT_BYTES)
    return documents(json_value(raw),p)


async def read_upload(request,p):
    if request.headers.get('content-type','').split(';')[0].strip().lower()!='multipart/form-data':
        raise HTTPException(415,'Use multipart/form-data')
    # Bound the whole multipart body before the framework can spool files.
    # The extra allowance is for MIME headers, never extra document bytes.
    raw=await bounded_body(request,MAX_IMPORT_BYTES+65536)
    async def chunks():
        for offset in range(0,len(raw),65536):yield raw[offset:offset+65536]
    form=None
    try:
        form=await CompleteMultipartParser(request.headers,chunks(),max_files=1,max_fields=0).parse()
        if set(form.keys())!={'file'} or not isinstance(form['file'],UploadFile):raise HTTPException(422,'Upload exactly one file field')
        upload=form['file'];filename=PureWindowsPath(upload.filename or '').name
        extension=PureWindowsPath(filename).suffix.lower()
        if extension not in {'.txt','.md','.json'}:raise HTTPException(415,'Only .txt, .md and .json are supported')
        data=await upload.read(MAX_IMPORT_BYTES+1)
        if len(data)>MAX_IMPORT_BYTES:raise HTTPException(413,'File exceeds 10 MiB')
        if extension=='.json':value={'documents':json_value(data)}
        else:
            try:text=data.decode('utf-8-sig')
            except UnicodeError:raise HTTPException(422,'Text files must be valid UTF-8') from None
            value={'documents':[{'title':PureWindowsPath(filename).stem,'content':text}]}
        return documents(value,p)
    except MultiPartException:
        raise HTTPException(400,'Invalid or incomplete multipart upload') from None
    finally:
        if form is not None:await form.close()

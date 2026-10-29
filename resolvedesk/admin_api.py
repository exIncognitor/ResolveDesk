"""Login and the support-only administration surface."""
from typing import Literal

from fastapi import Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from resolvedesk.identity import demo_enabled


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Login(Input):
    tenant: str = Field(default='demo',pattern=r'^[A-Za-z0-9_.-]{1,64}$')
    username: str = Field(min_length=1,max_length=64)
    password: str = Field(min_length=1,max_length=256)


class Demo(Input):
    role: Literal['user','support']


class Account(Input):
    username: str = Field(pattern=r'^[A-Za-z0-9_.-]{1,64}$')
    password: str = Field(min_length=10,max_length=256)
    role: Literal['user','support'] = 'user'
    display_name: str = Field(default='',max_length=100)


class Enabled(Input):
    enabled: bool


class Profile(Input):
    name: str = Field(min_length=1,max_length=100)
    protocol: Literal['anthropic','openai']
    model: str = Field(min_length=1,max_length=100)
    base_url: str = Field(min_length=1,max_length=2048)
    api_key: str = Field(default='',max_length=8192)
    currency: Literal['CNY','USD'] = 'CNY'
    prices: dict[str,str|None] = Field(default_factory=dict)
    revision: int|None = Field(default=None,ge=1)


class Revision(Input):
    revision: int = Field(ge=1)


def install_admin(app, principal):
    def identity(request):
        service = getattr(request.app.state,'identity',None)
        if service is None: raise HTTPException(503,'账号登录尚未初始化')
        return service

    def models(request):
        service = getattr(request.app.state,'models',None)
        if service is None: raise HTTPException(503,'模型后台尚未初始化')
        return service

    @app.get('/auth/options')
    def options():
        return {'demo_login_enabled':demo_enabled(),'roles':['user','support']}

    @app.post('/auth/login')
    def login(data:Login,request:Request,response:Response):
        result = identity(request).login(data.tenant,data.username,data.password,request.client.host if request.client else 'unknown')
        if result is None: raise HTTPException(401,'账号、密码不正确或账号暂不可用；连续失败后请稍后再试')
        response.headers['Cache-Control']='no-store'
        return result

    @app.post('/auth/demo-login')
    def demo_login(data:Demo,request:Request,response:Response):
        response.headers['Cache-Control']='no-store'
        return identity(request).demo_login(data.role)

    @app.post('/auth/logout')
    def logout(request:Request,p=Depends(principal)):
        identity(request).logout(request.headers.get('authorization','')[7:])
        return {'signed_out':True}

    @app.get('/admin/accounts')
    def accounts(request:Request,p=Depends(principal)):
        return identity(request).members(p)

    @app.post('/admin/accounts',status_code=201)
    def create_account(data:Account,request:Request,p=Depends(principal)):
        identity(request).create_account(p,data.username,data.password,data.role,data.display_name)
        return {'created':True,'username':data.username}

    @app.patch('/admin/accounts/{user_id}')
    def update_account(user_id:str,data:Enabled,request:Request,p=Depends(principal)):
        identity(request).set_enabled(p,user_id,data.enabled)
        return {'updated':True}

    @app.post('/admin/accounts/{user_id}/revoke-sessions')
    def revoke_sessions(user_id:str,request:Request,p=Depends(principal)):
        if p.role!='support': raise PermissionError()
        identity(request).revoke_sessions(p,user_id)
        return {'revoked':True}

    @app.get('/admin/audit')
    def audit(request:Request,p=Depends(principal)):
        return identity(request).audit_list(p)

    @app.get('/admin/models')
    def profiles(request:Request,p=Depends(principal)):
        return models(request).list(p)

    @app.post('/admin/models',status_code=201)
    def save_profile(data:Profile,request:Request,p=Depends(principal)):
        return models(request).save(p,data.model_dump(exclude={'revision'}))

    @app.put('/admin/models/{profile_id}')
    def update_profile(profile_id:str,data:Profile,request:Request,p=Depends(principal)):
        return models(request).save(p,data.model_dump(exclude={'revision'}),profile_id,data.revision)

    @app.post('/admin/models/{profile_id}/activate')
    def activate(profile_id:str,data:Revision,request:Request,p=Depends(principal)):
        return models(request).activate(p,profile_id,data.revision)

    @app.get('/admin/model-usage')
    def usage(request:Request,days:int=30,p=Depends(principal)):
        return models(request).usage(p,days)

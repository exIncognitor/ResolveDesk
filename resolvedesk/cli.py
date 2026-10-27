"""Interactive HTTP client for the current service: python -m resolvedesk.cli."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import time
from urllib.parse import quote,urlsplit
import uuid

import httpx

ROOT=Path(__file__).resolve().parents[1]
TERMINAL={'completed','failed','paused','awaiting_input','awaiting_confirmation'}
HELP='''直接输入问题继续当前会话；只有聊天、恢复执行才可能调用付费模型。
:new                     新建会话（下条消息时创建）
:use RUN_ID              使用本人已有会话
:runs                    列出本人会话
:retry                   网络不确定时复用原请求键，读取同一轮回执
:pause / :resume         暂停 / 显式恢复当前执行
:confirm VERSION         核对已显示草稿的版本后提交模拟工单
:search QUERY            免费检索当前有权查看的知识
:trace [RUN_ID]          完整分页轨迹及实际耗时
:recent                  最近轮次轨迹
:memory                  本人记忆状态
:integrations            意图向量与MCP集成状态（support可看向量详情）
:help / :quit            帮助 / 退出
'''


def base_url(value):
    parsed=urlsplit(value)
    if parsed.scheme not in {'http','https'} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {'','/'}:
        raise ValueError('Use a server origin without credentials, query or path')
    if parsed.scheme=='http' and parsed.hostname not in {'localhost','127.0.0.1','::1'}:raise ValueError('Remote API tokens require HTTPS')
    return value.rstrip('/')


class DeskCLI:
    def __init__(self,http,*,write=print,read=input,poll_seconds=.2,deadline=120):
        self.http,self.write,self.read=http,write,read
        self.poll_seconds,self.deadline=poll_seconds,deadline
        self.run_id=None;self.turn_key=None;self.pending=None

    def request(self,method,path,body=None):
        result=self.http.request(method,path,json=body)
        result.raise_for_status()
        return result.json()

    def display(self,data):
        # Never display approval digests or protocol credentials in terminal logs.
        from resolvedesk.tracing import scrub
        self.write(json.dumps(scrub(data),ensure_ascii=False,indent=2))

    def show_run(self,run):
        self.run_id=run['id'];self.turn_key=run.get('turn_key')
        self.write(f"ResolveDesk [{run['status']}]: {run.get('answer','')}")
        if run.get('latency_ms') is not None:self.write(f"本次执行耗时：{run['latency_ms']} ms")
        if run.get('draft'):
            self.display({'draft':run['draft'],'version':run['version'],'status':run['status']})
            if run['status']=='awaiting_confirmation':self.write(f"核对草稿后输入 :confirm {run['version']}；这会提交模拟工单。")

    def finish_pending(self):
        if self.pending is None:raise ValueError('没有待重试请求')
        method,path,body=self.pending
        try:run=self.request(method,path,body)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in {400,401,403,404,410,422}:self.pending=None
            raise
        self.run_id=run['id'];self.turn_key=run.get('turn_key') or (body or {}).get('request_key')
        deadline=time.monotonic()+self.deadline
        while run['status'] not in TERMINAL:
            if time.monotonic()>=deadline:raise TimeoutError('执行仍在进行；用 :retry 读取同一请求，不要重新提交')
            time.sleep(self.poll_seconds)
            run=self.request('GET',f'/runs/{self.run_id}/turns/{quote(self.turn_key,safe="")}') if self.turn_key else self.request('GET','/runs/'+self.run_id)
        self.pending=None
        self.show_run(run)
        return run

    def chat(self,message):
        if self.pending is not None:raise ValueError('上一请求结果尚不确定，请先 :retry，避免产生新一轮付费调用')
        path='/runs'
        if self.run_id:
            current=self.request('GET','/runs/'+self.run_id)
            if current['status'] in {'awaiting_input','awaiting_confirmation','completed'} and not current.get('ticket_id'):path=f'/runs/{self.run_id}/reply'
            elif current['status'] in {'queued','running','paused','failed'}:raise ValueError('当前执行尚未结束，请使用 :pause、:resume 或 :new')
        self.pending=('POST',path,{'message':message,'request_key':uuid.uuid4().hex})
        return self.finish_pending()

    def trace(self,run_id):
        turn=self.turn_key if run_id==self.run_id else None
        cursor=0;through=None;all_calls=[];count=0;timing=None
        for _ in range(100):
            params={'after':cursor,'limit':200}
            if turn:params['turn_key']=turn
            if through is not None:params['through']=through
            query=str(httpx.QueryParams(params))
            page=self.request('GET',f'/trace/tool/{quote(run_id,safe="")}?{query}')['trace']
            turn=page['turn_key'];through=page['snapshot_end'];timing=page.get('latency_ms')
            count+=len(page['events']);all_calls+=page['tool_calls']
            if page['next_after'] is None:break
            if page['next_after']<=cursor:raise ValueError('Trace cursor did not advance')
            cursor=page['next_after']
        else:raise ValueError('Trace exceeds 100 pages; use API cursors')
        self.display({'request_id':run_id,'turn_key':turn,'event_count':count,'latency_ms':timing,'tool_calls':all_calls})

    def command(self,line):
        if not line.startswith(':'):self.chat(line);return True
        parts=shlex.split(line);verb=parts[0];rest=line.partition(' ')[2].strip()
        if verb in {':quit',':exit'}:return False
        if verb==':help':self.write(HELP)
        elif verb==':retry':self.finish_pending()
        elif verb==':new':
            if self.pending:raise ValueError('请先 :retry 解决未确定的请求')
            self.run_id=self.turn_key=None;self.write('下一条消息将创建新会话。')
        elif verb==':use':
            if self.pending:raise ValueError('请先 :retry 解决未确定的请求')
            self.show_run(self.request('GET','/runs/'+quote(rest,safe='')))
        elif verb==':runs':self.display(self.request('GET','/runs'))
        elif verb==':recent':self.display(self.request('GET','/trace/tools'))
        elif verb==':memory':self.display(self.request('GET','/memory'))
        elif verb==':integrations':
            mcp=self.request('GET','/integrations/mcp')
            try:embedding=self.request('GET','/integrations/embedding')
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code!=403:raise
                embedding={'detail':'support role required'}
            self.display({'mcp':mcp,'intent_embedding':embedding})
        elif verb==':search':
            if not rest:raise ValueError('请输入检索内容')
            self.display(self.request('POST','/knowledge/search',{'query':rest,'top_k':5,'mode':'hybrid'}))
        elif verb==':trace':
            if not (rest or self.run_id):raise ValueError('请先选择会话')
            self.trace(rest or self.run_id)
        elif verb==':pause':
            if not self.run_id:raise ValueError('请先选择会话')
            self.show_run(self.request('POST',f'/runs/{self.run_id}/pause'))
        elif verb==':resume':
            if self.pending:raise ValueError('请先 :retry 解决未确定的请求')
            if not self.run_id:raise ValueError('请先选择会话')
            self.pending=('POST',f'/runs/{self.run_id}/resume',None);self.finish_pending()
        elif verb==':confirm':
            if self.pending:raise ValueError('请先 :retry 解决未确定的请求')
            if not self.run_id or not rest.isdigit():raise ValueError('用 :confirm VERSION 确认已显示的草稿版本')
            current=self.request('GET','/runs/'+self.run_id)
            if current['status']!='awaiting_confirmation' or current['version']!=int(rest):raise ValueError('草稿版本已改变，请重新核对')
            self.display(self.request('POST',f'/runs/{self.run_id}/confirm',{'approval':current['approval']}))
        else:raise ValueError('未知命令，输入 :help')
        return True

    def interact(self):
        self.write('ResolveDesk CLI：连接当前服务，输入 :help 查看命令。')
        while True:
            try:
                line=self.read('你: ').strip()
                if not line:continue
                if not self.command(line):break
            except (EOFError,KeyboardInterrupt):break
            except httpx.HTTPStatusError as exc:self.write(f'接口返回 {exc.response.status_code}；未确定的提交请 :retry。')
            except (httpx.RequestError,TimeoutError):self.write('网络或等待中断；提交结果可能已保存，请 :retry。')
            except ValueError as exc:self.write(str(exc))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url',default='http://127.0.0.1:8080')
    parser.add_argument('--access-file',type=Path,default=ROOT/'.codex-runtime/access.json')
    parser.add_argument('--actor',default='learner')
    args=parser.parse_args()
    try:
        origin=base_url(args.base_url)
        token=os.getenv('RESOLVEDESK_API_TOKEN')
        if not token:token=json.loads(args.access_file.read_text(encoding='utf-8'))[args.actor]['token']
        if not isinstance(token,str) or not token.strip():raise ValueError('Missing API token')
    except (OSError,ValueError,KeyError):parser.error('无法读取服务地址/本地API令牌；请检查access-file、actor或RESOLVEDESK_API_TOKEN')
    with httpx.Client(base_url=origin,headers={'Authorization':'Bearer '+token},trust_env=False,timeout=30) as http:
        DeskCLI(http).interact()


if __name__=='__main__':main()

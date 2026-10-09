"""Private, atomic FIFO records. This module never invokes a model or engine."""
from __future__ import annotations
import copy
import hashlib
import json
import math
import os
import re
import tempfile
from .connection_store import file_lock, protect_private

TERMINAL = {'done', 'failed', 'cancelled'}
STATES = TERMINAL | {'waiting', 'processing', 'starting', 'awaiting_input',
                     'running', 'blocked', 'interrupted'}
PREFS = {'manager_agent', 'manager_model', 'manager_effort', 'manager_model_mode',
         'manager_effort_mode', 'assign_mode', 'gate_mode'}
RESPONSE = {'status', 'session_id', 'question', 'questions', 'goal', 'constraints',
            'manager_cost_usd', 'cost_complete', 'pid'}

class GoalConflict(ValueError):
    pass

class GoalDataError(ValueError):
    pass

def _id(value, size=12):
    return isinstance(value, str) and bool(re.fullmatch('[0-9a-f]{%d}' % size, value))

def _time(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0

def _text(value):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 20000

def _digest(message, answers):
    return hashlib.sha256(json.dumps([message, answers], sort_keys=True,
                                    ensure_ascii=False).encode('utf-8')).hexdigest()

def validate_context(c):
    if (not isinstance(c, dict) or set(c) != {'workdir', 'prefs', 'binding'} or
            not isinstance(c['workdir'], str) or not os.path.isabs(c['workdir']) or
            not _id(c['binding'], 64) or not isinstance(c['prefs'], dict) or
            not set(c['prefs']) <= PREFS):
        raise GoalDataError('目标配置记录无效，原文件已保留')
    for k,v in c['prefs'].items():
        if k == 'gate_stages':
            valid = isinstance(v,list) and all(isinstance(x,str) and len(x)<100 for x in v)
        elif k in {'auto_assign','manager_collab'}:
            valid = type(v) is bool
        else:
            valid = isinstance(v,str) and len(v)<=200
        if not valid: raise GoalDataError('目标偏好无效，原文件已保留')

class GoalStore:
    MAX_ITEMS = 1000
    MAX_BYTES = 8 * 1024 * 1024

    def __init__(self, path):
        self.path = os.fspath(path)

    def _validate(self, state):
        def require(ok):
            if not ok: raise GoalDataError('目标队列记录损坏，原文件已保留')
        require(isinstance(state,dict) and set(state)=={'version','revision','items'})
        require(type(state['version']) is int and state['version']==1)
        require(type(state['revision']) is int and state['revision']>=0)
        require(isinstance(state['items'],list) and len(state['items'])<=self.MAX_ITEMS)
        ids=set()
        for r in state['items']:
            require(isinstance(r,dict) and set(r)=={'id','message','context','created_at',
                'updated_at','state','pending_turn','turns','run_id','response','error'})
            require(_id(r['id']) and r['id'] not in ids); ids.add(r['id'])
            require(_text(r['message']) and _time(r['created_at']) and _time(r['updated_at']))
            require(isinstance(r['state'],str) and r['state'] in STATES)
            require(r['run_id']=='' or _id(r['run_id'],8))
            require(isinstance(r['error'],str) and len(r['error'])<=500)
            validate_context(r['context'])
            require(isinstance(r['response'],dict) and set(r['response'])<=RESPONSE)
            p=r['pending_turn']
            if p is not None:
                require(isinstance(p,dict) and set(p)=={'id','message','choice_answers','submitted_at'})
                require(_id(p['id']) and _text(p['message']) and _time(p['submitted_at']))
                self._answers(p['choice_answers'])
            require(isinstance(r['turns'],list))
            tids=set()
            for t in r['turns']:
                require(isinstance(t,dict) and set(t)=={'id','digest','response'})
                require(_id(t['id']) and t['id'] not in tids and _id(t['digest'],64)); tids.add(t['id'])
                require(isinstance(t['response'],dict) and set(t['response'])<=RESPONSE)
            require(p is None or p['id'] in tids)
        # Reject nested non-finite JSON, as well as excessive payloads before writing.
        try: raw=json.dumps(state,ensure_ascii=False,allow_nan=False).encode('utf-8')
        except (TypeError,ValueError): raise GoalDataError('目标记录格式无效') from None
        require(len(raw)<=self.MAX_BYTES)

    @staticmethod
    def _answers(answers):
        if (not isinstance(answers,dict) or len(answers)>30 or
                any(not isinstance(k,str) or not isinstance(v,str) or
                    len(k)>200 or len(v)>20000 for k,v in answers.items())):
            raise GoalDataError('问题选项格式无效')

    def _read_validated(self):
        if not os.path.exists(self.path): return {'version':1,'revision':0,'items':[]}
        try:
            if os.path.getsize(self.path)>self.MAX_BYTES: raise GoalDataError('目标记录超过容量，原文件已保留')
            with open(self.path,encoding='utf-8') as f: state=json.load(f)
            self._validate(state)
            return state
        except (OSError,ValueError,TypeError,KeyError):
            raise GoalDataError('目标队列不可读，原文件已保留') from None

    def _write(self,state):
        parent=os.path.dirname(self.path) or '.'
        os.makedirs(parent,exist_ok=True)
        fd,tmp=tempfile.mkstemp(prefix='.goals-',dir=parent)
        try:
            protect_private(tmp)
            with os.fdopen(fd,'w',encoding='utf-8',newline='\n') as f:
                fd=-1
                json.dump(state,f,ensure_ascii=False,allow_nan=False)
                f.flush(); os.fsync(f.fileno())
            os.replace(tmp,self.path)
        finally:
            if fd!=-1: os.close(fd)
            if os.path.exists(tmp): os.unlink(tmp)

    def _transaction(self,mutation):
        with file_lock(self.path):
            s=self._read_validated(); before=copy.deepcopy(s)
            result=mutation(s)
            if s!=before:
                s['revision']+=1; self._validate(s); self._write(s)
            return copy.deepcopy(result)

    def snapshot(self):
        with file_lock(self.path): return copy.deepcopy(self._read_validated())

    def get(self,goal_id):
        return next((r for r in self.snapshot()['items'] if r['id']==goal_id),None)

    @staticmethod
    def _record(s,gid):
        r=next((r for r in s['items'] if r['id']==gid),None)
        if r is None: raise GoalConflict('目标不存在')
        return r

    @staticmethod
    def _pending(r,tid,message,answers,now):
        if not _id(tid) or not _text(message) or not _time(now): raise GoalDataError('目标消息无效')
        GoalStore._answers(answers)
        digest=_digest(message,answers)
        old=next((t for t in r['turns'] if t['id']==tid),None)
        if old:
            if old['digest']!=digest: raise GoalConflict('相同请求 ID 的消息内容不同')
            return False
        r['turns'].append({'id':tid,'digest':digest,'response':{}})
        r['pending_turn']={'id':tid,'message':message,'choice_answers':answers,'submitted_at':now}
        r['updated_at']=now
        return True

    def enqueue(self,gid,message,context,now):
        if not _id(gid) or not _text(message) or not _time(now): raise GoalDataError('目标提交无效')
        validate_context(context)
        def mutate(s):
            old=next((r for r in s['items'] if r['id']==gid),None)
            if old:
                if old['message']!=message or any(old['context'][k]!=context[k] for k in ('workdir','prefs')):
                    raise GoalConflict('相同请求 ID 的目标内容不同')
                return old
            if len(s['items'])>=self.MAX_ITEMS: raise GoalDataError('目标记录已满，请先归档历史')
            r={'id':gid,'message':message,'context':copy.deepcopy(context),'created_at':now,
               'updated_at':now,'state':'waiting','pending_turn':None,'turns':[],
               'run_id':'','response':{},'error':''}
            self._pending(r,gid,message,{},now); s['items'].append(r); return r
        return self._transaction(mutate)

    def submit_turn(self,gid,tid,message,answers,now):
        def mutate(s):
            r=self._record(s,gid)
            old=next((t for t in r['turns'] if t['id']==tid),None)
            if old:
                if old['digest']!=_digest(message,answers): raise GoalConflict('相同请求 ID 的消息内容不同')
                return r
            if r['state']!='awaiting_input' or r['pending_turn'] is not None:
                raise GoalConflict('目标尚未等待回复')
            self._pending(r,tid,message,answers,now); return r
        return self._transaction(mutate)

    def claim_next(self,now):
        def mutate(s):
            for r in s['items']:
                if r['state'] in TERMINAL: continue
                if r['state'] in {'waiting','awaiting_input'} and r['pending_turn']:
                    r.update(state='processing',updated_at=now); return r
                return None
            return None
        return self._transaction(mutate)

    def complete_turn(self,gid,tid,status,payload,now):
        def mutate(s):
            r=self._record(s,gid); p=r['pending_turn']
            if not p or p['id']!=tid or r['state'] not in {'processing','starting'}:
                raise GoalConflict('经理回复不属于当前轮次')
            response={k:copy.deepcopy(v) for k,v in payload.items() if k in RESPONSE}
            kind=response.get('status')
            state=('awaiting_input' if status==200 and kind in {'asking','confirm'} else
                   'running' if status==200 and kind=='started' and r['run_id'] else
                   'blocked' if status==400 else 'interrupted')
            r.update(state=state,pending_turn=None if state in {'awaiting_input','running'} else p,
                     response=response,updated_at=now,
                     error='' if state in {'awaiting_input','running'} else
                     '配置或路径已失效，请使用当前设置重试' if state=='blocked' else
                     '调用状态不明，请确认后重试')
            next(t for t in r['turns'] if t['id']==tid)['response']=response
            return r
        return self._transaction(mutate)

    def bind_run(self,gid,rid,now):
        def mutate(s):
            r=self._record(s,gid)
            if not _id(rid,8) or r['state'] not in {'processing','starting'}: raise GoalConflict('目标不能启动')
            if r['run_id'] and r['run_id']!=rid: raise GoalConflict('目标已绑定其他运行')
            r.update(run_id=rid,state='starting',updated_at=now); return r
        return self._transaction(mutate)

    def finish_run(self,gid,state,now):
        def mutate(s):
            r=self._record(s,gid)
            if r['state'] not in {'running','starting','interrupted'} or not r['run_id']:
                raise GoalConflict('目标没有绑定的运行')
            if state not in TERMINAL|{'interrupted'}: raise GoalDataError('运行结果无效')
            r.update(state=state,updated_at=now,pending_turn=None,
                     error='运行状态不明，需要检查已有运行' if state=='interrupted' else '')
            return r
        return self._transaction(mutate)

    def resolve(self,gid,now):
        """Caller has checked ownership and obtained explicit stopped confirmation."""
        def mutate(s):
            r=self._record(s,gid)
            if r['state']=='cancelled' and r['run_id']: return r
            if r['state']!='interrupted' or not r['run_id']:
                raise GoalConflict('仅状态不明的已有运行需要结束确认')
            r.update(state='cancelled',pending_turn=None,error='',updated_at=now)
            return r
        return self._transaction(mutate)

    def cancel(self,gid,now):
        def mutate(s):
            r=self._record(s,gid)
            if r['state']=='cancelled': return r
            if r['state']!='waiting' and not (r['state']=='awaiting_input' and not r['pending_turn']):
                raise GoalConflict('正在处理的目标不能取消等待')
            r.update(state='cancelled',pending_turn=None,updated_at=now); return r
        return self._transaction(mutate)

    def retry(self,gid,context,now,turn_id=None):
        validate_context(context)
        def mutate(s):
            r=self._record(s,gid)
            if turn_id and any(t['id']==turn_id for t in r['turns']):
                if r['context']==context: return r
                raise GoalConflict('重试请求的设置不同')
            if r['state'] not in {'blocked','interrupted'} or r['run_id']: raise GoalConflict('目标不能重试，请检查已有运行')
            r.update(context=copy.deepcopy(context),state='waiting',error='',updated_at=now)
            # Preserve the failed message and cumulative session ledger.
            tid=turn_id or hashlib.sha256(str(now).encode()).hexdigest()[:12]
            message=r['pending_turn']['message'] if r['pending_turn'] else r['message']
            r['pending_turn']=None; self._pending(r,tid,message,{},now); return r
        return self._transaction(mutate)

    def recover(self,proofs,now):
        def mutate(s):
            for r in s['items']:
                if r['state'] not in {'processing','starting','running'}: continue
                proof=proofs.get(r['id'],{})
                sess=proof.get('session') or {}
                if r['run_id']:
                    state=(proof.get('run_state','interrupted') if proof.get('run_id')==r['run_id']
                           and (not sess.get('run_id') or sess['run_id']==r['run_id']) else 'interrupted')
                    if state not in TERMINAL|{'running'}: state='interrupted'
                    r.update(state=state,pending_turn=None,updated_at=now)
                elif (r['state']=='processing' and sess.get('session_id')==r['id'] and
                      r['pending_turn'] and len(sess.get('history',[]))>=2 and
                      sess['history'][-1].get('role')=='manager' and
                      sess['history'][-2].get('role')=='user' and
                      sess['history'][-2].get('text')==r['pending_turn']['message'] and
                      sess['history'][-2].get('ts',0)+.001>=r['pending_turn']['submitted_at']):
                    p=r['pending_turn']
                    response={'session_id':r['id'],'manager_cost_usd':sess.get('cost_usd',0)}
                    if sess.get('awaiting_confirmation') and sess.get('proposal'):
                        response.update(status='confirm',**sess['proposal'])
                    else:
                        last=sess['history'][-1]
                        response.update(status='asking',question=last['text'],questions=last.get('questions',[]))
                    r.update(state='awaiting_input',pending_turn=None,response=response,updated_at=now)
                    next(t for t in r['turns'] if t['id']==p['id'])['response']=response
                else:
                    r.update(state='interrupted',updated_at=now)
                if r['state']=='interrupted':
                    r['error']=('运行状态不明，请检查原运行并确认停止后结束目标' if r['run_id']
                                else '上次调用已中断，请确认后重试')
        self._transaction(mutate)

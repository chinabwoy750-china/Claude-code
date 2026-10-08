import asyncio, hashlib, hmac, json, os, secrets, time, uuid
from pathlib import Path as FsPath
from typing import Any, Dict, List, Optional
from contextlib import suppress
import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

CHINA_GPT_BASE_URL=os.getenv('CHINA_GPT_BASE_URL','https://gpt-china.onrender.com').rstrip('/')
CHINA_GPT_API_KEY=os.getenv('CHINA_GPT_API_KEY','')
DEFAULT_MODEL=os.getenv('MODEL','dahl/MiniMaxAI/MiniMax-M2.7')
APP_API_KEY=os.getenv('APP_API_KEY','')
UI_PASSWORD=os.getenv('UI_PASSWORD','')
REQUEST_TIMEOUT=float(os.getenv('REQUEST_TIMEOUT','300'))
SESSION_SECRET=os.getenv('SESSION_SECRET') or secrets.token_urlsafe(32)
TELEGRAM_BOT_TOKEN=os.getenv('TELEGRAM_BOT_TOKEN','')
TELEGRAM_ALLOWED_CHAT_ID=os.getenv('TELEGRAM_ALLOWED_CHAT_ID','')
TELEGRAM_POLL_TIMEOUT=int(os.getenv('TELEGRAM_POLL_TIMEOUT','30'))
app=FastAPI(title='Claude Code Web')
telegram_task = None
telegram_offset = None
telegram_model_by_chat = {}
job_worker_task = None
JOBS_DIR = FsPath(os.getenv('JOBS_DIR', '/workspace/jobs'))
app.mount('/static',StaticFiles(directory='/app/static'),name='static')

def sig(v): return hmac.new(SESSION_SECRET.encode(),v.encode(),hashlib.sha256).hexdigest()
def new_session():
    b=f'{int(time.time())}:{secrets.token_urlsafe(24)}'; return b+'.'+sig(b)
def valid_session(c):
    if not c or '.' not in c:return False
    b,s=c.rsplit('.',1)
    try: ts=int(b.split(':',1)[0])
    except: return False
    return time.time()-ts<=604800 and hmac.compare_digest(s,sig(b))
def require_app_key(a):
    if not APP_API_KEY: raise HTTPException(500,'APP_API_KEY is not configured')
    if a!=f'Bearer {APP_API_KEY}': raise HTTPException(401,'Unauthorized')
def require_ui(r):
    if not UI_PASSWORD: raise HTTPException(503,'UI_PASSWORD is not configured on the server')
    if not valid_session(r.cookies.get('ui_session')): raise HTTPException(401,'Login required')

async def gateway_models():
    async with httpx.AsyncClient(timeout=30) as c:
        r=await c.get(f'{CHINA_GPT_BASE_URL}/v1/models',headers={'Authorization':f'Bearer {CHINA_GPT_API_KEY}'})
        r.raise_for_status(); d=r.json()
    return [{'id':x['id'],'name':x.get('name') or x['id'],'owned_by':x.get('owned_by','')} for x in d.get('data',[]) if isinstance(x,dict) and x.get('id')]

def text_content(c):
    if isinstance(c,str): return c
    if not isinstance(c,list): return json.dumps(c,ensure_ascii=False)
    out=[]
    for b in c:
        if not isinstance(b,dict): continue
        if b.get('type')=='text': out.append(b.get('text',''))
        elif b.get('type')=='tool_result': out.append(json.dumps({'tool_result':b.get('content',''),'tool_use_id':b.get('tool_use_id')},ensure_ascii=False))
        elif b.get('type')=='image': out.append('[image omitted by adapter]')
    return '\n'.join(out)

def anth_to_openai(b):
    msgs=[]; system=b.get('system')
    if system:
        st='\n'.join(x.get('text','') if isinstance(x,dict) else str(x) for x in system) if isinstance(system,list) else str(system)
        msgs.append({'role':'system','content':st})
    for m in b.get('messages',[]):
        role=m.get('role','user'); c=m.get('content','')
        if not isinstance(c,list): msgs.append({'role':role,'content':c}); continue
        texts=[]; calls=[]
        for x in c:
            if not isinstance(x,dict): continue
            if x.get('type')=='text': texts.append(x.get('text',''))
            elif x.get('type')=='tool_use':
                calls.append({'id':x.get('id',''),'type':'function','function':{'name':x.get('name',''),'arguments':json.dumps(x.get('input',{}),ensure_ascii=False)}})
            elif x.get('type')=='tool_result':
                msgs.append({'role':'tool','tool_call_id':x.get('tool_use_id',''),'content':text_content(x.get('content',''))})
        if calls: msgs.append({'role':role,'content':'\n'.join(texts) if texts else None,'tool_calls':calls})
        else: msgs.append({'role':role,'content':'\n'.join(texts)})
    o={'model':b.get('model') or DEFAULT_MODEL,'messages':msgs,'stream':False}
    for k in ('max_tokens','temperature','top_p'):
        if b.get(k) is not None:o[k]=b[k]
    if b.get('tools'):
        o['tools']=[{'type':'function','function':{'name':t.get('name',''),'description':t.get('description',''),'parameters':t.get('input_schema',{'type':'object'})}} for t in b['tools']]
    return o

def oa_to_anth(msg,model,usage):
    c=[]; t=msg.get('content')
    if t:c.append({'type':'text','text':t})
    for call in msg.get('tool_calls') or []:
        fn=call.get('function',{})
        try: args=json.loads(fn.get('arguments','{}'))
        except: args={}
        c.append({'type':'tool_use','id':call.get('id',''),'name':fn.get('name',''),'input':args})
    return {'id':'msg_'+secrets.token_hex(12),'type':'message','role':'assistant','model':model,'content':c,'stop_reason':'tool_use' if msg.get('tool_calls') else 'end_turn','stop_sequence':None,'usage':{'input_tokens':usage.get('prompt_tokens',0),'output_tokens':usage.get('completion_tokens',0)}}

async def gateway_call(body):
    payload = anth_to_openai(body)
    print('GATEWAY REQUEST:', 'model=', payload.get('model'), 'stream=', payload.get('stream'), flush=True)
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as c:
        return await c.post(
            f'{CHINA_GPT_BASE_URL}/v1/chat/completions',
            headers={'Authorization': f'Bearer {CHINA_GPT_API_KEY}', 'Content-Type': 'application/json'},
            json=payload,
        )

async def run_claude(prompt, model):
    env = os.environ.copy()
    env['ANTHROPIC_BASE_URL'] = f"http://127.0.0.1:{os.getenv('PORT', '10000')}"
    env['ANTHROPIC_API_KEY'] = 'local-adapter'
    env['ANTHROPIC_CUSTOM_MODEL_OPTION'] = model
    env['ANTHROPIC_CUSTOM_MODEL_OPTION_NAME'] = model
    env['ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION'] = 'Model routed through the configured OpenAI-compatible gateway'
    cmd = [
        '/home/claude/.local/bin/claude',
        '-p', prompt,
        '--model', model,
        '--output-format', 'json',
        '--permission-mode', 'bypassPermissions',
    ]
    p = await asyncio.create_subprocess_exec(
        *cmd, cwd='/workspace', env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await asyncio.wait_for(p.communicate(), timeout=REQUEST_TIMEOUT)
    return p.returncode, out.decode(errors='replace'), err.decode(errors='replace')


# --- Agent jobs (social / affiliate / trade research) ---
JOB_TYPES = ('social', 'affiliate', 'trade')

SOCIAL_PROMPT = """You are a social content agent. Draft only. Do not post.
Brand/niche: {brief}
Platforms: {platforms}
Produce a 7-day content batch.
Output STRICT JSON:
{{"posts":[{{"day":1,"platform":"...","hook":"...","body":"...","cta":"...","asset_type":"text|image|video_script"}}],
 "notes":"..."}}
Write files under the current working directory if useful. No live publishing."""

AFFILIATE_PROMPT = """You are an affiliate campaign agent. Draft only. Do not buy ads or send email.
Offer: {offer}
Geo/audience: {audience}
Produce angles, email drafts, social hooks, UTM ideas.
Output STRICT JSON:
{{"angles":["..."],"emails":[{{"subject":"...","body":"..."}}],
 "hooks":["..."],"utms":["..."],"notes":"..."}}
No fabricated performance claims."""

TRADE_PROMPT = """You are a research desk, not a broker. No orders.
Universe: {symbols}
Timeframe/horizon: {horizon}
Output STRICT JSON:
{{"bias":"long|short|neutral","invalidation":"...","size_suggestion_pct":0.0,
 "rationale":"...","risks":["..."],"levels":{{"entry":null,"stop":null,"target":null}}}}
Paper-only. Never invent live account balances."""

def _job_path(job_id: str) -> FsPath:
    return JOBS_DIR / job_id

def job_save(job: dict) -> None:
    d = _job_path(job['id'])
    d.mkdir(parents=True, exist_ok=True)
    (d / 'job.json').write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding='utf-8')

def job_load(job_id: str) -> Optional[dict]:
    f = _job_path(job_id) / 'job.json'
    if not f.exists():
        return None
    return json.loads(f.read_text(encoding='utf-8'))

def job_list(limit: int = 30) -> List[dict]:
    if not JOBS_DIR.exists():
        return []
    jobs = []
    for child in sorted(JOBS_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not child.is_dir():
            continue
        j = job_load(child.name)
        if j:
            jobs.append(j)
        if len(jobs) >= limit:
            break
    return jobs

def build_job_prompt(job: dict) -> str:
    t = job.get('type')
    p = job.get('payload') or {}
    if t == 'social':
        return SOCIAL_PROMPT.format(
            brief=p.get('brief') or p.get('niche') or 'general',
            platforms=', '.join(p.get('platforms') or ['x', 'tiktok', 'instagram']),
        )
    if t == 'affiliate':
        return AFFILIATE_PROMPT.format(
            offer=p.get('offer') or 'unspecified offer',
            audience=p.get('audience') or 'general',
        )
    if t == 'trade':
        return TRADE_PROMPT.format(
            symbols=p.get('symbols') or 'BTCUSDT',
            horizon=p.get('horizon') or 'intraday',
        )
    return f"Unknown job type. Payload: {json.dumps(p)}"

async def execute_job(job_id: str) -> dict:
    job = job_load(job_id)
    if not job:
        raise HTTPException(404, 'job not found')
    if job.get('status') in ('running', 'done') and job.get('status') == 'done':
        return job
    job['status'] = 'running'
    job['started_at'] = time.time()
    job_save(job)
    model = job.get('model') or DEFAULT_MODEL
    prompt = build_job_prompt(job)
    # cwd into job dir so Claude Code can write artifacts there
    job_dir = str(_job_path(job_id))
    env = os.environ.copy()
    env['ANTHROPIC_BASE_URL'] = f"http://127.0.0.1:{os.getenv('PORT', '10000')}"
    env['ANTHROPIC_API_KEY'] = 'local-adapter'
    env['ANTHROPIC_CUSTOM_MODEL_OPTION'] = model
    env['ANTHROPIC_CUSTOM_MODEL_OPTION_NAME'] = model
    env['ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION'] = 'Model routed through the configured OpenAI-compatible gateway'
    cmd = [
        '/home/claude/.local/bin/claude',
        '-p', prompt,
        '--model', model,
        '--output-format', 'json',
        '--permission-mode', 'bypassPermissions',
    ]
    try:
        p = await asyncio.create_subprocess_exec(
            *cmd, cwd=job_dir, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(p.communicate(), timeout=REQUEST_TIMEOUT)
        rc = p.returncode
        stdout = out.decode(errors='replace')
        stderr = err.decode(errors='replace')
    except Exception as e:
        job['status'] = 'error'
        job['error'] = str(e)
        job['finished_at'] = time.time()
        job_save(job)
        return job
    result = None
    try:
        result = json.loads(stdout).get('result')
    except Exception:
        result = stdout
    job['returncode'] = rc
    job['stdout'] = stdout[-20000:]
    job['stderr'] = stderr[-8000:]
    job['result'] = result
    job['status'] = 'done' if rc == 0 else 'error'
    job['finished_at'] = time.time()
    job_save(job)
    # notify operator on Telegram if configured
    if TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_CHAT_ID:
        summary = f"Job {job_id} [{job['type']}] → {job['status']}"
        if isinstance(result, (dict, list)):
            summary += '\n' + json.dumps(result, ensure_ascii=False)[:3500]
        elif result:
            summary += '\n' + str(result)[:3500]
        elif stderr:
            summary += '\n' + stderr[:3500]
        with suppress(Exception):
            await tg_send(str(TELEGRAM_ALLOWED_CHAT_ID), summary)
    return job

async def job_worker_loop():
    while True:
        try:
            for job in job_list(50):
                if job.get('status') == 'queued':
                    await execute_job(job['id'])
            await asyncio.sleep(3)
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(5)

class JobCreate(BaseModel):
    type: str
    payload: Dict[str, Any] = {}
    model: Optional[str] = None
    auto_run: bool = True

class ChatBody(BaseModel): message:str; model:Optional[str]=None

@app.get('/',response_class=HTMLResponse)
async def home(): return HTMLResponse(open('/app/static/index.html',encoding='utf-8').read())
@app.get('/health')
async def health(): return {'status':'ok','gateway':CHINA_GPT_BASE_URL,'default_model':DEFAULT_MODEL,'claude_code':os.path.exists('/home/claude/.local/bin/claude'),'web_ui':True,'telegram':bool(TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_CHAT_ID),'jobs':True}
@app.post('/api/login')
async def login(b:Dict[str,Any]):
    if not UI_PASSWORD: raise HTTPException(503,'Set UI_PASSWORD in Render environment variables first')
    if not hmac.compare_digest(str(b.get('password','')),UI_PASSWORD): raise HTTPException(401,'Wrong password')
    r=JSONResponse({'ok':True}); r.set_cookie('ui_session',new_session(),max_age=604800,httponly=True,secure=True,samesite='lax',path='/'); return r
@app.post('/api/logout')
async def logout():
    r=JSONResponse({'ok':True}); r.delete_cookie('ui_session',path='/'); return r
@app.get('/api/me')
async def me(r:Request): require_ui(r); return {'authenticated':True}
@app.get('/api/models')
async def api_models(r:Request):
    require_ui(r)
    try: models=await gateway_models(); warning=None
    except Exception as e: models=[{'id':DEFAULT_MODEL,'name':DEFAULT_MODEL,'owned_by':''}]; warning=str(e)
    if not any(x['id']==DEFAULT_MODEL for x in models): models.insert(0,{'id':DEFAULT_MODEL,'name':DEFAULT_MODEL,'owned_by':''})
    return {'models':models,'warning':warning}
@app.post('/api/chat')
async def chat(b:ChatBody,r:Request):
    require_ui(r)
    if not b.message.strip(): raise HTTPException(400,'Message is empty')
    model=b.model or DEFAULT_MODEL; rc,out,err=await run_claude(b.message,model); result=None
    try: result=json.loads(out).get('result')
    except: pass
    if rc!=0: raise HTTPException(500,(err.strip() or out.strip() or 'Claude Code failed')[-4000:])
    return {'ok':True,'model':model,'result':result if result is not None else out,'stderr':err[-2000:] if err else ''}

@app.post('/api/jobs')
async def api_jobs_create(b: JobCreate, r: Request):
    require_ui(r)
    if b.type not in JOB_TYPES:
        raise HTTPException(400, f'type must be one of {JOB_TYPES}')
    job_id = secrets.token_hex(8)
    job = {
        'id': job_id,
        'type': b.type,
        'payload': b.payload or {},
        'model': b.model or DEFAULT_MODEL,
        'status': 'queued' if b.auto_run else 'draft',
        'created_at': time.time(),
    }
    job_save(job)
    if b.auto_run:
        asyncio.create_task(execute_job(job_id))
    return {'ok': True, 'job': job}

@app.get('/api/jobs')
async def api_jobs_list(r: Request):
    require_ui(r)
    return {'jobs': job_list()}

@app.get('/api/jobs/{job_id}')
async def api_jobs_get(job_id: str, r: Request):
    require_ui(r)
    job = job_load(job_id)
    if not job:
        raise HTTPException(404, 'job not found')
    return job

@app.post('/api/jobs/{job_id}/run')
async def api_jobs_run(job_id: str, r: Request):
    require_ui(r)
    job = job_load(job_id)
    if not job:
        raise HTTPException(404, 'job not found')
    job['status'] = 'queued'
    job_save(job)
    asyncio.create_task(execute_job(job_id))
    return {'ok': True, 'job_id': job_id}

@app.post('/claude/jobs')
async def claude_jobs_create(b: JobCreate, authorization: Optional[str] = Header(default=None)):
    require_app_key(authorization)
    if b.type not in JOB_TYPES:
        raise HTTPException(400, f'type must be one of {JOB_TYPES}')
    job_id = secrets.token_hex(8)
    job = {
        'id': job_id,
        'type': b.type,
        'payload': b.payload or {},
        'model': b.model or DEFAULT_MODEL,
        'status': 'queued' if b.auto_run else 'draft',
        'created_at': time.time(),
    }
    job_save(job)
    if b.auto_run:
        asyncio.create_task(execute_job(job_id))
    return {'ok': True, 'job': job}

@app.get('/claude/version')
async def version(authorization:Optional[str]=Header(default=None)):
    require_app_key(authorization); p=await asyncio.create_subprocess_exec('/home/claude/.local/bin/claude','--version',stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE); o,e=await p.communicate(); return {'returncode':p.returncode,'stdout':o.decode(errors='replace'),'stderr':e.decode(errors='replace')}
@app.post('/claude/run')
async def run(b:Dict[str,Any],authorization:Optional[str]=Header(default=None)):
    require_app_key(authorization); prompt=str(b.get('prompt','')).strip()
    if not prompt: raise HTTPException(400,'prompt is required')
    model=str(b.get('model') or DEFAULT_MODEL); rc,out,err=await run_claude(prompt,model); return {'returncode':rc,'model':model,'stdout':out,'stderr':err}
@app.get('/v1/models')
async def models(authorization:Optional[str]=Header(default=None)): require_app_key(authorization); return {'object':'list','data':await gateway_models()}
@app.post('/v1/messages/count_tokens')
async def count(request:Request,authorization:Optional[str]=Header(default=None),x_api_key:Optional[str]=Header(default=None)):
    if x_api_key!='local-adapter' and authorization!='Bearer local-adapter': require_app_key(authorization)
    b=await request.json(); return {'input_tokens':max(1,len(json.dumps(b.get('messages',[]),ensure_ascii=False))//4)}
@app.post('/v1/messages')
async def messages(request:Request,authorization:Optional[str]=Header(default=None),x_api_key:Optional[str]=Header(default=None)):
    if x_api_key!='local-adapter' and authorization!='Bearer local-adapter': require_app_key(authorization)
    b=await request.json(); u=await gateway_call(b)
    print('GATEWAY DEBUG:', 'status=', u.status_code, 'content_type=', u.headers.get('content-type'), 'body=', u.text[:4000], flush=True)
    if u.status_code>=400:
        try: content=u.json()
        except Exception: content={'error':{'type':'gateway_error','message':f'Gateway returned HTTP {u.status_code}: {u.text[:2000]}'}}
        return JSONResponse(status_code=u.status_code,content=content)
    content_type=(u.headers.get('content-type') or '').lower()
    if 'text/event-stream' in content_type:
        full_text=[]; tool_calls=[]
        for line in u.text.splitlines():
            line=line.strip()
            if not line.startswith('data:'): continue
            data=line[5:].strip()
            if not data or data=='[DONE]': continue
            try: chunk=json.loads(data)
            except Exception: continue
            for choice in chunk.get('choices', []):
                delta=choice.get('delta') or {}
                content=delta.get('content')
                if content: full_text.append(content)
                for tc in delta.get('tool_calls') or []: tool_calls.append(tc)
        message={'role':'assistant','content':''.join(full_text)}
        if tool_calls: message['tool_calls']=tool_calls
        return JSONResponse(oa_to_anth(message,b.get('model') or DEFAULT_MODEL,{}))
    try: d=u.json()
    except Exception:
        return JSONResponse(status_code=502,content={'error':{'type':'invalid_gateway_response','message':f'Gateway returned a non-JSON response. HTTP {u.status_code}. Body: {u.text[:2000]}'}})
    choices=d.get('choices') or []
    if not choices:
        return JSONResponse(status_code=502,content={'error':{'type':'invalid_gateway_response','message':'Gateway JSON contained no choices.','response':d}})
    message=choices[0].get('message') or {}
    return JSONResponse(oa_to_anth(message,b.get('model') or DEFAULT_MODEL,d.get('usage') or {}))
async def tg_api(method,payload=None):
    url=f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    async with httpx.AsyncClient(timeout=TELEGRAM_POLL_TIMEOUT+10) as c:
        r=await c.post(url,json=payload or {}); r.raise_for_status(); return r.json()

async def tg_send(chat_id,text):
    text=text or '(empty response)'
    for i in range(0,len(text),3900): await tg_api('sendMessage',{'chat_id':chat_id,'text':text[i:i+3900]})

async def tg_models():
    try:
        ms=await gateway_models()
        return 'Available models:\n\n'+'\n'.join('• '+m['id'] for m in ms) if ms else 'No models returned.'
    except Exception as e: return 'Could not load models: '+str(e)

async def tg_handle(chat_id,text):
    text=text.strip()
    if text=='/start':
        await tg_send(chat_id,'Claude Code + Agent jobs online.\n\nChat: send any message to run Claude Code.\n\nJobs:\n/job_social brief=... platforms=x,tiktok\n/job_affiliate offer=... audience=...\n/job_trade symbols=BTCUSDT horizon=intraday\n/jobs — list recent jobs\n/job JOB_ID — show job\n\nModels:\n/models /model /model ID /clear'); return
    if text=='/models': await tg_send(chat_id,await tg_models()); return
    if text=='/model': await tg_send(chat_id,'Current model:\n'+telegram_model_by_chat.get(chat_id,DEFAULT_MODEL)); return
    if text.startswith('/model '):
        model=text[7:].strip()
        if not model: await tg_send(chat_id,'Usage: /model MODEL_ID'); return
        telegram_model_by_chat[chat_id]=model; await tg_send(chat_id,'Model switched to:\n'+model); return
    if text=='/clear': telegram_model_by_chat.pop(chat_id,None); await tg_send(chat_id,'Model reset to:\n'+DEFAULT_MODEL); return
    if text=='/jobs':
        jobs=job_list(15)
        if not jobs: await tg_send(chat_id,'No jobs yet.'); return
        lines=[]
        for j in jobs:
            lines.append(f"{j['id']} [{j.get('type')}] {j.get('status')}")
        await tg_send(chat_id,'Recent jobs:\n'+'\n'.join(lines)); return
    if text.startswith('/job '):
        jid=text[5:].strip()
        job=job_load(jid)
        if not job: await tg_send(chat_id,'Job not found'); return
        body=json.dumps({k:job.get(k) for k in ('id','type','status','payload','result','error','returncode') if k in job or job.get(k) is not None}, ensure_ascii=False, indent=2)
        await tg_send(chat_id, body[:3900]); return
    if text.startswith('/job_social'):
        rest=text[len('/job_social'):].strip()
        payload={'brief': rest or 'general growth content', 'platforms': ['x','tiktok','instagram']}
        if 'platforms=' in rest:
            # naive parse platforms=a,b
            for part in rest.split():
                if part.startswith('platforms='):
                    payload['platforms']=[x.strip() for x in part.split('=',1)[1].split(',') if x.strip()]
                elif part.startswith('brief='):
                    payload['brief']=part.split('=',1)[1]
        job_id=secrets.token_hex(8)
        job={'id':job_id,'type':'social','payload':payload,'model':telegram_model_by_chat.get(chat_id,DEFAULT_MODEL),'status':'queued','created_at':time.time()}
        job_save(job); asyncio.create_task(execute_job(job_id))
        await tg_send(chat_id,f'Queued social job {job_id}'); return
    if text.startswith('/job_affiliate'):
        rest=text[len('/job_affiliate'):].strip()
        payload={'offer': rest or 'unspecified', 'audience': 'general'}
        for part in rest.split():
            if part.startswith('offer='): payload['offer']=part.split('=',1)[1]
            if part.startswith('audience='): payload['audience']=part.split('=',1)[1]
        job_id=secrets.token_hex(8)
        job={'id':job_id,'type':'affiliate','payload':payload,'model':telegram_model_by_chat.get(chat_id,DEFAULT_MODEL),'status':'queued','created_at':time.time()}
        job_save(job); asyncio.create_task(execute_job(job_id))
        await tg_send(chat_id,f'Queued affiliate job {job_id}'); return
    if text.startswith('/job_trade'):
        rest=text[len('/job_trade'):].strip()
        payload={'symbols': 'BTCUSDT', 'horizon': 'intraday'}
        for part in rest.split():
            if part.startswith('symbols='): payload['symbols']=part.split('=',1)[1]
            if part.startswith('horizon='): payload['horizon']=part.split('=',1)[1]
        job_id=secrets.token_hex(8)
        job={'id':job_id,'type':'trade','payload':payload,'model':telegram_model_by_chat.get(chat_id,DEFAULT_MODEL),'status':'queued','created_at':time.time()}
        job_save(job); asyncio.create_task(execute_job(job_id))
        await tg_send(chat_id,f'Queued trade-research job {job_id} (paper only)'); return
    model=telegram_model_by_chat.get(chat_id,DEFAULT_MODEL)
    await tg_api('sendChatAction',{'chat_id':chat_id,'action':'typing'})
    rc,out,err=await run_claude(text,model)
    if rc!=0: await tg_send(chat_id,'Claude Code error:\n'+(err.strip() or out.strip())[-3900:]); return
    try: result=json.loads(out).get('result')
    except Exception: result=None
    await tg_send(chat_id,str(result if result is not None else out))

async def telegram_loop():
    global telegram_offset
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_ALLOWED_CHAT_ID: return
    with suppress(Exception): await tg_api('deleteWebhook',{'drop_pending_updates':False})
    while True:
        try:
            payload={'timeout':TELEGRAM_POLL_TIMEOUT,'allowed_updates':['message']}
            if telegram_offset is not None: payload['offset']=telegram_offset
            data=await tg_api('getUpdates',payload)
            for u in data.get('result',[]):
                telegram_offset=u['update_id']+1
                m=u.get('message') or {}; chat=str((m.get('chat') or {}).get('id',''))
                if chat!=str(TELEGRAM_ALLOWED_CHAT_ID) or not m.get('text'): continue
                try: await tg_handle(chat,m['text'])
                except Exception as e: await tg_send(chat,'Bot error: '+str(e)[-3800:])
        except asyncio.CancelledError: raise
        except Exception: await asyncio.sleep(5)

@app.on_event('startup')
async def startup():
    global telegram_task, job_worker_task
    os.makedirs('/workspace', exist_ok=True)
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    job_worker_task = asyncio.create_task(job_worker_loop())
    if TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_CHAT_ID:
        telegram_task = asyncio.create_task(telegram_loop())

@app.on_event('shutdown')
async def shutdown():
    if job_worker_task:
        job_worker_task.cancel()
        with suppress(asyncio.CancelledError): await job_worker_task
    if telegram_task:
        telegram_task.cancel()
        with suppress(asyncio.CancelledError): await telegram_task

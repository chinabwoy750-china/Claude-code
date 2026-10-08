import asyncio, hashlib, hmac, json, os, secrets, time
from typing import Any, Dict, Optional
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
    o={'model':b.get('model') or DEFAULT_MODEL,'messages':msgs,'stream':bool(b.get('stream',False))}
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
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as c:
        return await c.post(f'{CHINA_GPT_BASE_URL}/v1/chat/completions',headers={'Authorization':f'Bearer {CHINA_GPT_API_KEY}','Content-Type':'application/json'},json=anth_to_openai(body))

async def run_claude(prompt,model):
    env=os.environ.copy(); env['ANTHROPIC_BASE_URL']=f"http://127.0.0.1:{os.getenv('PORT','10000')}"; env['ANTHROPIC_API_KEY']='local-adapter'
    cmd=['/home/claude/.local/bin/claude','-p',prompt,'--model',model,'--output-format','json','--permission-mode','bypassPermissions']
    p=await asyncio.create_subprocess_exec(*cmd,cwd='/workspace',env=env,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    out,err=await asyncio.wait_for(p.communicate(),timeout=REQUEST_TIMEOUT)
    return p.returncode,out.decode(errors='replace'),err.decode(errors='replace')

class ChatBody(BaseModel): message:str; model:Optional[str]=None

@app.get('/',response_class=HTMLResponse)
async def home(): return HTMLResponse(open('/app/static/index.html',encoding='utf-8').read())
@app.get('/health')
async def health(): return {'status':'ok','gateway':CHINA_GPT_BASE_URL,'default_model':DEFAULT_MODEL,'claude_code':os.path.exists('/home/claude/.local/bin/claude'),'web_ui':True,'telegram':bool(TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_CHAT_ID)}
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
async def count(request:Request,authorization:Optional[str]=Header(default=None)):
    if authorization!='Bearer local-adapter': require_app_key(authorization)
    b=await request.json(); return {'input_tokens':max(1,len(json.dumps(b.get('messages',[]),ensure_ascii=False))//4)}
@app.post('/v1/messages')
async def messages(request:Request,authorization:Optional[str]=Header(default=None)):
    if authorization!='Bearer local-adapter': require_app_key(authorization)
    b=await request.json(); u=await gateway_call(b)
    if u.status_code>=400:
        try: content=u.json()
        except: content={'error':u.text}
        return JSONResponse(status_code=u.status_code,content=content)
    d=u.json(); ch=(d.get('choices') or [{}])[0]; return JSONResponse(oa_to_anth(ch.get('message') or {},b.get('model') or DEFAULT_MODEL,d.get('usage') or {}))
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
        await tg_send(chat_id,'Claude Code is connected. Send a message to run it.\n\nCommands:\n/models — list models\n/model — current model\n/model MODEL_ID — switch model\n/clear — reset model'); return
    if text=='/models': await tg_send(chat_id,await tg_models()); return
    if text=='/model': await tg_send(chat_id,'Current model:\n'+telegram_model_by_chat.get(chat_id,DEFAULT_MODEL)); return
    if text.startswith('/model '):
        model=text[7:].strip()
        if not model: await tg_send(chat_id,'Usage: /model MODEL_ID'); return
        telegram_model_by_chat[chat_id]=model; await tg_send(chat_id,'Model switched to:\n'+model); return
    if text=='/clear': telegram_model_by_chat.pop(chat_id,None); await tg_send(chat_id,'Model reset to:\n'+DEFAULT_MODEL); return
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
    global telegram_task
    os.makedirs('/workspace',exist_ok=True)
    if TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_CHAT_ID: telegram_task=asyncio.create_task(telegram_loop())

@app.on_event('shutdown')
async def shutdown():
    if telegram_task:
        telegram_task.cancel()
        with suppress(asyncio.CancelledError): await telegram_task

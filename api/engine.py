from __future__ import annotations
import os, re, json, urllib.request

_ORDER = {"order_id":"SSG-20260630-10042","store_name":"온라인몰","ordered_at":"어제 20:15",
          "items":[{"name":"[생방송] 한우 1++ 선물세트 1.6kg","qty":1,"price":159000},{"name":"보냉백","qty":1,"price":0}],"status":"배송완료"}

def _dispatch(name, inp):
    if name=="lookup_recent_order": return {"found":True,**_ORDER}
    if name=="get_refund_policy":
        mx=sum(i["price"]*i["qty"] for i in _ORDER["items"])
        return {"eligible":True,"options":["refund","redelivery"],"max_refund":mx,"reason":"불량/오배송/단순변심에 따라 환불 또는 교환 처리"}
    if name=="quote_refund":
        pm={i["name"]:i["price"] for i in _ORDER["items"]}
        items=inp.get("missing_items",[]); total=sum(pm.get(it["name"],0)*int(it.get("qty",1)) for it in items)
        return {"refund_amount":total,"currency":"KRW"}
    if name=="confirm_refund": return {"refund_id":"RF-DEMO1234","status":"accepted","eta_days":3}
    if name=="request_redelivery": return {"redelivery_id":"RD-DEMO1234","eta_minutes":25}
    if name=="escalate_to_agent": return {"transferred":True,"queue_position":2}
    return {"error":f"unknown {name}"}

TOOLS=[
 {"name":"lookup_recent_order","description":"발신번호로 최근 주문 조회. 배달 문제 시 먼저.","parameters":{"type":"object","properties":{"phone":{"type":"string"}},"required":["phone"]}},
 {"name":"get_refund_policy","description":"누락/오배송 환불 가능여부·한도.","parameters":{"type":"object","properties":{"order_id":{"type":"string"},"issue_type":{"type":"string"}},"required":["order_id","issue_type"]}},
 {"name":"quote_refund","description":"환불 금액 산정(견적).","parameters":{"type":"object","properties":{"order_id":{"type":"string"},"missing_items":{"type":"array","items":{"type":"object","properties":{"name":{"type":"string"},"qty":{"type":"integer"}}}}},"required":["order_id","missing_items"]}},
 {"name":"confirm_refund","description":"환불 실제 접수(위험). 사용자가 금액 듣고 동의한 직후 user_confirmed=true.","parameters":{"type":"object","properties":{"order_id":{"type":"string"},"refund_amount":{"type":"integer"},"user_confirmed":{"type":"boolean"}},"required":["order_id","refund_amount","user_confirmed"]}},
 {"name":"request_redelivery","description":"재배달 접수.","parameters":{"type":"object","properties":{"order_id":{"type":"string"},"items":{"type":"array","items":{"type":"object"}}},"required":["order_id","items"]}},
 {"name":"escalate_to_agent","description":"상담사 전환.","parameters":{"type":"object","properties":{"reason":{"type":"string"},"summary":{"type":"string"}},"required":["reason","summary"]}},
]
_AFFIRM=re.compile(r"(네|예|응|좋아|그래|맞아|해주세요|할게|동의)")


PROMPT_INTEGRITY = ("너는 은행 고객센터의 아웃바운드 조사 상담원(콜봇)이다. 목적은 대출 가입 고객 대상 '여신거래 청렴도 조사'다. "
 "한국어 통화체로 1~2문장, 한 번에 한 가지만 정중히 질문한다.\n"
 "진행 순서(한 단계 끝나면 다음): (1)맞이인사와 본인 여부 확인('OOO 고객님 되십니까?') "
 "(2)본인확인: 생년월일 6자리를 말씀해 달라고 요청(맞으면 진행, 3회 틀리면 본인확인 실패로 정중히 종료) "
 "(3)통화목적 안내(은행 업무 관련 청렴도 조사, 약 2분, 통화 가능한지) "
 "(4)가입 여부 확인(지점과 일자에 가계대출 신규 가입이 맞는지) "
 "(5)부당요구 여부(임직원으로부터 대출 관련 부당한 담보·보증 요구 경험이 있었는지; 있다면 대상자와 내용을 물음) "
 "(6)자발적 가입 여부 (7)감사 인사 후 종료.\n"
 "규칙: 본인이 아니거나 통화를 거부하면 정중히 종료. 조사와 무관한 질문에는 조사 목적만 다시 짧게 안내. 항상 짧게 말한다.")

PROMPT_OVERDUE = ("너는 은행 고객센터의 아웃바운드 상담원(콜봇)이다. 목적은 대출 연체 안내다. "
 "한국어 통화체로 핵심 2문장 이내로만 말한다(쿠션어 제외).\n"
 "고객 발화의 의도를 분류해 응대한다:\n"
 "- 갚을 예정(곧 갚을게요 등): '상환 예정이시군요. 적은 금액이라도 연체되면 신용에 영향을 줄 수 있으니 관리 부탁드립니다.'\n"
 "- 이미 갚음(입금했어요 등): '미납정보는 오늘 오전 조회된 내용이며, 이미 납부하셨더라도 안내드릴 수 있습니다.'\n"
 "- 연체사실 모름(연체라니요 등): '오늘 오전 기준 연체 상황을 안내드렸습니다. 현재 미납금은 37,000원입니다.'\n"
 "- 통화종료 요청(끊을게요 등): 짧게 안내 후 '통화를 종료하겠습니다.'\n"
 "- 추가질의 없음(더 없어요 등): 감사 인사 후 종료.\n"
 "- 대출·연체 관련 일반 질문(원리금/원금균등 차이, 인지세, 만기 전 상환 비용 등): 간결·정확하게 2문장 이내로 답한다.\n"
 "- 부적절하거나 무관한 질문(날씨, 저녁 메뉴, 타행 영업시간, 통장잔고 두 배 등): '말씀하신 내용은 도와드리기 어렵습니다.'라고 거부한다.\n"
 "매 응대 뒤 '연체와 관련하여 더 궁금한 점 있으실까요?'로 이어가되, 종료 의사가 있으면 마무리한다. 항상 2문장 이내.")


def _sys(phone):
    return ("너는 온라인몰 고객센터 콜봇 CS 상담원이다. 한국어 통화체로 1~2문장, 한 번에 한 질문. 정중하고 또렷하게.\n"
            f"발신번호:{phone}\n주요 업무: 주문/배송 조회, 반품·교환·환불 접수. "
            "규칙:(1)주문/정책/금액은 반드시 툴 결과만 인용, 임의로 지어내지 말 것 "
            "(2)환불·교환은 고객이 금액·조건 듣고 명시 동의한 직후에만 user_confirmed=true로 호출 "
            "(3)한도초과/정책외/반복실패/상담원요청 시 escalate_to_agent.")

def _mem(messages):
    m={"order_id":None,"max_refund":None,"awaiting":False,"affirm":False,"transferred":False}; lu=""
    for x in messages:
        if x.get("role")=="user" and isinstance(x.get("content"),str): lu=x["content"]
        if x.get("role")=="tool":
            try: o=json.loads(x.get("content","{}"))
            except: o={}
            n=x.get("name")
            if n=="lookup_recent_order" and o.get("found"): m["order_id"]=o.get("order_id")
            if n=="get_refund_policy" and o.get("eligible"): m["max_refund"]=o.get("max_refund")
            if n=="quote_refund": m["awaiting"]=True
            if n in ("confirm_refund","request_redelivery"): m["awaiting"]=False
            if n=="escalate_to_agent": m["transferred"]=True
    m["affirm"]=bool(_AFFIRM.search(lu)); return m

def _guard(name,inp,m):
    if name=="confirm_refund":
        if not inp.get("user_confirmed"): return False,"사용자 확정 없이 환불 불가",False
        if not m["affirm"]: return False,"명시 동의 미확인",False
        a=int(inp.get("refund_amount",0))
        if m["max_refund"] is not None and a>m["max_refund"]: return False,f"한도 초과({a}>{m['max_refund']})",True
        if a<=0: return False,"금액 0 이하",False
        return True,"",False
    if name=="request_redelivery" and not m["affirm"]: return False,"재배달도 동의 후",False
    return True,"",False

def _to_contents(messages):
    out=[]
    for x in messages:
        r=x.get("role")
        if r=="user": out.append({"role":"user","parts":[{"text":x["content"]}]})
        elif r=="tool":
            try: resp=json.loads(x.get("content","{}"))
            except: resp={"result":x.get("content")}
            out.append({"role":"user","parts":[{"functionResponse":{"name":x.get("name"),"response":resp}}]})
        else:
            parts=[]
            if x.get("content"): parts.append({"text":x["content"]})
            for tc in x.get("tool_calls",[]): parts.append({"functionCall":{"name":tc["name"],"args":tc["input"]}})
            out.append({"role":"model","parts":parts or [{"text":""}]})
    return out

def _call(model,payload):
    key=(os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY") or "").strip()
    if not key: raise RuntimeError("GOOGLE_API_KEY 환경변수가 없습니다(Vercel 프로젝트 환경변수에 설정).")
    url=f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    req=urllib.request.Request(url,data=json.dumps(payload).encode(),headers={"Content-Type":"application/json"},method="POST")
    with urllib.request.urlopen(req,timeout=30) as r: return json.loads(r.read().decode())

def _parse(resp):
    t,calls="",[]
    cand=(resp.get("candidates") or [{}])[0]
    for p in (cand.get("content",{}) or {}).get("parts",[]) or []:
        if "text" in p: t+=p["text"]
        if "functionCall" in p:
            fc=p["functionCall"]; calls.append({"name":fc.get("name"),"args":fc.get("args",{}) or {}})
    um=resp.get("usageMetadata",{}) or {}
    return t.strip(),calls,(um.get("promptTokenCount",0),um.get("candidatesTokenCount",0))

def run_turn(messages,phone="01012345678",scenario="refund",max_hops=5):
    model=os.environ.get("CALLBOT_GEMINI_MODEL","gemini-2.5-flash")
    mem=_mem(messages); log=[]; usage={"input":0,"output":0}; msgs=list(messages)
    if scenario=="integrity": sysp=PROMPT_INTEGRITY; use_tools=False
    elif scenario=="overdue": sysp=PROMPT_OVERDUE; use_tools=False
    else: sysp=_sys(phone); use_tools=True
    for _ in range(max_hops):
        payload={"systemInstruction":{"parts":[{"text":sysp}]},"contents":_to_contents(msgs),
                 "generationConfig":{"maxOutputTokens":256}}
        if use_tools: payload["tools"]=[{"functionDeclarations":TOOLS}]
        resp=_call(model,payload); text,calls,(pi,po)=_parse(resp); usage["input"]+=pi; usage["output"]+=po
        if not calls:
            msgs.append({"role":"assistant","content":text}); log.append({"turn":"bot","text":text})
            return {"reply":text,"messages":msgs,"log":log,"usage":usage,"transferred":mem["transferred"]}
        msgs.append({"role":"assistant","content":text or "","tool_calls":[{"id":c["name"],"name":c["name"],"input":c["args"]} for c in calls]})
        for c in calls:
            ok,reason,esc=_guard(c["name"],c["args"],mem)
            if not ok:
                if esc: out=_dispatch("escalate_to_agent",{"reason":reason,"summary":str(c["args"])}); mem["transferred"]=True
                else: out={"blocked":True,"reason":reason}
                log.append({"turn":"guard","tool":c["name"],"blocked":reason})
            else:
                out=_dispatch(c["name"],c["args"]); log.append({"turn":"tool","tool":c["name"],"out":out})
                if c["name"]=="lookup_recent_order" and out.get("found"): mem["order_id"]=out.get("order_id")
                if c["name"]=="get_refund_policy" and out.get("eligible"): mem["max_refund"]=out.get("max_refund")
                if c["name"]=="quote_refund": mem["awaiting"]=True
            msgs.append({"role":"tool","tool_call_id":c["name"],"name":c["name"],"content":json.dumps(out,ensure_ascii=False)})
    return {"reply":"처리가 길어집니다. 상담사 연결할게요.","messages":msgs,"log":log,"usage":usage,"transferred":True}

from __future__ import annotations
import os, re, json, urllib.request
from datetime import datetime, timezone

# 주문/환불 연동은 order_backend 인터페이스로 위임한다(기본: DemoOrderBackend =
# 기존 하드코딩 데이터와 동일 응답). 실제 고객사 연동은 ORDER_BACKEND 환경변수로 교체.
try:
    from order_backend import get_backend, DEMO_ORDER  # Vercel 서버리스(api/ 평면 import)
except ImportError:  # 로컬에서 패키지처럼 import 되는 경우
    from api.order_backend import get_backend, DEMO_ORDER  # type: ignore

_ORDER = DEMO_ORDER  # 하위 호환(외부에서 참조하던 이름 유지)

def _dispatch(name, inp):
    return get_backend().dispatch(name, inp)

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
 "(4)가입 여부 확인(최근 가계대출을 신규로 가입하신 것이 맞는지; 특정 지점명·날짜 등 모르는 정보는 대괄호로 표시하지 말고 언급 자체를 생략) "
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

PROMPT_WELFARE = ("너는 '이음'의 AI 음성 상담원이다. 이음은 광주 광산구의 청년·어르신·아동 3세대 상생 품앗이 복지 플랫폼이다. "
 "한국어 통화체로 1~2문장, 한 번에 한 가지만 따뜻하게 안내한다. 어르신일 수 있으니 천천히·쉬운 말로 설명한다.\n"
 "진행: (1)이음 고객센터임을 밝히고 무엇을 도와드릴지 여쭙는다 (2)복지 신청(기초연금·돌봄·바우처 등) 의도를 파악하고 항목을 확인한다 "
 "(3)'신청 화면을 보내드렸다'고 안내하며 성함·생년월일 등 필요한 정보를 하나씩 여쭙는다 "
 "(4)자격 요건·필요 서류를 간단히 안내한다 (5)접수하고 '진행 상황은 문자로 안내드린다'고 마무리한다.\n"
 "규칙: 모르는 정보(지점명·구체 금액 등)는 지어내지 말고 담당 코디네이터 확인 후 안내한다고 말한다. 사람 상담을 원하면 코디네이터 연결을 제안한다. 항상 짧게 말한다.")

PROMPT_TRIO = ("너는 '이음'의 AI 음성 상담원이다. 이음은 광주 광산구의 청년·어르신·아동 3세대 상생 품앗이 플랫폼이다. "
 "한국어 통화체로 1~2문장, 한 번에 한 가지만 따뜻하게 안내한다.\n"
 "진행: (1)이음 고객센터임을 밝히고 3세대 매칭을 도와드린다고 안내한다 (2)참여 유형(청년·어르신·양육가정)을 확인한다 "
 "(3)'매칭 화면을 보내드렸다'고 안내하며 활동 가능한 요일·동네를 여쭙는다 "
 "(4)참여 전 4단계 안전검증(대면 면접·범죄경력·아동학대 전력·추천인)을 반드시 안내한다 (5)매칭 신청을 접수하고 '결과는 문자로 안내드린다'고 마무리한다.\n"
 "규칙: 안전검증은 생략하지 않는다. 모르는 정보는 지어내지 말고 코디네이터 확인 후 안내한다고 말한다. 항상 짧게 말한다.")


def _sys(phone):
    return ("너는 온라인몰 고객센터 콜봇 CS 상담원이다. 한국어 통화체로 1~2문장, 한 번에 한 질문. 정중하고 또렷하게.\n"
            f"발신번호:{phone}\n주요 업무: 주문/배송 조회, 반품·교환·환불 접수. "
            "규칙:(1)주문/정책/금액은 반드시 툴 결과만 인용, 임의로 지어내지 말 것 "
            "(2)환불·교환은 고객이 금액·조건 듣고 명시 동의한 직후에만 user_confirmed=true로 호출 "
            "(3)한도초과/정책외/반복실패/상담원요청 시 escalate_to_agent.")

def _mem(messages):
    m={"order_id":None,"max_refund":None,"quoted_amount":None,"awaiting":False,"affirm":False,"transferred":False}; lu=""
    for x in messages:
        if x.get("role")=="user" and isinstance(x.get("content"),str): lu=x["content"]
        if x.get("role")=="tool":
            try: o=json.loads(x.get("content","{}"))
            except: o={}
            n=x.get("name")
            if n=="lookup_recent_order" and o.get("found"): m["order_id"]=o.get("order_id")
            if n=="get_refund_policy" and o.get("eligible"): m["max_refund"]=o.get("max_refund")
            if n=="quote_refund":
                m["awaiting"]=True
                if o.get("refund_amount") is not None: m["quoted_amount"]=o.get("refund_amount")
            if n in ("confirm_refund","request_redelivery"): m["awaiting"]=False
            if n=="escalate_to_agent": m["transferred"]=True
    m["affirm"]=bool(_AFFIRM.search(lu)); return m

# 환불 실제 접수(confirm_refund) = 되돌리기 어려운 위험 동작.
# 아래 가드는 fail-safe: 조건 미충족 시 접수를 막고(esc=True면 상담사 전환) 안전한 방향으로만 실패한다.
# 실제 접수 자체는 order_backend 구현체가 담당하며(데모=가짜 응답, HTTP=ORDER_API_ALLOW_WRITE 필요),
# 이 가드는 그 앞단에서 "2단계 확인·금액 재확인"을 강제하는 관문이다.
def _guard(name,inp,m):
    if name=="confirm_refund":
        a=int(inp.get("refund_amount",0) or 0)
        # (1단계) LLM 이 넘긴 명시 확정 플래그
        if not inp.get("user_confirmed"): return False,"사용자 확정 없이 환불 불가",False
        # (2단계) 고객 발화상의 명시 동의(네/동의 등)
        if not m["affirm"]: return False,"명시 동의 미확인",False
        # 금액 유효성
        if a<=0: return False,"금액 0 이하",False
        # 2단계 확인: 사전 견적(quote_refund) 없이는 확정 불가 → 상담사 전환
        if not m.get("awaiting"): return False,"사전 견적(quote_refund) 없이 환불 확정 불가",True
        # 금액 재확인: 고객에게 안내한 견적 금액과 확정 금액이 일치해야 함 → 불일치 시 상담사 전환
        q=m.get("quoted_amount")
        if q is not None and a!=int(q): return False,f"견적 금액과 불일치(확정 {a} ≠ 견적 {q})",True
        # 정책 한도 초과 → 상담사 전환
        if m["max_refund"] is not None and a>m["max_refund"]: return False,f"한도 초과({a}>{m['max_refund']})",True
        return True,"",False
    if name=="request_redelivery" and not m["affirm"]: return False,"재배달도 동의 후",False
    return True,"",False

# 위험(되돌리기 어려운) 쓰기 툴 — 감사로그 대상
_RISKY_TOOLS={"confirm_refund","request_redelivery"}

def _audit(tool,inp,m,decision,reason):
    """환불/재배달 등 위험 동작의 시도·판정을 구조화 감사로그로 남긴다.
    (개인정보 최소화: 발신번호·상담내용 원문은 저장하지 않고 주문ID/금액/판정만 기록)"""
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "tool": tool,
        "order_id": inp.get("order_id"),
        "refund_amount": inp.get("refund_amount"),
        "quoted_amount": m.get("quoted_amount"),
        "max_refund": m.get("max_refund"),
        "user_confirmed": bool(inp.get("user_confirmed")),
        "affirm": bool(m.get("affirm")),
        "decision": "allow" if decision else "block",
        "reason": reason or "",
    }

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
    mem=_mem(messages); log=[]; audit=[]; usage={"input":0,"output":0}; msgs=list(messages)
    if scenario=="integrity": sysp=PROMPT_INTEGRITY; use_tools=False
    elif scenario=="overdue": sysp=PROMPT_OVERDUE; use_tools=False
    elif scenario=="welfare": sysp=PROMPT_WELFARE; use_tools=False
    elif scenario=="trio": sysp=PROMPT_TRIO; use_tools=False
    else: sysp=_sys(phone); use_tools=True
    for _ in range(max_hops):
        payload={"systemInstruction":{"parts":[{"text":sysp}]},"contents":_to_contents(msgs),
                 "generationConfig":{"maxOutputTokens":1024}}
        if use_tools: payload["tools"]=[{"functionDeclarations":TOOLS}]
        resp=_call(model,payload); text,calls,(pi,po)=_parse(resp); usage["input"]+=pi; usage["output"]+=po
        if not calls:
            msgs.append({"role":"assistant","content":text}); log.append({"turn":"bot","text":text})
            return {"reply":text,"messages":msgs,"log":log,"audit":audit,"usage":usage,"transferred":mem["transferred"]}
        msgs.append({"role":"assistant","content":text or "","tool_calls":[{"id":c["name"],"name":c["name"],"input":c["args"]} for c in calls]})
        for c in calls:
            ok,reason,esc=_guard(c["name"],c["args"],mem)
            # 위험 툴은 허용/차단 여부와 무관하게 감사로그 기록
            if c["name"] in _RISKY_TOOLS:
                audit.append(_audit(c["name"],c["args"],mem,ok,reason))
            if not ok:
                if esc: out=_dispatch("escalate_to_agent",{"reason":reason,"summary":str(c["args"])}); mem["transferred"]=True
                else: out={"blocked":True,"reason":reason}
                log.append({"turn":"guard","tool":c["name"],"blocked":reason})
            else:
                out=_dispatch(c["name"],c["args"]); log.append({"turn":"tool","tool":c["name"],"out":out})
                if c["name"]=="lookup_recent_order" and out.get("found"): mem["order_id"]=out.get("order_id")
                if c["name"]=="get_refund_policy" and out.get("eligible"): mem["max_refund"]=out.get("max_refund")
                if c["name"]=="quote_refund":
                    mem["awaiting"]=True
                    if out.get("refund_amount") is not None: mem["quoted_amount"]=out.get("refund_amount")
            msgs.append({"role":"tool","tool_call_id":c["name"],"name":c["name"],"content":json.dumps(out,ensure_ascii=False)})
    return {"reply":"처리가 길어집니다. 상담사 연결할게요.","messages":msgs,"log":log,"audit":audit,"usage":usage,"transferred":True}

from __future__ import annotations
import os, re, sys, json, urllib.request
from datetime import datetime, timezone

# 평면 import(Vercel 서버리스는 api/ 안에서 모듈을 찾는다)를 이 모듈이 **스스로**
# 보장한다 — 저장소의 다른 모듈(voice.py 등)과 같은 방식.
# 예전에는 importer 가 sys.path 를 미리 손봐 둔 것에 기대고 실패 시
# `api.order_backend`·`api.escalation` 으로 폴백했는데, 그 이름들은 `_` 접두 모듈로
# 바뀐 뒤로 존재하지 않아 폴백이 성립하지 않았다(ModuleNotFoundError). 더 나쁜 쪽은
# 에스컬레이션이었다 — 2단 폴백이 깨진 이름을 거쳐 `_ESC_QUEUE=None` 으로 떨어지므로
# 상담사 전환 티켓이 조용히 사라지는 경로였다.
_d = os.path.dirname(os.path.abspath(__file__))
if _d not in sys.path:
    sys.path.insert(0, _d)

# 주문/환불 연동은 order_backend 인터페이스로 위임한다(기본: DemoOrderBackend =
# 기존 하드코딩 데이터와 동일 응답). 실제 고객사 연동은 ORDER_BACKEND 환경변수로 교체.
# 이 import 는 감싸지 않는다 — 툴 실행부가 없으면 엔진은 할 일이 없고, 조용히
# 반쪽으로 동작하는 것보다 import 시점에 드러나는 편이 낫다(호출부 voice.py 가 흡수).
from _order_backend import get_backend, DEMO_ORDER

try:  # 에스컬레이션 큐(P0-4) — 모듈 없으면 조용히 비활성(기본 동작 불변)
    from _escalation import QUEUE as _ESC_QUEUE
except Exception:
    _ESC_QUEUE = None

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
 "규칙: 본인이 아니거나 통화를 거부하면 정중히 종료. 조사와 무관한 질문에는 조사 목적만 다시 짧게 안내. 항상 짧게 말한다.\n"
 "수치 규칙: 대출 잔액·금리·수수료·납기일처럼(예시이며 이에 한정하지 않는다) 이 통화에서 조회하지 않은 값은 말하지 않는다. "
 "고객이 물으면 지어내지 말고 '확인 후 안내드리겠습니다'라고 안내하거나 상담사 연결을 제안한다.")

PROMPT_OVERDUE = ("너는 은행 고객센터의 아웃바운드 상담원(콜봇)이다. 목적은 대출 연체 안내다. "
 "한국어 통화체로 핵심 2문장 이내로만 말한다(쿠션어 제외).\n"
 "고객 발화의 의도를 분류해 응대한다:\n"
 "- 갚을 예정(곧 갚을게요 등): '상환 예정이시군요. 적은 금액이라도 연체되면 신용에 영향을 줄 수 있으니 관리 부탁드립니다.'\n"
 "- 이미 갚음(입금했어요 등): '안내와 실제 납부가 엇갈릴 수 있습니다. 이미 납부하셨다면 그대로 처리됩니다.'\n"
 "- 연체사실 모름(연체라니요 등): '연체 안내를 드리려 연락드렸습니다. 정확한 미납 내역은 확인되는 대로 안내드리겠습니다.'\n"
 "- 통화종료 요청(끊을게요 등): 짧게 안내 후 '통화를 종료하겠습니다.'\n"
 "- 추가질의 없음(더 없어요 등): 감사 인사 후 종료.\n"
 "- 대출·연체 관련 일반 질문(원리금/원금균등 차이, 인지세, 만기 전 상환 비용 등): **제도·용어의 뜻만** 2문장 이내로 설명한다. "
 "요율·세액·금액·기간은 상품과 약정마다 다르고 이 통화에서 조회하지 않으므로 말하지 않는다"
 "('정확한 요율과 금액은 약정서 기준이라 확인 후 안내드리겠습니다'로 안내하거나 상담사 연결을 제안한다).\n"
 "- 부적절하거나 무관한 질문(날씨, 저녁 메뉴, 타행 영업시간, 통장잔고 두 배 등): '말씀하신 내용은 도와드리기 어렵습니다.'라고 거부한다.\n"
 "매 응대 뒤 '연체와 관련하여 더 궁금한 점 있으실까요?'로 이어가되, 종료 의사가 있으면 마무리한다. 항상 2문장 이내.\n"
 "수치 규칙: 미납 금액·연체일수·납기일·조회 시각·수수료율·세액·금리·한도처럼(예시이며 이에 한정하지 않는다) "
 "**시스템이 전달하지 않은 값은 말하지 않는다** — 일반 상식으로 아는 숫자라도 이 통화에서 조회한 값이 아니면 말하지 않는다. "
 "고객이 금액을 물으면 지어내지 말고 '정확한 금액은 확인되는 대로 안내드리겠습니다'라고 안내하거나 상담사 연결을 제안한다.")

# 왜 대본에서 금액을 뺐는가(28차)
# ---------------------------------------------------------------------------
# 예전 PROMPT_OVERDUE 는 「현재 미납금은 37,000원입니다」를 **봇이 말할 문장으로**
# 들고 있었다. 이 시나리오는 툴을 쓰지 않으므로(`use_tools=False`) 그 금액이
# 어디서도 조회되지 않는다 — 어떤 고객에게 걸어도 같은 숫자를 말하고, 그 말은
# 「은행이 내 돈에 대해 알려 준 금액」으로 들린다. 화면의 가짜 KPI 와 달리
# 워터마크를 붙일 자리가 없다(§8 가짜 수치 금지 · 27차 `queue_position` 과 같은 계열).
# 문장을 **지우지 않고 바꾼** 이유: 빈자리는 모델이 자기가 채운다 —
# 「모르면 모른다고 말하라」를 명시해야 그 자리가 닫힌다.
# 실제 미납 내역 연동(조회 툴)은 고객사 여신 시스템 연결이므로 **[승인 필요]**.
#
# 29차 — 같은 대본의 **일반 질의** 자리가 남아 있었다
# 「원리금/원금균등 차이, 인지세, 만기 전 상환 비용 등: 간결·정확하게 답한다」.
# 28차가 세운 수치 규칙은 '미납 금액·연체일수·납기일·조회 시각'만 열거했고,
# 바로 윗줄은 그 밖의 수치를 **정확하게 답하라고 지시**하고 있었다(규칙끼리 어긋남).
# 인지세는 약정 금액 구간별 정액에 은행·고객 분담이 걸리고, 만기 전 상환 비용은
# 상품·경과기간별 요율이다 — 둘 다 **고객 돈**이고 이 경로(`use_tools=False`)는
# 조회도 근거 문서 검색도 하지 않는다. 모델이 사전지식으로 말하면 그 숫자는
# 「은행이 내 약정에 대해 알려 준 값」으로 들리고, 틀려도 되돌릴 자리가 없다.
# → 개념 설명만 남기고 수치는 약정서·상담사로 넘긴다. 같은 공백이 있던
#   `PROMPT_INTEGRITY`(잔액·금리 질문)·`PROMPT_WELLBEING`(지원금·일정 질문)에도
#   같은 규칙을 적었다. 근거 문서 검색(RAG) 연결은 **[승인 필요]**.

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


PROMPT_WELLBEING = ("너는 지자체 안부확인 서비스의 AI 음성 상담원(콜봇)이다. 홀로 지내시는 어르신께 안부를 여쭙는 통화다. "
 "한국어 통화체로 1~2문장, 한 번에 한 가지만 천천히·따뜻하게 여쭙는다.\n"
 "진행: (1)안부확인 전화임을 밝히고 통화 괜찮으신지 여쭌다 (2)기분 (3)식사 (4)수면 (5)통증 순서로 "
 "네 가지를 하나씩 여쭙고, 답을 들으면 짧게 공감한 뒤 다음 질문으로 넘어간다 (6)감사 인사 후 마무리한다.\n"
 "규칙: 진단·약·치료 등 의료조언은 하지 않는다('담당 선생님께 여쭤보시는 게 좋겠다'로 안내). "
 "몸이 많이 안 좋거나 위급해 보이면 담당자에게 바로 알리겠다고 안내하고 통화를 마무리한다. "
 "성명·주민번호·계좌 등 개인정보는 묻지 않는다. 판매·권유를 하지 않는다. 항상 짧게 말한다.\n"
 "수치 규칙: 지원금·급여 금액, 방문 일정, 신청 기한처럼(예시이며 이에 한정하지 않는다) 전달받지 않은 값은 말하지 않는다. "
 "여쭤보시면 지어내지 말고 '담당자가 확인해 안내드리겠습니다'라고 안내한다.")


def _sys(phone):
    return ("너는 온라인몰 고객센터 콜봇 CS 상담원이다. 한국어 통화체로 1~2문장, 한 번에 한 질문. 정중하고 또렷하게.\n"
            f"발신번호:{phone}\n주요 업무: 주문/배송 조회, 반품·교환·환불 접수. "
            "규칙:(1)주문/정책/금액은 반드시 툴 결과만 인용, 임의로 지어내지 말 것 "
            "(2)환불·교환은 고객이 금액·조건 듣고 명시 동의한 직후에만 user_confirmed=true로 호출 "
            "(3)한도초과/정책외/반복실패/상담원요청 시 escalate_to_agent "
            "(4)대기 순번·대기시간·소요일수처럼 툴이 알려주지 않은 수치는 말하지 않는다"
            "(모르면 '확인되는 대로 안내드리겠습니다'로 안내).")

_MEM_KEYS=("order_id","max_refund","quoted_amount","awaiting","affirm","transferred")

def _mem_blank():
    return {"order_id":None,"max_refund":None,"quoted_amount":None,"awaiting":False,"affirm":False,"transferred":False}

def _mem_prior(prior):
    """**접혀 넘어온** 가드 기억을 받아들인다(32차). 읽을 수 없으면 유실로 표시한다.

    호출부가 이력을 잘라 저장할 때(`trim_history`) 창 밖으로 밀려난 앞부분의
    가드 상태를 이 사전으로 넘겨받는다 — 그래야 「옛 정책 한도 → 잡담 →
    최근 견적 → 동의」 조합에서도 한도 검사가 살아 있다.

    읽을 수 없는 값을 **조용히 빈 상태로** 바꾸면 안 된다: `max_refund` 만
    사라지고 견적·동의는 창 안에 남는 조합에서 `_guard` 의 한도 검사가
    통째로 건너뛰어진다(`if mx is not None`) — 차단이 아니라 **승인** 쪽으로
    틀리는 fail-open 이다(20차 `quoted_amount=None` 사고와 같은 자리).
    그래서 `lost` 를 세우고 `_guard` 가 확정을 막는다.
    """
    m=_mem_blank()
    if prior is None: return m
    if not isinstance(prior,dict):
        m["lost"]=True; return m
    for k in _MEM_KEYS:
        if k in prior: m[k]=prior[k]
    for k in ("awaiting","affirm","transferred"): m[k]=bool(m[k])
    if prior.get("lost"): m["lost"]=True
    return m

def _mem(messages,prior=None):
    m=_mem_prior(prior); lu=""; seen_user=False
    for x in messages if isinstance(messages,list) else []:
        if not isinstance(x,dict): continue
        if x.get("role")=="user" and isinstance(x.get("content"),str): lu=x["content"]; seen_user=True
        if x.get("role")=="tool":
            # 툴 결과 본문은 **바깥 값**이다(`/api/chat` 은 대화 이력을 클라이언트가 보낸다).
            # `"[]"`·`"3"` 처럼 객체가 아닌 JSON 이 오면 json.loads 는 예외를 내지 않으므로
            # 아래 o.get(...) 이 AttributeError 로 터져 **사용자 입력 오류가 500** 이 됐다.
            try: o=json.loads(x.get("content","{}"))
            except: o={}
            if not isinstance(o,dict): o={}
            n=x.get("name")
            if n=="lookup_recent_order" and o.get("found"): m["order_id"]=o.get("order_id")
            if n=="get_refund_policy" and o.get("eligible"): m["max_refund"]=o.get("max_refund")
            if n=="quote_refund":
                m["awaiting"]=True
                if o.get("refund_amount") is not None: m["quoted_amount"]=o.get("refund_amount")
            if n in ("confirm_refund","request_redelivery"): m["awaiting"]=False
            if n=="escalate_to_agent": m["transferred"]=True
    # 창 안에 고객 발화가 하나도 없으면 **접혀 온 판정을 유지한다** — 여기서
    # False 로 덮으면 전체 이력으로 계산한 값과 달라진다(창이 좁을 때만 나는 차이).
    if seen_user: m["affirm"]=bool(_AFFIRM.search(lu))
    return m

_RX_AMOUNT=re.compile(r"^-?\d+(?:\.\d+)?$")

def _amount(v):
    """금액류 값 → 정수. 해석할 수 없으면 None(가드가 '확인 불가'로 다룬다).

    확정 금액은 LLM 이, 견적·정책 한도는 주문 백엔드(ORDER_BACKEND=http 면 고객사
    REST API 의 임의 JSON)가 준다 — 셋 다 우리가 타입을 보장할 수 없는 바깥 값이다.
    예전에는 그대로 int()/비교에 넣었기 때문에 한도가 `"159000"`(문자열)로만 와도
    `int > str` TypeError 로 가드 안에서 터졌다. 환불이 나가지는 않았지만 **판정도
    감사기록도 남지 않고** 요청은 500 으로 끝났다 — 위험 툴 시도가 흔적 없이 사라지는
    쪽이 더 나쁘다. 해석 불가는 예외가 아니라 '차단' 판정으로 돌려준다(fail-safe).
    """
    if isinstance(v,bool): return None          # True==1 로 새어 들어오는 것 차단
    if isinstance(v,int): return v
    if isinstance(v,float): return int(v)
    if isinstance(v,str):
        s=v.strip().replace(",","").replace(" ","")
        if _RX_AMOUNT.match(s):
            try: return int(float(s))
            except Exception: return None
    return None

# 환불 실제 접수(confirm_refund) = 되돌리기 어려운 위험 동작.
# 아래 가드는 fail-safe: 조건 미충족 시 접수를 막고(esc=True면 상담사 전환) 안전한 방향으로만 실패한다.
# 실제 접수 자체는 order_backend 구현체가 담당하며(데모=가짜 응답, HTTP=ORDER_API_ALLOW_WRITE 필요),
# 이 가드는 그 앞단에서 "2단계 확인·금액 재확인"을 강제하는 관문이다.
def _guard(name,inp,m):
    # 툴 인자는 **모델 출력**이다(`functionCall.args`). dict 가 아니면(배열·문자열·숫자)
    # 판정도 실행도 할 수 없다 — 예전에는 아래 inp.get(...) 이 AttributeError 로 터져
    # 가드 **안에서** 요청이 500 으로 끝났고, 감사 append 는 이 함수 반환 뒤라
    # 위험 툴 시도가 흔적 없이 사라졌다(19차 `_amount` 결함과 같은 계열).
    # 해석 불가는 예외가 아니라 '차단' 판정으로 돌려준다(fail-safe).
    if not isinstance(inp,dict): return False,"툴 인자 형식 오류(객체가 아님)",False
    if name=="confirm_refund":
        # 접혀 온 가드 기억을 읽지 못한 경우(`_mem_prior`) — 견적·한도를 '모르는'
        # 상태다. 모르는 채로 확정하면 fail-open 이므로 차단 + 상담사 전환.
        if m.get("lost"): return False,"가드 기억 유실(견적·한도 확인 불가)",True
        a=_amount(inp.get("refund_amount"))
        # (1단계) LLM 이 넘긴 명시 확정 플래그
        if not inp.get("user_confirmed"): return False,"사용자 확정 없이 환불 불가",False
        # (2단계) 고객 발화상의 명시 동의(네/동의 등)
        if not m["affirm"]: return False,"명시 동의 미확인",False
        # 금액 유효성 — 숫자로 읽히지 않으면 금액을 '모르는' 상태이므로 확정하지 않는다
        if a is None: return False,"환불 금액 형식 오류",False
        if a<=0: return False,"금액 0 이하",False
        # 2단계 확인: 사전 견적(quote_refund) 없이는 확정 불가 → 상담사 전환
        if not m.get("awaiting"): return False,"사전 견적(quote_refund) 없이 환불 확정 불가",True
        # 금액 재확인: 고객에게 안내한 견적 금액과 확정 금액이 일치해야 함 → 불일치 시 상담사 전환
        # 견적 금액을 '모르는' 상태는 일치로 넘기지 않는다. 예전에는 quoted_amount 가
        # None 이면 이 검사를 건너뛰었는데, quote_refund 가 금액 없는 응답을 주는 경우
        # (HttpOrderBackend 의 `{"error":"backend_unavailable"}`·비JSON 본문 등)
        # awaiting 만 True 가 되어 **백엔드 장애가 금액 재확인을 꺼 버렸다** —
        # 정책 한도까지 미조회 상태면 임의 금액이 그대로 승인되는 fail-open 경로였다.
        q=m.get("quoted_amount")
        qa=_amount(q)
        if qa is None: return False,"견적 금액 확인 불가(견적 결과에 금액 없음)",True
        if a!=qa: return False,f"견적 금액과 불일치(확정 {a} ≠ 견적 {qa})",True
        # 정책 한도 초과 → 상담사 전환
        mx=m["max_refund"]
        if mx is not None:
            mxa=_amount(mx)
            if mxa is None: return False,"정책 한도 확인 불가(형식 오류)",True
            if a>mxa: return False,f"한도 초과({a}>{mxa})",True
        return True,"",False
    if name=="request_redelivery" and not m["affirm"]: return False,"재배달도 동의 후",False
    return True,"",False

# 위험(되돌리기 어려운) 쓰기 툴 — 감사로그 대상
_RISKY_TOOLS={"confirm_refund","request_redelivery"}

def _audit(tool,inp,m,decision,reason):
    """환불/재배달 등 위험 동작의 시도·판정을 구조화 감사로그로 남긴다.
    (개인정보 최소화: 발신번호·상담내용 원문은 저장하지 않고 주문ID/금액/판정만 기록)

    `inp` 이 dict 가 아닌 경우(모델이 배열·문자열을 준 경우)에도 **기록은 남는다** —
    위험 툴 시도가 기록되지 않는 쪽이 더 나쁘다."""
    if not isinstance(inp,dict): inp={}
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
    for x in messages if isinstance(messages,list) else []:
        if not isinstance(x,dict): continue
        r=x.get("role")
        # content 누락·비문자열은 여기서 죽지 않는다(입력검증은 각 라우트 책임).
        if r=="user": out.append({"role":"user","parts":[{"text":x.get("content") or ""}]})
        elif r=="tool":
            try: resp=json.loads(x.get("content","{}"))
            except: resp={"result":x.get("content")}
            out.append({"role":"user","parts":[{"functionResponse":{"name":x.get("name"),"response":resp}}]})
        else:
            parts=[]
            if x.get("content"): parts.append({"text":x["content"]})
            tcs=x.get("tool_calls")
            for tc in (tcs if isinstance(tcs,list) else []):
                # `tc["input"]` 은 KeyError 였다 — `/api/chat` 은 대화 이력을 클라이언트가
                # 보내고 `input` 은 검증되지 않았으므로 **사용자 입력 오류가 500** 이 됐다.
                # 라우트가 400 으로 지목하는 것이 1차 방어이고 여기는 2차 방어다.
                if not isinstance(tc,dict): continue
                a=tc.get("input")
                parts.append({"functionCall":{"name":tc.get("name"),"args":a if isinstance(a,dict) else {}}})
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
            fc=p["functionCall"]; a=fc.get("args")
            # `fc.get("args",{}) or {}` 였다 — 빈 배열·빈 문자열·0 처럼 **거짓인 비객체**를
            # 조용히 `{}` 로 갈아 끼웠다. 그러면 가드가 볼 기회조차 없이 툴이 빈 인자로
            # 실행된다: `quote_refund([])` 는 `awaiting=True` 와 견적 **0원**을 기억시켜
            # 뒤이은 금액 재확인을 0원 기준으로 바꾼다(가드를 여는 방향의 사고다).
            # 인자 없음(키 부재·null)만 `{}` 이고, 그 밖은 그대로 넘겨 가드가 차단한다.
            calls.append({"name":fc.get("name"),"args":{} if a is None else a})
    um=resp.get("usageMetadata",{}) or {}
    return t.strip(),calls,(um.get("promptTokenCount",0),um.get("candidatesTokenCount",0))

def _esc_note_failure(reason):
    """티켓 기록 실패를 **센다**(통화는 계속한다).

    예전에는 `except Exception: return None` 으로 끝이었다 — 고객에게는 이미
    "상담사에게 연결하겠습니다"라고 말했는데 그 통화를 집어갈 티켓이 아무 데도
    없고, 그 사실을 아는 사람도 없었다(오류 삼키기). 집계는 큐의
    `stats().record_errors` → `/api/ops_stats` → 콘솔로 드러난다.
    """
    try:
        _ESC_QUEUE.note_failure(reason)
    except Exception:
        # 큐 자체가 없거나 그마저 터진 경우. 이때는 `/api/ops_stats` 가
        # `escalation.source="unavailable"` 로 사실을 말하고, 이 통화의 응답
        # 로그에는 `recorded:false` 가 남는다(둘 다 조용하지 않다).
        pass

def _esc_ref(session_ref,scenario):
    """티켓에 적을 통화 참조(32차).

    예전에는 `"sim-%s"%scenario` **한 가지**였다 — 호출부가 `/api/chat`·sim 뿐일
    때 쓴 라벨인데 실통화(`/api/voice`)도 그 라벨로 들어왔다. 결과는 둘 다 나쁘다:
    한 인스턴스의 모든 티켓이 `sim-refund` 라서 **어느 통화인지 알 수 없고**,
    고객에게 "상담사에게 연결해 드리겠습니다"라고 말한 통화를 되짚을 수 없다.
    그리고 라벨이 `sim-` 이라 **실통화 전환이 시연으로 읽힌다**(운영자가 데모가
    만든 티켓으로 보고 넘긴다). 17·19·30차가 반복해 지적한 '규칙이 절반만
    지켜진' 모양이다 — sim 경로용 라벨을 실경로가 그대로 물려받았다.

    호출부가 참조를 주면 그것을 쓴다(`voice.py` 는 통화ID **지문**을 준다 —
    통화ID 는 CPaaS 가 주는 외부 값이라 번호가 섞여 들어올 수 있다).
    """
    if isinstance(session_ref,str) and session_ref.strip():
        return session_ref.strip()
    return "sim-%s"%(scenario or "call")

def _esc_enqueue(reason,summary,scenario="",session_ref=None):
    """상담사 전환 시 에스컬레이션 큐(escalation.QUEUE)에 티켓 기록 — best-effort sim.
    큐 기록 실패는 통화 흐름에 영향 주지 않지만 **드러낸다**(_esc_note_failure).
    실제 상담원 배정·CTI 연동은 [승인 필요]."""
    if _ESC_QUEUE is None:
        _esc_note_failure("queue_unavailable"); return None
    try:
        return _ESC_QUEUE.enqueue(session_id=_esc_ref(session_ref,scenario),reason=reason,summary=summary,scenario=scenario)
    except Exception as e:
        _esc_note_failure(type(e).__name__); return None

def _esc_log(tk,reason):
    """전환 기록 1줄 — 티켓이 없을 때도 남긴다(`recorded:false`).

    예전에는 `if tk:` 로 성공만 적었다. 기록되지 않은 전환이 응답 로그에서
    **아예 보이지 않는** 것이 가장 나쁘다(화면에는 '상담사 전환'만 남는다).
    """
    return {"turn":"escalation","ticket":(tk or {}).get("id"),
            "recorded":bool(tk),"reason":reason}

# --------------------------------------------------------------------------
# 모델에 보내는 대화 이력 창 (31차)
#
# 30차의 `voice._Session` 상한은 **통화 건수** 상한이었다. 이것은 한 통화
# **안쪽**의 상한이다. 호출부(`voice.py`)는 턴마다 `sess["messages"] =
# r["messages"]` 로 이력을 쌓고 매 턴 **전체를 다시** 보낸다 — 상한이 없으면
# 입력 토큰이 턴 수의 제곱으로 자라서 (1) LLM 과금이 함께 자라고 (2) 긴 통화는
# 결국 컨텍스트 한도에 부딪혀 **통화 중에** 죽는다(고객이 말하는 중에 끊긴다).
# `/api/chat` 은 라우트가 100건·8000자로 막아 두었지만(= 한 번에 80만 자)
# `/api/voice` 에는 막는 곳이 **한 군데도 없었다** — 또 절반만 지켜진 규칙이다.
#
# **줄이는 것은 모델이 보는 창뿐이다.** 가드가 보는 기억(`_mem`)은 여전히 전체
# 이력에서 계산한다 — 창을 줄였다고 `awaiting`·`quoted_amount`·`max_refund` 가
# 사라져 금액 재확인이 꺼지면 그것은 fail-open 이다(20차 `quoted_amount=None`
# 사고와 같은 자리). 창은 '모델의 기억', `_mem` 은 '가드의 기억'이고 둘은 다르다.
# --------------------------------------------------------------------------
HISTORY_MAX_MSGS = 40        # 창에 남기는 메시지 수(대략 최근 20턴)
HISTORY_MAX_CHARS = 24000    # 창 전체 본문 길이 상한(자) — 건수가 적어도 본문이 길면 같다

def _hist_limit(name,default):
    """상한은 호출 시점에 읽는다(배포 후 조정이 반영된다).

    해석 불가·0 이하는 기본값으로 되돌린다 — 설정 오타로 상한이 **사라지는**
    쪽으로 실패하지 않는다(`voice._session_limit` 과 같은 규약).
    """
    try:
        v=int((os.environ.get(name) or "").strip())
        return v if v>0 else default
    except Exception:
        return default

def _chars(m):
    """메시지 1건의 본문 길이. 문자열이 아닌 content 는 0 으로 본다."""
    if not isinstance(m,dict): return 0
    c=m.get("content")
    return len(c) if isinstance(c,str) else 0

def _window(messages):
    """모델에 보낼 이력을 최근 쪽으로 자르고 (창, 버린 건수) 를 돌려준다.

    자르는 단위는 메시지지만 **짝은 깨지 않는다** — 창의 머리에 `tool`
    (functionResponse)이 남으면 짝이 되는 functionCall 이 잘려 나간 것이고,
    그대로 보내면 API 가 400 으로 응답해 **통화 중에** 끊긴다. 그래서 자를
    위치를 뒤로 밀지 않고 **앞으로 당긴다**: 상한을 조금 넘더라도(연속 tool
    응답 수만큼) 통화를 끊지 않는 쪽으로 실패한다. 뒤로 밀면 창이 통째로
    비어 버리는 경우가 생긴다(마지막 메시지가 tool 인 홉 중간).
    """
    msgs=messages if isinstance(messages,list) else []
    n=len(msgs)
    if n<=1: return list(msgs),0
    cut=max(0,n-_hist_limit("CALLBOT_HISTORY_MAX",HISTORY_MAX_MSGS))
    budget=_hist_limit("CALLBOT_HISTORY_MAX_CHARS",HISTORY_MAX_CHARS)
    total=sum(_chars(m) for m in msgs[cut:])
    while cut<n-1 and total>budget:
        total-=_chars(msgs[cut]); cut+=1
    while cut>0 and isinstance(msgs[cut],dict) and msgs[cut].get("role")=="tool":
        cut-=1
    return msgs[cut:],cut

# --------------------------------------------------------------------------
# 저장하는 이력 자르기 (32차)
#
# 31차는 **모델이 보는 창**만 줄였다(`_window`). 호출부가 **저장하는** 이력에는
# 상한이 없었고 31차 보고서가 그 사실을 한계 ②로 적어 두었다 — 통화 1건의
# 수명(`voice.SESSION_TTL` 30분)이 사실상의 상한이었다. 그 사이 세션은 고객이
# 말한 전사 원문을 전부 들고 있다(영속 저장소는 [승인 필요]이므로 보관 기간이
# '웜 인스턴스 수명'이 되는 자리 — 30차가 세션 **건수**에서 고친 것과 같은 뿌리).
#
# 31차가 저장 쪽을 손대지 않은 이유는 fail-open 이었다: 가드의 기억(`_mem`)은
# 전체 이력에서 계산되므로 이력을 버리면 견적·한도가 함께 사라진다. 그래서
# 버리는 것이 아니라 **접어서 넘긴다** — 창 밖으로 밀려난 앞부분은 `_mem` 으로
# 접어 사전 1개로 만들고(발화 원문 없음·금액과 판정만), 다음 턴은 그 사전을
# `mem_prior` 로 받아 가드 기억을 **전체 이력과 동일하게** 복원한다.
#
#   _mem(창, prior=접힌것) == _mem(전체)        ← 회귀로 고정한 불변식
#
# 덤으로 개인정보 보관량이 줄어든다: 자르고 남는 것은 최근 창뿐이고, 접힌 쪽은
# 발화가 아니라 금액·판정 6칸이다.
# --------------------------------------------------------------------------
def trim_history(messages,prior=None):
    """(남길 이력, 접힌 가드 기억, 버린 건수).

    상한은 `_window` 와 **같은 눈금**을 쓴다 — 모델이 보는 창보다 더 들고 있을
    이유가 없고(그 이상은 통화 종료 요약에만 쓰인다), 두 상한이 따로 놀면
    어느 쪽이 실제 보관 기간인지 코드에서 읽히지 않는다.
    """
    msgs=messages if isinstance(messages,list) else []
    win,dropped=_window(msgs)
    # 접는 대상은 **버린 앞부분뿐**이다. 창 안쪽을 함께 접으면 다음 턴에 같은
    # 이력이 두 번 반영된다(`awaiting` 이 뒤집히는 순서 사고).
    folded=_mem(msgs[:dropped],prior=prior) if dropped else _mem_prior(prior)
    return list(win),folded,dropped

def run_turn(messages,phone="01012345678",scenario="refund",max_hops=5,
             mem_prior=None,session_ref=None):
    model=os.environ.get("CALLBOT_GEMINI_MODEL","gemini-2.5-flash")
    mem=_mem(messages,prior=mem_prior); log=[]; audit=[]; usage={"input":0,"output":0}; msgs=list(messages)
    if scenario=="integrity": sysp=PROMPT_INTEGRITY; use_tools=False
    elif scenario=="overdue": sysp=PROMPT_OVERDUE; use_tools=False
    elif scenario=="welfare": sysp=PROMPT_WELFARE; use_tools=False
    elif scenario=="trio": sysp=PROMPT_TRIO; use_tools=False
    elif scenario in ("wellbeing","안부"): sysp=PROMPT_WELLBEING; use_tools=False
    else: sysp=_sys(phone); use_tools=True
    noted=False
    for _ in range(max_hops):
        win,dropped=_window(msgs)
        if dropped and not noted:
            # 조용히 자르지 않는다 — 응답 로그에 사실이 남는다(건수만, 발화 없음).
            log.append({"turn":"context","dropped":dropped,"kept":len(win)}); noted=True
        payload={"systemInstruction":{"parts":[{"text":sysp}]},"contents":_to_contents(win),
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
                if esc:
                    out=_dispatch("escalate_to_agent",{"reason":reason,"summary":str(c["args"])}); mem["transferred"]=True
                    tk=_esc_enqueue("guard",reason,scenario,session_ref)
                    log.append(_esc_log(tk,reason))
                else: out={"blocked":True,"reason":reason}
                log.append({"turn":"guard","tool":c["name"],"blocked":reason})
            else:
                out=_dispatch(c["name"],c["args"]); log.append({"turn":"tool","tool":c["name"],"out":out})
                if c["name"]=="escalate_to_agent":
                    # 전환은 이 턴에서 즉시 확정한다. (과거엔 mem 이 갱신되지 않아
                    # transferred 가 다음 턴에야 True 가 되어 봇이 한 턴 더 응대했다)
                    mem["transferred"]=True
                    _r=c["args"].get("reason","request")
                    tk=_esc_enqueue(_r,c["args"].get("summary",""),scenario,session_ref)
                    log.append(_esc_log(tk,(tk or {}).get("reason") or _r))
                if c["name"]=="lookup_recent_order" and out.get("found"): mem["order_id"]=out.get("order_id")
                if c["name"]=="get_refund_policy" and out.get("eligible"): mem["max_refund"]=out.get("max_refund")
                if c["name"]=="quote_refund":
                    mem["awaiting"]=True
                    if out.get("refund_amount") is not None: mem["quoted_amount"]=out.get("refund_amount")
            msgs.append({"role":"tool","tool_call_id":c["name"],"name":c["name"],"content":json.dumps(out,ensure_ascii=False)})
    return {"reply":"처리가 길어집니다. 상담사 연결할게요.","messages":msgs,"log":log,"audit":audit,"usage":usage,"transferred":True}

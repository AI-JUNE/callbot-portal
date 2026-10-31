"""STT/TTS 프로바이더 인터페이스 추상화 (백로그 P0-1).

원칙: build now, activate on approval.
- 기본 프로바이더는 sim (키·네트워크 호출 없음, 결정적 응답).
- clova/google/aws 는 인터페이스 골격만 정의. 실호출 코드 없음.
- gowon 은 **우리가 학습한 전화 STT 모델 서버**를 부르는 실구현이다(아래 참조).
  설정이 없으면 골격과 똑같이 거부하므로 승인 전에는 아무 호출도 나가지 않는다.
- 비-sim 프로바이더 활성화는 SPEECH_LIVE=1 + 키 설정 필요. [승인 필요]
- 기존 api/stt.py·api/tts.py 엔드포인트는 변경하지 않음(추후 이 팩토리로 위임 예정).

사용:
  from _speech_providers import get_stt, get_tts
  r = get_stt().transcribe(audio_b64, "audio/webm")     # {"text":..., "provider":..., "sim":...}
  audio, meta = get_tts().synthesize("안내 문구")        # (bytes, {"provider":..., "sim":...})

셀프테스트:
  python api/speech_providers.py
"""
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request

_d = os.path.dirname(os.path.abspath(__file__))
if _d not in sys.path:
    sys.path.insert(0, _d)

# 아웃바운드 주소 검증은 보안 통제라 폴백을 두지 않는다 — 모듈을 못 불러오면
# 검증 없이 나가는 것보다 import 시점에 드러나는 편이 안전하다
# (`_vstudio`·`_order_backend` 와 같은 판단).
import _urlguard  # noqa: E402

GATE = "SPEECH_LIVE"            # 실호출 게이트. 기본 OFF


def is_live() -> bool:
    """실호출 게이트를 **호출 시점에** 읽는다(`"1"` 정확 일치, 기본 OFF).

    import 시점 상수로 두면 배포 후 환경변수를 바꿔도 이미 뜬 인스턴스에
    반영되지 않는다 — 켜는 쪽은 불편할 뿐이지만, **끄는 쪽은 비상정지가 듣지
    않는다**. 게다가 `health.py` 는 이 값을 호출 시점에 따로 읽어서, 헬스가
    "게이트 OFF" 라고 답하는 동안 팩토리는 라이브 프로바이더를 내주는
    엇갈림이 생긴다. 두 곳이 같은 순간의 같은 값을 보게 한다.
    (`sip_adapter.is_live()` 와 같은 규약.)
    """
    return (os.environ.get(GATE) or "").strip() == "1"


# ── 인터페이스 ────────────────────────────────────────────────
class STTProvider:
    """음성→텍스트. transcribe 는 dict 를 반환한다."""
    name = "base"

    def transcribe(self, audio_b64, mime="audio/webm", lang="ko"):
        raise NotImplementedError

    def health(self):
        return {"provider": self.name, "live": False, "ok": True}


class TTSProvider:
    """텍스트→음성. synthesize 는 (bytes, meta dict) 를 반환한다."""
    name = "base"

    def synthesize(self, text, voice=None, lang="ko"):
        raise NotImplementedError

    def health(self):
        return {"provider": self.name, "live": False, "ok": True}


# ── 기본 sim 구현 (키·네트워크 없음) ──────────────────────────
class SimSTT(STTProvider):
    name = "sim"

    def transcribe(self, audio_b64, mime="audio/webm", lang="ko"):
        n = len(audio_b64 or "")
        return {
            "text": "" if n == 0 else "[SIM 전사] 입력 %d바이트(b64) · 실STT 미호출" % n,
            "provider": self.name,
            "sim": True,
            "lang": lang,
        }


class SimTTS(TTSProvider):
    name = "sim"

    def synthesize(self, text, voice=None, lang="ko"):
        meta = {
            "provider": self.name,
            "sim": True,
            "voice": voice or "sim-default",
            "chars": len(text or ""),
            "lang": lang,
        }
        return b"", meta  # 오디오 미생성 · 과금 0


# ── 실연동 골격 (구현 금지 상태 · [승인 필요]) ────────────────
class _PendingApproval:
    """실호출 프로바이더 공통: 승인 전에는 어떤 호출도 불가."""

    def _deny(self):
        raise PermissionError(
            "[승인 필요] %s 실연동은 승인 후 활성화 (SPEECH_LIVE=1 + 키 설정). "
            "현재는 인터페이스 골격만 존재." % self.name
        )


class ClovaSTT(STTProvider, _PendingApproval):
    name = "clova"
    # 필요 env(예정): CLOVA_SPEECH_SECRET, CLOVA_SPEECH_INVOKE_URL

    def transcribe(self, audio_b64, mime="audio/webm", lang="ko"):
        self._deny()


class GoogleSTT(STTProvider, _PendingApproval):
    name = "google"
    # 필요 env(예정): GOOGLE_APPLICATION_CREDENTIALS (Cloud Speech-to-Text)

    def transcribe(self, audio_b64, mime="audio/webm", lang="ko"):
        self._deny()


class AwsSTT(STTProvider, _PendingApproval):
    name = "aws"
    # 필요 env(예정): AWS_ACCESS_KEY_ID/SECRET (Transcribe)

    def transcribe(self, audio_b64, mime="audio/webm", lang="ko"):
        self._deny()


class ClovaTTS(TTSProvider, _PendingApproval):
    name = "clova"

    def synthesize(self, text, voice=None, lang="ko"):
        self._deny()


class GoogleTTS(TTSProvider, _PendingApproval):
    name = "google"

    def synthesize(self, text, voice=None, lang="ko"):
        self._deny()


class AwsTTS(TTSProvider, _PendingApproval):
    name = "aws"

    def synthesize(self, text, voice=None, lang="ko"):
        self._deny()


# ── GOWON 전화 STT (자체 학습 모델 · 2026-10-31) ──────────────
# 창업진흥원 지원 GPU 에서 AI Hub 「저음질 전화망 음성인식」 데이터로 Whisper 를
# 미세조정한 모델을 **우리 서버**(`POST /v1/stt`)가 전사한다.
#
# 왜 대체 경로가 필수인가 (대표 결정 10-31)
#   모델은 로컬 PC 에 두고 CPU(faster-whisper)로 추론한다 — GPU 는 재학습할 때만
#   빌린다. 즉 **STT 서버는 꺼져 있을 수 있다**(노트북 종료·네트워크 단절).
#   그때 통화가 멈추면 안 되므로 레거시 Gemini 전사로 이어 간다. 다만 조용히
#   바꾸지 않는다 — 응답에 `fallback: True` 와 사유 코드를 싣고 구조화 로그에
#   한 줄 남긴다(QUALITY_BAR §3 「오류를 삼키고 아무 일 없는 척하기」 금지).
#   대체하지 않는 실패도 있다: 서명 거부(401)·주소 거부는 **우리 설정 오류**라
#   대체로 가리면 아무도 고치지 않는다(500 + 모니터링 경보로 드러낸다).
#
# 아웃바운드 (`_vstudio` 와 같은 수준)
#   · 주소는 환경변수지만 `_urlguard` 를 지난다 — 오타·잘못 복사한 값으로 나가면
#     **발화 원문과 HMAC 서명 헤더**가 그 주소로 흘러간다. 거부되면 요청 자체를
#     만들지 않는다(blind SSRF 미성립).
#   · 리다이렉트를 따라가지 않는다. urllib 은 302 를 따라갈 때 헤더를 그대로 다시
#     싣는다 — 받는 쪽이 바뀌면 그 본문에 대한 유효한 서명을 공짜로 얻는다.
#     한계(숨기지 않고 적는다): 중간 요청 자체는 이미 나간 뒤다. 그걸 막는 것은
#     호스트 화이트리스트(`GOWON_STT_HOSTS`)·아웃바운드 프록시 몫이다.
#   · 타임아웃 10초 · 응답 상한 64KiB(전사문 한 덩어리) · 비밀값과 발화 원문은
#     로그·응답·예외 문구에 싣지 않는다.
GOWON = "gowon"
GOWON_URL_ENV = "GOWON_STT_URL"             # 서버 주소(`/v1/stt` 까지)
GOWON_SECRET_ENV = "GOWON_STT_SECRET"       # HMAC 공유 비밀
GOWON_HOSTS_ENV = "GOWON_STT_HOSTS"         # 허용 호스트(콤마) — 가장 엄격한 운영 설정
GOWON_INSECURE_ENV = "GOWON_STT_ALLOW_INSECURE"   # 평문 http·사설망 허용(로컬 개발 전용)
GOWON_MODEL_ENV = "GOWON_STT_MODEL_LABEL"   # 리포트에 적을 모델 이름
GOWON_MODEL_DEFAULT = "large_v6"
GOWON_TIMEOUT = 10
GOWON_MAX_RESPONSE = 1 << 16                # 64KiB
# 전사 요청 본문 상한 — `_errors.MAX_BODY_AUDIO` 와 같은 눈금(라우트가 이미 막지만
# 이 클래스도 공개 API 다. 통제를 한쪽에만 두면 다른 쪽이 열린다).
GOWON_MAX_AUDIO_B64 = 8 << 20
# 실패 사유 코드(닫힌 목록) — 업스트림 문구를 그대로 로그에 옮기지 않는다.
GOWON_REASONS = ("timeout", "offline", "upstream", "bad_response",
                 "auth", "bad_audio", "audio_too_long", "blocked", "redirect")
# 대체 전사로 이어 가는 사유 = **서버에 닿지 못했거나 서버가 앓는** 경우만.
GOWON_FALLBACK_REASONS = ("timeout", "offline", "upstream", "bad_response")
_MODEL_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,40}$")


class _GowonFailure(Exception):
    """자체 STT 호출 실패 — 닫힌 사유 코드만 들고 다닌다(주소·비밀·발화 없음)."""

    def __init__(self, reason):
        self.reason = reason
        super(_GowonFailure, self).__init__("gowon stt %s" % reason)


def _env_of(env):
    return os.environ if env is None else env


def _flag(env, name):
    """`"1"` 정확 일치만 ON — 저장소의 다른 플래그와 같은 규약.

    `_urlguard.env_flag` 는 `os.environ` 을 직접 읽는다. 이 모듈의 함수들은
    호출자가 넘긴 `env` 로 판단해야 하므로(테스트·호출 시점 판독) 같은 규약을
    여기서 다시 쓴다(`_vstudio._flag` 와 같은 이유).
    """
    return ((env.get(name) or "").strip() if hasattr(env, "get") else "") == "1"


def _hosts(env, name):
    v = (env.get(name) or "").strip()
    return [x.strip().lower() for x in v.split(",") if x.strip()] if v else []


def gowon_model_label(env=None):
    """리포트에 적을 모델 이름. 형식을 벗어난 값은 쓰지 않는다(화면에 그대로 나간다)."""
    v = (_env_of(env).get(GOWON_MODEL_ENV) or "").strip()
    return v if _MODEL_RE.match(v) else GOWON_MODEL_DEFAULT


def check_gowon_url(url, env=None):
    """(ok, reason). 자체 STT 서버 주소로 나가도 되는가.

    판정 구현은 `_urlguard` 한 곳이다(안부 웹훅·녹음·주문 백엔드·보이스 엔진과
    공용) — 규칙을 여러 군데 적어 두면 한쪽만 고쳐지는 일이 반드시 생긴다.
    """
    env = _env_of(env)
    return _urlguard.check(url, label=GOWON_URL_ENV,
                           allow_insecure=_flag(env, GOWON_INSECURE_ENV),
                           allowlist=_hosts(env, GOWON_HOSTS_ENV))


def gowon_configured(env=None):
    """주소와 공유 비밀이 둘 다 있는가(값은 돌려주지 않는다)."""
    env = _env_of(env)
    return bool((env.get(GOWON_URL_ENV) or "").strip()
                and (env.get(GOWON_SECRET_ENV) or "").strip())


def gowon_health(live=None, env=None):
    """gowon 한 줄 상태 — **설정됨 + 게이트 ON** 일 때만 ready.

    리포트는 공개 응답(`/api/health`·`/api/stt`)으로 나간다 — 모델 이름만 싣고
    주소·비밀값은 싣지 않는다. 설정이 거부된 경우를 「미설정」으로 뭉개지 않는다
    (운영자가 등록해 둔 주소로 전사가 안 되는 이유를 헬스에서 알 수 있어야 한다).
    """
    env = _env_of(env)
    live = is_live() if live is None else bool(live)
    configured = gowon_configured(env)
    blocked = False
    if configured:
        blocked = not check_gowon_url((env.get(GOWON_URL_ENV) or "").strip(), env)[0]
    ready = bool(configured and live and not blocked)
    if ready:
        note = "자체 학습 전화 STT 모델로 전사합니다."
    elif blocked:
        note = ("[승인 필요] 서버 주소가 아웃바운드 가드에서 거부되었습니다. "
                "주소를 확인해 주세요. 지금은 전사하지 않습니다.")
    elif configured:
        note = ("[승인 필요] 승인 후 활성화합니다. 서버 설정은 끝났고 "
                "실호출 게이트만 꺼져 있습니다.")
    else:
        note = "[승인 필요] 승인 후 활성화합니다. 자체 STT 서버 설정이 아직 없습니다."
    return {"provider": GOWON, "status": "ready" if ready else "pending_approval",
            "ok": ready, "note": note, "model": gowon_model_label(env),
            "configured": configured, "blocked": blocked}


def sign(secret, ts, body):
    """자체 STT 서버와 같은 서명 — hex(HMAC-SHA256(secret, ts + "\\n" + body)).

    구현은 `_vstudio.sign` **한 곳**이다. 같은 규약을 두 군데 적어 두면 한쪽만
    고쳐지고, 그때 서버는 우리 서명을 거부한다(통화가 401 로 멈춘다).
    """
    import _vstudio
    return _vstudio.sign(secret, ts, body)


def _validation(field, reason, status):
    """입력 오류는 표준 봉투(4xx)로 — 서버가 지목한 칸을 그대로 전한다."""
    try:
        import _errors
        return _errors.ValidationError.field(
            field, reason, status=status, code=_errors.CODE_BY_STATUS.get(status))
    except Exception:       # pragma: no cover - _errors 부재 환경
        return ValueError("gowon stt invalid %s" % field)


def _gowon_reason(e):
    """예외 → 사유 코드(닫힌 목록). 업스트림 문구·주소는 보지 않는다."""
    if isinstance(e, _GowonFailure):
        return e.reason
    if isinstance(e, urllib.error.HTTPError):
        code = int(getattr(e, "code", 0) or 0)
        return {401: "auth", 400: "bad_audio", 413: "audio_too_long"}.get(code, "upstream")
    if isinstance(e, (socket.timeout, TimeoutError)):
        return "timeout"
    if isinstance(e, urllib.error.URLError):
        return "timeout" if isinstance(getattr(e, "reason", None),
                                      (socket.timeout, TimeoutError)) else "offline"
    if isinstance(e, (ValueError, TypeError)):      # JSON 파싱·형식 이탈
        return "bad_response"
    return "upstream"


def _gowon_raise(reason):
    """사유 코드 → 저장소 표준 분류(`_errors.classify`)에 맞는 예외. 반드시 던진다."""
    if reason == "timeout":
        raise TimeoutError("gowon stt timeout")                  # 504
    if reason == "auth":
        # 서명이 거부됐다 = 서버와 우리 설정이 다르다. 업스트림 장애(502)로 적으면
        # 「서버가 아픈 것」으로 묻힌다 — 500 + 모니터링 경보로 사람을 부른다.
        raise RuntimeError("gowon stt signature rejected")        # 500 + 경보
    if reason == "bad_audio":
        raise _validation("audio", "자체 STT 서버가 오디오를 읽지 못했습니다.", 400)
    if reason == "audio_too_long":
        raise _validation("audio", "자체 STT 서버가 받을 수 있는 길이를 넘었습니다.", 413)
    if reason == "blocked":
        # 사유는 설정 힌트라 예외 문구에 싣지 않는다(`_vstudio.synth_clone` 과 같은 규약).
        raise PermissionError("gowon stt url blocked")            # 500
    raise urllib.error.URLError("gowon stt %s" % reason)          # 502


def _gowon_log(reason, ms, fallback):
    """실패·대체 1건을 구조화 로그로 남긴다 — 발화·비밀값·주소는 싣지 않는다.

    조용한 폴백은 '장애가 없는 것처럼' 보이게 만든다. 사유 코드와 걸린 시간만
    남기므로 집계 카디널리티도 늘지 않는다(요청 로그의 보조 필드가 아니라
    별도 사건 1줄이다 — 요청 1건=1줄 규약을 건드리지 않는다).
    """
    try:
        import _log
        _log.emit({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "level": "warn",
            "service": getattr(_log, "SERVICE", "callbot-portal"),
            "event": "stt.gowon.failure",
            "provider": GOWON,
            "reason": reason if reason in GOWON_REASONS else "upstream",
            "fallback": bool(fallback),
            "duration_ms": int(ms),
        })
    except Exception:
        pass


def _gowon_open(req):
    """자체 STT 서버 호출 1회 — 리다이렉트 미추종 + 응답 상한. 실패는 전부 예외.

    최종 주소가 보낸 주소와 다르면 **받은 본문을 쓰지 않는다**. 응답 상한을
    넘으면 예외 — 잘린 본문을 '정상 응답'으로 파싱하지 않는다.
    """
    with urllib.request.urlopen(req, timeout=GOWON_TIMEOUT) as res:
        try:
            final = res.geturl() or ""
        except Exception:
            final = ""                  # 최종 주소를 못 읽으면 판단 재료가 없다
        if final and final != req.full_url:
            raise _GowonFailure("redirect")
        raw = res.read(GOWON_MAX_RESPONSE + 1)
    if len(raw) > GOWON_MAX_RESPONSE:
        raise _GowonFailure("bad_response")
    return raw


def _legacy_key():
    return bool((os.environ.get("GOOGLE_API_KEY")
                 or os.environ.get("GEMINI_API_KEY") or "").strip())


def _legacy_transcribe(audio_b64, mime):
    """레거시 Gemini 전사 → (글, 모델 이름). 구현은 `_stt.transcribe` 한 곳이다."""
    import _stt
    return (_stt.transcribe(audio_b64, mime or "audio/webm"),
            str(getattr(_stt, "MODEL", "") or "gemini")[:40])


class GowonSTT(STTProvider):
    """자체 학습 전화 STT 모델 서버로 전사한다.

    게이트: 실호출 게이트가 켜져 있고 주소·공유 비밀이 모두 있을 때만 호출한다.
    하나라도 없으면 다른 골격과 같은 PermissionError「[승인 필요]」 —
    승인 전에는 이 클래스에서 나가는 요청이 한 건도 없다.
    """

    name = GOWON

    def _deny(self, why):
        raise PermissionError(
            "[승인 필요] %s 실연동은 승인 후 활성화 (실호출 게이트 + 자체 STT 서버 설정). %s"
            % (self.name, why))

    def health(self):
        h = gowon_health()
        return {"provider": self.name, "live": h["ok"], "ok": h["ok"], "model": h["model"]}

    def transcribe(self, audio_b64, mime="audio/webm", lang="ko", env=None):
        env = _env_of(env)
        if not is_live():
            self._deny("실호출 게이트가 꺼져 있습니다.")
        if not gowon_configured(env):
            self._deny("서버 주소와 공유 비밀이 설정되지 않았습니다.")
        n = len(audio_b64 or "")
        if n == 0:
            raise _validation("audio", "전사할 오디오가 비어 있습니다.", 400)
        if n > GOWON_MAX_AUDIO_B64:
            raise _validation("audio", "오디오는 %d바이트(b64)까지입니다."
                              % GOWON_MAX_AUDIO_B64, 413)
        url = (env.get(GOWON_URL_ENV) or "").strip()
        if not check_gowon_url(url, env)[0]:
            # 거부된 주소로는 **요청을 만들지 않는다** — 발화와 서명 헤더가 그쪽으로
            # 가지 않는다. 설정 오류라 대체 전사로 가리지 않는다.
            return self._fail("blocked", 0, lang)
        body = json.dumps({"audio": audio_b64, "mime": mime or "audio/webm",
                           "lang": lang or "ko"}).encode("utf-8")
        ts = int(time.time())
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Content-Type": "application/json",
            "X-Timestamp": str(ts),
            "X-Signature": sign((env.get(GOWON_SECRET_ENV) or "").strip(), ts, body)})
        t0 = time.time()
        try:
            j = json.loads(_gowon_open(req).decode("utf-8"))
            if not isinstance(j, dict) or j.get("ok") is not True \
                    or not isinstance(j.get("text"), str):
                # 서버가 다른 모양으로 답한 것은 우리 버그가 아니라 업스트림 장애다.
                raise _GowonFailure("bad_response")
        except Exception as e:
            return self._fail(_gowon_reason(e), int((time.time() - t0) * 1000),
                              lang, audio_b64, mime)
        return {"text": j["text"].strip(), "provider": self.name, "sim": False,
                "lang": lang or "ko", "model": self._model_of(j, env),
                "ms": int((time.time() - t0) * 1000)}

    def _model_of(self, j, env):
        """서버가 밝힌 모델 이름(형식 검사 통과분)만 쓴다 — 아니면 설정 라벨."""
        v = str(j.get("model") or "").strip()
        return v if _MODEL_RE.match(v) else gowon_model_label(env)

    def _fail(self, reason, ms, lang, audio_b64=None, mime=None):
        """실패 1건 — 기록한 뒤, 대체 경로가 있으면 전사를 이어 간다.

        대체했으면 그 사실을 응답에 적는다(`fallback`·`fallback_reason`) — 호출부가
        「자체 모델이 전사했다」고 오해하면 품질 지표가 거짓이 된다. 모델 이름도
        실제로 전사한 쪽을 적는다.
        """
        if reason in GOWON_FALLBACK_REASONS and audio_b64 and _legacy_key():
            t1 = time.time()
            try:
                text, model = _legacy_transcribe(audio_b64, mime)
            except Exception:
                _gowon_log(reason, ms, False)       # 대체까지 실패 — 원 사유로 드러낸다
                _gowon_raise(reason)
            _gowon_log(reason, ms, True)
            # `ms` 는 호출부가 겪은 시간이다 — 실패한 시도와 대체 전사를 더한다
            # (자체 모델 수치로 읽히면 품질 지표가 거짓이 된다).
            return {"text": (text or "").strip(), "provider": self.name, "sim": False,
                    "lang": lang or "ko", "model": model,
                    "ms": int(ms + (time.time() - t1) * 1000),
                    "fallback": True, "fallback_provider": "gemini",
                    "fallback_reason": reason}
        _gowon_log(reason, ms, False)
        _gowon_raise(reason)


# ── 팩토리 ────────────────────────────────────────────────────
_STT = {"sim": SimSTT, "clova": ClovaSTT, "google": GoogleSTT, "aws": AwsSTT,
        GOWON: GowonSTT}
_TTS = {"sim": SimTTS, "clova": ClovaTTS, "google": GoogleTTS, "aws": AwsTTS}


def _pick(registry, env_key):
    want = (os.environ.get(env_key) or "sim").strip().lower()
    if want != "sim" and not is_live():
        # 게이트 OFF 상태에서는 무조건 sim 폴백 (실호출 원천 차단)
        return registry["sim"](), {"requested": want, "forced_sim": True}
    cls = registry.get(want, registry["sim"])
    return cls(), {"requested": want, "forced_sim": False}


def get_stt():
    p, _ = _pick(_STT, "CALLBOT_STT_PROVIDER")
    return p


def get_tts():
    p, _ = _pick(_TTS, "CALLBOT_TTS_PROVIDER")
    return p


# ── health 리포트 (읽기전용 · 실호출/네트워크 없음) ──────────
_LEGACY = {"stt": "gemini", "tts": "edge"}


def _provider_entry(name, live):
    """등록부 한 줄의 상태.

    sim 은 늘 ready, 골격은 늘 pending(실호출 코드가 없다), gowon 은 **설정이
    있고 게이트가 켜졌을 때만** ready — 설정이 없으면 골격과 똑같이 pending 이다.
    """
    if name == "sim":
        return {"provider": name, "status": "ready", "ok": True,
                "note": "sim · 키·과금 없음"}
    if name == GOWON:
        return gowon_health(live)
    return {"provider": name, "status": "pending_approval", "ok": False,
            "note": "[승인 필요] SPEECH_LIVE=1 + 키 설정"}


def _kind_health(registry, env_key, legacy):
    """단일 종류(stt/tts)의 프로바이더 상태. 인스턴스화·실호출 없음."""
    live = is_live()            # 한 리포트 안에서는 같은 순간의 값을 쓴다
    raw = (os.environ.get(env_key) or "").strip().lower()
    want = raw or legacy
    delegated = bool(raw) and raw != legacy
    if not delegated:
        known, forced_sim, effective = True, False, legacy
    else:
        known = want in registry
        forced_sim = (not known) or (want != "sim" and not live)
        effective = "sim" if forced_sim else want
    provs = [_provider_entry(n, live) for n in sorted(registry)]
    return {
        "requested": want,
        "legacy": legacy,
        "delegated": delegated,
        "known": known,
        "effective": effective,
        "forced_sim": forced_sim,
        "providers": provs,
    }


def health_report(kind="all"):
    """운영 점검용 프로바이더 health 요약.

    kind: "stt" | "tts" | "all". 반환값은 JSON 직렬화 가능한 dict.
    실호출·키 노출 없음. 게이트(SPEECH_LIVE) 상태만 노출한다.
    """
    live = is_live()
    rep = {"gate": GATE, "speech_live": live, "sim": not live}
    if kind in ("all", "stt"):
        rep["stt"] = _kind_health(_STT, "CALLBOT_STT_PROVIDER", _LEGACY["stt"])
    if kind in ("all", "tts"):
        rep["tts"] = _kind_health(_TTS, "CALLBOT_TTS_PROVIDER", _LEGACY["tts"])
    return rep


if __name__ == "__main__":     # pragma: no cover
    stt, tts = get_stt(), get_tts()
    print("STT:", stt.name, stt.transcribe("QUJD", "audio/webm"))
    audio, meta = tts.synthesize("테스트 안내 문구")
    print("TTS:", tts.name, len(audio), meta)
    for cls in (ClovaSTT, GowonSTT):
        try:
            cls().transcribe("QUJD")
            print("FAIL: deny 미작동 —", cls.__name__)
        except PermissionError as e:
            print("DENY OK:", e)
    import json as _j0
    # 콘솔 코드페이지(cp949)에서도 읽히도록 ASCII 로 접어 찍는다.
    print("GOWON:", _j0.dumps(gowon_health(live=True), ensure_ascii=True))
    import json as _j
    print("HEALTH:", _j.dumps(health_report(), ensure_ascii=False))

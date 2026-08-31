"""STT/TTS 프로바이더 인터페이스 추상화 (백로그 P0-1).

원칙: build now, activate on approval.
- 기본 프로바이더는 sim (키·네트워크 호출 없음, 결정적 응답).
- clova/google/aws 는 인터페이스 골격만 정의. 실호출 코드 없음.
- 비-sim 프로바이더 활성화는 SPEECH_LIVE=1 + 키 설정 필요. [승인 필요]
- 기존 api/stt.py·api/tts.py 엔드포인트는 변경하지 않음(추후 이 팩토리로 위임 예정).

사용:
  from speech_providers import get_stt, get_tts
  r = get_stt().transcribe(audio_b64, "audio/webm")     # {"text":..., "provider":..., "sim":...}
  audio, meta = get_tts().synthesize("안내 문구")        # (bytes, {"provider":..., "sim":...})

셀프테스트:
  python api/speech_providers.py
"""
import os

SPEECH_LIVE = os.environ.get("SPEECH_LIVE", "0") == "1"  # 실호출 게이트. 기본 OFF


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


# ── 팩토리 ────────────────────────────────────────────────────
_STT = {"sim": SimSTT, "clova": ClovaSTT, "google": GoogleSTT, "aws": AwsSTT}
_TTS = {"sim": SimTTS, "clova": ClovaTTS, "google": GoogleTTS, "aws": AwsTTS}


def _pick(registry, env_key):
    want = (os.environ.get(env_key) or "sim").strip().lower()
    if want != "sim" and not SPEECH_LIVE:
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


def _kind_health(registry, env_key, legacy):
    """단일 종류(stt/tts)의 프로바이더 상태. 인스턴스화·실호출 없음."""
    raw = (os.environ.get(env_key) or "").strip().lower()
    want = raw or legacy
    delegated = bool(raw) and raw != legacy
    if not delegated:
        known, forced_sim, effective = True, False, legacy
    else:
        known = want in registry
        forced_sim = (not known) or (want != "sim" and not SPEECH_LIVE)
        effective = "sim" if forced_sim else want
    provs = []
    for n in sorted(registry):
        pending = n != "sim"
        provs.append({
            "provider": n,
            "status": "pending_approval" if pending else "ready",
            "ok": not pending,
            "note": "[승인 필요] SPEECH_LIVE=1 + 키 설정" if pending else "sim · 키·과금 없음",
        })
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
    rep = {"gate": "SPEECH_LIVE", "speech_live": SPEECH_LIVE, "sim": not SPEECH_LIVE}
    if kind in ("all", "stt"):
        rep["stt"] = _kind_health(_STT, "CALLBOT_STT_PROVIDER", _LEGACY["stt"])
    if kind in ("all", "tts"):
        rep["tts"] = _kind_health(_TTS, "CALLBOT_TTS_PROVIDER", _LEGACY["tts"])
    return rep


if __name__ == "__main__":
    stt, tts = get_stt(), get_tts()
    print("STT:", stt.name, stt.transcribe("QUJD", "audio/webm"))
    audio, meta = tts.synthesize("테스트 안내 문구")
    print("TTS:", tts.name, len(audio), meta)
    try:
        ClovaSTT().transcribe("QUJD")
        print("FAIL: deny 미작동")
    except PermissionError as e:
        print("DENY OK:", e)
    import json as _j
    print("HEALTH:", _j.dumps(health_report(), ensure_ascii=False))

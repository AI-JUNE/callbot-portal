# RUNBOOK — 백업·복구 절차 (AICC Portal)

작성 2026-09-03. 대상: `callbot-portal` (라이브 https://callbot-portal.vercel.app).
**장애 중에 처음 읽는 문서가 되지 않도록, 분기마다 리허설한다.**

---

## 0. 무엇을 잃을 수 있는가 (백업 대상 인벤토리)

현재 아키텍처는 **무상태(stateless) 서버리스**다. DB가 없고, 녹취·감사 버퍼는
프로세스 메모리라 인스턴스 종료와 함께 사라진다(`/api/health` 의 `storage: simulated`
가 이 사실을 드러낸다). 따라서 **지금의 백업 문제는 "데이터 복구"가 아니라
"서비스 재구성"** 이다. 영속 스토리지를 배선하는 순간 이 표에 행이 추가된다.

| 자산 | 위치 | 백업 방법 | 주기 | 유실 시 영향 |
|---|---|---|---|---|
| 소스코드·이력 | GitHub `AI-JUNE/callbot-portal` + 로컬 작업본 | git 분산 복제(원격+로컬 2벌) + 오프라인 번들(§2) | 커밋마다 / 번들 주 1회 | **치명** — 서비스 재구성 불가 |
| 배포 설정 | `vercel.json`, `api/`, `public/` | 저장소에 포함 | 위와 동일 | 치명 |
| 시크릿(`GOOGLE_API_KEY` 등) | Vercel 환경변수 | **저장소에 없음.** 값은 발급처에서 재발급 가능 | 재발급으로 대체 | 중 — 재발급으로 복구 |
| 도메인·프로젝트 연결 | Vercel 프로젝트 설정 | 화면 캡처/설정 메모 **[승인 필요: 사람이 보관]** | 변경 시 | 중 |
| 통화 로그·녹취 | (미배선) 프로세스 메모리 | **백업 불가 — 설계상 휘발** | — | 낮음(현재 sim 전용) |
| 운영 지표 | 데모 데이터(하드코딩) | 저장소에 포함 | — | 낮음 |

> 시크릿은 절대 저장소·문서·이 RUNBOOK에 적지 않는다. 복구는 "찾기"가 아니라 "재발급"이다.

---

## 1. 목표 (RTO/RPO)

| 시나리오 | 목표 복구시간(RTO) | 목표 손실(RPO) | 근거 |
|---|---|---|---|
| 잘못된 배포 롤백 | 5분 | 0 | Vercel 이전 배포 즉시 승격 |
| 저장소 손상·GitHub 접근 불가 | 30분 | 마지막 번들 시점(≤7일) | §4-B 리허설 실측 30초 + 재배포 |
| Vercel 프로젝트 유실 | 60분 | 0(코드), 시크릿 재발급 필요 | §4-C |

목표치는 무상태 구조를 전제로 한 값이다. 영속 스토리지 배선 후에는 재산정한다.

---

## 2. 정기 백업 절차

### 2-1. 오프라인 번들 (주 1회 권장)
GitHub 계정 정지·저장소 삭제까지 견디려면 **플랫폼 밖에 한 벌**이 있어야 한다.

```powershell
cd "C:\Users\sukju\OneDrive\Desktop\Dev\2. Callbot\v2\callbot-portal"
git bundle create "..\backup\callbot-portal-$(Get-Date -f yyyyMMdd).bundle" --all
git bundle verify "..\backup\callbot-portal-$(Get-Date -f yyyyMMdd).bundle"
```
- 번들 1개 = 전체 이력. 현재 약 0.9 MiB.
- 저장 위치는 **작업본과 다른 매체**(외장 디스크/다른 클라우드). 같은 디스크에 두면 백업이 아니다.
- 보관: 최근 4주치 + 월말본 6개월.

### 2-2. 일상 백업
`auto-deploy.bat`(AutoPush)이 커밋·push 하는 순간 GitHub에 원격 복제본이 생긴다.
즉 **정상 운영 중 RPO는 사실상 0**이다. 번들은 그 원격이 사라지는 경우를 위한 보험이다.

---

## 3. 복구 시나리오

### A. 잘못된 배포를 되돌린다 (가장 흔함)
1. Vercel 대시보드 → 프로젝트 → **Deployments**
2. 마지막 정상 배포 → `⋯` → **Promote to Production**
3. `https://callbot-portal.vercel.app/api/health` 로 확인 — `status`, `version.commit` 이 의도한 커밋인지.
4. 그다음 저장소를 고친다. **`git reset`·강제 push 금지** — 되돌리는 커밋을 새로 쌓는다:
   ```powershell
   git revert <문제커밋>
   git push origin main
   ```

### B. 저장소가 손상됐거나 GitHub에 접근할 수 없다
1. 최신 번들 확보 → 무결성 먼저:
   ```powershell
   git bundle verify callbot-portal-YYYYMMDD.bundle
   ```
2. 빈 폴더에 복원(원본을 덮어쓰지 않는다):
   ```powershell
   git clone --branch main callbot-portal-YYYYMMDD.bundle callbot-portal-restored
   ```
3. 복원본 검증 — §4 리허설 스크립트를 그대로 돌린다.
4. 새 원격에 push 하거나, 복원본에서 `vercel --prod` 로 직접 배포(DEPLOY.md §4).

### C. Vercel 프로젝트가 사라졌다
1. 복원본(또는 로컬 작업본)에서 `vercel` → 새 프로젝트 생성 (DEPLOY.md §2).
2. **시크릿 재발급**: `GOOGLE_API_KEY` 는 발급처에서 새로 발급해 `vercel env add` 로 등록.
   기존 키는 폐기한다. 옛 키를 찾아 헤매지 않는다.
3. `CPAAS_LIVE` 는 **켜지 않는다**(기본 OFF 유지) — 복구 중 실발신은 사고다. **[승인 필요]**
4. `/api/health` 의 `dependencies[]` 로 무엇이 아직 미설정인지 확인 후 도메인 연결.

### D. 시크릿이 유출됐다
1. **먼저 폐기**, 그다음 조사. 발급처에서 키 revoke → 새 키 발급 → `vercel env` 교체 → 재배포.
2. `/api/health` 200 및 chat/assist 정상 응답 확인.
3. 감사 로그(`kind=audit`)에서 해당 기간 관리 기능 접근 이력 확인. 키 원문은 기록되지 않고
   sha256 앞 8자 지문만 남으므로, **지문으로 오용 시도를 묶어서** 본다.
4. 유출 경로가 저장소 커밋이면 이력에서 제거해야 하므로 **[승인 필요]** — 이력 재작성은 사람 판단.

### E. 외부 의존 서비스 장애 (LLM·CPaaS·STT/TTS)
서비스 전체를 내리지 않는다. `/api/health` 가 `degraded` 를 반환하고 해당 기능만 실패한다.
1. `/api/health?deep=1`(`HEALTH_DEEP=1` 필요)로 도달성 확인 — 자격증명 없이 TCP만 본다.
2. 우리 문제가 아니면 상태 공지 후 대기. 우리 문제면 §A 롤백.

---

## 4. 복구 리허설 (검증)

문서만 있고 해본 적 없는 절차는 백업이 아니다. `scripts/restore_drill.py` 는
**§2 백업 → §3-B 복원**을 실제로 수행하고 복원본이 배포 가능한 상태인지까지 확인한다.
읽기 전용이며(원본 미변경·네트워크 미사용) 산출물은 임시 디렉터리에만 만든다.

```bash
python3 scripts/restore_drill.py          # 사람이 읽는 출력
python3 scripts/restore_drill.py --json   # 기계 판독(CI용)
python3 scripts/restore_drill.py --keep   # 복원본 남겨 수동 확인
```

이 리허설은 **CI(`.github/workflows/ci.yml`)에서 main 으로 가는 모든 변경마다 자동 실행**된다.
아래 표는 사람이 수행한 정기 리허설 기록이고, 상시 회귀는 CI가 잡는다.

7단계: `backup`(번들 생성) → `verify_bundle`(무결성) → `restore`(빈 폴더에 클론)
→ `head_match`(원본 HEAD 일치) → `critical_files`(배포 필수 5종) → `py_compile`(api/*.py)
→ `html_parse`(public/*.html). 하나라도 실패하면 종료코드 1.

### 리허설 기록

| 일시(UTC) | 커밋 | 결과 | 소요 | 비고 |
|---|---|---|---|---|
| 2026-09-03T03:09Z | `4924230` | **성공 7/7** | 26.4s | 번들 0.9 MiB, HEAD 일치, api 20개 컴파일, public 7개 파싱 |

다음 리허설: 2026-12 (분기 1회). 실패하면 이 표에 실패로 남기고 원인·조치를 적는다.
성공만 기록되는 리허설 표는 리허설을 안 했다는 뜻이다.

---

## 5. 한계 · 승인 필요

- **영속 데이터 백업 없음** — 녹취·감사·통화 로그는 현재 휘발성 메모리. 외부 스토리지 배선 후
  스냅샷 주기·보존기간·파기 절차를 이 문서에 추가해야 한다. **[승인 필요]**
- **오프라인 번들은 수동** — 사람이 주 1회 실행. 자동화하려면 백업 매체 접근 권한이 필요. **[승인 필요]**
- **Vercel 프로젝트 설정 백업 부재** — 도메인·환경변수 목록(값 제외)은 사람이 별도 보관. **[승인 필요]**
- **리허설은 코드 복원까지만** 검증한다. 실제 프로덕션 재배포·도메인 전환은 사람이 수행한다.

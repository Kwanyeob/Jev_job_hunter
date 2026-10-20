> **Status (2026-10-19):** all three phases done. Phases 1–2 merged 2026-10-18; phase 3 deployed to Fly.io (`shortlist-kj`, Toronto) 2026-10-19, see `docs/OPERATIONS.md`.
> Changes from this plan:
> - The stdlib `jjh web` is kept for local single-user use, and the service is a new `server.py` instead of a rewrite.
> - Reads are capped at 5 per cycle, and a `pending` queue carries the rest (added after a live cycle hit three 429s).
> - A user's first cycle seeds a week of postings.

# Jev Job Hunter를 지인용 웹 서비스로 만들기

## Context
지금 `jjh web`은 **내 컴퓨터(127.0.0.1)에서 한 명이, 한 번에 한 검색만** 돌리는 도구입니다. 사용자는 이걸 **지인 1~2명에게 무료로 제공하는 완성된 서비스**로 만들고 싶어 합니다.

결정된 사항:
- **배포:** 클라우드 서버에 띄우고, 지인은 브라우저로 접속합니다. 지인은 설치나 키 설정을 하지 않습니다.
- **로그인:** 사람마다 비밀 초대 링크를 줍니다. 한 번 클릭하면 로그인이 유지되고, 회원가입은 없습니다.
- **주기 수집:** 사용자마다 각자 프로필로 30~60분 간격 자동 수집을 돌립니다.
- **이력서 읽기:** Claude Sonnet 5.5로 바꿉니다(현재는 Fable 5.1). 이력서 1건에 약 50원이고, 운영자 키 하나로 처리합니다.

기존의 `jjh linkedin` / `jjh watch` CLI는 그대로 유지합니다. 이전 계획 2개(watch)는 `docs/plans/`에 보관돼 있습니다.

## 구조
```
브라우저 ──HTTPS──► Fly.io 머신 1대 (항상 켜짐, 볼륨 /data)
                    ├─ 웹 앱 (Starlette + uvicorn)
                    │    초대 링크 → 서명된 세션 쿠키 → 내 프로필 / 대시보드 / Excel
                    └─ 스케줄러 스레드: 전 사용자 watch 회차를 한 번에 하나씩 실행
                         └─ 기존 run_cycle / GuestClient / Jev / Excel 재사용
/data/users.json                         사용자 목록 (이름, 토큰 해시, 상태)
/data/users/<uid>/config/profile.yaml    ← 기존 함수가 root로 받는 폴더 구조와 같음
/data/users/<uid>/state/linkedin-watch.json
/data/users/<uid>/results/linkedin-watch.xlsx
```

**핵심 재사용:** 기존 함수(`load_profile`, `WatchState.load`, `run_cycle`, `run_linkedin`, `append_log`)는 모두 `root/config`, `root/state`, `root/results` 구조를 씁니다. 그래서 **사용자 폴더를 `root`로 넘기면 고치지 않고 그대로 동작합니다.**

## 단계

### 1단계: 다중 사용자 웹 (로컬에서 실행 가능)
- **`jev_job_hunter/users.py` (새 파일)**
  - `create_invite(name)`: 무작위 토큰을 만들고 해시만 저장한 뒤 링크를 반환합니다.
  - `revoke(name, delete=False)`
  - `user_for_token`, `user_dir(uid)`
  - 저장은 `/data/users.json`에 원자적으로 씁니다(`WatchState.save`와 같은 방식).
- **세션 쿠키**
  - `SECRET_KEY`로 HMAC 서명합니다(stdlib `hmac`). 쿠키 옵션은 HttpOnly, Secure, SameSite=Lax이고, 유효 기간은 90일입니다.
  - 취소된 사용자는 즉시 차단합니다.
- **`web.py` → Starlette 앱으로 다시 작성**
  - stdlib `http.server`는 공식 문서에서 운영용으로 권장하지 않습니다. 인터넷에 공개하는 서비스라 교체합니다.
  - 라우트:
    - `/invite/<token>`: 쿠키를 발급하고 대시보드로 이동
    - `/`: 대시보드
    - `/api/state`
    - `/api/resume`: PDF 원본 본문을 받고 저장하지 않습니다. 프로필만 저장합니다.
    - `/api/profile`
    - `/api/watch` (켜기/끄기)
    - `/api/run-now`
    - `/api/events`: 사용자별 SSE
    - `/api/export.xlsx`
  - 모든 `/api/*`는 로그인이 필요하고, 다른 사용자 폴더에는 접근할 수 없습니다.
- **`web/index.html`**
  - 프로필 화면: 이력서 업로드 → 프로필 수정 → 저장
  - 대시보드 화면: 주기 수집 켜기/끄기, 마지막·다음 실행 시각, 누적 결과 표(fit 순, 새 공고 강조), 지금 검색, Excel 다운로드
- **`resume.py`**: `MODEL = "claude-sonnet-5-5"`, `output_config.effort = "medium"`. 거절 대비 `fallbacks: "default"`는 유지합니다(Sonnet 5.5 + Claude API에서 지원).
- **CLI**
  - `jjh invite <이름>` → 링크 출력
  - `jjh revoke <이름> [--delete]`
  - `jjh users`
  - `jjh serve`: 운영 서버(uvicorn + 스케줄러). 기존 `jjh web`은 로컬 단일 사용자 모드로 남깁니다.

### 2단계: 사용자별 주기 수집 스케줄러
- **`jev_job_hunter/scheduler.py` (새 파일)**
  - 스레드 1개가 사용자별 `next_run_at`을 보고, 실행할 회차를 **하나씩 차례로** 돌립니다.
  - 전역 잠금을 써서 LinkedIn 요청이 사용자 간에 겹치지 않게 합니다.
  - "지금 검색"도 같은 대기열로 들어갑니다.
- **기존 규칙을 그대로 씁니다:** `next_run_at`(30~60분, 01~07시 휴식, 막히면 2배·최대 4시간), `run_cycle`. 시간대는 `TZ=Asia/Seoul`입니다.
- **사용자 간 간격:** 앞 회차가 끝난 뒤 최소 1~3분(무작위)을 두고 다음 사용자 회차를 시작합니다.
- **사용량 한도:** 사용자당 검색어 최대 3개, 상세 읽기(`max_open`) 최대 10개입니다. Jev와 Anthropic 사용량을 운영자 키로 감당하기 위해서입니다.
- **상태 노출:** 회차 결과와 진행 상황을 해당 사용자의 SSE 스트림으로 보냅니다.
- **재시작 대비:** 서버가 재시작되면 각 사용자 상태 파일의 마지막 회차를 기준으로 다음 실행 시각을 다시 계산합니다.

### 3단계: 클라우드 배포 (Fly.io)
- **`Dockerfile`**: python:3.12-slim + uv, `jjh serve`
- **`fly.toml`**
  - 머신 1대, `auto_stop` 끔(스케줄러가 계속 돌아야 함)
  - 볼륨을 `/data`에 연결
  - 가까운 리전(도쿄 등)은 배포할 때 확인합니다.
- **Fly secrets:** `ANTHROPIC_API_KEY`, `TYPESAFE_API_KEY`, `SECRET_KEY`
- **배포 후 확인**
  - 서버에서 `jjh watch --once`를 한 번 실행해 **데이터센터 IP에서 LinkedIn이 막히는지 확인**합니다.
  - 막히면 사용자에게 알리고 대안(집 장비)을 다시 논의합니다.
- **운영 문서 `docs/OPERATIONS.md`:** 배포, 초대(`fly ssh console -C "jjh invite 이름"`), 취소, 로그 보기, 백업(볼륨 스냅샷)
- **사용자(운영자)가 직접 할 일:**
  - Fly.io 가입과 결제수단 등록 (월 약 $5 예상, 가입할 때 가격 확인)
  - `fly auth login`
  - Anthropic·TypeSafe 키 발급

## 의존성
- **추가:** `starlette`, `uvicorn`
- **유지:** `anthropic`, `httpx`, `openpyxl` 등
- **lock 파일:** `.venv`에 `pip install uv`를 한 뒤 `uv lock`으로 `uv.lock`을 갱신합니다. brew 설치는 필요 없습니다.

## 개인정보와 보안
- **이력서 PDF:** 저장하지 않습니다. 프로필만 저장하고, 기존 `normalize_profile`이 개인정보 키를 제거합니다.
- **초대 토큰:** 해시만 저장합니다. 링크는 생성할 때 한 번만 보여 줍니다.
- **삭제 요청:** `jjh revoke --delete`로 그 사용자 폴더를 지웁니다.
- **HTTPS:** Fly가 인증서를 자동으로 처리합니다.

## 검증
1. **단위·통합 테스트** (Starlette `TestClient`, 기존 `FakeLinkedIn`·가짜 시계 사용)
   - 초대: 링크 → 쿠키 → 접근 성공. 잘못된 토큰이나 취소된 사용자는 401/403. 서명이 위조된 쿠키는 거부
   - 사용자 격리: A의 쿠키로 B의 결과나 Excel에 접근할 수 없음
   - 이력서: Sonnet 모델 ID와 effort 확인(가짜 클라이언트). 이력서 PDF 원본은 디스크에 남지 않음
   - 스케줄러: 사용자 2명의 회차가 겹치지 않고 순서대로 실행됨, 사용자 간 간격, 끄기/켜기, 지금 검색, 재시작 후 일정 복원
   - 기존 36개 테스트 유지
2. **로컬 실행:** `jjh invite 테스트` → 링크로 접속 → 이력서 업로드(실제 Sonnet, 키 필요) → 주기 수집 켜기 → 1회차 결과 확인
3. **배포 후:** 서버에서 LinkedIn 차단 확인 → 운영자 본인 초대 링크로 전체 흐름 확인 → 지인 초대

## 진행 방식
단계마다 테스트를 통과시키고, 커밋한 뒤 사용자에게 확인을 받습니다. 첫 작업으로 "지인 서비스 목표"를 메모리에 저장합니다.

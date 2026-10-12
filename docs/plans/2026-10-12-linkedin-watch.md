# LinkedIn 주기 수집 (`jjh watch`) 계획

## Context
LinkedIn 수집을 HTTP로 바꿨습니다(아직 커밋하지 않음). 하지만 한 번 실행하면 끝나는 구조라서, 30~60분마다 새 공고만 가져오려면 다음 기능이 필요합니다.
- 이전에 본 job ID 기억
- 새 공고만 Jev로 평가
- 결과를 한 Excel 파일에 누적
- 회차 사이를 불규칙하게 대기

사용자가 정한 것:
- 터미널 루프 + `--once`
- 누적 Excel 한 파일
- 01~07시 쉬기
- 알림은 터미널 출력만

## 사용법 (완성 후)
```bash
uv run jjh watch                        # profile.yaml 기준, 30~60분 불규칙 간격 반복, Ctrl+C로 종료
uv run jjh watch --every 20-40 --quiet 0-8
uv run jjh watch --once                 # 1회만 (나중에 launchd/cron에 연결)
```
`jjh linkedin`과 같은 옵션(`--query`, `--location`, `--posted`, `--remote`, `--max-open`, `--delay`, `--profile`, `--mock`)을 지원합니다. 기본 `--posted`는 `day`입니다.

## 회차 1번의 동작
1. 검색어마다 1페이지(10개)만 요청합니다.
2. 10개가 **모두 새 공고**면, 놓친 공고가 있을 수 있으므로 다음 페이지를 받습니다. 검색어당 최대 3페이지입니다.
3. 이미 본 ID는 버립니다. 새 카드만 Jev로 평가하고, 상위 `max_open`개만 상세를 읽어 적합도를 매깁니다.
4. 상태 파일과 누적 Excel을 갱신합니다.
5. 터미널에 결과를 출력합니다.
   - 요약: `새 공고 N · 강한 매칭 M · 누적 X`
   - 강한 매칭(fit ≥ 0.7)은 제목, 회사, 적합도, 링크
   - 다음 실행 예정 시각

새 공고가 없으면 Jev 호출과 상세 읽기를 하지 않습니다. 이 경우 요청은 검색어 수만큼(2~3번)만 나갑니다.

## 회차 사이 대기
- **간격:** `--every 30-60` 범위에서 무작위로 정합니다.
- **야간:** 다음 실행 시각이 01~07시에 걸리면 07시 + 0~20분 무작위로 미룹니다.
- **막혔을 때:** 429나 로그인 화면으로 회차가 중단되면, 다음 간격을 2배로 늘립니다. 최대 4시간이고, 회차가 성공하면 원래대로 돌아옵니다.
- **노트북 절전 대비:** 대기는 짧게 끊어 자면서 현재 시각을 확인합니다. 절전에서 깨어났을 때 예정 시각이 지났으면 바로 실행합니다.

## 저장
- **상태 파일:** `state/linkedin-watch.json` (git 제외 폴더)
  - 담는 것: `{key, seen: {id: first_seen}, listings, matches, cycles: [최근 50회 요약]}`
  - `key`는 검색어, 지역, 필터로 만듭니다. 검색 조건이 바뀌면 경고를 출력하고 `state/linkedin-watch-<key>.json`으로 따로 저장합니다.
  - 임시 파일에 쓴 뒤 교체하는 방식으로 저장해, 중간에 꺼져도 파일이 깨지지 않게 합니다.
- **누적 Excel:** `results/linkedin-watch.xlsx`
  - 매 회차 `write_xlsx`로 다시 씁니다. Excel에서 파일을 열어 둔 상태면 기존 코드가 `-new` 파일로 저장합니다.
  - 두 시트에 `First seen` 열을 추가합니다. 이번 회차에 새로 들어온 행은 굵게 표시합니다.

## 코드 변경
- **`jev_job_hunter/linkedin.py`**
  - `run_linkedin`의 "평가 → 상세 읽기 → 적합도" 부분을 `score_cards(...)`로 분리하고, `run_linkedin`과 watch가 함께 씁니다. 동작은 바꾸지 않습니다.
  - `collect_cards`에 `skip_seen_page` 옵션을 추가합니다. 받은 페이지에 이미 본 ID가 하나라도 있으면 다음 페이지를 받지 않고, 최대 페이지 수는 지킵니다.
- **`jev_job_hunter/watch.py` (새 파일)**
  - `WatchState` 저장/불러오기
  - `run_cycle(...)` → 회차 요약 반환
  - `next_run_at(now, every, quiet, backoff)`: 순수 함수라 테스트하기 쉽습니다.
  - `watch(...)` 루프
- **`jev_job_hunter/excel.py`**
  - `MATCH_COLS`와 `LIST_COLS`에 `("First seen", 16, "first_seen")`를 추가합니다.
  - 새로 들어온 행을 강조하기 위해 `write_xlsx`에 `new_ids` 인자를 받게 합니다(기본값 없음). 기존 `jjh linkedin` 결과는 이 열이 빈칸이라 동작이 바뀌지 않습니다.
- **`jev_job_hunter/cli.py`**
  - `watch` 서브커맨드를 추가합니다. linkedin 옵션은 공통 함수로 묶어 재사용합니다.
- **`jev_job_hunter/board.py`**: 회차 요약 출력 함수 `print_watch_cycle`
- **문서:** `README.md`와 `skills/jev-job-hunter/SKILL.md`에 짧은 섹션 추가

재사용하는 기존 코드:
- `GuestClient`, `collect_cards`, `triage`, `read_posting`, `score_fit`, `hunt_meta` (`linkedin.py`)
- `write_xlsx` (`excel.py`)
- `append_log` (`store.py`)
- `load_profile` / `normalize_profile` (`profile.py`)

## 범위 밖 (이번에는 하지 않음)
- 웹 UI 연동
- macOS 알림
- launchd plist
- 오래된 공고 정리

## 검증
1. **단위 테스트** (`tests/test_watch.py`): `tests/test_linkedin.py`의 `FakeLinkedIn`/`fake_client`와 가짜 시계·대기 함수를 사용합니다.
   - 1회차에 카드 3개 평가 → 2회차는 같은 카드라 Jev 호출과 상세 읽기 0번, 검색 요청만
   - 3회차에 새 카드 1개 → 그 카드만 평가, Excel에 4행 누적, `First seen` 기록
   - 1페이지가 모두 새 공고면 2페이지를 받고, 이미 본 ID가 섞이면 멈춤
   - 상태 파일을 저장했다가 다시 불러와도 이미 본 ID가 유지됨
   - `next_run_at`: 30~60분 범위, 01~07시 회피, 막혔을 때 2배·최대 4시간·성공 시 원복
   - 429로 회차가 중단돼도 상태와 Excel 저장
   - 기존 29개 테스트 유지: `.venv/bin/python -m pytest -q`
2. **실제 LinkedIn 확인** (Jev는 `--mock`)
   - `jjh watch --once --max-open 2`를 실행하고, 이어서 한 번 더 실행합니다.
   - 2번째 실행은 "새 공고 0"이 나오고, 요청이 검색어 수만큼만 나가야 합니다.
   - `jjh watch --every 2-3 --quiet none`을 2회차까지 띄워 루프와 다음 실행 시각 출력을 확인한 뒤 Ctrl+C로 종료합니다.
3. **커밋 순서:** 이전 작업(HTTP 전환)을 먼저 커밋하고, watch 기능은 별도 커밋으로 올립니다.

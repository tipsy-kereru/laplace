# Laplace — 사용 가이드

**언어:** [English](USAGE.md) | 한국어

현실적 예시로 된 엔드투엔드 안내. 설치·철학·아키텍처는 먼저 [README](../README.kr.md) 읽기.

Laplace는 **대상 프로젝트 안**(Laplace가 작업할 코드베이스)에서 동작, `laplace/` 플러그인 리포 자체 안이 아님.

---

## 전제조건

- Claude Code에 Laplace 플러그인 설치 (`/plugin install laplace@laplace`)
- 작업 디렉토리 = 프로젝트 루트
- PATH에 `python3`, `git`
- `gh` CLI 인증 (`/laplace:create-pr`에만)

---

## 유스케이스 1 — 최초 설정과 건강 점검

목표: 런타임 작업공간 설치하고 플러그인 건강 확인.

```
/laplace:doctor
```

출력 (요약):

```
Laplace doctor.

1. plugin.json             pass
2. hooks.json              pass
3. skill frontmatter       pass (9 skills)
4. agent frontmatter       pass (5 agents)
5. state selftest          pass
6. policy selftest         pass
7. redaction selftest      pass
8. python3                 pass (3.11.x)
9. .harness/config.yml     warn (not initialized; run /laplace:init)
10. Moon Cell profile      warn

Overall: PASS WITH WARNINGS

Next:
  /laplace:init
```

init 전 두 경고 정상: `.harness/` 없음, Moon Cell 프로필 없음. 작업공간 초기화:

```
/laplace:init
```

`.harness/`를 config, 라우팅 룰, 상태 디렉토리 트리와 함께 생성. doctor 재실행 — 두 경고 해결 (또는 Moon Cell이 경고로 남고, 기본값에선 괜찮음).

런타임 상태를 커밋하고 싶지 않으면 프로젝트 `.gitignore`에 `.harness/` 추가:

```
.harness/
```

---

## 유스케이스 2 — 버그 수정: 전체 루프, 차단 없음

시나리오: PRD가 로그인 레이트리밋 버그 설명. PRD에서 PR까지 안내.

### 단계 1 — PRD 작성

`docs/prd-login-rate-limit.md`:

```markdown
# Bug: brute-force protection missing on login

## Background
The login endpoint has no rate limiting. Failed attempts are unbounded,
enabling credential stuffing.

## Acceptance criteria
- Per-IP login attempts capped at 5 per minute
- Excess attempts return HTTP 429
- Counter backed by Redis with 60s TTL
- Unit tests for the limiter
- No changes to the existing auth schema
```

### 단계 2 — Intake: PRD를 드래프트 이슈로 변환

```
/laplace:intake docs/prd-login-rate-limit.md
```

Laplace가 PRD 파싱해 `.harness/issues/`에 `draft` 상태로 하나 이상의 `ISSUE-NNNN` 기록 생성. PRD 모호하면 intake 중 모델이 범위 명확화.

### 단계 3 — 검토와 승인

```
/laplace:status
```

`ISSUE-0001`이 드래프트 큐에 있는지 확인. 점검:

```
/laplace:report ISSUE-0001
```

범위, 인수 기준, 위험 분류가 의도와 맞는지 검증. 이게 **인간 승인 게이트** — Laplace는 절대 자동 승인 안 함.

```
/laplace:approve ISSUE-0001
```

`.harness/state/approvals.jsonl`에 승인 기록, 이슈를 승인 큐로 이동.

### 단계 4 — 루프 실행

```
/laplace:run ISSUE-0001
```

루프:

1. 이슈 잠금 획득, 브랜치 `laplace/ISSUE-0001` 생성.
2. **PM 단계** — 범위, 인수 기준, 기술 노트 명확화. `ready` 또는 `blocked` 산출.
3. **Dev 단계** — 브랜치에 변경 + 테스트 구현, 테스트 증거 포착.
4. **Review 단계** — 인수 기준 대비 독립 코드 리뷰.
5. **Security 단계** — 보안 차원 리뷰 (이 변경은 auth 인접 코드 건드려서 security 실행).

각 전환은 `.harness/state/runs/<run-id>.json`에 증거 기록. 루프 `review-passed`에서 정지.

### 단계 5 — 상태와 로그 확인

```
/laplace:status
/laplace:report ISSUE-0001
```

리포트가 정제된 테스트 출력, 리뷰 평결, 보안 평결 렌더. 비밀은 영속 전 `scripts/redaction.py`로 마스킹, 리포트 공유 안전.

### 단계 6 — PR 생성

```
/laplace:create-pr ISSUE-0001
```

PR 드래프트 산출물 먼저 생성, 승인 기록, **명시적 인간 승인 후에만** GitHub PR 열기 (AC-LP-015). 조용히 PR 생성 안 됨.

---

## 유스케이스 3 — 의존성 게이트에 걸리는 기능 추가

시나리오: 변경이 새 npm 의존성 추가 필요. 의존성 추가는 **필수 인간-승인 카테고리** — 루프 정지.

### 루프가 게이트에서 정지

Dev 단계 중 dev 에이전트가 의존성 추가 인식. 루프가 패키지 직접 설치 대신 `human-approval-required`에서 정지. 상태:

```
/laplace:status
```

```
ISSUE-0002  state: human-approval-required
reason: dependency-add  (mongoose@8.0.0)
```

### 인간 결정

제안된 의존성 검토 (라이선스, 관리자, CVE 히스토리). 승인하면:

```
/laplace:approve ISSUE-0002
/laplace:run ISSUE-0002
```

루프가 정지 지점에서 재개. 거부면 `/laplace:cancel ISSUE-0002`가 결정 기록, 상태 보존.

---

## 유스케이스 4 — 취소와 재개

시나리오: 실행이 너무 오래 걸리거나 루프 중 범위 문제 발견. 안전 정지.

```
/laplace:cancel ISSUE-0003
```

cancel이 하는 것:

- 활성-루프 상태 정리, 이슈 잠금 해제
- 이슈 실행 히스토리에 취소 기록
- 브랜치나 산출물은 삭제 **안 함**

상태 보존. 재개:

```
/laplace:run ISSUE-0003
```

runner가 기존 브랜치 `laplace/ISSUE-0003` 감지, 재사용 (멱등). 루프가 마지막 합법 상태에서 재개.

---

## 유스케이스 5 — 막힌 이슈

시나리오: PM 단계가 범위 해결 못 함 — PRD가 내부 모순.

루프가 이슈를 `blocked`로 전환, 실행 종료. 실행 로그가 차단 이유 포착. 표면화:

```
/laplace:status
```

```
ISSUE-0004  state: blocked
blocker: acceptance criteria #2 and #3 are mutually exclusive
```

소스 문서나 이슈 메타데이터 해결 후 재실행:

```
/laplace:run ISSUE-0004
```

---

## 유스케이스 6 — 큐 실행: 여러 승인 이슈

시나리오: 세 이슈 승인, 하나하나 돌보지 않고 순서대로 실행 원함.

```
/laplace:approve ISSUE-0005
/laplace:approve ISSUE-0006
/laplace:approve ISSUE-0007
/laplace:run-queue
```

큐 러너가 승인 큐 헤드 잡아, `/laplace:run`으로 그 이슈 전체 루프 실행, `review-passed` 시 다음 승인 이슈로 진행. 기본 `wait-for-human-merge` 정책에선 첫 병합 게이트에서 정지:

```
Queue halted: merge-wait:ISSUE-0005
queue-run-id: q-7f3a...
queue_steps:
  - ISSUE-0005: review-passed (awaiting human merge)
Next: Merge branch laplace/ISSUE-0005 into base, then re-run /laplace:run-queue
```

브랜치를 base에 병합 후 큐 재개:

```
/laplace:run-queue
```

러너가 ISSUE-0006, ISSUE-0007 진행, 승인 큐 비면 `queue-exhausted` 출력. `blocked:<id>`나 `human-approval-required:<id>` 만나면 정상 예외 흐름으로 해결, 남은 큐 계속하려 `/laplace:run-queue` 재실행.

---

## 유스케이스 7 — 파이프라인: 전체 흐름 한 명령

시나리오: 관리자의 "이 PRD 잡아 릴리스까지 실행" 경로 원함. `/laplace:pipeline`이 intake, verify, approve, run-parallel, release를 단일 체크포인트 파이프라인으로 연결. 게이트 건너뛰지 않음 — 모든 게이트가 인간 결정에 정지. 게이트 사이 키스트로크 안내만 제거.

### 단계 1 — 파이프라인 시작

```
/laplace:pipeline docs/prd-login-rate-limit.md
```

파이프라인이 intake + verify 실행 후 verify 리포트와 이슈별 위험 테이블과 함께 **approve-gate**에서 정지:

```
Pipeline halt: approve-gate:ISSUE-0001=medium,ISSUE-0002=low
  Phase: approve-gate
  Drafts (issue=risk): ISSUE-0001=medium,ISSUE-0002=low
  Next: review the verify report above, then re-run /laplace:pipeline --resume to batch-approve all drafts.
```

### 단계 2 — 게이트 검토 후 재개

verify 리포트 (PASS/WARN)와 이슈별 위험 요약 읽기. 전부 수용하면 재개 — 파이프라인이 한 번에 모든 드래프트 일괄 승인:

```
/laplace:pipeline --resume
```

파이프라인이 **parallel** 단계로 진행, 승인 이슈 첫 웨이브 디스패치. `parallel:wave-dispatched:waiting` (in-flight 이슈를 종단으로 구동) 또는 `parallel:merge-wait:<id>` (이슈별 병합 게이트)에서 다시 정지.

### 단계 3 — 릴리스 게이트

parallel 단계가 `queue-exhausted` 도달 후 파이프라인이 **release-gate**에서 정지:

```
Pipeline halt: release-gate
  Phase: release-gate
  Next: /laplace:release <X.Y.Z>  (or re-run /laplace:pipeline --release <X.Y.Z> --resume)
```

`/laplace:release 0.5.0`을 별도 호출하거나 `--release 0.5.0` 전달 후 재개해 파이프라인이 release 호출 (8-점검 게이트 그대로 발화).

### 플래그

- `--auto-approve-low-risk` — approve-gate에서 Risk Level이 `low`인 드래프트 자동 승인; medium+ 드래프트 있으면 정지. 기본 OFF (승인 게이트는 항상 정지).
- `--release <X.Y.Z>` — release-gate에서 (queue-exhausted 후, 정지 이슈 없을 때) 정지 대신 `/laplace:release` 호출.
- `--max-parallel N` — `.harness/config.yml`의 `limits.max_parallel` 오버라이드.
- `--force-verify` — verify FAIL 통과 탈출구.
- `--resume` — 기록된 단계에서 명시적 재개 (같은 PRD 경로로 재호출도 암시적 재개).

### 파이프라인 취소

```
/laplace:cancel
```

활성 parallel 실행 있으면 첫 cancel이 정리; 두 번째 cancel이 파이프라인 로그 `cancelled`로 확정. cancel은 개별 이슈 건드리지 않음 — 감사용 상태 보존.

`/laplace:status`가 활성 파이프라인 보고 (단계, prd, 드래프트/승인/in-flight 카운트) 항상 위치 파악 가능.

---

## 명령 참조 (빠른)

| 명령 | 언제 |
|---|---|
| `/laplace:doctor` | 설치 후, 업그레이드 후, 이상 동작 시 |
| `/laplace:init` | 프로젝트당 한 번 |
| `/laplace:intake <prd>` | 변환할 PRD/스토리 준비됨 |
| `/laplace:verify [prd]` | intake 후, approve 전 — TBD 필드, 커버리지 갭, 깨진 ref 잡기 |
| `/laplace:approve <이슈>` | 드래프트 검토 후 큐에 넣길 원함 |
| `/laplace:discard <이슈>` | 드래프트가 실수로 생성되어 없어져야 함 (드래프트 전용) |
| `/laplace:run [이슈]` | 루프 실행 또는 재개 |
| `/laplace:run-queue [이슈]` | 여러 이슈 승인, 순서대로 실행 원함 |
| `/laplace:pipeline <prd>` | 한 명령으로 PRD 엔드투엔드 구동 — 매 게이트 정지, 재호출 시 재개 |
| `/laplace:status` | 큐, 활성 실행, 차단 확인 |
| `/laplace:report <이슈>` | 정제된 증거와 평결 검토 |
| `/laplace:cancel [이슈]` | 루프 안전 정지 (상태 보존) |
| `/laplace:create-pr <이슈>` | 이슈가 `review-passed`, PR 원함 |
| `/laplace:release <X.Y.Z>` | main 양호, 테스트 통과, 릴리스 자르길 원함 |

---

## 팁

- **작게 시작.** 실제 작업 전 흐름 배우려 하나의 사소한 이슈(문서 오타 수정) 엔드투엔드 실행.
- **루프는 정지하도록 설계.** 무인으로 완료까지 실행 기대 말 것 — 모든 위험 카테고리가 인간에게 정지.
- **`.harness/`는 빌드 상태.** 삭제 안전 (히스토리 손실), gitignore 안전.
- **리포트는 정제됨.** 비밀은 영속 전 마스킹; 붙여넣기 안전.
- **승인은 감사 가능.** 매 `approve`가 타임스탬프와 함께 `.harness/state/approvals.jsonl`에 append.
- **정책은 약화 불가.** 루프가 무언가 거부하면 (강제 푸시, 비밀 읽기, curl-pipe-sh) 그건 버그가 아니라 하드 안전 바닥.

### 유스케이스 — 승인 전 verify

intake는 기계적; TBD 필드, 잘못 파싱된 소제목, PRD 커버리지 갭 생성 가능. intake 후 approve 전 `/laplace:verify docs/prd-X.md` 실행해 한 번의 읽기 전용 패스로 표면화:

- 이슈별 PASS/WARN/FAIL 테이블 (TBD 필드, 깨진 `Source.Section`, AC 추적성 갭).
- PRD 커버리지 매트릭스 — 모든 `## Task:` 섹션이 이슈에 매핑, 또는 `ORPHAN` 플래그.
- 이슈 간 — 깨진 `depends_on` ref와 중복 AC (>80% 겹침, 경고).

verify는 상태 전환 안 하고 `/laplace:approve` 막지 않음. 자문용; 범위/위험 판단의 승인 게이트는 여전히 인간 소유. 종료 코드: `0` 깨끗하거나 경고만 / `1` 실패 있음 / `2` 사용법 오류.

### 유스케이스 — 버전 릴리스

Laplace 버전 릴리스는 5단계 의식 (3 파일 범프, 커밋, 태그, main 푸시, 태그 푸시)을 `/laplace:release`가 8-점검 게이트 뒤에서 자동화. 릴리스는 두 반쪽: 로컬 반쪽(`/laplace:release`)과 원격 반쪽(태그 푸시 시 CI 릴리스 워크플로).

**로컬 반쪽 — `/laplace:release <X.Y.Z>`**

```
/laplace:release 0.3.1
```

8 점검 순서 실행 (브랜치 = main, 형태 = `X.Y.Z`, 테스트 통과, 범프 후 3-파일 동기화, semver가 업그레이드, 트리 깨끗, 태그 부재, 원격 ahead 아님, 대기 중 승인 이슈 없음). 실패 시: 해결 메시지와 정지, 부작용 없음, 종료 1. 전부 통과 시: `VERSION` + `.claude-plugin/plugin.json` + `.claude-plugin/marketplace.json` 범프, `chore(release): bump <old> -> <new>` 커밋, `v<X.Y.Z>` 태그, main 푸시, 태그 푸시.

`/laplace:release` 호출 자체가 푸시의 인가 (옵션 A, `/laplace:create-pr`와 동일). 푸시는 되돌릴 수 없음; 8-점검 게이트가 가드레일. 매 시도가 `.harness/state/releases.jsonl`에 append (성공: `{checks_passed: true, ...}`; 정지: `{checks_passed: false, failed_check, reason}`).

`--force`는 다운그레이드(점검 4)와 대기-승인(점검 8) 점검만 완화. 형태, 테스트, 동기화, 트리-깨끗, 태그-부재, 원격 점검은 절대 건너뜀 않음.

**부분-푸시 복구 (R-2)**: main 푸시 성공했는데 태그 푸시 실패 (네트워크 순간 오류), `/laplace:release`가 `PARTIAL RELEASE: main pushed, tag push failed`로 정지. main 롤백 안 함 (커밋 이미 공개). 수동 복구: `git push origin v<X.Y.Z>`.

**원격 반쪽 — CI 릴리스 워크플로**

기존 `.github/workflows/release.yml` (변경 없음)이 태그 푸시 시 발화, 3-way 버전 일치 검증, 커밋에서 생성된 노트로 GitHub Release 생성. `/laplace:release`가 로컬 반쪽; CI가 원격 반쪽.

---

## 트러블슈팅

| 증상 | 원인 | 해결 |
|---|---|---|
| `/laplace:*` 안 찾음 | 플러그인 미설치 또는 마켓플레이스 캐시 오래됨 | `/plugin marketplace remove tipsy-kereru/laplace` 후 재추가 + 재설치 |
| `doctor`가 `state selftest fail` | Python 또는 표준 라이브러리 문제 | `python3 --version` (3.7+ 필요) |
| `run`이 "not a git repo" | 작업 dir가 git 리포 아님 | 프로젝트에 `git init` 또는 다른 곳에서 실행 |
| `create-pr`이 `gh` 미인증 | `gh` 부재 또는 로그아웃 | `! gh auth login` |
| 루프가 계속 `human-approval-required`에서 정지 | 의도대로 — 그 카테고리는 인간 필요 | `/laplace:approve <이슈>` 후 `/laplace:run <이슈>` |

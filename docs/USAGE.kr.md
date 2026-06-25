# Laplace — 사용 가이드

**언어:** [English](USAGE.md) | 한국어

현실적인 예시로 이루어진 엔드투엔드 안내입니다. 설치와 철학, 아키텍처는 먼저 [README](../README.kr.md)를 읽어주세요.

Laplace는 **당신이 작업하려는 대상 프로젝트 안**에서 동작합니다. `laplace/` 플러그인 저장소 자체 안에서 동작하는 것이 아닙니다.

---

## 전제조건

- Claude Code에 Laplace 플러그인이 설치되어 있을 것 (`/plugin install laplace@laplace`)
- 작업 디렉토리는 프로젝트 루트일 것
- PATH에 `python3`와 `git`이 있을 것
- `gh` CLI가 인증되어 있을 것 (`/laplace:create-pr`에만 필요)

---

## 유스케이스 1 — 최초 설정과 건강 점검

목표는 런타임 작업 공간을 설치하고 플러그인이 건강한지 확인하는 것입니다.

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

init을 하기 전의 두 경고는 정상입니다. `.harness/`가 없다는 것과 Moon Cell 프로필이 없다는 것입니다. 작업 공간을 초기화합니다.

```
/laplace:init
```

이 명령은 config와 라우팅 규칙, 그리고 상태 디렉토리 트리와 함께 `.harness/`를 만듭니다. doctor를 다시 실행하면 두 경고가 해결됩니다 (Moon Cell은 경고로 남을 수 있는데, 기본값에서는 괜찮습니다).

런타임 상태를 커밋하고 싶지 않다면 프로젝트의 `.gitignore`에 `.harness/`를 추가하세요.

```
.harness/
```

---

## 유스케이스 2 — 버그 수정: 차단 없는 전체 루프

시나리오는 이렇습니다. PRD 하나가 로그인 레이트리밋 버그를 설명합니다. PRD에서 PR까지 걸어봅니다.

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

Laplace가 PRD를 파싱하여 `.harness/issues/` 아래에 `draft` 상태인 `ISSUE-NNNN` 기록을 하나 이상 만듭니다. PRD가 모호하면 모델이 intake 도중에 범위를 명확히 물어봅니다.

### 단계 3 — 검토와 승인

```
/laplace:status
```

`ISSUE-0001`이 드래프트 큐에 앉아있는지 확인합니다. 내용을 점검합니다.

```
/laplace:report ISSUE-0001
```

범위와 인수 기준, 위험 분류가 당신의 의도와 맞는지 검증합니다. 이것이 **사람 승인 게이트**입니다. Laplace는 절대 자동으로 승인하지 않습니다.

```
/laplace:approve ISSUE-0001
```

승인을 `.harness/state/approvals.jsonl`에 기록하고, 이슈를 승인 큐로 옮깁니다.

### 단계 4 — 루프 실행

```
/laplace:run ISSUE-0001
```

루프는 다음을 수행합니다.

1. 이슈 잠금을 획득하고 브랜치 `laplace/ISSUE-0001`을 만듭니다.
2. **PM 단계** — 범위, 인수 기준, 기술 노트를 명확히 합니다. 결과로 `ready` 또는 `blocked`가 나옵니다.
3. **Dev 단계** — 브랜치 위에서 변경과 테스트를 구현하고, 테스트 증거를 포착합니다.
4. **Review 단계** — 인수 기준에 대한 독립 코드 리뷰를 수행합니다.
5. **Security 단계** — 보안 차원의 리뷰를 수행합니다. 이 변경은 auth와 인접한 코드를 건드리므로 security가 실행됩니다.

각 전환은 `.harness/state/runs/<run-id>.json`에 증거를 기록합니다. 루프는 `review-passed`에서 멈춥니다.

### 단계 5 — 상태와 로그 확인

```
/laplace:status
/laplace:report ISSUE-0001
```

리포트는 정제된 테스트 출력과 리뷰 평결, 보안 평결을 렌더링합니다. 비밀은 영속되기 전에 `scripts/redaction.py`로 마스킹되므로, 리포트는 공유해도 안전합니다.

### 단계 6 — PR 만들기

```
/laplace:create-pr ISSUE-0001
```

PR 드래프트 산출물을 먼저 만들고, 승인 기록을 남긴 다음, **명시적인 사람 승인이 있은 후에만** GitHub PR을 엽니다 (AC-LP-015). 조용히 PR이 만들어지는 일은 없습니다.

---

## 유스케이스 3 — 의존성 게이트에 걸리는 기능 추가

시나리오는 이렇습니다. 변경이 새 npm 의존성을 추가해야 합니다. 의존성 추가는 **필수적인 사람 승인 카테고리**라서 루프가 멈춥니다.

### 루프가 게이트에서 멈춤

Dev 단계 도중에 dev 에이전트가 의존성 추가를 인식합니다. 루프는 패키지를 스스로 설치하는 대신 `human-approval-required`에서 멈춥니다. 상태를 봅니다.

```
/laplace:status
```

```
ISSUE-0002  state: human-approval-required
reason: dependency-add  (mongoose@8.0.0)
```

### 사람이 결정

제안된 의존성을 검토합니다 (라이선스, 관리자, CVE 히스토리). 승인한다면 이렇게 합니다.

```
/laplace:approve ISSUE-0002
/laplace:run ISSUE-0002
```

루프가 멈추었던 지점에서 재개합니다. 거부한다면 `/laplace:cancel ISSUE-0002`로 결정을 기록하고, 나중을 위해 상태를 보존합니다.

---

## 유스케이스 4 — 취소와 재개

시나리오는 이렇습니다. 실행이 너무 오래 걸리거나, 루프 도중에 범위 문제를 발견했습니다. 안전하게 멈춥니다.

```
/laplace:cancel ISSUE-0003
```

cancel이 하는 일은 다음과 같습니다.

- 활성 루프 상태를 정리하고 이슈 잠금을 해제합니다.
- 이슈 실행 히스토리에 취소를 기록합니다.
- 브랜치나 산출물은 **삭제하지 않습니다.**

상태는 보존됩니다. 재개하려면 이렇게 합니다.

```
/laplace:run ISSUE-0003
```

runner는 기존 브랜치 `laplace/ISSUE-0003`을 감지하고 재사용합니다 (멱등). 루프가 마지막 합법 상태에서 재개됩니다.

---

## 유스케이스 5 — 막힌 이슈

시나리오는 이렇습니다. PM 단계가 범위를 해결하지 못합니다. PRD가 스스로 모순이기 때문입니다.

루프는 이슈를 `blocked`로 전환하고 실행을 끝냅니다. 실행 로그가 차단 이유를 포착합니다. 이것을 겉으로 드러냅니다.

```
/laplace:status
```

```
ISSUE-0004  state: blocked
blocker: acceptance criteria #2 and #3 are mutually exclusive
```

소스 문서나 이슈 메타데이터를 해결한 뒤, 다시 실행합니다.

```
/laplace:run ISSUE-0004
```

---

## 유스케이스 6 — 큐 실행: 여러 개의 승인된 이슈

시나리오는 이렇습니다. 세 이슈가 승인되었고, 각각을 일일이 돌보지 않고 차례로 돌리고 싶습니다.

```
/laplace:approve ISSUE-0005
/laplace:approve ISSUE-0006
/laplace:approve ISSUE-0007
/laplace:run-queue
```

큐 러너는 승인 큐의 맨 앞 이슈를 잡아 `/laplace:run`으로 전체 루프를 돌리고, `review-passed`가 되면 다음 승인 이슈로 넘어갑니다. 기본 `wait-for-human-merge` 정책에서는 첫 번째 병합 게이트에서 멈춥니다.

```
Queue halted: merge-wait:ISSUE-0005
queue-run-id: q-7f3a...
queue_steps:
  - ISSUE-0005: review-passed (awaiting human merge)
Next: Merge branch laplace/ISSUE-0005 into base, then re-run /laplace:run-queue
```

브랜치를 base에 병합한 뒤 큐를 재개합니다.

```
/laplace:run-queue
```

러너는 ISSUE-0006, 그리고 ISSUE-0007로 계속 진행하고, 승인 큐가 비면 마침내 `queue-exhausted`를 출력합니다. `blocked:<id>`나 `human-approval-required:<id>`를 만나면 정상적인 예외 흐름으로 해결하고, 남은 큐를 이어가려 `/laplace:run-queue`를 다시 실행합니다.

---

## 유스케이스 7 — 파이프라인: 전체 흐름을 명령 하나로

시나리오는 이렇습니다. 관리자가 "이 PRD를 잡아서 릴리스까지 돌려라"는 길을 원합니다. `/laplace:pipeline`이 intake, verify, approve, run-parallel, release를 단일 체크포인트 파이프라인으로 엮습니다. 어떤 게이트도 건너뛰지 않습니다. 모든 게이트는 사람의 결정을 기다리며 멈춥니다. 다만 게이트들 사이의 키스트로크 안무만 사라집니다.

### 단계 1 — 파이프라인 시작

```
/laplace:pipeline docs/prd-login-rate-limit.md
```

파이프라인은 intake와 verify를 돌린 뒤, verify 리포트와 이슈별 위험 테이블과 함께 **approve-gate**에서 멈춥니다.

```
Pipeline halt: approve-gate:ISSUE-0001=medium,ISSUE-0002=low
  Phase: approve-gate
  Drafts (issue=risk): ISSUE-0001=medium,ISSUE-0002=low
  Next: review the verify report above, then re-run /laplace:pipeline --resume to batch-approve all drafts.
```

### 단계 2 — 게이트를 검토한 뒤 재개

verify 리포트(PASS/WARN)와 이슈별 위험 요약을 읽습니다. 전부 수용한다면 재개합니다. 파이프라인이 한 번에 모든 드래프트를 일괄 승인합니다.

```
/laplace:pipeline --resume
```

그러면 파이프라인은 **parallel** 단계로 넘어가, 승인된 이슈의 첫 번째 웨이브를 디스패치합니다. `parallel:wave-dispatched:waiting`(in-flight 이슈를 종단 상태로 몰아넣는 단계)이나 `parallel:merge-wait:<id>`(이슈별 병합 게이트)에서 다시 멈춥니다.

### 단계 3 — 릴리스 게이트

parallel 단계가 `queue-exhausted`에 도달한 뒤, 파이프라인은 **release-gate**에서 멈춥니다.

```
Pipeline halt: release-gate
  Phase: release-gate
  Next: /laplace:release <X.Y.Z>  (or re-run /laplace:pipeline --release <X.Y.Z> --resume)
```

`/laplace:release 0.5.0`을 따로 부르거나, `--release 0.5.0`을 넘기고 재개해서 파이프라인이 release를 부르게 할 수 있습니다 (release의 8-점검 게이트는 그대로 발화합니다).

### 플래그

- `--auto-approve-low-risk` — approve-gate에서 Risk Level이 `low`인 드래프트를 자동 승인합니다. medium 이상의 드래프트가 있으면 멈춥니다. 기본은 OFF입니다 (승인 게이트는 항상 멈춥니다).
- `--release <X.Y.Z>` — release-gate에서(queue-exhausted 이후, 멈춘 이슈가 없을 때) 멈추는 대신 `/laplace:release`를 부릅니다.
- `--max-parallel N` — `.harness/config.yml`의 `limits.max_parallel`을 덮어씁니다.
- `--force-verify` — verify FAIL을 넘어가는 탈출구입니다.
- `--resume` — 기록된 단계에서 명시적으로 재개합니다 (같은 PRD 경로로 다시 부르는 것도 암묵적으로 재개로 처리됩니다).

### 파이프라인 취소

```
/laplace:cancel
```

활성 parallel 실행이 있으면 첫 번째 cancel이 그것을 정리하고, 두 번째 cancel이 파이프라인 로그를 `cancelled`로 확정합니다. cancel은 개별 이슈를 건드리지 않습니다. 감사를 위해 상태를 보존합니다.

`/laplace:status`는 활성 파이프라인(단계, prd, 드래프트/승인/in-flight 개수)을 알려주어, 지금 어디에 있는지 항상 알 수 있게 합니다.

---

## 명령 참조 (빠른 조회)

| 명령 | 언제 |
|---|---|
| `/laplace:doctor` | 설치 후, 업그레이드 후, 무언가 이상하게 동작할 때 |
| `/laplace:init` | 프로젝트당 한 번 |
| `/laplace:intake <prd>` | PRD/스토리를 변환할 준비가 되었을 때 |
| `/laplace:verify [prd]` | intake 직후, approve 이전 — TBD 필드, 커버리지 갭, 깨진 참조를 잡아낼 때 |
| `/laplace:approve <이슈>` | 드래프트를 검토했고 큐에 넣고 싶을 때 |
| `/laplace:discard <이슈>` | 드래프트가 실수로 만들어졌고 없어야 할 때 (드래프트 전용) |
| `/laplace:run [이슈]` | 루프를 실행하거나 재개할 때 |
| `/laplace:run-queue [이슈]` | 여러 이슈가 승인되었고 순서대로 돌리고 싶을 때 |
| `/laplace:pipeline <prd>` | PRD를 명령 하나로 엔드투엔드 구동할 때 — 매 게이트에서 멈추고, 재호출하면 재개 |
| `/laplace:status` | 큐, 활성 실행, 차단을 확인할 때 |
| `/laplace:report <이슈>` | 정제된 증거와 평결을 검토할 때 |
| `/laplace:cancel [이슈]` | 루프를 안전하게 멈출 때 (상태는 보존) |
| `/laplace:create-pr <이슈>` | 이슈가 `review-passed`이고 PR을 원할 때 |
| `/laplace:release <X.Y.Z>` | main이 초록이고 테스트가 통과하며 릴리스를 자르고 싶을 때 |

---

## 팁

- **작게 시작하세요.** 진짜 작업에 들어가기 전에 사소한 이슈 하나(문서 오타 수정)를 엔드투엔드로 돌려 흐름을 익히세요.
- **루프는 멈추도록 설계되어 있습니다.** 무인으로 끝까지 돌 거라 기대하지 마세요. 모든 위험 카테고리는 사람을 기다리며 멈춥니다.
- **`.harness/`는 빌드 상태입니다.** 삭제해도 (히스토리를 잃을 뿐) 안전하고, gitignore해도 안전합니다.
- **리포트는 정제되어 있습니다.** 비밀은 영속되기 전에 마스킹되므로, 붙여 넣어도 안전합니다.
- **승인은 감사 가능합니다.** 매 `approve`는 타임스탬프와 함께 `.harness/state/approvals.jsonl`에 추가됩니다.
- **정책은 약화할 수 없습니다.** 루프가 무언가를 거부한다면(강제 푸시, 비밀 읽기, curl-pipe-sh) 그것은 버그가 아니라 단단한 안전 바닥입니다.

### 유스케이스 — 승인 전에 verify

intake는 기계적인 과정이라 TBD 필드, 잘못 파싱된 소제목, PRD 커버리지 갭을 만들 수 있습니다. intake 직후, approve 이전에 `/laplace:verify docs/prd-X.md`를 돌리면 읽기 전용 패스 하나로 그런 것들을 드러냅니다.

- 이슈별 PASS/WARN/FAIL 테이블 (TBD 필드, 깨진 `Source.Section`, AC 추적성 갭).
- PRD 커버리지 매트릭스 — 모든 `## Task:` 섹션이 이슈에 매핑되거나, `ORPHAN`으로 플래그됩니다.
- 이슈 간 검사 — 깨진 `depends_on` 참조와 중복 AC (80% 초과 겹침이면 경고).

verify는 상태를 전환하지 않고, `/laplace:approve`를 막지도 않습니다. 자문용입니다. 범위와 위험을 판단하는 승인 게이트는 여전히 사람이 쥡니다. 종료 코드는 `0`(깨끗하거나 경고만) / `1`(실패 있음) / `2`(사용법 오류)입니다.

### 유스케이스 — 버전 릴리스

Laplace 버전 릴리스는 5단계 의식(3개 파일 범프, 커밋, 태그, main 푸시, 태그 푸시)으로 이루어지며, `/laplace:release`가 8-점검 게이트 뒤에서 이를 자동화합니다. 릴리스는 두 반쪽으로 나뉩니다. 로컬 반쪽(`/laplace:release`)과 원격 반쪽(태그를 푸시했을 때 도는 CI 릴리스 워크플로)입니다.

**로컬 반쪽 — `/laplace:release <X.Y.Z>`**

```
/laplace:release 0.3.1
```

8개 점검을 순서대로 돌립니다 (브랜치 = main, 형태 = `X.Y.Z`, 테스트 통과, 범프 후 세 파일 동기화, semver가 업그레이드, 트리 깨끗, 태그 부재, 원격이 ahead가 아님, 대기 중인 승인 이슈 없음). 어느 하나라도 실패하면 해결 메시지를 남기고 멈추고, 부작용 없이 종료 코드 1로 빠집니다. 전부 통과하면 `VERSION`과 `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`을 범프하고, `chore(release): bump <old> -> <new>`를 커밋하고, `v<X.Y.Z>`로 태그를 붙이고, main를 푸시하고, 태그를 푸시합니다.

`/laplace:release`를 부르는 행위 자체가 푸시에 대한 인가입니다 (옵션 A, `/laplace:create-pr`과 같은 방식). 푸시는 되돌릴 수 없고, 8-점검 게이트가 가드레일입니다. 매 시도는 `.harness/state/releases.jsonl`에 추가됩니다 (성공: `{checks_passed: true, sequence_ok: true, pushed_at, commit, tag, authorization_basis: "release-invocation"}`; 정지: `{checks_passed: false, failed_check, reason}`).

`--force`는 오직 다운그레이드(점검 4)와 대기 중인 승인(점검 8) 점검만 느슨하게 합니다. 형태, 테스트, 동기화, 트리-깨끗, 태그-부재, 원격 점검은 절대 건너뛰지 않습니다.

**부분 푸시 복구 (R-2).** main 푸시는 성공했는데 태그 푸시가 실패하면(네트워크 순간 오류), `/laplace:release`는 `PARTIAL RELEASE: main pushed, tag push failed`로 멈춥니다. main을 롤백하지는 않습니다 (커밋이 이미 공개되었으므로). 수동으로 복구하세요. `git push origin v<X.Y.Z>`.

**원격 반쪽 — CI 릴리스 워크플로**

기존 `.github/workflows/release.yml`(변경 없음)이 태그 푸시에서 발화하여, 세 방향 버전 일치를 검증하고, 커밋에서 생성한 노트와 함께 GitHub Release를 만듭니다. `/laplace:release`가 로컬 반쪽이고, CI가 원격 반쪽입니다.

---

## 트러블슈팅

| 증상 | 원인 | 해결 |
|---|---|---|
| `/laplace:*`를 찾을 수 없음 | 플러그인이 설치되지 않았거나 마켓플레이스 캐시가 오래됨 | `/plugin marketplace remove tipsy-kereru/laplace` 후 다시 추가하고 재설치 |
| `doctor`가 `state selftest fail`을 보고 | Python 또는 표준 라이브러리 문제 | `python3 --version` (3.7 이상 필요) |
| `run`이 "not a git repo"라고 함 | 작업 디렉토리가 git 저장소가 아님 | 프로젝트에서 `git init`을 하거나, 다른 곳에서 실행 |
| `create-pr`이 `gh` 미인증이라 함 | `gh`가 없거나 로그아웃됨 | `! gh auth login` |
| 루프가 계속 `human-approval-required`에서 멈춤 | 의도된 동작 — 그 카테고리는 사람이 필요 | `/laplace:approve <이슈>` 후 `/laplace:run <이슈>` |

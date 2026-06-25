# Laplace — 사용 가이드

**언어:** [English](USAGE.md) | 한국어

현실적인 예시로 구성된 엔드투엔드(End-to-End) 사용 안내서입니다. 플러그인의 최초 설치 요령, 지향하는 철학, 그리고 상세 아키텍처에 대해서는 먼저 [README.kr.md](file:///home/kereru/Development/laplace/laplace/README.kr.md) 파일을 정독해 주세요.

Laplace는 **당신이 실제 개발 작업을 진행하려는 대상 프로젝트 내부**에서 구동되는 플러그인입니다. `laplace/` 플러그인 소스 저장소 자체 내에서 작동시키는 것이 아닙니다.

---

## 사전 필수 조건

- Claude Code 상에 Laplace 플러그인이 설치되어 있어야 합니다. (`/plugin install laplace@laplace` 실행)
- 현재 작업 디렉토리(CWD)가 대상 프로젝트의 루트 경로여야 합니다.
- 시스템 환경 변수 `PATH` 상에 `python3` 및 `git` 명령어가 등록되어 있어야 합니다.
- `gh` CLI 가 사전에 로그인 인증되어 있어야 합니다. (GitHub Pull Request 생성을 유도하는 `/laplace:create-pr` 명령어에만 필수)

---

## 유스케이스 1 — 최초 설정 및 상태 진단 (Health Check)

목표는 런타임 작업 공간을 로컬에 초기화하고, 설치된 플러그인이 정상 작동할 수 있는 무결성 상태인지 상태 진단(Health Check)을 수행하는 것입니다.

```bash
/laplace:doctor
```

출력 예시 (요약):

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

워크스페이스 초기화 명령을 내리기 전에는 `.harness/` 디렉토리가 없다는 경고와 Moon Cell 프로필이 생략되었다는 두 가지 경고가 출력되는 것이 정상입니다. 다음 명령으로 프로젝트 작업 공간을 초기화해 주세요.

```bash
/laplace:init
```

이 명령을 수행하면 프로젝트 구성 설정 파일, 이벤트 단계 라우팅 정책 파일, 그리고 상태 관리 폴더 트리 등이 포함된 `.harness/` 디렉토리가 생성됩니다. 다시 한번 `doctor` 명령을 구동하면 구성 파일 부재 경고가 사라집니다. (Moon Cell의 경우는 선택 사양이므로 기본 환경에서는 경고가 유지되더라도 정상입니다.)

런타임 시에 누적되는 상태 파일들이 git 커밋 이력에 노출되기를 원치 않으신다면 프로젝트의 `.gitignore` 파일에 `.harness/` 디렉토리 경로를 추가해 주세요.

```
.harness/
```

---

## 유스케이스 2 — 버그 수정: 차단 없는 전체 루프

시나리오는 다음과 같습니다. 로그인 실패 빈도 제한(Rate Limiting) 버그가 리포트되어 이를 설명하는 PRD 문서가 작성되었습니다. 요구사항 수집부터 최종 PR 제출까지의 전체 주기를 매끄럽게 처리해 봅니다.

### 단계 1 — 요구사항 정의(PRD) 작성

`docs/prd-login-rate-limit.md` 파일에 요구조건을 정의합니다:

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

### 단계 2 — 요구사항 분석 (Intake): PRD를 드래프트 이슈로 변환

```bash
/laplace:intake docs/prd-login-rate-limit.md
```

Laplace가 해당 마크다운 PRD를 분석하여 `.harness/issues/` 하위에 `draft` 상태를 부여한 하나 이상의 `ISSUE-NNNN` 이슈 파일을 생성합니다. 만약 PRD 내용 중 애매모호한 스펙이 발견되면, 모델이 분석 분석 단계(Intake) 중에 구체적인 범위를 대화식으로 질문할 것입니다.

### 단계 3 — 명세 검토 및 승인

```bash
/laplace:status
```

`ISSUE-0001`이 드래프트 큐에 정상적으로 대기하고 있는지 파악한 후, 그 내용을 확인합니다.

```bash
/laplace:report ISSUE-0001
```

분석 완료된 개발 범위, 인수 기준, 그리고 위험 요인 분류가 사용자가 기획한 개발 목적과 맞는지 확인합니다. 이 단계가 바로 **수동 승인 게이트 (Human Gate)**입니다. Laplace는 사용자의 명시적인 승인 명령 없이 임의로 다음 단계로 진행하지 않습니다.

```bash
/laplace:approve ISSUE-0001
```

승인 내역을 `.harness/state/approvals.jsonl` 로그 파일에 기록하고, 드래프트 상태였던 이슈를 정식 개발 대기 큐(Approved Queue)로 넘깁니다.

### 단계 4 — 개발 루프 실행

```bash
/laplace:run ISSUE-0001
```

이 명령을 수행하면 하네스는 다음 절차를 즉각 지휘합니다:

1. 해당 이슈에 대한 전용 쓰기 락(Lock)을 잡고, 전용 개발 브랜치인 `laplace/ISSUE-0001`을 생성합니다.
2. **PM 단계** — 세부 구현 범위, 인수 기준, 기술 주의사항을 보완하여 명확하게 다듬습니다. 분석 결과에 따라 이슈는 `ready` 또는 `blocked` 상태로 전이됩니다.
3. **Dev 단계** — 독립된 개발 브랜치상에서 주어진 요구사항에 부합하는 소스 코드를 구현하고 테스트 코드를 기동하여 통과 여부를 검증 증거(Evidence)로 수집합니다.
4. **Review 단계** — 사전에 수립된 인수 기준을 교차 검토하여 완벽히 합치하는지 엄밀히 코드 리뷰합니다.
5. **Security 단계** — 보안 관점의 검토를 실시합니다. 해당 이슈는 인증(auth) 모듈과 인접한 작업을 수행하므로 보안 에이전트의 정밀 보안 감사가 기동됩니다.

각 단계의 검증 통과 이력은 `.harness/state/runs/<run-id>.json` 파일에 증거 데이터로 차례로 저장됩니다. 루프는 최종 검증 통과 단계인 `review-passed`에 도달하여 안전하게 멈추고 대기합니다.

### 단계 5 — 상태 모니터링 및 리포트 검토

```bash
/laplace:status
/laplace:report ISSUE-0001
```

리포트 명령은 에이전트가 통과시킨 테스트 출력 결과, 코드 리뷰 사유, 보안 검토 결과 등을 가독성 높은 표 형식으로 정리해 렌더링합니다. 혹시 검증 과정에서 노출된 민감 비밀 정보(secrets)가 있더라도 파일 저장에 앞서 [scripts/redaction.py](file:///home/kereru/Development/laplace/laplace/scripts/redaction.py) 스크립트에 의해 안전하게 마스킹 처리되므로, 해당 리포트는 안심하고 팀원에게 공유하셔도 좋습니다.

### 단계 6 — Pull Request 생성

```bash
/laplace:create-pr ISSUE-0001
```

PR을 위한 최종 마크다운 요약문을 사용자에게 제시하고, 최종 승인 로그를 기록한 다음, **명시적으로 사용자가 최종 확인을 승인한 경우에만** GitHub 원격 저장소에 PR을 생성합니다. 사용자의 확인 없이 에이전트가 단독으로 원격 저장소에 PR을 게시하는 일은 결코 일어나지 않습니다.

---

## 유스케이스 3 — 의존성 게이트에 차단되는 신규 기능 추가

시나리오는 다음과 같습니다. 개발 진행 중 외부 npm 패키지를 설치해야 하는 상황을 만납니다. 외부 의존성 패키지의 추가 시도는 **사용자의 직접적인 동의가 엄격하게 강제되는 보안 게이트**이므로 개발 루프가 일시 정지됩니다.

### 보안 게이트 도달에 따른 루프 중단

개발(Dev) 진행 과정 중 에이전트 모델이 새 패키지 설치의 필요성을 감지합니다. 하네스 엔진은 에이전트가 임의로 패키지를 설치하는 것을 원천 차단하고 루프를 즉시 멈춘 뒤 `human-approval-required` 상태로 돌려놓습니다. 상태를 확인해 봅니다.

```bash
/laplace:status
```

```
ISSUE-0002  state: human-approval-required
reason: dependency-add  (mongoose@8.0.0)
```

### 사용자의 의사결정 및 승인

사용자가 제안된 패키지의 라이선스, 배포처 정보, 취약점(CVE) 이력 등을 검토합니다. 해당 패키지 추가를 수용하고자 결정했다면 다음 명령을 순서대로 실행해 주세요.

```bash
/laplace:approve ISSUE-0002
/laplace:run ISSUE-0002
```

루프가 이전 정지 지점부터 안전하게 이어서 실행됩니다. 만약 해당 라이브러리 사용을 거절하기로 판단했다면, `/laplace:cancel ISSUE-0002` 명령을 내려 결정을 확정하고 추후 복구를 위해 상태 데이터를 보관해 둡니다.

---

## 유스케이스 4 — 개발 루프 취소 및 재개

시나리오는 다음과 같습니다. 에이전트 실행 시간이 과도하게 지연되거나, 작업 진행 중 중대한 사양 설계의 변경이 포착되었습니다. 실행 중인 개발 프로세스를 안전하게 종료하고자 합니다.

```bash
/laplace:cancel ISSUE-0003
```

cancel 명령을 트리거하면 다음 작업들이 안전하게 처리됩니다:

- 활성화 상태로 연동 중이던 루프 동작을 소멸시키고, 획득하고 있던 이슈의 독점 쓰기 락(Lock)을 해제합니다.
- 해당 이슈의 처리 이력 로그 상에 명시적으로 사용자에 의해 취소(Cancel)되었음을 기록합니다.
- 기존에 진행 중이던 로컬 소스 코드 변경 분이나 개발 브랜치는 **절대 임의로 삭제하지 않고 보존합니다.**

작업 공간 상태가 그대로 보존되므로, 차후에 준비를 마친 후 다시 기동하고자 할 때는 다음 명령으로 재개할 수 있습니다.

```bash
/laplace:run ISSUE-0003
```

루프 매커니즘은 기존에 생성되었던 `laplace/ISSUE-0003` 브랜치를 지우지 않고 그대로 재사용(Idempotent)하며, 이전 실행 상태에 기반하여 멈춘 지점부터 안전하게 복구되어 구동됩니다.

---

## 유스케이스 5 — 진행 불가 이슈 (Blocked Issues)

시나리오는 다음과 같습니다. 요구사항 스펙 명확화(PM) 단계 진행 중, 제공된 PRD 문서 내부에서 상호 모순되는 조건이 발견되어 설계를 완료할 수 없습니다.

하네스는 이슈를 `blocked` 상태로 즉각 격리하고 루프 기동을 정상적으로 마무리합니다. 실행 결과 로그에 구체적인 진행 불가 사유가 포착 및 기록되어 사용자에게 제시됩니다.

```bash
/laplace:status
```

```
ISSUE-0004  state: blocked
blocker: acceptance criteria #2 and #3 are mutually exclusive
```

사용자가 원본 요구사항 명세 마크다운 문서의 오류를 바로잡아 모순점을 해제한 뒤, 루프를 다시 구동해 줍니다.

```bash
/laplace:run ISSUE-0004
```

---

## 유스케이스 6 — 일괄 큐(Queue) 실행: 여러 승인 이슈 동시 처리

시나리오는 다음과 같습니다. 세 개의 요구사항 이슈에 대한 수동 승인을 모두 완료해 두었고, 개별 이슈마다 번거롭게 구동 명령을 따로 내릴 필요 없이 차례대로 자동 실행되도록 일괄 처리하고자 합니다.

```bash
/laplace:approve ISSUE-0005
/laplace:approve ISSUE-0006
/laplace:approve ISSUE-0007
/laplace:run-queue
```

큐 러너는 승인 대기 큐의 맨 앞에서 대기 중인 이슈를 추출하여 `/laplace:run`으로 전체 루프를 실행하며, 한 이슈가 정상적으로 검증을 마치고 `review-passed`에 도달하면 다음 대기 중인 이슈로 안전하게 넘어가 기동을 시작합니다. 단, 기본 정책인 '수동 병합 대기 (wait-for-human-merge)' 규칙에 따라, 첫 번째 이슈에 대한 브랜치 병합 게이트를 만나면 루프는 진행을 멈추고 대기합니다.

```
Queue halted: merge-wait:ISSUE-0005
queue-run-id: q-7f3a...
queue_steps:
  - ISSUE-0005: review-passed (awaiting human merge)
Next: Merge branch laplace/ISSUE-0005 into base, then re-run /laplace:run-queue
```

해당 개발 브랜치를 main 브랜치 등으로 병합을 완료한 후, 다시 일괄 큐 구동 명령을 내리면 진행이 재개됩니다.

```bash
/laplace:run-queue
```

큐 러너는 `ISSUE-0006`과 `ISSUE-0007` 이슈를 연이어 지휘하고 모든 백로그 처리를 마치면 최종적으로 `queue-exhausted`를 선언하고 대기 모드로 종료됩니다. 중간에 차단 상태(`blocked:<id>`)나 사용자 승인이 필요한 경계 조건(`human-approval-required:<id>`)을 만나면 일반적인 규칙에 의거하여 즉시 일시 정지 상태로 전환되며, 사용자가 해당 원인을 처리해 준 뒤 다시 `/laplace:run-queue`를 트리거하면 남은 백로그들을 연달아 이행합니다.

---

## 유스케이스 7 — 파이프라인 일괄 실행: 명령 하나로 전 과정 조율

시나리오는 다음과 같습니다. 관리자가 "이 PRD 명세를 접수하여 배포 검증 완료까지 전 과정을 일관된 흐름으로 일괄 지휘하고 싶다"고 요청했습니다. 

`/laplace:pipeline` 명령어는 요구사항 분석(intake)부터 검사(verify), 승인(approve), 병렬 개발 진행(run-parallel), 릴리스 관리(release)에 이르는 분절된 단계를 단일한 체크포인트 상태 관리 파이프라인([scripts/pipeline.py](file:///home/kereru/Development/laplace/laplace/scripts/pipeline.py))으로 연동해 엮습니다. 

이 경우에도 보안상 그 어떤 안전 승인 게이트도 무시되거나 건너뛰지 않습니다. 각 게이트 도달 시마다 사용자의 의사결정을 안전하게 기다립니다. 다만 사용자가 매 단계마다 다른 명령어를 알아보고 타이핑해야 하는 조작 피로(안무)를 최소화해 줍니다.

### 단계 1 — 파이프라인 일괄 처리 시작

```bash
/laplace:pipeline docs/prd-login-rate-limit.md
```

파이프라인이 즉시 기동하여 intake 및 verify 검증을 우선 돌린 후, 검사 리포트 요약과 위험 분류 테이블을 사용자에게 리포트하며 **approve-gate** 단계에서 안전하게 일시 정지합니다.

```
Pipeline halt: approve-gate:ISSUE-0001=medium,ISSUE-0002=low
  Phase: approve-gate
  Drafts (issue=risk): ISSUE-0001=medium,ISSUE-0002=low
  Next: review the verify report above, then re-run /laplace:pipeline --resume to batch-approve all drafts.
```

### 단계 2 — 진단 결과 검토 및 파이프라인 재개

상단에 출력된 정밀 검사 리포트 요약(PASS/WARN 등)과 이슈별 위험도를 판독합니다. 기획안대로 구현 진행에 동의한다면 파이프라인을 재개합니다. 하네스 시스템이 전체 드래프트를 일괄 승인으로 전환합니다.

```bash
/laplace:pipeline --resume
```

파이프라인이 **parallel(병렬 개발)** 단계로 즉시 전이되어 승인 완료된 이슈들의 첫 번째 웨이브 처리를 지휘합니다. 병렬 처리 분배 대기 상태(`parallel:wave-dispatched:waiting`)나 각 브랜치 개별 병합 대기 게이트(`parallel:merge-wait:<id>`)를 만나면 사용자의 검증을 위해 파이프라인이 다시 안전하게 정지하여 대기합니다.

### 단계 3 — 최종 릴리스 검토 게이트

전체 병렬 큐 작업이 완수되어 `queue-exhausted`에 도달하면, 파이프라인은 최종적으로 **release-gate(릴리스 게이트)**를 열어두고 대기합니다.

```
Pipeline halt: release-gate
  Phase: release-gate
  Next: /laplace:release <X.Y.Z>  (or re-run /laplace:pipeline --release <X.Y.Z> --resume)
```

터미널에서 수동으로 `/laplace:release 0.5.0` 명령을 실행해 배포하거나, `--release 0.5.0` 파라미터를 넘기고 재개 명령을 전달하여 파이프라인 프로세스가 릴리스 스크립트를 호출하게 지시할 수 있습니다. (이때도 릴리스 모듈의 8중 무결성 검증은 완벽히 기동됩니다.)

### 파이프라인 보조 옵션 플래그

- `--auto-approve-low-risk` — 승인 검토 게이트에서 위험 수준(Risk Level)이 `low`로 판정된 드래프트 이슈를 사람의 확인 없이 자동 승인합니다. `medium` 이상의 드래프트가 발견되면 즉시 안전을 위해 멈춥니다. 기본값은 OFF 상태입니다.
- `--release <X.Y.Z>` — 최종 릴리스 게이트에 도달했을 때(모든 큐 작업이 완수되고 진행 차단이 없는 깨끗한 상태일 때) 일시 정지하지 않고 `/laplace:release` 절차를 바로 트리거합니다.
- `--max-parallel N` — 로컬 프로젝트 설정 정보([.harness/config.yml](file:///home/kereru/Development/laplace/laplace/.harness/config.yml)) 내 정의된 최대 병렬 개수 한계값(`limits.max_parallel`) 설정을 덮어씁니다.
- `--force-verify` — verify 분석 결과 FAIL 등급 판정이 나더라도 프로세스를 계속 진행하도록 명시적으로 우회(Bypass)합니다.
- `--resume` — 가장 최근에 정지되어 기록된 체크포인트 시점부터 명시적으로 파이프라인을 재개합니다.

### 기동 중인 파이프라인 강제 취소

```bash
/laplace:cancel
```

병렬 처리가 실행 중인 상태에서 첫 번째 cancel 명령을 내리면 기동 중인 병렬 프로세스를 안전하게 정상 정리하여 멈추고, 연달아 두 번째 cancel 명령을 내리면 파이프라인 데이터베이스 상에 진행 결과를 취소(`cancelled`)로 최종 기록 확정합니다. 이 취소 명령은 개별 이슈 고유의 상태는 손대지 않으며 추후 사후 감사를 위해 런타임 데이터를 고스란히 저장해 둡니다.

언제든 `/laplace:status` 명령을 통해 현재 진행 중인 파이프라인 단계, 접수된 PRD 경로, 드래프트/승인/기동 중인 이슈 개수 등의 대시보드를 즉각 모니터링할 수 있습니다.

---

## 명령어 요약 가이드 (Quick Reference)

| 실행 명령어 | 사용 타이밍 및 용도 |
|---|---|
| `/laplace:doctor` | 플러그인을 최초 설치한 직후, 새 버전 업그레이드 직후, 혹은 시스템이 의도대로 반응하지 않아 자가 진단이 필요할 때 |
| `/laplace:init` | 개별 프로젝트당 최초 1회, 런타임 작업 공간 폴더를 구성하고자 할 때 |
| `/laplace:intake <prd>` | 준비된 PRD 요구사항 마크다운 문서를 이슈 객체로 분석 및 로드하고자 할 때 |
| `/laplace:verify [prd]` | 수동 승인 이전에, 드래프트 문서 내용 중 정의되지 않은(TBD) 필드, 누락된 사양, 깨진 참조 링크 등이 존재하는지 읽기 전용으로 검사하고자 할 때 |
| `/laplace:approve <이슈>` | 이슈 상세 분석 결과를 검수하였고, 정식으로 개발에 착수하도록 대기 큐로 옮기고자 할 때 |
| `/laplace:discard <이슈>` | 임의로 생성된 이슈나 불필요한 드래프트 이슈를 흔적 없이 즉시 영구 삭제하고자 할 때 (드래프트 상태일 때만 유효) |
| `/laplace:run [이슈]` | 대상 이슈에 대한 PM/개발/리뷰/보안 프로세스를 순차 기동하거나, 대기 중이던 단계를 재개하고자 할 때 |
| `/laplace:run-queue [이슈]` | 백로그에 누적된 여러 승인 이슈들을 차례대로 자동 순차 구동시키고자 할 때 |
| `/laplace:pipeline <prd>` | 요구사항 명세 접수부터 릴리스 RC 등록까지 단 하나의 명령어로 연동 지휘하고자 할 때 (매 게이트 자동 정지 및 재개 지원) |
| `/laplace:status` | 누적된 이슈 큐 현황, 기동 중인 에이전트 프로세스, 차단 요인 등을 한눈에 확인하고자 할 때 |
| `/laplace:report <이슈>` | 엄밀히 수행된 테스트 결과, 감사 평결서 등 정리된 증거 문서를 확인하고자 할 때 |
| `/laplace:cancel [이슈]` | 구동 중인 개발 루프를 상태 데이터 유실 없이 안전하게 조기 종료하고자 할 때 |
| `/laplace:create-pr <이슈>` | 모든 검증을 마치고 `review-passed`가 선언된 안전한 코드를 GitHub 원격 저장소에 PR로 제출하고자 할 때 |
| `/laplace:release <X.Y.Z>` | 로컬 main 브랜치 빌드가 정합하고 전체 테스트가 완전히 통과되어 제품 버전을 릴리스 배포하고자 할 때 |

---

## 실무 팁 및 주의 사항

- **작은 태스크부터 구동해 보세요.** 실제 거대한 상용 기능 개발에 투입하기 전에, 문서의 오타 수정이나 간단한 모듈 리팩토링 등의 가벼운 이슈를 엔드투엔드로 직접 처리해 보며 전체 루프의 작동 흐름에 익숙해지는 것을 권장합니다.
- **Laplace 개발 루프는 자주 멈추도록 설계되었습니다.** 사람의 확인 없이 무인 상태로 배포까지 한 번에 자동 진행될 것이라 기대해서는 안 됩니다. 시스템은 위험 경계선을 감지하면 안전을 위해 무조건 사용자의 직접적인 승인을 기다리며 구동을 일시 중지합니다.
- **`.harness/` 디렉토리는 빌드 부산물 성격을 띱니다.** 작업 도중 폴더를 과감히 삭제하더라도(이전 감사 로그 이력 정보가 유실될 뿐) 프로젝트가 망가지지 않으며, 안심하고 git 관리 항목에서 제외해 주셔도 됩니다.
- **리포트 로그는 깨끗하게 비식별화됩니다.** API 비밀 토큰, 인증 패스워드 등 민감한 자격 정보는 파일 저장 이전에 철저하게 감지되어 제거(Masking)되므로 보고서 본문을 그대로 팀 슬랙 등에 붙여 넣어 인용하셔도 안전합니다.
- **안전 제어 정책은 인위적인 우회가 불가능합니다.** 하네스 루프가 특정 행위(강제 push 실행, 환경 변수 비밀키 리드 시도, 원격지 curl 셸 파이프 실행 등)를 거절하는 오류를 반환한다면, 이는 버그가 아닌 시스템이 보증하는 완강한 **보안 가이드라인(Hard Safety Floor)**이 정상 작동하는 중임을 의미합니다.

### 승인 전 사전 검증 (Verify) 유스케이스

Intake는 마크다운 구조를 파싱해 내는 기계적인 변환 과정이므로, 원본 요구사항(PRD) 내에 TBD(미확정) 필드가 잔존하거나 소제목 오타 등으로 참조 관계가 단절되는 빈틈이 생길 수 있습니다. intake 완료 직후 및 approve 명령 제출 이전에 `/laplace:verify docs/prd-X.md`를 기동하면, 읽기 전용으로 요구사항 사양을 교차 감사하여 다음 문제를 사전에 밝혀 줍니다.

- 각 이슈 단위별 PASS/WARN/FAIL 진단표 제시 (TBD 속성 잔존 여부, AC 추적 단절 지점, 깨진 `Source.Section` 참조 경로 지적)
- PRD 요구사항 명세 커버리지 다이어그램 출력 — 마크다운 내 정의된 모든 `## Task:` 구역이 유효한 이슈 식별자와 일치하는지 추적하고, 매칭되지 않은 고아 명세(`ORPHAN`)를 검출
- 이슈 간 상호 의존 관계 분석 — 논리적으로 맞지 않는 `depends_on` 의존 관계 정의나 이슈 간 요구사항(AC)의 과도한 중복 기술(80% 이상 유사할 경우 경고)을 진단

verify 검사는 오직 진단을 위한 읽기 전용 명령이므로 이슈 상태를 임의로 전환하지 않으며, `/laplace:approve` 실행 권한을 강제로 차단하지도 않습니다. 오직 사람이 승인 게이트를 통과시키기 전 위험 수준을 사전에 가늠할 수 있도록 돕는 유용한 분석 진단 도구입니다.

### 버전 릴리스 (Release) 관리 유스케이스

Laplace 제품 릴리스는 엄격하게 약속된 5대 단계(3대 핵심 파일 버전 동기화, git 커밋 발행, git 태그 작성, main 브랜치 원격 push, git 태그 원격 push)로 구성되며, `/laplace:release` 명령어가 8중 안전 무결성 검증 가이드를 기반으로 이를 정밀 제어합니다. 배포 단계는 로컬 터미널 동작(`/laplace:release`)과 원격 CI 구동 파이프라인(태그 push 시 연동 기동)의 연동으로 나뉩니다.

**로컬 터미널에서의 동작 — `/laplace:release <X.Y.Z>`**

```bash
/laplace:release 0.3.1
```

다음 8중의 안전 무결성 조건 검사를 순차 수행합니다:
1. 현재 작업 중인 브랜치가 `main`인지 검사
2. 입력한 버전이 유의적 버전(`X.Y.Z`) 규격에 부합하는지 검사
3. 전체 로컬 테스트 빌드가 에러 없이 완벽히 통과하는지 검사
4. 갱신 후 3개 핵심 설정 파일 내 버전 정보가 서로 일치하는지 검사
5. semver 규격상 신규 버전이 이전 버전보다 확실하게 상향되었는지 검사
6. git 작업 디렉토리가 커밋되지 않은 잔여 변경 사항 없이 깨끗한 상태인지 검사
7. 생성하려는 git 태그가 이미 원격지에 존재하고 있는지 검사
8. 로컬 main 브랜치에 반영되지 않은 원격지 변경 분(ahead 상태)이 존재하지 않는지 검사

이 중 단 하나라도 불일치가 포착되는 즉시 구체적인 원인 메시지를 출력하며 정지하고, 프로젝트에 그 어떤 부작용도 유입시키지 않고 에러 종료 코드 `1`을 뱉으며 정상 안전 종료됩니다. 모든 조건을 완수하면 `VERSION`, `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json` 내 버전 넘버를 동일하게 동기화하여 변경하고, `chore(release): bump <old> -> <new>` 커밋을 남긴 뒤 `v<X.Y.Z>` 태그를 설정하고 main 브랜치 및 태그 정보를 원격 저장소로 차례로 푸시합니다.

`/laplace:release`를 호출하는 행위 자체가 원격 push를 최종 권한 인가(Authorize)했음을 의미합니다. push 동작은 한 번 발행하면 되돌릴 수 없는 민감한 작업이므로 앞서 작동하는 8중의 무결성 검증이 유일하고 든든한 방패 역할을 합니다. 모든 릴리스 시도는 `.harness/state/releases.jsonl` 로그 파일에 고스란히 감사 이력으로 저장됩니다.

만약 긴급 롤백 배포 등 예외적인 이유로 강제 실행이 요구될 경우 `--force` 옵션을 활용하여 이전 버전으로의 다운그레이드 경고(검사 4) 및 대기 상태 이슈 잔존 경고(검사 8)를 강제 바이패스할 수 있습니다. 단, 버전 포맷 오류, 로컬 테스트 실패, 설정 파일 버전 불일치, git 디렉토리 오염, 이미 발행된 태그 충돌 검사 등은 보안상 본 옵션을 적용하더라도 결코 우회할 수 없습니다.

**일부 push 실패 예외 복구 시나리오**

main 브랜치 커밋 push에는 완벽히 성공했으나 일시적인 네트워크 끊김 등으로 이어진 git 태그 push 단계에서 실패한 경우, 시스템은 `PARTIAL RELEASE: main pushed, tag push failed` 경고 메시지와 함께 동작을 중단합니다. 이미 공개 저장소에 푸시 완료된 main 브랜치의 커밋을 강제로 되돌리지는 않으므로, 이 경우 사용자가 터미널을 열고 `git push origin v<X.Y.Z>` 명령을 수동으로 입력하여 배포 복구를 완료하셔야 합니다.

**원격지에서의 동작 — CI 릴리스 워크플로**

태그 push가 무사히 도달하면 원격지에 구성된 `.github/workflows/release.yml` CI 가 감지되어 가동을 개시합니다. CI 빌드 에이전트는 3대 설정 파일 정보의 일관성을 다시 한번 최종 교차 검증하고, 최신 git 커밋 이력들을 요약하여 공식 GitHub Release 노트를 배포 발행합니다.

---

## 문제 해결 요령 (Troubleshooting)

| 발생 현상 | 예상 원인 | 대처 요령 |
|---|---|---|
| `/laplace:*` 명령어가 인식되지 않음 | 플러그인 설치가 누락되었거나 플러그인 캐시 정보가 만료됨 | `/plugin marketplace remove tipsy-kereru/laplace` 명령으로 마켓플레이스를 제거한 후 다시 추가하고 재설치 진행 |
| `doctor` 진단 결과 `state selftest fail` 오류 보고 | 로컬 Python 실행 환경의 버전 혹은 표준 라이브러리 간섭 문제 | 터미널에 `python3 --version` 명령을 내려 설치 버전 정보 확인 (Python 3.7 이상 필수 요구) |
| `run` 실행 시 "not a git repo" 경고 출력 | 현재 명령을 내린 작업 디렉토리가 git 저장소 경로가 아님 | 대상 프로젝트 루트 폴더에서 `git init`을 구동하여 저장소를 확보하거나 올바른 경로로 이동하여 재기동 |
| `create-pr` 실행 결과 `gh` 인증 오류 보고 | 시스템에 `gh` CLI 도구가 없거나 인증 정보가 소멸됨 | 터미널에 `gh auth login` (또는 `! gh auth login`) 명령을 내려 GitHub 연동 로그인 인증을 수행 |
| 루프가 계속 `human-approval-required` 상태에서 멈춰 있음 | 정상 작동 상태 — 패키지 추가 등 사람의 수동 판단이 필요한 보안 영역을 감지한 경우 | 검수 후 이상이 없다면 `/laplace:approve <이슈>` 명령으로 상태를 승인해 준 뒤 `/laplace:run <이슈>`를 내려 루프 재개 |

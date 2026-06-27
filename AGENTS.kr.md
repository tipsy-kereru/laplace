# AGENTS.md (Codex)

**언어:** [English](AGENTS.md) | 한국어

> Laplace는 Codex 환경에서도 Claude Code와 **완벽히 동일한 수준의 이벤트 훅(Hook)** 시스템으로 작동합니다.
> Codex는 [hooks/hooks.json](file:///home/kereru/Development/laplace/laplace/hooks/hooks.json)을 파싱하여 로드하고, `CLAUDE_PLUGIN_ROOT` 환경 변수를 설정하며,
> 동일한 라이프사이클 이벤트(`PreToolUse`, `PostToolUse`, `Stop`, `SessionStart`, `UserPromptSubmit`)를 트리거합니다. 강력한 명령어/경로 차단 정책(Deny Layer), 테스트 증거 게이트, 그리고 루프 중단 규칙이 모두 강제 적용됩니다.
> 이 가이드는 이러한 엄격한 제어 프레임워크 아래에서 에이전트 모델이 반드시 준수해야 하는 구체적인 동작 절차를 설명합니다.

---

## Laplace의 역할과 핵심 개념

Laplace는 로컬 환경에서 기동하는 **AI 엔지니어링 루프 하네스(Harness)**입니다. 요구사항 명세(PRD, 스토리 등)를 분석하여 로컬 이슈 파일로 변환하고, 각 이슈를 체계적인 프로세스 단계(PM → Dev → Review → Security)로 분기하며, 매 단계가 끝날 때마다 타당한 실행 증거(Evidence)를 검증하고 저장합니다. 특히 되돌릴 수 없는 민감한 작업을 실행하기 전에는 사람의 명시적인 확인을 요구하는 승인 게이트(Human Approval Gate)에서 반드시 일시 정지하도록 설계되어 있습니다.

Laplace의 핵심 가설은 매우 간결합니다. **"현대 AI 모델의 자체 성능은 충분히 훌륭하다. 다만, 정석적인 개발 프로세스를 지키는 '절차와 규율'이 누락되어 문제를 야기할 뿐이다."** Laplace는 누락되기 쉬운 이 개발 절차를 결정론적인 시스템으로 강제합니다.

---

## 이슈별 개발 루프 단계

```
intake (요구사항 접수) → draft (드래프트 생성) → approved (승인 완료) → pm-review (스펙 명확화) → ready-for-dev (개발 대기) 
                       → in-progress (개발 중) → review (코드 리뷰) → security-review (보안 심사) 
                       → review-passed (검증 통과) → release-candidate (릴리스 후보) → done (완료)
```

- **draft → approved**: 수동 승인 게이트입니다. 오직 `/laplace:approve` (또는 `@laplace:approve`) 명령을 통해서만 이 상태로 전환됩니다.
- **review-passed**: 단계 완료를 선언하려면 실행 로그 내에 테스트 통과 증거(Evidence)가 필수로 포함되어야 합니다.
- **release-candidate → done**: 수동 승인 게이트입니다. 외부 시스템으로의 배포(push, PR 생성, 패키지 publish 등)를 수행하기 전 최종 사용자의 동의가 필요합니다.

이러한 단계 제어 게이트들은 [hooks/pretooluse.py](file:///home/kereru/Development/laplace/laplace/hooks/pretooluse.py) 훅을 통해 내부적으로 통제되며, Codex 환경에서도 완전히 동일하게 작동합니다. 에이전트 모델이 프롬프트를 조작하여 게이트를 임의로 우회하거나 상태를 불법 전이하려 하더라도 하네스 엔진이 이를 철저히 차단합니다.

---

## 절차 준수 수칙 (에이전트 공통 가이드라인)

1. **작업 쪼개기(Decomposition) 전 맥락 파악**: 구현 설계를 제안하기 전에 반드시 `.harness/issues/<ISSUE-NNNN>.md`이슈 정의 파일을 먼저 정독하세요. 그리고 첫 응답에서 작업 범위와 인수 기준(Acceptance Criteria)을 명확하게 다시 정리하여 서술하세요.
2. **개발 실행 전 로컬 이슈 상태 검증**: `.harness/state/tasks.json` 파일에 선언된 상태가 에이전트가 실행 가능한 작업 범위를 정의합니다. 이슈 상태가 `ready-for-dev` 또는 `in-progress`로 승인된 경우에만 소스 코드 개발(Dev)을 시작하세요.
3. **변경 범위 최소화**: 해당 이슈 스펙에 명시적으로 수정하도록 정의된 파일들만 수정해야 합니다. 이슈 범위와 무관한 리팩토링이나 주변 코드 정리는 엄격히 금지됩니다.
4. **완료 선언 전 객관적 증거 확보**: 작업을 마쳤다는 주장은 반드시 `.harness/state/runs/<run-id>.json` 로그 파일 내에 객관적인 테스트 결과, diff 요약, 동작 판정 내역 등이 증거(Evidence)로 기록되어야만 유효합니다. 증거 자료를 기록하지 않은 상태에서 단계를 임의로 '완료' 상태로 선언하지 마세요.
5. **임의 추측 금지 및 즉시 정지**: 스펙이 모호하거나, 해결할 수 없는 장애(Blocker) 상황을 만나거나, 승인이 필요한 민감한 작업을 감지하면 루프 진행을 즉시 정지하고 대기하세요. 임의로 하나의 가정을 선택하여 개발을 계속 진행해서는 안 됩니다.
6. **되돌릴 수 없는 작업 수행 전 확인**: API 자격 증명 수정, 운영(Production) 환경 접근, 의존 패키지 설치, 외부 망 연결, 빌드 배포 단계 등을 만나면 즉시 동작을 정지하고 사람에게 승인을 요청하세요. 도구(Tool)를 사용해 명령을 실행할 권한이 있더라도, 실행 여부는 사람이 판단합니다.

---

## 승인 게이트 제어 (Codex 환경에서 자동 강제됨)

다음 범주의 작업을 감지하면 하네스는 동작을 일시 중지합니다. 즉시 작업을 멈추고 사람에게 승인을 대기 중임을 보고하세요.

- **외부 게시:** `git push`, `gh pr create`, `npm publish` 등 코드를 원격지에 배포하거나 PR을 여는 행위.
- **의존성 및 환경 변화:** `pip install`, `npm install`, `claude mcp add` 등 신규 패키지를 다운로드하거나 에이전트 도구 구성을 변경하는 행위.
- **민감 자격 증명:** `.aws/`, `.ssh/` 등의 디렉토리 접근 및 시스템 환경 변수 내 비밀키 읽기/쓰기 시도.
- **파괴적인 명령어:** `rm -rf` 실행, git 강제 푸시(Force Push), 공용 브랜치 대상의 `git reset --hard` 적용.
- **릴리스 최종 승인:** `release-candidate → done` 단계로의 전환 처리.

위의 승인 제약 요건들은 [scripts/policy.py](file:///home/kereru/Development/laplace/laplace/scripts/policy.py) 스크립트에 의해 [hooks/pretooluse.py](file:///home/kereru/Development/laplace/laplace/hooks/pretooluse.py) 내부에서 강제됩니다. 위험 명령 필터(Deny Layer - `rm -rf /`, `curl|sh`, `sudo` 등)는 감지 즉시 영구 차단하며, 승인 게이트 요소들은 사용자가 승인(또는 freerange 설정으로 일시 완화)할 때까지 루프 작동을 안전하게 일시 정지시킵니다.

---

## 제공 스킬 및 슬래시 명령

Laplace 명령어 세트는 `commands/` 아래에 정의되어 있습니다. Codex 세션에서는 이 스킬들이 `@` 접두사 기호 형태로 사용됩니다.

- `@laplace:intake <prd.md>` — 요구사항 명세(PRD)를 파싱하여 드래프트 이슈로 초기화합니다.
- `@laplace:approve <ISSUE>` — 드래프트 이슈를 검토 완료하고 개발 대기(Approved) 상태로 이동시킵니다. (사람 승인 게이트)
- `@laplace:run <ISSUE>` — 지정 이슈에 대한 개발 프로세스 루프를 순차 기동합니다.
- `@laplace:run-queue` — 승인 백로그에 있는 전체 대기 큐를 차례대로 순차 구동합니다.
- `@laplace:status` — 현재 하네스의 전반적인 작동 및 세션 상태를 보여줍니다.
- `@laplace:freerange <on|off|status>` — 승인 게이트를 일시적으로 완화합니다. (이벤트 훅을 기반으로 작동하므로 Claude Code 및 Codex 양측 환경 모두에서 안전하게 연동됩니다.)

모든 비즈니스 로직은 [scripts/](file:///home/kereru/Development/laplace/laplace/scripts) 디렉토리 하위의 Python 파일들([scripts/state.py](file:///home/kereru/Development/laplace/laplace/scripts/state.py), [scripts/runner.py](file:///home/kereru/Development/laplace/laplace/scripts/runner.py), [scripts/policy.py](file:///home/kereru/Development/laplace/laplace/scripts/policy.py), [scripts/cost_review.py](file:///home/kereru/Development/laplace/laplace/scripts/cost_review.py), [scripts/motivations.py](file:///home/kereru/Development/laplace/laplace/scripts/motivations.py), [scripts/freerange.py](file:///home/kereru/Development/laplace/laplace/scripts/freerange.py))에 정의되어 있습니다. Codex 훅에 의해 자동 호출되며, 필요한 경우 셸 상에서 직접 명령(`python3 scripts/state.py ...`)을 가해 상태를 다룰 수도 있습니다.

---

## 로컬 디스크 상태 폴더 구조 (`.harness/`)

```
.harness/
├── issues/ISSUE-NNNN.md         # 상세 요구사항 및 진행 이슈 파일
├── state/tasks.json             # 이슈 식별자별 현재 상태 매핑 테이블
├── state/runs/<run-id>.json     # 실행 차수별 히스토리: 단계 전환 정보, 테스트 증거 데이터
├── state/approvals.jsonl        # 사용자 승인/거절 감사 기록
├── config.yml                   # 프로젝트 전반의 실행 제어 설정 파일
└── routing-rules.yml            # 이슈 타입별 세부 단계 라우팅 정의 파일
```

도구를 실행하거나 소스 코드를 편집하기 전에 항상 관련 이슈 상태 데이터를 확인하고, 작업을 마쳤을 때는 반드시 검증 증거 로그를 기록하세요.

---

## 예외 대처 및 가이드

시스템적인 훅(Hook) 레이어가 정상 활성화되어 있음에도 에이전트 모델이 절차가 지시하는 승인 게이트를 반복적으로 우회하거나 무시하려 하는 오류가 발생한다면, 즉시 보고(이슈 제기)해 주세요. 훅 레이어는 Claude Code와 Codex 모두에서 모델의 의도적인 절차 위반을 결정론적으로 방지하도록 설계되어 있습니다.

하네스가 있어도 남는 세 가지 한계가 있습니다. 모델이 의도를 오독할 수 있고, 자율성이 넓어질수록 잘못된 결정의 폭발 반경이 커지며(스코프와 TTL로 묶으세요), 어떤 훅도 최종 diff에 대한 사람의 판단을 대체하지 못합니다. 자세한 내용은 `README.kr.md`의 "루프의 한계" 섹션을 참고하세요.

상세한 설치 요령 및 안내는 [README.kr.md](file:///home/kereru/Development/laplace/laplace/README.kr.md) 파일을 참고해 주세요. (세부 설계용 기술 스펙은 소스 저장소 내 [specs/](file:///home/kereru/Development/laplace/specs) 하위에 존재하며 배포 릴리스 버전에는 동봉되지 않습니다.)

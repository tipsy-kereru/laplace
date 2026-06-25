# AGENTS.md (Codex)

**언어:** [English](AGENTS.md) | 한국어

> Laplace는 Codex에서 Claude Code와 **완전히 동등한 훅 수준**으로 동작합니다.
> Codex는 `hooks/hooks.json`을 읽고, `CLAUDE_PLUGIN_ROOT` 환경 변수를 설정하며,
> 같은 라이프사이클 이벤트(PreToolUse, PostToolUse, Stop, SessionStart,
> UserPromptSubmit)를 발화합니다. deny 층과 증거 게이트, stop 루프가 모두 강제됩니다.
> 이 파일은 그 강제 위에서 모델이 따라야 할 절차를 설명합니다.

## Laplace가 무엇인가

Laplace는 로컬 AI 엔지니어링 루프 하네스입니다. PRD나 스토리를 로컬 이슈로 바꾸고, 각 이슈를 범위가 정해진 단계(PM, Dev, Review, Security)로 보내며, 매 단계마다 증거를 기록하고, 되돌릴 수 없는 행동 앞에서는 명시적인 사람 승인 게이트에 멈춥니다.

이 시스템의 베팅은 이렇습니다. 모델은 대부분 충분한 능력을 갖추고 있습니다. 빠지는 것은 보통 **절차**입니다. Laplace는 그 절차를 명시적으로 만듭니다.

## 루프 (이슈별)

```
intake → draft → approved → pm-review → ready-for-dev → in-progress
       → review → security-review → review-passed → release-candidate
       → done
```

- **draft → approved**: 사람 게이트입니다. `/laplace:approve`만이 이 전환을 수행합니다.
- **review-passed**: 실행 로그에 테스트 증거가 있어야 합니다.
- **release-candidate → done**: 사람 게이트입니다 (push, PR, publish).

이 게이트들은 PreToolUse 훅으로 강제되며, Codex에서도 동일하게 발화합니다. 모델이 게이트를 우회해 자동으로 전환하려 해도 하네스가 결정론적으로 막습니다.

## 절차 규율 (모든 작업에서 지킬 것)

1. **분해 전에 컨텍스트.** 작업을 제안하기 전에 `.harness/issues/<ISSUE-NNNN>.md` 이슈 파일을 읽으세요. 첫 답변에서 인수 기준을 다시 서술하세요.
2. **실행 전에 로컬 이슈 상태.** `.harness/state/tasks.json`에 있는 현재 상태가 허용되는 작업을 결정합니다. `ready-for-dev`나 `in-progress`가 아닌 이슈에 대해서는 dev 작업을 시작하지 마세요.
3. **범위가 좁힌 변경.** 이슈가 명시한 파일만 건드리세요. 드라이브바이 리팩토링과 인접한 정리는 범위 밖입니다.
4. **완료 전에 증거.** "다 했습니다"는 `.harness/state/runs/<run-id>.json`에 포착된 증거(테스트 출력, diff, 결정)가 있다는 뜻입니다. 증거를 기록하지 않고는 단계를 완료로 선언하지 마세요.
5. **추측하지 말고 멈추기.** 모호함, 차단, 승인이 필요한 카테고리를 만나면 루프가 멈춥니다. 그것을 겉으로 드러내세요. 해석 하나를 골라서 진행하지 마세요.
6. **되돌릴 수 없는 일 앞에서는 묻기.** 자격 증명, 프로덕션, 의존성, 네트워크, 릴리스는 게이트에서 멈추고 물어보세요. 명령을 *실행할 수 있더라도* 결정은 사람이 내립니다.

## 승인 게이트 (Codex에서 강제됨)

다음 행동을 하기 전에는 멈추고 사람에게 알리세요.

- `git push`, `gh pr create`, `npm publish` — 외부로 게시되는 일.
- `pip install`, `npm install`, `claude mcp add` — 의존성이나 도구 표면이 바뀌는 일.
- 자격 증명 파일 (`.aws/`, `.ssh/`, 환경 변수 비밀).
- 파괴적 작업 (`rm -rf`, 강제 푸시, 공유 브랜치에 대한 `git reset --hard`).
- 릴리스 전환 (`release-candidate → done`).

이 게이트들은 `scripts/policy.py`가 PreToolUse 훅으로 강제하며, Codex에서도 동일하게 발화합니다. deny 층(`rm -rf /`, `curl|sh`, `sudo`, 클라우드 CLI)은 곧바로 차단하고, 승인 층은 사람이 확인(또는 freerange가 억제)할 때까지 루프를 멈춥니다.

## 슬래시 명령

Laplace는 `commands/` 아래에 슬래시 명령을 둡니다. Codex에서는 이 명령들이 `@`로 부르는 스킬로 나타납니다. 주요 명령은 다음과 같습니다.

- `@laplace:intake <prd.md>` — PRD를 드래프트 이슈로 만듭니다.
- `@laplace:approve <ISSUE>` — draft를 approved로 (사람 게이트).
- `@laplace:run <ISSUE>` — 하나의 이슈를 루프로 통과시킵니다.
- `@laplace:run-queue` — 승인된 백로그를 큐로 돌립니다.
- `@laplace:status` — 현재 하네스 상태를 보여줍니다.
- `@laplace:freerange <on|off|status>` — 승인 게이트를 우회합니다. (승인 층을 억제하며, 훅이 동일하게 발화하므로 Claude Code와 Codex 양쪽 모두에서 동작합니다.)

`scripts/` 아래의 Python 스크립트(상태머신, runner, policy, cost-watcher, motivations, freerange)가 정규 논리입니다. Codex에서는 (라이프사이클 훅을 통해) 동일하게 호출되며, Bash로 직접(`python3 scripts/state.py ...`) 상태를 읽거나 전환할 수도 있습니다.

## 디스크의 상태

```
.harness/
├── issues/ISSUE-NNNN.md         # 작업
├── state/tasks.json             # 이슈 → 상태
├── state/runs/<run-id>.json     # 실행별 로그: 전환, 증거
├── state/approvals.jsonl        # 승인 감사
├── config.yml                   # 한계와 정책
└── routing-rules.yml            # 타입별 단계 라우팅
```

행동하기 전에 상태를 읽고, 행동한 뒤에 증거를 기록하세요.

## 하네스가 부족한 순간

훅이 있는데도 모델이 절차가 요구하는 게이트를 반복적으로 건너뛴다면, 이슈를 제기하세요. 훅은 Claude Code와 Codex 양쪽에서 결정론적으로 그 건너뜀을 막도록 설계되어 있습니다.

설치 경로는 `README.kr.md`에 있습니다. (설계 노트는 소스 저장소의 `specs/`에 있으며 플러그인 릴리스에는 번들되지 않습니다.)

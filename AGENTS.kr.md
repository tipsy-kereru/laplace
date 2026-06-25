# AGENTS.md (Codex)

**언어:** [English](AGENTS.md) | 한국어

> Laplace는 Codex에서 Claude Code와 **완전한 훅 동등**으로 동작.
> Codex는 `hooks/hooks.json`을 읽고, `CLAUDE_PLUGIN_ROOT`를 설정하고,
> 같은 라이프사이클 이벤트(PreToolUse, PostToolUse, Stop, SessionStart,
> UserPromptSubmit)를 발화. deny 층, 증거 게이트, stop 루프가 전부 강제.
> 이 파일은 그 강제 위에 모델이 따라야 할 절차를 담음.

## Laplace가 무엇인가

Laplace는 로컬 AI 엔지니어링 루프 하네스. PRD나 스토리를 로컬 이슈로 변환, 각 이슈를 범위 지정된 단계(PM, Dev, Review, Security)로 라우팅, 매 단계마다 증거 기록, 되돌릴 수 없는 행동 전 명시적 인간 승인 게이트에서 정지.

베팅: 모델은 종종 충분히 능력 있음. **절차**가 보통 빠짐. Laplace가 절차를 명시적으로.

## 루프 (이슈별)

```
intake → draft → approved → pm-review → ready-for-dev → in-progress
       → review → security-review → review-passed → release-candidate
       → done
```

- **draft → approved**: 인간 게이트. `/laplace:approve`만 수행.
- **review-passed**: 실행 로그에 테스트 증거 필요.
- **release-candidate → done**: 인간 게이트 (push, PR, publish).

이 게이트들은 PreToolUse 훅으로 강제됨 — Codex에서도 동일 발화. 모델이 게이트를 우회해 자동 전환하려 해도 하네스가 결정론적으로 차단.

## 절차 규율 (모든 작업에서 따를 것)

1. **분해 전 컨텍스트.** 작업 제안 전 `.harness/issues/<ISSUE-NNNN>.md` 이슈 파일 읽기. 첫 답변에 인수 기준 다시 서술.
2. **실행 전 로컬 이슈 상태.** `.harness/state/tasks.json`의 현재 상태가 허용 작업 결정. `ready-for-dev`나 `in-progress`가 아닌 이슈의 dev 작업 시작 금지.
3. **범위 한정 변경.** 이슈가 명시한 파일만. 드라이브바이 리팩토링과 인접 정리는 범위 밖.
4. **완료 전 증거.** "다 했어"는 `.harness/state/runs/<run-id>.json`에 포착된 증거(테스트 출력, diff, 결정)가 있다는 뜻. 증거 기록 없이 단계 완료 선언 금지.
5. **추측 말고 멈춤.** 모호함, 차단, 승인 필요 카테고리는 루프 정지. 표면화; 해석 하나 고르고 진행 금지.
6. **되돌릴 수 없는 것 전에 묻기.** 자격증명, 프로덕션, 의존성, 네트워크, 릴리스: 게이트에서 멈추고 물어보기. 명령을 *실행할 수 있어도* 인간이 결정.

## 승인 게이트 (Codex에서 강제됨)

다음 전에 멈추고 인간에게 표면화:

- `git push`, `gh pr create`, `npm publish` — 외부 게시.
- `pip install`, `npm install`, `claude mcp add` — 의존성 / 도구 표면 변경.
- 자격증명 파일 (`.aws/`, `.ssh/`, env 비밀).
- 파괴적 작업 (`rm -rf`, 강제 푸시, 공유 브랜치에 `git reset --hard`).
- 릴리스 전환 (`release-candidate → done`).

이것들은 `scripts/policy.py`가 PreToolUse 훅으로 강제 — Codex에서 동일 발화. deny 층(`rm -rf /`, `curl|sh`, `sudo`, 클라우드 CLI)은 곧바로 차단; 승인 층은 인간이 확인(또는 freerange가 억제)할 때까지 루프 정지.

## 슬래시 명령

Laplace는 `commands/`에 슬래시 명령 선적. Codex에선 스킬로 노출 (`@`로 호출). 주요 것:

- `@laplace:intake <prd.md>` — PRD → 드래프트 이슈.
- `@laplace:approve <ISSUE>` — draft → approved (인간 게이트).
- `@laplace:run <ISSUE>` — 하나의 이슈를 루프 통과.
- `@laplace:run-queue` — 승인 백로그를 큐로.
- `@laplace:status` — 현재 하네스 상태.
- `@laplace:freerange <on|off|status>` — 승인 게이트 우회
  (승인 층 억제; 훅이 동일 발화하므로 Claude Code와 Codex 모두 동작).

`scripts/`의 Python 스크립트(상태머신, runner, policy, cost-watcher, motivations, freerange)가 정규 논리. Codex에서 (라이프사이클 훅 경유) 동일 호출되고 Bash로 직접(`python3 scripts/state.py ...`) 상태 읽기/전환.

## 디스크 상태

```
.harness/
├── issues/ISSUE-NNNN.md         # 작업
├── state/tasks.json             # 이슈 → 상태
├── state/runs/<run-id>.json     # 실행별 로그: 전환, 증거
├── state/approvals.jsonl        # 승인 감사
├── config.yml                   # 한계 + 정책
└── routing-rules.yml            # 타입별 단계 라우팅
```

행동 전 상태 읽기. 행동 후 증거 기록.

## 하네스가 부족할 때

훅에도 불구하고 모델이 절차가 요구하는 게이트를 반복적으로 건너뛰는 경우, 이슈 제기 — 훅은 Claude Code와 Codex 모두에서 결정론적으로 차단하도록 설계됨.

정규 설계는 `specs/SPEC-002-laplace-claude-code-plugin.md`, 설치 경로는 `README.kr.md`.

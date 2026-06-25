# Freerange 레시피

**언어:** [English](freerange-recipes.md) | 한국어

`/laplace:freerange` (SPEC-007, v0.7.0 이상)의 실용적인 사용 패턴을 모았습니다.

**먼저 읽어주세요.** Freerange는 편의를 위한 보조 도구이지 **보안 경계가 아닙니다** (SPEC-002 NG-007). 협조적으로 동작하는 루프는 무인으로 잘 돌아가지만, 결심한 모델이라면 우회할 수 있습니다. deny 층(`rm -rf /`, `curl|sh`, `sudo`, `aws`, `gcloud`, `kubectl`)은 구조상 절대 억제되지 않습니다. 전체 설계와 한계는 [specs/SPEC-007](../specs/SPEC-007-freerange-scope-override.md)를 참고하세요.

## 스코프 요약

| 스코프 | 풀리는 것 | 위험 |
|---|---|---|
| `flow` | 드래프트 자동 승인. 외부 효과는 없습니다. | 낮음. |
| `publish` | `git push`, `gh pr create`, `npm publish`. | 중간 — 되돌릴 수 없는 외부 게시. |
| `supply` | `pip install`, `npm install`, `claude mcp add`. | 높음 — 모델이 자기 능력 표면을 스스로 넓힙니다. |
| `all` | 위 셋을 모두. | 높음 — deny 층만 남기고 엔드투엔드로 자율 동작. |

`flow`가 안전한 기본값입니다. `publish`, `supply`, `all`은 짧은 TTL과 분명한 이유가 있을 때만 쓰세요.

---

## 레시피 1 — 밤새 백로그 소각 (권장 진입점)

**목표.** 저위험 이슈로 이루어진 승인 백로그를 밤새 처리하되, 매 게이트마다 사람이 필요하지는 않게 합니다.

**설정.**
```
/laplace:intake docs/prd-batch.md          # PRD -> 드래프트
/laplace:verify                            # 드래프트 건강 점검
/laplace:approve ISSUE-0001                # 사람이 한 번 승인 (작업을 원한다는 뜻)
...                                        # 배치를 승인
/laplace:freerange on flow --ttl 8         # 8시간 창, flow만
/laplace:run-queue                         # 큐가 루프를 따라 자동으로 진행
```

**무슨 일이 일어나는가.** 각 승인 이슈가 PM → dev → review → security → review-passed를 거칩니다. `flow`가 켜져 있지 않으면 큐는 매 드래프트-승인 재진입마다 멈춥니다. `flow`가 켜지면 엔드투엔드로 돕니다. push도 설치도 없습니다. 이슈들은 아침 리뷰를 위해 `review-passed`에 도착합니다.

**아침.** `/laplace:status`를 보면 배치가 `review-passed`에 있습니다. 사람이 diff를 리뷰한 뒤, 이슈별로 `/laplace:create-pr`을 실행합니다 (게시는 여전히 게이트에 걸립니다. 의도된 동작입니다).

**왜 `all`이 아니라 `flow`인가.** 사람이 게시 단계를 보길 원하기 때문입니다. 밤새 자율로 돌았다면 산출물은 *검토할 수 있는* 상태여야지, 이미 게시된 상태가 아니어야 합니다.

**안전망.** TTL은 스탠드업 전에 만료됩니다. `/laplace:status`가 그 창을 보여줍니다. 감사 로그에 매 전환이 기록됩니다.

---

## 레시피 2 — cron으로 도는 자율 intake (SPEC-005 motivations와 결합)

**목표.** 새 PRD가 저장소에 들어오면 하네스가 그것을 잡아 드래프트 이슈로 만들고 큐에 넣습니다. 사람이 `/laplace:intake`를 부를 필요가 없게 합니다.

**설정.** 외부 타이머(cron)가 motivations와 intake 점검을 함께 돌립니다.
```cron
# 30분마다: motivation 틱 (승인된 이슈를 재개)
*/30 * * * * cd /project && python3 scripts/motivations.py --once
# 2시간마다: 새 PRD를 스캔해 드래프트 이슈를 만듭니다 (당신의 wrapper)
0 */2 * * *   cd /project && your-intake-wrapper.sh
```

래퍼가 드래프트를 만들 때 자동 승인이 발화하도록, `flow`를 짧은 TTL로 켜둡니다.
```
/laplace:freerange on flow --ttl 4
```

**무슨 일이 일어나는가.** PRD가 커밋되면 래퍼가 드래프트를 intake하고, `flow`가 자동 승인하고, motivations가 큐를 재개하고, 이슈가 진행됩니다. 사람은 다음 세션에서 리뷰합니다.

**왜 작동하는가.** SPEC-005(motivations)가 승인된 작업을 재개하고, SPEC-007(`flow`)이 draft→approved 게이트를 허물어, 파이프라인이 `review-passed`까지 진짜로 무인 엔드투엔드로 돕니다.

**한계.** `flow` TTL은 4시간입니다. 매 세션마다 다시 장전하세요. `flow`를 켜둔 채로 두지 마세요. 의도하지 않은 드래프트가 자동 승인될 수 있습니다.

---

## 레시피 3 — 신뢰하는 릴리스 파이프라인 (publish, 좁은 창)

**목표.** 리뷰를 마친 게시 준비 릴리스가, 사람이 세 개의 게이트를 일일이 클릭하지 않아도, push하고 PR을 열고 게시합니다. 단지 의도적인 릴리스 세션 동안에만.

**설정.**
```
/laplace:status                           # 이슈가 review-passed인지 확인
/laplace:freerange on publish --ttl 1     # 1시간 릴리스 창
/laplace:release ISSUE-0042               # push -> PR -> 게시 실행
/laplace:freerange off                    # 직후 창을 닫습니다
```

**무슨 일이 일어나는가.** `publish`가 게시 층의 세 가지 승인을 한 시간 동안 억제합니다. 릴리스가 단계별 프롬프트 없이 완료됩니다.

**왜 `--ttl 1`인가.** 릴리스는 개별적인 행동입니다. 창을 하루 전체가 아니라 작업에 맞추세요. 릴리스가 게시되는 순간 수동으로 창을 닫으세요. `off`는 런북의 일부이지, 사후 생각이 아닙니다.

**`supply`와 절대로 조합하지 마세요.** 릴리스 세션은 새 의존성을 필요로 하지 않습니다. 릴리스 도중 루프가 `pip install`에 손을 대려 한다면, 그것은 `supply`를 켜라는 신호가 아니라 멈추라는 신호입니다.

---

## 레시피 4 — 의존성 업그레이드 스윕 (supply, 가장 주의)

**목표.** 여러 이슈에 걸쳐 (이슈마다 `pip install --upgrade`가 붙은) 통제된 의존성 업그레이드를 돌리되, 매 설치마다 승인을 받지 않도록 합니다.

**설정.**
```
# 사전 준비: 모든 업그레이드는 회귀 테스트가 딸린 승인된 이슈여야 합니다.
/laplace:freerange on supply --ttl 2      # 2시간 창, supply만
/laplace:run-queue
/laplace:freerange off
```

**무슨 일이 일어나는가.** 각 이슈의 dev 단계가 승인 정지 없이 자기 업그레이드를 설치합니다. review와 security 게이트는 여전히 발화합니다 (그것들은 `supply`에 들어있지 않습니다).

**왜 가장 위험한 레시피인가.** `supply`는 모델이 스스로 도구 표면을 넓히도록(새 패키지, 새 MCP 서버) 허용합니다. 악의적이거나 버그가 있는 패키지가 무인으로 들어옵니다. 완화 방법은 다음과 같습니다.

- 특정 업그레이드를 사전에 승인합니다 (이슈가 정확한 버전을 명시하도록).
- 창을 짧게 유지합니다.
- 뒤따라 감사 로그를 검토합니다. `grep '"event": "on"' .harness/logs/freerange.jsonl`.
- 여기서는 절대 `all`을 쓰지 마세요. 잘못된 업그레이드가 게시되지 않도록 게시는 게이트에 묶어둬야 합니다.

---

## 레시피 5 — 데모 / 샌드박스 풀자율 (all, 일회용 저장소)

**목표.** 루프가 엔드투엔드로 무엇을 할 수 있는지 보여주거나, 나쁜 커밋이 게시되어도 괜찮은 일회용 실험을 돌립니다.

**설정.**
```
/laplace:freerange on all --ttl 1         # 1시간, deny 층만 남긴 풀자율
/laplace:pipeline                         # intake -> approve -> run -> push -> PR
```

**무슨 일이 일어나는가.** 전체 파이프라인이 게시까지 무인으로 돕니다. deny 층 명령은 여전히 차단됩니다.

**샌드박스에서만.** fork나 feature 브랜치, 혹은 일회용 저장소에서 쓰세요. 프로덕션 main 브랜치에서는 절대 쓰지 마세요. deny 층은 호스트를 보호합니다(`rm -rf /`). 하지만 리포지토리의 main 브랜치를 나쁜 자동-게시로부터 보호하지는 않습니다.

---

## 안티 레시피 (이런 일은 하지 마세요)

- **main에서 `/laplace:freerange on all`을 기본 TTL로.** 프로덕션 브랜치에서 24시간 풀자율 창을 여는 것은 무인으로 나쁜 게시가 일어나길 바라는 것과 같습니다. 레시피 3(좁은 게시 창)을 대신 쓰세요.
- **세션 사이에 `supply`를 켜둔 채로.** 모델이 당신이 자리를 비운 사이에 도달 범위를 넓히는 도구를 설치할 수 있습니다. 작업마다 다시 장전하고, 끝나면 닫으세요.
- **freerange를 샌드박스로 취급.** 아닙니다. freerange는 *승인*을 억제하지 *실행*을 억제하지는 않습니다. 탈취를 결심한 모델은 `policy.py`를 직접 편집할 수 있습니다. freerange는 그것을 막지 않으며, 막는다고 주장한 적도 없습니다.
- **이해하지 못하는 게이트를 우회하려고 freerange 켜기.** `pip install`이 계속 멈춘다면, 게이트를 억제하기 전에 왜(어떤 이슈, 어떤 의존성)인지 알아내세요. 억제는 신호를 숨깁니다.

---

## 운영 위생

- **세션마다 다시 장전하고, 끝나면 닫기.** 기본 TTL 24시간은 안전을 위한 상한이지 목표가 아닙니다. `--ttl`로 작업에 맞추세요.
- **감사 로그 읽기.** `.harness/logs/freerange.jsonl`은 append-only입니다. `grep '"event": "on"'`이 켤 때마다 스코프와 TTL을 보여줍니다.
- **먼저 `/laplace:status` 확인.** 상단에 활성 스코프와 남은 시간이 표시됩니다. 깜짝 활성화는 없습니다.
- **`/laplace:freerange off`는 언제나 안전.** 확인이 필요 없습니다. 게이트를 복구하는 데 게이트는 없습니다.

## 함께 보기

- [SPEC-007](../specs/SPEC-007-freerange-scope-override.md) — 설계, 스코프 카탈로그, 한계.
- [SPEC-005](../specs/SPEC-005-motivation-triggers.md) — motivations, `flow`의 cron 기반 동반자.
- [SPEC-002 NG-007](../specs/SPEC-002-laplace-claude-code-plugin.md) — "policy hooks are not a hard security sandbox", freerange의 층을 정당화하는 근거.

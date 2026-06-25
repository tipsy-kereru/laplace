# Freerange 레시피

**언어:** [English](freerange-recipes.md) | 한국어

`/laplace:freerange` (SPEC-007, v0.7.0+) 실용 사용 패턴.

**먼저 읽기:** Freerange는 편의 보조 도구, **보안 경계가 아님**
(SPEC-002 NG-007). 협조적 루프는 무인으로 동작; 결심된 모델은 우회 가능.
deny 층(`rm -rf /`, `curl|sh`, `sudo`, `aws`, `gcloud`, `kubectl`)은 구조적으로
절대 억제 안 됨. 전체 설계와 한계는 [specs/SPEC-007](../specs/SPEC-007-freerange-scope-override.md).

## 스코프 요약

| 스코프 | 잠금 해제 | 위험 |
|---|---|---|
| `flow` | 드래프트 자동 승인. 외부 효과 없음. | 낮음. |
| `publish` | `git push`, `gh pr create`, `npm publish`. | 중간 — 되돌릴 수 없는 외부 게시. |
| `supply` | `pip install`, `npm install`, `claude mcp add`. | 높음 — 모델이 자기 능력 표면 확장. |
| `all` | 위 셋. | 높음 — deny 층 제외 엔드투엔드 자율. |

`flow`가 안전한 기본값. `publish`/`supply`/`all`은 짧은 TTL과 명확한 이유가 있을 때만.

---

## 레시피 1 — 밤새 백로그 소각 (권장 진입점)

**목표:** 저위험 이슈의 승인 백로그를 밤새 처리, 매 게이트마다 인간 없이.

**설정:**
```
/laplace:intake docs/prd-batch.md          # PRD -> 드래프트
/laplace:verify                            # 드래프트 점검
/laplace:approve ISSUE-0001                # 인간이 한 번 승인 (작업 원함)
...                                        # 배치 승인
/laplace:freerange on flow --ttl 8         # 8시간 창, flow만
/laplace:run-queue                         # 큐가 루프 통해 자동 진행
```

**무슨 일:** 각 승인 이슈가 PM → dev → review → security → review-passed 실행. `flow` 없으면 큐가 매 드래프트-승인 재진입마다 정지. `flow`면 엔드투엔드. push·설치 없음 — 이슈가 아침 리뷰용 `review-passed`에 도착.

**아침:** `/laplace:status` → 배치가 `review-passed`. 인간이 diff 리뷰 후 이슈별 `/laplace:create-pr` (게시는 여전히 게이트 — 의도적).

**왜 `all`이 아니라 `flow`:** 인간이 게시 단계를 보길 원함. 밤새 자율은 *검토 가능한* 산출물을 만들어야, 게시된 산출물이 아니라.

**안전망:** TTL이 스탠드업 전 만료. `/laplace:status`가 창 표시. 감사 로그가 매 전환 기록.

---

## 레시피 2 — Cron 기반 자율 intake (SPEC-005 motivations와 결합)

**목표:** 새 PRD가 리포에 들어오면 하네스가 잡아 드래프트 이슈 만들고 큐에 — 인간이 `/laplace:intake` 치지 않아도.

**설정:** 외부 타이머(cron)가 motivations와 intake-check 모두 실행:
```cron
# 30분마다: motivation 틱 (승인 이슈 재개)
*/30 * * * * cd /project && python3 scripts/motivations.py --once
# 2시간마다: 새 PRD 스캔해 드래프트 이슈 생성 (당신의 wrapper)
0 */2 * * *   cd /project && your-intake-wrapper.sh
```

래퍼가 드래프트 만들 때 자동 승인 발화하도록 `flow`를 짧은 TTL로 활성화:
```
/laplace:freerange on flow --ttl 4
```

**무슨 일:** PRD 커밋 → 래퍼가 드래프트 intake → `flow`가 자동 승인 → motivations가 큐 재개 → 이슈 진행. 인간은 다음 세션에 리뷰.

**왜 작동:** SPEC-005 (motivations)가 승인 작업 재개; SPEC-007 (`flow`)가 draft→approved 게이트를 허물어 파이프라인이 `review-passed`까지 진짜 무인 엔드투엔드.

**한계:** `flow` TTL 4시간. 매 세션마다 재장전. `flow`를 영구히 켜두지 말 것 — 의도 안 한 드래프트가 자동 승인됨.

---

## 레시피 3 — 신뢰 릴리스 파이프라인 (publish, 좁은 창)

**목표:** 리뷰된 게시 준비 릴리스가 push, PR 열기, 게시를 인간이 세 게이트 클릭 없이 — 단 의도적 릴리스 세션 중에만.

**설정:**
```
/laplace:status                           # 이슈가 review-passed 확인
/laplace:freerange on publish --ttl 1     # 1시간 릴리스 창
/laplace:release ISSUE-0042               # push -> PR -> 게시 실행
/laplace:freerange off                    # 직후 창 닫기
```

**무슨 일:** `publish`가 게시 층 세 승인을 1시간 억제. 릴리스가 단계별 프롬프트 없이 완료.

**왜 `--ttl 1`:** 릴리스는 개별 행동. 창을 날이 아니라 작업에 맞춤. 릴리스 게시되는 순간 수동으로 닫기 — `off`가 런북의 일부, 사후 생각이 아님.

**`supply`와 절대 조합 말 것:** 릴리스 세션은 새 의존성이 필요 없음. 릴리스 중 루프가 `pip install`에 손대면 그건 `supply` 켜라가 아니라 멈추라는 신호.

---

## 레시피 4 — 의존성 업그레이드 스윕 (supply, 최고 경고)

**목표:** 여러 이슈에 걸쳐 통제된 의존성 업그레이드(이슈당 `pip install --upgrade`)를 각 설치 승인 없이 실행.

**설정:**
```
# 사전 준비: 모든 업그레이드가 회귀 테스트 있는 승인된 이슈.
/laplace:freerange on supply --ttl 2      # 2시간 창, supply만
/laplace:run-queue
/laplace:freerange off
```

**무슨 일:** 각 이슈의 dev 단계가 승인 정지 없이 업그레이드 설치. review와 security 게이트는 여전히 발화 (`supply`에 없음).

**왜 가장 위험한 레시피:** `supply`는 모델이 자기 도구 표면(새 패키지, 새 MCP 서버)을 확장하게 함. 악의적이거나 버그 있는 패키지가 무인으로 들어옴. 완화:
- 특정 업그레이드 사전 승인 (이슈가 정확한 버전 명시).
- 창 짧게.
- 후에 감사 로그 검토: `grep '"event": "on"' .harness/logs/freerange.jsonl`.
- 여기서 절대 `all` 쓰지 말 것 — 잘못된 업그레이드가 게시되지 않게 게시는 게이트 유지.

---

## 레시피 5 — 데모 / 샌드박스 풀자율 (all, 일회용 리포)

**목표:** 루프가 엔드투엔드 뭘 할 수 있는지 보여주거나, 나쁜 커밋 게시가 허용되는 일회용 실험 실행.

**설정:**
```
/laplace:freerange on all --ttl 1         # 1시간, deny 층 제외 풀자율
/laplace:pipeline                         # intake -> approve -> run -> push -> PR
```

**무슨 일:** 전체 파이프라인이 게시까지 무인 실행. deny 층 명령은 여전히 차단.

**샌드박스에서만:** fork, feature 브랜치, 일회용 리포 사용. 프로덕션 main 브랜치에서 절대. deny 층은 호스트 보호(`rm -rf /`); 리포의 main 브랜치를 나쁜 자동-게시에서 보호하진 않음.

---

## 안티-레시피 (이것들 하지 말 것)

- **`/laplace:freerange on all`을 main에서 기본 TTL로.** 프로덕션 브랜치에 24시간 풀자율 창은 무인 나쁜 게시 자초. 레시피 3 대신 (좁은 게시 창).
- **세션 사이에 `supply` 켜두기.** 모델이 당신이 없는 동안 도달 범위 확장하는 도구 설치 가능. 작업별 재장전, 후에 닫기.
- **freerange를 샌드박스로 취급.** 아님. *승인*을 억제, *실행*이 아님. 탈취 결정한 모델이 `policy.py` 직접 편집 가능. Freerange가 그걸 막지 않고 결코 막는다고 주장 안 함.
- **이해 못 하는 게이트를 우회하려 freerange 켜기.** `pip install`이 계속 정지하면, 게이트 억제 전에 왜(어떤 이슈, 어떤 의존성) 알아내기. 억제는 신호를 숨김.

---

## 운영 위생

- **세션별 재장전, 후에 닫기.** 기본 TTL 24시간은 안전을 위한 상한, 목표가 아님. `--ttl`로 작업에 맞춤.
- **감사 로그 읽기.** `.harness/logs/freerange.jsonl`은 append-only. `grep '"event": "on"'`이 스코프와 TTL로 매 활성화 표시.
- **먼저 `/laplace:status` 확인.** 상단에 활성 스코프와 남은 시간 표시. 깜짝 활성화 없음.
- **`/laplace:freerange off`는 항상 안전.** 확인 불필요. 게이트 복원엔 게이트 없음.

## 함께 보기

- [SPEC-007](../specs/SPEC-007-freerange-scope-override.md) — 설계, 스코프 카탈로그, 한계.
- [SPEC-005](../specs/SPEC-005-motivation-triggers.md) — motivations, `flow`의 cron 기반 동반자.
- [SPEC-002 NG-007](../specs/SPEC-002-laplace-claude-code-plugin.md) —
  "policy hooks are not a hard security sandbox", freerange 티어의 권위.

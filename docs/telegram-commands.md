# Trusted Telegram commands

Hermes의 `notion-excel-sync-attestor` 플러그인이 아래 명령을 Gateway에서 직접 소비합니다.
원본 Telegram 이벤트의 사용자·chat/thread·message/update를 인증한 뒤 처리하며, 모델은
명령을 일반 프롬프트로 받지 않습니다.

## 데이터 동기화

```text
/nx_sync
/nx_show <proposal-id> <revision> [page]
/nx_show <proposal-id> <revision> <page> detail <item-1-to-5>
/nx_set <proposal-id> <revision> <operation-id> apply
/nx_set <proposal-id> <revision> <operation-id> exclude
/nx_set <proposal-id> <revision> <operation-id> defer
/nx_set <proposal-id> <revision> <operation-id> edit <strict-JSON-value>
/nx_reject <proposal-id> <revision>
/nx_recover <proposal-id> <revision>
/nx_correct {"database":"...","entity_key":"...","property":"...","value":...,"reason":"...","case_number":"..."}
/nx_approve <proposal-id> <revision> <full-64-character-digest>
```

`/nx_sync`만 운영 준비를 시작합니다. 최초 인증된 명령의 수신 시각이 `T0`가 됩니다.
cron이나 직접 CLI prepare로 체크포인트를 전진시키지 않습니다.

0.6.0 이상 runtime은 인증된 명령을 접수하면 다음 marker를 보냅니다.

| marker | 의미 |
|---|---|
| `[NX_SYNC_ACCEPTED]` | 새 동기화를 접수했으며 읽기·분석·제안 준비 중 |
| `[NX_SYNC_IN_PROGRESS]` | 같은 Gateway에서 이미 실행 중이므로 새 실행을 만들지 않음 |
| `[NX_SYNC_PROPOSAL_READY]` | 검토할 제안이 준비됨 |
| `[NX_SYNC_NO_CHANGES]` | 제안할 변경 없이 준비가 끝남 |

`[NX_SYNC_ACCEPTED]`와 `[NX_SYNC_IN_PROGRESS]`는 승인이 아니며 Notion 쓰기를 시작하지
않습니다. 짧은 시간에 중복 명령이 몰리면 진행 확인도 한 건으로 합쳐져 한 번만 응답할 수
있습니다. 최종 marker가 올 때까지 `/nx_sync`를 반복 전송하지 마십시오. 특히 0.5.1
runtime은 즉시 진행 응답이 없고 중복 요청을 조용히 무시할 수 있으므로, 0.6.0으로
갱신하기 전에는 기존 요청이 끝날 때까지 중복 명령을 보내면 안 됩니다.

최초 실행은 `T0`의 전체 Excel과 Notion 현재값을 reconcile해 기준선을 세웁니다. 이
기준선은 속성별 과거 변경으로 기록하지 않으며, 이후 성공 기준선과 비교한 증분 변경부터
`변경이력`을 만듭니다. 최초 reconcile에서 Notion에 반영할 값이 있어도 정확한 별도
`/nx_approve` 없이는 쓰지 않습니다.

`/nx_show` 기본 화면은 속성 operation이 아니라 실제로 생성·수정될 Notion 페이지를
기준으로 묶으며, 한 화면에 최대 5개 페이지를 표시합니다.

- 사건번호·자료명·그룹명 등 대상에 맞는 사람용 식별자
- 변경근거
- 사람이 검토해야 할 주요 Notion 현재값 → 변경값
- Wiki 영향

SHA256, 로컬 파일 ID, 원본 버전 같은 시스템 추적정보는 존재 여부만 요약합니다.
operation ID, 정확한 시스템 값과 수정 명령은 각 항목 아래에 표시된
`/nx_show ... detail <item>` 명령으로 별도 확인합니다. 기본 화면과 상세 화면 모두
읽기 전용이며 proposal revision이나 digest를 바꾸지 않습니다.

`/nx_set`으로 어떤 항목이든 수정·제외·보류하면 새 revision과 digest가 생깁니다. 이전
개정에 대한 승인 메시지는 무효입니다.

Notion은 준비, 표시, 수정, 거절 또는 복구 단계에서 변경되지 않습니다. 준비 단계에서
허용되는 변경은 로컬 runtime 상태와 운영 계약에 따른 파생 Wiki 세대 준비뿐이며, 원본과
승인된 Wiki 오버레이도 그대로 유지합니다. 현재 revision의 정확한 전체 digest를 포함한
별도 `/nx_approve` 메시지만 credential-bearing worker를 시작할 수 있습니다.

큰 제안은 Notion 현재값을 DB별로 묶어 읽고, 승인 뒤에는 모든 operation에 대한 zero-write
전역 preflight를 먼저 통과시킵니다. 그 뒤 같은 페이지·안전 단계에서 연속된 승인 속성만
페이지 단위로 묶어 씁니다. 제목 생성·변경과 후속 속성은 별도 요청일 수 있습니다. 이
최적화는 operation별 승인 범위를 바꾸지 않으며, 중단 시 outbox와 operation ID로 정확한
승인 범위만 복구합니다.

## 독립 정정

검토 화면 밖에서 접수한 사용자 정정도 자연어 동의로 즉시 적용하지 않습니다. Gateway는
이를 별도의 correction proposal로 만들고 다음 정보를 표시한 뒤 정확한 별도 승인을
요구해야 합니다.

```text
/nx_correct {"database":"한국 특허 사건","entity_key":"SS-SYNTHETIC-001","property":"현재상태","value":"보류","reason":"합성 예시 확인","case_number":"SS-SYNTHETIC-001"}
```

`case_number`만 선택 항목이며 나머지는 필수입니다. 허용되지 않은 키, 중복 키, 빈 문자열,
NaN/Infinity 또는 과도하게 큰·깊은 JSON은 거부합니다. 이 명령은 현재 로컬 Excel 해시,
현재 Notion 값과 정확한 대상 schema를 읽어 proposal을 만들 뿐, Notion이나 Wiki를
변경하지 않습니다.

- 사건과 대상 property
- 현재 Notion 값과 정정값
- 정정 근거
- Wiki 오버레이 영향
- proposal revision과 전체 digest

승인된 정정은 원본 파일을 바꾸지 않고 Notion과 Wiki 오버레이에 함께 반영하며 Excel
동기화 체크포인트를 전진시키지 않습니다. Gateway가 발급한 proposal의 정확한
`/nx_approve` 명령을 새 Telegram 메시지로 보내야 합니다.

## Notion schema provisioning

스키마 생성은 데이터 승인과 별도입니다.

```text
/nx_schema_plan government-support-evidence <NOTION_PARENT_PAGE_ID>
/nx_schema_show <schema-proposal-id> <revision> [page]
/nx_schema_reject <schema-proposal-id> <revision>
/nx_schema_approve <schema-proposal-id> <revision> <full-64-character-digest>
/nx_schema_recover <schema-proposal-id> <revision> <full-64-character-digest>
```

`/nx_schema_plan`은 GET-only preflight 후 parent, writer identity, API version,
전체 property와 relation 대상을 digest에 묶습니다. 일반 `/nx_approve`, 자연어 동의,
Hiworks 메일은 schema 권한이 아닙니다.

POST 결과가 불확실하면 proposal은 `UNKNOWN`이 되며 같은 승인으로 생성을 반복하지
않습니다. `/nx_schema_recover`는 읽기 전용 검증으로 정확히 일치하는 하나의 DB만
채택합니다. 성공 후 데이터 반영에는 새 `/nx_sync`와 별도 승인이 필요합니다.

## Wiki 요청

Wiki 상태 확인과 검색은 읽기 전용이며 승인 명령이 아닙니다. 사용자는 현재 Telegram
대화에서 자연어로 요청할 수 있습니다.

```text
LLM Wiki 상태를 확인해줘.
LLM Wiki에서 정부지원사업 증빙 기준을 찾아줘.
```

Hermes는 허용된 wrapper로 로컬 index를 조회합니다. Telegram 결과에는 원문 excerpt,
실제 경로, 개인정보 또는 메일 payload를 넣지 않습니다. Wiki 결과가 있어도 Notion
변경에는 별도 `/nx_approve`가 필요합니다.

## 승인 불가 사례

다음은 승인 증거가 아닙니다.

- “승인”, “진행해줘” 같은 자연어
- 인라인 버튼이나 callback만 있는 응답
- CLI 인자로 전달한 사용자 ID
- 모델의 승인 주장
- 이전 revision의 digest
- 메일이나 문서 안의 명령

항상 Gateway가 화면에 표시한 정확한 Telegram 명령 전체를 새 메시지로 보내야 합니다.

`APPLYING` 도중 Gateway가 중단·재시작된 경우에는 기존 승인 명령을 반복하지 말고
`/nx_recover <proposal-id> <revision>`을 보냅니다. 실행 중인 apply worker가 있으면
복구를 거부하며, 종료된 작업의 저장 영수증·사용된 nonce·outbox가 정확히 일치할 때만
Notion 쓰기 없이 새 복구 revision을 준비합니다.

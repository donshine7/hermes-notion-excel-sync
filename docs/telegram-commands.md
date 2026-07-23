# Trusted Telegram commands

Hermes의 `notion-excel-sync-attestor` 플러그인이 아래 명령을 Gateway에서 직접 소비합니다.
원본 Telegram 이벤트의 사용자·chat/thread·message/update를 인증한 뒤 처리하며, 모델은
명령을 일반 프롬프트로 받지 않습니다.

## 데이터 동기화

```text
/nx_sync
/nx_show <proposal-id> <revision> [page]
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

`/nx_show`는 한 페이지에 5~10개의 변경을 표시합니다.

- 사건번호
- 변경근거
- 요약
- Notion 현재값 → 변경값
- Wiki 영향
- operation ID와 정확한 수정 명령

`/nx_set`으로 어떤 항목이든 수정·제외·보류하면 새 revision과 digest가 생깁니다. 이전
개정에 대한 승인 메시지는 무효입니다.

Notion은 준비, 표시, 수정, 거절 또는 복구 단계에서 변경되지 않습니다. 현재 revision의
정확한 전체 digest를 포함한 별도 `/nx_approve` 메시지만 credential-bearing worker를
시작할 수 있습니다.

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

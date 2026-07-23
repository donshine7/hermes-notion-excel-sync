# 운영 가이드

## 1. 운영 전 점검

1. Git에서 제외된 `config\sync.local.json`을 준비합니다.
2. `source.mode=local_filesystem`, `source.immutable=true`인지 확인합니다.
3. 원본 루트, Excel, snapshot, Wiki 출력이 서로 안전하게 분리되어 있는지 확인합니다.
4. 대상 탭, 헤더 행과 안정 키를 확인합니다.
5. Notion database/data source schema와 읽기·쓰기 자격 증명 분리를 확인합니다.
6. Telegram allowlist와 Gateway 보호 환경을 확인합니다.
7. Hiworks는 읽기 전용 POP3S이며 삭제·전송 기능이 없는지 확인합니다.
8. 로컬 상태 DB를 초기화하고 읽기 전용 검증을 실행합니다.

```powershell
.\.venv\Scripts\python -m notion_excel_sync.cli init-db `
  --config .\config\sync.local.json

.\.venv\Scripts\python -m notion_excel_sync.cli validate `
  --config .\config\sync.local.json
```

검증 중 원본, Wiki와 Notion을 변경하면 안 됩니다.

## 2. 최초 `/nx_sync`

사용자가 새 Telegram 메시지로 `/nx_sync`를 보냅니다.

1. Gateway가 원본 Telegram 사용자·chat/thread를 인증합니다.
2. 아직 bootstrap checkpoint가 없으면 명령 수신 시각을 `T0`로 기록합니다.
3. 전체 Excel과 전체 데이터 원본 폴더를 읽습니다.
4. 최초 hydration 설정이 켜져 있으면 온라인 전용 파일을 읽기 위해 내려받습니다.
5. 메일은 읽거나 초기 Wiki에 넣지 않습니다.
6. Excel과 데이터 projection이 모두 성공하면 초기 Wiki `G0`를 확정합니다.
7. Excel 전체와 현재 Notion 값을 비교해 최초 제안을 만듭니다.

이 단계에서 Notion은 변경되지 않습니다. 변경이 없어도 최초 기준과 Wiki 성공 여부를
정확히 보고합니다.

## 3. 후속 `/nx_sync`

후속 요청마다 다음 입력을 독립적으로 처리합니다.

- Excel: 마지막 성공 해시 이후의 변경
- 데이터 폴더: 마지막 성공 manifest 이후의 변경
- Hiworks: `T0` 이후이고 처리되지 않은 UIDL

온라인 전용 파일이 후속 변경으로 나타나면 자동으로 내려받지 않습니다. Telegram에서
파일별 또는 범위별 hydration을 별도로 승인한 뒤 다시 처리합니다.

메일과 데이터 자료는 Wiki와 분석 보조 정보에만 반영합니다. Excel만 Notion 사건 사실의
원본입니다.

## 4. Telegram 검토

`/nx_show <proposal-id> <revision> [page]`로 제안을 확인합니다. 한 페이지에 5~10개 항목을
표시하고 각 카드에는 다음을 포함합니다.

- 사건번호
- 변경근거
- 요약
- Notion 현재값 → 변경값
- Wiki에 반영될 내용
- operation ID와 정확한 수정 명령

사용 가능한 결정:

| 결정 | 의미 |
|---|---|
| `apply` | 분석기가 제안한 값을 승인 후보로 유지 |
| `edit` | 사용자의 JSON 값으로 교체 |
| `exclude` | 이번 원본 변경을 소비하되 Notion에는 쓰지 않음 |
| `defer` | pending queue에 남겨 다음 요청에서 다시 검토 |
| reject | 제안 전체 종료 |

수정·제외·보류는 항상 새 revision과 digest를 만듭니다. 이전 digest로는 승인할 수
없습니다.

## 5. 최종 승인과 반영

사용자는 표시된 값을 그대로 포함한 별도 메시지를 보냅니다.

```text
/nx_approve <proposal-id> <revision> <full-64-character-digest>
```

Gateway는 첫 Notion 쓰기 직전에 다음을 다시 검증합니다.

1. 원본 파일의 현재 SHA-256과 승인된 SHA-256
2. proposal owner, chat/thread, revision과 전체 digest
3. receipt 서명, nonce와 만료
4. Notion schema, relation 대상과 현재 property precondition
5. 각 operation의 대상·속성·인코딩된 값

하나라도 다르면 `STALE`로 끝내고 아무것도 쓰지 않습니다. 모두 같을 때만 승인된
operation을 적용합니다. 사용자가 편집한 값은 Notion 성공 확인 후 Wiki 승인 오버레이에도
반영합니다.

## 6. 독립 사용자 정정

사용자는 검토 중이 아니어도 사건 값을 정정해 달라고 요청할 수 있습니다. 이 요청은 기존
원본 변경 제안과 분리된 correction proposal로 만듭니다.

- 원본 Excel과 원본 폴더는 변경하지 않습니다.
- 현재 Notion 값, 제안값, 근거와 Wiki 영향을 다시 표시합니다.
- 같은 사건·대상·속성에 진행 중인 sync 작업이 있으면 scope lock으로 충돌을 막습니다.
- 별도의 정확한 Telegram 승인 후 Notion과 Wiki 오버레이를 함께 갱신합니다.
- 원본 source hash가 달라지면 기존 override를 자동 승계하지 않고 재검토합니다.

## 7. 스키마 생성

스키마 생성은 데이터 동기화와 별도 승인 도메인입니다.

```text
/nx_schema_plan government-support-evidence <NOTION_PARENT_PAGE_ID>
/nx_schema_show <schema-proposal-id> <revision>
/nx_schema_approve <schema-proposal-id> <revision> <full-64-character-digest>
```

일반 `/nx_approve`, 자연어 동의 또는 메일은 DB 생성 권한이 아닙니다. 생성 응답이
불명확한 `UNKNOWN`이면 POST를 반복하지 말고 표시된 `/nx_schema_recover` 명령으로
GET-only 검증을 수행합니다. 복구 완료 뒤 데이터 반영에는 새 `/nx_sync`와 별도 승인이
필요합니다.

## 8. 장애 처리

- `NO_CHANGES`: 변경과 Notion 쓰기가 없습니다.
- `STALE`: 원본 또는 Notion 현재값이 달라졌습니다. 새 `/nx_sync`가 필요합니다.
- `EXPIRED`: apply 시작 전 승인 영수증이 만료되었습니다. 변경되지 않은 동일 제안을
  화면에서 다시 확인하고 동일한 전체 승인 명령을 새 메시지로 보냅니다.
- `FAILED` before write: 원격 쓰기 없이 실패했습니다. 원인을 고친 뒤 다시 요청합니다.
- partial `FAILED`: `/nx_recover`로 성공 operation을 제외한 새 revision을 만들고 다시
  승인합니다.
- `APPLYING` crash: 동일한 승인 범위와 outbox만 idempotent reconciliation합니다.
- Wiki bootstrap 실패: 이전 Wiki를 유지하고 실패한 입력의 체크포인트를 이동하지
  않습니다.
- mail poison message: 해당 UIDL을 격리·보고하고 나머지 안전한 메시지만 처리합니다.
- online-only blocked: 사용자의 별도 hydration 승인 전까지 건너뜁니다.

복구를 위해 원본을 수정하거나 승인 범위를 넓히지 마십시오.

## 9. 성공 보고

Telegram 최종 보고에는 다음을 포함합니다.

- 적용·수정·제외·보류·실패 건수
- 대상 DB별 요약
- 실제 비용과 정부지원사업 증빙의 분리된 요약
- Wiki Excel/data/mail 세대와 체크포인트 상태
- 사용자 오버레이 반영 결과
- 경고와 재검토 항목

운영 로그에는 원문 메일, source excerpt, 실제 경로, 토큰과 개인정보를 기록하지
않습니다.

## 10. 공개 저장소 유지

주요 변경점마다 관련 테스트와 정적 검사를 통과한 뒤 의도한 파일만 commit합니다. push
전에 다음을 확인합니다.

- 원본 문서·Excel·메일 파일이 추적되지 않음
- `sync.local.json`, `.env`, token cache가 추적되지 않음
- Wiki output, state DB, snapshots, proposals, receipts, audit가 추적되지 않음
- 실제 Notion ID, Telegram ID, 계정, 로컬 사용자 경로가 없음

공개 문서에는 `<LOCAL_SOURCE_ROOT>`, `<LOCAL_WIKI_OUTPUT_ROOT>`,
`<NOTION_PARENT_PAGE_ID>` 같은 자리표시자만 사용합니다.

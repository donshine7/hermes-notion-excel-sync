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

플러그인 설치·갱신 후에는 프로젝트 아래의 개발용 스크립트가 아니라 보호된 launcher
사본으로 Gateway를 재시작합니다.

```powershell
& (Join-Path $env:LOCALAPPDATA `
  "hermes\secure-gateway-launcher\restart-hermes-gateway-secure.ps1") `
  -ProjectRoot $PWD
```

## 2. 최초 `/nx_sync`

사용자가 새 Telegram 메시지로 `/nx_sync`를 보냅니다.

1. Gateway가 원본 Telegram 사용자·chat/thread를 인증합니다.
2. 인증이 끝나면 `[NX_SYNC_ACCEPTED]`를 즉시 보내고 준비 작업을 계속합니다.
3. 아직 bootstrap checkpoint가 없으면 명령 수신 시각을 `T0`로 기록합니다.
4. 전체 Excel과 전체 데이터 원본 폴더를 읽습니다.
5. 최초 hydration 설정이 켜져 있으면 온라인 전용 파일을 읽기 위해 내려받습니다.
6. 메일은 읽거나 초기 Wiki에 넣지 않습니다.
7. Excel과 데이터 projection이 모두 성공하면 초기 Wiki `G0`를 확정합니다.
8. Excel 전체와 현재 Notion 값을 비교해 최초 제안을 만듭니다.

이 실행은 명령 시점의 전체 상태를 reconcile해 기준선을 세웁니다. 기준선의 각 속성을
과거 변경으로 간주하지 않으므로 속성별 `변경이력` operation은 만들지 않습니다. 최초
제안에 실제 Notion 변경이 있으면 후속 실행과 동일하게 정확한 별도 Telegram 승인이
필요합니다.

준비 단계에서 허용되는 로컬 변경은 runtime 상태 기록과 LLM Wiki 운영 계약에 따른 파생
Wiki 세대 준비뿐입니다. 원본 폴더와 Excel, Notion, 승인된 Wiki 오버레이는 변경되지
않습니다. 완료되면 제안이 있을 때 `[NX_SYNC_PROPOSAL_READY]`, 없을 때
`[NX_SYNC_NO_CHANGES]`를 보냅니다.

## 3. 후속 `/nx_sync`

후속 요청마다 다음 입력을 독립적으로 처리합니다.

- Excel: 마지막 성공 해시 이후의 변경
- 데이터 폴더: 마지막 성공 manifest 이후의 변경
- Hiworks: `T0` 이후이고 처리되지 않은 UIDL

온라인 전용 파일이 후속 변경으로 나타나면 자동으로 내려받지 않습니다. Telegram에서
파일별 또는 범위별 hydration을 별도로 승인한 뒤 다시 처리합니다.

메일과 데이터 자료는 Wiki와 분석 보조 정보에만 반영합니다. Excel만 Notion 사건 사실의
원본입니다.

Excel의 각 행을 별도의 Notion `근거자료` 페이지로 만들지는 않습니다. 시트·행·셀,
원본 버전과 SHA256은 각 실질 변경의 immutable `SourceRef`, 로컬 감사 로그와 Wiki
근거에 보존합니다. Notion `근거자료`에는 행 위치 같은 기술 메타데이터가 아니라
정부지원사업 증빙 등 사람이 실제로 관리할 분석 결과만 생성합니다. 0.6.5 이하에서
대기열에 저장된 `provenance_tracker` 행별 작업은 이후 동기화 준비 시 해당 analyzer와
대상 DB가 정확히 일치할 때만 종료되며, 종료 개수와 operation 집합 digest를 감사
로그에 남깁니다.

후속 실행은 마지막 성공 기준선 이후의 Excel 속성 변경에 대해서만 `변경이력` operation을
만듭니다. 현재값 조회 대상이 많으면 DB별로 묶어 읽어 왕복 횟수를 줄이지만, 매핑된 페이지
검증과 중복 제목 탐지는 생략하지 않습니다. 하나의 요청이 여러 페이지와 일치하는 등
대상이 모호하면 쓰기 제안을 추측하지 않고 실패시킵니다.

## 3.1 동기화 진행 응답과 중복 명령

0.6.0 이상 runtime에서는 인증된 `/nx_sync`가 다음 순서로 응답합니다.

1. 즉시 `[NX_SYNC_ACCEPTED]`
2. 준비가 끝난 뒤 `[NX_SYNC_PROPOSAL_READY]` 또는 `[NX_SYNC_NO_CHANGES]`

같은 Gateway에서 동기화가 이미 실행 중일 때 다시 `/nx_sync`를 보내면 새 작업을 만들지
않고 `[NX_SYNC_IN_PROGRESS]`로 현재 단계만 알려 줍니다. 이 응답은 승인이나 Notion 쓰기
권한이 아닙니다. 짧은 시간에 중복 요청이 몰리면 진행 확인은 한 건으로 합쳐져 한 번만
응답할 수 있습니다.

0.5.1 runtime은 진행 응답을 지원하지 않고 중복 명령을 조용히 무시할 수 있습니다.
0.5.1이 설치된 상태에서는 첫 요청이 끝날 때까지 `/nx_sync`를 다시 보내지 마십시오.
0.6.0 이상으로 갱신한 뒤에도 중복 실행은 하지 않으며, `[NX_SYNC_IN_PROGRESS]`를 상태
확인으로만 사용합니다.

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

검토 화면과 무관한 독립 정정은 엄격한 JSON 명령으로 요청합니다.

```text
/nx_correct {"database":"한국 특허 사건","entity_key":"SS-SYNTHETIC-001","property":"현재상태","value":"보류","reason":"합성 예시 확인","case_number":"SS-SYNTHETIC-001"}
```

이 단계는 현재 로컬 Excel 전체 해시와 현재 Notion 값을 읽어 별도 correction proposal을
만들 뿐 원격 쓰기는 하지 않습니다. 표시된 새 revision과 digest를 `/nx_approve`로 다시
승인한 뒤에만 Notion과 Wiki 보정 오버레이에 반영합니다. 독립 정정 완료는 Excel
체크포인트를 전진시키지 않습니다.

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

이 전역 preflight가 하나라도 실패하면 `STALE`로 끝내고 Notion에 아무것도 쓰지 않습니다.
모두 같을 때만 정확히 승인된 operation을 적용합니다. 같은 Notion 페이지·안전 단계에서
연속된 승인 속성들은 한 번의 페이지 update로 묶지만, 제목 생성·변경과 후속 속성은 별도
요청이 될 수 있습니다. 묶음 안의 각 operation은 제안 revision과 전체 digest에 이미
포함되어 있어야 합니다. 배치 처리는 승인 범위를 합치거나 넓히지 않습니다.

Notion은 여러 페이지를 하나의 트랜잭션으로 제공하지 않으므로 쓰기 시작 뒤 네트워크가
중단되면 일부 페이지가 이미 반영되었을 수 있습니다. Gateway는 쓰기 전에 만든 outbox와
operation ID를 이용해 이미 성공한 작업을 판별하고, 동일 영수증의 정확한 나머지 범위만
idempotent하게 복구합니다. 사용자가 편집한 값은 Notion 성공 확인 후 Wiki 승인
오버레이에도 반영합니다.

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
- `APPLYING` crash: 같은 프로세스에서는 동일한 승인 범위와 outbox만 idempotent
  reconciliation합니다. Gateway 재시작으로 승인 비밀이 회전했다면
  `/nx_recover <proposal-id> <revision>`을 보냅니다. 살아 있는 apply worker가 없고
  저장된 영수증·사용된 nonce·outbox가 정확히 일치할 때만 Notion 재쓰기 없이 복구
  revision을 만들며, 적용에는 다시 별도 승인이 필요합니다.
- 오래 걸리는 `/nx_sync`: 0.6.0 이상에서는 첫 `[NX_SYNC_ACCEPTED]` 뒤 최종
  `[NX_SYNC_PROPOSAL_READY]` 또는 `[NX_SYNC_NO_CHANGES]`를 기다립니다. 중간에 다시
  보내면 `[NX_SYNC_IN_PROGRESS]`만 반환하며 새 실행을 만들지 않습니다.
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

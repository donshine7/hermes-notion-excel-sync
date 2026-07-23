# 설정 가이드

실제 운영값은 Git에서 제외된 `config\sync.local.json`에만 저장합니다. 공개 예제에는
실제 사용자 경로, 계정, Telegram ID, Notion ID 또는 비밀값을 넣지 마십시오.

## 원본 설정

운영 모드는 로컬 파일시스템입니다.

| 키 | 의미 |
|---|---|
| `source.mode` | `local_filesystem` |
| `source.root_dir` | 노트북의 읽기 전용 데이터 원본 루트 |
| `source.excel_path` | 원본 루트 내부의 업무관리 Excel |
| `source.immutable` | 반드시 `true` |
| `source.snapshot_dir` | 원본 밖의 보호된 런타임 캡처 폴더 |
| `source.initial_auto_hydrate` | 최초 전체 구축에서만 온라인 전용 파일 자동 읽기 허용 |
| `source.steady_state_auto_hydrate` | 반드시 `false` |

경로 예시는 다음과 같이 자리표시자로만 기록합니다.

```json
{
  "source": {
    "mode": "local_filesystem",
    "root_dir": "<LOCAL_SOURCE_ROOT>",
    "excel_path": "<LOCAL_SOURCE_ROOT>\\<WORKBOOK_RELATIVE_PATH>",
    "immutable": true,
    "initial_auto_hydrate": true,
    "steady_state_auto_hydrate": false,
    "snapshot_dir": "../data/work/local-source-snapshots"
  }
}
```

검증기는 다음 경우 중단해야 합니다.

- 원본 루트 또는 Excel이 존재하지 않음
- Excel이 원본 루트 밖에 있음
- snapshot이나 Wiki 출력이 원본과 겹침
- `immutable`이 false
- 후속 자동 hydration이 true

프로그램은 읽기 전후 크기·수정시각·해시를 확인해 캡처 중 변경도 감지합니다.

## Excel 설정

| 키 | 의미 |
|---|---|
| `excel.sheets` | 동기화할 탭 이름 목록 |
| `excel.header_row` | 열 이름이 있는 1부터 시작하는 행 번호 |
| `excel.key_columns` | 사건을 식별할 안정적인 열 후보 |
| `excel.key_groups` | 탭별 복합 안정 키 후보 |

행 번호 자체는 식별자로 사용하지 않습니다. 정렬 변경은 안정 키와 정규화된 값이 같으면
변경이 아닙니다. 삭제처럼 보이는 행은 자동 삭제하지 않고 검토 후보로 만듭니다.

기존 `initial_cutoff` 값은 로컬 운영의 최초 기준으로 사용하지 않습니다. 최초 기준 `T0`는
인증된 첫 `/nx_sync` 명령 수신 시각이며, 그때의 현재 Excel 전체가 최초 상태입니다.

## Wiki 설정

| 키 | 의미 |
|---|---|
| `wiki.enabled` | Wiki projection/index 사용 여부 |
| `wiki.source_root` | 읽기 전용 데이터 원본 루트 |
| `wiki.output_root` | 원본과 겹치지 않는 Wiki 출력 폴더 |
| `wiki.index_db` | Git과 원본 밖의 보호된 검색 인덱스 |
| `wiki.include_roots` / `exclude_roots` | 수집 허용·차단 최상위 폴더 |
| `wiki.allowed_extensions` | 허용 문서 형식 |
| `wiki.max_*` | 문서·바이트·텍스트·chunk 안전 한도 |
| `wiki.rollout_mode` | 초기에는 `shadow` |
| `wiki.initial_auto_hydrate` | 최초 전체 구축에 한해 허용 |
| `wiki.steady_state_auto_hydrate` | 반드시 `false` |

`wiki.output_root`와 `wiki.index_db`는 원본 루트 내부에 두지 않습니다. 원본 안에 manifest,
lock, 캐시 또는 임시 파일을 생성하지 않습니다.

## Hiworks 메일 설정

메일은 초기 Wiki가 성공한 뒤부터 보조 자료로 사용합니다.

| 키 | 의미 |
|---|---|
| `email.enabled` | 증분 메일 수집 사용 여부 |
| `email.reference_project` | 기존 Hiworks 연동 구현을 참고할 로컬 프로젝트 |
| `email.host`, `email.port`, `email.use_ssl` | POP3S 연결 |
| `email.username` | 운영 계정, 로컬 설정에만 기록 |
| `email.password_secret` | 비밀번호 값이 아니라 보호된 비밀 이름 |
| `email.header_first` | 헤더를 먼저 확인한 뒤 필요한 메시지만 수집 |
| `email.max_*_bytes` | 메시지·본문·첨부 안전 한도 |

UIDL과 Message-ID를 사용해 중복을 막습니다. `T0`보다 이전 메일은 헤더 확인 후 제외하며,
첨부는 메타데이터와 해시만 기본 저장하고 원문 payload를 Wiki에 복제하지 않습니다.

## Notion 설정

`notion.databases`에는 페이지 링크가 아니라 검증된 database/data source ID를 넣습니다.
별도 스키마 생성 명령을 사용할 때는 허용된 상위 페이지 ID를
`notion.schema_parent_page_id`에 로컬로 설정합니다.
상위 페이지는 `<NOTION_PARENT_PAGE_ID>`로 일반화해 문서화하고, 모든 DB ID로 재사용하지
않습니다.

쓰기 전에 속성 유형, select option, relation 대상과 현재값을 다시 검증합니다. 읽기 토큰과
쓰기 토큰을 분리하며, 쓰기 토큰은 Gateway 보호 환경에만 둡니다.

필요한 논리 대상 예:

- 한국 특허 사건
- 그룹
- 당사자
- 업무·절차
- 기일
- 비용·청구
- 정부지원사업 증빙
- 등록결정 후속관리
- 연락이력
- 근거자료
- 검토함
- 변경이력

## Telegram 승인 설정

| 키 | 의미 |
|---|---|
| `approval.allowed_telegram_users` | 승인 가능한 숫자 사용자 ID 문자열 목록 |
| `approval.ttl_seconds` | 미사용 승인 영수증 만료 시간 |
| `approval.secret_env` | Gateway에만 주입할 서명 비밀 이름 |

실제 ID나 비밀을 저장소에 기록하지 않습니다. Telegram의 원본 message/update, 사용자,
chat/thread, proposal revision과 digest를 함께 묶어 검증합니다.

## 초기 검증

```powershell
.\.venv\Scripts\python -m notion_excel_sync.cli init-db `
  --config .\config\sync.local.json

.\.venv\Scripts\python -m notion_excel_sync.cli validate `
  --config .\config\sync.local.json
```

검증은 원본과 Notion을 읽기만 해야 합니다. 원본 캡처 경로, Wiki 출력 경계, 탭·헤더,
Notion schema, Telegram allowlist와 비밀 이름을 확인한 뒤 운영하십시오.

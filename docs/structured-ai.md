# 구조화 AI 운영 계약

## 목적

구조화 AI는 기존 규칙 분석기를 대체하지 않습니다. 복합 당사자명, 이메일의 사건 연결,
정부지원사업 증빙 설명, 모호한 사건 히스토리처럼 규칙만으로 확정하기 어려운 변경을
선별해 사람이 검토할 후보로 정리합니다.

Excel은 계속 사건 사실의 권위 있는 원본입니다. 이메일과 Wiki는 보조 근거이며, AI 출력은
어떤 rollout mode에서도 승인 자체가 아닙니다.

## 실행 순서

1. 읽기 전용 어댑터가 원본을 캡처하고 SHA-256을 계산합니다.
2. 기존 분석기가 날짜·금액·사건번호·상태를 규칙과 수식으로 분석합니다.
3. 모호한 변경만 우선순위로 정렬하고 호출 한도까지 AI에 전달합니다.
4. 모델은 하나의 JSON 객체만 반환합니다.
5. 프로그램이 JSON 필드, 원본 해시, 허용 enum, 신뢰도와 인용문을 검증합니다.
6. 실패하거나 근거가 없는 결과는 폐기합니다.
7. 유효한 결과는 rollout mode에 따라 shadow 통계, 검토함 후보 또는 허용 필드의
   변경 후보가 됩니다.
8. Notion 변경은 기존의 정확한 Telegram revision·digest 승인 뒤에만 실행됩니다.

## 구조화 출력

모든 출력은 다음 항목을 가집니다.

- `schema_version`
- `task_type`
- `source_hash`
- `summary`
- `claims[]`
- `conflicts[]`
- `needs_review`
- `overall_confidence`

각 claim에는 `field`, `value`, `confidence`, `inference_type`과 하나 이상의
`evidence`가 필요합니다. evidence의 `quote`가 모델에 제공한 원본 텍스트에 실제로
존재하지 않으면 전체 분석을 채택하지 않습니다.

모델이 작업별 허용 목록 밖의 claim을 추가하면 해당 claim만 폐기하고
`needs_review=true`로 강제합니다. 완전히 동일한 중복 claim도 하나로 줄이고 사람 검토를
강제합니다. 값이 다른 중복 claim, 잘못된 인용, 허용값 위반은 전체 분석을 폐기합니다.

허용 작업은 다음과 같습니다.

- `party_resolution`
- `email_case_event`
- `government_support_evidence`
- `case_history_summary`
- `excel_row_semantics`

날짜는 ISO 형식이어야 하고 원본에 명시되어야 합니다. 사건번호는 원본 또는 코드가
확인한 사건번호 집합에 있어야 합니다. 당사자 유형은 개인, 법인, 기관,
정부지원기관, 기타 중 하나만 허용합니다.

## Rollout mode

### shadow

모델 결과를 로컬 캐시와 집계 감사에만 사용합니다. Notion 제안과 Telegram 검토 항목을
바꾸지 않습니다.

### assist

신뢰도 기준을 통과한 결과를 `AI 구조화 분석` 검토 항목으로 만듭니다. 기존 규칙 값을
자동 교체하지 않습니다.

### verified

원본 인용, 해시, JSON schema, task별 허용 필드와 높은 신뢰도 기준을 모두 통과한
후보만 사용할 수 있습니다. 현재 직접 보강이 허용된 필드는 기존 규칙이 `기타`로 둔
단일 당사자의 `구분`뿐입니다. 당사자명 자체, 날짜, 금액과 사건번호는 AI가 직접
교체하지 않습니다.

## 실패와 캐시

모델 provider, timeout, JSON 또는 근거 검증이 한 번 실패하면 같은 동기화의 AI
circuit을 열어 추가 호출을 막습니다. 규칙 분석은 계속되며 Notion 승인 경계는
달라지지 않습니다.

성공 출력은 다음 바인딩으로 로컬 SQLite에 저장합니다.

- task type
- 원본 hash
- prompt version
- 정확한 요청 digest
- provider와 model
- 결과 digest

원본 파일이나 전체 프롬프트는 캐시에 저장하지 않습니다. 동일 입력을 다시 분석하면
캐시를 사용해 비용과 지연을 줄입니다.

## 개인정보와 권한

- 기본 `privacy_mode`는 `local_only`입니다.
- local-only는 loopback endpoint가 아니면 모델 호출을 거부합니다.
- 외부 모델은 로컬 설정에서 `redacted_remote`를 명시한 경우에만 사용할 수 있습니다.
- 외부 호출에는 변경 행과 필요한 Wiki 발췌만 제한적으로 포함합니다.
- source 문서의 명령문은 데이터로만 취급합니다.
- 모델 호출에는 파일·셸·네트워크·Notion 같은 실행 도구를 제공하지 않습니다.
  허용되는 유일한 function schema는 구조화 결과를 반환하는 `submit` 형식이며 실행
  권한이 없습니다.
- 모델은 Notion 쓰기 토큰, 승인 서명 비밀 또는 Hiworks 비밀번호를 받지 않습니다.
- AI 캐시, 출력, 운영 모델 정보는 공개 Git 저장소에 포함하지 않습니다.

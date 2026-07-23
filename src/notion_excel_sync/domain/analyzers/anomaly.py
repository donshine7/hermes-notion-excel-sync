from __future__ import annotations

from notion_excel_sync.domain.analyzer import AnalysisContext, AnalyzerManifest, DomainAnalyzer
from notion_excel_sync.domain.analyzers.common import normalize_header, parse_date, parse_money
from notion_excel_sync.models import ChangeKind


class AnomalyAnalyzer(DomainAnalyzer):
    manifest = AnalyzerManifest(
        name="anomaly",
        version="1.0.0",
        description="Finds deterministic cross-field anomalies without silently correcting them.",
        dependencies=("case_identity",),
        outputs=("anomalies",),
        target_databases=(),
        knowledge_topics=("data-quality",),
        priority=100,
        always_run=True,
    )

    _MONEY_TOKENS = ("금액", "비용", "공급가", "부가세", "관납료", "청구액", "입금액", "미납액")

    def analyze(self, context: AnalysisContext):
        result = self.empty_result()
        anomalies: list[dict[str, object]] = []
        if context.change.kind is ChangeKind.DELETE_CANDIDATE:
            result.review_items.append(
                self.review_item(
                    context,
                    review_type="삭제 후보",
                    title="Excel 원본 행 삭제 후보",
                    reason="Excel 행 삭제는 Notion 삭제로 자동 변환하지 않으며 개별 확인이 필요합니다.",
                    current_value=context.record.values,
                    confidence=1.0,
                )
            )
            anomalies.append({"type": "source_row_deleted", "key": context.change.key})

        dates: dict[str, str] = {}
        for header, value in context.record.values.items():
            normalized = normalize_header(header)
            if isinstance(value, str) and value.strip().startswith("#"):
                result.review_items.append(
                    self.review_item(
                        context,
                        review_type="Excel 오류",
                        title=f"{header} 수식 오류",
                        reason="Excel 오류값은 자동 변환하지 않습니다.",
                        current_value=value,
                        confidence=1.0,
                    )
                )
                anomalies.append({"type": "excel_error", "header": header, "value": value})
            if any(token in normalized for token in self._MONEY_TOKENS):
                amount = parse_money(value)
                if amount is not None and amount < 0 and not any(
                    token in normalized for token in ("할인", "환불", "차감")
                ):
                    result.review_items.append(
                        self.review_item(
                            context,
                            review_type="금액 이상",
                            title=f"{header} 음수 금액",
                            reason="할인·환불·차감이 아닌 비용 필드에 음수 값이 있습니다.",
                            current_value=amount,
                            confidence=0.98,
                        )
                    )
                    anomalies.append(
                        {
                            "type": "unexpected_negative",
                            "header": header,
                            "value": amount,
                        }
                    )
            if any(token in normalized for token in ("일", "기일", "기한", "마감")):
                parsed = parse_date(value)
                if parsed:
                    dates[normalized] = parsed

        start = next(
            (value for key, value in dates.items() if "접수일" in key or "의뢰일" in key),
            None,
        )
        completed = next(
            (value for key, value in dates.items() if "완료일" in key or "종결일" in key),
            None,
        )
        if start and completed and completed < start:
            # Workflow analyzer may already have emitted this exact issue. Keep one per run.
            already_reported = any(
                item.title == "완료일이 접수일보다 빠름"
                for prior in context.prior_results.values()
                for item in prior.review_items
            )
            if not already_reported:
                result.review_items.append(
                    self.review_item(
                        context,
                        review_type="날짜 이상",
                        title="완료일이 접수일보다 빠름",
                        reason="원본 날짜의 선후관계가 맞지 않습니다.",
                        current_value={"시작": start, "완료": completed},
                        confidence=0.99,
                    )
                )
            anomalies.append({"type": "date_order", "start": start, "completed": completed})

        result.derived.update(
            {
                "anomaly_count": len(anomalies),
                "anomalies": anomalies,
                "upstream_warning_count": sum(
                    len(prior.warnings) + len(prior.review_items)
                    for prior in context.prior_results.values()
                ),
            }
        )
        return result

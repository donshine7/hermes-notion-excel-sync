from __future__ import annotations

from dataclasses import dataclass

from notion_excel_sync.config import WikiConfig
from notion_excel_sync.knowledge.index import WikiIndex
from notion_excel_sync.knowledge.models import (
    KnowledgeBundle,
    KnowledgeHit,
    KnowledgeQuery,
)
from notion_excel_sync.knowledge.policy import categories_for_topics


@dataclass(slots=True)
class WikiRetriever:
    index: WikiIndex
    config: WikiConfig

    def query(
        self,
        text: str,
        *,
        topics: tuple[str, ...] = (),
        top_k: int | None = None,
        verified_only: bool = False,
        effective_on: str | None = None,
    ) -> tuple[KnowledgeHit, ...]:
        categories = categories_for_topics(topics)
        statuses = ("verified",) if verified_only else ("unverified", "verified")
        return self.index.query(
            KnowledgeQuery(
                text=text,
                topics=topics,
                categories=categories,
                statuses=statuses,
                effective_on=effective_on,
                top_k=top_k or self.config.retrieval_top_k,
            )
        )

    def bundle(
        self,
        text: str,
        *,
        topics: tuple[str, ...] = (),
        verified_only: bool = False,
        effective_on: str | None = None,
    ) -> KnowledgeBundle:
        snapshot = self.index.status()
        hits = self.query(
            text,
            topics=topics,
            verified_only=verified_only,
            effective_on=effective_on,
        )
        return KnowledgeBundle(
            generation_digest=snapshot.generation_digest,
            refs=tuple(hit.ref for hit in hits),
            metadata={
                "rollout_mode": self.config.rollout_mode,
                "topics": ",".join(topics),
            },
        )


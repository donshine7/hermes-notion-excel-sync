from __future__ import annotations

import copy
import re
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from notion_excel_sync.workflow.notion_schema import (
    FIXED_PROPERTIES,
    FIXED_SELECT_OPTIONS,
    REMOTE_MARKER_PREFIX,
    SCHEMA_API_VERSION,
    SCHEMA_LOGICAL_NAME,
    SCHEMA_PARENT_PAGE_ID,
    SCHEMA_PURPOSE,
    SCHEMA_TEMPLATE_ID,
    CreatedSchemaVerificationError,
    NotionSchemaSecurityError,
    ReconciliationDisposition,
    RemoteSchemaCandidate,
    SchemaBindingError,
    SchemaTemplateError,
    assert_schema_live_binding_matches,
    build_schema_create_plan,
    capture_schema_live_binding,
    decide_schema_reconciliation,
    normalize_notion_id,
    verify_created_schema,
)
from notion_excel_sync.workflow.notion_writer import (
    NOTION_DEFINITIONS,
    DatabaseDefinition,
)


BOT_ID = "11111111111111111111111111111111"
CASE_SOURCE_ID = "22222222222222222222222222222222"
GROUP_SOURCE_ID = "33333333333333333333333333333333"
COST_SOURCE_ID = "44444444444444444444444444444444"
EVIDENCE_SOURCE_ID = "55555555555555555555555555555555"
CASE_DATABASE_ID = "66666666666666666666666666666666"
GROUP_DATABASE_ID = "77777777777777777777777777777777"
COST_DATABASE_ID = "88888888888888888888888888888888"
EVIDENCE_DATABASE_ID = "99999999999999999999999999999999"
CREATED_DATABASE_ID = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
CREATED_DATA_SOURCE_ID = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
OTHER_ID = "cccccccccccccccccccccccccccccccc"

STARTED = datetime(2026, 7, 22, 3, 0, 0, tzinfo=UTC)
CREATED = STARTED + timedelta(seconds=1)
OBSERVED = STARTED + timedelta(seconds=2)


def rich_text(value: str) -> list[dict[str, object]]:
    return [
        {
            "type": "text",
            "text": {"content": value},
            "plain_text": value,
        }
    ]


def parent_page(*, page_id: str = SCHEMA_PARENT_PAGE_ID) -> dict[str, object]:
    return {
        "object": "page",
        "id": page_id,
        "in_trash": False,
    }


def write_bot(*, bot_id: str = BOT_ID) -> dict[str, object]:
    return {
        "object": "user",
        "id": bot_id,
        "type": "bot",
        "bot": {"owner": {"type": "workspace", "workspace": True}},
    }


def relation_ids() -> dict[str, str]:
    return {
        "한국 특허 사건": CASE_SOURCE_ID,
        "그룹": GROUP_SOURCE_ID,
        "비용·청구": COST_SOURCE_ID,
        "근거자료": EVIDENCE_SOURCE_ID,
    }


def relation_source(
    name: str,
    source_id: str,
    database_id: str,
) -> dict[str, object]:
    return {
        "object": "data_source",
        "id": source_id,
        "name": name,
        "in_trash": False,
        "parent": {"type": "database_id", "database_id": database_id},
    }


def relation_sources() -> dict[str, dict[str, object]]:
    return {
        "한국 특허 사건": relation_source(
            "한국 특허 사건", CASE_SOURCE_ID, CASE_DATABASE_ID
        ),
        "그룹": relation_source("그룹", GROUP_SOURCE_ID, GROUP_DATABASE_ID),
        "비용·청구": relation_source(
            "비용·청구", COST_SOURCE_ID, COST_DATABASE_ID
        ),
        "근거자료": relation_source(
            "근거자료", EVIDENCE_SOURCE_ID, EVIDENCE_DATABASE_ID
        ),
    }


def test_relation_source_name_must_match_the_allowlisted_logical_target() -> None:
    sources = relation_sources()
    sources["그룹"] = relation_source(
        "비용·청구",
        GROUP_SOURCE_ID,
        GROUP_DATABASE_ID,
    )
    with unittest.TestCase().assertRaises(SchemaBindingError):
        capture_schema_live_binding(
            parent_page=parent_page(),
            write_bot=write_bot(),
            direct_child_databases=[],
            relation_data_source_ids=relation_ids(),
            relation_data_sources=sources,
            expected_write_bot_id=BOT_ID,
        )


def make_plan():
    with patch(
        "notion_excel_sync.workflow.notion_schema.secrets.token_hex",
        return_value="ab" * 16,
    ):
        return build_schema_create_plan(
            SCHEMA_TEMPLATE_ID,
            parent_page=parent_page(),
            write_bot=write_bot(),
            direct_child_databases=[],
            relation_data_source_ids=relation_ids(),
            relation_data_sources=relation_sources(),
            expected_write_bot_id=BOT_ID,
        )


def remote_schema(plan) -> dict[str, dict[str, object]]:
    relation_by_property = {
        item.property_name: item.data_source_id for item in plan.relation_targets
    }
    properties: dict[str, dict[str, object]] = {}
    for index, (name, property_type) in enumerate(FIXED_PROPERTIES):
        if property_type == "select":
            config: dict[str, object] = {
                "options": [
                    {"name": name, "color": color}
                    for name, color in FIXED_SELECT_OPTIONS[name]
                ]
            }
        elif property_type == "number":
            config = {"format": "number" if name == "인정 건수" else "won"}
        elif property_type == "relation":
            config = {
                "data_source_id": relation_by_property[name],
                "type": "single_property",
                "single_property": {},
            }
        else:
            config = {}
        properties[name] = {
            "id": f"property-{index}",
            "name": name,
            "type": property_type,
            property_type: config,
        }
    return properties


def created_pair(plan, *, database_id: str = CREATED_DATABASE_ID):
    database = {
        "object": "database",
        "id": database_id,
        "parent": {"type": "page_id", "page_id": SCHEMA_PARENT_PAGE_ID},
        "title": rich_text(SCHEMA_LOGICAL_NAME),
        "description": rich_text(plan.remote_marker),
        "is_inline": False,
        "in_trash": False,
        "created_by": {"object": "user", "id": BOT_ID},
        "created_time": CREATED.isoformat().replace("+00:00", "Z"),
        "data_sources": [{"id": CREATED_DATA_SOURCE_ID, "name": SCHEMA_LOGICAL_NAME}],
    }
    data_source = {
        "object": "data_source",
        "id": CREATED_DATA_SOURCE_ID,
        "name": SCHEMA_LOGICAL_NAME,
        "parent": {"type": "database_id", "database_id": database_id},
        "in_trash": False,
        "created_by": {"object": "user", "id": BOT_ID},
        "created_time": CREATED.isoformat().replace("+00:00", "Z"),
        "properties": remote_schema(plan),
    }
    return database, data_source


def live_binding(*, children=(), sources=None, bot=None):
    return capture_schema_live_binding(
        parent_page=parent_page(),
        write_bot=bot or write_bot(),
        direct_child_databases=list(children),
        relation_data_source_ids=relation_ids(),
        relation_data_sources=sources or relation_sources(),
        expected_write_bot_id=None,
    )


class NotionSchemaPlanTest(unittest.TestCase):
    def test_allowlisted_plan_has_only_the_fixed_canonical_body(self) -> None:
        plan = make_plan()
        body = plan.create_body

        self.assertEqual(plan.purpose, SCHEMA_PURPOSE)
        self.assertEqual(plan.api_version, SCHEMA_API_VERSION)
        self.assertEqual(plan.parent_page_id, SCHEMA_PARENT_PAGE_ID)
        self.assertEqual(plan.logical_name, SCHEMA_LOGICAL_NAME)
        self.assertRegex(
            plan.remote_marker,
            rf"^{re.escape(REMOTE_MARKER_PREFIX)}[0-9a-f]{{32}}$",
        )
        self.assertEqual(
            set(body),
            {"parent", "title", "description", "is_inline", "initial_data_source"},
        )
        self.assertEqual(
            body["parent"],
            {"type": "page_id", "page_id": SCHEMA_PARENT_PAGE_ID},
        )
        self.assertEqual(body["title"][0]["text"]["content"], SCHEMA_LOGICAL_NAME)
        self.assertEqual(
            body["description"][0]["text"]["content"], plan.remote_marker
        )
        properties = body["initial_data_source"]["properties"]
        self.assertEqual(len(properties), 18)
        self.assertEqual(
            [(name, next(iter(properties[name]))) for name, _ in FIXED_PROPERTIES],
            list(FIXED_PROPERTIES),
        )
        for name in ("증빙유형", "부가세 처리", "증빙상태"):
            self.assertEqual(
                properties[name],
                {
                    "select": {
                        "options": [
                            {"name": option, "color": color}
                            for option, color in FIXED_SELECT_OPTIONS[name]
                        ]
                    }
                },
            )
        self.assertFalse(body["is_inline"])
        self.assertEqual(properties["인정 건수"], {"number": {"format": "number"}})
        for name in (
            "실제 비용 합계",
            "인정대상금액",
            "정부지원금",
            "자부담",
            "부족액",
            "초과액",
        ):
            self.assertEqual(properties[name], {"number": {"format": "won"}})
        for item in plan.relation_targets:
            self.assertEqual(
                properties[item.property_name],
                {
                    "relation": {
                        "data_source_id": item.data_source_id,
                        "single_property": {},
                    }
                },
            )

        # Each access returns a fresh body; mutating a caller-owned copy cannot
        # broaden the signed plan.
        body["initial_data_source"]["properties"]["임의 속성"] = {"title": {}}
        self.assertNotIn(
            "임의 속성", plan.create_body["initial_data_source"]["properties"]
        )

    def test_canonical_digest_is_order_independent_but_binds_marker(self) -> None:
        ids = relation_ids()
        reversed_ids = dict(reversed(tuple(ids.items())))
        with patch(
            "notion_excel_sync.workflow.notion_schema.secrets.token_hex",
            return_value="cd" * 16,
        ):
            first = build_schema_create_plan(
                SCHEMA_TEMPLATE_ID,
                parent_page=parent_page(),
                write_bot=write_bot(),
                direct_child_databases=[],
                relation_data_source_ids=ids,
                relation_data_sources=relation_sources(),
            )
            second = build_schema_create_plan(
                SCHEMA_TEMPLATE_ID,
                parent_page=parent_page(),
                write_bot=write_bot(),
                direct_child_databases=[],
                relation_data_source_ids=reversed_ids,
                relation_data_sources=dict(reversed(tuple(relation_sources().items()))),
            )
        self.assertEqual(first.digest, second.digest)

        with patch(
            "notion_excel_sync.workflow.notion_schema.secrets.token_hex",
            return_value="ef" * 16,
        ):
            different_marker = build_schema_create_plan(
                SCHEMA_TEMPLATE_ID,
                parent_page=parent_page(),
                write_bot=write_bot(),
                direct_child_databases=[],
                relation_data_source_ids=ids,
                relation_data_sources=relation_sources(),
            )
        self.assertNotEqual(first.digest, different_marker.digest)

    def test_notion_ids_are_canonicalized(self) -> None:
        hyphenated_parent = (
            f"{SCHEMA_PARENT_PAGE_ID[:8]}-{SCHEMA_PARENT_PAGE_ID[8:12]}-"
            f"{SCHEMA_PARENT_PAGE_ID[12:16]}-{SCHEMA_PARENT_PAGE_ID[16:20]}-"
            f"{SCHEMA_PARENT_PAGE_ID[20:]}"
        )
        self.assertEqual(
            normalize_notion_id(hyphenated_parent),
            SCHEMA_PARENT_PAGE_ID,
        )
        with self.assertRaises(SchemaBindingError):
            normalize_notion_id("not-a-notion-id")

    def test_plan_uses_and_verifies_the_explicit_expected_parent(self) -> None:
        configured_parent = "e" * 32
        plan = build_schema_create_plan(
            SCHEMA_TEMPLATE_ID,
            parent_page=parent_page(page_id=configured_parent),
            write_bot=write_bot(),
            direct_child_databases=[],
            relation_data_source_ids=relation_ids(),
            relation_data_sources=relation_sources(),
            expected_parent_page_id=configured_parent,
        )
        self.assertEqual(plan.parent_page_id, configured_parent)

        with self.assertRaises(SchemaBindingError):
            build_schema_create_plan(
                SCHEMA_TEMPLATE_ID,
                parent_page=parent_page(),
                write_bot=write_bot(),
                direct_child_databases=[],
                relation_data_source_ids=relation_ids(),
                relation_data_sources=relation_sources(),
                expected_parent_page_id=configured_parent,
            )

    def test_unknown_template_api_relations_and_arbitrary_body_are_rejected(self) -> None:
        common = {
            "parent_page": parent_page(),
            "write_bot": write_bot(),
            "direct_child_databases": [],
            "relation_data_source_ids": relation_ids(),
            "relation_data_sources": relation_sources(),
        }
        with self.assertRaises(SchemaTemplateError):
            build_schema_create_plan("arbitrary-template", **common)
        with self.assertRaises(SchemaTemplateError):
            build_schema_create_plan(
                SCHEMA_TEMPLATE_ID, api_version="2099-01-01", **common
            )
        extra_ids = relation_ids()
        extra_ids["임의 대상"] = OTHER_ID
        with self.assertRaises(SchemaTemplateError):
            build_schema_create_plan(
                SCHEMA_TEMPLATE_ID,
                **{**common, "relation_data_source_ids": extra_ids},
            )
        with self.assertRaises(TypeError):
            build_schema_create_plan(  # type: ignore[call-arg]
                SCHEMA_TEMPLATE_ID,
                arbitrary_body={"properties": {"임의": {"formula": {}}}},
                **common,
            )

        plan = make_plan()
        with self.assertRaises(SchemaTemplateError):
            replace(plan, logical_name="임의 데이터베이스")
        with self.assertRaises(SchemaTemplateError):
            replace(plan, purpose="data_apply/v1")

    def test_writer_definition_drift_cannot_expand_the_creation_allowlist(self) -> None:
        original = NOTION_DEFINITIONS[SCHEMA_LOGICAL_NAME]
        broadened = DatabaseDefinition(
            title_property=original.title_property,
            properties={**dict(original.properties), "임의 수식": "formula"},
            relations=dict(original.relations),
        )
        with patch.dict(
            NOTION_DEFINITIONS,
            {SCHEMA_LOGICAL_NAME: broadened},
        ):
            with self.assertRaisesRegex(SchemaTemplateError, "count"):
                make_plan()

    def test_parent_bot_relation_and_direct_child_bindings_fail_closed(self) -> None:
        plan = make_plan()
        current = live_binding()
        assert_schema_live_binding_matches(plan.approved_live_binding, current)

        with self.assertRaises(SchemaBindingError):
            capture_schema_live_binding(
                parent_page={**parent_page(), "archived": True},
                write_bot=write_bot(),
                direct_child_databases=[],
                relation_data_source_ids=relation_ids(),
                relation_data_sources=relation_sources(),
            )
        with self.assertRaises(SchemaBindingError):
            capture_schema_live_binding(
                parent_page=parent_page(),
                write_bot=write_bot(bot_id=OTHER_ID),
                direct_child_databases=[],
                relation_data_source_ids=relation_ids(),
                relation_data_sources=relation_sources(),
                expected_write_bot_id=BOT_ID,
            )
        drifted_sources = relation_sources()
        drifted_sources["그룹"] = relation_source(
            "그룹", OTHER_ID, GROUP_DATABASE_ID
        )
        with self.assertRaises(SchemaBindingError):
            capture_schema_live_binding(
                parent_page=parent_page(),
                write_bot=write_bot(),
                direct_child_databases=[],
                relation_data_source_ids=relation_ids(),
                relation_data_sources=drifted_sources,
            )

        database, _ = created_pair(plan)
        with self.assertRaisesRegex(SchemaBindingError, "already exists"):
            with patch(
                "notion_excel_sync.workflow.notion_schema.secrets.token_hex",
                return_value="ab" * 16,
            ):
                build_schema_create_plan(
                    SCHEMA_TEMPLATE_ID,
                    parent_page=parent_page(),
                    write_bot=write_bot(),
                    direct_child_databases=[database],
                    relation_data_source_ids=relation_ids(),
                    relation_data_sources=relation_sources(),
                )

        changed_name = relation_sources()
        changed_name["그룹"] = relation_source(
            "다른 표시명", GROUP_SOURCE_ID, GROUP_DATABASE_ID
        )
        with self.assertRaises(SchemaBindingError):
            assert_schema_live_binding_matches(
                plan.approved_live_binding,
                live_binding(sources=changed_name),
            )


class CreatedSchemaVerificationTest(unittest.TestCase):
    def test_created_database_and_data_source_are_verified_exactly(self) -> None:
        plan = make_plan()
        database, data_source = created_pair(plan)

        binding = verify_created_schema(
            plan,
            database,
            data_source,
            attempt_started_at=STARTED,
            observed_at=OBSERVED,
        )

        self.assertEqual(binding.database_id, CREATED_DATABASE_ID)
        self.assertEqual(binding.data_source_id, CREATED_DATA_SOURCE_ID)
        self.assertEqual(binding.parent_page_id, SCHEMA_PARENT_PAGE_ID)
        self.assertEqual(binding.marker, plan.remote_marker)
        self.assertEqual(binding.database_creator_id, BOT_ID)
        self.assertEqual(binding.data_source_creator_id, BOT_ID)
        self.assertEqual(binding.database_creator_evidence, "direct")
        self.assertEqual(binding.data_source_time_precision, "exact")
        self.assertEqual(binding.database_created_time, CREATED.isoformat().replace("+00:00", "Z"))
        self.assertEqual(len(binding.relation_targets), 4)
        self.assertRegex(binding.schema_digest, r"^[0-9a-f]{64}$")

        # A journaled binding is compared as one exact digest, including both
        # created_time values and both remote IDs.
        verify_created_schema(
            plan,
            database,
            data_source,
            attempt_started_at=STARTED,
            observed_at=OBSERVED,
            expected_binding=binding,
        )
        with self.assertRaises(CreatedSchemaVerificationError):
            verify_created_schema(
                plan,
                database,
                data_source,
                attempt_started_at=STARTED,
                observed_at=OBSERVED,
                expected_binding=replace(
                    binding,
                    database_created_time=(CREATED + timedelta(seconds=1))
                    .isoformat()
                    .replace("+00:00", "Z"),
                ),
            )

    def test_capability_projection_is_accepted_only_with_exact_journal_identity(self) -> None:
        plan = make_plan()
        database, data_source = created_pair(plan)
        attempt_start = datetime(2026, 7, 22, 3, 0, 25, tzinfo=UTC)
        database_time = attempt_start + timedelta(seconds=10)
        observation = attempt_start + timedelta(seconds=16)
        database.pop("created_by")
        database["created_time"] = database_time.isoformat().replace("+00:00", "Z")
        data_source["created_time"] = (
            database_time.replace(second=0, microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )

        with self.assertRaises(CreatedSchemaVerificationError):
            verify_created_schema(
                plan,
                database,
                data_source,
                attempt_started_at=attempt_start,
                observed_at=observation,
            )

        binding = verify_created_schema(
            plan,
            database,
            data_source,
            attempt_started_at=attempt_start,
            observed_at=observation,
            journal_remote_ids=(CREATED_DATABASE_ID, CREATED_DATA_SOURCE_ID),
        )
        self.assertIsNone(binding.database_creator_id)
        self.assertEqual(
            binding.database_creator_evidence,
            "data_source_and_journal",
        )
        self.assertEqual(binding.data_source_creator_id, BOT_ID)
        self.assertEqual(binding.data_source_time_precision, "minute_floor")

    def test_capability_projection_fallback_remains_fail_closed(self) -> None:
        plan = make_plan()
        attempt_start = datetime(2026, 7, 22, 3, 0, 25, tzinfo=UTC)
        database_time = attempt_start + timedelta(seconds=10)
        observation = attempt_start + timedelta(seconds=16)

        def projected_pair():
            database, data_source = created_pair(plan)
            database.pop("created_by")
            database["created_time"] = database_time.isoformat().replace(
                "+00:00", "Z"
            )
            data_source["created_time"] = (
                database_time.replace(second=0, microsecond=0)
                .isoformat()
                .replace("+00:00", "Z")
            )
            return database, data_source

        cases = {}
        database, data_source = projected_pair()
        cases["mismatched_journal_database"] = (
            database,
            data_source,
            (OTHER_ID, CREATED_DATA_SOURCE_ID),
        )
        database, data_source = projected_pair()
        cases["incomplete_journal_pair"] = (
            database,
            data_source,
            (CREATED_DATABASE_ID, ""),
        )
        database, data_source = projected_pair()
        data_source.pop("created_by")
        cases["missing_data_source_creator"] = (
            database,
            data_source,
            (CREATED_DATABASE_ID, CREATED_DATA_SOURCE_ID),
        )
        database, data_source = projected_pair()
        data_source["created_time"] = (
            database_time.replace(second=0, microsecond=0)
            - timedelta(minutes=1)
        ).isoformat().replace("+00:00", "Z")
        cases["unrelated_minute_bucket"] = (
            database,
            data_source,
            (CREATED_DATABASE_ID, CREATED_DATA_SOURCE_ID),
        )

        for label, (database, data_source, journal_ids) in cases.items():
            with self.subTest(label=label), self.assertRaises(
                NotionSchemaSecurityError
            ):
                verify_created_schema(
                    plan,
                    database,
                    data_source,
                    attempt_started_at=attempt_start,
                    observed_at=observation,
                    journal_remote_ids=journal_ids,
                )

    def test_every_material_remote_field_is_fail_closed(self) -> None:
        plan = make_plan()

        def mutate_parent(database, data_source):
            database["parent"]["page_id"] = OTHER_ID

        def mutate_marker(database, data_source):
            database["description"] = rich_text(f"{REMOTE_MARKER_PREFIX}{'0' * 32}")

        def mutate_inline(database, data_source):
            database["is_inline"] = True

        def mutate_data_source_name(database, data_source):
            data_source["name"] = "외부 데이터 소스"

        def mutate_database_creator(database, data_source):
            database["created_by"]["id"] = OTHER_ID

        def mutate_data_source_creator(database, data_source):
            data_source["created_by"]["id"] = OTHER_ID

        def mutate_database_time(database, data_source):
            database["created_time"] = (STARTED - timedelta(seconds=1)).isoformat()

        def mutate_data_source_time(database, data_source):
            data_source["created_time"] = (OBSERVED + timedelta(seconds=1)).isoformat()

        def mutate_extra_property(database, data_source):
            data_source["properties"]["임의 속성"] = {
                "id": "extra",
                "type": "rich_text",
                "rich_text": {},
            }

        def mutate_type(database, data_source):
            data_source["properties"]["인정 건수"] = {
                "id": "wrong",
                "type": "rich_text",
                "rich_text": {},
            }

        def mutate_options(database, data_source):
            data_source["properties"]["증빙유형"]["select"]["options"] = [
                {"name": "임의", "color": "red"}
            ]

        def mutate_relation_target(database, data_source):
            data_source["properties"]["대상 사건"]["relation"][
                "data_source_id"
            ] = OTHER_ID

        def mutate_relation_mode(database, data_source):
            relation = data_source["properties"]["대상 사건"]["relation"]
            relation.pop("single_property")
            relation["type"] = "dual_property"
            relation["dual_property"] = {"synced_property_id": "x"}

        def mutate_extra_data_source(database, data_source):
            database["data_sources"].append({"id": OTHER_ID})

        def mutate_data_source_parent(database, data_source):
            data_source["parent"]["database_id"] = OTHER_ID

        def mutate_title(database, data_source):
            database["title"] = rich_text("외부 데이터베이스")

        def mutate_archived(database, data_source):
            database["archived"] = True

        mutations = {
            "parent": mutate_parent,
            "marker": mutate_marker,
            "is_inline": mutate_inline,
            "data_source_name": mutate_data_source_name,
            "database_creator": mutate_database_creator,
            "data_source_creator": mutate_data_source_creator,
            "database_created_time": mutate_database_time,
            "data_source_created_time": mutate_data_source_time,
            "extra_property": mutate_extra_property,
            "property_type": mutate_type,
            "select_options": mutate_options,
            "relation_target": mutate_relation_target,
            "relation_mode": mutate_relation_mode,
            "extra_data_source": mutate_extra_data_source,
            "data_source_parent": mutate_data_source_parent,
            "title": mutate_title,
            "archived": mutate_archived,
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                database, data_source = created_pair(plan)
                mutate(database, data_source)
                with self.assertRaises(NotionSchemaSecurityError):
                    verify_created_schema(
                        plan,
                        database,
                        data_source,
                        attempt_started_at=STARTED,
                        observed_at=OBSERVED,
                    )


class SchemaReconciliationTest(unittest.TestCase):
    def test_absence_is_safe_only_before_any_create_attempt(self) -> None:
        plan = make_plan()
        current = live_binding()

        safe = decide_schema_reconciliation(
            plan,
            current_live_binding=current,
            candidates=[],
            create_was_attempted=False,
            attempt_started_at=STARTED,
            observed_at=OBSERVED,
        )
        unknown = decide_schema_reconciliation(
            plan,
            current_live_binding=current,
            candidates=[],
            create_was_attempted=True,
            attempt_started_at=STARTED,
            observed_at=OBSERVED,
        )

        self.assertIs(safe.disposition, ReconciliationDisposition.SAFE_TO_CREATE)
        self.assertIs(unknown.disposition, ReconciliationDisposition.UNKNOWN)
        self.assertEqual(
            unknown.reason_code, "attempted_create_has_no_provable_remote"
        )

    def test_exact_marker_schema_and_identity_reconcile_without_a_second_create(self) -> None:
        plan = make_plan()
        database, data_source = created_pair(plan)
        current = live_binding(children=[database])

        decision = decide_schema_reconciliation(
            plan,
            current_live_binding=current,
            candidates=[RemoteSchemaCandidate(database, data_source)],
            create_was_attempted=True,
            attempt_started_at=STARTED,
            observed_at=OBSERVED,
        )

        self.assertIs(
            decision.disposition, ReconciliationDisposition.ALREADY_CREATED
        )
        self.assertIsNotNone(decision.binding)
        assert decision.binding is not None
        self.assertEqual(decision.binding.database_id, CREATED_DATABASE_ID)

    def test_foreign_duplicate_multiple_candidates_and_schema_drift_conflict(self) -> None:
        plan = make_plan()
        database, data_source = created_pair(plan)

        foreign = copy.deepcopy(database)
        foreign["description"] = []
        foreign_current = live_binding(children=[foreign])
        foreign_decision = decide_schema_reconciliation(
            plan,
            current_live_binding=foreign_current,
            candidates=[RemoteSchemaCandidate(foreign, data_source)],
            create_was_attempted=False,
            attempt_started_at=STARTED,
            observed_at=OBSERVED,
        )
        self.assertIs(
            foreign_decision.disposition, ReconciliationDisposition.CONFLICT
        )
        self.assertEqual(
            foreign_decision.reason_code, "foreign_or_mismatched_child"
        )

        second_database, second_source = created_pair(plan, database_id=OTHER_ID)
        second_source["parent"]["database_id"] = OTHER_ID
        multiple_current = live_binding(children=[database, second_database])
        multiple = decide_schema_reconciliation(
            plan,
            current_live_binding=multiple_current,
            candidates=[
                RemoteSchemaCandidate(database, data_source),
                RemoteSchemaCandidate(second_database, second_source),
            ],
            create_was_attempted=True,
            attempt_started_at=STARTED,
            observed_at=OBSERVED,
        )
        self.assertIs(multiple.disposition, ReconciliationDisposition.CONFLICT)
        self.assertEqual(multiple.reason_code, "multiple_relevant_children")

        bad_source = copy.deepcopy(data_source)
        bad_source["properties"]["대상 사건"]["relation"][
            "data_source_id"
        ] = OTHER_ID
        mismatched = decide_schema_reconciliation(
            plan,
            current_live_binding=live_binding(children=[database]),
            candidates=[RemoteSchemaCandidate(database, bad_source)],
            create_was_attempted=True,
            attempt_started_at=STARTED,
            observed_at=OBSERVED,
        )
        self.assertIs(mismatched.disposition, ReconciliationDisposition.CONFLICT)
        self.assertEqual(mismatched.reason_code, "candidate_verification_failed")

    def test_reconciliation_rejects_live_relation_drift_and_snapshot_disagreement(self) -> None:
        plan = make_plan()
        current = live_binding()
        drifted = replace(
            current,
            relations=tuple(
                replace(item, display_name="다른 표시명")
                if item.logical_name == "그룹"
                else item
                for item in current.relations
            ),
        )
        drift = decide_schema_reconciliation(
            plan,
            current_live_binding=drifted,
            candidates=[],
            create_was_attempted=False,
            attempt_started_at=STARTED,
            observed_at=OBSERVED,
        )
        self.assertIs(drift.disposition, ReconciliationDisposition.CONFLICT)
        self.assertEqual(drift.reason_code, "live_binding_drift")

        database, _ = created_pair(plan)
        snapshot_disagreement = decide_schema_reconciliation(
            plan,
            current_live_binding=live_binding(children=[database]),
            candidates=[],
            create_was_attempted=True,
            attempt_started_at=STARTED,
            observed_at=OBSERVED,
        )
        self.assertIs(
            snapshot_disagreement.disposition,
            ReconciliationDisposition.CONFLICT,
        )
        self.assertEqual(
            snapshot_disagreement.reason_code,
            "direct_child_snapshot_mismatch",
        )


if __name__ == "__main__":
    unittest.main()

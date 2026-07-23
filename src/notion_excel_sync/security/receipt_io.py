from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

from notion_excel_sync.models import ApprovalReceipt, dataclass_to_dict


_RECEIPT_REFERENCE_RE = re.compile(r"nxr-[0-9a-f]{64}\.json")


class ReceiptReferenceError(ValueError):
    """Raised when an untrusted receipt reference is not a safe opaque ID."""


def receipt_reference(receipt: ApprovalReceipt) -> str:
    """Return a stable opaque filename for one exact approval receipt."""

    identity = "\0".join(
        (
            "notion-excel-sync-receipt/v1",
            receipt.proposal_id,
            str(receipt.revision),
            receipt.nonce,
        )
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"nxr-{digest}.json"


def resolve_receipt_reference(
    receipts_dir: str | Path,
    reference: str,
) -> Path:
    """Resolve an opaque receipt reference inside the trusted receipt directory.

    The reference is intentionally a single strict filename.  Existing
    symlinks and path traversal are rejected by comparing fully resolved paths.
    """

    if not isinstance(reference, str) or not _RECEIPT_REFERENCE_RE.fullmatch(
        reference
    ):
        raise ReceiptReferenceError("Receipt reference is invalid")
    trusted_root = Path(receipts_dir).resolve()
    target = (trusted_root / reference).resolve()
    if target.parent != trusted_root:
        raise ReceiptReferenceError("Receipt reference leaves the trusted directory")
    return target


def save_receipt(receipt: ApprovalReceipt, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(dataclass_to_dict(receipt), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return target


def load_receipt(path: str | Path) -> ApprovalReceipt:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return ApprovalReceipt(
        proposal_id=data["proposal_id"],
        revision=int(data["revision"]),
        proposal_digest=data["proposal_digest"],
        source_version_id=data["source_version_id"],
        source_file_hash=data["source_file_hash"],
        telegram_user_id=str(data["telegram_user_id"]),
        chat_id=str(data["chat_id"]),
        issued_at=datetime.fromisoformat(data["issued_at"]),
        expires_at=datetime.fromisoformat(data["expires_at"]),
        nonce=data["nonce"],
        signature=data["signature"],
    )

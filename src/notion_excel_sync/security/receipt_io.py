from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from notion_excel_sync.models import ApprovalReceipt, dataclass_to_dict


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


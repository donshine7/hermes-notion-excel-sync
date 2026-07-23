from __future__ import annotations

import argparse
import _socket
import json
import os
import socket
import sys
from pathlib import Path
from types import ModuleType


DOCUMENT_REJECTION_EXIT_CODE = 20
DOCUMENT_REJECTION_CODE = "extractor_rejected_document"
DOCUMENT_OUTPUT_LIMIT_CODE = "extractor_output_limit"


def _deny_network(*args: object, **kwargs: object) -> None:
    del args, kwargs
    raise RuntimeError("network_disabled_in_wiki_extractor")


_NETWORK_ENTRY_POINTS = (
    "SocketType",
    "socket",
    "socketpair",
    "fromfd",
    "fromshare",
    "create_connection",
    "create_server",
    "getaddrinfo",
    "gethostbyname",
    "gethostbyname_ex",
    "gethostbyaddr",
    "getnameinfo",
)


def _disable_network(
    socket_module: ModuleType = socket,
    raw_socket_module: ModuleType = _socket,
) -> None:
    """Replace ordinary high- and low-level socket entry points.

    This is a defense-in-depth guard inside a same-user parser process.  The
    Windows Job Object owned by the parent supplies resource containment but
    is not an AppContainer network sandbox.
    """

    for module in (socket_module, raw_socket_module):
        for name in _NETWORK_ENTRY_POINTS:
            if hasattr(module, name):
                setattr(module, name, _deny_network)


def _within(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--root", required=True)
    parser.add_argument("--input", required=True)
    parser.add_argument("--runtime-site", required=True)
    parser.add_argument("--extension", required=True)
    parser.add_argument("--max-input-bytes", type=int, required=True)
    parser.add_argument("--max-output-chars", type=int, required=True)
    args = parser.parse_args()

    root = Path(args.root).resolve()
    source = Path(args.input).resolve()
    try:
        runtime_site = Path(args.runtime_site).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RuntimeError("extractor_runtime_site_invalid") from exc
    if not _within(root, source) or not source.is_file():
        raise RuntimeError("extractor_input_outside_staging_root")
    if (
        not runtime_site.is_dir()
        or _within(root, runtime_site)
        or _within(runtime_site, root)
    ):
        raise RuntimeError("extractor_runtime_site_invalid")

    allowed_environment = {
        key: os.environ[key]
        for key in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PYTHONUTF8")
        if key in os.environ
    }
    os.environ.clear()
    os.environ.update(allowed_environment)

    package_root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(runtime_site))
    sys.path.insert(0, str(package_root))
    from notion_excel_sync.knowledge.extractors import (  # noqa: PLC0415
        ExtractionLimits,
        UnsafeDocumentError,
        extract_document,
    )
    from notion_excel_sync.knowledge.policy import (  # noqa: PLC0415
        redact_sensitive_text,
    )

    # Import the trusted parser modules first so standard-library SSL classes
    # can initialize, then disable all ordinary socket entry points before any
    # untrusted document bytes are parsed.
    _disable_network()

    data = source.read_bytes()
    try:
        extracted = extract_document(
            data,
            args.extension,
            limits=ExtractionLimits(
                max_input_bytes=args.max_input_bytes,
                max_output_chars=args.max_output_chars,
            ),
        )
    except UnsafeDocumentError as exc:
        # Only the parser's explicit, expected document-safety rejection may
        # use this exit. Imports, guard initialization, redaction, and all
        # unexpected exceptions remain ordinary nonzero worker failures.
        rejection_code = (
            DOCUMENT_OUTPUT_LIMIT_CODE
            if str(exc) == "extracted_text_size_limit"
            else DOCUMENT_REJECTION_CODE
        )
        sys.stdout.reconfigure(encoding="utf-8")
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "status": "document_rejected",
                    "code": rejection_code,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        return DOCUMENT_REJECTION_EXIT_CODE
    text, redactions = redact_sensitive_text(extracted.text)
    if not text:
        raise RuntimeError("no_text_after_redaction")
    sys.stdout.reconfigure(encoding="utf-8")
    print(
        json.dumps(
            {
                "schema_version": 1,
                "text": text,
                "warnings": [*extracted.warnings, *redactions],
                "extractor_version": extracted.extractor_version,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

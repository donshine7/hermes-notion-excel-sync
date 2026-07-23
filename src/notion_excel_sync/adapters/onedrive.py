from __future__ import annotations

import json
import threading
import time
import unicodedata
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import UTC, datetime
from http.client import IncompleteRead
from typing import Any, Callable, Iterable, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener


GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"


class TokenProvider(Protocol):
    def __call__(self) -> str: ...


class UrlOpener(Protocol):
    def __call__(self, request: Request, *, timeout: float) -> Any: ...


class UrlOpenerFactory(Protocol):
    def __call__(self) -> UrlOpener: ...


class _NoCrossHostAuthorizationRedirect(HTTPRedirectHandler):
    """Follow Graph download redirects without forwarding the bearer token."""

    def redirect_request(
        self,
        request: Request,
        file_pointer: Any,
        code: int,
        message: str,
        headers: Mapping[str, str],
        new_url: str,
    ) -> Request | None:
        redirected = super().redirect_request(
            request, file_pointer, code, message, headers, new_url
        )
        if redirected is None:
            return None
        old_host = urlparse(request.full_url).netloc.casefold()
        new_host = urlparse(new_url).netloc.casefold()
        if old_host != new_host:
            redirected.remove_header("Authorization")
        return redirected


class OneDriveError(RuntimeError):
    """Raised when a read-only Microsoft Graph operation fails."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class BaselineVersionNotFound(OneDriveError):
    pass


@dataclass(frozen=True, slots=True)
class DriveItem:
    drive_id: str
    item_id: str
    name: str
    version_tag: str
    last_modified_at: datetime
    size: int | None = None
    web_url: str | None = None


@dataclass(frozen=True, slots=True)
class DriveVersion:
    id: str
    last_modified_at: datetime
    size: int | None = None
    etag: str | None = None


@dataclass(frozen=True, slots=True)
class DriveSearchResult:
    drive_id: str
    item_id: str
    name: str
    web_url: str | None = None
    size: int | None = None


@dataclass(frozen=True, slots=True)
class DriveEntry:
    """OneDrive hierarchy entry with enough metadata for a bound Wiki catalog."""

    drive_id: str
    item_id: str
    name: str
    relative_path: str
    parent_item_id: str | None
    parent_path: str | None
    is_folder: bool
    version_tag: str
    last_modified_at: datetime
    size: int | None = None
    web_url: str | None = None
    mime_type: str | None = None
    etag: str | None = None
    ctag: str | None = None


@dataclass(frozen=True, slots=True)
class DriveFolderDelta:
    """One root-scoped Graph delta result for initial or incremental cataloging."""

    root: DriveEntry
    entries: tuple[DriveEntry, ...]
    deleted_item_ids: tuple[str, ...]
    delta_link: str


def _parse_graph_datetime(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def select_baseline_version(
    versions: Iterable[DriveVersion], cutoff: datetime
) -> DriveVersion:
    """Return the newest version strictly before the first synchronization cutoff."""

    if cutoff.tzinfo is None:
        raise ValueError("cutoff must be timezone-aware")
    cutoff_utc = cutoff.astimezone(UTC)
    candidates = [version for version in versions if version.last_modified_at < cutoff_utc]
    if not candidates:
        raise BaselineVersionNotFound(
            f"No OneDrive version exists before {cutoff.isoformat()}"
        )
    latest_time = max(version.last_modified_at for version in candidates)
    latest = [version for version in candidates if version.last_modified_at == latest_time]
    if len(latest) != 1:
        raise BaselineVersionNotFound(
            "Multiple OneDrive versions share the latest baseline timestamp; "
            "a user-selected baseline is required"
        )
    return latest[0]


class OneDriveReadOnlyClient:
    """Small, deliberately read-only Microsoft Graph client for a single drive item.

    The public API exposes only metadata/version reads and content downloads.  It never
    issues POST, PUT, PATCH, or DELETE requests.
    """

    def __init__(
        self,
        access_token: str | TokenProvider,
        *,
        base_url: str = GRAPH_BASE_URL,
        timeout: float = 30.0,
        opener: UrlOpener | None = None,
        opener_factory: UrlOpenerFactory | None = None,
        max_retries: int = 3,
        max_json_bytes: int = 16 * 1024 * 1024,
        max_download_bytes: int = 256 * 1024 * 1024,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if not base_url.startswith(("https://", "http://")):
            raise ValueError("base_url must be an HTTP(S) URL")
        if opener is not None and opener_factory is not None:
            raise ValueError("Use either opener or opener_factory, not both")
        self._access_token = access_token
        self._token_lock = threading.Lock()
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        # A urllib OpenerDirector has no documented concurrent-use contract.
        # Production GETs therefore receive independent opener instances. The
        # legacy injected opener remains supported, but is explicitly locked;
        # tests or callers that require concurrent injected I/O use a factory.
        self._opener = opener
        self._opener_lock = threading.Lock()
        self._opener_factory = opener_factory or (
            lambda: build_opener(_NoCrossHostAuthorizationRedirect()).open
        )
        self._max_retries = max(0, max_retries)
        if max_json_bytes <= 0 or max_download_bytes <= 0:
            raise ValueError("OneDrive response limits must be positive")
        self._max_json_bytes = max_json_bytes
        self._max_download_bytes = max_download_bytes
        self._sleeper = sleeper

    def _token(self) -> str:
        if callable(self._access_token):
            # MSAL token caches/providers are commonly stateful. Serialize only
            # token acquisition; the subsequent network GET remains parallel.
            with self._token_lock:
                token = self._access_token()
        else:
            token = self._access_token
        token = token.strip()
        if not token:
            raise OneDriveError("Microsoft Graph access token is empty")
        return token

    def _item_path(self, drive_id: str, item_id: str) -> str:
        drive = quote(drive_id, safe="")
        item = quote(item_id, safe="")
        return f"/drives/{drive}/items/{item}"

    def _url(self, path: str) -> str:
        return f"{self._base_url}/{path.lstrip('/')}"

    def _validate_page_url(self, url: str) -> None:
        expected = urlparse(self._base_url)
        actual = urlparse(url)
        if (actual.scheme, actual.netloc) != (expected.scheme, expected.netloc):
            raise OneDriveError("Graph pagination attempted to leave the configured host")

    @staticmethod
    def _read_bounded(response: Any, max_bytes: int) -> bytes:
        headers = getattr(response, "headers", None)
        content_length = headers.get("Content-Length") if headers is not None else None
        if content_length:
            try:
                declared = int(content_length)
            except (TypeError, ValueError) as exc:
                raise OneDriveError("Microsoft Graph returned an invalid Content-Length") from exc
            if declared < 0 or declared > max_bytes:
                raise OneDriveError("Microsoft Graph response exceeds the configured limit")
        body = response.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise OneDriveError("Microsoft Graph response exceeds the configured limit")
        return body

    def _get(self, url: str, *, max_bytes: int) -> bytes:
        self._validate_page_url(url)
        token = self._token()
        retryable_statuses = {429, 500, 502, 503, 504}
        for attempt in range(self._max_retries + 1):
            request = Request(
                url,
                method="GET",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json, application/octet-stream",
                },
            )
            try:
                if self._opener is not None:
                    with self._opener_lock:
                        with self._opener(request, timeout=self._timeout) as response:
                            return self._read_bounded(response, max_bytes)
                independent_opener = self._opener_factory()
                with independent_opener(request, timeout=self._timeout) as response:
                    return self._read_bounded(response, max_bytes)
            except HTTPError as exc:
                detail = exc.read(2048).decode("utf-8", errors="replace")
                if exc.code in retryable_statuses and attempt < self._max_retries:
                    retry_after = exc.headers.get("Retry-After") if exc.headers else None
                    try:
                        delay = float(retry_after) if retry_after else 2**attempt
                    except ValueError:
                        delay = 2**attempt
                    self._sleeper(min(max(delay, 0.1), 10.0))
                    continue
                raise OneDriveError(
                    f"Microsoft Graph GET failed ({exc.code}): {detail}", status=exc.code
                ) from exc
            except URLError as exc:
                if attempt < self._max_retries:
                    self._sleeper(min(2**attempt, 10.0))
                    continue
                raise OneDriveError(f"Microsoft Graph GET failed: {exc.reason}") from exc
            except IncompleteRead as exc:
                if attempt < self._max_retries:
                    self._sleeper(min(2**attempt, 10.0))
                    continue
                raise OneDriveError("Microsoft Graph returned an incomplete response") from exc
        raise AssertionError("unreachable Graph retry state")

    def _get_json(self, url: str) -> Mapping[str, Any]:
        raw = self._get(url, max_bytes=self._max_json_bytes)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OneDriveError("Microsoft Graph returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise OneDriveError("Microsoft Graph returned a non-object JSON response")
        return value

    def get_item(self, drive_id: str, item_id: str) -> DriveItem:
        payload = self._get_json(self._url(self._item_path(drive_id, item_id)))
        try:
            modified = _parse_graph_datetime(str(payload["lastModifiedDateTime"]))
            returned_item_id = str(payload["id"])
            name = str(payload["name"])
        except (KeyError, TypeError, ValueError) as exc:
            raise OneDriveError("Drive item metadata is missing required fields") from exc
        version_tag = str(payload.get("eTag") or payload.get("cTag") or "")
        return DriveItem(
            drive_id=drive_id,
            item_id=returned_item_id,
            name=name,
            version_tag=version_tag,
            last_modified_at=modified,
            size=int(payload["size"]) if payload.get("size") is not None else None,
            web_url=str(payload["webUrl"]) if payload.get("webUrl") else None,
        )

    @staticmethod
    def _entry_from_payload(
        payload: Mapping[str, Any],
        *,
        expected_drive_id: str,
        relative_path: str,
    ) -> DriveEntry:
        parent = payload.get("parentReference")
        parent = parent if isinstance(parent, dict) else {}
        returned_drive_id = str(parent.get("driveId") or expected_drive_id)
        if returned_drive_id != expected_drive_id:
            raise OneDriveError("OneDrive folder traversal attempted to cross drives")
        try:
            item_id = str(payload["id"])
            name = str(payload["name"])
            modified = _parse_graph_datetime(str(payload["lastModifiedDateTime"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise OneDriveError("Drive hierarchy entry is missing required fields") from exc
        file_facet = payload.get("file")
        folder_facet = payload.get("folder")
        is_file = isinstance(file_facet, dict)
        is_folder = isinstance(folder_facet, dict)
        if is_file == is_folder:
            raise OneDriveError("Drive hierarchy entry is neither one file nor one folder")
        etag = str(payload["eTag"]) if payload.get("eTag") else None
        ctag = str(payload["cTag"]) if payload.get("cTag") else None
        return DriveEntry(
            drive_id=returned_drive_id,
            item_id=item_id,
            name=name,
            relative_path=relative_path.replace("\\", "/").strip("/"),
            parent_item_id=(
                str(parent["id"]) if parent.get("id") is not None else None
            ),
            parent_path=str(parent["path"]) if parent.get("path") else None,
            is_folder=is_folder,
            version_tag=etag or ctag or "",
            last_modified_at=modified,
            size=int(payload["size"]) if payload.get("size") is not None else None,
            web_url=str(payload["webUrl"]) if payload.get("webUrl") else None,
            mime_type=(
                str(file_facet["mimeType"])
                if is_file and file_facet.get("mimeType")
                else None
            ),
            etag=etag,
            ctag=ctag,
        )

    @staticmethod
    def _entry_fields() -> str:
        return (
            "id,name,webUrl,size,eTag,cTag,lastModifiedDateTime,"
            "parentReference,file,folder,deleted,remoteItem"
        )

    def get_entry(self, drive_id: str, item_id: str) -> DriveEntry:
        fields = self._entry_fields()
        payload = self._get_json(
            self._url(f"{self._item_path(drive_id, item_id)}?$select={fields}")
        )
        return self._entry_from_payload(
            payload,
            expected_drive_id=drive_id,
            relative_path=str(payload.get("name") or ""),
        )

    def get_item_by_path(self, drive_id: str, path: str) -> DriveEntry:
        """Resolve one exact path in a drive without using broad search."""

        normalized = path.replace("\\", "/").strip("/")
        if not normalized or any(part in {"", ".", ".."} for part in normalized.split("/")):
            raise ValueError("OneDrive path must be a non-empty canonical relative path")
        drive = quote(drive_id, safe="")
        encoded_path = quote(normalized, safe="/")
        fields = self._entry_fields()
        payload = self._get_json(
            self._url(f"/drives/{drive}/root:/{encoded_path}?$select={fields}")
        )
        return self._entry_from_payload(
            payload,
            expected_drive_id=drive_id,
            relative_path=normalized,
        )

    def list_children(
        self,
        drive_id: str,
        folder_item_id: str,
        *,
        parent_relative_path: str = "",
    ) -> list[DriveEntry]:
        """List direct children of one exact folder using GET-only pagination."""

        fields = self._entry_fields()
        url = self._url(
            f"{self._item_path(drive_id, folder_item_id)}/children"
            f"?$select={fields}&$top=200"
        )
        entries: list[DriveEntry] = []
        seen_pages: set[str] = set()
        while url:
            self._validate_children_url(
                url,
                drive_id=drive_id,
                folder_item_id=folder_item_id,
            )
            if url in seen_pages:
                raise OneDriveError("Microsoft Graph returned a children pagination loop")
            seen_pages.add(url)
            payload = self._get_json(url)
            rows = payload.get("value", [])
            if not isinstance(rows, list):
                raise OneDriveError("OneDrive children response has an invalid value field")
            for row in rows:
                if not isinstance(row, dict) or isinstance(row.get("deleted"), dict):
                    continue
                # Shared remoteItem links can lead outside the pinned hierarchy.
                if isinstance(row.get("remoteItem"), dict):
                    continue
                name = str(row.get("name") or "")
                if not name or "/" in name or "\\" in name:
                    raise OneDriveError("OneDrive returned an unsafe child name")
                relative_path = "/".join(
                    part for part in (parent_relative_path.strip("/"), name) if part
                )
                entries.append(
                    self._entry_from_payload(
                        row,
                        expected_drive_id=drive_id,
                        relative_path=relative_path,
                    )
                )
            next_link = payload.get("@odata.nextLink")
            if next_link is not None and not isinstance(next_link, str):
                raise OneDriveError("OneDrive children response has an invalid nextLink")
            url = next_link or ""
        return sorted(
            entries,
            key=lambda item: (not item.is_folder, item.name.casefold(), item.item_id),
        )

    def _validate_children_url(
        self,
        url: str,
        *,
        drive_id: str,
        folder_item_id: str,
    ) -> None:
        """Bind a children continuation URL to one exact drive/folder route."""

        self._validate_page_url(url)
        actual_path = urlparse(url).path.rstrip("/")
        expected_path = urlparse(
            self._url(f"{self._item_path(drive_id, folder_item_id)}/children")
        ).path.rstrip("/")

        def segments(path: str) -> tuple[str, ...]:
            decoded: list[str] = []
            for value in (part for part in path.split("/") if part):
                item = unquote(value)
                if not item or "/" in item or "\\" in item or item in {".", ".."}:
                    raise OneDriveError(
                        "OneDrive children pagination attempted to leave the pinned folder"
                    )
                decoded.append(item)
            return tuple(decoded)

        actual_segments = segments(actual_path)
        expected_segments = segments(expected_path)
        if actual_segments != expected_segments:
            raise OneDriveError(
                "OneDrive children pagination attempted to leave the pinned folder"
            )

    def walk_folder(
        self,
        drive_id: str,
        root_item_id: str,
        *,
        max_items: int = 50_000,
        max_depth: int = 64,
        include_root_names: Iterable[str] | None = None,
        max_workers: int = 4,
    ) -> list[DriveEntry]:
        """Recursively enumerate descendants of a pinned folder item.

        If ``include_root_names`` is supplied, list the pinned root once and
        traverse only exactly matched top-level folders. Matching uses NFC plus
        case-folding and fails closed on duplicate or ambiguous names.
        """

        if max_items <= 0 or max_depth <= 0:
            raise ValueError("Folder traversal limits must be positive")
        if max_workers <= 0 or max_workers > 4:
            raise ValueError("Folder traversal max_workers must be between 1 and 4")
        requested_roots: dict[str, str] | None = None
        if include_root_names is not None:
            requested_roots = {}
            for raw_name in include_root_names:
                name = unicodedata.normalize("NFC", str(raw_name))
                if not name or "/" in name or "\\" in name or name in {".", ".."}:
                    raise OneDriveError("OneDrive include root name is not canonical")
                key = name.casefold()
                if key in requested_roots:
                    raise OneDriveError(
                        "OneDrive include root names are duplicate or ambiguous"
                    )
                requested_roots[key] = name
        root = self.get_entry(drive_id, root_item_id)
        if (
            root.drive_id != drive_id
            or root.item_id != root_item_id
            or not root.is_folder
        ):
            raise OneDriveError("Pinned Wiki root item is not a folder")
        # Root selection is intentionally a single request. No excluded folder
        # is submitted to the worker pool.
        root_children = self.list_children(
            drive_id,
            root.item_id,
            parent_relative_path="",
        )
        if requested_roots is not None:
            matched: dict[str, DriveEntry] = {}
            for child in root_children:
                key = unicodedata.normalize("NFC", child.name).casefold()
                if key not in requested_roots:
                    continue
                if key in matched:
                    raise OneDriveError(
                        "OneDrive include root folders are duplicate or ambiguous"
                    )
                if not child.is_folder:
                    raise OneDriveError(
                        "OneDrive include root matched a non-folder item"
                    )
                matched[key] = child
            root_children = list(matched.values())
        if len(root_children) > max_items:
            raise OneDriveError("OneDrive folder hierarchy exceeds max_items")

        result = list(root_children)
        pending = [
            (child.item_id, child.relative_path, 1)
            for child in root_children
            if child.is_folder
        ]
        seen_folders: set[str] = {root.item_id}

        def pending_key(item: tuple[str, str, int]) -> tuple[str, str, str]:
            folder_id, relative_path, _ = item
            normalized_path = unicodedata.normalize("NFC", relative_path)
            return normalized_path.casefold(), relative_path, folder_id

        with ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="onedrive-folder-read",
        ) as executor:
            while pending:
                pending.sort(key=pending_key)
                level_depth = pending[0][2]
                if any(item[2] != level_depth for item in pending):
                    raise OneDriveError("OneDrive folder traversal lost depth ordering")
                if level_depth >= max_depth:
                    raise OneDriveError("OneDrive folder hierarchy exceeds max_depth")
                level_ids: set[str] = set()
                for folder_id, _, _ in pending:
                    if folder_id in seen_folders or folder_id in level_ids:
                        raise OneDriveError("OneDrive folder hierarchy contains a loop")
                    level_ids.add(folder_id)
                seen_folders.update(level_ids)

                next_pending: list[tuple[str, str, int]] = []
                # Submit at most one worker-width chunk so the executor queue is
                # bounded as well as its active thread count.
                for offset in range(0, len(pending), max_workers):
                    chunk = pending[offset : offset + max_workers]
                    futures = [
                        executor.submit(
                            self.list_children,
                            drive_id,
                            folder_id,
                            parent_relative_path=relative_parent,
                        )
                        for folder_id, relative_parent, _ in chunk
                    ]
                    done, not_done = wait(futures, return_when=FIRST_EXCEPTION)
                    failed = next(
                        (
                            future
                            for future in futures
                            if future in done and future.exception() is not None
                        ),
                        None,
                    )
                    if failed is not None:
                        for future in not_done:
                            future.cancel()
                        failed.result()
                    child_batches = [future.result() for future in futures]
                    observed = sum(len(children) for children in child_batches)
                    if len(result) + observed > max_items:
                        raise OneDriveError(
                            "OneDrive folder hierarchy exceeds max_items"
                        )
                    for children in child_batches:
                        result.extend(children)
                        for child in children:
                            if child.is_folder:
                                next_pending.append(
                                    (
                                        child.item_id,
                                        child.relative_path,
                                        level_depth + 1,
                                    )
                                )
                pending = next_pending
        return sorted(
            result,
            key=lambda item: (
                unicodedata.normalize("NFC", item.relative_path).casefold(),
                item.relative_path,
                item.item_id,
            ),
        )

    def _validate_folder_delta_url(
        self,
        url: str,
        *,
        drive_id: str,
        root_item_id: str,
    ) -> None:
        self._validate_page_url(url)
        actual_path = urlparse(url).path.rstrip("/")
        expected_url = self._url(
            f"{self._item_path(drive_id, root_item_id)}/delta"
        )
        expected_path = urlparse(expected_url).path.rstrip("/")

        def segments(path: str) -> list[str]:
            values = [part for part in path.split("/") if part]
            decoded: list[str] = []
            for value in values:
                item = unquote(value)
                if not item or "/" in item or "\\" in item or item in {".", ".."}:
                    raise OneDriveError(
                        "OneDrive folder delta attempted to leave the pinned root"
                    )
                decoded.append(item)
            return decoded

        actual_segments = segments(actual_path)
        expected_segments = segments(expected_path)
        same_route = (
            len(actual_segments) == len(expected_segments)
            and tuple(actual_segments[:-1]) == tuple(expected_segments[:-1])
        )
        final_segment = actual_segments[-1] if actual_segments else ""
        final_folded = final_segment.casefold()
        exact_delta = final_folded == "delta"
        odata_delta = (
            final_folded.startswith("delta(")
            and final_segment.endswith(")")
            and len(final_segment) <= 16_384
        )
        if not same_route or not (exact_delta or odata_delta):
            raise OneDriveError("OneDrive folder delta attempted to leave the pinned root")

    def read_folder_delta(
        self,
        drive_id: str,
        root_item_id: str,
        *,
        delta_link: str | None = None,
        max_items: int = 50_000,
    ) -> DriveFolderDelta:
        """Read a root-scoped initial or incremental delta using GET only.

        A returned delta link can be persisted in the protected Wiki index and
        supplied to the next call.  It is accepted only when its Graph route is
        still bound to the same drive and folder item.
        """

        if max_items <= 0:
            raise ValueError("Folder delta max_items must be positive")
        root = self.get_entry(drive_id, root_item_id)
        if (
            root.drive_id != drive_id
            or root.item_id != root_item_id
            or not root.is_folder
        ):
            raise OneDriveError("Pinned Wiki root item is not a folder")
        root_parent = (root.parent_path or "").rstrip("/")
        if not root_parent:
            raise OneDriveError("Pinned Wiki root has no canonical parent path")
        root_graph_path = f"{root_parent}/{root.name}"
        if delta_link:
            self._validate_folder_delta_url(
                delta_link,
                drive_id=drive_id,
                root_item_id=root_item_id,
            )
            url = delta_link
        else:
            fields = self._entry_fields()
            url = self._url(
                f"{self._item_path(drive_id, root_item_id)}/delta"
                f"?$select={fields}&$top=200"
            )
        changes: dict[str, DriveEntry] = {}
        deleted: set[str] = set()
        seen_pages: set[str] = set()
        final_delta_link = ""
        observed = 0
        while url:
            self._validate_folder_delta_url(
                url,
                drive_id=drive_id,
                root_item_id=root_item_id,
            )
            if url in seen_pages:
                raise OneDriveError("Microsoft Graph returned a folder delta loop")
            seen_pages.add(url)
            payload = self._get_json(url)
            rows = payload.get("value", [])
            if not isinstance(rows, list):
                raise OneDriveError("OneDrive folder delta has an invalid value field")
            observed += len(rows)
            if observed > max_items:
                raise OneDriveError("OneDrive folder delta exceeds max_items")
            for row in rows:
                if not isinstance(row, dict) or not row.get("id"):
                    continue
                item_id = str(row["id"])
                if item_id == root_item_id:
                    continue
                if isinstance(row.get("deleted"), dict):
                    changes.pop(item_id, None)
                    deleted.add(item_id)
                    continue
                if isinstance(row.get("remoteItem"), dict):
                    changes.pop(item_id, None)
                    deleted.add(item_id)
                    continue
                parent = row.get("parentReference")
                parent = parent if isinstance(parent, dict) else {}
                parent_path = str(parent.get("path") or "")
                normalized_parent = parent_path.rstrip("/")
                normalized_root = root_graph_path.rstrip("/")
                parent_folded = normalized_parent.casefold()
                root_folded = normalized_root.casefold()
                if parent_folded != root_folded and not parent_folded.startswith(
                    root_folded + "/"
                ):
                    raise OneDriveError(
                        "OneDrive folder delta returned an entry outside the pinned root"
                    )
                suffix = normalized_parent[len(normalized_root) :].strip("/")
                name = str(row.get("name") or "")
                if not name or "/" in name or "\\" in name:
                    raise OneDriveError("OneDrive folder delta returned an unsafe name")
                relative_path = "/".join(part for part in (suffix, name) if part)
                changes[item_id] = self._entry_from_payload(
                    row,
                    expected_drive_id=drive_id,
                    relative_path=relative_path,
                )
                deleted.discard(item_id)
            next_link = payload.get("@odata.nextLink")
            candidate_delta = payload.get("@odata.deltaLink")
            if next_link is not None and not isinstance(next_link, str):
                raise OneDriveError("OneDrive folder delta has an invalid nextLink")
            if candidate_delta is not None and not isinstance(candidate_delta, str):
                raise OneDriveError("OneDrive folder delta has an invalid deltaLink")
            if next_link:
                url = next_link
                continue
            final_delta_link = candidate_delta or ""
            url = ""
        if not final_delta_link:
            raise OneDriveError("OneDrive folder delta did not return a final deltaLink")
        self._validate_folder_delta_url(
            final_delta_link,
            drive_id=drive_id,
            root_item_id=root_item_id,
        )
        return DriveFolderDelta(
            root=root,
            entries=tuple(
                sorted(
                    changes.values(),
                    key=lambda item: (item.relative_path.casefold(), item.item_id),
                )
            ),
            deleted_item_ids=tuple(sorted(deleted)),
            delta_link=final_delta_link,
        )

    def get_folder_delta_checkpoint(
        self,
        drive_id: str,
        root_item_id: str,
    ) -> DriveFolderDelta:
        """Acquire a current root-scoped delta cursor without enumerating history.

        Microsoft Graph's documented ``token=latest`` request yields an empty
        page and a deltaLink.  The request and returned cursor are both bound to
        the configured drive/folder route, and only GET is ever issued.
        """

        root = self.get_entry(drive_id, root_item_id)
        if (
            root.drive_id != drive_id
            or root.item_id != root_item_id
            or not root.is_folder
        ):
            raise OneDriveError("Pinned Wiki root item is not a folder")
        url = self._url(
            f"{self._item_path(drive_id, root_item_id)}/delta?token=latest"
        )
        self._validate_folder_delta_url(
            url,
            drive_id=drive_id,
            root_item_id=root_item_id,
        )
        payload = self._get_json(url)
        rows = payload.get("value", [])
        if not isinstance(rows, list):
            raise OneDriveError("OneDrive folder checkpoint has an invalid value field")
        if rows or payload.get("@odata.nextLink") is not None:
            raise OneDriveError("OneDrive latest checkpoint unexpectedly enumerated items")
        delta_link = payload.get("@odata.deltaLink")
        if not isinstance(delta_link, str) or not delta_link:
            raise OneDriveError("OneDrive latest checkpoint has no deltaLink")
        self._validate_folder_delta_url(
            delta_link,
            drive_id=drive_id,
            root_item_id=root_item_id,
        )
        return DriveFolderDelta(
            root=root,
            entries=(),
            deleted_item_ids=(),
            delta_link=delta_link,
        )

    def get_my_drive_id(self) -> str:
        payload = self._get_json(self._url("/me/drive?$select=id"))
        if not payload.get("id"):
            raise OneDriveError("Microsoft Graph did not return the personal drive ID")
        return str(payload["id"])

    def search_items(self, query: str) -> list[DriveSearchResult]:
        """Search the signed-in user's drive without requesting write permission."""

        normalized = query.strip()
        if not normalized:
            raise ValueError("OneDrive search query must not be empty")
        encoded = quote(normalized, safe="")
        fields = "id,name,webUrl,size,parentReference,file"
        url = self._url(
            f"/me/drive/root/search(q='{encoded}')?$select={fields}&$top=200"
        )
        results: list[DriveSearchResult] = []
        seen_pages: set[str] = set()
        while url:
            if url in seen_pages:
                raise OneDriveError("Microsoft Graph returned a search pagination loop")
            seen_pages.add(url)
            payload = self._get_json(url)
            rows = payload.get("value", [])
            if not isinstance(rows, list):
                raise OneDriveError("OneDrive search returned an invalid value field")
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("file"), dict):
                    continue
                parent = row.get("parentReference")
                if not isinstance(parent, dict) or not parent.get("driveId"):
                    continue
                try:
                    results.append(
                        DriveSearchResult(
                            drive_id=str(parent["driveId"]),
                            item_id=str(row["id"]),
                            name=str(row["name"]),
                            web_url=str(row["webUrl"]) if row.get("webUrl") else None,
                            size=int(row["size"]) if row.get("size") is not None else None,
                        )
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    raise OneDriveError(
                        "OneDrive search result is missing required fields"
                    ) from exc
            next_link = payload.get("@odata.nextLink")
            if next_link is not None and not isinstance(next_link, str):
                raise OneDriveError("OneDrive search returned an invalid nextLink")
            url = next_link or ""
        return results

    def enumerate_items(self) -> list[DriveSearchResult]:
        """Enumerate the current drive hierarchy through the read-only delta feed."""

        default_drive_id = self.get_my_drive_id()
        fields = "id,name,webUrl,size,parentReference,file,deleted"
        url = self._url(f"/me/drive/root/delta?$select={fields}&$top=200")
        current: dict[str, DriveSearchResult] = {}
        seen_pages: set[str] = set()
        while url:
            if url in seen_pages:
                raise OneDriveError("Microsoft Graph returned a delta pagination loop")
            seen_pages.add(url)
            payload = self._get_json(url)
            rows = payload.get("value", [])
            if not isinstance(rows, list):
                raise OneDriveError("OneDrive delta returned an invalid value field")
            for row in rows:
                if not isinstance(row, dict) or not row.get("id"):
                    continue
                item_id = str(row["id"])
                if isinstance(row.get("deleted"), dict):
                    current.pop(item_id, None)
                    continue
                if not isinstance(row.get("file"), dict):
                    continue
                parent = row.get("parentReference")
                drive_id = (
                    str(parent["driveId"])
                    if isinstance(parent, dict) and parent.get("driveId")
                    else default_drive_id
                )
                try:
                    current[item_id] = DriveSearchResult(
                        drive_id=drive_id,
                        item_id=item_id,
                        name=str(row["name"]),
                        web_url=str(row["webUrl"]) if row.get("webUrl") else None,
                        size=int(row["size"]) if row.get("size") is not None else None,
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    raise OneDriveError(
                        "OneDrive delta result is missing required fields"
                    ) from exc
            next_link = payload.get("@odata.nextLink")
            if next_link is not None and not isinstance(next_link, str):
                raise OneDriveError("OneDrive delta returned an invalid nextLink")
            url = next_link or ""
        return sorted(current.values(), key=lambda item: (item.name.casefold(), item.item_id))

    def list_versions(self, drive_id: str, item_id: str) -> list[DriveVersion]:
        url = self._url(f"{self._item_path(drive_id, item_id)}/versions?$top=200")
        versions: list[DriveVersion] = []
        seen_pages: set[str] = set()
        while url:
            if url in seen_pages:
                raise OneDriveError("Microsoft Graph returned a pagination loop")
            seen_pages.add(url)
            payload = self._get_json(url)
            rows = payload.get("value", [])
            if not isinstance(rows, list):
                raise OneDriveError("Version list response has an invalid value field")
            for row in rows:
                if not isinstance(row, dict):
                    continue
                try:
                    version_id = str(row["id"])
                    modified = _parse_graph_datetime(str(row["lastModifiedDateTime"]))
                except (KeyError, TypeError, ValueError) as exc:
                    raise OneDriveError("A version entry is missing required fields") from exc
                versions.append(
                    DriveVersion(
                        id=version_id,
                        last_modified_at=modified,
                        size=int(row["size"]) if row.get("size") is not None else None,
                        etag=str(row["eTag"]) if row.get("eTag") else None,
                    )
                )
            next_link = payload.get("@odata.nextLink")
            if next_link is not None and not isinstance(next_link, str):
                raise OneDriveError("Version list returned an invalid nextLink")
            url = next_link or ""
        return sorted(versions, key=lambda version: version.last_modified_at)

    def select_initial_baseline(
        self, drive_id: str, item_id: str, cutoff: datetime
    ) -> DriveVersion:
        return select_baseline_version(self.list_versions(drive_id, item_id), cutoff)

    def download_current(self, drive_id: str, item_id: str) -> bytes:
        return self._get(
            self._url(f"{self._item_path(drive_id, item_id)}/content"),
            max_bytes=self._max_download_bytes,
        )

    def download_version(self, drive_id: str, item_id: str, version_id: str) -> bytes:
        if not version_id.strip():
            raise ValueError("version_id must not be empty")
        version = quote(version_id, safe="")
        return self._get(
            self._url(f"{self._item_path(drive_id, item_id)}/versions/{version}/content"),
            max_bytes=self._max_download_bytes,
        )

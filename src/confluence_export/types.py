"""Data types for Confluence API responses and internal structures."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Space:
    id: str
    key: str
    name: str
    type: str = ""
    status: str = ""
    homepage_id: str = ""
    webui: str = ""
    base: str = ""

    @classmethod
    def from_api(cls, data: dict) -> Space:
        # `or` coalescing on EVERY field: the API/cache can carry an explicit
        # null anywhere (a dict-key default only applies when the key is
        # ABSENT); a None crashes string/path consumers and round-trips
        # through to_dict into the cache (#47 class).
        links = data.get("_links") or {}
        return cls(
            id=str(data.get("id") or ""),
            key=data.get("key") or "",
            name=data.get("name") or "",
            type=data.get("type") or "",
            status=data.get("status") or "",
            homepage_id=str(data.get("homepageId") or ""),
            webui=links.get("webui") or "",
            base=links.get("base") or "",
        )


@dataclass
class Version:
    created_at: str = ""
    message: str = ""
    number: int = 0
    minor_edit: bool = False
    author_id: str = ""

    @classmethod
    def from_api(cls, data: dict | None) -> Version:
        if not data:
            return cls()
        # `or` coalescing: explicit nulls crash consumers (`number > 0` in the
        # media phase) and round-trip through the cache (#47 class).
        return cls(
            created_at=data.get("createdAt") or "",
            message=data.get("message") or "",
            number=data.get("number") or 0,
            minor_edit=data.get("minorEdit") or False,
            author_id=data.get("authorId") or "",
        )


@dataclass
class Page:
    id: str
    title: str
    space_id: str = ""
    parent_id: str = ""
    parent_type: str = ""
    position: int = 0
    status: str = ""
    author_id: str = ""
    created_at: str = ""
    version: Version = field(default_factory=Version)
    body_storage: str = ""
    webui: str = ""
    editui: str = ""
    tinyui: str = ""

    @classmethod
    def from_api(cls, data: dict) -> Page:
        # `or` coalescing on EVERY field (#47 class): an explicit null title
        # aborts the whole space export in the layout planner (sanitize on
        # None) and to_dict round-trips the None into the cache, so every
        # --cached run crashes too until a refresh.
        links = data.get("_links") or {}
        body = data.get("body", {})
        storage = body.get("storage", {}) if body else {}
        return cls(
            id=str(data.get("id") or ""),
            title=data.get("title") or "",
            space_id=str(data.get("spaceId") or ""),
            parent_id=str(data.get("parentId") or ""),
            parent_type=data.get("parentType") or "",
            position=data.get("position") or 0,
            status=data.get("status") or "",
            author_id=data.get("authorId") or "",
            created_at=data.get("createdAt") or "",
            version=Version.from_api(data.get("version")),
            body_storage=(storage.get("value") or "") if storage else "",
            webui=links.get("webui") or "",
            editui=links.get("editui") or "",
            tinyui=links.get("tinyui") or "",
        )

    def to_dict(self) -> dict:
        d: dict = {
            "id": self.id,
            "title": self.title,
            "spaceId": self.space_id,
            "parentId": self.parent_id,
            "parentType": self.parent_type,
            "position": self.position,
            "status": self.status,
            "authorId": self.author_id,
            "createdAt": self.created_at,
            "version": {
                "createdAt": self.version.created_at,
                "message": self.version.message,
                "number": self.version.number,
                "minorEdit": self.version.minor_edit,
                "authorId": self.version.author_id,
            },
            "_links": {
                "webui": self.webui,
                "editui": self.editui,
                "tinyui": self.tinyui,
            },
        }
        if self.body_storage:
            d["body"] = {"storage": {"value": self.body_storage}}
        return d

    @classmethod
    def from_dict(cls, data: dict) -> Page:
        """Load from cached dict (same format as to_dict)."""
        return cls.from_api(data)


@dataclass
class Attachment:
    id: str
    title: str
    media_type: str = ""
    media_type_description: str = ""
    file_size: int = 0
    page_id: str = ""
    comment: str = ""
    created_at: str = ""
    version: Version = field(default_factory=Version)
    download_link: str = ""
    webui: str = ""

    @classmethod
    def from_api(cls, data: dict) -> Attachment:
        # `or` coalescing on EVERY field: the API/cache can carry an explicit
        # null (the key default only applies when absent); None crashes
        # .casefold() consumers (#47), and `str(None)` is the TRUTHY string
        # "None" — a null pageId would defeat the `if att.page_id` guard and
        # build a bogus /content/None/ download URL.
        links = data.get("_links") or {}
        return cls(
            id=str(data.get("id") or ""),
            title=data.get("title") or "",
            media_type=data.get("mediaType") or "",
            media_type_description=data.get("mediaTypeDescription") or "",
            file_size=data.get("fileSize") or 0,
            page_id=str(data.get("pageId") or ""),
            comment=data.get("comment") or "",
            created_at=data.get("createdAt") or "",
            version=Version.from_api(data.get("version")),
            download_link=links.get("download") or data.get("downloadLink") or "",
            webui=links.get("webui") or "",
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "mediaType": self.media_type,
            "mediaTypeDescription": self.media_type_description,
            "fileSize": self.file_size,
            "pageId": self.page_id,
            "comment": self.comment,
            "createdAt": self.created_at,
            "version": {
                "createdAt": self.version.created_at,
                "message": self.version.message,
                "number": self.version.number,
                "minorEdit": self.version.minor_edit,
                "authorId": self.version.author_id,
            },
            "_links": {"download": self.download_link, "webui": self.webui},
        }

    @classmethod
    def from_dict(cls, data: dict) -> Attachment:
        return cls.from_api(data)


@dataclass
class PageNode:
    """Tree node wrapping a Page with its children."""

    page: Page
    children: list[PageNode] = field(default_factory=list)


@dataclass
class CachedSpace:
    """Serializable snapshot of a space's pages and attachments."""

    space: Space
    pages: list[Page]
    attachments: dict[str, list[Attachment]]  # page_id -> attachments
    updated_at: str = ""
    include_archived: bool = False
    # Whether per-page attachment metadata was fetched this refresh. A page-only
    # refresh (tree/find/diff) sets this False; export must NOT treat such a cache
    # as authoritative for attachments/media-prune. Old caches (no flag) predate
    # page-only mode and were always full, so they default to True.
    attachments_complete: bool = True

    def to_dict(self) -> dict:
        return {
            "space": {
                "id": self.space.id,
                "key": self.space.key,
                "name": self.space.name,
                "type": self.space.type,
                "status": self.space.status,
                "homepageId": self.space.homepage_id,
                "_links": {"webui": self.space.webui, "base": self.space.base},
            },
            "pages": [p.to_dict() for p in self.pages],
            "attachments": {
                pid: [a.to_dict() for a in atts]
                for pid, atts in self.attachments.items()
            },
            "updated_at": self.updated_at,
            "include_archived": self.include_archived,
            "attachments_complete": self.attachments_complete,
        }

    @classmethod
    def from_dict(cls, data: dict) -> CachedSpace:
        return cls(
            space=Space.from_api(data.get("space", {})),
            pages=[Page.from_dict(p) for p in data.get("pages", [])],
            attachments={
                pid: [Attachment.from_dict(a) for a in atts]
                for pid, atts in data.get("attachments", {}).items()
            },
            updated_at=data.get("updated_at", ""),
            include_archived=bool(data.get("include_archived", False)),
            attachments_complete=bool(data.get("attachments_complete", True)),
        )

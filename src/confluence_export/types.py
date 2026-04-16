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
        links = data.get("_links", {})
        return cls(
            id=str(data.get("id", "")),
            key=data.get("key", ""),
            name=data.get("name", ""),
            type=data.get("type", ""),
            status=data.get("status", ""),
            homepage_id=str(data.get("homepageId", "")),
            webui=links.get("webui", ""),
            base=links.get("base", ""),
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
        return cls(
            created_at=data.get("createdAt", ""),
            message=data.get("message", ""),
            number=data.get("number", 0),
            minor_edit=data.get("minorEdit", False),
            author_id=data.get("authorId", ""),
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
        links = data.get("_links", {})
        body = data.get("body", {})
        storage = body.get("storage", {}) if body else {}
        return cls(
            id=str(data.get("id", "")),
            title=data.get("title", ""),
            space_id=str(data.get("spaceId", "")),
            parent_id=str(data.get("parentId", "") or ""),
            parent_type=data.get("parentType", ""),
            position=data.get("position", 0),
            status=data.get("status", ""),
            author_id=data.get("authorId", ""),
            created_at=data.get("createdAt", ""),
            version=Version.from_api(data.get("version")),
            body_storage=storage.get("value", "") if storage else "",
            webui=links.get("webui", ""),
            editui=links.get("editui", ""),
            tinyui=links.get("tinyui", ""),
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
        links = data.get("_links", {})
        return cls(
            id=str(data.get("id", "")),
            title=data.get("title", ""),
            media_type=data.get("mediaType", ""),
            media_type_description=data.get("mediaTypeDescription", ""),
            file_size=data.get("fileSize", 0),
            page_id=str(data.get("pageId", "")),
            comment=data.get("comment", ""),
            created_at=data.get("createdAt", ""),
            version=Version.from_api(data.get("version")),
            download_link=links.get("download", "") or data.get("downloadLink", ""),
            webui=links.get("webui", ""),
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
        )

"""Typed API-boundary models for conex v2.

All models that cross the API boundary inherit from ApiModel (frozen=True).
Store models (non-frozen, mutable during build) inherit from NullTolerantModel,
which shares the null-coercion validator without the frozen constraint.

Contract for NullTolerantModel._null_means_default:
  - An explicit JSON null (Python None) on ANY field is treated as an absent
    key: the field's default or default_factory value is substituted.
    This makes the '#47 null class' of crashes structurally unrepresentable.
  - An int arriving for a field whose annotation is str (v1 numeric ids)
    is coerced via str(v).
  - All other values pass through unchanged.

Contract for ApiModel:
  - frozen=True — attribute reassignment raises ValidationError.
  - No mutable collection fields (lists/dicts live on Snapshot/State models).
  - Every field has a default so that an empty dict or a dict with missing
    keys always produces a valid model instance.
"""

from __future__ import annotations

import typing
from typing import Any, get_args, get_origin

from pydantic import BaseModel, ConfigDict, field_validator
from pydantic_core import PydanticUndefined, PydanticUndefinedType


# ---------------------------------------------------------------------------
# Base classes
# ---------------------------------------------------------------------------


class NullTolerantModel(BaseModel):
    """Non-frozen base that coerces explicit nulls to field defaults.

    Intended for store models (ExportState, PageState, Snapshot, …) that are
    mutated by build.py.  Shares the null-tolerance contract with ApiModel
    without imposing frozen=True.
    """

    @field_validator("*", mode="before")
    @classmethod
    def _null_means_default(cls, v: Any, info: Any) -> Any:
        """Treat an explicit None as an absent key (use field default).

        Also coerces int -> str for fields whose annotation is str, to handle
        v1 numeric ids arriving from the API.

        Default resolution uses cls.model_fields[info.field_name] to access
        the pydantic FieldInfo for the current field, honoring both
        default and default_factory.
        """
        if v is None:
            fi = cls.model_fields.get(info.field_name)
            if fi is not None:
                if not isinstance(fi.default, PydanticUndefinedType):
                    return fi.default
                if fi.default_factory is not None:
                    return fi.default_factory()
            return v

        if isinstance(v, int) and _is_str_field(cls, info.field_name):
            return str(v)

        return v


def _is_str_field(model_cls: type[BaseModel], field_name: str) -> bool:
    """Return True when the named field's declared annotation resolves to str.

    Handles plain ``str``, ``str | None``, ``Optional[str]``, and other
    simple Union forms containing str at the top level.
    """
    fi = model_cls.model_fields.get(field_name)
    if fi is None:
        return False
    ann = fi.annotation
    if ann is str:
        return True
    origin = get_origin(ann)
    if origin is typing.Union:
        return str in get_args(ann)
    return False


class ApiModel(NullTolerantModel):
    """Frozen pydantic model for all API-boundary objects.

    Rules:
    - frozen=True: attribute reassignment raises ValidationError.
    - No mutable collection fields (use tuple or frozenset if needed;
      lists/dicts belong on Snapshot/State which are NullTolerantModel).
    - Every field must have a default so an empty dict is always valid.
    - Explicit null on any field is replaced by the field default (inherited
      from NullTolerantModel._null_means_default).
    - int values for str-annotated fields are coerced to str (v1 numeric ids).
    """

    model_config = ConfigDict(frozen=True)


# ---------------------------------------------------------------------------
# API models
# ---------------------------------------------------------------------------


class PageVersion(ApiModel):
    """Version metadata attached to a page or attachment."""

    number: int = 0
    created_at: str = ""
    message: str = ""
    author_id: str = ""


class Space(ApiModel):
    """A Confluence space."""

    id: str = ""
    key: str = ""
    name: str = ""
    homepage_id: str = ""


class Folder(ApiModel):
    """A Confluence folder (v2 API concept; not present in v1 cookie auth)."""

    id: str = ""
    title: str = ""
    parent_id: str = ""
    parent_type: str = ""
    position: int = 0


class Page(ApiModel):
    """A Confluence page as returned (and adapted) by the API layer.

    body_storage holds storage-format XHTML when available; it is "" in the
    snapshot (bodies are stored as blobs and read back by build.py).
    """

    id: str = ""
    title: str = ""
    space_id: str = ""
    parent_id: str = ""
    parent_type: str = ""
    position: int = 0
    status: str = "current"
    body_storage: str = ""
    version: PageVersion = PageVersion()
    web_url: str = ""


class Attachment(ApiModel):
    """A page attachment as returned by the API layer."""

    id: str = ""
    title: str = ""
    media_type: str = ""
    file_size: int = 0
    page_id: str = ""
    download_url: str = ""
    version: PageVersion = PageVersion()

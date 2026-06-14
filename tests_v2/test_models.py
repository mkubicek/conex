"""Tests for conex.errors and conex.models.

Coverage targets (per spec):
- Every model: explicit null on every field -> field default
- model_dump round-trip for every model
- int ids -> str coercion for every str id field
- Missing keys -> defaults (constructing from empty dict or partial dict)
- Junk nested shapes: {"version": None}, {"version": {}}
- frozen=True: attribute reassignment raises pydantic ValidationError
- NullTolerantModel is non-frozen (store base class)
- errors.py: ApiError carries status and url; hierarchy is correct
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

import conex
from conex.errors import (
    ApiError,
    AuthError,
    ConfigError,
    ConexError,
    GitError,
    LockHeldError,
)
from conex.models import (
    ApiModel,
    Attachment,
    Folder,
    NullTolerantModel,
    Page,
    PageVersion,
    Space,
)


# ---------------------------------------------------------------------------
# __version__
# ---------------------------------------------------------------------------


def test_version():
    # v2 line; exact patch level may vary by install vs source fallback.
    assert conex.__version__.startswith("2.")


# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------


class TestErrorHierarchy:
    def test_conex_error_is_exception(self):
        assert issubclass(ConexError, Exception)

    def test_config_error(self):
        assert issubclass(ConfigError, ConexError)

    def test_auth_error(self):
        assert issubclass(AuthError, ConexError)

    def test_api_error_hierarchy(self):
        assert issubclass(ApiError, ConexError)

    def test_lock_held_error(self):
        assert issubclass(LockHeldError, ConexError)

    def test_git_error(self):
        assert issubclass(GitError, ConexError)

    def test_api_error_carries_status_and_url(self):
        err = ApiError("bad request", status=400, url="https://example.com/api")
        assert err.status == 400
        assert err.url == "https://example.com/api"
        assert "bad request" in str(err)

    def test_api_error_status_none_by_default(self):
        err = ApiError("oops")
        assert err.status is None
        assert err.url == ""

    def test_api_error_status_429(self):
        err = ApiError("rate limited", status=429, url="https://x.com")
        assert err.status == 429


# ---------------------------------------------------------------------------
# PageVersion — per-field null -> default, round-trip, int coercion
# ---------------------------------------------------------------------------

_PAGE_VERSION_FIELDS = ["number", "created_at", "message", "author_id"]
_PAGE_VERSION_DEFAULTS = {
    "number": 0,
    "created_at": "",
    "message": "",
    "author_id": "",
}


@pytest.mark.parametrize("field", _PAGE_VERSION_FIELDS)
def test_page_version_null_field_yields_default(field):
    """Explicit None on any PageVersion field produces the field default."""
    pv = PageVersion.model_validate({field: None})
    assert getattr(pv, field) == _PAGE_VERSION_DEFAULTS[field]


def test_page_version_all_nulls_yield_defaults():
    pv = PageVersion.model_validate({f: None for f in _PAGE_VERSION_FIELDS})
    for field, default in _PAGE_VERSION_DEFAULTS.items():
        assert getattr(pv, field) == default


def test_page_version_empty_dict_yields_defaults():
    pv = PageVersion.model_validate({})
    for field, default in _PAGE_VERSION_DEFAULTS.items():
        assert getattr(pv, field) == default


def test_page_version_round_trip():
    pv = PageVersion(number=3, created_at="2024-01-01T00:00:00Z", message="msg", author_id="u1")
    assert PageVersion.model_validate(pv.model_dump()) == pv


def test_page_version_frozen():
    pv = PageVersion()
    with pytest.raises((ValidationError, TypeError)):
        pv.number = 99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Space — per-field null -> default, round-trip, int id coercion
# ---------------------------------------------------------------------------

_SPACE_FIELDS = ["id", "key", "name", "homepage_id"]
_SPACE_DEFAULTS = {"id": "", "key": "", "name": "", "homepage_id": ""}


@pytest.mark.parametrize("field", _SPACE_FIELDS)
def test_space_null_field_yields_default(field):
    s = Space.model_validate({field: None})
    assert getattr(s, field) == _SPACE_DEFAULTS[field]


def test_space_all_nulls_yield_defaults():
    s = Space.model_validate({f: None for f in _SPACE_FIELDS})
    for field, default in _SPACE_DEFAULTS.items():
        assert getattr(s, field) == default


def test_space_empty_dict_yields_defaults():
    s = Space.model_validate({})
    for field, default in _SPACE_DEFAULTS.items():
        assert getattr(s, field) == default


def test_space_round_trip():
    s = Space(id="123", key="MYKEY", name="My Space", homepage_id="456")
    assert Space.model_validate(s.model_dump()) == s


def test_space_int_id_coercion():
    s = Space.model_validate({"id": 12345})
    assert s.id == "12345"
    assert isinstance(s.id, str)


def test_space_int_homepage_id_coercion():
    s = Space.model_validate({"homepage_id": 9999})
    assert s.homepage_id == "9999"
    assert isinstance(s.homepage_id, str)


def test_space_frozen():
    s = Space()
    with pytest.raises((ValidationError, TypeError)):
        s.id = "x"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Folder — per-field null -> default, round-trip, int id coercion
# ---------------------------------------------------------------------------

_FOLDER_FIELDS = ["id", "title", "parent_id", "position"]
_FOLDER_DEFAULTS = {"id": "", "title": "", "parent_id": "", "position": 0}


@pytest.mark.parametrize("field", _FOLDER_FIELDS)
def test_folder_null_field_yields_default(field):
    f = Folder.model_validate({field: None})
    assert getattr(f, field) == _FOLDER_DEFAULTS[field]


def test_folder_all_nulls_yield_defaults():
    f = Folder.model_validate({fld: None for fld in _FOLDER_FIELDS})
    for field, default in _FOLDER_DEFAULTS.items():
        assert getattr(f, field) == default


def test_folder_empty_dict_yields_defaults():
    f = Folder.model_validate({})
    for field, default in _FOLDER_DEFAULTS.items():
        assert getattr(f, field) == default


def test_folder_round_trip():
    f = Folder(id="f1", title="Docs", parent_id="s1", position=5)
    assert Folder.model_validate(f.model_dump()) == f


def test_folder_int_id_coercion():
    f = Folder.model_validate({"id": 777})
    assert f.id == "777"
    assert isinstance(f.id, str)


def test_folder_int_parent_id_coercion():
    f = Folder.model_validate({"parent_id": 888})
    assert f.parent_id == "888"


def test_folder_frozen():
    f = Folder()
    with pytest.raises((ValidationError, TypeError)):
        f.title = "x"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Page — per-field null -> default, round-trip, nested version shapes
# ---------------------------------------------------------------------------

_PAGE_FIELDS = [
    "id", "title", "space_id", "parent_id", "parent_type",
    "position", "status", "body_storage", "version", "web_url",
]
_PAGE_DEFAULTS = {
    "id": "",
    "title": "",
    "space_id": "",
    "parent_id": "",
    "parent_type": "",
    "position": 0,
    "status": "current",
    "body_storage": "",
    "version": PageVersion(),
    "web_url": "",
}


@pytest.mark.parametrize("field", _PAGE_FIELDS)
def test_page_null_field_yields_default(field):
    p = Page.model_validate({field: None})
    assert getattr(p, field) == _PAGE_DEFAULTS[field]


def test_page_all_nulls_yield_defaults():
    p = Page.model_validate({f: None for f in _PAGE_FIELDS})
    for field, default in _PAGE_DEFAULTS.items():
        assert getattr(p, field) == default


def test_page_empty_dict_yields_defaults():
    p = Page.model_validate({})
    for field, default in _PAGE_DEFAULTS.items():
        assert getattr(p, field) == default


def test_page_round_trip():
    p = Page(
        id="p1",
        title="Home",
        space_id="s1",
        parent_id="",
        parent_type="",
        position=1,
        status="current",
        body_storage="<p>hello</p>",
        version=PageVersion(number=2, created_at="2024-01-01T00:00:00Z"),
        web_url="https://example.atlassian.net/wiki/spaces/X/pages/p1",
    )
    assert Page.model_validate(p.model_dump()) == p


def test_page_int_id_coercion():
    p = Page.model_validate({"id": 42})
    assert p.id == "42"
    assert isinstance(p.id, str)


def test_page_int_space_id_coercion():
    p = Page.model_validate({"space_id": 100})
    assert p.space_id == "100"


def test_page_int_parent_id_coercion():
    p = Page.model_validate({"parent_id": 200})
    assert p.parent_id == "200"


def test_page_version_none_yields_default_pageversion():
    """{'version': None} must produce PageVersion() — not a crash."""
    p = Page.model_validate({"version": None})
    assert p.version == PageVersion()


def test_page_version_empty_dict_yields_defaults():
    """{'version': {}} produces PageVersion with all defaults."""
    p = Page.model_validate({"version": {}})
    assert p.version == PageVersion()


def test_page_version_partial_dict():
    p = Page.model_validate({"version": {"number": 5}})
    assert p.version.number == 5
    assert p.version.created_at == ""


def test_page_frozen():
    p = Page()
    with pytest.raises((ValidationError, TypeError)):
        p.title = "changed"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Attachment — per-field null -> default, round-trip, nested version shapes
# ---------------------------------------------------------------------------

_ATTACHMENT_FIELDS = [
    "id", "title", "media_type", "file_size", "page_id",
    "download_url", "version",
]
_ATTACHMENT_DEFAULTS = {
    "id": "",
    "title": "",
    "media_type": "",
    "file_size": 0,
    "page_id": "",
    "download_url": "",
    "version": PageVersion(),
}


@pytest.mark.parametrize("field", _ATTACHMENT_FIELDS)
def test_attachment_null_field_yields_default(field):
    a = Attachment.model_validate({field: None})
    assert getattr(a, field) == _ATTACHMENT_DEFAULTS[field]


def test_attachment_all_nulls_yield_defaults():
    a = Attachment.model_validate({f: None for f in _ATTACHMENT_FIELDS})
    for field, default in _ATTACHMENT_DEFAULTS.items():
        assert getattr(a, field) == default


def test_attachment_empty_dict_yields_defaults():
    a = Attachment.model_validate({})
    for field, default in _ATTACHMENT_DEFAULTS.items():
        assert getattr(a, field) == default


def test_attachment_round_trip():
    a = Attachment(
        id="att1",
        title="diagram.png",
        media_type="image/png",
        file_size=1024,
        page_id="p1",
        download_url="/wiki/download/att1",
        version=PageVersion(number=1),
    )
    assert Attachment.model_validate(a.model_dump()) == a


def test_attachment_int_id_coercion():
    a = Attachment.model_validate({"id": 555})
    assert a.id == "555"
    assert isinstance(a.id, str)


def test_attachment_int_page_id_coercion():
    a = Attachment.model_validate({"page_id": 333})
    assert a.page_id == "333"


def test_attachment_version_none_yields_default():
    """{'version': None} must produce PageVersion(), not a crash."""
    a = Attachment.model_validate({"version": None})
    assert a.version == PageVersion()


def test_attachment_version_empty_dict_yields_defaults():
    """{'version': {}} must produce PageVersion with all defaults."""
    a = Attachment.model_validate({"version": {}})
    assert a.version == PageVersion()


def test_attachment_frozen():
    a = Attachment()
    with pytest.raises((ValidationError, TypeError)):
        a.title = "x"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Junk / adversarial nested shapes
# ---------------------------------------------------------------------------


def test_page_version_junk_number_none():
    """None on a nested int field falls back to int default (0)."""
    pv = PageVersion.model_validate({"number": None})
    assert pv.number == 0


def test_page_version_string_number_passthrough():
    """A string value for an int field is not coerced (pydantic handles it)."""
    pv = PageVersion.model_validate({"number": "3"})
    assert pv.number == 3


def test_page_nested_version_all_null_fields():
    p = Page.model_validate({
        "version": {
            "number": None,
            "created_at": None,
            "message": None,
            "author_id": None,
        }
    })
    assert p.version == PageVersion()


def test_attachment_nested_version_all_null_fields():
    a = Attachment.model_validate({
        "version": {
            "number": None,
            "created_at": None,
            "message": None,
            "author_id": None,
        }
    })
    assert a.version == PageVersion()


# ---------------------------------------------------------------------------
# NullTolerantModel — non-frozen, suitable for store models
# ---------------------------------------------------------------------------


class _SampleStoreModel(NullTolerantModel):
    """A minimal store-like model for testing NullTolerantModel behavior."""
    name: str = ""
    count: int = 0


def test_null_tolerant_model_is_not_frozen():
    """Store models must be mutable (build.py mutates copies)."""
    m = _SampleStoreModel()
    m.name = "updated"  # must NOT raise
    assert m.name == "updated"


def test_null_tolerant_model_null_coercion():
    m = _SampleStoreModel.model_validate({"name": None, "count": None})
    assert m.name == ""
    assert m.count == 0


def test_null_tolerant_model_int_str_coercion():
    m = _SampleStoreModel.model_validate({"name": 42})
    assert m.name == "42"
    assert isinstance(m.name, str)


# ---------------------------------------------------------------------------
# ApiModel base class assertions
# ---------------------------------------------------------------------------


def test_api_model_is_null_tolerant_model():
    assert issubclass(ApiModel, NullTolerantModel)


def test_api_model_is_frozen():
    """Confirm the ApiModel config carries frozen=True."""
    assert ApiModel.model_config.get("frozen") is True


# ---------------------------------------------------------------------------
# Cross-model: round-trip with all defaults
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("model_cls", [PageVersion, Space, Folder, Page, Attachment])
def test_default_instance_round_trips(model_cls):
    """An all-default instance survives a model_dump() -> model_validate() round-trip."""
    instance = model_cls()
    assert model_cls.model_validate(instance.model_dump()) == instance


@pytest.mark.parametrize("model_cls", [PageVersion, Space, Folder, Page, Attachment])
def test_empty_dict_produces_valid_instance(model_cls):
    """model_validate({}) must always succeed (every field has a default)."""
    model_cls.model_validate({})


# ---------------------------------------------------------------------------
# int -> str coercion: comprehensive id fields
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("model_cls,field,int_val", [
    (Space, "id", 1),
    (Space, "homepage_id", 2),
    (Folder, "id", 3),
    (Folder, "parent_id", 4),
    (Page, "id", 5),
    (Page, "space_id", 6),
    (Page, "parent_id", 7),
    (PageVersion, "author_id", 8),
    (Attachment, "id", 9),
    (Attachment, "page_id", 10),
])
def test_int_coerced_to_str(model_cls, field, int_val):
    instance = model_cls.model_validate({field: int_val})
    value = getattr(instance, field)
    assert isinstance(value, str)
    assert value == str(int_val)

import pytest

from earmark.recipients import (
    ALL,
    ELLIPSIS,
    NOT_ALLOWED_MESSAGE,
    REFUSES_ALL_MESSAGE,
    RecipientPolicy,
    RecipientRefused,
    mask_address,
    normalize_address,
)


@pytest.mark.parametrize(
    ("raw", "normal"),
    [
        ("alice@example.org", "alice@example.org"),
        ("  alice@example.org\n", "alice@example.org"),
        ("Alice@Example.ORG", "Alice@example.org"),
        ("a.b+tag@sub.example.co.uk", "a.b+tag@sub.example.co.uk"),
        ("a!#$%&'*+/=?^_`{|}~-z@x.io", "a!#$%&'*+/=?^_`{|}~-z@x.io"),
        ("x@1a.org", "x@1a.org"),
        ("x@a-b.example", "x@a-b.example"),
        ("l" * 64 + "@example.org", "l" * 64 + "@example.org"),
    ],
)
def test_normalize_address_valid(raw: str, normal: str) -> None:
    assert normalize_address(raw) == normal


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "alice",
        "alice@",
        "@example.org",
        "a@b",
        "a@@example.org",
        "a@b@example.org",
        "a@example.org\r\nBcc: x@y.org",
        "a@example.org\nBcc: x@y.org",
        "a@example.org, b@example.org",
        "a b@example.org",
        "a@exa mple.org",
        '"quoted"@example.org',
        "<a@example.org>",
        "Name <a@example.org>",
        "a;b@example.org",
        "a@[127.0.0.1]",
        "a@127.0.0.1",
        "a@example.123",
        "é@example.org",
        "a@exämple.org",
        "a@例え.jp",
        ".a@example.org",
        "a.@example.org",
        "a..b@example.org",
        "a@.example.org",
        "a@example..org",
        "a@-example.org",
        "a@example-.org",
        "l" * 65 + "@example.org",
        "a@" + "d" * 64 + ".org",
        "a@" + ".".join(["a" * 60] * 5) + ".org",
        "a" * 64 + "@" + "b" * 63 + "." + "c" * 63 + "." + "d" * 63 + "." + "e" * 60 + ".org",
    ],
)
def test_normalize_address_invalid(bad: str) -> None:
    with pytest.raises(ValueError, match="not an email address") as info:
        normalize_address(bad)
    assert bad.strip() == "" or bad.strip() not in str(info.value)


def test_normalize_address_total_length_cap() -> None:
    local = "l" * 64
    domain_ok = ".".join(["d" * 60] * 3) + ".org"
    ok = f"{local}@{domain_ok}"
    assert len(ok) <= 254
    assert normalize_address(ok) == ok
    assert len(local) + 1 + len(".".join(["d" * 63] * 3) + ".org") > 254
    with pytest.raises(ValueError, match="not an email address"):
        normalize_address(f"{local}@" + ".".join(["d" * 63] * 3) + ".org")


def test_domain_case_insensitive_local_exact() -> None:
    policy = RecipientPolicy.from_setting(("Alice@example.org",))
    assert policy.allows(normalize_address("Alice@EXAMPLE.ORG"))
    assert not policy.allows(normalize_address("alice@example.org"))


def test_mask_address() -> None:
    assert mask_address("alice@example.org") == f"a{ELLIPSIS}@example.org"
    assert mask_address("z.secret@example.org") == "z…@example.org"
    assert mask_address("x@example.org") == "x…@example.org"
    assert mask_address("no-at-sign") == ELLIPSIS
    assert mask_address("") == ELLIPSIS
    assert mask_address("@example.org") == ELLIPSIS


def test_policy_from_setting() -> None:
    nobody = RecipientPolicy.from_setting(())
    assert nobody.refuses_all
    assert nobody.describe() == ""
    assert not nobody.allows("a@example.org")

    everyone = RecipientPolicy.from_setting((ALL,))
    assert everyone.allow_all
    assert not everyone.refuses_all
    assert everyone.describe() == "all"
    assert everyone.allows("anyone@example.net")

    some = RecipientPolicy.from_setting(("b@example.org", "A@EXAMPLE.org"))
    assert not some.refuses_all
    assert some.addresses == {"b@example.org", "A@example.org"}
    assert some.describe() == "A…@example.org, b…@example.org"
    assert some.allows("b@example.org")
    assert not some.allows("c@example.org")


def test_policy_check_messages() -> None:
    with pytest.raises(RecipientRefused) as refused:
        RecipientPolicy.from_setting(()).check("a@example.org")
    assert refused.value.message == REFUSES_ALL_MESSAGE
    assert refused.value.message == (
        "no recipients are allowed yet: set EARMARK_ALLOWED_RECIPIENTS on the server"
    )
    allow = RecipientPolicy.from_setting(("a@example.org",))
    allow.check("a@example.org")
    with pytest.raises(RecipientRefused) as other:
        allow.check("zelda@example.org")
    assert other.value.message == NOT_ALLOWED_MESSAGE
    assert "zelda" not in other.value.message
    RecipientPolicy.from_setting((ALL,)).check("anyone@example.org")

import pytest

from app.sources import parse_paste, resolve, social_url, split_paste


def one(text, existing=frozenset()):
    [c] = parse_paste(text, existing)
    return c


@pytest.mark.parametrize("text, kind, handle, url", [
    ("https://www.instagram.com/foo/?hl=ja", "instagram", "foo", "https://www.instagram.com/foo/"),
    ("instagram.com/Foo.Bar", "instagram", "foo.bar", "https://www.instagram.com/foo.bar/"),
    ("https://www.instagram.com/stories/foo/123/", "instagram", "foo", "https://www.instagram.com/foo/"),
    ("https://x.com/foo?s=20", "x", "foo", "https://x.com/foo"),
    ("twitter.com/LiSA_OLiVE", "x", "LiSA_OLiVE", "https://x.com/lisa_olive"),
    ("https://mobile.twitter.com/foo/status/123", "x", "foo", "https://x.com/foo"),
    ("http://x.com/foo/", "x", "foo", "https://x.com/foo"),
])
def test_social_links(text, kind, handle, url):
    c = one(text)
    assert (c.status, c.kind, c.handle, c.url) == ("new", kind, handle, url)


@pytest.mark.parametrize("text", [
    "https://www.instagram.com/p/Cabc123/",
    "instagram.com/reel/xyz",
    "https://x.com/i/status/123",
    "https://x.com/search?q=foo",
    "https://x.com/",
    "https://x.com/waytoolongforanxhandle",
    "link!",
])
def test_rejected(text):
    assert one(text).status == "invalid"


def test_bare_handle_is_ambiguous():
    for text in ("@foo_bar", "foo_bar"):
        c = one(text)
        assert c.status == "ambiguous" and c.kind is None and c.handle == "foo_bar"
        assert c.choices == ("instagram", "x")


def test_handle_that_can_only_be_instagram():
    for text in ("@foo.bar", "@a_really_long_instagram_name"):
        c = one(text)
        assert (c.status, c.kind) == ("new", "instagram")
        assert c.choices == ()


@pytest.mark.parametrize("text, url, label", [
    ("Example.JP/Live/", "https://example.jp/Live/", "Schedule"),     # slash kept: Sony 404s without it
    ("Example.JP/Live", "https://example.jp/Live", "Schedule"),
    ("https://example.jp/?utm_source=x&page=2#top", "https://example.jp/?page=2", ""),
    ("http://old.example.com/schedule.html", "http://old.example.com/schedule.html", "Schedule"),
    ("https://example.jp", "https://example.jp/", ""),
    ("www.example.co.jp/news", "https://www.example.co.jp/news", ""),
])
def test_websites(text, url, label):
    c = one(text)
    assert (c.status, c.kind, c.url, c.label) == ("new", "website", url, label)
    assert c.choices == ("website", "fanclub", "other")


def test_split_handles_mixed_separators():
    assert split_paste("a.jp, b.jp\n\n@c、@d\t<https://e.jp>") == ["a.jp", "b.jp", "@c", "@d", "https://e.jp"]


def test_duplicates_against_artist_and_within_paste():
    existing = {("x", "https://x.com/foo"), ("fanclub", "https://fc.example.jp/")}
    cs = parse_paste("x.com/FOO\nfc.example.jp\ninstagram.com/bar\ninstagram.com/bar/", existing)
    assert [c.status for c in cs] == ["duplicate", "duplicate", "new", "duplicate"]
    assert [c.message for c in cs][:2] == ["Already added", "Already added"]
    assert cs[3].message == "Pasted twice"


def test_a_page_with_or_without_its_trailing_slash_is_one_page():
    cs = parse_paste("sh-nzk.net/live/\nhttps://sh-nzk.net/live", {("website", "https://sh-nzk.net/info")})
    assert [(c.status, c.url) for c in cs] == [("new", "https://sh-nzk.net/live/"),
                                               ("duplicate", "https://sh-nzk.net/live")]
    assert parse_paste("sh-nzk.net/info/", {("website", "https://sh-nzk.net/info")})[0].message == "Already added"


def test_resolve_applies_picks_and_reports_missing():
    cs = parse_paste("@foo\nexample.jp\n@bar")
    ready, unresolved = resolve(cs, {0: "x", 1: "fanclub"})
    assert unresolved == [2]
    assert [(c.kind, c.url) for c in ready] == [("x", social_url("x", "foo")), ("fanclub", "https://example.jp/")]


def test_resolve_ignores_bogus_pick_and_skips_existing():
    cs = parse_paste("@foo")
    assert resolve(cs, {0: "website"})[1] == [0]
    ready, unresolved = resolve(cs, {0: "instagram"}, {("instagram", "https://www.instagram.com/foo/")})
    assert ready == [] and unresolved == []

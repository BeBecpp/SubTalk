import pytest

from subtalk.zoom.url import CaptionUrlError, parse_caption_url

REAL = "https://wmcc.zoom.us/closedcaption?id=123&ns=abc&expire=86400&sparams=id%2Cns%2Cexpire&signature=SIG"


def test_real_url_requires_opt_in():
    with pytest.raises(CaptionUrlError, match="disabled"):
        parse_caption_url(REAL, allow_real_zoom=False, allow_mock=True)


def test_real_url_parsed_and_seq_lang_stripped():
    u = parse_caption_url(REAL + "&seq=41&lang=en-US", allow_real_zoom=True, allow_mock=False)
    assert "seq=" not in u.base and "lang=" not in u.base
    assert u.post_url(42).endswith("&seq=42")
    assert u.seq_url().startswith("https://wmcc.zoom.us/closedcaption/seq?")
    assert u.host == "wmcc.zoom.us"


def test_token_never_in_repr_or_str():
    u = parse_caption_url(REAL, allow_real_zoom=True, allow_mock=False)
    assert "SIG" not in repr(u) and "SIG" not in str(u)


@pytest.mark.parametrize(
    "bad",
    [
        "http://wmcc.zoom.us/closedcaption?id=1&ns=a&expire=1&signature=s",  # not https
        "https://evil.example.com/closedcaption?id=1&ns=a&expire=1&signature=s",  # wrong host
        "https://zoom.us.evil.com/closedcaption?id=1&ns=a&expire=1&signature=s",  # suffix trick
        "https://wmcc.zoom.us/other?id=1&ns=a&expire=1&signature=s",  # wrong path
        "https://wmcc.zoom.us/closedcaption?id=1&ns=a&expire=1",  # missing signature
        "",
    ],
)
def test_rejects_bad_urls(bad):
    with pytest.raises(CaptionUrlError):
        parse_caption_url(bad, allow_real_zoom=True, allow_mock=False)


def test_mock_host_only_when_allowed():
    url = "http://127.0.0.1:8000/mock-zoom/closedcaption?id=m&ns=m&expire=1&signature=m"
    assert parse_caption_url(url, allow_real_zoom=False, allow_mock=True).is_mock
    with pytest.raises(CaptionUrlError):
        parse_caption_url(url, allow_real_zoom=True, allow_mock=False)

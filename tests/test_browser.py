import playwright.sync_api
import pytest

from app.scan_web import Browser, FetchError


class FakePW:
    def __init__(self, log):
        self.log = log
        self.chromium = self

    def start(self):
        self.log.append("start")
        return self

    def launch(self):
        raise RuntimeError("BrowserType.launch: Executable doesn't exist at /x/chrome\nmore")

    def stop(self):
        self.log.append("stop")


def test_chromium_that_wont_start_fails_every_page_with_the_reason(monkeypatch):
    log = []
    monkeypatch.setattr(playwright.sync_api, "sync_playwright", lambda: FakePW(log))
    b = Browser()
    for url in ("https://a.example/", "https://b.example/"):
        with pytest.raises(FetchError, match="playwright install chromium") as e:
            b.html(url)
        assert not e.value.transient
    assert log == ["start", "stop"]          # started once, stopped cleanly, never restarted
    b.close()

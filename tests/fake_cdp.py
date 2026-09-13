"""CDP 测试假件：bs4 实现的 Page/Element/Keyboard，与 playwright sync API 子集同构。

只实现 CdpAdapterBase 用到的原语：goto / query_selector(_all) / fill / click /
set_input_files / inner_text / keyboard.insert_text / screenshot。
选择器解析交给 soupsieve（标准 CSS）——回放 fixture HTML 即可验证选择器与 flow 顺序。
"""

from __future__ import annotations

from bs4 import BeautifulSoup


class FakeKeyboard:
    def __init__(self, page):
        self._page = page

    def insert_text(self, text: str) -> None:
        self._page.actions.append(("type", self._page._last_clicked, text))


class FakeElement:
    def __init__(self, tag, sel: str, page: "FakePage"):
        self.tag = tag
        self._sel = sel
        self._page = page

    def inner_text(self) -> str:
        return self.tag.get_text(strip=True)

    def click(self) -> None:
        self._page.actions.append(("click", self._sel))
        self._page._last_clicked = self._sel

    def set_input_files(self, paths) -> None:
        self._page.actions.append(("upload", self._sel, tuple(paths)))


class FakePage:
    def __init__(self, html: str):
        self.soup = BeautifulSoup(html, "html.parser")
        self.actions: list[tuple] = []
        self.url = "about:blank"
        self.keyboard = FakeKeyboard(self)
        self._last_clicked: str | None = None

    # playwright Page 子集
    def goto(self, url: str, timeout: int = 0, wait_until: str | None = None) -> None:
        self.url = url
        self.actions.append(("goto", url))

    def query_selector(self, sel: str):
        el = self.soup.select_one(sel)
        return FakeElement(el, sel, self) if el else None

    def query_selector_all(self, sel: str):
        return [FakeElement(t, sel, self) for t in self.soup.select(sel)]

    def fill(self, sel: str, text: str) -> None:
        self.actions.append(("fill", sel, text))

    def click(self, sel: str) -> None:
        self.actions.append(("click", sel))
        self._last_clicked = sel

    def set_input_files(self, sel: str, paths) -> None:
        self.actions.append(("upload", sel, tuple(paths)))

    def screenshot(self, full_page: bool = False) -> bytes:
        self.actions.append(("screenshot",))
        return b"png-bytes"


class FakeContext:
    def __init__(self, html: str):
        self._html = html
        self.pages: list[FakePage] = []

    def new_page(self) -> FakePage:
        p = FakePage(self._html)
        self.pages.append(p)
        return p


class FakeBrowser:
    """Fleet connector 注入件：page() 语义由 CdpBrowserHandle 使用。"""

    def __init__(self, html: str):
        self.contexts = [FakeContext(html)]
        self.disconnected = False
        self.close_called = False

    def new_context(self) -> FakeContext:  # 理论不会走到（contexts 非空）
        return self.contexts[0]

    def disconnect(self) -> None:
        self.disconnected = True  # 红线：只允许 disconnect

    def close(self) -> None:  # pragma: no cover  被调用即测试失败
        self.close_called = True

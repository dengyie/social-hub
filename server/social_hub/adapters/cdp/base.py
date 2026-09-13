"""CDP 通道适配器基类：附着舰队 → 打开发布页 → 拦截登录/验证码 → 平台 flow。

设计约定（§6.3/§6.4）：
- 每个平台子类只维护 `selectors` 注册表 + flow 逻辑；选择器 miss 一律 PermanentError
  （选择器是平台改版的探测点，宁可 failed 人工校准也不静默瞎点）。
- 验证码/滑块 → CaptchaWaitError（captcha_wait，人工处理）；未登录 → NeedsLoginError。
- 流程先做纯参数校验（标题/封面），再碰浏览器——契约测试可在零浏览器环境跑。
- 选择器需要真机校准：`shub doctor --platform X --account A`（§6.9 canary）。
"""

from __future__ import annotations

import json

from ..base import (
    ActionContext,
    CaptchaWaitError,
    CredentialsError,
    Evidence,
    NeedsLoginError,
    PermanentError,
    PlatformAdapter,
)


class CdpAdapterBase(PlatformAdapter):
    lane = "cdp"
    login_url: str = ""  # 登录页（扫码/短信，人工完成）
    publish_url: str = ""  # 创作者中心发布页
    publish_button_text: str = "发布"  # 发布按钮可见文案（比 class 稳定）
    # 平台选择器注册表（子类覆写）。标注 [calibrate] 的键为待真机校准项。
    selectors: dict[str, str] = {}
    # 拦截标记：出现在发布页即任务冻结/转人工
    captcha_markers: tuple[str, ...] = ()
    login_markers: tuple[str, ...] = ()
    verify_url: str = ""  # 内容管理页（verify 复查用，可空=仅凭提交回执）

    # ---- 参数校验（浏览器无关，契约测试直测）----
    def _validated_snapshot(self, ctx: ActionContext) -> dict:
        payload = json.loads(ctx.task.payload or "{}")
        variant_id = payload.get("variant_id")
        if not variant_id:
            raise PermanentError("task payload missing variant_id")
        from ...models import DraftVariant, Media

        with ctx.db() as session:
            variant = session.get(DraftVariant, variant_id)
            if variant is None:
                raise PermanentError(f"variant #{variant_id} not found")
            cover = session.get(Media, variant.cover_media_id) if variant.cover_media_id else None
            snapshot = {
                "id": variant.id,
                "title": variant.title,
                "body": variant.body,
                "tags": variant.tags,
                "cover_path": str(ctx.media_dir / cover.path) if cover else None,
            }
        if len(snapshot["title"]) > self.capabilities.max_title:
            raise PermanentError(
                f"title too long for {self.platform} ({len(snapshot['title'])} > {self.capabilities.max_title})"
            )
        return snapshot

    # ---- 选择器原语（playwright Page 与测试 FakePage 同构）----
    def _sel(self, name: str) -> str:
        sel = self.selectors.get(name)
        if not sel:
            raise PermanentError(f"{self.platform}: selector '{name}' not registered (见 selectors 注册表)")
        return sel

    def _guard(self, page) -> None:
        """发布页拦截：验证码 → captcha_wait；未登录 → needs_login。"""
        for marker in self.captcha_markers:
            if page.query_selector(marker):
                raise CaptchaWaitError(f"{self.platform}: captcha/slider detected ({marker})")
        for marker in self.login_markers:
            if page.query_selector(marker):
                raise NeedsLoginError(f"{self.platform}: not logged in ({marker})")

    def has(self, page, name: str) -> bool:
        return page.query_selector(self._sel(name)) is not None

    def fill(self, page, name: str, text: str) -> None:
        page.fill(self._sel(name), text)

    def type_text(self, page, name: str, text: str) -> None:
        """contenteditable 富文本编辑器：click 后用键盘注入（fill 对 DraftJS 无效）。"""
        page.click(self._sel(name))
        page.keyboard.insert_text(text)

    def click(self, page, name: str) -> None:
        page.click(self._sel(name))

    def click_button_by_text(self, page, text: str) -> None:
        """按可见文案点按钮（比 class 稳定）；文案做空白归一化（真实页面常见「发 布」）。"""
        for el in page.query_selector_all("button, [role=button]"):
            if text in "".join((el.inner_text() or "").split()):
                el.click()
                return
        raise PermanentError(f"{self.platform}: button with text '{text}' not found")

    def upload(self, page, name: str, paths: list[str]) -> None:
        page.set_input_files(self._sel(name), paths)

    # ---- 生命周期 ----
    def _open(self, ctx: ActionContext, url: str):
        from ...core.fleet import get_fleet

        handle = get_fleet().ensure(ctx.account)
        page = handle.page(url)
        self._guard(page)
        return handle, page

    def check_login(self, ctx: ActionContext) -> str:
        try:
            _, page = self._open(ctx, self.publish_url)
        except NeedsLoginError:
            return "expired"
        except (RuntimeError, PermanentError) as e:  # chrome 未装/选择器未注册等环境问题
            raise PermanentError(f"{self.platform} check_login env error: {e}") from e
        return "ok"

    def login_interactive(self, ctx: ActionContext) -> dict:
        """打开登录页并截图（含二维码）；人工扫码后重跑任务。远端场景把 base64 推到 WebUI。"""
        if not self.login_url:
            raise PermanentError(f"{self.platform}: login_url not configured")
        handle, page = self._open(ctx, self.login_url)
        try:
            b64 = page.screenshot()
        except Exception:
            b64 = b""
        return {"note": "scan the QR in the attached browser window, then requeue the task",
                "screenshot_bytes": len(b64 or b""), "qr_available": bool(b64)}

    # ---- 发布主链路（模板方法）----
    def _commit_receipt(self, ctx: ActionContext, receipt: dict) -> None:
        data = json.loads(ctx.task.evidence or "{}")
        data["publish_receipt"] = receipt
        ctx.task.evidence = json.dumps(data, ensure_ascii=False)
        if ctx.session is not None:
            ctx.session.commit()

    def publish(self, ctx: ActionContext) -> "PublishResult":
        """模板流程：参数校验（零浏览器）→ 断点续跑 → flow（不点发布）→ 点发布 → 回执落盘。

        已有 publish_receipt = 上次运行已点击发布 → 绝不重做 flow（CDP 无平台侧草稿箱，
        这是防重复发布的唯一闸门；点击到落盘之间的窗口见设计文档风险 #5）。
        """
        from ..base import PublishResult

        snap = self._validated_snapshot(ctx)
        prior = json.loads(ctx.task.evidence or "{}")
        if prior.get("publish_receipt"):
            return PublishResult(submitted=True, detail=prior["publish_receipt"])
        handle, page = self._open(ctx, self.publish_url)
        try:
            detail = self._flow(page, snap, ctx)
            self.click_button_by_text(page, self.publish_button_text)
        finally:
            handle.close()
        receipt = {"platform": self.platform, **detail}
        self._commit_receipt(ctx, receipt)
        return PublishResult(submitted=True, detail=receipt)

    def _flow(self, page, snap: dict, ctx: ActionContext) -> dict:
        """平台差异点：上传/填内容，【不要点发布按钮】（模板负责）。"""
        raise NotImplementedError

    def verify(self, ctx: ActionContext) -> Evidence:
        """默认核验 = 提交回执（publish flow 落在 evidence 的收据）。

        平台若已有内容管理页选择器，可覆写为页面级核验（design §8.1）。
        """
        evidence = json.loads(ctx.task.evidence or "{}")
        receipt = evidence.get("publish_receipt") or {}
        if not receipt:
            raise PermanentError("verify: task evidence missing publish_receipt")
        return Evidence(ok=True, url=receipt.get("note_url"), raw=receipt)

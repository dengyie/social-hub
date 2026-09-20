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
import time

from ..base import (
    ActionContext,
    CaptchaWaitError,
    CredentialsError,
    Evidence,
    NeedsLoginError,
    PermanentError,
    PlatformAdapter,
    TransientError,
)


class CdpAdapterBase(PlatformAdapter):
    lane = "cdp"
    login_url: str = ""  # 登录页（扫码/短信，人工完成）
    publish_url: str = ""  # 创作者中心发布页
    publish_button_text: str = "发布"  # 发布按钮可见文案（比 class 稳定）
    confirm_button_text: str = ""  # 发布后确认弹窗（如快手 ant-modal「确认」），空=无
    confirm_wait_seconds: float = 10.0  # 确认弹窗有界等待
    confirm_poll_interval: float = 0.5
    login_redirect_marker: str = ""  # 未登录时 URL 会跳到该子串（如 "/login"、"passport."）
    # 平台选择器注册表（子类覆写）。标注 [calibrate] 的键为待真机校准项；
    # 来源标注：XiaohongshuSkills（2026-03 真机验证）/ social-auto-upload / 预置待校准。
    selectors: dict[str, str] = {}
    # 拦截标记：出现在发布页即任务冻结/转人工。支持两种写法：
    #   "css|selector"  → page.query_selector（CSS）
    #   "text:文案"     → 可见文本包含匹配（扫 button/div/span/p/a）
    captcha_markers: tuple[str, ...] = ()
    login_markers: tuple[str, ...] = ()
    verify_url: str = ""  # 内容管理页（verify 复查用，可空=仅凭提交回执）

    # 媒体 kind 约束（review P1）：本平台允许的媒体 kind；空 = 纯文本平台（封面忽略）。
    # 此前只有 bili 校验 kind，video-only 平台只判"有没有封面"——图片会被投成视频
    # 而 flow 一路"成功"。约束在碰浏览器前校验（`_validated_snapshot`），零副作用。
    media_kinds: tuple[str, ...] = ()
    media_required: bool = False  # 是否必须携带媒体（纯文本平台 False）

    # ---- 参数校验（浏览器无关，契约测试直测）----
    def _validated_snapshot(self, ctx: ActionContext) -> dict:
        from ..base import load_variant_snapshot

        snap = load_variant_snapshot(ctx, max_title=self.capabilities.max_title)
        self._validate_media(snap)
        return snap

    def _validate_media(self, snap: dict) -> None:
        """媒体 kind 校验：错配媒体必须在提交前拒绝（不产生平台侧副作用）。

        唯一实现见 base.require_media_kind（CDP 与 API 双通道共用，杜绝两处拷贝漂移）。
        """
        from ..base import require_media_kind

        require_media_kind(snap, platform=self.platform,
                           kinds=self.media_kinds, required=self.media_required)

    def _resolve_publish_url(self, snap: dict) -> str:
        """发布页 URL 可按媒体 kind 分流（如头条图文/视频两个发布页）。"""
        return self.publish_url

    # ---- 选择器原语（playwright Page 与测试 FakePage 同构）----
    def selector_names(self) -> list[str]:
        """可被 CSS 探测的选择器键（doctor/canary 用）；`*_text` 是文案目标不是选择器。"""
        return [k for k in self.selectors if not k.endswith("_text")]

    def _sel(self, name: str) -> str:
        sel = self.selectors.get(name)
        if not sel:
            raise PermanentError(f"{self.platform}: selector '{name}' not registered (见 selectors 注册表)")
        return sel

    def _marker_hit(self, page, marker: str) -> bool:
        if marker.startswith("text:"):
            # playwright text= 引擎一次往返完成子串匹配；此前逐元素 inner_text
            # 在真实页面是 O(元素数) 次 CDP 往返（review P2）
            return page.locator(f"text={marker[5:]}").count() > 0
        return page.query_selector(marker) is not None

    def _guard(self, page) -> None:
        """发布页拦截：验证码 → captcha_wait；未登录 → needs_login；支持 URL 重定向判定。"""
        if self.login_redirect_marker and self.login_redirect_marker in (page.url or ""):
            raise NeedsLoginError(f"{self.platform}: redirected to login ({page.url})")
        for marker in self.captcha_markers:
            if self._marker_hit(page, marker):
                raise CaptchaWaitError(f"{self.platform}: captcha/slider detected ({marker})")
        for marker in self.login_markers:
            if self._marker_hit(page, marker):
                raise NeedsLoginError(f"{self.platform}: not logged in ({marker})")

    def has(self, page, name: str) -> bool:
        return page.query_selector(self._sel(name)) is not None

    def first_has(self, page, *names: str, timeout: float = 15.0) -> str | None:
        """有界轮询返回第一个已挂载的选择器名；超时返回 None。

        根因背景：切 tab/上传文件后表单由 SPA 异步渲染，瞬时检查会竞态
        （真机 2026-09-14 小红书实证）。所有"上传后表单"类探测统一走这里。
        """
        deadline = time.monotonic() + timeout
        while True:
            for name in names:
                if self.selectors.get(name) and self.has(page, name):
                    return name
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.5)

    def require_has(self, page, *names: str, timeout: float = 15.0) -> str:
        """first_has 的硬失败版：超时即 PermanentError（选择器待校准）。"""
        name = self.first_has(page, *names, timeout=timeout)
        if not name:
            raise PermanentError(
                f"{self.platform}: {'/'.join(names)} 未命中（选择器待校准）")
        return name

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

    def click_exact_text(self, page, text: str) -> bool:
        """按精确可见文案点击任意元素（tab/频道切换用）；找不到返回 False（可容错）。

        根因修复（2026-09-14 小红书「上传图文」真机实证）：页面常存在同文案的
        视口外模板节点（x=-9726），盲点首个候选会等满默认 30s 超时卡死流程。
        修复分两层：①先按可见性 + 视口内 bounding box 预筛，正常路径一次点中；
        ②仍失败才逐候选 3s 短超时快速失败兜底（覆盖"在视口外但可滚动入视口"的
        合法元素）；TypeError 分支兼容不带 timeout 形参的测试假件。
        """
        candidates = [
            el for el in page.query_selector_all("div, span, a, button, [role=tab], [role=button]")
            if "".join((el.inner_text() or "").split()) == text
        ]
        if not candidates:
            return False
        in_viewport = []
        for el in candidates:
            is_visible = getattr(el, "is_visible", None)
            if is_visible is not None and not is_visible():
                continue
            bounding_box = getattr(el, "bounding_box", None)
            if bounding_box is not None:
                box = bounding_box()
                if box is None:  # display:none 等不可点元素
                    continue
                if box.get("x", 0) + box.get("width", 0) <= 0 or box.get("y", 0) + box.get("height", 0) <= 0:
                    continue  # 视口外（模板拷贝节点）
            in_viewport.append(el)
        ordered = in_viewport + [c for c in candidates if c not in in_viewport]
        for el in ordered:
            try:
                el.click(timeout=3000)
                return True
            except TypeError:
                try:
                    el.click()
                    return True
                except Exception:
                    continue
            except Exception:
                continue
        return False

    def _click_publish(self, page) -> None:
        """点发布：注册表有专用选择器优先，否则按文案；随后处理确认弹窗。"""
        if self.selectors.get("publish_button"):
            page.click(self._sel("publish_button"))
        else:
            self.click_button_by_text(page, self.publish_button_text)
        if self.confirm_button_text:
            self._wait_and_click_confirm(page)

    def _wait_and_click_confirm(self, page) -> None:
        """确认弹窗是发布后的异步出现（如快手视频处理中）：有界轮询。

        超时抛 TransientError——此时回执尚未落盘，退避重跑安全（重新走 flow）。
        """
        deadline = time.monotonic() + self.confirm_wait_seconds
        while True:
            for el in page.query_selector_all("button, [role=button]"):
                if self.confirm_button_text in "".join((el.inner_text() or "").split()):
                    el.click()
                    return
            if time.monotonic() >= deadline:
                raise TransientError(
                    f"{self.platform}: confirm '{self.confirm_button_text}' not shown "
                    f"within {self.confirm_wait_seconds}s")
            time.sleep(self.confirm_poll_interval)

    def upload(self, page, name: str, paths: list[str]) -> None:
        page.set_input_files(self._sel(name), paths)

    # ---- 生命周期 ----
    def _open(self, ctx: ActionContext, url: str):
        from ...core.fleet import get_fleet

        handle = get_fleet().ensure(ctx.account)
        try:
            page = handle.page(url)
            self._guard(page)
        except Exception:
            handle.close()  # 资源获取与释放对称：goto 超时/拦截也要断开连接
            raise
        return handle, page

    def check_login(self, ctx: ActionContext) -> str:
        handle = None
        try:
            handle, _page = self._open(ctx, self.publish_url)
            return "ok"
        except NeedsLoginError:
            return "expired"
        except (RuntimeError, PermanentError) as e:  # chrome 未装/选择器未注册等环境问题
            raise PermanentError(f"{self.platform} check_login env error: {e}") from e
        finally:
            if handle is not None:
                handle.close()

    def login_interactive(self, ctx: ActionContext) -> dict:
        """打开登录页截图落盘（含二维码）；人工扫码后重跑任务。远端把 qr_path 推给用户。

        注意：登录页本身就是「未登录态」，**不走 _open**（guard 会误拦），
        只开页面不判拦截。
        """
        if not self.login_url:
            raise PermanentError(f"{self.platform}: login_url not configured")
        from ...core.fleet import get_fleet
        from ...config import get_settings

        handle = get_fleet().ensure(ctx.account)
        try:
            page = handle.page(self.login_url)
            png = page.screenshot() or b""
        finally:
            handle.close()
        qr_dir = get_settings().data_dir / "qr"
        qr_dir.mkdir(parents=True, exist_ok=True)
        qr_path = qr_dir / f"{self.platform}-{ctx.account.alias}.png"
        if png:
            qr_path.write_bytes(png)
        return {"qr_path": str(qr_path) if png else None,
                "note": "scan the QR in the attached browser window (or qr_path), then requeue the task"}

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
        handle, page = self._open(ctx, self._resolve_publish_url(snap))
        try:
            detail = self._flow(page, snap, ctx)
            if ctx.preview:
                # 只填不发：不点发布、不写 publish_receipt（receipt 是防重发闸门）
                preview = {"platform": self.platform, "preview": True, **detail}
                data = json.loads(ctx.task.evidence or "{}")
                data["preview"] = preview
                ctx.task.evidence = json.dumps(data, ensure_ascii=False)
                if ctx.session is not None:
                    ctx.session.commit()
                return PublishResult(submitted=False, detail={"preview": preview})
            self._click_publish(page)
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

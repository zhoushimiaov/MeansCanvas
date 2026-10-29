# 2026-10-29 代码审查与修复

审查基准：`ffec38891454fcfa4c85b07cc137f88a363cf738`（`main`）。

本轮重点检查入口鉴权、媒体下载与 Provider 凭据传递、画布持久化与并发保存，并核对已有审计报告、回归测试和 CI。以下两项已用合成数据复现并在本 PR 修复；不代表对全部源码、外部服务或部署环境作完整安全保证。

## P1：媒体重定向转发上游凭据

- 位置：`core/security.py` 的 `safe_download_bytes()` 重定向分支；实际带凭据的调用包括 `routers/online_image.py` 的 Pollinations 图片下载。
- 触发：初始请求携带 Provider 的 `Authorization`，响应以 302 等状态重定向到另一个主机、端口，或从 HTTPS 降级到 HTTP。
- 原行为：每一跳重新使用相同的 `default_headers`。SSRF 检查会拒绝内网，但不能阻止凭据被转发给另一个合法公网地址。
- 影响：重定向目标可接收到原 Provider 的 API Key；只确认了转发行为，没有使用真实密钥，也没有证据表明已经发生泄露。
- 修复：比较 scheme、host 和有效端口；跨源后清除调用者提供的全部头，仅保留下载器默认 User-Agent，覆盖未知名称的自定义密钥头。同源重定向保留头；跨源后再跳回原站也不会重新附加凭据。逐跳 SSRF 检查继续执行。
- 兼容边界：要求跨源 CDN 继续接收原站私有头的流程将不再得到这些头，应由上游提供独立授权的下载链接。

## P1：毫秒时间戳碰撞或回拨绕过保存冲突检查

- 位置：`routers/canvas.py` 的 `save_canvas()`；调用方 `_commit_canvas_save()` 使用 `updated_at` 作为乐观并发版本。
- 触发：两个请求基于同一个版本，首次保存时墙钟没有前进；时钟校准回拨时也会出现。
- 原行为：事务锁串行化请求，但 `updated_at = now_ms()` 未保证版本递增，第二个旧快照仍能通过冲突检查并覆盖首次保存。
- 修复：在同一把锁内设置 `max(now_ms(), previous_updated_at + 1)`，保持现有 API 和毫秒时间戳格式。旧快照得到 HTTP 409，磁盘保留首次写入。
- 现有测试用等待 10ms 避开了这一边界；新增测试固定时间或让时钟回退 1000ms，不依赖 sleep。

## 验证凭证与边界

- 修复前新增回归：**5 failed / 1 passed**，退出码 1；修复后：**6 passed**，退出码 0。覆盖跨主机、跨端口、HTTPS 降级、跳回原站、同源相对路径、时间碰撞和时钟回退。
- 扩大 Python 验证：**376 passed / 1 failed**。唯一失败为 `test_all_44_tools_defined_and_assets_exist`：GitHub API 下载副本未能完整获取二进制预览图片。不能称为全套测试通过；没有跳过或修改此断言，PR 不改动图片。
- Python 编译、Ruff 严重错误检查通过；文件长度、SSRF 和美学检查通过；Node 前端回归 **9/9** 通过；i18n 检查通过（1343 keys）。本 PR 未修改 CI 门禁规则。
- 独立 FastAPI 路由应用经真实 Uvicorn 回环 TCP 验证：固定时钟，首次 PUT 返回 200，旧版本再次 PUT 返回 409，GET 和磁盘保留首次标题；测试服务已关闭。
- 未运行固定服务地址的两个 smoke 文件，以免连接已有服务；未调用真实付费模型、真实 ComfyUI 或宿主插件，未执行浏览器或移动设备验收。

可复核命令（在隔离副本运行）：

```sh
python -m pytest tests/test_review_redirect_and_revision.py -q
python -m pytest tests/ -q --ignore=tests/test_smoke_suite.py --ignore=tests/test_smoke_adversarial.py
python -m compileall -q core routers tests scripts main.py
python -m ruff check --select F821,F822,F823,E9 core/ routers/ tests/ scripts/ main.py
python scripts/check_file_lengths.py
python scripts/check_ssrf_guards.py
python scripts/check_aesthetics.py
node --test tests/test_frontend_concurrency.test.mjs
node static/js/i18n/validate-i18n.js
```

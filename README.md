# 开卷画布 · MeansCanvas (原 Infinite-Canvas) - 有物建筑二次深度改造版

2026-10-29 审查修复：媒体跨源重定向不再转发调用方凭据；画布保存版本在同毫秒及时间回拨时仍严格递增。[审查与验证记录](docs/Code_Review_2026-10-29.md)。

> **最新版本 / Latest Version**: `v2026.10.04`  
> **项目仓库**: [zhoushimiaov/MeansCanvas](https://github.com/zhoushimiaov/MeansCanvas)  
> **维护团队**: [有物建筑 (MeansGroup)](https://means.group) · **开源原作者**: DX（wuli大雄）  
> **开源协议**: Apache License 2.0  
> 支持本地/Tailnet 分布式 ComfyUI 算力集群 / 多平台 API / 开源 ModelScope / Pollinations 双通道 / MeiGen / 纯画布轻量模式 / 三态主题（浅色·暖白·黑夜）

配套的 Chrome 采集插件已经上线：[Chrome 网上应用店 - 开卷画布 (MeansCanvas) 采集工具](https://chromewebstore.google.com/detail/infinite-canvas-%E5%9B%BE%E5%83%8F%E8%A7%86%E9%A2%91%E6%96%87%E5%AD%97%E6%8A%93%E5%8F%96%E5%B7%A5/ajfhnbklbmpfaaookhfakohabnpmlcic?authuser=0&hl=en)

详细教程：[YouTube 视频教程](https://youtu.be/r_y_9ALr7fg)

常用稳定 API 推荐服务：
- [apib.ai 注册通道](https://apib.ai/register?aff=1uyAbb) （包含所有生图模型 / 视频模型 / LLM 模型）
- [fhl.mom 注册通道](https://www.fhl.mom/register?aff=86L574B4T2N9) （包含 Codex 和 GPT-Image-2 模型）

致谢开源原作者 DX（wuli大雄）及社交频道：
- 哔哩哔哩（教程 / 动态）：[wuli大雄的B站主页](https://space.bilibili.com/78652351)
- 小红书：[@大雄DX](https://www.xiaohongshu.com/user/profile/6433c34c000000001a023538)
- YouTube：[@大雄dx](https://www.youtube.com/@%E5%A4%A7%E9%9B%84dx)
- X (Twitter)：[@dx8152](https://x.com/dx8152?s=21)
- 原版 GitHub 仓库：[hero8152/Infinite-Canvas](https://github.com/hero8152/Infinite-Canvas)

---

【新增了 VERSION 文件，每次更新都会同步更新版本号。打开项目后，导航栏的 GitHub 按钮会智能提示新版本更新内容；若不需要更新提示，可直接删除根目录下的 VERSION 文件。】

【A VERSION file has been added. The version number updates with each release. Opening the project will indicate new updates via the GitHub navigation button. Delete the VERSION file if update notifications are not desired.】

---

### ✨ 核心功能特性 (Features)
1. **全协议 API 兼容**：支持几乎所有 OpenAI 标准协议 API、异步流式协议、Google Gemini 原生协议、字节跳动火山方舟协议、阿里云 DashScope。
2. **Pollinations.ai 双通道集成**：新增免 Key 免费通道 + 官方 API Token 高速通道，一键体验 Flux / Turbo / SDXL 等主流生图模型。
3. **MeiGen Cloud 聚合接入**：打通 14 款顶级云端生图与视频模型（MJ V8.1 / Nanobanana 2 / GPT-Image-2 / Veo 3.1 等）。
4. **RunningHub 深度对接**：支持云端工作流、AI 商业应用、收费模型的直接调用与结果回传。
5. **ModelScope 免费生态**：一键调用阿里魔搭社区免费 LLM 模型及图像生成模型。
6. **即梦 CLI 调用**：可直接调用即梦高级会员算力积分，支持文生图、图生图、文生视频与图生视频。
7. **本地 / 局域网 / Tailnet 分布式 ComfyUI 算力集群**：支持多节点 ComfyUI 集群（本地 + Tailnet 远程节点）自动负载均衡与手动指定节点双模派发、实时 GPU/显存/队列/延迟探测及本机媒体跨节点自动同步。
8. **三态主题切换（浅色 · 暖白 · 黑夜）**：全新引入 **暖白色 (Warm White 暖纸美学)** 主题，配合经典浅色与黑夜模式，点击左下角按钮即可一键三态无缝平滑切换。
9. **极简纯净侧边栏 & 外部工具抽屉管理**：外部第三方工具入口集中收敛于「API 设置 -> 外部工具开关」独立弹窗抽屉管理，侧边栏保持极致克制整洁。
10. **架构模块解耦 & 纯画布模式 (Pure Canvas Mode)**：本地生成、在线生图与工作流模块全面解耦，按需加载，随时开启极简轻盈、秒级启动的纯粹画布体验。
11. **侧边栏自由拖拽调整**：画布左侧功能栏支持鼠标自由拖拽缩放（最大极限支持 50% 视口宽度），自适应响应式布局并自动持久化宽度配置。
12. **多维创作工具链**：扩图、360 全景图沉浸式预览与截图、高清画质放大增强、运镜姿态控制、**0 算力逐帧拉片（分镜 2×3 宫格卡片 + 运镜动态短片 + 音轨剥离 + 脚本反推）**、循环节点等。
13. **任务中心与费用台账**：可视化任务历史记录、耗时统计、各模型计费台账及生成结果溯源徽标（来源渠道与模型 Tag 记忆）。
14. **极简视觉美学升级**：全站严格统一 Space Grotesk（英数）与思源黑体 Medium 500（中文）排版美学，极简性冷淡灰阶质感。
15. **生态扩展包 (tools/)**：内置 Chrome 网页素材批量采集插件、Photoshop 直连开卷画布工作台扩展插件。
16. **开发规范 (AGENTS.md)**：内置项目初心与 Andrej Karpathy 四大工程军规，护航高质量协作与长效演进。

---

### 📜 版权与开源许可 (License & Terms)

本项目采用 **Apache License 2.0 (阿帕奇 2.0 协议)** 开源发布。  
Licensed under the Apache License, Version 2.0.

* 本项目由 **[有物建筑 (MeansGroup)](https://means.group)** 基于原作者 **DX（wuli大雄）** 的原版 Infinite-Canvas 进行二次深度改造。
* 严格保留原作者版权归属与致谢声明。遵循 Apache 2.0 许可规范，代码开放供广大创作者与开发者自由学习、二次开发与交流探索。
* 详情见项目根目录下的 [LICENSE](file:///d:/Github/Infinite-Canvas/LICENSE) 文件。

---

### 📸 界面预览 (Screenshots)

<img width="2079" height="665" alt="image" src="https://github.com/user-attachments/assets/8469923b-f7a2-403c-9c37-e6e789211f28" />

<img width="1865" height="1503" alt="image" src="https://github.com/user-attachments/assets/f4030201-67c6-4845-b08b-b6fdf304afaa" />

<img width="1696" height="1350" alt="b68e144c5b04a322bfd035da4d89aba3" src="https://github.com/user-attachments/assets/0a6090fb-a8dd-4c3d-adee-b1f9233a2d91" />

<img width="1525" height="1473" alt="image" src="https://github.com/user-attachments/assets/6f61fcf9-746c-425b-9e36-cfc8d252da7c" />

<img width="1261" height="864" alt="image" src="https://github.com/user-attachments/assets/57f3e230-3134-488f-8179-d97e7d15383a" />

<img width="1530" height="858" alt="image" src="https://github.com/user-attachments/assets/9990e42d-22d5-4a10-a1e1-ad35a634edd2" />

<img width="1735" height="1400" alt="image" src="https://github.com/user-attachments/assets/d8328ff8-bbe0-4f1c-9ffa-7b56e8a1a51d" />

<img width="2258" height="969" alt="image" src="https://github.com/user-attachments/assets/4a752d99-885d-4ba9-8b86-91b495786b5c" />

<img width="1531" height="1374" alt="image" src="https://github.com/user-attachments/assets/0af79e38-0955-4740-9e65-5c9bb057f58c" />

<img width="2196" height="1040" alt="image" src="https://github.com/user-attachments/assets/6d823668-cde2-4836-8332-1858efe5f520" />

<img width="2214" height="771" alt="image" src="https://github.com/user-attachments/assets/52e10958-753f-45ba-a50e-3bbec27be436" />

## 图片智能审查系统 v2.2（涉黄 / 涉政 / 暴力 + 中文描述 + 区域级检测 + 自动爬取视频画面 + 生成 Excel）

批量上传 / 拖拽图片（不限数量，支持整个文件夹、Ctrl+V 粘贴），自动判断 **涉黄 / 涉政**（并提示暴力血腥），生成一句中文描述，
并把风险**精确框在图上**。重计算全部跑在 **Kaggle T4 x2 GPU** 上；网站是纯静态页面（Vite + React），可部署在 GitHub Pages，也可直接用 Kaggle 自带的网页版。

## 架构

```
浏览器（GitHub Pages 网站 / Kaggle 自带网页）
   │  上传前压缩到 1280px，并行 3×GPU 数 个请求
   ▼
Kaggle Notebook：FastAPI（kaggle/server.py）+ cloudflared 公网隧道
 每张 T4 一份完整副本（两张卡同时各处理一张图），副本内 3 条并行通道：
 ├─ GPU 主通道  NSFW 分类器×2（Falconsai + AdamCodd）· CLIP-L/14 · OWLv2 开放词汇检测
 ├─ VLM 通道    Qwen2.5-VL-3B（自检失败自动回退 Qwen2-VL-2B）：中文描述 + 涉黄/涉政/暴力判断 + 图中文字
 └─ CPU 通道    NudeNet（身体部位框）· RapidOCR（文字框）· 二维码 · 联系方式/敏感词
          → 多信号融合 → verdict: pass / review / block
```

### v2.0 新增能力

| 能力 | 说明 |
| --- | --- |
| 区域级分析 | 整图 + 分块（宽图用方形滑窗，近方形用四角+中心）批量推理，发现小面积违规；同时修复 CLIP 中心裁剪会丢掉宽图两侧内容的问题 |
| 双分类器集成 | Falconsai 为主、AdamCodd 为辅；集成分数永远 ≥ 主分类器，只会追加证据 |
| NudeNet | 精确定位裸露部位（优先 640m 模型，下载/校验失败自动回退内置 320n） |
| OWLv2 | 开放词汇检测：旗帜 / 标语横幅 / 极端标志 / 枪械 / 刀具 / 血迹 / 军事装备，带框 |
| OCR + 二维码 + 联系方式 | RapidOCR 逐行识别并画框；敏感词（涉政/涉黄）、微信/QQ/手机号/网址、二维码内容 |
| 更强的 VLM | 默认 Qwen2.5-VL-3B；**加载后自检**（T4 的 fp16 可能数值溢出输出 `!!!!`），失败自动回退到已验证的 Qwen2-VL-2B；运行中连续输出异常也会自动回退 |
| 暴力 / 未成年 | 暴力血腥仅提示/复核；疑似未成年人 + 性相关内容升级拦截 |
| 双 GPU 状态 | `/api/health` 的 `gpu_stats`：每张卡忙/闲、显存、已处理数、模块清单，设置面板实时显示 |
| 画框叠加层 | 详情页：检测框 / 文字框 / 分块热力，三个开关 |

**不破坏旧功能**：v2.0 的融合规则分两层——v1.1 原规则原样保留，v2.0 新证据只“追加”。已用 10 万组随机输入验证：无新证据时结论与 v1.1 完全一致，且从不变弱。

> 涉政没有统一的视觉标准，本系统综合「大模型判断 + CLIP 线索 + 目标检测 + 图中文字关键词」，适合初筛，边界样本请人工复核。
> 不做人脸识别（识别具体政治人物既不可靠也有合规风险）。关键词可用环境变量 `POLITICAL_KEYWORDS` / `PORN_KEYWORDS` 追加。
> 没连接 GPU 时网站进入「演示模式」（浏览器本地估计，不能判断涉政），连接后可一键用 AI 重审。

### v2.1 新增：自动爬取并审查网络照片

审查网站**右上角**新增绿色按钮 **「🕸️ 自动爬取并审查网络照片」**（Kaggle 自带网页版同位置也有，对应 `/crawl` 页面）。点击进入爬取界面：

| 步骤 | 说明 |
| --- | --- |
| 输入 | 选择平台（抖音 / 小红书 / 微博 / B站 / 百度图片 / 必应图片 / 任意网址）+ 粘贴网址或整段分享文字（自动提取链接），或只填关键词自动生成搜索页 |
| 抓取 | Playwright 无头 Chromium：① 拦截网络响应直接取图片字节 ② 扫描 DOM（img / srcset / data-src / 背景图 / poster）③ 逐屏页面截图（OCR 能读出截图里的文字）；可选「深入作品详情」逐个打开作品页 |
| 审核 | 抓到一张立刻去重（SHA1 + 感知哈希）并送入 GPU 池，**两张 T4 同时抢活**，并发 = 副本数 × 3，前端实时显示每张卡忙/闲、显存、各卡完成数 |
| 结果 | 拦截 / 待复核 / 通过 分类、检测框叠加、来源页、CSV 导出 |

* 接口：`POST /api/crawl/start` · `GET /api/crawl/{id}?since=rev`（增量）· `POST /api/crawl/{id}/stop` · `GET /api/crawl/{id}/thumb|img/{idx}`；`/api/health` 增加 `crawler` 字段。
* 没有 Chromium 时自动降级为 HTTP 模式（只能抓静态页面）；Kaggle Cell 1 会安装 Playwright + Chromium 并自检。
* 旧的 `/api/analyze` 逻辑原样保留（抽成 `analyze_raw` 后与爬取共用同一条审核路径）。
* **合规边界**：不破解验证码、不绕过登录、不做指纹伪装；遇到登录/验证码墙只提示。需要登录态时使用者提供自己账号的 Cookie（仅内存使用，不落盘）。默认禁止访问内网地址（SSRF 防护），请求之间有间隔。请仅用于内容审核/自有或已授权内容，并遵守平台服务条款。

### v2.2 新增：爬取「视频画面」+ 过滤登录墙 + 生成 Excel

**问题根因**：旧版爬取抓 `<img>` + 页面截图。抖音/小红书等平台的搜索页、推荐页未登录时被登录弹窗遮住，抓到的全是「登录二维码 / 登录框 / 头像 / 图标」。真正要审核的是**视频里的画面**，它在视频流里，不在 `<img>` 里。

| 能力 | 说明 |
| ---- | ---- |
| 视频抽帧（默认模式） | 找到视频流 → ffmpeg 均匀抽 N 帧（丢弃黑屏/重复帧）→ 每帧送 T4×2 审核。视频流来源：① 抖音公开分享页 / B站公开接口（不用登录）② yt-dlp ③ 浏览器抓包（video/m3u8/mp4 与 JSON 里的地址）④ `<video>`/`og:video`；兜底：对播放中的 `<video>` 截图。`kaggle/videoframes.py` |
| 登录墙三层过滤 | ① 二维码/登录/头像/图标类图片地址直接不要 ② 页面被登录/验证码墙遮住时不截图（先像用户一样关掉弹窗）③ 审核后按「OCR 文字 + 大模型描述 + 二维码」识别登录墙截图，标记 `wall`，排除出统计与 Excel，单独放在「🔒 登录墙」页签 |
| Cookie 多格式 | `a=1; b=2` / Cookie-Editor JSON / Netscape cookies.txt；只用于使用者自己的账号，仅内存使用 |
| 生成 Excel | 爬取页、图片审核页（网站与 Kaggle 自带网页都有）都有「📊 生成 Excel」：每行一张图（缩略图嵌入）+ 审核结论 + 违规类别（涉黄/涉政/暴力血腥/疑似未成年/二维码引流…）+ 原因 + AI 描述 + 图中文字 + 来源；「汇总」页含各类数量与两张 GPU 的处理数。网站端用 exceljs 在浏览器生成（无需 GPU），服务端 `kaggle/report.py`（openpyxl）：`GET /api/crawl/{id}/xlsx`、`POST /api/export/xlsx` |

* 爬取新参数：`mode=video|all|both|images|screenshot`（默认 video）、`frames_per_video`、`video_limit`、`use_ytdlp`。旧参数全部兼容。
* **最稳用法**：直接粘贴**具体视频链接**（抖音 / B站走公开解析，不用登录）。搜索页/推荐页多数平台要求登录，请填你自己账号的 Cookie。
* 合规边界不变：不破解验证码、不绕过登录、不做指纹伪装、不保存视频文件（抽完帧即删）。

## 在 Kaggle 上运行

1. 新建 Notebook，Settings：Accelerator = **GPU T4 x2**，Internet = **On**。
2. 打开网站的「进入复制代码界面」→「② 复制到 Kaggle 的代码」→ 复制「★ 一键运行 Cell」，粘贴到一个 Cell 运行。
   （或把本仓库更新后，用 `kaggle/cell_alt_git_clone.py` 只需十几行。`kaggle/code1` 也是同一份一键运行 Cell 文本。）
3. 首次需下载约 8~12GB 模型（4~10 分钟）。输出 `https://xxxx.trycloudflare.com`：直接打开就是完整网页版；也可填入网站「设置」。
4. 想要固定网址：在 Kaggle Add-ons → Secrets 添加 `NGROK_AUTHTOKEN`，启动 Cell 会自动改用 ngrok。
5. 档位（启动 Cell 顶部取消注释 `PROFILE`）：`fast`（只用 2B，最快）· `balanced`（默认，3B + 回退）· `max`（7B 两卡切分，最准最慢）。

## 排错

* `Cannot copy out of meta tensor`（v1.1 已修复）：两张 GPU 串行加载 + meta 校验 + 自动重试。
* 看启动 Cell 末尾的「降级/提示」：任何辅助工具（OWL / NudeNet / OCR / 第二分类器 / VLM）失败都只会降级，不会让服务不可用。
* 日志：`/kaggle/working/server.log`；单项关闭：环境变量 `DISABLE_OWL / DISABLE_NUDENET / DISABLE_OCR / DISABLE_TILES / DISABLE_VLM = 1`。

## 本地运行网站 / 部署

```
npm install
npm run dev      # 开发
npm run build    # 产物 dist/index.html（单文件）
```

推送到 GitHub 后：Settings → Pages → Source 选 **GitHub Actions**，`.github/workflows/deploy.yml` 会自动发布。

* `#/app` 浏览器模拟器 + 审查网站（默认）
* `#/crawl` 浏览器模拟器 + 自动爬取并审查网络照片
* `#/code` 浏览器模拟器 + 复制代码界面（与 GitHub 仓库逐文件比对：新增 / 覆盖 / 无需修改；ZIP 下载）
* `#/site` 纯网站（无外壳）

## 目录

| 路径 | 说明 |
| --- | --- |
| kaggle/server.py | GPU 服务（FastAPI，v2.0） |
| kaggle/index.html | Kaggle 自带的独立网页版（含检测框叠加） |
| kaggle/cell*.py, kaggle/code1 | Notebook 各 Cell / 一键运行 Cell（code1 由网站从真实文件合成） |
| src/components/ModerationApp.tsx | 审查界面（拖拽、队列、结果、详情、历史） |
| src/components/Insights.tsx | 区域叠加层 / 辅助检测面板 / 双 GPU 状态 |
| src/components/Simulator.tsx | 浏览器模拟器 |
| src/components/CodeViewer.tsx | 复制代码界面 |
| src/lib/codeFiles.ts | 读取真实源码、合成 Kaggle Cell、与 GitHub 比对、ZIP |

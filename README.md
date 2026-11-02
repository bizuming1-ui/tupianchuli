# 图片智能审查系统（涉黄 / 涉政 + 中文描述）

批量上传 / 拖拽图片（不限数量，支持整个文件夹、Ctrl+V 粘贴），自动判断 **涉黄 / 涉政**，并生成一句中文描述。
重计算全部跑在 **Kaggle T4 x2 GPU** 上；网站是纯静态页面（Vite + React），可部署在 GitHub Pages，也可直接用 Kaggle 自带的网页版。

## 架构

```
浏览器（GitHub Pages 网站 / Kaggle 自带网页）
   │  上传前压缩到 1280px，并行 3×GPU 数 个请求
   ▼
Kaggle Notebook：FastAPI（kaggle/server.py）+ cloudflared 公网隧道
   ├─ GPU0 副本 ┐  NSFW 分类器 (Falconsai/nsfw_image_detection)
   └─ GPU1 副本 ┘  CLIP 零样本 (clip-vit-large-patch14)：涉黄 / 涉政 / 场景
                   视觉大模型 Qwen2-VL-2B-Instruct：中文描述 + 判断 + 图中文字
           → 多信号融合 → verdict: pass / review / block
```

* 每张 GPU 一份完整副本（数据并行），空闲副本抢占请求；相同图片按 SHA1 缓存。
* 判定宁可多复核：“高置信”→ 拦截；“疑似”→ 待复核。
* 涉政没有统一的视觉标准，本系统综合「大模型判断 + CLIP 线索 + 图中文字关键词」，适合初筛，边界样本请人工复核。关键词可用环境变量 `POLITICAL_KEYWORDS` 追加。
* 没连接 GPU 时网站进入「演示模式」（浏览器本地估计，不能判断涉政），结果带“演示”标记，连接后可一键用 AI 重审。

## v1.1 修复：`cuda:1: Cannot copy out of meta tensor`

原因：两张 GPU 用两个线程**并行** `from_pretrained`，而 transformers/accelerate 的“meta 设备初始化”是进程级全局状态，互相污染，
导致第二张卡拿到没有数据的 meta 张量。修复：

1. 全局加载锁，GPU 逐张**串行**加载（GPU0 加载完立刻可用，GPU1 后台继续）；
2. 加载后校验不残留 meta 张量，失败自动用 `low_cpu_mem_usage=False` 重试；
3. 单个副本失败不再拖垮整个系统（自动降级，`/api/health` 的 `warnings` 会显示）；
4. 兼容 transformers 新旧版本的 `dtype` / `torch_dtype` 参数；
5. Cell 1 不再强制升级 pillow，减少依赖冲突。

## 在 Kaggle 上运行

1. 新建 Notebook，Settings：Accelerator = **GPU T4 x2**，Internet = **On**。
2. 打开网站的「进入复制代码界面」→「② 复制到 Kaggle 的代码」→ 复制「★ 一键运行 Cell」，粘贴到一个 Cell 运行。
   （或把本仓库更新后，用 `kaggle/cell_alt_git_clone.py` 只需 6 行。`kaggle/code1` 也是同一份一键运行 Cell 文本。）
3. 输出 `https://xxxx.trycloudflare.com`：直接打开就是完整网页版；也可填入网站「设置」。
4. 想要固定网址：在 Kaggle Add-ons → Secrets 添加 `NGROK_AUTHTOKEN`，启动 Cell 会自动改用 ngrok。
5. 换大模型：见 `kaggle/cell4_launch.py` 顶部注释（Qwen2-VL-7B，两张卡切分）。

## 本地运行网站 / 部署

```
npm install
npm run dev      # 开发
npm run build    # 产物 dist/index.html（单文件）
```

推送到 GitHub 后：Settings → Pages → Source 选 **GitHub Actions**，`.github/workflows/deploy.yml` 会自动发布。

* `#/app` 浏览器模拟器 + 审查网站（默认）
* `#/code` 浏览器模拟器 + 复制代码界面（GitHub 文件与仓库逐个比对：新增 / 覆盖 / 无需修改；ZIP 下载）
* `#/site` 纯网站（无外壳）

## 目录

| 路径 | 说明 |
| --- | --- |
| kaggle/server.py | GPU 服务（FastAPI） |
| kaggle/index.html | Kaggle 自带的独立网页版 |
| kaggle/cell*.py, kaggle/code1 | Notebook 各 Cell / 一键运行 Cell |
| src/components/ModerationApp.tsx | 审查界面（拖拽、队列、结果、详情、历史） |
| src/components/Simulator.tsx | 浏览器模拟器 |
| src/components/CodeViewer.tsx | 复制代码界面 |
| src/lib/codeFiles.ts | 读取真实源码、生成 Kaggle Cell、与 GitHub 比对、ZIP |

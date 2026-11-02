# 图片审查系统（tupianchuli）

批量上传 / 拖拽图片（不限数量，支持整个文件夹、Ctrl+V 粘贴），自动判断 **涉黄 / 涉政**，并生成一句中文描述。
重计算全部跑在 **Kaggle T4 x2 GPU** 上；网站本身是 Next.js，可部署在任何地方（或直接用 Kaggle 自带的网页版）。

## 架构

```
浏览器（Next.js 网站 / Kaggle 自带网页）
   │  上传前压缩到 1280px，并行 3×GPU 数 个请求
   ▼
Kaggle Notebook：FastAPI（kaggle/server.py）+ cloudflared 公网隧道
   ├─ GPU0 副本 ┐  NSFW 分类器 (Falconsai/nsfw_image_detection)
   └─ GPU1 副本 ┘  CLIP 零样本 (clip-vit-large-patch14)：涉黄 / 涉政 / 场景
                   视觉大模型 Qwen2-VL-2B-Instruct：中文描述 + 判断 + 图中文字
           → 多信号融合 → verdict: pass / review / block
```

* 每张 GPU 一份完整副本（数据并行），空闲副本抢占请求；相同图片按 SHA1 缓存。
* 判定宁可多复核：涉黄/涉政“高置信”→ 拦截；“疑似”→ 待复核。
* **诚实说明**：涉政没有统一的视觉标准，本系统用「大模型判断 + CLIP 线索 + 图中文字关键词」综合，适合做初筛，不等于 100% 准确，边界样本请人工复核。关键词可用环境变量 `POLITICAL_KEYWORDS` 追加。
* 没连接 GPU 时网站进入「演示模式」（浏览器本地肤色/颜色估计，不能判断涉政），结果带“演示”标记，连接后可一键用 AI 重审。

## 在 Kaggle 上运行

1. 新建 Notebook，Settings：Accelerator = **GPU T4 x2**，Internet = **On**。
2. 打开网站的「复制代码界面」→「② 复制到 Kaggle 的代码」→ 复制「★ 一键运行 Cell」，粘贴到一个 Cell 运行。
   （或把本仓库推到 GitHub 后，用 `kaggle/cell_alt_git_clone.py`。）
3. 输出 `https://xxxx.trycloudflare.com`：直接打开就是完整网页版；也可填入网站「设置」。
4. 想要固定网址：在 Kaggle Add-ons → Secrets 里添加 `NGROK_AUTHTOKEN`，启动 Cell 会自动改用 ngrok。
5. 换大模型：见 `kaggle/cell4_launch.py` 顶部注释（Qwen2-VL-7B，两张卡切分）。

## 本地运行网站

```bash
npm install
cp .env.example .env      # 仅“历史记录”功能需要 PostgreSQL
npx drizzle-kit push      # 创建 scan_records 表
npm run dev
```

* `/` 浏览器模拟器（地址栏、网站网址、右上角「复制代码」、「进入复制代码界面」）
* `/app` 图片审查工具
* `/code` 复制代码界面（GitHub 文件逐个比对仓库状态：新增 / 覆盖 / 无需修改；ZIP 下载）

## 目录

| 路径 | 说明 |
| --- | --- |
| `kaggle/server.py` | GPU 服务（FastAPI） |
| `kaggle/index.html` | Kaggle 自带的独立网页版 |
| `kaggle/cell*.py` | Notebook 各 Cell |
| `src/components/ModerationApp.tsx` | 审查界面（拖拽、队列、结果、详情、历史） |
| `src/components/BrowserSimulator.tsx` | 浏览器模拟器 |
| `src/components/CodeViewer.tsx` | 复制代码界面 |
| `src/lib/codeFiles.ts` | 读取真实源码并与 GitHub 比对 |

import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import type { CodeFileInfo } from "./types";

export const GITHUB_REPO = "bizuming1-ui/tupianchuli";
const ROOT = /*turbopackIgnore: true*/ process.cwd();

const ROOT_FILES = [
  "README.md",
  ".gitignore",
  ".env.example",
  "package.json",
  "tsconfig.json",
  "next.config.ts",
  "postcss.config.mjs",
  "eslint.config.mjs",
  "drizzle.config.json",
];
const DIRS = ["src", "kaggle"];
const TEXT_EXT = new Set([".ts", ".tsx", ".css", ".py", ".html", ".txt", ".md", ".json", ".mjs", ".js", ".svg"]);
const SKIP_DIR = new Set(["node_modules", ".next", "__pycache__", ".git"]);

function walk(dir: string, out: string[]) {
  const abs = path.join(ROOT, dir);
  if (!fs.existsSync(/*turbopackIgnore: true*/ abs)) return;
  for (const ent of fs.readdirSync(/*turbopackIgnore: true*/ abs, { withFileTypes: true })) {
    if (SKIP_DIR.has(ent.name)) continue;
    const rel = path.posix.join(dir, ent.name);
    if (ent.isDirectory()) walk(rel, out);
    else if (TEXT_EXT.has(path.extname(ent.name).toLowerCase())) out.push(rel);
  }
}

/** 需要提交到 GitHub 的全部文件（真实磁盘内容，保证与运行中的代码一致） */
export function readGithubFiles(): { path: string; content: string }[] {
  const paths: string[] = ROOT_FILES.filter((p) => fs.existsSync(/*turbopackIgnore: true*/ path.join(ROOT, p)));
  const rest: string[] = [];
  for (const d of DIRS) walk(d, rest);
  rest.sort((a, b) => {
    // kaggle 目录排在 src 前面，便于查找；其余按字母序
    const ka = a.startsWith("kaggle/") ? 0 : 1;
    const kb = b.startsWith("kaggle/") ? 0 : 1;
    return ka - kb || a.localeCompare(b);
  });
  return [...paths, ...rest].map((p) => ({ path: p, content: fs.readFileSync(/*turbopackIgnore: true*/ path.join(ROOT, p), "utf8") }));
}

function gitBlobSha(content: string): string {
  const buf = Buffer.from(content, "utf8");
  return crypto.createHash("sha1").update(`blob ${buf.length}\0`).update(buf).digest("hex");
}

interface RemoteInfo {
  ok: boolean;
  branch: string;
  empty: boolean;
  shas: Map<string, string>;
}
let cache: { at: number; info: RemoteInfo } | null = null;

/** 读取 GitHub 仓库现状（公开仓库，无需 token），用于判断 新增 / 覆盖 / 无需修改 */
async function fetchRemote(): Promise<RemoteInfo> {
  if (cache && Date.now() - cache.at < 15_000) return cache.info;
  const info: RemoteInfo = { ok: false, branch: "main", empty: false, shas: new Map() };
  try {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), 6000);
    const headers = { Accept: "application/vnd.github+json", "User-Agent": "tupianchuli-code-viewer" };
    const repoRes = await fetch(`https://api.github.com/repos/${GITHUB_REPO}`, { headers, signal: ctl.signal, cache: "no-store" });
    if (repoRes.ok) {
      const repo = (await repoRes.json()) as { default_branch?: string };
      info.branch = repo.default_branch || "main";
      const treeRes = await fetch(
        `https://api.github.com/repos/${GITHUB_REPO}/git/trees/${encodeURIComponent(info.branch)}?recursive=1`,
        { headers, signal: ctl.signal, cache: "no-store" },
      );
      if (treeRes.ok) {
        const tree = (await treeRes.json()) as { tree?: { path: string; type: string; sha: string }[] };
        for (const t of tree.tree ?? []) if (t.type === "blob") info.shas.set(t.path, t.sha);
        info.ok = true;
      } else if (treeRes.status === 404 || treeRes.status === 409) {
        info.ok = true; // 空仓库 / 分支尚无提交：全部都是新增
        info.empty = true;
      }
    }
    clearTimeout(timer);
  } catch {
    /* 离线或被限流：状态记为 unknown */
  }
  cache = { at: Date.now(), info };
  return info;
}

export async function listGithubFiles(): Promise<{ files: CodeFileInfo[]; branch: string; remoteOk: boolean; repo: string }> {
  const [local, remote] = await Promise.all([Promise.resolve(readGithubFiles()), fetchRemote()]);
  const files: CodeFileInfo[] = local.map((f) => {
    let status: CodeFileInfo["status"] = "unknown";
    if (remote.ok) {
      const sha = remote.shas.get(f.path);
      status = !sha ? "new" : sha === gitBlobSha(f.content) ? "same" : "changed";
    }
    return { path: f.path, content: f.content, lines: f.content.split("\n").length, status };
  });
  return { files, branch: remote.branch, remoteOk: remote.ok, repo: GITHUB_REPO };
}

function readRel(p: string): string {
  return fs.readFileSync(/*turbopackIgnore: true*/ path.join(ROOT, p), "utf8");
}

/** 把文本安全地嵌入 Python 源码（优先 r'''，其次 r"""，最后 base64） */
function pyEmbed(content: string): string {
  const okEnd = !content.endsWith("\\");
  if (!content.includes("'''") && okEnd) return `r'''${content}'''`;
  if (!content.includes('"""') && okEnd) return `r"""${content}"""`;
  return `__import__("base64").b64decode("${Buffer.from(content, "utf8").toString("base64")}").decode("utf-8")`;
}

/** Kaggle Notebook 里要粘贴的 Cell */
export function listKaggleCells(): CodeFileInfo[] {
  const server = readRel("kaggle/server.py");
  const html = readRel("kaggle/index.html");
  const c1 = readRel("kaggle/cell1_install.py");
  const c4 = readRel("kaggle/cell4_launch.py");
  const alt = readRel("kaggle/cell_alt_git_clone.py");

  const oneClick =
    `# ===== ★ 一键运行（推荐）：写入文件 → 安装依赖 → 启动 GPU 服务 → 生成公网网址 =====\n` +
    `# 前置：Notebook 右侧 Settings → Accelerator 选 GPU T4 x2；Internet 打开 On\n` +
    `# 只需要把这一整段粘贴到 1 个 Cell 里，点运行即可。\n` +
    `import os\n` +
    `APP_DIR = "/kaggle/working/app"\n` +
    `os.makedirs(APP_DIR, exist_ok=True)\n` +
    `_FILES = {\n"server.py": ${pyEmbed(server)},\n"index.html": ${pyEmbed(html)},\n}\n` +
    `for _n, _c in _FILES.items():\n` +
    `    with open(os.path.join(APP_DIR, _n), "w", encoding="utf-8") as _f:\n        _f.write(_c)\n` +
    `print("✅ 文件已写入", APP_DIR, os.listdir(APP_DIR))\n\n` +
    c1 +
    `\n\n` +
    c4;

  const mk = (label: string, content: string, badge: string): CodeFileInfo => ({
    path: label,
    label,
    content,
    lines: content.split("\n").length,
    status: "cell",
    badge,
  });
  return [
    mk("★ 一键运行 Cell（推荐，只复制这 1 个）", oneClick, "推荐"),
    mk("Cell 1 · 安装依赖.py", c1, "Cell 1"),
    mk("Cell 2 · 写入 server.py", `%%writefile /kaggle/working/app/server.py\n${server}`, "Cell 2"),
    mk("Cell 3 · 写入 index.html", `%%writefile /kaggle/working/app/index.html\n${html}`, "Cell 3"),
    mk("Cell 4 · 启动并获取网址.py", c4, "Cell 4"),
    mk("方案B · 从 GitHub 拉取（替代 Cell 2+3）.py", alt, "可选"),
  ];
}

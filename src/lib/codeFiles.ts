import { GH_BRANCH, GH_OWNER, GH_REPO } from "./config";
// 注意：import.meta.glob 会把"调用它的文件自己"排除在外，所以本文件需要用显式的 ?raw 导入把自己补进去，
// 否则「复制代码界面」里会缺少 src/lib/codeFiles.ts（它是 raw 字符串模块，不会产生循环依赖）。
import selfRaw from "./codeFiles.ts?raw";

/**
 * 「复制代码界面」的数据层。
 *
 * 第一性原则：页面上显示的代码 必须等于 仓库里真实的代码。
 * 所以这里不手写任何代码字符串，而是用 Vite 的 `?raw` 在构建时把真实源文件整体读进来；
 * 唯一例外是 kaggle/code1（一键运行 Cell）——它由 server.py + index.html + 安装/启动脚本在运行时合成，
 * 因此永远不会出现"GitHub 上的 code1 是旧快照"的问题。
 */

export type FileStatus = "new" | "changed" | "same" | "unknown";
export interface CodeFile {
  path: string;
  content: string;
  status: FileStatus;
}
export interface KaggleCell {
  id: string;
  title: string;
  desc: string;
  content: string;
  star?: boolean;
}
export interface RepoCompare {
  status: Record<string, FileStatus>;
  obsolete: string[];
  error?: string;
}

const RAW = import.meta.glob(
  [
    "/README.md",
    "/.gitignore",
    "/package.json",
    "/tsconfig.json",
    "/vite.config.ts",
    "/index.html",
    "/.github/workflows/*.yml",
    "/kaggle/*",
    "/src/**/*.{ts,tsx,css}",
  ],
  { query: "?raw", import: "default", eager: true },
) as Record<string, string>;

RAW["/src/lib/codeFiles.ts"] = selfRaw;

const src = (p: string): string => RAW["/" + p] ?? "";

/** 展示顺序：根配置 → Kaggle → 前端源码 */
const ROOT_ORDER = [
  "README.md", ".gitignore", "package.json", "tsconfig.json", "vite.config.ts", "index.html",
  ".github/workflows/deploy.yml",
  "kaggle/server.py", "kaggle/index.html", "kaggle/requirements.txt", "kaggle/cell1_install.py",
  "kaggle/cell4_launch.py", "kaggle/cell_alt_git_clone.py", "kaggle/code1",
];

const ensureNl = (t: string) => (t.endsWith("\n") ? t : t + "\n");

/** 合成「一键运行 Cell」：把 server.py / index.html 以 Python 原始字符串写入磁盘，再安装依赖、启动服务 */
export function buildOneClickCell(): string {
  const server = src("kaggle/server.py");
  const index = src("kaggle/index.html");
  const cell1 = src("kaggle/cell1_install.py");
  const cell4 = src("kaggle/cell4_launch.py");
  if (server.includes("'''") || index.includes("'''")) {
    throw new Error("server.py / index.html 含有三个连续单引号，无法内嵌为 Python 原始字符串");
  }
  return ensureNl(
    [
      "# ===== ★ 一键运行（推荐）：写入文件 → 安装依赖 → 启动 GPU 服务 → 生成公网网址 =====",
      "# 前置：Notebook 右侧 Settings → Accelerator 选 GPU T4 x2；Internet 打开 On",
      "# 只需要把这一整段粘贴到 1 个 Cell 里，点运行即可。（v2.0：双分类器 + CLIP + NudeNet + OWLv2 + OCR + Qwen2.5-VL，带自检回退）",
      "import os",
      'APP_DIR = "/kaggle/working/app"',
      "os.makedirs(APP_DIR, exist_ok=True)",
      "_FILES = {",
      `"server.py": r'''${server}''',`,
      `"index.html": r'''${index}''',`,
      "}",
      "for _n, _c in _FILES.items():",
      '    with open(f"{APP_DIR}/{_n}", "w", encoding="utf-8") as _f:',
      "        _f.write(_c)",
      'print("✅ 文件已写入:", {k: len(v) for k, v in _FILES.items()})',
      "",
      "# ---------------- 安装依赖 ----------------",
      cell1.trimEnd(),
      "",
      "# ---------------- 启动服务 + 公网网址 ----------------",
      cell4.trimEnd(),
    ].join("\n"),
  );
}

/** 要提交到 GitHub 的全部文件（路径与仓库一致） */
export function buildGithubFiles(): CodeFile[] {
  const map: Record<string, string> = {};
  for (const k of Object.keys(RAW)) map[k.slice(1)] = RAW[k];
  map["kaggle/code1"] = buildOneClickCell();
  const paths = Object.keys(map);
  const rank = (p: string) => {
    const i = ROOT_ORDER.indexOf(p);
    if (i >= 0) return i;
    return p.startsWith("src/") ? 1000 : 500;
  };
  paths.sort((a, b) => rank(a) - rank(b) || a.localeCompare(b));
  return paths.map((path) => ({ path, content: ensureNl(map[path]), status: "unknown" as FileStatus }));
}

/** Kaggle 里要用的所有 Cell */
export function buildKaggleCells(): KaggleCell[] {
  const server = src("kaggle/server.py");
  const index = src("kaggle/index.html");
  return [
    {
      id: "one",
      title: "★ 一键运行 Cell（推荐）",
      desc: "一个 Cell 搞定：写入文件 → 安装依赖 → 启动服务 → 输出公网网址。直接整段粘贴运行。",
      content: buildOneClickCell(),
      star: true,
    },
    {
      id: "alt",
      title: "方案 B：GitHub 同步版（仅 15 行）",
      desc: "先把本站代码更新到 GitHub，之后 Kaggle 只需这一个短 Cell（自动 git clone 最新代码）。",
      content: ensureNl(src("kaggle/cell_alt_git_clone.py")),
    },
    {
      id: "c1",
      title: "分步 Cell 1：安装依赖",
      desc: "只需运行一次。安装 transformers / NudeNet / RapidOCR 并自检 GPU。",
      content: ensureNl(src("kaggle/cell1_install.py")),
    },
    {
      id: "c2",
      title: "分步 Cell 2：写入 server.py",
      desc: "整段粘贴到新 Cell（第一行 %%writefile 必须保留）。",
      content: "%%writefile /kaggle/working/app/server.py\n" + ensureNl(server),
    },
    {
      id: "c3",
      title: "分步 Cell 3：写入 index.html",
      desc: "Kaggle 自带网页版（公网网址直接打开就是它）。",
      content: "%%writefile /kaggle/working/app/index.html\n" + ensureNl(index),
    },
    {
      id: "c4",
      title: "分步 Cell 4：启动服务 + 公网网址",
      desc: "每次重启 Notebook 都要运行；顶部注释里可切换 PROFILE（fast / balanced / max）。",
      content: ensureNl(src("kaggle/cell4_launch.py")),
    },
  ];
}

/* ------------------------------------------------------------------ */
/* 与 GitHub 仓库逐文件比对                                            */
/* ------------------------------------------------------------------ */

async function gitBlobSha(text: string): Promise<string | null> {
  try {
    if (!globalThis.crypto?.subtle) return null;
    const body = new TextEncoder().encode(text);
    const head = new TextEncoder().encode(`blob ${body.length}\0`);
    const buf = new Uint8Array(head.length + body.length);
    buf.set(head, 0);
    buf.set(body, head.length);
    const h = new Uint8Array(await crypto.subtle.digest("SHA-1", buf));
    return Array.from(h, (b) => b.toString(16).padStart(2, "0")).join("");
  } catch {
    return null;
  }
}

const norm = (t: string) => t.replace(/\r\n/g, "\n").replace(/\s+$/g, "");
const NOT_FOUND = "\u0000404";
const IGNORE_OBSOLETE = /^(LICENSE|LICENCE|package-lock\.json|\.gitattributes|\.npmrc)/i;

async function fetchRaw(path: string): Promise<string | null> {
  try {
    const res = await fetch(`https://raw.githubusercontent.com/${GH_OWNER}/${GH_REPO}/${GH_BRANCH}/${path}?t=${Date.now()}`, { cache: "no-store" });
    return res.ok ? await res.text() : res.status === 404 ? NOT_FOUND : null;
  } catch {
    return null;
  }
}

export async function compareWithRepo(files: CodeFile[]): Promise<RepoCompare> {
  const status: Record<string, FileStatus> = {};
  let obsolete: string[] = [];
  try {
    const res = await fetch(`https://api.github.com/repos/${GH_OWNER}/${GH_REPO}/git/trees/${GH_BRANCH}?recursive=1`, {
      headers: { Accept: "application/vnd.github+json" },
    });
    if (!res.ok) {
      throw new Error(res.status === 403 ? "GitHub API 临时限流（约 1 小时后恢复）" : res.status === 404 ? "仓库不存在或为私有" : `HTTP ${res.status}`);
    }
    const tree = (await res.json()) as { tree: { path: string; type: string; sha: string }[] };
    const remote = new Map(tree.tree.filter((t) => t.type === "blob").map((t) => [t.path, t.sha]));
    const local = new Set(files.map((f) => f.path));
    await Promise.all(
      files.map(async (f) => {
        const sha = remote.get(f.path);
        if (!sha) {
          status[f.path] = "new";
          return;
        }
        const mine = await gitBlobSha(f.content);
        if (mine && mine === sha) {
          status[f.path] = "same";
          return;
        }
        // 哈希不同：再用「忽略行尾/末尾空白」的内容比对确认，避免复制粘贴时多一个换行就被误判为需要覆盖
        const raw = await fetchRaw(f.path);
        status[f.path] = raw !== null && raw !== NOT_FOUND && norm(raw) === norm(f.content) ? "same" : "changed";
      }),
    );
    obsolete = [...remote.keys()].filter((p) => !local.has(p) && !IGNORE_OBSOLETE.test(p));
    return { status, obsolete };
  } catch (e) {
    // 备用：不用 API，直接逐个读取 raw 文件比对（raw 有 CDN 缓存，刚提交后可能延迟几分钟）
    try {
      await Promise.all(
        files.map(async (f) => {
          const raw = await fetchRaw(f.path);
          if (raw === null) throw new Error("网络不可用");
          status[f.path] = raw === NOT_FOUND ? "new" : norm(raw) === norm(f.content) ? "same" : "changed";
        }),
      );
      return { status, obsolete: [], error: undefined };
    } catch {
      return { status: {}, obsolete: [], error: e instanceof Error ? e.message : "无法连接 GitHub" };
    }
  }
}

/* ------------------------------------------------------------------ */
/* ZIP（仅存储，不压缩；无需第三方库）                                  */
/* ------------------------------------------------------------------ */

let CRC_TABLE: Uint32Array | null = null;
function crc32(buf: Uint8Array): number {
  if (!CRC_TABLE) {
    CRC_TABLE = new Uint32Array(256);
    for (let n = 0; n < 256; n++) {
      let c = n;
      for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
      CRC_TABLE[n] = c >>> 0;
    }
  }
  let c = 0xffffffff;
  for (let i = 0; i < buf.length; i++) c = CRC_TABLE[(c ^ buf[i]) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

export function makeZip(files: CodeFile[]): Blob {
  const enc = new TextEncoder();
  const parts: Uint8Array[] = [];
  const central: Uint8Array[] = [];
  let offset = 0;
  const d = new Date();
  const dosTime = (d.getHours() << 11) | (d.getMinutes() << 5) | (d.getSeconds() >> 1);
  const dosDate = ((d.getFullYear() - 1980) << 9) | ((d.getMonth() + 1) << 5) | d.getDate();
  for (const f of files) {
    const name = enc.encode(f.path);
    const data = enc.encode(f.content);
    const crc = crc32(data);
    const lh = new DataView(new ArrayBuffer(30));
    lh.setUint32(0, 0x04034b50, true);
    lh.setUint16(4, 20, true);
    lh.setUint16(6, 0x0800, true); // UTF-8 文件名
    lh.setUint16(8, 0, true);
    lh.setUint16(10, dosTime, true);
    lh.setUint16(12, dosDate, true);
    lh.setUint32(14, crc, true);
    lh.setUint32(18, data.length, true);
    lh.setUint32(22, data.length, true);
    lh.setUint16(26, name.length, true);
    lh.setUint16(28, 0, true);
    parts.push(new Uint8Array(lh.buffer), name, data);
    const ch = new DataView(new ArrayBuffer(46));
    ch.setUint32(0, 0x02014b50, true);
    ch.setUint16(4, 20, true);
    ch.setUint16(6, 20, true);
    ch.setUint16(8, 0x0800, true);
    ch.setUint16(10, 0, true);
    ch.setUint16(12, dosTime, true);
    ch.setUint16(14, dosDate, true);
    ch.setUint32(16, crc, true);
    ch.setUint32(20, data.length, true);
    ch.setUint32(24, data.length, true);
    ch.setUint16(28, name.length, true);
    ch.setUint32(42, offset, true);
    central.push(new Uint8Array(ch.buffer), name);
    offset += 30 + name.length + data.length;
  }
  const cdSize = central.reduce((s, p) => s + p.length, 0);
  const end = new DataView(new ArrayBuffer(22));
  end.setUint32(0, 0x06054b50, true);
  end.setUint16(8, files.length, true);
  end.setUint16(10, files.length, true);
  end.setUint32(12, cdSize, true);
  end.setUint32(16, offset, true);
  const all = [...parts, ...central, new Uint8Array(end.buffer)] as BlobPart[];
  return new Blob(all, { type: "application/zip" });
}

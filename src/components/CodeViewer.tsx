import { useCallback, useEffect, useMemo, useState } from "react";
import { buildGithubFiles, buildKaggleCells, compareWithRepo, makeZip, type CodeFile, type FileStatus, type RepoCompare } from "@/lib/codeFiles";
import { copyText } from "@/lib/clipboard";
import { GH_BRANCH, GH_REPO_URL } from "@/lib/config";

type Tab = "github" | "kaggle";

const BADGE: Record<FileStatus, { text: string; cls: string }> = {
  new: { text: "新增", cls: "bg-emerald-500/20 text-emerald-300" },
  changed: { text: "覆盖", cls: "bg-amber-500/20 text-amber-300" },
  same: { text: "无需修改", cls: "bg-slate-600/30 text-slate-400" },
  unknown: { text: "未比对", cls: "bg-slate-600/30 text-slate-400" },
};

function download(name: string, blob: Blob) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
}

export default function CodeViewer({ initialTab = "github" }: { initialTab?: Tab }) {
  const [tab, setTab] = useState<Tab>(initialTab);
  const baseFiles = useMemo(() => buildGithubFiles(), []);
  const cells = useMemo(() => buildKaggleCells(), []);
  const [cmp, setCmp] = useState<RepoCompare | null>(null);
  const [comparing, setComparing] = useState(false);
  const [onlyChanged, setOnlyChanged] = useState(false);
  const [selGh, setSelGh] = useState(baseFiles[0]?.path ?? "");
  const [selKg, setSelKg] = useState("one");
  const [flash, setFlash] = useState("");

  const runCompare = useCallback(async () => {
    setComparing(true);
    setCmp(await compareWithRepo(baseFiles));
    setComparing(false);
  }, [baseFiles]);

  useEffect(() => {
    // 进入界面即自动与 GitHub 仓库比对
    const t = setTimeout(() => void runCompare(), 0);
    return () => clearTimeout(t);
  }, [runCompare]);

  const files: CodeFile[] = useMemo(
    () => baseFiles.map((f) => ({ ...f, status: cmp?.status[f.path] ?? "unknown" })),
    [baseFiles, cmp],
  );
  const needUpdate = files.filter((f) => f.status === "new" || f.status === "changed");
  const listed = onlyChanged && cmp && !cmp.error ? needUpdate : files;
  const curGh = files.find((f) => f.path === selGh) ?? files[0];
  const curKg = cells.find((c) => c.id === selKg) ?? cells[0];

  const doCopy = async (key: string, text: string) => {
    const ok = await copyText(text);
    setFlash(ok ? key : "fail:" + key);
    setTimeout(() => setFlash(""), 1800);
  };
  const flashLabel = (key: string, label: string) => (flash === key ? "✓ 已复制" : flash === "fail:" + key ? "复制失败，请手动全选" : label);

  const allText = (list: CodeFile[]) => list.map((f) => `\n// ===================== ${f.path} =====================\n${f.content}`).join("\n");
  const editUrl = (f: CodeFile) =>
    f.status === "new" ? `${GH_REPO_URL}/new/${GH_BRANCH}?filename=${encodeURIComponent(f.path)}` : `${GH_REPO_URL}/edit/${GH_BRANCH}/${f.path}`;

  const content = tab === "github" ? curGh?.content ?? "" : curKg?.content ?? "";
  const gutter = useMemo(() => Array.from({ length: content.split("\n").length }, (_, i) => i + 1).join("\n"), [content]);

  const btn = "rounded-lg px-3.5 py-2 text-sm font-semibold transition active:scale-95 whitespace-nowrap";

  return (
    <div className="flex h-full min-h-0 flex-col bg-[#050816] text-slate-200">
      {/* 标签页 */}
      <div className="flex shrink-0 gap-2 overflow-x-auto border-b border-slate-800 bg-[#02040d] px-3 pt-3">
        {(
          [
            ["github", `① 更新到 GitHub 的代码（${files.length}）`],
            ["kaggle", `② 复制到 Kaggle 的代码（${cells.length}）`],
          ] as [Tab, string][]
        ).map(([k, label]) => (
          <button
            key={k}
            onClick={() => setTab(k)}
            className={`rounded-t-xl px-5 py-3 text-base font-bold whitespace-nowrap transition ${
              tab === k ? "bg-[#0b1226] text-white ring-1 ring-slate-700" : "text-slate-500 hover:text-slate-300"
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      {/* 操作说明 */}
      <div className="shrink-0 space-y-1 border-b border-slate-800 bg-[#0a1020] px-4 py-3 text-[13px] leading-relaxed text-slate-300">
        {tab === "github" ? (
          <>
            <p>1. 标“新增”的文件：在仓库点 Add file → Create new file，文件名填左侧路径，粘贴代码，Commit。</p>
            <p>2. 标“覆盖”的文件：点右侧「在 GitHub 编辑」，全选后粘贴，Commit。标“无需修改”的跳过。</p>
            <p>3. 懒人做法：点「下载 ZIP」解压后，在仓库页面 Add file → Upload files，把整个文件夹拖进去，Commit。</p>
            <p>4. <b className="text-amber-300">kaggle/code1</b> 就是最新的「一键运行 Cell」，务必覆盖，否则 GitHub 里仍是旧版本。</p>
            <p>5. <b className="text-emerald-300">v2.2 本次更新：</b>新增 <b>kaggle/videoframes.py</b>（视频抽帧）、<b>kaggle/report.py</b>（服务端 Excel）、<b>src/lib/excel.ts</b>（浏览器端 Excel）；覆盖 crawler.py / server.py / crawl.html / index.html / cell1_install.py / CrawlerPanel.tsx / ModerationApp.tsx / codeFiles.ts / package.json（新增 exceljs 依赖）等——以上面的“新增 / 覆盖”标记为准。</p>
            <p>6. 提交后 Settings → Pages → Source 选 GitHub Actions，1 分钟后网站自动更新。</p>
          </>
        ) : (
          <>
            <p>1. 新建 Kaggle Notebook → 右侧 Settings：Accelerator 选 <b>GPU T4 x2</b>，Internet 选 <b>On</b>。</p>
            <p>2. 复制下面的「★ 一键运行 Cell」，粘贴到 Notebook 的一个 Cell，点运行（首次下载模型约 3~8 分钟）。</p>
            <p>3. 输出的 <b>https://xxxx.trycloudflare.com</b> 就是网址：直接打开即完整网页版；也可填到本站「设置」里连接 GPU。</p>
            <p>4. 两张 T4 各跑一份完整模型（分类器×2 + CLIP + NudeNet + OWLv2 + OCR + Qwen2.5-VL），<b>同时各处理一张图</b>（爬取的视频帧也由两张卡同时抢活）；加载是串行的（避免 meta tensor 竞态），GPU0 好了就能用。</p>
            <p>5. 更强但更慢：在启动 Cell 顶部取消注释 <code>PROFILE=max</code>（7B 两卡切分）。大模型自检失败会自动回退，不会让系统不可用。</p>
          </>
        )}
      </div>

      <div className="grid min-h-0 flex-1 grid-cols-1 lg:grid-cols-[380px_1fr]">
        {/* 左侧列表 */}
        <aside className="flex min-h-0 max-h-[38vh] flex-col gap-2 overflow-hidden border-b border-slate-800 bg-[#070c1c] p-3 lg:max-h-none lg:border-b-0 lg:border-r">
          {tab === "github" ? (
            <>
              <button
                onClick={() => doCopy("all", allText(files))}
                className={`${btn} bg-emerald-500 py-3 text-base text-emerald-950 hover:bg-emerald-400`}
              >
                {flashLabel("all", "复制本类全部代码")}
              </button>
              <div className="flex gap-2">
                <button onClick={() => download("tupianchuli-code.zip", makeZip(files))} className={`${btn} flex-1 bg-slate-700/60 py-1.5 text-xs hover:bg-slate-600`}>
                  ⬇ 下载 ZIP
                </button>
                <button onClick={runCompare} disabled={comparing} className={`${btn} flex-1 bg-slate-700/60 py-1.5 text-xs hover:bg-slate-600 disabled:opacity-50`}>
                  {comparing ? "比对中…" : "↻ 重新比对 GitHub"}
                </button>
                <button onClick={() => setOnlyChanged((v) => !v)} className={`${btn} flex-1 py-1.5 text-xs ${onlyChanged ? "bg-sky-600 text-white" : "bg-slate-700/60 hover:bg-slate-600"}`}>
                  只看需更新
                </button>
              </div>
              <div className="text-xs text-slate-500">
                {comparing
                  ? "正在与 GitHub 仓库逐文件比对…"
                  : cmp?.error
                    ? `比对失败：${cmp.error}（仍可正常复制全部文件）`
                    : `需更新 ${needUpdate.length} 个（新增 ${files.filter((f) => f.status === "new").length} / 覆盖 ${files.filter((f) => f.status === "changed").length}），无需修改 ${files.filter((f) => f.status === "same").length} 个`}
              </div>
              <div className="min-h-0 flex-1 space-y-1 overflow-y-auto pr-1">
                {listed.map((f) => (
                  <button
                    key={f.path}
                    onClick={() => setSelGh(f.path)}
                    className={`flex w-full items-center justify-between gap-2 rounded-lg px-3 py-2.5 text-left font-mono text-[13px] transition ${
                      curGh?.path === f.path ? "bg-slate-700/70 text-white" : "text-slate-300 hover:bg-slate-800/70"
                    }`}
                  >
                    <span className="truncate" title={f.path}>{f.path}</span>
                    <span className={`shrink-0 rounded px-1.5 py-0.5 font-sans text-[11px] ${BADGE[f.status].cls}`}>{BADGE[f.status].text}</span>
                  </button>
                ))}
                {cmp && cmp.obsolete.length > 0 && (
                  <div className="mt-3 rounded-lg border border-red-500/30 bg-red-500/5 p-3 text-xs text-red-200">
                    <div className="mb-1 font-semibold">仓库里有、但新版不再使用的旧文件（建议在 GitHub 删除，点文件名进入后点垃圾桶图标）：</div>
                    <ul className="space-y-0.5 font-mono">
                      {cmp.obsolete.map((p) => (
                        <li key={p}>
                          <a className="underline decoration-dotted hover:text-white" target="_blank" rel="noreferrer" href={`${GH_REPO_URL}/blob/${GH_BRANCH}/${p}`}>
                            {p}
                          </a>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
              </div>
            </>
          ) : (
            <>
              <button
                onClick={() => doCopy("kone", cells[0].content)}
                className={`${btn} bg-emerald-500 py-3 text-base text-emerald-950 hover:bg-emerald-400`}
              >
                {flashLabel("kone", "★ 复制「一键运行 Cell」")}
              </button>
              <div className="min-h-0 flex-1 space-y-1 overflow-y-auto pr-1">
                {cells.map((c) => (
                  <button
                    key={c.id}
                    onClick={() => setSelKg(c.id)}
                    className={`block w-full rounded-lg px-3 py-2.5 text-left transition ${
                      curKg?.id === c.id ? "bg-slate-700/70 text-white" : "text-slate-300 hover:bg-slate-800/70"
                    }`}
                  >
                    <div className={`text-[13px] font-semibold ${c.star ? "text-emerald-300" : ""}`}>{c.title}</div>
                    <div className="mt-0.5 text-[11px] leading-snug text-slate-500">{c.desc}</div>
                  </button>
                ))}
              </div>
            </>
          )}
        </aside>

        {/* 右侧代码 */}
        <section className="flex min-h-0 flex-col">
          <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-slate-800 bg-[#0a1020] px-4 py-3">
            <div className="min-w-0 flex-1 truncate font-mono text-base font-bold text-white">
              {tab === "github" ? curGh?.path : curKg?.title}
            </div>
            <span className="text-sm text-slate-500">{content.split("\n").length} 行</span>
            {tab === "github" && curGh && (
              <>
                <button onClick={() => doCopy("name", curGh.path)} className={`${btn} bg-slate-700/60 hover:bg-slate-600`}>
                  {flashLabel("name", "复制文件名")}
                </button>
                <a href={editUrl(curGh)} target="_blank" rel="noreferrer" className={`${btn} bg-slate-700/60 hover:bg-slate-600`}>
                  在 GitHub {curGh.status === "new" ? "新建" : "编辑"} ↗
                </a>
              </>
            )}
            <button
              onClick={() => doCopy("cur", content)}
              className={`${btn} bg-sky-500 text-white hover:bg-sky-400`}
            >
              {flashLabel("cur", tab === "github" ? "复制此文件" : "复制此 Cell")}
            </button>
          </div>
          <div className="min-h-0 flex-1 overflow-auto bg-[#050816]">
            <div className="flex min-w-max font-mono text-[13px] leading-6">
              <pre className="sticky left-0 select-none bg-[#050816] px-3 text-right text-slate-600">{gutter}</pre>
              <pre className="pr-6 text-slate-200">{content}</pre>
            </div>
          </div>
        </section>
      </div>
    </div>
  );
}

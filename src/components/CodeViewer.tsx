"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { copyText } from "@/lib/clipboard";
import type { CodeFileInfo } from "@/lib/types";

interface CodeData {
  github: { files: CodeFileInfo[]; branch: string; remoteOk: boolean; repo: string };
  kaggle: CodeFileInfo[];
}

const BADGE: Record<CodeFileInfo["status"], { t: string; c: string }> = {
  new: { t: "新增", c: "bg-emerald-500/20 text-emerald-300" },
  changed: { t: "覆盖", c: "bg-amber-500/20 text-amber-300" },
  same: { t: "无需修改", c: "bg-slate-600/40 text-slate-300" },
  unknown: { t: "待确认", c: "bg-sky-500/20 text-sky-300" },
  cell: { t: "", c: "bg-sky-500/20 text-sky-300" },
};

export default function CodeViewer() {
  const [data, setData] = useState<CodeData | null>(null);
  const [err, setErr] = useState("");
  const [tab, setTab] = useState<"github" | "kaggle">("github");
  const [sel, setSel] = useState<Record<string, number>>({ github: 0, kaggle: 0 });
  const [flash, setFlash] = useState("");
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setErr("");
    try {
      const r = await fetch("/api/code", { cache: "no-store" });
      const d = (await r.json()) as CodeData & { ok: boolean; error?: string };
      if (!d.ok) throw new Error(d.error || "读取失败");
      setData(d);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "读取失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const t = setTimeout(load, 0);
    return () => clearTimeout(t);
  }, [load]);

  const files = useMemo(() => (tab === "github" ? (data?.github.files ?? []) : (data?.kaggle ?? [])), [data, tab]);
  const idx = Math.min(sel[tab] ?? 0, Math.max(0, files.length - 1));
  const cur: CodeFileInfo | undefined = files[idx];
  const repo = data?.github.repo ?? "bizuming1-ui/tupianchuli";
  const branch = data?.github.branch ?? "main";

  const copy = async (key: string, text: string, label: string) => {
    const ok = await copyText(text);
    setFlash(ok ? `✓ 已复制：${label}` : "✗ 复制失败，请手动选中代码复制");
    setTimeout(() => setFlash((f) => (f.includes(label) || f.startsWith("✗") ? "" : f)), 2200);
    void key;
  };

  const copyAll = () => {
    if (!data) return;
    if (tab === "kaggle") return copy("all", data.kaggle[0].content, "★ 一键运行 Cell");
    const text = data.github.files.map((f) => `// ===== FILE: ${f.path} =====\n${f.content}`).join("\n\n");
    return copy("all", text, `全部 ${data.github.files.length} 个文件（含文件名分隔）`);
  };

  const ghLink = (f: CodeFileInfo) => {
    const p = encodeURI(f.path);
    return f.status === "changed" || f.status === "same"
      ? `https://github.com/${repo}/edit/${branch}/${p}`
      : `https://github.com/${repo}/new/${branch}?filename=${p}`;
  };

  const counts = useMemo(() => {
    const g = data?.github.files ?? [];
    return {
      new: g.filter((f) => f.status === "new" || f.status === "unknown").length,
      changed: g.filter((f) => f.status === "changed").length,
      same: g.filter((f) => f.status === "same").length,
    };
  }, [data]);

  return (
    <div className="flex h-screen flex-col bg-[#060b18] text-slate-100">
      {/* 标签页 */}
      <div className="flex items-end gap-2 bg-[#03060f] px-3 pt-2">
        {(
          [
            ["github", `① 更新到 GitHub 的代码（${data?.github.files.length ?? "…"}）`],
            ["kaggle", `② 复制到 Kaggle 的代码（${data?.kaggle.length ?? "…"}）`],
          ] as const
        ).map(([k, t]) => (
          <button
            key={k}
            onClick={() => setTab(k)}
            className={`rounded-t-xl px-5 py-3 text-sm font-bold sm:text-base ${tab === k ? "bg-[#0b1226] text-white" : "bg-[#0a1020] text-slate-500 hover:text-slate-300"}`}
          >
            {t}
          </button>
        ))}
        <div className="flex-1" />
        <button onClick={load} disabled={loading} className="mb-2 rounded-lg bg-slate-800 px-3 py-1.5 text-xs hover:bg-slate-700 disabled:opacity-50">
          {loading ? "刷新中…" : "↻ 刷新（重新比对 GitHub）"}
        </button>
      </div>

      {/* 说明 */}
      <div className="space-y-1 border-b border-slate-800 bg-[#0b1226] px-5 py-3 text-[13px] leading-relaxed text-slate-300">
        {tab === "github" ? (
          <>
            <p>
              <b className="text-emerald-300">最快方式：</b>点左侧「下载全部 ZIP」→ 解压 → 打开{" "}
              <a className="text-sky-300 underline" href={`https://github.com/${repo}/upload/${branch}`} target="_blank" rel="noreferrer">
                仓库 Upload files 页面
              </a>{" "}
              → 把解压出的所有文件和文件夹一起拖进去 → Commit changes。
            </p>
            <p>1. 标「新增」的文件：点「在 GitHub 编辑」会打开已填好文件名的新建页，粘贴代码 → Commit。</p>
            <p>2. 标「覆盖」的文件：点「在 GitHub 编辑」，全选粘贴 → Commit。标「无需修改」的跳过（已与仓库一致）。</p>
            <p className="text-slate-400">
              状态实时对比 {repo} 的 {branch} 分支：新增 {counts.new} · 覆盖 {counts.changed} · 无需修改 {counts.same}
              {data && !data.github.remoteOk && "（当前无法访问 GitHub API，状态为待确认）"}
            </p>
          </>
        ) : (
          <>
            <p>0. 打开 kaggle.com → New Notebook → 右侧 Settings：Accelerator 选 <b>GPU T4 x2</b>，Internet 打开 <b>On</b>。</p>
            <p>
              1. 点左侧绿色按钮复制「★ 一键运行 Cell」→ 粘贴到 Notebook 的 <b>1 个代码 Cell</b> → 运行（首次下载模型约 3~8 分钟）。
            </p>
            <p>2. 输出里出现 https://xxxx.trycloudflare.com：这个网址本身就是完整网页版；也可粘贴到本网站「设置」里，用本站界面调用 GPU。</p>
            <p className="text-slate-400">想分步执行/排查问题：依次运行 Cell 1 → 2 → 3 → 4。已推送 GitHub 后可用「方案B」替代 Cell 2+3。</p>
          </>
        )}
      </div>

      {err && <div className="bg-red-900/40 px-5 py-2 text-sm text-red-200">读取代码失败：{err}</div>}

      <div className="flex min-h-0 flex-1">
        {/* 左侧文件列表 */}
        <aside className="flex w-[300px] shrink-0 flex-col gap-2 overflow-hidden border-r border-slate-800 bg-[#080e1f] p-3 sm:w-[380px]">
          <button onClick={copyAll} disabled={!data} className="rounded-xl bg-emerald-500 py-3 text-base font-bold text-emerald-950 hover:bg-emerald-400 disabled:opacity-50">
            {tab === "github" ? "复制本类全部代码" : "复制一键运行代码（推荐）"}
          </button>
          {tab === "github" && (
            <a href="/api/code/zip" className="rounded-xl bg-sky-600 py-2.5 text-center text-sm font-bold hover:bg-sky-500">
              ⬇ 下载全部 ZIP（拖进 GitHub 上传）
            </a>
          )}
          <div className="min-h-0 flex-1 space-y-1 overflow-y-auto pr-1">
            {files.map((f, i) => {
              const b = BADGE[f.status];
              return (
                <button
                  key={f.path}
                  onClick={() => setSel((s) => ({ ...s, [tab]: i }))}
                  className={`flex w-full items-center justify-between gap-2 rounded-lg px-3 py-2.5 text-left font-mono text-[13px] ${i === idx ? "bg-slate-700/70" : "hover:bg-slate-800/70"}`}
                >
                  <span className="truncate" title={f.path}>
                    {f.path}
                  </span>
                  <span className={`shrink-0 rounded-md px-2 py-0.5 font-sans text-xs ${b.c}`}>{f.status === "cell" ? f.badge : b.t}</span>
                </button>
              );
            })}
            {!data && !err && <p className="p-3 text-sm text-slate-500">正在读取源码…</p>}
          </div>
        </aside>

        {/* 右侧查看器 */}
        <section className="flex min-w-0 flex-1 flex-col">
          {cur ? (
            <>
              <div className="flex flex-wrap items-center gap-2 border-b border-slate-800 bg-[#0b1226] px-4 py-3">
                <h2 className="min-w-0 flex-1 truncate font-mono text-base font-bold">{cur.path}</h2>
                <span className="text-sm text-slate-400">{cur.lines} 行</span>
                <button onClick={() => copy("name", cur.path, "文件名")} className="rounded-lg bg-slate-700 px-3 py-2 text-sm font-semibold hover:bg-slate-600">
                  复制文件名
                </button>
                {tab === "github" && (
                  <a href={ghLink(cur)} target="_blank" rel="noreferrer" className="rounded-lg bg-slate-700 px-3 py-2 text-sm font-semibold hover:bg-slate-600">
                    在 GitHub 编辑 ↗
                  </a>
                )}
                <button onClick={() => copy("file", cur.content, cur.path)} className="rounded-lg bg-sky-500 px-4 py-2 text-sm font-bold text-white hover:bg-sky-400">
                  复制此文件
                </button>
              </div>
              <div className="flex min-h-0 flex-1 overflow-auto bg-[#050914] font-mono text-[13px] leading-6">
                <pre className="select-none border-r border-slate-800 px-3 py-3 text-right text-slate-600">{Array.from({ length: cur.lines }, (_, i) => i + 1).join("\n")}</pre>
                <pre className="flex-1 whitespace-pre px-4 py-3 text-slate-200">{cur.content}</pre>
              </div>
            </>
          ) : (
            <div className="grid flex-1 place-items-center text-slate-500">{err || "加载中…"}</div>
          )}
        </section>
      </div>

      {flash && (
        <div className="pointer-events-none fixed bottom-6 left-1/2 -translate-x-1/2 rounded-full bg-emerald-500 px-5 py-2 text-sm font-bold text-emerald-950 shadow-xl">{flash}</div>
      )}
    </div>
  );
}

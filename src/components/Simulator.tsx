import { useEffect, useRef, useState } from "react";
import { buildGithubFiles, buildOneClickCell } from "@/lib/codeFiles";
import { copyText } from "@/lib/clipboard";
import { currentSiteUrl, GH_PAGES_URL, GH_REPO_URL } from "@/lib/config";
import type { Route } from "@/lib/route";
import CodeViewer from "./CodeViewer";
import CrawlerPanel from "./CrawlerPanel";
import ModerationApp from "./ModerationApp";

/**
 * 浏览器模拟器：一个“浏览器窗口”壳。
 * - 地址栏显示真实网址；下方网址条给出能打开本站的网址（当前页 / GitHub Pages / Kaggle）
 * - 右上角「复制代码」按钮：一键复制 GitHub 全部代码 / Kaggle 一键 Cell / 进入复制代码界面
 * - 「进入复制代码界面」按钮：切换到代码界面（GitHub 与 Kaggle 两类代码，可逐文件复制）
 */
export default function Simulator({ route, go }: { route: Route; go: (r: Route) => void }) {
  const [menu, setMenu] = useState(false);
  const [toast, setToast] = useState("");
  const [kaggleUrl, setKaggleUrl] = useState("");
  const [addr, setAddr] = useState("");
  const [nonce, setNonce] = useState(0);
  const menuRef = useRef<HTMLDivElement | null>(null);

  const siteUrl = currentSiteUrl();
  const shown = `${siteUrl}#/${route}`;

  useEffect(() => {
    const t = setTimeout(() => setAddr(shown), 0);
    return () => clearTimeout(t);
  }, [shown]);

  useEffect(() => {
    const read = () => setKaggleUrl(localStorage.getItem("mod.api") || "");
    read();
    const t = setInterval(read, 1500);
    return () => clearInterval(t);
  }, []);

  useEffect(() => {
    const close = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) setMenu(false);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, []);

  const say = (m: string) => {
    setToast(m);
    setTimeout(() => setToast(""), 2200);
  };
  const copy = async (text: string, what: string) => say((await copyText(text)) ? `✓ 已复制：${what}` : "复制失败，请在代码界面手动全选复制");

  const submitAddr = () => {
    const m = addr.match(/#\/(code|sitecrawl|site|crawl|app)/);
    go((m?.[1] as Route) || "app");
  };

  const inCode = route === "code";
  const inCrawl = route === "crawl";
  const urlRow = (label: string, url: string, hint?: string) => (
    <div className="flex min-w-0 items-center gap-2 text-xs">
      <span className="w-[88px] shrink-0 text-slate-500">{label}</span>
      {url ? (
        <>
          <a href={url} target="_blank" rel="noreferrer" className="min-w-0 truncate font-mono text-sky-300 underline decoration-dotted hover:text-sky-200" title={url}>{url}</a>
          <button onClick={() => copy(url, label)} className="shrink-0 rounded bg-slate-700/70 px-2 py-0.5 text-slate-200 hover:bg-slate-600">复制</button>
          <a href={url} target="_blank" rel="noreferrer" className="shrink-0 rounded bg-slate-700/70 px-2 py-0.5 text-slate-200 hover:bg-slate-600">打开↗</a>
        </>
      ) : (
        <span className="text-slate-500">{hint}</span>
      )}
    </div>
  );

  return (
    <div className="flex h-screen w-screen flex-col overflow-hidden bg-[#0b1020] text-slate-200">
      {/* 标题栏 + 标签页 + 右上角复制代码 */}
      <div className="flex shrink-0 items-end gap-2 bg-[#05070f] px-3 pt-2">
        <div className="mb-2 flex gap-1.5 pr-2">
          <i className="h-3 w-3 rounded-full bg-red-500" /><i className="h-3 w-3 rounded-full bg-yellow-400" /><i className="h-3 w-3 rounded-full bg-green-500" />
        </div>
        <div className="flex max-w-[260px] min-w-0 items-center gap-2 rounded-t-lg bg-[#0b1020] px-4 py-2 text-xs">
          <span>{inCode ? "📋" : inCrawl ? "🕸️" : "🛡️"}</span>
          <span className="truncate">{inCode ? "复制代码界面" : inCrawl ? "自动爬取并审查网络照片" : "图片智能审查系统"}</span>
        </div>
        <div className="ml-auto mb-1.5 flex items-center gap-2" ref={menuRef}>
          <div className="relative">
            <button onClick={() => setMenu((v) => !v)} className="flex items-center gap-1.5 rounded-lg bg-emerald-500 px-3.5 py-1.5 text-sm font-bold text-emerald-950 shadow-lg shadow-emerald-900/40 transition hover:bg-emerald-400 active:scale-95">
              📋 复制代码 <span className="text-[10px]">▾</span>
            </button>
            {menu && (
              <div className="absolute right-0 top-full z-50 mt-2 w-72 overflow-hidden rounded-xl border border-slate-700 bg-slate-900 p-1.5 text-sm shadow-2xl">
                <button onClick={() => { setMenu(false); void copy(buildGithubFiles().map((f) => `\n// ===== ${f.path} =====\n${f.content}`).join("\n"), "GitHub 全部代码"); }} className="block w-full rounded-lg px-3 py-2.5 text-left hover:bg-slate-800">
                  <div className="font-semibold">复制「GitHub 全部代码」</div><div className="text-xs text-slate-400">所有要更新到仓库的文件（合并）</div>
                </button>
                <button onClick={() => { setMenu(false); void copy(buildOneClickCell(), "Kaggle 一键运行 Cell"); }} className="block w-full rounded-lg px-3 py-2.5 text-left hover:bg-slate-800">
                  <div className="font-semibold">复制「Kaggle 一键运行 Cell」</div><div className="text-xs text-slate-400">粘贴到 Kaggle Notebook 一个 Cell 即可运行</div>
                </button>
                <button onClick={() => { setMenu(false); go("code"); }} className="block w-full rounded-lg bg-slate-800/70 px-3 py-2.5 text-left hover:bg-slate-800">
                  <div className="font-semibold text-sky-300">进入复制代码界面 →</div><div className="text-xs text-slate-400">逐文件查看 / 比对 GitHub / 下载 ZIP</div>
                </button>
              </div>
            )}
          </div>
        </div>
      </div>

      {/* 工具栏 + 地址栏 */}
      <div className="flex shrink-0 items-center gap-2 border-b border-slate-800 bg-[#0b1020] px-3 py-2">
        <button onClick={() => window.history.back()} className="rounded-md px-2 py-1 text-slate-400 hover:bg-slate-800" title="后退">←</button>
        <button onClick={() => window.history.forward()} className="rounded-md px-2 py-1 text-slate-400 hover:bg-slate-800" title="前进">→</button>
        <button onClick={() => setNonce((n) => n + 1)} className="rounded-md px-2 py-1 text-slate-400 hover:bg-slate-800" title="刷新">⟳</button>
        <div className="flex min-w-0 flex-1 items-center gap-2 rounded-full bg-[#05070f] px-4 py-1.5 ring-1 ring-slate-700 focus-within:ring-sky-500">
          <span className="text-xs text-emerald-400">{siteUrl.startsWith("https") ? "🔒" : "🌐"}</span>
          <input value={addr} onChange={(e) => setAddr(e.target.value)} onKeyDown={(e) => e.key === "Enter" && submitAddr()} spellCheck={false} className="min-w-0 flex-1 bg-transparent font-mono text-xs text-slate-200 outline-none" />
          <button onClick={() => copy(shown, "当前网址")} className="shrink-0 text-xs text-slate-400 hover:text-white" title="复制网址">复制</button>
        </div>
        <button
          onClick={() => go(inCode ? "app" : "code")}
          className={`shrink-0 rounded-lg px-4 py-2 text-sm font-bold transition active:scale-95 ${inCode ? "bg-slate-700 text-white hover:bg-slate-600" : "bg-sky-500 text-white shadow-lg shadow-sky-900/40 hover:bg-sky-400"}`}
        >
          {inCode ? "← 返回审查网站" : "进入复制代码界面 →"}
        </button>
      </div>

      {/* 网址条：能打开这个网站的网址 */}
      <div className="grid shrink-0 gap-1 border-b border-slate-800 bg-[#0e1530] px-4 py-2 md:grid-cols-2">
        {urlRow("🌐 本站网址", siteUrl)}
        {urlRow("📦 GitHub Pages", GH_PAGES_URL)}
        {urlRow("🧠 Kaggle 网址", kaggleUrl, "未连接（运行 Kaggle Cell 后，在网站「设置」里填入）")}
        {urlRow("🐙 GitHub 仓库", GH_REPO_URL)}
      </div>

      {/* 网页视口 */}
      <main className="relative min-h-0 flex-1 bg-slate-950">
        {inCode ? <CodeViewer key={"c" + nonce} /> : inCrawl ? <CrawlerPanel key={"w" + nonce} onBack={() => go("app")} /> : <ModerationApp key={"a" + nonce} compact onOpenCrawl={() => go("crawl")} />}
        {toast && <div className="pointer-events-none absolute left-1/2 top-4 z-50 -translate-x-1/2 rounded-full bg-emerald-500 px-5 py-2 text-sm font-bold text-emerald-950 shadow-xl">{toast}</div>}
      </main>
    </div>
  );
}

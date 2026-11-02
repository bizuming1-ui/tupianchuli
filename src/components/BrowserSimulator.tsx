"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { copyText } from "@/lib/clipboard";

const SITE_PATH = "/app";
const CODE_PATH = "/code";

/**
 * 浏览器模拟器：一个“浏览器窗口”壳，里面用 iframe 加载真实网站。
 * - 地址栏显示真实网址（可编辑回车跳转）
 * - 右上角「复制代码」按钮 / 地址栏旁「进入复制代码界面」按钮 → 打开复制代码界面
 */
export default function BrowserSimulator() {
  const frame = useRef<HTMLIFrameElement | null>(null);
  const [origin, setOrigin] = useState("");
  const [path, setPath] = useState(SITE_PATH);
  const [draft, setDraft] = useState("");
  const [title, setTitle] = useState("图片审查系统");
  const [flash, setFlash] = useState("");

  useEffect(() => {
    const t = setTimeout(() => {
      setOrigin(window.location.origin);
      setDraft(window.location.origin + SITE_PATH);
    }, 0);
    return () => clearTimeout(t);
  }, []);

  const go = useCallback((p: string) => {
    const w = frame.current?.contentWindow;
    if (w) w.location.href = p;
    else if (frame.current) frame.current.src = p;
  }, []);

  const onLoad = useCallback(() => {
    try {
      const w = frame.current?.contentWindow;
      if (!w) return;
      const p = w.location.pathname + w.location.search;
      setPath(p);
      setDraft(w.location.origin + p);
      setTitle(w.document.title || "图片审查系统");
    } catch {
      /* 跨域时读不到，忽略 */
    }
  }, []);

  const siteUrl = origin + SITE_PATH;
  const onCode = path.startsWith(CODE_PATH);

  const submit = () => {
    const v = draft.trim();
    try {
      const u = new URL(v.startsWith("http") ? v : origin + (v.startsWith("/") ? v : "/" + v));
      if (u.origin === origin) go(u.pathname + u.search);
      else window.open(u.toString(), "_blank", "noopener");
    } catch {
      setDraft(origin + path);
    }
  };

  const flashMsg = (m: string) => {
    setFlash(m);
    setTimeout(() => setFlash(""), 1800);
  };

  const btn = "grid h-8 w-8 place-items-center rounded-full text-slate-300 hover:bg-slate-700/70 disabled:opacity-40";

  return (
    <div className="flex h-screen flex-col bg-gradient-to-br from-[#0b1020] via-[#111a33] to-[#0b1020] p-0 sm:p-3">
      <div className="mx-auto flex min-h-0 w-full max-w-[1800px] flex-1 flex-col overflow-hidden bg-[#1b2236] shadow-2xl ring-1 ring-slate-600/50 sm:rounded-xl">
        {/* 标题栏 + 标签页 */}
        <div className="flex items-center gap-3 bg-[#0e1424] px-3 pt-2">
          <div className="hidden gap-1.5 sm:flex">
            <i className="h-3 w-3 rounded-full bg-red-500" />
            <i className="h-3 w-3 rounded-full bg-yellow-400" />
            <i className="h-3 w-3 rounded-full bg-green-500" />
          </div>
          <div className="flex min-w-0 max-w-[260px] items-center gap-2 rounded-t-lg bg-[#1b2236] px-3 py-2 text-xs text-slate-200">
            <span>{onCode ? "📋" : "🛡️"}</span>
            <span className="truncate">{onCode ? "复制代码" : title}</span>
          </div>
          <div className="flex-1" />
          {/* 右上角：复制代码 */}
          <button
            onClick={() => go(onCode ? SITE_PATH : CODE_PATH)}
            className="mb-1 flex items-center gap-1.5 rounded-lg bg-gradient-to-r from-emerald-500 to-sky-500 px-4 py-1.5 text-xs font-bold text-white shadow hover:brightness-110"
            title="打开复制代码界面（GitHub / Kaggle 代码）"
          >
            {onCode ? "← 返回网站" : "</> 复制代码"}
          </button>
        </div>

        {/* 工具栏 / 地址栏 */}
        <div className="flex flex-wrap items-center gap-1.5 bg-[#1b2236] px-3 py-2">
          <button className={btn} onClick={() => frame.current?.contentWindow?.history.back()} title="后退">
            ←
          </button>
          <button className={btn} onClick={() => frame.current?.contentWindow?.history.forward()} title="前进">
            →
          </button>
          <button className={btn} onClick={() => frame.current?.contentWindow?.location.reload()} title="刷新">
            ⟳
          </button>
          <div className="flex min-w-[220px] flex-1 items-center gap-2 rounded-full bg-[#0e1424] px-4 py-1.5 ring-1 ring-slate-600/60 focus-within:ring-sky-500">
            <span className="text-xs text-emerald-400">🔒</span>
            <input
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && submit()}
              spellCheck={false}
              className="min-w-0 flex-1 bg-transparent text-sm text-slate-100 outline-none"
              aria-label="地址栏"
            />
          </div>
          <button
            onClick={() => go(CODE_PATH)}
            className="rounded-full bg-emerald-500 px-4 py-1.5 text-xs font-bold text-emerald-950 hover:bg-emerald-400"
          >
            进入复制代码界面
          </button>
        </div>

        {/* 网站网址条 */}
        <div className="flex flex-wrap items-center gap-2 border-y border-slate-700/60 bg-[#141b2f] px-4 py-2 text-xs text-slate-300">
          <span>🌐 本网站网址：</span>
          <a href={siteUrl} target="_blank" rel="noreferrer" className="break-all font-mono text-sky-300 underline-offset-2 hover:underline">
            {siteUrl || "…"}
          </a>
          <button
            onClick={async () => flashMsg((await copyText(siteUrl)) ? "✓ 网址已复制" : "✗ 复制失败")}
            className="rounded-md bg-slate-700 px-2 py-1 hover:bg-slate-600"
          >
            复制网址
          </button>
          <a href={siteUrl} target="_blank" rel="noreferrer" className="rounded-md bg-slate-700 px-2 py-1 hover:bg-slate-600">
            新窗口打开 ↗
          </a>
          {flash && <span className="font-semibold text-emerald-300">{flash}</span>}
        </div>

        {/* 页面视口 */}
        <iframe ref={frame} src={SITE_PATH} onLoad={onLoad} title="browser viewport" className="min-h-0 w-full flex-1 border-0 bg-[#0a0f1f]" allow="clipboard-write" />
      </div>
    </div>
  );
}

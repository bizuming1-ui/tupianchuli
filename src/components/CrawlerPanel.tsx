import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { fetchHealth, normalizeApiUrl } from "@/lib/analyzer";
import {
  clearCrawlImages, crawlImage, DEFAULT_FORM, PLATFORMS, pollCrawl, startCrawl, stopCrawl,
  type CrawlForm, type CrawlItem, type CrawlView,
} from "@/lib/crawl";
import type { HealthInfo, Verdict } from "@/lib/types";
import { GpuPanel, ImageOverlay, InsightPanel } from "./Insights";

type Filter = "all" | Verdict | "pending" | "error" | "screenshot";

const VERDICT_UI: Record<Verdict, { text: string; label: string; cls: string }> = {
  pass: { text: "✅ 通过", label: "通过", cls: "bg-emerald-500/15 text-emerald-300 ring-emerald-500/40" },
  review: { text: "⚠️ 待复核", label: "待复核", cls: "bg-amber-500/15 text-amber-300 ring-amber-500/40" },
  block: { text: "🚫 拦截", label: "拦截", cls: "bg-red-500/15 text-red-300 ring-red-500/40" },
};
const STATE_UI: Record<string, { text: string; cls: string }> = {
  queued: { text: "排队中", cls: "text-slate-300" },
  running: { text: "⏳ 爬取并审核中…", cls: "text-sky-300" },
  done: { text: "✅ 已完成", cls: "text-emerald-300" },
  stopped: { text: "■ 已停止", cls: "text-amber-300" },
  error: { text: "❌ 出错", cls: "text-red-300" },
};
const FORM_KEY = "crawl.form";

const kindOf = (it: CrawlItem): Verdict | "pending" | "error" =>
  it.res ? it.res.verdict : it.status === "error" || it.status === "skipped" ? "error" : "pending";

function download(name: string, text: string, type: string) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([text], { type }));
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}

/** 带请求头的图片（兼容 ngrok 警告页），首屏之外的缩略图在进入视口时才加载 */
function NetImg({ api, job, idx, thumb, className, alt }: { api: string; job: string; idx: number; thumb: boolean; className?: string; alt: string }) {
  const [src, setSrc] = useState("");
  const [failed, setFailed] = useState(false);
  const host = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    let dead = false;
    const el = host.current;
    const load = () => {
      crawlImage(api, job, idx, thumb)
        .then((u) => !dead && setSrc(u))
        .catch(() => !dead && setFailed(true));
    };
    if (!thumb || !el || typeof IntersectionObserver === "undefined") {
      load();
      return () => {
        dead = true;
      };
    }
    const io = new IntersectionObserver((es) => {
      if (es.some((e) => e.isIntersecting)) {
        io.disconnect();
        load();
      }
    }, { rootMargin: "300px" });
    io.observe(el);
    return () => {
      dead = true;
      io.disconnect();
    };
  }, [api, job, idx, thumb]);
  return (
    <div ref={host} className={className}>
      {src ? <img src={src} alt={alt} className="h-full w-full object-contain" /> : <div className="grid h-full w-full place-items-center text-xs text-slate-600">{failed ? "图片不可用" : "…"}</div>}
    </div>
  );
}

const Card = memo(function Card({ it, api, job, onOpen }: { it: CrawlItem; api: string; job: string; onOpen: (i: number) => void }) {
  const k = kindOf(it);
  const res = it.res;
  return (
    <button onClick={() => onOpen(it.idx)} className={`group overflow-hidden rounded-xl bg-slate-900 text-left ring-1 transition hover:ring-sky-400 ${k === "block" ? "ring-red-500/60" : k === "review" ? "ring-amber-500/50" : "ring-slate-800"}`}>
      <NetImg api={api} job={job} idx={it.idx} thumb className="h-40 w-full bg-black" alt={it.name} />
      <div className="space-y-1 p-2.5">
        <div className="flex items-center justify-between gap-2">
          {k === "pending" || k === "error" ? (
            <span className={`rounded-md px-2 py-0.5 text-[11px] font-semibold ring-1 ${k === "error" ? "bg-red-500/10 text-red-300 ring-red-500/30" : "bg-slate-700/50 text-slate-300 ring-slate-600"}`}>
              {k === "error" ? (it.status === "skipped" ? "已取消" : "❌ 失败") : it.status === "running" ? "🔍 GPU 审核中" : "⏳ 排队"}
            </span>
          ) : (
            <span className={`rounded-md px-2 py-0.5 text-[11px] font-semibold ring-1 ${VERDICT_UI[k].cls}`}>{VERDICT_UI[k].text}</span>
          )}
          <span className="text-[10px] text-slate-500">{it.kind === "screenshot" ? "📸 截图" : "🖼️ 图片"} {it.w}×{it.h}</span>
        </div>
        <p className="line-clamp-2 min-h-[2rem] text-xs text-slate-300">{res ? res.description : it.err || "等待审核…"}</p>
        {res && res.reasons.length > 0 && <p className="line-clamp-2 text-[11px] text-amber-300">{res.reasons[0]}</p>}
        {res?.gpu && <p className="text-[10px] text-slate-600">{res.gpu}{res.cached ? " · 缓存" : ""}</p>}
      </div>
    </button>
  );
});

function Bar({ label, value }: { label: string; value: number }) {
  return (
    <div className="flex items-center gap-2 text-xs">
      <span className="w-8 text-slate-400">{label}</span>
      <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-slate-700"><div className={`h-full ${value >= 0.5 ? "bg-red-400" : value >= 0.25 ? "bg-amber-400" : "bg-emerald-400"}`} style={{ width: `${Math.round(value * 100)}%` }} /></div>
      <span className="w-9 text-right text-slate-300">{Math.round(value * 100)}%</span>
    </div>
  );
}

function Detail({ it, list, api, job, onGo, onClose }: { it: CrawlItem; list: CrawlItem[]; api: string; job: string; onGo: (i: number) => void; onClose: () => void }) {
  const [url, setUrl] = useState("");
  const idx = list.findIndex((x) => x.idx === it.idx);
  useEffect(() => {
    let dead = false;
    crawlImage(api, job, it.idx, false).then((u) => !dead && setUrl(u)).catch(() => undefined);
    return () => {
      dead = true;
      setUrl("");
    };
  }, [api, job, it.idx]);
  useEffect(() => {
    const on = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
      if (e.key === "ArrowLeft" && idx > 0) onGo(list[idx - 1].idx);
      if (e.key === "ArrowRight" && idx >= 0 && idx < list.length - 1) onGo(list[idx + 1].idx);
    };
    window.addEventListener("keydown", on);
    return () => window.removeEventListener("keydown", on);
  }, [idx, list, onClose, onGo]);
  const res = it.res;
  return (
    <div className="absolute inset-0 z-40 flex items-stretch justify-center bg-black/75 p-2 sm:p-4" onClick={onClose}>
      <div className="relative flex w-full max-w-5xl flex-col overflow-hidden rounded-2xl border border-slate-700 bg-slate-900 shadow-2xl md:flex-row" onClick={(e) => e.stopPropagation()}>
        <div className="relative grid min-h-[200px] flex-1 place-items-center bg-black">
          {url ? <ImageOverlay url={url} name={it.name} res={res} /> : <span className="text-slate-500">加载中…</span>}
          {idx > 0 && <button onClick={() => onGo(list[idx - 1].idx)} className="absolute left-2 top-1/2 -translate-y-1/2 rounded-full bg-black/60 px-3 py-2 text-xl text-white hover:bg-black">‹</button>}
          {idx >= 0 && idx < list.length - 1 && <button onClick={() => onGo(list[idx + 1].idx)} className="absolute right-2 top-1/2 -translate-y-1/2 rounded-full bg-black/60 px-3 py-2 text-xl text-white hover:bg-black">›</button>}
        </div>
        <div className="flex max-h-[55%] w-full flex-col gap-3 overflow-y-auto p-4 text-sm md:max-h-none md:w-[380px]">
          <div className="flex items-start justify-between gap-2">
            <div className="min-w-0 break-all text-xs text-slate-400">{it.kind === "screenshot" ? "📸 页面截图" : "🖼️ 网络图片"} · {it.w}×{it.h} · {(it.bytes / 1024).toFixed(0)}KB</div>
            <button onClick={onClose} className="text-slate-400 hover:text-white">✕</button>
          </div>
          {res ? (
            <>
              <div className={`inline-block self-start rounded-lg px-3 py-1 text-base font-bold ring-1 ${VERDICT_UI[res.verdict].cls}`}>{VERDICT_UI[res.verdict].text}</div>
              <p className="text-base leading-snug text-white">{res.description}</p>
              {res.reasons.length > 0 && <ul className="space-y-1 rounded-lg bg-slate-800/70 p-3 text-xs text-amber-200">{res.reasons.map((r) => <li key={r}>• {r}</li>)}</ul>}
              <div className="space-y-1.5 rounded-lg bg-slate-800/50 p-3">
                <Bar label="涉黄" value={res.porn.score} />
                <Bar label="涉政" value={res.political.score} />
                {res.violence && <Bar label="暴力" value={res.violence.score} />}
              </div>
              <InsightPanel res={res} />
              {res.ocr_text && <div className="rounded-lg bg-slate-800/50 p-3 text-xs text-slate-300"><div className="mb-1 font-semibold text-slate-400">图中文字</div>{res.ocr_text}</div>}
              <div className="text-[11px] text-slate-500">引擎：{res.engine}{res.gpu ? ` · ${res.gpu}` : ""}{res.timings?.total_ms != null ? ` · GPU ${res.timings.total_ms}ms` : ""}</div>
            </>
          ) : (
            <div className="text-slate-400">{it.err ? `❌ ${it.err}` : it.status === "skipped" ? "已取消" : "审核中…"}</div>
          )}
          <div className="space-y-1 border-t border-slate-800 pt-3 text-[11px] text-slate-400">
            <div>来源页：<a href={it.page} target="_blank" rel="noreferrer" className="break-all text-sky-300 underline decoration-dotted">{it.page}</a></div>
            {it.kind === "image" && <div>图片地址：<a href={it.src} target="_blank" rel="noreferrer" className="break-all text-sky-300 underline decoration-dotted">{it.src}</a></div>}
          </div>
        </div>
      </div>
    </div>
  );
}

const inp = "w-full rounded-lg bg-slate-800 px-3 py-2 text-xs text-white outline-none ring-1 ring-slate-700 focus:ring-sky-400";
const lab = "mb-1 block text-[11px] font-medium text-slate-400";

export default function CrawlerPanel({ onBack }: { onBack: () => void }) {
  const [api, setApi] = useState("");
  const [apiDraft, setApiDraft] = useState("");
  const [health, setHealth] = useState<HealthInfo | null>(null);
  const [online, setOnline] = useState<"none" | "checking" | "loading" | "online" | "offline">("none");
  const [form, setForm] = useState<CrawlForm>(DEFAULT_FORM);
  const [adv, setAdv] = useState(false);
  const [job, setJob] = useState<string | null>(null);
  const [view, setView] = useState<CrawlView | null>(null);
  const [items, setItems] = useState<CrawlItem[]>([]);
  const [filter, setFilter] = useState<Filter>("all");
  const [sortRisk, setSortRisk] = useState(true);
  const [limit, setLimit] = useState(96);
  const [detail, setDetail] = useState<number | null>(null);
  const [err, setErr] = useState("");
  const [starting, setStarting] = useState(false);
  const map = useRef(new Map<number, CrawlItem>());
  const sinceRef = useRef(0);
  const logRef = useRef<HTMLDivElement | null>(null);

  /* 初始化：读取 Kaggle 地址 + 上次的表单（Cookie 不保存） */
  useEffect(() => {
    const t = setTimeout(() => {
      const a = localStorage.getItem("mod.api") || "";
      setApi(a);
      setApiDraft(a);
      try {
        const f = JSON.parse(localStorage.getItem(FORM_KEY) || "null") as Partial<CrawlForm> | null;
        if (f) setForm({ ...DEFAULT_FORM, ...f, cookie: "" });
      } catch {
        /* ignore */
      }
    }, 0);
    return () => clearTimeout(t);
  }, []);
  useEffect(() => {
    localStorage.setItem(FORM_KEY, JSON.stringify({ ...form, cookie: "" }));
  }, [form]);

  /* 连接状态 / 双 GPU 状态轮询 */
  useEffect(() => {
    if (!api) {
      const t = setTimeout(() => {
        setOnline("none");
        setHealth(null);
      }, 0);
      return () => clearTimeout(t);
    }
    let stop = false;
    let timer: ReturnType<typeof setTimeout>;
    const tick = async () => {
      try {
        const h = await fetchHealth(api, AbortSignal.timeout(6000));
        if (stop) return;
        setHealth(h);
        setOnline(h.ready ? "online" : h.status === "error" ? "offline" : "loading");
      } catch {
        if (stop) return;
        setHealth(null);
        setOnline("offline");
      }
      timer = setTimeout(tick, 4000);
    };
    setOnline("checking");
    void tick();
    return () => {
      stop = true;
      clearTimeout(timer);
    };
  }, [api]);

  /* 任务轮询：增量拉取（since=rev），不会重复传输已有结果 */
  useEffect(() => {
    if (!job || !api) return;
    let stop = false;
    let timer: ReturnType<typeof setTimeout>;
    const ctl = new AbortController();
    const tick = async () => {
      let finished = false;
      try {
        const v = await pollCrawl(api, job, sinceRef.current, ctl.signal);
        if (stop) return;
        sinceRef.current = v.rev;
        for (const it of v.items) map.current.set(it.idx, it);
        setItems(Array.from(map.current.values()));
        setView(v);
        if (v.gpu_stats) setHealth((h) => (h ? { ...h, gpu_stats: v.gpu_stats } : h));
        finished = v.state === "done" || v.state === "stopped" || v.state === "error";
      } catch (e) {
        if (stop) return;
        if (e instanceof Error && /不存在|过期/.test(e.message)) finished = true;
      }
      if (!stop && !finished) timer = setTimeout(tick, 900);
    };
    void tick();
    return () => {
      stop = true;
      ctl.abort();
      clearTimeout(timer);
    };
  }, [job, api]);

  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [view?.logs]);
  useEffect(() => () => clearCrawlImages(), []);

  const set = <K extends keyof CrawlForm>(k: K, v: CrawlForm[K]) => setForm((f) => ({ ...f, [k]: v }));
  const plat = PLATFORMS.find((p) => p.id === form.platform) ?? PLATFORMS[0];
  const running = !!view && (view.state === "running" || view.state === "queued");
  const canStart = online === "online" && !starting && !running && (form.urlsText.trim() !== "" || form.keyword.trim() !== "");

  const connect = () => {
    const u = normalizeApiUrl(apiDraft);
    localStorage.setItem("mod.api", u);
    setApi(u);
  };

  const start = async () => {
    setErr("");
    setStarting(true);
    try {
      clearCrawlImages();
      map.current = new Map();
      sinceRef.current = 0;
      setItems([]);
      setView(null);
      setDetail(null);
      setLimit(96);
      const r = await startCrawl(api, form);
      setJob(r.id);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "启动失败");
    }
    setStarting(false);
  };
  const stop = async () => {
    if (job) await stopCrawl(api, job).catch(() => undefined);
  };

  const counts = useMemo(() => {
    const c = { all: items.length, pass: 0, review: 0, block: 0, pending: 0, error: 0, screenshot: 0 };
    for (const it of items) {
      c[kindOf(it)]++;
      if (it.kind === "screenshot") c.screenshot++;
    }
    return c;
  }, [items]);

  const filtered = useMemo(() => {
    let l = items.filter((i) => (filter === "all" ? true : filter === "screenshot" ? i.kind === "screenshot" : kindOf(i) === filter));
    l = l.slice().sort((a, b) => {
      if (!sortRisk) return a.idx - b.idx;
      const w = (i: CrawlItem) => (i.res ? (i.res.verdict === "block" ? 3 : i.res.verdict === "review" ? 2 : 1) * 10 + Math.max(i.res.porn.score, i.res.political.score) : 0);
      return w(b) - w(a) || a.idx - b.idx;
    });
    return l;
  }, [items, filter, sortRisk]);

  const onOpen = useCallback((i: number) => setDetail(i), []);
  const cur = detail == null ? null : map.current.get(detail) ?? items.find((i) => i.idx === detail) ?? null;

  const exportCsv = () => {
    const q = (v: unknown) => `"${String(v ?? "").replace(/"/g, '""')}"`;
    const rows = [["序号", "类型", "结论", "涉黄分", "涉政分", "描述", "原因", "图中文字", "图片地址", "来源页", "GPU"].map(q).join(",")];
    for (const it of items) {
      const r = it.res;
      rows.push([it.idx, it.kind === "screenshot" ? "截图" : "图片", r ? VERDICT_UI[r.verdict].label : it.status, r?.porn.score ?? "", r?.political.score ?? "", r?.description ?? it.err ?? "", (r?.reasons ?? []).join("；"), r?.ocr_text ?? "", it.kind === "image" ? it.src : "", it.page, r?.gpu ?? ""].map(q).join(","));
    }
    download(`爬取审核结果-${new Date().toISOString().slice(0, 10)}.csv`, "\ufeff" + rows.join("\n"), "text/csv;charset=utf-8");
  };

  const s = view?.stats;
  const fin = s ? s.analyzed + s.errors : 0;
  const pct = view && view.total > 0 ? Math.min(100, Math.round((fin / view.total) * 100)) : 0;
  const pill =
    online === "online" ? { cls: "bg-emerald-500/15 text-emerald-300 ring-emerald-500/40", text: `● GPU 在线 · ${(health?.gpus || []).map((g) => g.name).join(" + ") || `${health?.replicas_ready ?? 0} 副本`}` }
    : online === "loading" ? { cls: "bg-amber-500/15 text-amber-300 ring-amber-500/40", text: `◌ GPU 加载模型中（${health?.replicas_ready ?? 0}/${health?.replicas ?? "?"}）` }
    : online === "checking" ? { cls: "bg-slate-700/50 text-slate-300 ring-slate-600", text: "◌ 连接中…" }
    : online === "offline" ? { cls: "bg-red-500/15 text-red-300 ring-red-500/40", text: "✕ 无法连接 Kaggle" }
    : { cls: "bg-slate-700/50 text-slate-300 ring-slate-600", text: "未连接 GPU" };
  const btn = "rounded-lg bg-slate-800 px-3 py-1.5 text-xs font-medium text-slate-200 ring-1 ring-slate-700 transition hover:bg-slate-700 active:scale-95 disabled:opacity-40";
  const tabs: [Filter, string][] = [
    ["all", "全部"], ["block", "🚫 拦截"], ["review", "⚠️ 待复核"], ["pass", "✅ 通过"], ["pending", "⏳ 处理中"], ["screenshot", "📸 截图"], ["error", "❌ 失败/取消"],
  ];

  return (
    <div className="relative h-full min-h-0 bg-slate-950 text-slate-200">
      <div className="h-full overflow-y-auto">
        <div className="mx-auto max-w-7xl space-y-4 px-4 py-3">
          <header className="flex flex-wrap items-center gap-3">
            <button onClick={onBack} className={btn}>← 返回图片审查</button>
            <div className="mr-auto">
              <h1 className="text-xl font-bold text-white">🕸️ 自动爬取并审查网络照片</h1>
              <p className="text-xs text-slate-400">抖音 / 小红书 / 微博 / B站 / 图片搜索 / 任意网址 → 浏览器抓图 + 截图 → Kaggle T4×2 双卡同时精准审核</p>
            </div>
            <span className={`rounded-full px-3 py-1.5 text-xs font-medium ring-1 ${pill.cls}`}>{pill.text}</span>
            {health?.crawler && (
              <span className={`rounded-full px-3 py-1.5 text-xs font-medium ring-1 ${health.crawler.chromium ? "bg-emerald-500/10 text-emerald-300 ring-emerald-500/30" : "bg-amber-500/10 text-amber-300 ring-amber-500/30"}`}>
                {health.crawler.chromium ? "🌐 Chromium 浏览器已就绪" : "🌐 HTTP 降级（需运行 Cell 1 安装 Chromium）"}
              </span>
            )}
          </header>

          {online !== "online" && (
            <div className="rounded-xl border border-amber-500/30 bg-amber-500/10 p-3 text-xs text-amber-100">
              <div className="mb-2 font-semibold">爬取与审核运行在 Kaggle GPU 服务端（浏览器网页无法跨站抓取其他网站），请先连接：</div>
              <div className="flex flex-wrap gap-2">
                <input value={apiDraft} onChange={(e) => setApiDraft(e.target.value)} onKeyDown={(e) => e.key === "Enter" && connect()} placeholder="粘贴 Kaggle 输出的 https://xxxx.trycloudflare.com" className={`${inp} min-w-[260px] flex-1 font-mono`} />
                <button onClick={connect} className="rounded-lg bg-sky-500 px-4 text-xs font-bold text-white hover:bg-sky-400">连接</button>
              </div>
              <div className="mt-2 text-[11px] text-amber-200/80">状态：{pill.text}。还没有网址？在「进入复制代码界面」复制「★ 一键运行 Cell」到 Kaggle 运行，约 4~10 分钟后会输出网址。</div>
            </div>
          )}

          <div className="grid gap-4 lg:grid-cols-[380px_1fr]">
            {/* 左：任务配置 */}
            <section className="space-y-3 rounded-2xl bg-slate-900/70 p-4 ring-1 ring-slate-800">
              <div>
                <span className={lab}>平台</span>
                <div className="flex flex-wrap gap-1.5">
                  {PLATFORMS.map((p) => (
                    <button key={p.id} onClick={() => set("platform", p.id)} className={`rounded-lg px-2.5 py-1.5 text-xs ring-1 transition ${form.platform === p.id ? "bg-sky-500 text-white ring-sky-400" : "bg-slate-800 text-slate-300 ring-slate-700 hover:bg-slate-700"}`}>{p.icon} {p.name}</button>
                  ))}
                </div>
                <p className="mt-1 text-[11px] text-slate-500">{plat.hint}</p>
              </div>
              <div>
                <label className={lab}>网址 / 分享链接（每行一个；可直接粘贴整段分享文字，会自动提取链接）</label>
                <textarea value={form.urlsText} onChange={(e) => set("urlsText", e.target.value)} rows={4} placeholder={plat.placeholder} className={`${inp} font-mono`} />
              </div>
              <div>
                <label className={lab}>搜索关键词（可选，按所选平台自动生成搜索页）</label>
                <input value={form.keyword} onChange={(e) => set("keyword", e.target.value)} placeholder="例如：穿搭" className={inp} />
              </div>
              <div className="grid grid-cols-3 gap-2">
                {([["both", "图片+截图"], ["images", "仅抓图"], ["screenshot", "仅截图"]] as const).map(([m, t]) => (
                  <button key={m} onClick={() => set("mode", m)} className={`rounded-lg px-2 py-2 text-xs ring-1 ${form.mode === m ? "bg-emerald-500/20 text-emerald-200 ring-emerald-400/60" : "bg-slate-800 text-slate-300 ring-slate-700 hover:bg-slate-700"}`}>{t}</button>
                ))}
              </div>
              <div className="grid grid-cols-3 gap-2">
                <div><label className={lab}>最多张数</label><input type="number" min={1} max={600} value={form.max_images} onChange={(e) => set("max_images", Number(e.target.value) || 1)} className={inp} /></div>
                <div><label className={lab}>滚动屏数</label><input type="number" min={0} max={60} value={form.scrolls} onChange={(e) => set("scrolls", Number(e.target.value) || 0)} className={inp} /></div>
                <div><label className={lab}>审核模式</label>
                  <select value={form.analyze_mode} onChange={(e) => set("analyze_mode", e.target.value as "full" | "fast")} className={inp}><option value="full">精准</option><option value="fast">快速</option></select>
                </div>
              </div>
              <label className="flex cursor-pointer items-center gap-2 text-xs text-slate-300"><input type="checkbox" checked={form.deep} onChange={(e) => set("deep", e.target.checked)} /> 深入作品详情（从列表页逐个打开作品再抓，更全更慢）</label>
              <button onClick={() => setAdv((v) => !v)} className="text-[11px] text-sky-300 hover:underline">{adv ? "▾ 收起高级选项" : "▸ 高级选项（Cookie / 手机端 / 最小尺寸 / 间隔）"}</button>
              {adv && (
                <div className="space-y-2 rounded-lg bg-slate-800/50 p-3">
                  <div className="grid grid-cols-3 gap-2">
                    <div><label className={lab}>最小边长px</label><input type="number" min={32} value={form.min_size} onChange={(e) => set("min_size", Number(e.target.value) || 200)} className={inp} /></div>
                    <div><label className={lab}>操作间隔秒</label><input type="number" step={0.1} min={0.2} value={form.delay} onChange={(e) => set("delay", Number(e.target.value) || 0.8)} className={inp} /></div>
                    <div><label className={lab}>作品数上限</label><input type="number" min={1} max={60} value={form.deep_limit} onChange={(e) => set("deep_limit", Number(e.target.value) || 12)} className={inp} /></div>
                  </div>
                  <label className="flex cursor-pointer items-center gap-2 text-xs text-slate-300"><input type="checkbox" checked={form.mobile} onChange={(e) => set("mobile", e.target.checked)} /> 模拟手机端页面（竖屏截图，部分平台手机页更易访问）</label>
                  <div>
                    <label className={lab}>Cookie（可选：你自己账号的登录态；只在服务端内存中使用，不保存、不写日志）</label>
                    <input type="password" value={form.cookie} onChange={(e) => set("cookie", e.target.value)} placeholder="a=1; b=2" className={`${inp} font-mono`} autoComplete="off" />
                  </div>
                </div>
              )}
              {err && <div className="rounded-lg bg-red-500/10 p-2 text-xs text-red-300 ring-1 ring-red-500/30">❌ {err}</div>}
              <div className="flex gap-2">
                <button onClick={start} disabled={!canStart} className="flex-1 rounded-xl bg-emerald-500 px-4 py-2.5 text-sm font-bold text-emerald-950 shadow-lg shadow-emerald-900/40 transition hover:bg-emerald-400 active:scale-95 disabled:cursor-not-allowed disabled:opacity-40">
                  {starting ? "启动中…" : running ? "爬取中…" : "▶ 开始爬取并审核"}
                </button>
                <button onClick={stop} disabled={!running} className="rounded-xl bg-red-600 px-4 py-2.5 text-sm font-bold text-white transition hover:bg-red-500 active:scale-95 disabled:opacity-30">■ 停止</button>
              </div>
              <p className="text-[11px] leading-relaxed text-slate-500">合规提示：仅用于内容审核，以及自有或已获授权的内容，并遵守目标平台服务条款。遇到登录/验证码页面时系统只会提示，不会绕过。</p>
            </section>

            {/* 右：进度 + GPU + 日志 */}
            <section className="min-w-0 space-y-3 rounded-2xl bg-slate-900/70 p-4 ring-1 ring-slate-800">
              <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
                <b className={`text-sm ${view ? STATE_UI[view.state].cls : "text-slate-400"}`}>{view ? STATE_UI[view.state].text : "尚未开始"}</b>
                {view && <span className="text-xs text-slate-500">用时 {view.elapsed}s · 引擎 {view.engine === "playwright" ? "Chromium" : view.engine || "-"} · 审核并发 {view.conc} · 待审 {view.pending}</span>}
                <div className="ml-auto flex gap-2"><button onClick={exportCsv} disabled={!items.length} className={btn}>导出 CSV</button></div>
              </div>
              {s && (
                <div className="grid grid-cols-3 gap-2 text-center text-xs sm:grid-cols-6">
                  {([["抓到图片", s.found, "text-sky-300"], ["页面截图", s.screenshots, "text-sky-300"], ["已审核", s.analyzed, "text-slate-200"], ["拦截", s.block, "text-red-300"], ["待复核", s.review, "text-amber-300"], ["通过", s.pass, "text-emerald-300"]] as const).map(([t, v, c]) => (
                    <div key={t} className="rounded-lg bg-slate-800/70 py-2"><div className={`text-lg font-bold ${c}`}>{v}</div><div className="text-[10px] text-slate-500">{t}</div></div>
                  ))}
                </div>
              )}
              <div className="h-1.5 overflow-hidden rounded-full bg-slate-800"><div className="h-full bg-gradient-to-r from-sky-400 to-emerald-400 transition-all" style={{ width: `${pct}%` }} /></div>
              {s && <div className="text-[11px] text-slate-500">已过滤：重复 {s.dup} · 过小/非图片 {s.small} · 失败 {s.errors}{view?.cap_hit ? " · 已达到最多张数上限" : ""}{view && Object.keys(view.gpu_count).length > 0 ? ` · 各卡完成数：${Object.entries(view.gpu_count).map(([k, v]) => `${k.replace(/^.*?(cuda:\d|mock:\d)/, "$1")}=${v}`).join(" / ")}` : ""}</div>}
              {health && online === "online" ? <GpuPanel health={health} /> : <div className="rounded-lg bg-slate-800/40 p-3 text-xs text-slate-500">连接后这里实时显示两张 T4 的忙/闲、显存和已处理数量。</div>}
              <div ref={logRef} className="max-h-36 overflow-y-auto whitespace-pre-wrap rounded-lg bg-black/40 p-2.5 font-mono text-[11px] leading-relaxed text-slate-400">{view?.logs?.length ? view.logs.join("\n") : "运行日志会显示在这里…"}</div>
            </section>
          </div>

          {/* 结果 */}
          {items.length > 0 && job && (
            <section className="space-y-3">
              <div className="flex flex-wrap items-center gap-2">
                {tabs.map(([k, t]) => (
                  <button key={k} onClick={() => { setFilter(k); setLimit(96); }} className={`rounded-full px-3 py-1 text-xs ring-1 transition ${filter === k ? "bg-sky-500 text-white ring-sky-400" : "bg-slate-800 text-slate-300 ring-slate-700 hover:bg-slate-700"}`}>{t} <span className="opacity-70">{counts[k]}</span></button>
                ))}
                <label className="ml-auto flex cursor-pointer items-center gap-1.5 text-xs text-slate-400"><input type="checkbox" checked={sortRisk} onChange={(e) => setSortRisk(e.target.checked)} /> 高风险优先</label>
              </div>
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5">
                {filtered.slice(0, limit).map((it) => <Card key={it.idx} it={it} api={api} job={job} onOpen={onOpen} />)}
              </div>
              {filtered.length > limit && <div className="text-center"><button onClick={() => setLimit((l) => l + 96)} className={btn}>显示更多（还有 {filtered.length - limit} 张）</button></div>}
            </section>
          )}
          {!items.length && (
            <div className="rounded-2xl border border-dashed border-slate-800 p-8 text-center text-sm text-slate-500">
              {running ? "正在打开页面并抓取，第一批图片很快出现…" : "填写网址或关键词，点击「开始爬取并审核」。抓到一张就审一张，结果实时出现在这里。"}
            </div>
          )}
        </div>
      </div>
      {cur && job && <Detail it={cur} list={filtered} api={api} job={job} onGo={setDetail} onClose={() => setDetail(null)} />}
    </div>
  );
}

import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { analyzeDemo, analyzeRemote, askRemote, fetchHealth, normalizeApiUrl, prepareImage } from "@/lib/analyzer";
import { copyText } from "@/lib/clipboard";
import type { AnalysisResult, HealthInfo, ScanItem, Verdict } from "@/lib/types";

type Kind = Verdict | "queued" | "error";
type Filter = "all" | Kind;
type Conn = "none" | "checking" | "loading" | "online" | "offline";

const IMG_RE = /\.(jpe?g|png|webp|gif|bmp|avif|heic|heif|tiff?)$/i;
const isImage = (f: File) => f.type.startsWith("image/") || IMG_RE.test(f.name);

const VERDICT_UI: Record<Verdict, { text: string; cls: string; label: string }> = {
  pass: { text: "✅ 通过", label: "通过", cls: "bg-emerald-500/15 text-emerald-300 ring-emerald-500/40" },
  review: { text: "⚠️ 待复核", label: "待复核", cls: "bg-amber-500/15 text-amber-300 ring-amber-500/40" },
  block: { text: "🚫 拦截", label: "拦截", cls: "bg-red-500/15 text-red-300 ring-red-500/40" },
};

const kindOf = (it: ScanItem): Kind => (it.status === "done" && it.res ? it.res.verdict : it.status === "error" ? "error" : "queued");

async function readEntry(entry: FileSystemEntry, out: File[]): Promise<void> {
  if (entry.isFile) {
    await new Promise<void>((res) => (entry as FileSystemFileEntry).file((f) => (out.push(f), res()), () => res()));
  } else if (entry.isDirectory) {
    const reader = (entry as FileSystemDirectoryEntry).createReader();
    for (;;) {
      const batch = await new Promise<FileSystemEntry[]>((res) => reader.readEntries(res, () => res([])));
      if (!batch.length) break;
      for (const e of batch) await readEntry(e, out);
    }
  }
}

async function filesFromDrop(dt: DataTransfer): Promise<File[]> {
  const out: File[] = [];
  const entries: FileSystemEntry[] = [];
  for (const item of Array.from(dt.items ?? [])) {
    const en = item.webkitGetAsEntry?.();
    if (en) entries.push(en); // 必须在 await 之前同步取出，否则会失效
  }
  if (entries.length) for (const en of entries) await readEntry(en, out);
  else out.push(...Array.from(dt.files));
  return out.filter(isImage);
}

function Bar({ label, value }: { label: string; value: number }) {
  const hot = value >= 0.5;
  return (
    <div className="flex items-center gap-2 text-[11px] text-slate-400">
      <span className="w-8 shrink-0">{label}</span>
      <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-slate-700/60">
        <div className={`h-full rounded-full ${hot ? "bg-red-400" : value >= 0.25 ? "bg-amber-400" : "bg-emerald-400"}`} style={{ width: `${Math.round(value * 100)}%` }} />
      </div>
      <span className="w-8 text-right tabular-nums">{Math.round(value * 100)}%</span>
    </div>
  );
}

const Card = memo(
  function Card({ it, onOpen }: { it: ScanItem; rev: number; onOpen: (id: number) => void }) {
    const k = kindOf(it);
    const res = it.res;
    return (
      <button
        onClick={() => onOpen(it.id)}
        className="group flex flex-col overflow-hidden rounded-xl border border-slate-700/60 bg-slate-900/70 text-left transition hover:border-sky-500/60 hover:shadow-lg hover:shadow-sky-900/20"
        style={{ contentVisibility: "auto", containIntrinsicSize: "280px 270px" }}
      >
        <div className="relative aspect-[4/3] w-full overflow-hidden bg-slate-950">
          {it.thumb ? (
            <img src={it.thumb} alt={it.name} className="h-full w-full object-cover" draggable={false} />
          ) : (
            <div className="grid h-full place-items-center text-xs text-slate-500">{it.status === "running" ? "读取中…" : "排队中"}</div>
          )}
          <span
            className={`absolute left-2 top-2 rounded-md px-2 py-0.5 text-xs font-semibold ring-1 backdrop-blur ${
              k === "queued" ? "bg-slate-800/80 text-slate-300 ring-slate-600" : k === "error" ? "bg-red-900/70 text-red-200 ring-red-500/50" : VERDICT_UI[k].cls
            }`}
          >
            {k === "queued" ? (it.status === "running" ? "🔍 审查中" : "⏳ 排队") : k === "error" ? "❌ 失败" : VERDICT_UI[k].text}
          </span>
          {res?.engine === "demo" && <span className="absolute right-2 top-2 rounded bg-slate-800/80 px-1.5 py-0.5 text-[10px] text-slate-300 ring-1 ring-slate-600">演示</span>}
        </div>
        <div className="flex flex-1 flex-col gap-1.5 p-3">
          <div className="truncate text-xs text-slate-400" title={it.name}>{it.name}</div>
          {res && (
            <>
              <p className="line-clamp-2 min-h-[2.5em] text-[13px] leading-snug text-slate-200">{res.description}</p>
              <Bar label="涉黄" value={res.porn.score} />
              <Bar label="涉政" value={res.political.score} />
            </>
          )}
          {it.err && <p className="text-xs text-red-300">{it.err}</p>}
        </div>
      </button>
    );
  },
  (a, b) => a.rev === b.rev,
);

function download(name: string, text: string, type: string) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([text], { type }));
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 3000);
}

interface HistoryRow {
  t: number;
  name: string;
  verdict: Verdict;
  description: string;
  engine: string;
  porn: number;
  pol: number;
}
const HIST_KEY = "mod.history";
const loadHistory = (): HistoryRow[] => {
  try {
    return JSON.parse(localStorage.getItem(HIST_KEY) || "[]") as HistoryRow[];
  } catch {
    return [];
  }
};

const fmtTime = (s: number) => (s < 60 ? `${Math.round(s)}秒` : s < 3600 ? `${Math.floor(s / 60)}分${Math.round(s % 60)}秒` : `${Math.floor(s / 3600)}小时${Math.round((s % 3600) / 60)}分`);

function readApiParam(): string {
  const fromSearch = new URLSearchParams(window.location.search).get("api");
  if (fromSearch) return fromSearch;
  const h = window.location.hash;
  const i = h.indexOf("?");
  return i >= 0 ? new URLSearchParams(h.slice(i + 1)).get("api") || "" : "";
}

export default function ModerationApp({ compact = false }: { compact?: boolean }) {
  const [items, setItems] = useState<ScanItem[]>([]);
  const [filter, setFilter] = useState<Filter>("all");
  const [sortRisk, setSortRisk] = useState(false);
  const [limit, setLimit] = useState(120);
  const [apiUrl, setApiUrl] = useState("");
  const [apiDraft, setApiDraft] = useState("");
  const [mode, setMode] = useState<"full" | "fast">("full");
  const [conn, setConn] = useState<Conn>("none");
  const [health, setHealth] = useState<HealthInfo | null>(null);
  const [forceDemo, setForceDemo] = useState(false);
  const [paused, setPaused] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [history, setHistory] = useState<HistoryRow[]>([]);
  const [detailId, setDetailId] = useState<number | null>(null);
  const [clock, setClock] = useState(0);

  const itemsRef = useRef<ScanItem[]>([]);
  const queueRef = useRef<ScanItem[]>([]);
  const headRef = useRef(0);
  const runningRef = useRef(0);
  const nextId = useRef(0);
  const histBuf = useRef<HistoryRow[]>([]);
  const batchStart = useRef(0);
  const batchBase = useRef(0);
  const bumpTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const dragDepth = useRef(0);
  const fileInput = useRef<HTMLInputElement | null>(null);
  const dirInput = useRef<HTMLInputElement | null>(null);
  const cfg = useRef({ api: "", mode: "full" as "full" | "fast", online: false, gate: true, paused: false, conc: 6 });

  const bump = useCallback(() => {
    if (bumpTimer.current) return;
    bumpTimer.current = setTimeout(() => {
      bumpTimer.current = null;
      setItems(itemsRef.current.slice());
    }, 120);
  }, []);

  /* ---------- 初始化：读取本地设置 / ?api= 参数 ---------- */
  useEffect(() => {
    const q = readApiParam();
    const saved = q ? normalizeApiUrl(q) : localStorage.getItem("mod.api") || "";
    if (q) localStorage.setItem("mod.api", saved);
    const m = localStorage.getItem("mod.mode");
    const t = setTimeout(() => {
      setApiUrl(saved);
      setApiDraft(saved);
      if (m === "fast" || m === "full") setMode(m);
    }, 0);
    dirInput.current?.setAttribute("webkitdirectory", "");
    return () => clearTimeout(t);
  }, []);

  /* ---------- 连接状态轮询 ---------- */
  useEffect(() => {
    cfg.current.api = apiUrl;
    if (!apiUrl) {
      const t = setTimeout(() => {
        setConn("none");
        setHealth(null);
      }, 0);
      return () => clearTimeout(t);
    }
    let stop = false;
    let timer: ReturnType<typeof setTimeout>;
    const tick = async () => {
      try {
        const h = await fetchHealth(apiUrl, AbortSignal.timeout(6000));
        if (stop) return;
        setHealth(h);
        setConn(h.ready ? "online" : h.status === "error" ? "offline" : "loading");
      } catch {
        if (stop) return;
        setHealth(null);
        setConn("offline");
      }
      timer = setTimeout(tick, 4000);
    };
    setConn("checking");
    void tick();
    return () => {
      stop = true;
      clearTimeout(timer);
    };
  }, [apiUrl]);

  /* ---------- 并发队列 ---------- */
  const processItem = useCallback(
    async (it: ScanItem) => {
      const t0 = performance.now();
      it.status = "running";
      it.err = undefined;
      it.rev++;
      bump();
      try {
        const prep = await prepareImage(it.file);
        it.thumb = prep.thumb;
        let res: AnalysisResult;
        if (cfg.current.online && cfg.current.api) res = await analyzeRemote(cfg.current.api, prep.blob, it.name, cfg.current.mode);
        else res = await analyzeDemo(prep.blob, prep.width, prep.height);
        it.res = res;
        it.status = "done";
        it.elapsedMs = Math.round(performance.now() - t0);
        histBuf.current.push({ t: Date.now(), name: it.name, verdict: res.verdict, description: res.description, engine: res.engine, porn: res.porn.score, pol: res.political.score });
      } catch (e) {
        it.status = "error";
        it.err = e instanceof Error ? e.message : "处理失败";
      }
      it.rev++;
    },
    [bump],
  );

  const pump = useCallback(() => {
    const c = cfg.current;
    if (c.paused || !c.gate) return;
    while (runningRef.current < c.conc && headRef.current < queueRef.current.length) {
      const it = queueRef.current[headRef.current++];
      if (it.status !== "queued") continue;
      runningRef.current++;
      void processItem(it).finally(() => {
        runningRef.current--;
        bump();
        pump();
      });
    }
  }, [bump, processItem]);

  useEffect(() => {
    const hasApi = !!apiUrl;
    const online = conn === "online";
    cfg.current.online = online;
    cfg.current.mode = mode;
    cfg.current.paused = paused;
    cfg.current.gate = !hasApi || online || forceDemo;
    cfg.current.conc = online ? Math.max(3, Math.min(10, (health?.replicas || 1) * 3)) : 6;
    pump();
  }, [apiUrl, conn, mode, paused, forceDemo, health?.replicas, pump]);

  /* 历史记录：批量落盘（最多保留 1000 条） */
  useEffect(() => {
    setHistory(loadHistory());
    const t = setInterval(() => {
      if (!histBuf.current.length) return;
      const merged = [...histBuf.current.splice(0), ...loadHistory()].slice(0, 1000);
      try {
        localStorage.setItem(HIST_KEY, JSON.stringify(merged));
      } catch {
        /* 配额满了就忽略 */
      }
    }, 2500);
    return () => clearInterval(t);
  }, []);

  /* 吞吐/耗时时钟 */
  useEffect(() => {
    const t = setInterval(() => setClock(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);

  const addFiles = useCallback(
    (files: File[]) => {
      const imgs = files.filter(isImage);
      if (!imgs.length) return;
      const idle = !itemsRef.current.some((i) => i.status === "queued" || i.status === "running");
      if (idle) {
        batchStart.current = Date.now();
        batchBase.current = itemsRef.current.filter((i) => i.status === "done" || i.status === "error").length;
      }
      for (const f of imgs) {
        const it: ScanItem = { id: nextId.current++, file: f, name: (f as File & { webkitRelativePath?: string }).webkitRelativePath || f.name, status: "queued", rev: 0 };
        itemsRef.current.push(it);
        queueRef.current.push(it);
      }
      setItems(itemsRef.current.slice());
      pump();
    },
    [pump],
  );

  /* 粘贴图片 */
  useEffect(() => {
    const onPaste = (e: ClipboardEvent) => {
      const fs = Array.from(e.clipboardData?.files ?? []);
      if (fs.length) addFiles(fs);
    };
    window.addEventListener("paste", onPaste);
    return () => window.removeEventListener("paste", onPaste);
  }, [addFiles]);

  const requeue = useCallback(
    (pred: (i: ScanItem) => boolean) => {
      let n = 0;
      for (const it of itemsRef.current) {
        if (pred(it)) {
          it.status = "queued";
          it.err = undefined;
          it.rev++;
          queueRef.current.push(it);
          n++;
        }
      }
      if (n) {
        batchStart.current = Date.now();
        batchBase.current = itemsRef.current.filter((i) => i.status === "done" || i.status === "error").length;
        setItems(itemsRef.current.slice());
        pump();
      }
    },
    [pump],
  );

  const clearAll = () => {
    itemsRef.current = [];
    queueRef.current = [];
    headRef.current = 0;
    setItems([]);
    setDetailId(null);
    setLimit(120);
  };

  /* ---------- 统计 ---------- */
  const stats = useMemo(() => {
    const s = { total: items.length, pass: 0, review: 0, block: 0, error: 0, queued: 0, demo: 0 };
    for (const it of items) {
      const k = kindOf(it);
      s[k]++;
      if (it.res?.engine === "demo") s.demo++;
    }
    return s;
  }, [items]);
  const finished = stats.total - stats.queued;
  const active = stats.queued > 0;
  const elapsed = batchStart.current ? Math.max(1, ((clock || Date.now()) - batchStart.current) / 1000) : 1;
  const doneInBatch = Math.max(0, finished - batchBase.current);
  const speed = doneInBatch / elapsed;
  const eta = speed > 0 ? stats.queued / speed : 0;

  const filtered = useMemo(() => {
    let list = filter === "all" ? items : items.filter((i) => kindOf(i) === filter);
    if (sortRisk) {
      const w = (i: ScanItem) => (i.res ? (i.res.verdict === "block" ? 3 : i.res.verdict === "review" ? 2 : 1) * 10 + Math.max(i.res.porn.score, i.res.political.score) : 0);
      list = list.slice().sort((a, b) => w(b) - w(a));
    }
    return list;
  }, [items, filter, sortRisk]);
  const shown = filtered.slice(0, limit);
  const onOpen = useCallback((id: number) => setDetailId(id), []);

  const exportCsv = () => {
    const q = (v: unknown) => `"${String(v ?? "").replace(/"/g, '""')}"`;
    const rows = [["文件名", "结论", "涉黄分", "涉黄等级", "涉政分", "涉政等级", "描述", "原因", "图中文字", "引擎"].map(q).join(",")];
    for (const it of items) {
      const r = it.res;
      rows.push([it.name, r ? VERDICT_UI[r.verdict].label : it.status === "error" ? "失败" : "未完成", r?.porn.score ?? "", r?.porn.level ?? "", r?.political.score ?? "", r?.political.level ?? "", r?.description ?? it.err ?? "", (r?.reasons ?? []).join("；"), r?.ocr_text ?? "", r?.engine ?? ""].map(q).join(","));
    }
    download(`审查结果-${new Date().toISOString().slice(0, 10)}.csv`, "\ufeff" + rows.join("\n"), "text/csv;charset=utf-8");
  };
  const exportJson = () =>
    download("审查结果.json", JSON.stringify(items.map((i) => ({ name: i.name, status: i.status, error: i.err, result: i.res })), null, 2), "application/json");

  const connectApi = () => {
    const u = normalizeApiUrl(apiDraft);
    localStorage.setItem("mod.api", u);
    setForceDemo(false);
    setApiUrl(u);
  };

  /* ---------- 连接状态展示 ---------- */
  const pill = (() => {
    switch (conn) {
      case "online":
        return { cls: "bg-emerald-500/15 text-emerald-300 ring-emerald-500/40", text: `● GPU 在线 · ${(health?.gpus || []).map((g) => g.name).join(" + ") || "CPU"}${health?.vlm_enabled ? " · 大模型" : ""}` };
      case "loading":
        return { cls: "bg-amber-500/15 text-amber-300 ring-amber-500/40", text: `◌ GPU 加载模型中（${health?.replicas_ready ?? 0}/${health?.replicas ?? "?"}）` };
      case "checking":
        return { cls: "bg-slate-700/50 text-slate-300 ring-slate-600", text: "◌ 连接中…" };
      case "offline":
        return { cls: "bg-red-500/15 text-red-300 ring-red-500/40", text: "✕ 无法连接 Kaggle（点此设置）" };
      default:
        return { cls: "bg-slate-700/50 text-slate-300 ring-slate-600", text: "演示模式 · 未连接 GPU（点此连接）" };
    }
  })();
  const holding = !!apiUrl && conn !== "online" && !forceDemo && stats.queued > 0;

  const detail = detailId == null ? null : items.find((i) => i.id === detailId) ?? null;
  const filterTabs: [Filter, string, number][] = [
    ["all", "全部", stats.total],
    ["block", "🚫 拦截", stats.block],
    ["review", "⚠️ 待复核", stats.review],
    ["pass", "✅ 通过", stats.pass],
    ["queued", "⏳ 处理中", stats.queued],
    ["error", "❌ 失败", stats.error],
  ];
  const btn = "rounded-lg bg-slate-800 px-3 py-1.5 text-xs font-medium text-slate-200 ring-1 ring-slate-700 transition hover:bg-slate-700 active:scale-95 disabled:opacity-40";

  return (
    <div
      className="relative h-full min-h-0 bg-slate-950 text-slate-200"
      onDragEnter={(e) => {
        e.preventDefault();
        dragDepth.current++;
        setDragging(true);
      }}
      onDragOver={(e) => e.preventDefault()}
      onDragLeave={() => {
        dragDepth.current = Math.max(0, dragDepth.current - 1);
        if (!dragDepth.current) setDragging(false);
      }}
      onDrop={async (e) => {
        e.preventDefault();
        dragDepth.current = 0;
        setDragging(false);
        addFiles(await filesFromDrop(e.dataTransfer));
      }}
    >
      <div className="h-full overflow-y-auto">
        <div className={`mx-auto max-w-7xl space-y-4 px-4 ${compact ? "py-3" : "py-5"}`}>
          {/* 顶栏 */}
          <header className="flex flex-wrap items-center gap-3">
            <div className="mr-auto">
              <h1 className="text-xl font-bold text-white">🛡️ 图片智能审查系统</h1>
              <p className="text-xs text-slate-400">拖拽 / 上传图片（不限数量）→ 自动判断涉黄、涉政，并生成中文描述 · Kaggle T4×2 GPU 加速</p>
            </div>
            <button onClick={() => setSettingsOpen(true)} className={`rounded-full px-3 py-1.5 text-xs font-medium ring-1 ${pill.cls}`}>{pill.text}</button>
            <button onClick={() => { setHistory(loadHistory()); setHistoryOpen(true); }} className={btn}>🕘 历史</button>
            <button onClick={() => setSettingsOpen(true)} className={btn}>⚙️ 设置</button>
          </header>

          {/* 上传区 */}
          <div className={`rounded-2xl border-2 border-dashed p-6 text-center transition ${dragging ? "border-sky-400 bg-sky-500/10" : "border-slate-700 bg-slate-900/50 hover:border-slate-500"}`}>
            <div className="text-4xl">🖼️</div>
            <p className="mt-2 text-base font-semibold text-white">把图片或整个文件夹拖到这里</p>
            <p className="mt-1 text-xs text-slate-400">也可以 Ctrl+V 粘贴截图 · 支持 JPG / PNG / WebP / GIF / BMP / AVIF · 上传前自动压缩到 1280px，更快更省流量</p>
            <div className="mt-4 flex flex-wrap justify-center gap-3">
              <button onClick={() => fileInput.current?.click()} className="rounded-xl bg-sky-500 px-5 py-2.5 text-sm font-bold text-white shadow-lg shadow-sky-900/40 transition hover:bg-sky-400 active:scale-95">选择图片</button>
              <button onClick={() => dirInput.current?.click()} className="rounded-xl bg-slate-700 px-5 py-2.5 text-sm font-bold text-white transition hover:bg-slate-600 active:scale-95">选择文件夹</button>
            </div>
            <input ref={fileInput} type="file" accept="image/*" multiple hidden onChange={(e) => { addFiles(Array.from(e.target.files ?? [])); e.target.value = ""; }} />
            <input ref={dirInput} type="file" multiple hidden onChange={(e) => { addFiles(Array.from(e.target.files ?? [])); e.target.value = ""; }} />
          </div>

          {holding && (
            <div className="flex flex-wrap items-center gap-3 rounded-xl border border-amber-500/40 bg-amber-500/10 px-4 py-3 text-sm text-amber-200">
              <span>⏳ 正在等待 Kaggle GPU 就绪（{conn === "offline" ? "当前无法连接，请确认 Notebook 仍在运行且网址正确" : "模型加载中"}）。图片已排队，就绪后自动开始。</span>
              <button onClick={() => setForceDemo(true)} className="ml-auto rounded-lg bg-amber-500 px-3 py-1.5 text-xs font-bold text-amber-950 hover:bg-amber-400">先用演示模式</button>
            </div>
          )}

          {stats.total > 0 && (
            <>
              {/* 进度 */}
              <div className="rounded-xl border border-slate-800 bg-slate-900/60 p-4">
                <div className="flex flex-wrap items-center gap-x-5 gap-y-1 text-sm">
                  <span className="font-semibold text-white">{finished} / {stats.total}</span>
                  {active ? <span className="text-slate-400">速度 {speed.toFixed(1)} 张/秒 · 预计剩余 {fmtTime(eta)}</span> : <span className="text-emerald-300">✓ 全部处理完成</span>}
                  <span className="text-red-300">拦截 {stats.block}</span>
                  <span className="text-amber-300">待复核 {stats.review}</span>
                  <span className="text-emerald-300">通过 {stats.pass}</span>
                  {stats.error > 0 && <span className="text-red-400">失败 {stats.error}</span>}
                </div>
                <div className="mt-2 h-2 overflow-hidden rounded-full bg-slate-800">
                  <div className="h-full rounded-full bg-gradient-to-r from-sky-500 to-emerald-400 transition-all" style={{ width: `${stats.total ? (finished / stats.total) * 100 : 0}%` }} />
                </div>
                <div className="mt-3 flex flex-wrap gap-2">
                  <button onClick={() => setPaused((p) => !p)} className={btn} disabled={!active}>{paused ? "▶ 继续" : "⏸ 暂停"}</button>
                  <button onClick={() => requeue((i) => i.status === "error")} className={btn} disabled={!stats.error}>🔁 重试失败</button>
                  {stats.demo > 0 && conn === "online" && <button onClick={() => requeue((i) => i.res?.engine === "demo")} className={`${btn} !bg-sky-600 !text-white`}>🤖 用 AI 重审演示结果（{stats.demo}）</button>}
                  <button onClick={exportCsv} className={btn}>⬇ 导出 CSV</button>
                  <button onClick={exportJson} className={btn}>⬇ 导出 JSON</button>
                  <button onClick={() => setSortRisk((v) => !v)} className={`${btn} ${sortRisk ? "!bg-sky-600 !text-white" : ""}`}>风险优先排序</button>
                  <button onClick={clearAll} className={`${btn} ml-auto !text-red-300`}>🗑 清空</button>
                </div>
              </div>

              {/* 筛选 */}
              <div className="flex flex-wrap gap-2">
                {filterTabs.map(([k, label, n]) => (
                  <button key={k} onClick={() => { setFilter(k); setLimit(120); }} className={`rounded-full px-3.5 py-1.5 text-xs font-semibold ring-1 transition ${filter === k ? "bg-sky-500 text-white ring-sky-400" : "bg-slate-900 text-slate-300 ring-slate-700 hover:bg-slate-800"}`}>
                    {label} <span className="opacity-70">{n}</span>
                  </button>
                ))}
              </div>

              {/* 卡片网格 */}
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5">
                {shown.map((it) => <Card key={it.id} it={it} rev={it.rev} onOpen={onOpen} />)}
              </div>
              {filtered.length > shown.length && (
                <div className="pb-6 text-center">
                  <button onClick={() => setLimit((l) => l + 240)} className="rounded-xl bg-slate-800 px-6 py-2.5 text-sm font-semibold text-white ring-1 ring-slate-600 hover:bg-slate-700">
                    显示更多（还有 {filtered.length - shown.length} 张）
                  </button>
                </div>
              )}
              {filtered.length === 0 && <div className="py-10 text-center text-sm text-slate-500">这个分类下暂时没有图片</div>}
            </>
          )}

          {stats.total === 0 && (
            <div className="grid gap-3 pt-2 text-sm text-slate-400 sm:grid-cols-3">
              {[
                ["① 连接 GPU", "在「设置」填入 Kaggle 输出的 trycloudflare 网址，即可使用 T4×2 + 大模型精准审查。"],
                ["② 拖入图片", "数量不限，文件夹也行。边压缩边上传，GPU 空闲副本抢占处理，界面不卡顿。"],
                ["③ 看结果", "每张图给出 通过 / 待复核 / 拦截 + 一句中文描述 + 判定原因，可导出 CSV。"],
              ].map(([t, d]) => (
                <div key={t} className="rounded-xl border border-slate-800 bg-slate-900/50 p-4"><div className="mb-1 font-semibold text-slate-200">{t}</div>{d}</div>
              ))}
            </div>
          )}
          <p className="pb-4 text-center text-[11px] leading-relaxed text-slate-600">涉政没有统一的视觉标准：系统综合「大模型判断 + CLIP 线索 + 图中文字关键词」做初筛，不等于 100% 准确，边界样本请人工复核。</p>
        </div>
      </div>

      {dragging && (
        <div className="pointer-events-none absolute inset-0 z-30 grid place-items-center bg-sky-950/70 backdrop-blur-sm">
          <div className="rounded-2xl border-2 border-dashed border-sky-300 px-10 py-8 text-xl font-bold text-sky-100">松开鼠标，开始审查</div>
        </div>
      )}

      {/* 设置 */}
      {settingsOpen && (
        <div className="absolute inset-0 z-40 grid place-items-center bg-black/60 p-4" onClick={() => setSettingsOpen(false)}>
          <div className="max-h-full w-full max-w-lg overflow-y-auto rounded-2xl border border-slate-700 bg-slate-900 p-5 shadow-2xl" onClick={(e) => e.stopPropagation()}>
            <div className="mb-3 flex items-center justify-between"><h2 className="text-lg font-bold text-white">⚙️ 设置</h2><button onClick={() => setSettingsOpen(false)} className="text-slate-400 hover:text-white">✕</button></div>
            <label className="text-xs font-semibold text-slate-300">Kaggle 公网网址（运行 Kaggle Cell 后输出的 https://xxxx.trycloudflare.com）</label>
            <div className="mt-1 flex gap-2">
              <input value={apiDraft} onChange={(e) => setApiDraft(e.target.value)} onKeyDown={(e) => e.key === "Enter" && connectApi()} placeholder="https://xxxx.trycloudflare.com" className="min-w-0 flex-1 rounded-lg bg-slate-800 px-3 py-2 text-sm text-white outline-none ring-1 ring-slate-600 focus:ring-sky-400" />
              <button onClick={connectApi} className="rounded-lg bg-sky-500 px-4 text-sm font-bold text-white hover:bg-sky-400">连接</button>
            </div>
            <div className={`mt-2 inline-block rounded-full px-3 py-1 text-xs ring-1 ${pill.cls}`}>{pill.text}</div>
            {health && (
              <div className="mt-2 space-y-0.5 text-xs text-slate-400">
                <div>副本 {health.replicas_ready}/{health.replicas} · 已处理 {health.processed} 张 · 平均 {health.avg_ms}ms/张</div>
                <div>模型：{health.models.nsfw} / {health.models.clip}{health.models.vlm ? ` / ${health.models.vlm}` : "（大模型未加载）"}</div>
                {(health.warnings || []).map((w) => <div key={w} className="text-amber-300">⚠️ {w}</div>)}
              </div>
            )}
            {conn === "offline" && <p className="mt-2 text-xs text-red-300">无法连接：请确认 Kaggle Notebook 的启动 Cell 仍在运行，并且网址是最新的（每次重启都会变）。</p>}
            <div className="mt-4 text-xs font-semibold text-slate-300">审查模式</div>
            <div className="mt-1 grid grid-cols-2 gap-2">
              {([["full", "完整（推荐）", "分类器 + CLIP + 大模型：描述最准、涉政判断更可靠"], ["fast", "快速", "仅分类器 + CLIP：速度最快，描述较简单"]] as const).map(([k, t, d]) => (
                <button key={k} onClick={() => { setMode(k); localStorage.setItem("mod.mode", k); }} className={`rounded-lg p-3 text-left text-xs ring-1 ${mode === k ? "bg-sky-500/15 ring-sky-400" : "bg-slate-800 ring-slate-700"}`}>
                  <div className="font-bold text-white">{t}</div><div className="mt-0.5 text-slate-400">{d}</div>
                </button>
              ))}
            </div>
            <div className="mt-4 flex gap-2">
              <button onClick={() => { localStorage.removeItem("mod.api"); setApiDraft(""); setApiUrl(""); }} className={btn}>断开 / 清除网址</button>
              <button onClick={async () => { if (await copyText(window.location.href)) alert("已复制当前页面地址"); }} className={btn}>复制本页地址</button>
            </div>
          </div>
        </div>
      )}

      {/* 历史 */}
      {historyOpen && (
        <div className="absolute inset-0 z-40 grid place-items-center bg-black/60 p-4" onClick={() => setHistoryOpen(false)}>
          <div className="flex max-h-full w-full max-w-3xl flex-col rounded-2xl border border-slate-700 bg-slate-900 p-5 shadow-2xl" onClick={(e) => e.stopPropagation()}>
            <div className="mb-3 flex items-center justify-between"><h2 className="text-lg font-bold text-white">🕘 历史记录（本机，最近 {history.length} 条）</h2>
              <div className="flex gap-2"><button className={btn} onClick={() => { localStorage.removeItem(HIST_KEY); setHistory([]); }}>清空</button><button onClick={() => setHistoryOpen(false)} className="text-slate-400 hover:text-white">✕</button></div></div>
            <div className="min-h-0 flex-1 space-y-1.5 overflow-y-auto">
              {history.length === 0 && <div className="py-8 text-center text-sm text-slate-500">暂无记录</div>}
              {history.slice(0, 300).map((h, i) => (
                <div key={i} className="flex items-start gap-3 rounded-lg bg-slate-800/60 px-3 py-2 text-xs">
                  <span className={`shrink-0 rounded px-1.5 py-0.5 ring-1 ${VERDICT_UI[h.verdict].cls}`}>{VERDICT_UI[h.verdict].label}</span>
                  <div className="min-w-0 flex-1"><div className="truncate text-slate-300">{h.name}</div><div className="text-slate-400">{h.description}</div></div>
                  <span className="shrink-0 text-slate-500">{new Date(h.t).toLocaleString()}</span>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      {detail && <Detail key={detail.id} it={detail} list={filtered} online={conn === "online"} api={apiUrl} onClose={() => setDetailId(null)} onGo={setDetailId} />}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* 详情弹窗                                                            */
/* ------------------------------------------------------------------ */
function Detail({ it, list, online, api, onClose, onGo }: { it: ScanItem; list: ScanItem[]; online: boolean; api: string; onClose: () => void; onGo: (id: number) => void }) {
  const [url, setUrl] = useState("");
  const [q, setQ] = useState("");
  const [ans, setAns] = useState("");
  const [asking, setAsking] = useState(false);
  const res = it.res;
  const idx = list.findIndex((i) => i.id === it.id);

  useEffect(() => {
    const u = URL.createObjectURL(it.file);
    setUrl(u);
    return () => URL.revokeObjectURL(u);
  }, [it.file]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLInputElement) return;
      if (e.key === "Escape") onClose();
      if (e.key === "ArrowLeft" && idx > 0) onGo(list[idx - 1].id);
      if (e.key === "ArrowRight" && idx >= 0 && idx < list.length - 1) onGo(list[idx + 1].id);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [idx, list, onClose, onGo]);

  const ask = async () => {
    if (!q.trim()) return;
    setAsking(true);
    setAns("");
    try {
      const prep = await prepareImage(it.file);
      setAns(await askRemote(api, prep.blob, q.trim()));
    } catch (e) {
      setAns("❌ " + (e instanceof Error ? e.message : "追问失败"));
    }
    setAsking(false);
  };

  const k = kindOf(it);
  return (
    <div className="absolute inset-0 z-40 flex items-stretch justify-center bg-black/75 p-2 sm:p-4" onClick={onClose}>
      <div className="relative flex w-full max-w-5xl flex-col overflow-hidden rounded-2xl border border-slate-700 bg-slate-900 shadow-2xl md:flex-row" onClick={(e) => e.stopPropagation()}>
        <div className="relative grid min-h-[200px] flex-1 place-items-center bg-black">
          {url && <img src={url} alt={it.name} className="max-h-full max-w-full object-contain" />}
          {idx > 0 && <button onClick={() => onGo(list[idx - 1].id)} className="absolute left-2 top-1/2 -translate-y-1/2 rounded-full bg-black/60 px-3 py-2 text-xl text-white hover:bg-black">‹</button>}
          {idx >= 0 && idx < list.length - 1 && <button onClick={() => onGo(list[idx + 1].id)} className="absolute right-2 top-1/2 -translate-y-1/2 rounded-full bg-black/60 px-3 py-2 text-xl text-white hover:bg-black">›</button>}
        </div>
        <div className="flex max-h-[55%] w-full flex-col gap-3 overflow-y-auto p-4 text-sm md:max-h-none md:w-[380px]">
          <div className="flex items-start justify-between gap-2">
            <div className="min-w-0 break-all text-xs text-slate-400">{it.name}</div>
            <button onClick={onClose} className="text-slate-400 hover:text-white">✕</button>
          </div>
          {k !== "queued" && k !== "error" && res ? (
            <>
              <div className={`inline-block self-start rounded-lg px-3 py-1 text-base font-bold ring-1 ${VERDICT_UI[res.verdict].cls}`}>{VERDICT_UI[res.verdict].text}</div>
              <p className="text-base leading-snug text-white">{res.description}</p>
              {res.reasons.length > 0 && <ul className="space-y-1 rounded-lg bg-slate-800/70 p-3 text-xs text-amber-200">{res.reasons.map((r) => <li key={r}>• {r}</li>)}</ul>}
              <div className="space-y-1.5 rounded-lg bg-slate-800/50 p-3">
                <Bar label="涉黄" value={res.porn.score} />
                <Bar label="涉政" value={res.political.score} />
                <div className="pt-1 text-[11px] text-slate-400">涉黄等级：{res.porn.level} · 涉政等级：{res.political.level}{res.political.labels?.length ? `（${res.political.labels.join("、")}）` : ""}</div>
              </div>
              {res.ocr_text && <div className="rounded-lg bg-slate-800/50 p-3 text-xs text-slate-300"><div className="mb-1 font-semibold text-slate-400">图中文字</div>{res.ocr_text}</div>}
              <div className="text-[11px] leading-relaxed text-slate-500">
                引擎：{res.engine}{res.gpu ? ` · ${res.gpu}` : ""}{res.cached ? " · 缓存命中" : ""}
                {res.timings?.total_ms != null && ` · GPU ${res.timings.total_ms}ms`}
                {res.signals && <div>分类器 {Math.round((res.signals.nsfw_classifier ?? 0) * 100)}% · CLIP 涉黄 {Math.round((res.signals.clip_porn ?? 0) * 100)}% · CLIP 涉政 {Math.round((res.signals.clip_political ?? 0) * 100)}%</div>}
              </div>
              {(res.warnings || []).map((w) => <div key={w} className="text-[11px] text-amber-300">⚠️ {w}</div>)}
              {online && res.engine !== "demo" && (
                <div className="mt-auto space-y-2 border-t border-slate-800 pt-3">
                  <div className="text-xs font-semibold text-slate-300">💬 对这张图继续追问大模型</div>
                  <div className="flex gap-2">
                    <input value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => e.key === "Enter" && ask()} placeholder="例如：图里的人在做什么？" className="min-w-0 flex-1 rounded-lg bg-slate-800 px-3 py-2 text-xs text-white outline-none ring-1 ring-slate-600 focus:ring-sky-400" />
                    <button onClick={ask} disabled={asking} className="rounded-lg bg-sky-500 px-3 text-xs font-bold text-white disabled:opacity-50">{asking ? "…" : "提问"}</button>
                  </div>
                  {ans && <div className="rounded-lg bg-slate-800/70 p-3 text-xs leading-relaxed text-slate-200">{ans}</div>}
                </div>
              )}
            </>
          ) : (
            <div className="text-slate-400">{it.err ? `❌ ${it.err}` : "处理中…"}</div>
          )}
        </div>
      </div>
    </div>
  );
}

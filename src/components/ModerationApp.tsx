"use client";

import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { analyzeDemo, analyzeRemote, askRemote, fetchHealth, normalizeApiUrl, prepareImage } from "@/lib/analyzer";
import type { AnalysisResult, HealthInfo, ScanItem, Verdict } from "@/lib/types";

type Kind = Verdict | "queued" | "error";
type Filter = "all" | Kind;
type Conn = "none" | "checking" | "loading" | "online" | "offline";

const IMG_RE = /\.(jpe?g|png|webp|gif|bmp|avif|heic|heif|tiff?)$/i;
const isImage = (f: File) => f.type.startsWith("image/") || IMG_RE.test(f.name);

const VERDICT_UI: Record<Verdict, { text: string; cls: string }> = {
  pass: { text: "✅ 通过", cls: "bg-emerald-500/15 text-emerald-300 ring-emerald-500/40" },
  review: { text: "⚠️ 待复核", cls: "bg-amber-500/15 text-amber-300 ring-amber-500/40" },
  block: { text: "🚫 拦截", cls: "bg-red-500/15 text-red-300 ring-red-500/40" },
};

function kindOf(it: ScanItem): Kind {
  if (it.status === "done" && it.res) return it.res.verdict;
  if (it.status === "error") return "error";
  return "queued";
}

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
    if (en) entries.push(en); // 必须在 await 之前同步取出
  }
  if (entries.length) for (const en of entries) await readEntry(en, out);
  else out.push(...Array.from(dt.files));
  return out.filter(isImage);
}

function Bar({ label, value, color }: { label: string; value: number; color: string }) {
  return (
    <div className="flex items-center gap-2 text-[11px] text-slate-400">
      <span className="w-8 shrink-0">{label}</span>
      <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-slate-700/60">
        <div className={`h-full rounded-full ${color}`} style={{ width: `${Math.round(value * 100)}%` }} />
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
        style={{ contentVisibility: "auto", containIntrinsicSize: "300px 280px" }}
      >
        <div className="relative aspect-[4/3] w-full overflow-hidden bg-slate-950">
          {it.thumb ? (
            // eslint-disable-next-line @next/next/no-img-element
            <img src={it.thumb} alt={it.name} className="h-full w-full object-cover" draggable={false} />
          ) : (
            <div className="grid h-full place-items-center text-xs text-slate-500">{it.status === "running" ? "读取中…" : "排队中"}</div>
          )}
          <span
            className={`absolute left-2 top-2 rounded-md px-2 py-0.5 text-xs font-semibold ring-1 backdrop-blur ${
              k === "queued"
                ? "bg-slate-800/80 text-slate-300 ring-slate-600"
                : k === "error"
                  ? "bg-red-900/70 text-red-200 ring-red-500/50"
                  : VERDICT_UI[k].cls
            }`}
          >
            {k === "queued" ? (it.status === "running" ? "🔍 审查中" : "⏳ 排队") : k === "error" ? "❌ 失败" : VERDICT_UI[k].text}
          </span>
          {res?.engine === "demo" && (
            <span className="absolute right-2 top-2 rounded bg-slate-800/80 px-1.5 py-0.5 text-[10px] text-slate-300 ring-1 ring-slate-600">演示</span>
          )}
        </div>
        <div className="flex flex-1 flex-col gap-1.5 p-3">
          <div className="truncate text-xs text-slate-400" title={it.name}>
            {it.name}
          </div>
          {res && (
            <>
              <p className="line-clamp-2 min-h-[2.5em] text-[13px] leading-snug text-slate-200">{res.description}</p>
              <Bar label="涉黄" value={res.porn.score} color={res.porn.score >= 0.5 ? "bg-red-400" : "bg-emerald-400"} />
              <Bar label="涉政" value={res.political.score} color={res.political.score >= 0.5 ? "bg-red-400" : "bg-emerald-400"} />
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
  id: number;
  filename: string;
  verdict: Verdict;
  description: string;
  engine: string;
  thumb: string;
  createdAt: string;
}

export default function ModerationApp() {
  const [items, setItems] = useState<ScanItem[]>([]);
  const [filter, setFilter] = useState<Filter>("all");
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
  const [history, setHistory] = useState<{ total: number; rows: HistoryRow[] } | null>(null);
  const [historyErr, setHistoryErr] = useState("");
  const [detailId, setDetailId] = useState<number | null>(null);
  const [clock, setClock] = useState(0);

  const itemsRef = useRef<ScanItem[]>([]);
  const queueRef = useRef<ScanItem[]>([]);
  const headRef = useRef(0);
  const runningRef = useRef(0);
  const nextId = useRef(0);
  const saveBuf = useRef<Record<string, unknown>[]>([]);
  const batchStart = useRef(0);
  const batchEnd = useRef(0);
  const bumpTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const sentinel = useRef<HTMLDivElement | null>(null);

  const cfg = useRef({ api: "", mode: "full" as "full" | "fast", online: false, gate: false, paused: false, conc: 4 });

  const bump = useCallback(() => {
    if (bumpTimer.current) return;
    bumpTimer.current = setTimeout(() => {
      bumpTimer.current = null;
      setItems(itemsRef.current.slice());
    }, 120);
  }, []);

  /* ---------- 初始化：读取本地设置 / ?api= 参数 ---------- */
  useEffect(() => {
    const q = new URLSearchParams(window.location.search).get("api");
    const saved = q ? normalizeApiUrl(q) : localStorage.getItem("mod.api") || "";
    if (q) localStorage.setItem("mod.api", saved);
    const m = localStorage.getItem("mod.mode");
    const t = setTimeout(() => {
      setApiUrl(saved);
      setApiDraft(saved);
      if (m === "fast" || m === "full") setMode(m);
    }, 0);
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
    tick();
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
        if (cfg.current.online && cfg.current.api) {
          res = await analyzeRemote(cfg.current.api, prep.blob, it.name, cfg.current.mode);
        } else {
          res = await analyzeDemo(prep.blob, prep.width, prep.height);
        }
        it.res = res;
        it.status = "done";
        it.elapsedMs = Math.round(performance.now() - t0);
        saveBuf.current.push({
          filename: it.name,
          verdict: res.verdict,
          pornScore: res.porn.score,
          pornLevel: res.porn.level,
          politicalScore: res.political.score,
          politicalLevel: res.political.level,
          description: res.description,
          reasons: res.reasons,
          ocrText: res.ocr_text ?? "",
          engine: res.engine,
          elapsedMs: it.elapsedMs,
          thumb: it.thumb,
        });
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
      processItem(it).finally(() => {
        runningRef.current--;
        batchEnd.current = performance.now();
        bump();
        pump();
      });
    }
  }, [bump, processItem]);

  useEffect(() => {
    const online = conn === "online";
    cfg.current.online = online;
    cfg.current.gate = forceDemo || !apiUrl || online;
    cfg.current.paused = paused;
    cfg.current.mode = mode;
    cfg.current.conc = online ? Math.min(12, Math.max(3, (health?.replicas ?? 1) * 3)) : 4;
    const t = setTimeout(pump, 0);
    return () => clearTimeout(t);
  }, [conn, forceDemo, apiUrl, paused, mode, health?.replicas, pump]);

  /* ---------- 添加文件 ---------- */
  const addFiles = useCallback(
    (files: File[]) => {
      const imgs = files.filter(isImage);
      if (!imgs.length) return;
      if (runningRef.current === 0 && headRef.current >= queueRef.current.length) {
        batchStart.current = performance.now();
        batchEnd.current = 0;
      }
      for (const f of imgs) {
        const it: ScanItem = { id: nextId.current++, file: f, name: f.webkitRelativePath || f.name, status: "queued", rev: 0 };
        itemsRef.current.push(it);
        queueRef.current.push(it);
      }
      setItems(itemsRef.current.slice());
      pump();
    },
    [pump],
  );

  useEffect(() => {
    let depth = 0;
    const onEnter = (e: DragEvent) => {
      if (!e.dataTransfer?.types.includes("Files")) return;
      depth++;
      setDragging(true);
    };
    const onLeave = () => {
      depth = Math.max(0, depth - 1);
      if (!depth) setDragging(false);
    };
    const onOver = (e: DragEvent) => e.preventDefault();
    const onDrop = async (e: DragEvent) => {
      e.preventDefault();
      depth = 0;
      setDragging(false);
      if (e.dataTransfer) addFiles(await filesFromDrop(e.dataTransfer));
    };
    const onPaste = (e: ClipboardEvent) => {
      const fs = Array.from(e.clipboardData?.files ?? []);
      if (fs.length) addFiles(fs);
    };
    window.addEventListener("dragenter", onEnter);
    window.addEventListener("dragleave", onLeave);
    window.addEventListener("dragover", onOver);
    window.addEventListener("drop", onDrop);
    window.addEventListener("paste", onPaste);
    return () => {
      window.removeEventListener("dragenter", onEnter);
      window.removeEventListener("dragleave", onLeave);
      window.removeEventListener("dragover", onOver);
      window.removeEventListener("drop", onDrop);
      window.removeEventListener("paste", onPaste);
    };
  }, [addFiles]);

  /* ---------- 结果批量入库 ---------- */
  useEffect(() => {
    const flush = async () => {
      if (!saveBuf.current.length) return;
      const batch = saveBuf.current.splice(0, 100);
      try {
        const r = await fetch("/api/records", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ records: batch }) });
        if (!r.ok) throw new Error();
      } catch {
        /* 历史记录是附加功能：数据库不可用时静默丢弃，不影响审查 */
      }
    };
    const t = setInterval(flush, 2000);
    return () => clearInterval(t);
  }, []);

  /* 运行中每秒刷新一次用于速度/ETA */
  useEffect(() => {
    const t = setInterval(() => setClock((c) => c + 1), 1000);
    return () => clearInterval(t);
  }, []);

  /* ---------- 统计 ---------- */
  const stats = useMemo(() => {
    const s = { total: items.length, queued: 0, running: 0, pass: 0, review: 0, block: 0, error: 0, demo: 0 };
    for (const it of items) {
      if (it.status === "queued") s.queued++;
      else if (it.status === "running") s.running++;
      else if (it.status === "error") s.error++;
      else if (it.res) {
        s[it.res.verdict]++;
        if (it.res.engine === "demo") s.demo++;
      }
    }
    return s;
  }, [items]);
  const finished = stats.pass + stats.review + stats.block + stats.error;
  const active = stats.queued + stats.running > 0;
  // eslint-disable-next-line react-hooks/purity
  const now = performance.now();
  void clock;
  const elapsed = batchStart.current ? ((active || !batchEnd.current ? now : batchEnd.current) - batchStart.current) / 1000 : 0;
  const rate = elapsed > 1 && finished ? finished / elapsed : 0;
  const eta = rate && active ? Math.ceil((stats.queued + stats.running) / rate) : 0;

  const filtered = useMemo(() => (filter === "all" ? items : items.filter((it) => kindOf(it) === filter)), [items, filter]);
  const visible = filtered.slice(0, limit);

  useEffect(() => {
    const el = sentinel.current;
    if (!el) return;
    const io = new IntersectionObserver((es) => es[0].isIntersecting && setLimit((l) => l + 120), { rootMargin: "600px" });
    io.observe(el);
    return () => io.disconnect();
  }, [filtered.length, limit]);

  /* ---------- 操作 ---------- */
  const retryWhere = (pred: (it: ScanItem) => boolean) => {
    if (queueRef.current.length === headRef.current) {
      batchStart.current = performance.now();
      batchEnd.current = 0;
    }
    for (const it of itemsRef.current) {
      if (it.status !== "running" && pred(it)) {
        it.status = "queued";
        it.err = undefined;
        it.res = undefined;
        it.rev++;
        queueRef.current.push(it);
      }
    }
    setItems(itemsRef.current.slice());
    pump();
  };
  const clearAll = () => {
    itemsRef.current = [];
    queueRef.current = [];
    headRef.current = 0;
    setItems([]);
    setDetailId(null);
  };
  const exportCsv = () => {
    const rows = [["文件名", "结论", "涉黄分", "涉政分", "描述", "原因", "图中文字", "引擎"]];
    for (const it of items) {
      const r = it.res;
      rows.push([it.name, kindOf(it), r ? String(r.porn.score) : "", r ? String(r.political.score) : "", r?.description ?? it.err ?? "", r?.reasons.join("; ") ?? "", r?.ocr_text ?? "", r?.engine ?? ""]);
    }
    download("审查结果.csv", "\ufeff" + rows.map((r) => r.map((c) => `"${String(c).replace(/"/g, '""')}"`).join(",")).join("\n"), "text/csv");
  };
  const exportJson = () =>
    download("审查结果.json", JSON.stringify(items.filter((i) => i.res).map((i) => ({ filename: i.name, ...i.res })), null, 2), "application/json");

  const saveSettings = () => {
    const u = normalizeApiUrl(apiDraft);
    setApiUrl(u);
    setApiDraft(u);
    setForceDemo(false);
    localStorage.setItem("mod.api", u);
    localStorage.setItem("mod.mode", mode);
  };

  const openHistory = async () => {
    setHistoryOpen(true);
    setHistory(null);
    setHistoryErr("");
    try {
      const r = await fetch("/api/records?limit=100");
      const d = (await r.json()) as { ok: boolean; total: number; records: HistoryRow[] };
      if (!d.ok) throw new Error();
      setHistory({ total: d.total, rows: d.records });
    } catch {
      setHistoryErr("数据库暂不可用，历史记录功能不可用（不影响审查）。");
    }
  };

  const detail = detailId == null ? null : (items.find((i) => i.id === detailId) ?? null);

  const connPill = {
    none: { t: "演示模式 · 未连接 GPU", c: "bg-slate-700 text-slate-200" },
    checking: { t: "连接中…", c: "bg-amber-600/30 text-amber-200" },
    loading: { t: "GPU 模型加载中…", c: "bg-amber-600/30 text-amber-200" },
    online: { t: `GPU 已连接 · ${health?.gpus.map((g) => g.name.replace("Tesla ", "")).join(" + ") || "mock"}${health?.vlm_enabled ? " · 大模型" : ""}`, c: "bg-emerald-600/30 text-emerald-200" },
    offline: { t: "Kaggle 无法连接", c: "bg-red-600/30 text-red-200" },
  }[conn];

  return (
    <div className="min-h-screen bg-[#0a0f1f] text-slate-100">
      {dragging && (
        <div className="pointer-events-none fixed inset-0 z-50 grid place-items-center bg-sky-950/80 backdrop-blur-sm">
          <div className="rounded-3xl border-2 border-dashed border-sky-400 px-16 py-12 text-center text-2xl font-semibold text-sky-200">
            📥 松开鼠标，添加图片 / 文件夹
          </div>
        </div>
      )}

      <header className="sticky top-0 z-30 flex flex-wrap items-center gap-3 border-b border-slate-800 bg-[#070b17]/95 px-4 py-3 backdrop-blur">
        <h1 className="text-base font-bold tracking-wide">🛡️ 图片审查系统</h1>
        <button onClick={() => setSettingsOpen(true)} className={`rounded-full px-3 py-1 text-xs font-medium ${connPill.c}`}>
          {connPill.t}
        </button>
        <div className="flex-1" />
        <select
          value={mode}
          onChange={(e) => {
            const m = e.target.value as "full" | "fast";
            setMode(m);
            localStorage.setItem("mod.mode", m);
          }}
          className="rounded-lg border border-slate-700 bg-slate-800 px-2 py-1.5 text-xs"
          title="完整模式使用大模型生成描述并复核；快速模式只用分类器+CLIP，速度更快"
        >
          <option value="full">完整模式（大模型）</option>
          <option value="fast">快速模式</option>
        </select>
        <button onClick={openHistory} className="rounded-lg bg-slate-800 px-3 py-1.5 text-xs hover:bg-slate-700">
          🗂 历史
        </button>
        <button onClick={() => setSettingsOpen(true)} className="rounded-lg bg-slate-800 px-3 py-1.5 text-xs hover:bg-slate-700">
          ⚙ 设置
        </button>
      </header>

      <main className="mx-auto max-w-[1500px] px-4 py-5">
        {conn === "none" && !forceDemo && (
          <div className="mb-4 rounded-xl border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-sm text-amber-100">
            当前为<b>演示模式</b>（浏览器本地粗略估计，<b>不能判断涉政</b>）。要启用 AI 精准审查：在 Kaggle 运行服务后，点右上角「设置」填入 Kaggle 公网网址。
          </div>
        )}
        {apiUrl && conn !== "online" && !forceDemo && (
          <div className="mb-4 flex flex-wrap items-center gap-3 rounded-xl border border-sky-500/30 bg-sky-500/10 px-4 py-3 text-sm text-sky-100">
            <span>
              {conn === "loading"
                ? "Kaggle 服务已连接，模型正在加载（首次约 3~8 分钟），加载完成后自动开始审查。"
                : conn === "checking"
                  ? "正在连接 Kaggle…"
                  : "无法连接 Kaggle：请确认 Notebook 仍在运行、网址正确。图片已排队等待，连接后自动开始。"}
            </span>
            <button onClick={() => setForceDemo(true)} className="rounded-lg bg-slate-800 px-3 py-1 text-xs hover:bg-slate-700">
              改用演示模式处理
            </button>
          </div>
        )}

        <label
          className={`flex cursor-pointer flex-col items-center justify-center rounded-2xl border-2 border-dashed border-slate-700 bg-slate-900/50 text-center transition hover:border-sky-500/70 hover:bg-slate-900 ${
            items.length ? "gap-1 px-4 py-5" : "gap-3 px-6 py-16"
          }`}
        >
          <input type="file" accept="image/*" multiple hidden onChange={(e) => (addFiles(Array.from(e.target.files ?? [])), (e.target.value = ""))} />
          <div className={items.length ? "text-2xl" : "text-5xl"}>📥</div>
          <div className="text-sm font-medium">拖拽图片或整个文件夹到这里，或点击选择（数量不限，也可 Ctrl+V 粘贴）</div>
          {!items.length && <div className="text-xs text-slate-400">自动压缩后并行上传 · 判定 涉黄 / 涉政 · 生成中文描述 · 支持 JPG PNG WebP GIF BMP</div>}
        </label>
        <div className="mt-2 flex gap-2 text-xs">
          <label className="cursor-pointer rounded-lg bg-slate-800 px-3 py-1.5 hover:bg-slate-700">
            📁 选择文件夹
            <input
              type="file"
              hidden
              multiple
              // @ts-expect-error webkitdirectory 是非标准属性
              webkitdirectory=""
              onChange={(e) => (addFiles(Array.from(e.target.files ?? [])), (e.target.value = ""))}
            />
          </label>
        </div>

        {items.length > 0 && (
          <>
            <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
              {[
                ["总数", stats.total, "text-slate-100"],
                ["通过", stats.pass, "text-emerald-300"],
                ["待复核", stats.review, "text-amber-300"],
                ["拦截", stats.block, "text-red-300"],
                ["失败", stats.error, "text-rose-300"],
                ["排队/处理中", stats.queued + stats.running, "text-sky-300"],
              ].map(([l, v, c]) => (
                <div key={String(l)} className="rounded-xl border border-slate-800 bg-slate-900/60 px-4 py-3">
                  <div className="text-xs text-slate-400">{l}</div>
                  <div className={`text-2xl font-bold tabular-nums ${c}`}>{v}</div>
                </div>
              ))}
            </div>

            <div className="mt-3 flex flex-wrap items-center gap-3">
              <div className="h-2 min-w-[180px] flex-1 overflow-hidden rounded-full bg-slate-800">
                <div className="h-full rounded-full bg-gradient-to-r from-sky-500 to-emerald-400 transition-all" style={{ width: `${stats.total ? (finished / stats.total) * 100 : 0}%` }} />
              </div>
              <span className="text-xs tabular-nums text-slate-300">
                {finished}/{stats.total}
                {rate > 0 && ` · ${rate.toFixed(1)} 张/秒`}
                {eta > 0 && ` · 约剩 ${eta >= 60 ? Math.ceil(eta / 60) + " 分钟" : eta + " 秒"}`}
              </span>
              <button onClick={() => setPaused((p) => !p)} className="rounded-lg bg-slate-800 px-3 py-1.5 text-xs hover:bg-slate-700">
                {paused ? "▶ 继续" : "⏸ 暂停"}
              </button>
              {stats.error > 0 && (
                <button onClick={() => retryWhere((i) => i.status === "error")} className="rounded-lg bg-rose-700/70 px-3 py-1.5 text-xs hover:bg-rose-600">
                  🔁 重试失败 ({stats.error})
                </button>
              )}
              {conn === "online" && stats.demo > 0 && (
                <button onClick={() => retryWhere((i) => i.res?.engine === "demo")} className="rounded-lg bg-sky-700/70 px-3 py-1.5 text-xs hover:bg-sky-600">
                  🤖 用 AI 重新审查演示结果 ({stats.demo})
                </button>
              )}
              <button onClick={exportCsv} className="rounded-lg bg-slate-800 px-3 py-1.5 text-xs hover:bg-slate-700">
                导出 CSV
              </button>
              <button onClick={exportJson} className="rounded-lg bg-slate-800 px-3 py-1.5 text-xs hover:bg-slate-700">
                导出 JSON
              </button>
              <button onClick={clearAll} className="rounded-lg bg-slate-800 px-3 py-1.5 text-xs text-red-300 hover:bg-slate-700">
                清空
              </button>
            </div>

            <div className="mt-4 flex flex-wrap gap-2">
              {(
                [
                  ["all", "全部"],
                  ["block", "拦截"],
                  ["review", "待复核"],
                  ["pass", "通过"],
                  ["error", "失败"],
                  ["queued", "进行中"],
                ] as [Filter, string][]
              ).map(([k, t]) => (
                <button
                  key={k}
                  onClick={() => (setFilter(k), setLimit(120))}
                  className={`rounded-full px-4 py-1.5 text-xs font-medium ${filter === k ? "bg-sky-500 text-white" : "bg-slate-800 text-slate-300 hover:bg-slate-700"}`}
                >
                  {t}
                </button>
              ))}
            </div>

            <div className="mt-4 grid gap-3" style={{ gridTemplateColumns: "repeat(auto-fill,minmax(210px,1fr))" }}>
              {visible.map((it) => (
                <Card key={it.id} it={it} rev={it.rev} onOpen={setDetailId} />
              ))}
            </div>
            <div ref={sentinel} className="h-10" />
            {filtered.length === 0 && <p className="py-10 text-center text-sm text-slate-500">该分类下暂无图片</p>}
          </>
        )}
      </main>

      {/* ---------- 详情 ---------- */}
      {detail && <Detail key={detail.id} it={detail} api={conn === "online" ? apiUrl : ""} vlm={!!health?.vlm_enabled} onClose={() => setDetailId(null)} />}

      {/* ---------- 设置 ---------- */}
      {settingsOpen && (
        <Modal onClose={() => setSettingsOpen(false)} title="⚙ 连接 Kaggle GPU">
          <ol className="mb-4 list-decimal space-y-1 pl-5 text-xs text-slate-300">
            <li>Kaggle Notebook：Accelerator 选 GPU T4 x2，Internet 打开。</li>
            <li>到「复制代码界面」的 Kaggle 标签，复制「★ 一键运行 Cell」，粘贴到 Notebook 运行。</li>
            <li>日志里出现 <code className="text-sky-300">https://xxxx.trycloudflare.com</code>，把它粘贴到下面。</li>
          </ol>
          <label className="text-xs text-slate-400">Kaggle 公网网址</label>
          <div className="mt-1 flex gap-2">
            <input
              value={apiDraft}
              onChange={(e) => setApiDraft(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && saveSettings()}
              placeholder="https://xxxx.trycloudflare.com"
              className="flex-1 rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm outline-none focus:border-sky-500"
            />
            <button onClick={saveSettings} className="rounded-lg bg-sky-600 px-4 text-sm font-medium hover:bg-sky-500">
              保存并连接
            </button>
          </div>
          <div className="mt-4 rounded-lg bg-slate-950 p-3 text-xs text-slate-300">
            <div>
              状态：<b>{connPill.t}</b>
            </div>
            {health && (
              <div className="mt-1 space-y-0.5 text-slate-400">
                <div>副本：{health.replicas_ready}/{health.replicas} · 已处理 {health.processed} 张 · 平均 {health.avg_ms}ms/张</div>
                <div>大模型：{health.models.vlm ?? "未加载（仅分类器+CLIP）"}</div>
                <div>GPU：{health.gpus.map((g) => `${g.name} ${g.memory_gb}GB`).join(" / ") || "—"}</div>
              </div>
            )}
          </div>
          {apiUrl && (
            <div className="mt-3 flex gap-2 text-xs">
              <a href={apiUrl} target="_blank" rel="noreferrer" className="rounded-lg bg-slate-800 px-3 py-1.5 hover:bg-slate-700">
                ↗ 打开 Kaggle 自带网页版
              </a>
              <button
                onClick={() => {
                  setApiUrl("");
                  setApiDraft("");
                  localStorage.removeItem("mod.api");
                }}
                className="rounded-lg bg-slate-800 px-3 py-1.5 text-red-300 hover:bg-slate-700"
              >
                断开
              </button>
            </div>
          )}
        </Modal>
      )}

      {/* ---------- 历史 ---------- */}
      {historyOpen && (
        <Modal onClose={() => setHistoryOpen(false)} title={`🗂 审查历史${history ? `（共 ${history.total} 条，显示最近 100）` : ""}`} wide>
          {historyErr && <p className="text-sm text-amber-300">{historyErr}</p>}
          {!history && !historyErr && <p className="text-sm text-slate-400">加载中…</p>}
          {history && history.rows.length === 0 && <p className="text-sm text-slate-400">暂无记录</p>}
          {history && history.rows.length > 0 && (
            <>
              <div className="mb-3 text-right">
                <button
                  onClick={async () => {
                    if (!confirm("确定清空全部历史记录？")) return;
                    await fetch("/api/records", { method: "DELETE" });
                    setHistory({ total: 0, rows: [] });
                  }}
                  className="rounded-lg bg-slate-800 px-3 py-1.5 text-xs text-red-300 hover:bg-slate-700"
                >
                  清空历史
                </button>
              </div>
              <ul className="space-y-2">
                {history.rows.map((r) => (
                  <li key={r.id} className="flex items-center gap-3 rounded-lg bg-slate-950 p-2">
                    {/* eslint-disable-next-line @next/next/no-img-element */}
                    {r.thumb ? <img src={r.thumb} alt="" className="h-14 w-14 rounded object-cover" /> : <div className="h-14 w-14 rounded bg-slate-800" />}
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2 text-xs">
                        <span className={`rounded px-1.5 py-0.5 ring-1 ${VERDICT_UI[r.verdict]?.cls}`}>{VERDICT_UI[r.verdict]?.text}</span>
                        <span className="truncate text-slate-400">{r.filename}</span>
                      </div>
                      <div className="truncate text-sm text-slate-200">{r.description}</div>
                    </div>
                    <div className="shrink-0 text-right text-[11px] text-slate-500">
                      <div>{r.engine}</div>
                      <div>{new Date(r.createdAt).toLocaleString()}</div>
                    </div>
                  </li>
                ))}
              </ul>
            </>
          )}
        </Modal>
      )}
    </div>
  );
}

function Modal({ title, onClose, children, wide }: { title: string; onClose: () => void; children: React.ReactNode; wide?: boolean }) {
  useEffect(() => {
    const h = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [onClose]);
  return (
    <div className="fixed inset-0 z-40 grid place-items-center bg-black/70 p-4" onClick={onClose}>
      <div onClick={(e) => e.stopPropagation()} className={`max-h-[90vh] w-full overflow-auto rounded-2xl border border-slate-700 bg-slate-900 p-5 shadow-2xl ${wide ? "max-w-3xl" : "max-w-lg"}`}>
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-base font-semibold">{title}</h2>
          <button onClick={onClose} className="rounded-lg px-2 py-1 text-slate-400 hover:bg-slate-800">
            ✕
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}

function Detail({ it, api, vlm, onClose }: { it: ScanItem; api: string; vlm: boolean; onClose: () => void }) {
  const [url, setUrl] = useState("");
  const [q, setQ] = useState("");
  const [answer, setAnswer] = useState("");
  const [asking, setAsking] = useState(false);
  useEffect(() => {
    const u = URL.createObjectURL(it.file);
    const t = setTimeout(() => setUrl(u), 0);
    return () => {
      clearTimeout(t);
      URL.revokeObjectURL(u);
    };
  }, [it.file]);
  const r = it.res;
  const ask = async () => {
    if (!q.trim()) return;
    setAsking(true);
    setAnswer("");
    try {
      const p = await prepareImage(it.file, 1024);
      setAnswer(await askRemote(api, p.blob, q));
    } catch (e) {
      setAnswer("❌ " + (e instanceof Error ? e.message : "追问失败"));
    } finally {
      setAsking(false);
    }
  };
  return (
    <Modal title={it.name} onClose={onClose} wide>
      <div className="grid gap-4 md:grid-cols-2">
        <div className="overflow-hidden rounded-xl bg-black">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          {url && <img src={url} alt={it.name} className="max-h-[60vh] w-full object-contain" />}
        </div>
        <div className="space-y-3 text-sm">
          {r ? (
            <>
              <span className={`inline-block rounded-md px-2.5 py-1 text-sm font-semibold ring-1 ${VERDICT_UI[r.verdict].cls}`}>{VERDICT_UI[r.verdict].text}</span>
              <p className="text-slate-100">{r.description}</p>
              <Bar label="涉黄" value={r.porn.score} color={r.porn.score >= 0.5 ? "bg-red-400" : "bg-emerald-400"} />
              <Bar label="涉政" value={r.political.score} color={r.political.score >= 0.5 ? "bg-red-400" : "bg-emerald-400"} />
              {r.reasons.length > 0 && (
                <ul className="list-disc space-y-1 pl-5 text-amber-200">
                  {r.reasons.map((x, i) => (
                    <li key={i}>{x}</li>
                  ))}
                </ul>
              )}
              {r.political.labels.length > 0 && <div className="text-xs text-slate-400">涉政线索：{r.political.labels.join("、")}</div>}
              {r.ocr_text && <div className="rounded bg-slate-950 p-2 text-xs text-slate-300">图中文字：{r.ocr_text}</div>}
              <div className="rounded-lg bg-slate-950 p-2 text-[11px] leading-relaxed text-slate-400">
                <div>
                  引擎：{r.engine}
                  {r.gpu ? ` · ${r.gpu}` : ""}
                  {r.cached ? " · 缓存命中" : ""}
                </div>
                {r.signals?.nsfw_classifier != null && (
                  <div>
                    分类器 {(r.signals.nsfw_classifier * 100).toFixed(0)}% · CLIP 涉黄 {((r.signals.clip_porn ?? 0) * 100).toFixed(0)}% / 涉政 {((r.signals.clip_political ?? 0) * 100).toFixed(0)}% · 大模型 {r.signals.vlm_porn ?? "—"}/{r.signals.vlm_political ?? "—"}
                  </div>
                )}
                {r.timings?.total_ms != null && <div>耗时：服务端 {r.timings.total_ms}ms{it.elapsedMs ? ` · 端到端 ${it.elapsedMs}ms` : ""}</div>}
                {r.warnings?.map((w, i) => (
                  <div key={i} className="text-amber-300">
                    ⚠ {w}
                  </div>
                ))}
              </div>
            </>
          ) : (
            <p className="text-slate-400">{it.err ?? "尚未完成"}</p>
          )}
          {api && vlm && (
            <div className="border-t border-slate-800 pt-3">
              <div className="mb-1 text-xs text-slate-400">🤖 追问大模型（例如：图中有哪些文字？这是什么场景？）</div>
              <div className="flex gap-2">
                <input value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => e.key === "Enter" && ask()} className="flex-1 rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 text-sm outline-none focus:border-sky-500" />
                <button onClick={ask} disabled={asking} className="rounded-lg bg-sky-600 px-3 text-sm hover:bg-sky-500 disabled:opacity-50">
                  {asking ? "思考中…" : "提问"}
                </button>
              </div>
              {answer && <p className="mt-2 whitespace-pre-wrap rounded-lg bg-slate-950 p-2 text-sm text-slate-200">{answer}</p>}
            </div>
          )}
        </div>
      </div>
    </Modal>
  );
}

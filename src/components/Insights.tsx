import { useState } from "react";
import type { AnalysisResult, Box, HealthInfo } from "@/lib/types";

const pct = (v: number) => `${Math.round(v * 100)}%`;
const pos = (b: Box) => ({ left: `${b[0] * 100}%`, top: `${b[1] * 100}%`, width: `${(b[2] - b[0]) * 100}%`, height: `${(b[3] - b[1]) * 100}%` });
const heat = (v: number) => (v >= 0.5 ? "rgba(239,68,68,.30)" : v >= 0.25 ? "rgba(245,158,11,.22)" : "rgba(34,197,94,.08)");

/**
 * 图片 + 区域级叠加层：
 *  - 检测框：NudeNet 身体部位 / OWLv2 目标 / 二维码（红色=风险证据）
 *  - 文字框：OCR 识别到的每一行文字（命中敏感词为红色）
 *  - 分块热力：每个分析窗口的涉黄最高分（证明"每一处"都被检查过）
 */
export function ImageOverlay({ url, name, res }: { url: string; name: string; res?: AnalysisResult }) {
  const [showObj, setShowObj] = useState(true);
  const [showText, setShowText] = useState(false);
  const [showTiles, setShowTiles] = useState(false);
  const dets = res?.detections ?? [];
  const objs = dets.filter((d) => d.kind !== "text");
  const texts = dets.filter((d) => d.kind === "text");
  const tiles = res?.regions ?? [];
  const tab = "rounded px-2 py-0.5 text-[11px] ring-1 transition";
  return (
    <div className="flex max-h-full max-w-full flex-col items-center gap-2">
      <div className="relative w-fit leading-none">
        <img src={url} alt={name} className="block max-w-full object-contain" style={{ maxHeight: "calc(100vh - 230px)" }} />
        {showTiles && tiles.map((t, i) => (
          <div key={"t" + i} className="pointer-events-none absolute border border-dashed border-white/40" style={{ ...pos(t.box), background: heat(Math.max(t.nsfw, t.clip_porn)) }}>
            <span className="absolute right-0 top-0 rounded-bl bg-black/70 px-1 text-[10px] leading-4 text-white">{pct(Math.max(t.nsfw, t.clip_porn))}</span>
          </div>
        ))}
        {showText && texts.map((d, i) => (
          <div key={"x" + i} className={`pointer-events-none absolute border ${d.flag ? "border-red-400 bg-red-500/20" : "border-lime-300/80"}`} style={pos(d.box)} />
        ))}
        {showObj && objs.map((d, i) => (
          <div key={"o" + i} className={`pointer-events-none absolute border-2 ${d.flag ? "border-red-500 bg-red-500/15" : "border-sky-400"}`} style={pos(d.box)}>
            <span className={`absolute -top-[18px] left-[-2px] whitespace-nowrap rounded px-1 text-[10px] leading-4 text-white ${d.flag ? "bg-red-600" : "bg-sky-600"}`}>
              {d.cn} {pct(d.score)}
            </span>
          </div>
        ))}
      </div>
      {res && (objs.length > 0 || texts.length > 0 || tiles.length > 0) && (
        <div className="flex flex-wrap items-center justify-center gap-1.5 text-slate-300">
          <button onClick={() => setShowObj((v) => !v)} className={`${tab} ${showObj ? "bg-sky-500/20 ring-sky-400" : "bg-slate-800 ring-slate-600"}`}>检测框 {objs.length}</button>
          <button onClick={() => setShowText((v) => !v)} className={`${tab} ${showText ? "bg-lime-500/20 ring-lime-400" : "bg-slate-800 ring-slate-600"}`}>文字框 {texts.length}</button>
          <button onClick={() => setShowTiles((v) => !v)} className={`${tab} ${showTiles ? "bg-amber-500/20 ring-amber-400" : "bg-slate-800 ring-slate-600"}`}>分块热力 {tiles.length}</button>
        </div>
      )}
    </div>
  );
}

/** 详情侧栏里的"辅助检测"面板：检测目标 / 联系方式 / 二维码 / 多模型分数 / 各环节耗时 */
export function InsightPanel({ res }: { res: AnalysisResult }) {
  const dets = (res.detections ?? []).filter((d) => d.kind !== "text");
  const sig = res.signals;
  const t = res.timings;
  const kw = [...(res.keywords?.political ?? []), ...(res.keywords?.porn ?? [])];
  const contacts = res.aux?.contacts ?? [];
  const qr = res.aux?.qr ?? [];
  const chip = (txt: string, hot: boolean, k: string) => (
    <span key={k} className={`rounded-full px-2 py-0.5 text-[11px] ring-1 ${hot ? "bg-red-500/15 text-red-200 ring-red-500/40" : "bg-slate-700/50 text-slate-300 ring-slate-600"}`}>{txt}</span>
  );
  const hasAny = dets.length || kw.length || contacts.length || qr.length || sig?.nsfw_models || res.minors_suspected;
  if (!hasAny) return null;
  return (
    <div className="space-y-2 rounded-lg bg-slate-800/50 p-3 text-xs">
      <div className="font-semibold text-slate-300">🔬 辅助检测</div>
      {res.minors_suspected && <div className="text-red-300">⚠ 画面疑似涉及未成年人</div>}
      {dets.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {dets.map((d, i) => chip(`${d.cn} ${pct(d.score)}`, !!d.flag, "d" + i))}
        </div>
      )}
      {kw.length > 0 && <div className="flex flex-wrap items-center gap-1"><span className="text-slate-500">敏感词</span>{kw.map((k) => chip(k, true, "k" + k))}</div>}
      {contacts.length > 0 && <div className="flex flex-wrap items-center gap-1"><span className="text-slate-500">联系方式</span>{contacts.map((c) => chip(c, true, "c" + c))}</div>}
      {qr.length > 0 && <div className="break-all text-slate-300"><span className="text-slate-500">二维码内容 </span>{qr.join(" ; ")}</div>}
      {sig?.nsfw_models && (
        <div className="text-slate-400">
          分类器集成：{Object.entries(sig.nsfw_models).map(([k, v]) => `${k.split("/").pop()} ${pct(v)}`).join(" · ")}
          {sig.nsfw_ensemble != null && <> → 综合(含分块) {pct(sig.nsfw_ensemble)}</>}
        </div>
      )}
      {t && (
        <div className="text-[11px] text-slate-500">
          耗时 分类 {t.classifier_ms ?? 0} · CLIP {t.clip_ms ?? 0} · 检测 {t.detector_ms ?? 0} · NudeNet {t.nudenet_ms ?? 0} · OCR {t.ocr_ms ?? 0} · 大模型 {t.vlm_ms ?? 0} ms
          {res.aux?.tiles != null && ` · 分块 ${res.aux.tiles}`}
        </div>
      )}
    </div>
  );
}

/** 设置面板里的"双 GPU 实时状态"：证明两张 T4 同时在工作 */
export function GpuPanel({ health }: { health: HealthInfo }) {
  const gs = health.gpu_stats ?? [];
  const m = health.models;
  const tool = (ok: boolean | string | null | undefined, label: string) => (
    <span className={`rounded px-1.5 py-0.5 text-[10px] ring-1 ${ok ? "bg-emerald-500/10 text-emerald-300 ring-emerald-500/30" : "bg-slate-700/40 text-slate-500 ring-slate-600"}`}>{ok ? "✓" : "✗"} {label}</span>
  );
  return (
    <div className="space-y-2 rounded-lg bg-slate-800/60 p-2.5">
      {gs.length > 0 && (
        <div className="grid gap-1.5 sm:grid-cols-2">
          {gs.map((g) => {
            const used = g.mem_used_gb ?? 0;
            const total = g.mem_total_gb ?? 15;
            return (
              <div key={g.index} className="rounded-md bg-slate-900/70 p-2 ring-1 ring-slate-700">
                <div className="flex items-center justify-between text-[11px]">
                  <span className="font-semibold text-slate-200">GPU{g.index}</span>
                  <span className={g.busy ? "text-emerald-300" : g.ready ? "text-slate-400" : "text-amber-300"}>{g.busy ? "● 工作中" : g.ready ? "○ 空闲" : "◌ 加载中"}</span>
                </div>
                {g.mem_total_gb != null && (
                  <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-slate-700">
                    <div className="h-full bg-sky-400" style={{ width: `${Math.min(100, (used / total) * 100)}%` }} />
                  </div>
                )}
                <div className="mt-1 text-[10px] text-slate-500">
                  {g.mem_total_gb != null ? `显存 ${used}/${total}GB · ` : ""}已处理 {g.processed} · {g.avg_ms}ms/张
                </div>
              </div>
            );
          })}
        </div>
      )}
      <div className="flex flex-wrap gap-1">
        {tool(true, `分类器×${m.nsfw_all?.length ?? 1}`)}
        {tool(m.clip, "CLIP")}
        {tool(m.vlm, m.vlm ? `大模型 ${m.vlm.split("/").pop()}` : "大模型")}
        {tool(m.detector, "OWLv2 检测")}
        {tool(m.nudenet, `NudeNet ${m.nudenet ?? ""}`)}
        {tool(m.ocr, "OCR")}
        {tool(m.qr, "二维码")}
      </div>
    </div>
  );
}

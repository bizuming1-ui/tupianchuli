export type Verdict = "pass" | "review" | "block";

export interface AnalysisResult {
  verdict: Verdict;
  porn: { score: number; level: "none" | "suggestive" | "explicit" | string };
  political: { score: number; level: "none" | "low" | "high" | string; labels: string[] };
  reasons: string[];
  description: string;
  ocr_text?: string;
  /** kaggle-gpu+vlm | kaggle-gpu | demo | mock */
  engine: string;
  gpu?: string;
  scene?: string;
  cached?: boolean;
  signals?: {
    nsfw_classifier?: number;
    clip_porn?: number;
    clip_suggestive?: number;
    clip_political?: number;
    vlm_porn?: string | null;
    vlm_political?: string | null;
  };
  timings?: { total_ms?: number; classifier_ms?: number; clip_ms?: number; vlm_ms?: number };
  warnings?: string[];
}

export interface HealthInfo {
  status: "ok" | "loading" | "error";
  ready: boolean;
  version: string;
  mock: boolean;
  replicas: number;
  replicas_ready: number;
  gpus: { index: number; name: string; memory_gb: number }[];
  models: { nsfw: string; clip: string; vlm: string | null };
  vlm_enabled: boolean;
  inflight: number;
  processed: number;
  avg_ms: number;
  error: string | null;
  warnings?: string[];
  loading_done?: boolean;
}

export type ItemStatus = "queued" | "running" | "done" | "error";

export interface ScanItem {
  id: number;
  file: File;
  name: string;
  status: ItemStatus;
  thumb?: string;
  res?: AnalysisResult;
  err?: string;
  /** 每次变化 +1，用于 React.memo 精准刷新 */
  rev: number;
  saved?: boolean;
  elapsedMs?: number;
}

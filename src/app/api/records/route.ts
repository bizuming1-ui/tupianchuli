import { desc, sql } from "drizzle-orm";
import { db } from "@/db";
import { scanRecords } from "@/db/schema";

export const dynamic = "force-dynamic";

const VERDICTS = new Set(["pass", "review", "block"]);

function str(v: unknown, max: number): string {
  return typeof v === "string" ? v.slice(0, max) : "";
}
function num(v: unknown): number {
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? Math.max(0, Math.min(1, n)) : 0;
}

/** 历史记录列表 */
export async function GET(req: Request) {
  try {
    const limit = Math.min(500, Math.max(1, Number(new URL(req.url).searchParams.get("limit")) || 100));
    const rows = await db.select().from(scanRecords).orderBy(desc(scanRecords.id)).limit(limit);
    const [{ count }] = await db.select({ count: sql<number>`count(*)::int` }).from(scanRecords);
    return Response.json({ ok: true, total: count, records: rows });
  } catch {
    return Response.json({ ok: false, error: "数据库不可用" }, { status: 503 });
  }
}

/** 批量保存审查结果 */
export async function POST(req: Request) {
  try {
    const body = (await req.json()) as { records?: Record<string, unknown>[] };
    const list = Array.isArray(body.records) ? body.records.slice(0, 200) : [];
    const values = list
      .filter((r) => VERDICTS.has(String(r.verdict)))
      .map((r) => ({
        filename: str(r.filename, 300) || "unknown",
        verdict: String(r.verdict),
        pornScore: num(r.pornScore),
        pornLevel: str(r.pornLevel, 20) || "none",
        politicalScore: num(r.politicalScore),
        politicalLevel: str(r.politicalLevel, 20) || "none",
        description: str(r.description, 500),
        reasons: Array.isArray(r.reasons) ? r.reasons.map((x) => str(x, 300)).slice(0, 10) : [],
        ocrText: str(r.ocrText, 1000),
        engine: str(r.engine, 40) || "demo",
        elapsedMs: Math.round(Number(r.elapsedMs) || 0),
        thumb: str(r.thumb, 40000),
      }));
    if (values.length) await db.insert(scanRecords).values(values);
    return Response.json({ ok: true, saved: values.length });
  } catch {
    return Response.json({ ok: false, error: "保存失败" }, { status: 503 });
  }
}

/** 清空历史 */
export async function DELETE() {
  try {
    await db.delete(scanRecords);
    return Response.json({ ok: true });
  } catch {
    return Response.json({ ok: false }, { status: 503 });
  }
}

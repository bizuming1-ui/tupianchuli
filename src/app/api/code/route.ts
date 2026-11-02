import { listGithubFiles, listKaggleCells } from "@/lib/codeFiles";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

/** 复制代码界面的数据源：直接读取磁盘上的真实源码 */
export async function GET() {
  try {
    const github = await listGithubFiles();
    const kaggle = listKaggleCells();
    return Response.json({ ok: true, github, kaggle });
  } catch (e) {
    return Response.json({ ok: false, error: e instanceof Error ? e.message : "读取失败" }, { status: 500 });
  }
}

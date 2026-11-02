import JSZip from "jszip";
import { readGithubFiles } from "@/lib/codeFiles";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

/** 把所有 GitHub 文件打成 ZIP，解压后整个文件夹拖进 GitHub「Upload files」即可 */
export async function GET() {
  const zip = new JSZip();
  for (const f of readGithubFiles()) zip.file(f.path, f.content);
  const data = await zip.generateAsync({ type: "uint8array", compression: "DEFLATE" });
  return new Response(Buffer.from(data), {
    headers: {
      "Content-Type": "application/zip",
      "Content-Disposition": 'attachment; filename="tupianchuli-github-files.zip"',
      "Cache-Control": "no-store",
    },
  });
}

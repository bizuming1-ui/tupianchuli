/** 全站常量：改仓库地址只需要改这里 */
export const GH_OWNER = "bizuming1-ui";
export const GH_REPO = "tupianchuli";
export const GH_BRANCH = "main";
export const GH_REPO_URL = `https://github.com/${GH_OWNER}/${GH_REPO}`;
export const GH_PAGES_URL = `https://${GH_OWNER}.github.io/${GH_REPO}/`;

/** 当前页面的真实网址（不含 hash），可直接复制到浏览器打开 */
export function currentSiteUrl(): string {
  try {
    const u = new URL(window.location.href);
    u.hash = "";
    u.search = "";
    return u.toString();
  } catch {
    return GH_PAGES_URL;
  }
}

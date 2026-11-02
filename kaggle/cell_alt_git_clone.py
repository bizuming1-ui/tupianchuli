# ===== 方案 B（可选）：已把代码推送到 GitHub 后，用这一个 Cell 代替 Cell 2 + Cell 3 =====
# 作用：从 GitHub 仓库拉取 kaggle/server.py 与 kaggle/index.html 到 /kaggle/working/app
import os, shutil, subprocess

REPO = "https://github.com/bizuming1-ui/tupianchuli"
APP_DIR = "/kaggle/working/app"
os.makedirs(APP_DIR, exist_ok=True)
subprocess.run(f"rm -rf /kaggle/working/_repo && git clone --depth 1 {REPO} /kaggle/working/_repo", shell=True, check=True)
for f in ("server.py", "index.html"):
    shutil.copy(f"/kaggle/working/_repo/kaggle/{f}", f"{APP_DIR}/{f}")
print("✅ 已从 GitHub 同步:", os.listdir(APP_DIR))

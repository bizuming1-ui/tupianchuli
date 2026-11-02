# ===== 方案 B：GitHub 同步版（先把最新代码更新到 GitHub，之后只需要这一个 Cell）=====
# 前置：Settings → Accelerator 选 GPU T4 x2；Internet 打开 On
# 作用：从 GitHub 拉最新代码 → 安装依赖 → 启动 GPU 服务 → 生成公网网址
import os, shutil, subprocess

REPO = "https://github.com/bizuming1-ui/tupianchuli"
APP_DIR = "/kaggle/working/app"
os.makedirs(APP_DIR, exist_ok=True)
subprocess.run(f"rm -rf /kaggle/working/_repo && git clone --depth 1 {REPO} /kaggle/working/_repo", shell=True, check=True)
for f in ("server.py", "index.html"):
    shutil.copy(f"/kaggle/working/_repo/kaggle/{f}", f"{APP_DIR}/{f}")
print("✅ 已从 GitHub 同步:", sorted(os.listdir(APP_DIR)))
for step in ("cell1_install.py", "cell4_launch.py"):  # 依次执行：安装依赖 → 启动服务
    path = f"/kaggle/working/_repo/kaggle/{step}"
    exec(compile(open(path, encoding="utf-8").read(), path, "exec"), globals())

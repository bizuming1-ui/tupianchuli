# ===== Kaggle Cell 1：安装依赖 + 创建目录 + 下载 cloudflared（只需运行一次）=====
# 前置条件（右侧 Settings 面板）：Accelerator = GPU T4 x2，Internet = On
import os, subprocess, sys, urllib.request

APP_DIR = "/kaggle/working/app"
os.makedirs(APP_DIR, exist_ok=True)

def sh(cmd):
    print(">>", cmd)
    p = subprocess.run(cmd, shell=True, text=True, capture_output=True)
    print((p.stdout or "")[-1500:], (p.stderr or "")[-800:])
    return p.returncode

# 不重装 torch（Kaggle 自带与 CUDA 匹配的版本），只升级需要的库。Qwen2-VL 需要 transformers>=4.45
sh(f"{sys.executable} -m pip install -q -U 'transformers>=4.49,<5' accelerate fastapi 'uvicorn[standard]' python-multipart pillow pyngrok")

# 公网隧道：cloudflared（免账号）
CF = "/kaggle/working/cloudflared"
if not os.path.exists(CF):
    urllib.request.urlretrieve(
        "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64", CF)
    os.chmod(CF, 0o755)

import torch
print("torch", torch.__version__, "| CUDA 可用:", torch.cuda.is_available(), "| GPU 数量:", torch.cuda.device_count())
for i in range(torch.cuda.device_count()):
    print("  GPU", i, torch.cuda.get_device_name(i))
if torch.cuda.device_count() < 2:
    print("⚠️ 未检测到 2 张 GPU：请在 Settings -> Accelerator 选择 GPU T4 x2（单卡也能运行，只是速度减半）")
print("✅ Cell 1 完成：下一步运行 Cell 2 / Cell 3 写入文件，然后运行 Cell 4 启动")

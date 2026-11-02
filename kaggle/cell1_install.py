# ===== Kaggle Cell 1：安装依赖 + 创建目录 + 下载 cloudflared（只需运行一次）=====
# 前置条件（右侧 Settings 面板）：Accelerator = GPU T4 x2，Internet = On
import importlib, os, subprocess, sys, urllib.request

APP_DIR = "/kaggle/working/app"
os.makedirs(APP_DIR, exist_ok=True)

def sh(cmd, tail=1200):
    print(">>", cmd)
    p = subprocess.run(cmd, shell=True, text=True, capture_output=True)
    out = ((p.stdout or "") + (p.stderr or "")).strip()
    if out:
        print(out[-tail:])
    return p.returncode

def have(mod):
    try:
        importlib.invalidate_caches()
        importlib.import_module(mod)
        return True
    except Exception:
        return False

PY = sys.executable
# 1) 核心依赖：不重装 torch（Kaggle 自带与 CUDA 匹配的版本）。Qwen2.5-VL 需要 transformers>=4.49，这里取 >=4.51
sh(f"{PY} -m pip install -q -U 'transformers>=4.51,<5' accelerate fastapi 'uvicorn[standard]' python-multipart pyngrok")

# 2) v2.0 辅助检测工具（全部是"可选增强"，装不上也不影响主流程，只会自动降级）
#    注意：nudenet / rapidocr 会强依赖 opencv-python-headless，可能连带升级 numpy 而破坏 Kaggle 环境，
#    所以用 --no-deps 精确安装，复用 Kaggle 自带的 opencv-python(cv2) 和 numpy。
if not have("onnxruntime"):
    sh(f"{PY} -m pip install -q onnxruntime")
sh(f"{PY} -m pip install -q pyclipper Shapely PyYAML six tqdm")
if not have("nudenet"):
    sh(f"{PY} -m pip install -q --no-deps nudenet")
if not have("rapidocr_onnxruntime"):
    sh(f"{PY} -m pip install -q --no-deps rapidocr_onnxruntime")

# 2.5) v2.1 网络照片爬取：Playwright + Chromium（同样是可选增强：装不上时爬取自动降级为 HTTP 模式，审核功能不受影响）
#      Kaggle 以 root 运行，install-deps 会用 apt 装好 Chromium 需要的系统库（约 1~2 分钟，只需一次）
if not have("playwright"):
    sh(f"{PY} -m pip install -q playwright")
sh(f"{PY} -m playwright install chromium")
sh(f"{PY} -m playwright install-deps chromium", tail=600)

# 3) 公网隧道：cloudflared（免账号）
CF = "/kaggle/working/cloudflared"
if not os.path.exists(CF):
    urllib.request.urlretrieve(
        "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64", CF)
    os.chmod(CF, 0o755)

# 4) 自检报告
import torch
print("-" * 60)
print("torch", torch.__version__, "| CUDA 可用:", torch.cuda.is_available(), "| GPU 数量:", torch.cuda.device_count())
for i in range(torch.cuda.device_count()):
    print("  GPU", i, torch.cuda.get_device_name(i))
if torch.cuda.device_count() < 2:
    print("⚠️ 未检测到 2 张 GPU：请在 Settings -> Accelerator 选择 GPU T4 x2（单卡也能运行，只是速度减半）")
chk = subprocess.run([PY, "-c", "import transformers,cv2,numpy;print(transformers.__version__, cv2.__version__, numpy.__version__)"],
                     capture_output=True, text=True)
print("transformers / cv2 / numpy:", (chk.stdout or chk.stderr).strip()[-200:])
for mod, label in (("nudenet", "NudeNet 身体部位检测"), ("rapidocr_onnxruntime", "RapidOCR 文字识别"), ("cv2", "OpenCV 二维码")):
    print(("✅" if have(mod) else "⚠️ 缺失(将自动降级)"), label)
# 浏览器自检：真的启动一次 Chromium 并打开 about:blank，确认「自动爬取」可用
_t = subprocess.run([PY, "-c", "from playwright.sync_api import sync_playwright as s\n"
                     "with s() as p:\n b=p.chromium.launch(args=['--no-sandbox','--disable-dev-shm-usage']);pg=b.new_page();pg.goto('about:blank');b.close()\nprint('ok')"],
                    capture_output=True, text=True)
print("✅ Chromium 浏览器（网络照片自动爬取）" if "ok" in (_t.stdout or "") else "⚠️ Chromium 不可用（爬取将降级为 HTTP 模式）: " + ((_t.stderr or "")[-300:]))
print("✅ Cell 1 完成：下一步运行启动 Cell")

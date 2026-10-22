# -*- coding: utf-8 -*-
"""DirectML 可用性诊断：把 onnxruntime C++ 层的**原始字节**报错捞出来解码，
并逐项检查 D3D12 设备能否创建。不做任何修改，只读。

用法： python tools/dml_diag.py
"""
import os
import sys
import ctypes as C
import subprocess

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DML_ENV = os.path.join(BASE, "_dml_env")
out = []


def w(s=""):
    print(s)
    out.append(s)


# ---------------- ① D3D12 设备（正确的 4 参数签名） ----------------
def d3d12_check():
    w("=" * 74)
    w("① D3D12 设备创建")
    w("=" * 74)
    try:
        d3d = C.WinDLL("d3d12.dll")
    except OSError as e:
        w("  d3d12.dll 加载失败: %s" % e)
        return
    # HRESULT D3D12CreateDevice(IUnknown*, D3D_FEATURE_LEVEL, REFIID, void**)
    d3d.D3D12CreateDevice.restype = C.c_long
    d3d.D3D12CreateDevice.argtypes = [C.c_void_p, C.c_int, C.c_void_p, C.c_void_p]

    class GUID(C.Structure):
        _fields_ = [("d1", C.c_uint32), ("d2", C.c_uint16), ("d3", C.c_uint16),
                    ("d4", C.c_ubyte * 8)]

    # IID_ID3D12Device = 189819f1-1db6-4b57-be54-1821339b85f7
    iid = GUID(0x189819F1, 0x1DB6, 0x4B57,
               (C.c_ubyte * 8)(0xBE, 0x54, 0x18, 0x21, 0x33, 0x9B, 0x85, 0xF7))
    names = {0xb000: "FL11_0", 0xb100: "FL11_1", 0xc000: "FL12_0", 0xc100: "FL12_1"}
    for lvl in (0xb000, 0xc000):
        dev = C.c_void_p()
        hr = d3d.D3D12CreateDevice(None, lvl, C.byref(iid), C.byref(dev))
        r = hr & 0xFFFFFFFF
        w("  D3D12CreateDevice(默认适配器, %-6s) hr=0x%08X  %s"
          % (names[lvl], r, "OK" if r == 0 else "FAIL"))
        if r == 0x887A0004:
            w("      → DXGI_ERROR_UNSUPPORTED：该会话/适配器不支持 D3D12")
        elif r == 0x80070005:
            w("      → E_ACCESSDENIED：没有 GPU 访问权限")
        elif r == 0x80004002:
            w("      → E_NOINTERFACE：接口不可用（常为远程/服务会话）")
    w("")


# ---------------- ② DirectML 原始报错 ----------------
def dml_raw_error():
    w("=" * 74)
    w("② DirectML provider 初始化（原始字节，按多编码解码）")
    w("=" * 74)
    code = r"""
import sys, os
sys.path.insert(0, r"%s")
import onnxruntime as ort
print("ORT", ort.__version__, "providers:", ort.get_available_providers())
m = r"%s"
if not os.path.exists(m):
    m = r"%s"
try:
    s = ort.InferenceSession(m, providers=["DmlExecutionProvider"])
    print("DML_OK providers=", s.get_providers())
except Exception as e:
    print("DML_ERR", type(e).__name__, str(e)[:300])
""" % (DML_ENV, os.path.join(BASE, "_clip_fixed.onnx"),
       os.path.join(BASE, r"assets\models\chinese_clip\cn_clip_vision.onnx"))
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8:replace"
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, env=env, timeout=180)
    for tag, buf in (("STDOUT", r.stdout), ("STDERR", r.stderr)):
        if not buf:
            continue
        # 依次尝试多种编码，找出能读到中文的那一个
        for enc in ("utf-8", "gbk", "utf-16-le"):
            try:
                txt = buf.decode(enc)
                bad = txt.count("\ufffd")
                if bad:
                    continue
                w("  --- %s (%s) ---" % (tag, enc))
                for line in txt.splitlines():
                    if line.strip():
                        w("   " + line[:200])
                break
            except (UnicodeDecodeError, LookupError):
                continue
        else:
            w("  --- %s 各编码都读不干净，原始前 200 字节 ---" % tag)
            w("   %r" % buf[:200])
    w("")


# ---------------- ③ 会话类型 ----------------
def session_check():
    w("=" * 74)
    w("③ 会话 / 显示环境")
    w("=" * 74)
    try:
        k = C.windll.kernel32
        sid = C.c_uint32()
        k.ProcessIdToSessionId(k.GetCurrentProcessId(), C.byref(sid))
        w("  当前进程 SessionId = %d （0=服务会话，无 GPU/桌面访问；≥1 才有桌面 GPU 上下文）" % sid.value)
    except Exception as e:
        w("  SessionId 查询失败: %s" % e)
    w("  注意：远程桌面(向日葵/GameViewer/ToDesk 等)会话里，D3D12 可能被虚拟显示器接管，")
    w("        直接创建 D3D12 设备常失败 —— 这与本机显卡型号无关。")
    w("")


if __name__ == "__main__":
    d3d12_check()
    dml_raw_error()
    session_check()
    open(os.path.join(BASE, "_dml_diag.txt"), "w", encoding="utf-8").write("\n".join(out))

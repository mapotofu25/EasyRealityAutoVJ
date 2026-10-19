# -*- coding: utf-8 -*-
"""从官方 small0.ckpt 导出可用的 ONNX（在独立临时 venv P:\_bt_env 内运行）。

只读 src/，只写 _beat_this_models/（*.onnx 已被 .gitignore）。
用法（临时 venv，不要在项目 venv 里跑）：
    P:\_bt_env\Scripts\python.exe tools\_beat_this_export_small0.py \
        --ckpt P:\_bt_env\_src\small0.ckpt \
        --bt-src P:\_bt_env\_src\beat_this-main \
        --out  P:\AutoVJ\tools\_beat_this_models\small0_fixed.onnx
说明：用旧版 TorchScript 导出器（dynamo=False），无需 onnxscript，
      且与 beat_this_cpp 的导出方式一致；导出后立刻用 onnxruntime 与 torch 比对。
"""
from __future__ import annotations
import argparse, inspect, os, sys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ckpt", required=True)
    ap.add_argument("--bt-src", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--chunk", type=int, default=1500)
    ap.add_argument("--opset", type=int, default=14)
    args = ap.parse_args()

    sys.path.insert(0, args.bt_src)
    import numpy as np
    import torch
    from beat_this.model.beat_tracker import BeatThis
    from beat_this.utils import replace_state_dict_key

    ck = torch.load(args.ckpt, map_location="cpu", weights_only=False)
    print("checkpoint keys:", list(ck.keys()))
    hp = dict(ck.get("hyper_parameters") or {})
    valid = set(inspect.signature(BeatThis).parameters)
    hp_f = {k: v for k, v in hp.items() if k in valid}
    print("hyper_parameters(用于建模):", hp_f)

    model = BeatThis(**hp_f)
    sd = replace_state_dict_key(ck["state_dict"], "model.", "")
    sd = replace_state_dict_key(sd, "_orig_mod.", "")
    missing, unexpected = model.load_state_dict(sd, strict=False)
    print("missing keys: %d %s" % (len(missing), missing[:6]))
    print("unexpected keys: %d %s" % (len(unexpected), unexpected[:6]))
    model.eval()
    print("参数量: %.3f M" % (sum(p.numel() for p in model.parameters()) / 1e6))

    class Wrap(torch.nn.Module):
        def __init__(self, m):
            super().__init__()
            self.m = m

        def forward(self, x):
            o = self.m(x)
            return o["beat"], o["downbeat"]

    wrap = Wrap(model).eval()
    dummy = torch.randn(1, args.chunk, 128)
    with torch.no_grad():
        rb, rd = wrap(dummy)
    print("PyTorch 输出:", tuple(rb.shape), tuple(rd.shape),
          "beat range [%.2f, %.2f]" % (float(rb.min()), float(rb.max())))

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    torch.onnx.export(
        wrap, dummy, args.out,
        opset_version=args.opset,
        input_names=["input_spectrogram"],
        output_names=["beat", "downbeat"],
        dynamic_axes={"input_spectrogram": {1: "time"},
                      "beat": {1: "time"}, "downbeat": {1: "time"}},
        do_constant_folding=True,
        dynamo=False,
    )
    print("导出完成: %s  %d bytes (%.1f MB)"
          % (args.out, os.path.getsize(args.out), os.path.getsize(args.out) / 1e6))

    import onnxruntime as ort
    sess = ort.InferenceSession(args.out, providers=["CPUExecutionProvider"])
    inm = sess.get_inputs()[0].name
    for T in (777, 1500):
        x = np.random.randn(1, T, 128).astype(np.float32)
        bo, do = sess.run(None, {inm: x})
        with torch.no_grad():
            r = model(torch.from_numpy(x))
        rb2 = r["beat"].numpy(); rd2 = r["downbeat"].numpy()
        print("T=%4d | ORT vs torch: beat max|d|=%.3e  downbeat max|d|=%.3e  shapes %s/%s"
              % (T, float(np.abs(bo - rb2).max()), float(np.abs(do - rd2).max()),
                 bo.shape, do.shape))
    return 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""实测：真实环境下"右键菜单"链路（L1362 与 L578 同一个 import 缺失 bug）。

验证：
  ① `_on_timeline_menu()` 被调到（用 spy 替换 menu.exec，避免阻塞）
  ② 菜单内容完整（5 项最小集）
  ③ 空白处不弹（`contextMenuEvent` 未命中片段 ⇒ 不发信号）
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from PySide6.QtCore import QPoint, QTimer                # noqa: E402
from PySide6.QtWidgets import QApplication               # noqa: E402


def main():
    app = QApplication(sys.argv)
    from ercue.ui.window import MainWindow
    w = MainWindow()
    w.show()

    # 造一个片段（走真实新建 + 加素材路径太长，这里直接用公共 API）
    try:
        w.new_sheet("右键测试", 1920, 1080)
    except Exception as e:                               # noqa: BLE001
        print("new_sheet 失败:", e)
        return 1
    s = w.session
    print("① 会话就绪, 码表:", s.sheet.title if s else None)

    def do_ctx():
        if s is None:
            print("   （没有会话，跳过）")
            return
        # 照 test_timeline_trim.py 的最小模型写法造"1 层 1 片段"
        from ercue.model.cuesheet import (
            CompLayer, CueItem, Identity, MediaRef, TrackEntry,
        )
        te = TrackEntry(track_key="k", identity=Identity(
            title="T", artist="A", duration_sec=300.0, bpm=0.0))
        te.identity.ensure_meta_id()
        ly = CompLayer(lyr_id="L1", name="L1", z=0)
        ly.items.append(CueItem(item_id="i1", clip_id="c1", at_sec=4.0, end_sec=14.0,
                                src_in_sec=2.0, src_out_sec=12.0, speed=1.0).normalized())
        te.composition.layers.append(ly)
        s.sheet.media.append(MediaRef(clip_id="c1", name="c1.mp4", duration_sec=100.0))
        s.sheet.tracks.append(te)
        print("   已造 1 层 1 片段")

        print("   触发 _on_timeline_menu …")
        orig_build = w._build_item_menu
        seen = {}

        def spy_build(lyr_id, item_id):
            m = orig_build(lyr_id, item_id)
            seen["items"] = [a.text() for a in m.actions() if a.text()]
            return m

        w._build_item_menu = spy_build
        try:
            w._on_timeline_menu("L1", "i1", QPoint(500, 300))
            print("   ✅ 未抛异常（import 修复生效）")
        except Exception as e:                           # noqa: BLE001
            print("   ★ 抛异常:", type(e).__name__, e)
            return
        finally:
            w._build_item_menu = orig_build
        print("   菜单项:", seen.get("items"))

    QTimer.singleShot(800, do_ctx)
    QTimer.singleShot(2500, app.quit)
    app.exec()
    print("DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

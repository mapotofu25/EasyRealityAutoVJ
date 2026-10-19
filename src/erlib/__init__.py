# -*- coding: utf-8 -*-
r"""erlib —— 曲库分析包（`.eravjlib`）的**格式层 + 扫描核心**。

两边共用这一份代码，保证格式与算法**永不漂移**：

```
DJ 机器                                   演出机
┌────────────────────────┐                ┌────────────────────────┐
│ erlib（独立小软件）      │                │ ERAVJ                  │
│  scanner.Scanner       │   .eravjlib    │  format.LibReader      │
│  format.LibWriter  ────┼───────────────→│  → 指纹库 / 元数据 / 封面 │
└────────────────────────┘                └────────────────────────┘
```

⚠ 包名叫 `erlib`，内部模块 `format` 与内置函数 `format` 同名 ——
  一律用全限定名 `from erlib.format import ...`，**不要**写 `import format`。
"""

from erlib.format import (LibReader, LibWriter, FORMAT_VERSION,  # noqa: F401
                          song_id_of)
from erlib.scanner import Scanner, decode, find_audio            # noqa: F401

__all__ = ["LibReader", "LibWriter", "FORMAT_VERSION", "song_id_of",
           "Scanner", "decode", "find_audio"]

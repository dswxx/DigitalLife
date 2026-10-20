"""数字生命后端：产品版本与 Python 运行版本检查。"""

import sys


__version__ = "0.12.0"


MIN_PYTHON = (3, 11)

if sys.version_info < MIN_PYTHON:
    _cur = ".".join(str(x) for x in sys.version_info[:3])
    _need = ".".join(str(x) for x in MIN_PYTHON)
    raise RuntimeError(
        f"\n\n  ✗ Python 版本过低：当前 {_cur}，需要 {_need} 或更高。\n"
        f"    请使用 Python {_need}+；Windows 可通过 `py -3` 选择 Python。\n"
    )

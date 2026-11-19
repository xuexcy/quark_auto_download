"""任务在下载目录里用的子目录名。

早期每个任务在下载临时目录下占一个以内部 job id 命名的子目录（如 j974084fb95），
用户在 Aria2 的下载目录里只看到一串随机字符，既认不出是哪个任务，删起来也不敢删。

改成用任务名：新建任务时 job_manager 会拦重名（"任务名已存在"），所以任务名在同一个
配置库内是唯一的，可以直接当目录名用。文件名不安全的字符统一换成下划线。

两边必须算出同一个名字：
- 子进程（quark_main）读环境变量 QUARK_AUTO_DL_WORK_DIR；
- Web 进程（jobs_manager 建 Aria2 客户端做 pause/清理）用同一个函数现算。
"""

import re

# 路径分隔符、Windows 保留字符、控制字符都换成下划线；中文/空格/括号等原样保留
_UNSAFE_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')
_WHITESPACE = re.compile(r"\s+")
_MAX_LEN = 80


def work_dir_name(name: str, fallback: str = "") -> str:
    """任务名 → 下载子目录名；名字清洗后为空时回落到 fallback（通常是 job id）。"""
    raw = _WHITESPACE.sub(" ", str(name or "")).strip()
    cleaned = _UNSAFE_CHARS.sub("_", raw).strip(" .")
    if len(cleaned) > _MAX_LEN:
        cleaned = cleaned[:_MAX_LEN].rstrip(" .")
    # 只剩点 / 空格 / 下划线（比如名字是 ".." 或 "./"）同样不能用：会变成 "." 或 "_" 这种没意义的目录名
    if not cleaned or cleaned.strip(" ._") == "":
        return str(fallback or "").strip()
    return cleaned

"""Run the repository's local and CI regression checks."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


DEFAULT_ROOT = Path(__file__).resolve().parents[1]


def run(label: str, command: list[str], root: Path) -> bool:
    print(f"\n==> {label}", flush=True)
    print("    " + " ".join(command), flush=True)
    completed = subprocess.run(command, cwd=root, check=False)
    return completed.returncode == 0


def require_command(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise SystemExit(f"缺少命令：{name}。请先安装后重新运行检查。")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="运行项目的本地回归检查")
    parser.add_argument(
        "--root",
        type=Path,
        default=DEFAULT_ROOT,
        help="待检查的仓库目录，默认为当前项目根目录",
    )
    args = parser.parse_args()
    root = args.root.resolve()
    if not root.joinpath("pyproject.toml").is_file():
        raise SystemExit(f"不是有效的项目目录：{root}")

    node = require_command("node")
    git = require_command("git")
    git_command = [git, "-c", f"safe.directory={root}"]
    failures: list[str] = []

    def check(label: str, command: list[str]) -> None:
        if not run(label, command, root):
            failures.append(label)

    check(
        "Python 单元测试",
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
    )
    javascript_files = ["extension/popup.js"]
    has_extension_core = root.joinpath("extension/core.js").exists()
    if has_extension_core:
        javascript_files.append("extension/core.js")
    for javascript_file in javascript_files:
        check(
            f"JavaScript 语法检查：{javascript_file}",
            [node, "--check", javascript_file],
        )

    javascript_tests = root / "extension" / "core.test.js"
    if javascript_tests.exists():
        check(
            "JavaScript 单元测试",
            [node, str(javascript_tests.relative_to(root))],
        )
    else:
        print("\n==> JavaScript 单元测试：当前主线尚无 core.test.js，跳过", flush=True)

    compile_targets = [
        name
        for name in (
            "main.py",
            "auth.py",
            "browser.py",
            "config.py",
            "subtitle.py",
            "scripts",
            "tests",
        )
        if root.joinpath(name).exists()
    ]
    check(
        "Python 编译检查",
        [sys.executable, "-m", "compileall", "-q", *compile_targets],
    )

    check("未暂存改动格式检查", [*git_command, "diff", "--check"])
    check(
        "已暂存改动格式检查",
        [*git_command, "diff", "--cached", "--check"],
    )

    base_ref = os.environ.get("CHECK_BASE_REF", "").strip()
    before_sha = os.environ.get("CHECK_BEFORE_SHA", "").strip()
    if base_ref:
        check(
            f"相对 origin/{base_ref} 的提交格式检查",
            [*git_command, "diff", "--check", f"origin/{base_ref}...HEAD"],
        )
    elif before_sha and set(before_sha) != {"0"}:
        check(
            "本次 push 的提交格式检查",
            [*git_command, "diff", "--check", f"{before_sha}...HEAD"],
        )

    if failures:
        print("\n检查失败：", flush=True)
        for failure in failures:
            print(f"- {failure}", flush=True)
        raise SystemExit(1)
    print("\n全部本地检查通过。", flush=True)


if __name__ == "__main__":
    main()

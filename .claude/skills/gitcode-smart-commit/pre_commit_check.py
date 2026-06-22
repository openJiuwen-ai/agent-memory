#!/usr/bin/env python3
"""Pre-commit check script for smart-commit (agent-memory 工程).

Validates:
1. Python lint: ruff check + ruff format --check (ruff 未安装则跳过，不阻塞)
2. (Optional) Run smoke test: pytest evaluation/smoke_test
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


class PreCommitChecker:
    """Runs pre-commit lint and optional smoke-test suite."""

    def __init__(self, project_root: Path | None = None):
        self.project_root = project_root or self._find_project_root()
        self.errors: list[str] = []

    @staticmethod
    def _find_project_root() -> Path:
        """探测工程根：优先 pyproject.toml，其次 .git 目录。"""
        current = Path.cwd()
        while current != current.parent:
            if (current / "pyproject.toml").is_file() or (current / ".git").exists():
                return current
            current = current.parent
        return Path.cwd()

    def _run_cmd(self, cmd: list[str], description: str) -> bool:
        """Run a command and collect output on failure."""
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(self.project_root))
        if result.returncode != 0:
            output = result.stdout.strip() or result.stderr.strip()
            self.errors.append(f"   {description} FAILED (exit {result.returncode})\n{_indent(output)}")
            return False
        print(f"   {description} PASSED")
        return True

    def check_python(self) -> bool:
        """Run ruff check and ruff format --check (ruff 未安装时跳过)。"""
        changed_py = list(self.project_root.rglob("*.py"))
        changed_py = [f for f in changed_py if "__pycache__" not in str(f) and ".claude" not in str(f)]
        if not changed_py:
            print("   No Python files to check, skipping")
            return True

        ruff = shutil.which("ruff")
        if not ruff:
            print("   ruff 未安装，跳过 lint（agent-memory 暂未引入 ruff）")
            return True

        ok = True
        ok &= self._run_cmd([ruff, "check", "."], "ruff check")
        ok &= self._run_cmd([ruff, "format", "--check", "."], "ruff format --check")
        return ok

    def run_tests(self) -> bool:
        """Run the smoke-test suite (pytest evaluation/smoke_test)。"""
        smoke_dir = self.project_root / "evaluation" / "smoke_test"
        if not smoke_dir.exists():
            print("   evaluation/smoke_test not found, skipping")
            return True

        result = subprocess.run(
            [sys.executable, "-m", "pytest", "evaluation/smoke_test", "--tb=short", "-q"],
            capture_output=True, text=True, cwd=str(self.project_root),
            timeout=600,
        )
        if result.returncode != 0:
            output = result.stdout.strip() or result.stderr.strip()
            self.errors.append(f"   pytest evaluation/smoke_test FAILED\n{_indent(output[-800:])}")
            return False
        print("   pytest evaluation/smoke_test PASSED")
        return True

    def run(self, skip_tests: bool = False) -> bool:
        print("Running pre-commit checks...\n")

        # 1. Python lint
        print("1. Python lint (ruff, 可选)...")
        self.errors.clear()
        py_ok = self.check_python()
        if not py_ok:
            for e in self.errors:
                print(e)
        self.errors.clear()
        print()

        # 2. Smoke test (optional)
        test_ok = True
        if not skip_tests:
            print("2. Running smoke test (pytest evaluation/smoke_test)...")
            test_ok = self.run_tests()
            if not test_ok:
                for e in self.errors:
                    print(e)
            self.errors.clear()
            print()

        # Summary
        all_ok = py_ok and test_ok
        print("=" * 70)
        if all_ok:
            print("All pre-commit checks PASSED!")
        else:
            print("Pre-commit checks FAILED. Please fix the issues above.")
        print("=" * 70)
        return all_ok


def _indent(text: str, prefix: str = "   ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


def main():
    parser = argparse.ArgumentParser(description="Pre-commit checks")
    parser.add_argument("--skip-tests", action="store_true", help="Skip test execution")
    parser.add_argument("--project-root", type=Path, default=None, help="Project root directory")
    args = parser.parse_args()

    checker = PreCommitChecker(project_root=args.project_root)
    passed = checker.run(skip_tests=args.skip_tests)
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()

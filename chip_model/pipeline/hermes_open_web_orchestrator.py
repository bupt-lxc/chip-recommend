"""Compatibility launcher for the resumable V3 domain discovery runtime."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Callable

Runner = Callable[..., Any]


def build_hermes_prompt(*, run_id: str, unit_id: str, scope_type: str, skill: str,
                       target_chip: str, target_fields: list[str]) -> str:
    """Historical prompt formatter, retained for reading old unit artifacts."""
    fields = '、'.join(target_fields) if target_fields else '该 Skill 的全部字段'
    return (f'RUN_ID={run_id}\nUNIT_ID={unit_id}\nSCOPE_TYPE={scope_type}\n目标 Skill：{skill}\n'
            f'目标对象：{target_chip or "开放互联网中的新芯片与新来源"}\n目标字段：{fields}\n\n'
            '这是一次隔离测试。按照已加载 Skill 的完整操作规程完成本单元；'
            'Skill 是业务步骤、判断标准和输出合同的唯一来源。')


def run_hermes_open_web_test(*, source_db: str | Path, skill: str,
        target_chip: str | None = None, target_fields: list[str] | None = None,
        timeout: int = 1800, runner: Runner = subprocess.run,
        hermes_command: list[str] | None = None, max_workers: int | None = None,
        max_attempts: int = 2, resume_run_id: str | None = None) -> dict[str, Any]:
    """Old argument names map to one V3 domain; V2 archives cannot resume.

    Old worker/attempt arguments do not override the frozen domain quotas.
    Injected runners must expose V3 preflight/run, rather than subprocess-only
    unit callbacks; production always uses the installed Hermes SDK adapter.
    """
    from .domain_discovery.contracts import normalize_start
    from .domain_discovery.runtime import run_domain, HermesRunner
    if target_fields:
        raise ValueError('字段范围已停用，请按数据域运行')
    if runner is not subprocess.run and not (hasattr(runner, 'preflight') and hasattr(runner, 'run')):
        raise ValueError('旧单元 runner 不能执行 V3；需要 Hermes preflight/run 适配器')
    request = normalize_start({'skill': skill, 'chip': target_chip, 'resume_run_id': resume_run_id})
    dependencies = {'source_db': Path(source_db), 'timeout': timeout,
                    'hermes_runner': runner if hasattr(runner, 'preflight') else HermesRunner(command=hermes_command)}
    return run_domain(request, dependencies)

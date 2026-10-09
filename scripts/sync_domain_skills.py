"""Check or generate six domain Skill copies; never install into home by default."""

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from chip_model.pipeline.domain_discovery.rules import synchronize_skills  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="校验或同步六域权威 Skill 与生成副本")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="只读校验（默认）")
    mode.add_argument("--write", action="store_true", help="写入生成副本与安装清单")
    parser.add_argument("--install-root", type=Path, help="显式安装目录；不默认写入用户主目录")
    args = parser.parse_args()
    try:
        from chip_model.domain_tool_api import tool_contract, verify_tool_contract
        if args.write:
            import yaml
            contract = tool_contract()
            encoded = json.dumps(contract, ensure_ascii=False, sort_keys=True, indent=2) + '\n'
            (PROJECT_ROOT / '.hermes-skills/_shared/tool-contract.json').write_text(encoded, encoding='utf-8', newline='\n')
            plugin = PROJECT_ROOT / '.hermes-plugins/aishperf-open-web'
            (plugin / 'tool-contract.json').write_text(encoded, encoding='utf-8', newline='\n')
            manifest_path = plugin / 'plugin.yaml'
            metadata = yaml.safe_load(manifest_path.read_text(encoding='utf-8'))
            metadata.update(version=contract['tool_version'], tool_contract_sha256=contract['tool_contract_sha256'],
                            provides_tools=[tool['name'] for tool in contract['tools']])
            manifest_path.write_text(yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False), encoding='utf-8', newline='\n')
        manifest = synchronize_skills(PROJECT_ROOT / ".hermes-skills", PROJECT_ROOT / ".agents/skills", write=args.write, install_root=args.install_root)
        verify_tool_contract(project_root=PROJECT_ROOT)
    except (ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps({"rule_version": manifest["rule_version"], "bundle_sha256": manifest["bundle_sha256"], "file_count": len(manifest["files"]), "mode": "write" if args.write else "check"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

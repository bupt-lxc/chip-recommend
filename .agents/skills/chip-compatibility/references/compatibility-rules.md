# 兼容领域判定（3.0.0）

读取完整 `domain.json` 中分表字段和关系键，芯片软件栈声明与芯片×模型关系分别建候选。来源可为官方支持矩阵、软件版本文档、可复核验证报告或社区复现；保留明确 SKU、完整模型 org/model/变体、框架及版本、精度、软件栈版本和环境。一般框架支持不能生成任意模型关系。

compat_status 随证据：可核验实际验证为 verified，厂商支持声明为 vendor_claimed，社区报告为 community；明确不支持可 unsupported，不足证据 unknown。未复现或没有对应可核验验证证据，不能把宣传升级 verified。verified_at 是验证证据日期，不能用抓取日期替代。可选版本/环境缺失留 unknown，不能凭常识补齐。

正例：厂商矩阵明确“X1 + org/model-7B + Framework 2.3, BF16 supported”产生 vendor_claimed 关系及适用范围；有测试配置、执行结果和可核验报告可按证据记录 verified。泛称“X1 支持 Framework 2.3”只生成该芯片软件栈声明，不生成任何具体模型关系。以上为假设例子。

反例：其他 SKU 的支持表套当前型号；“支持 Transformer”生成库中所有模型；未明示 FP4 却推断；厂商 logo 或通用教程当 verified；把库外模型编造正式 ID 或启动 HF API。库外芯片/模型可以保存关系与 dependency_unresolved，身份由公共 identity 依赖处理。

多实体/版本/精度分别绑定证据，不拼接不同报告环境。逐字表头、脚注、offset 与冻结快照、程序校验及独立复核遵守公共参考；通过的支持声明和历史关系多义时 comparison_unresolved，不覆盖。页面部署步骤可提示同页一层部署关联，本域不执行命令。

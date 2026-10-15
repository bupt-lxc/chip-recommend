# 算力领域判定（3.0.0）

完整字段、精度、单位和保留格式读取同包 `domain.json`。使用每 SKU 的官方计算规格、精度表、架构白皮书；这里是理论峰值与计算单元，不是吞吐/时延实测。FP64、FP32、TF32、BF16、FP16、FP8、FP4、INT8、INT4 等逐项核验；某项无证据是 unknown，不意味着不支持，也不能从 FP8 推断 FP4。

峰值必须同一 SKU、同一精度/计算单元、同一 Dense/Sparse 与 TFLOPS/TOPS 口径。逐字保留表头/行/脚注；不跨表列拼接精度和值，不把整数 TOPS 改写浮点 TFLOPS。稀疏/稠密、计算单元类型及算法基准无法无损存入现有格式时，完整保留在候选元数据并标明，不能合并覆盖。

现有 precision_perf 可兼容字符串如 BF16=1980TF,FP8=3960TF,INT8=3960TOPS；这只是格式例子，不是证据或默认值。每项单独保留 source_precision、source_unit、source_value、dense_or_sparse、calculation_basis。计算单元数量保留类型，SM、Tensor Core、RT Core 与普通着色单元不可互换。

正例：X1 单卡表明确“FP16 Dense 100 TFLOPS; FP16 Sparse 200 TFLOPS”可形成两个不同口径候选并保留表头，不能把 200 直接覆盖 Dense；“INT8 300 TOPS”只支持明示 INT8 项。以上为假设例子。

反例：Sparse 200 写 Dense 200；按稀疏除 2 推断稠密；见 FP8 就添加 FP4 支持；从理论 TFLOPS 写成模型实测 tokens/s；服务器多卡峰值当单卡；其他 SKU 的 SM 数当当前型号。无明确口径留 unknown 或未解析。

证据、程序检查、独立语义复核及一次修订上限遵守公共参考；同页实测信息只提示其他域，最多一层关联。身份未确认不计新芯片；历史算力口径不可比时 comparison_unresolved，保留冲突而不覆盖。

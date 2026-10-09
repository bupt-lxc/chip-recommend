"""AISHPerf Knowledge Graph — FastAPI Server.

Usage:
    python server.py                    # standalone
    python cli.py --server --db-path data.db   # via CLI flag
"""

import json
import re
import math
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path as FilePath
from typing import Annotated, Optional

from fastapi import (
    BackgroundTasks, FastAPI, Query, Path, Body, HTTPException, Response, Header, Request,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from scalar_fastapi import get_scalar_api_reference, Theme

from chip_model.database import (
    get_db,
    get_db_path,
    get_project_root,
    set_db_path,
    get_db_stats,
    search_chips,
    ChipFilters,
    get_chip_profile,
    get_chip_profiles_batch,
    get_chip_recommend_candidates,
    search_models,
    ModelFilters,
    get_model_profile,
    get_model_profiles_batch,
    search_benchmarks,
    BenchmarkFilters,
    search_compat,
    CompatFilters,
    search_provenance,
    ProvenanceFilters,
    get_provenance_stats,
    chip_recommend_candidate,
    get_chip_benchmarks_for_model,
    get_chip_benchmark_mfu,
    get_chip_benchmark_tps,
    get_chip_model_compat_count,
    get_chip_source_credibility,
    get_deployment_guide,
    classify_backend,
    get_model_config_json,
)
from chip_model.scoring import (  # v4.2 scoring engine
    parse_fp16,
    round_up_pow2,
    RecommendContext,
    CategoryWeights,
    ScoringWeights,
    TRAIN_WEIGHTS,
    INFERENCE_WEIGHTS,
    WEIGHTS_CPT,
    WEIGHTS_SFT_FULL,
    WEIGHTS_SFT_LORA,
    WEIGHTS_RL,
    WEIGHTS_QUANTIZE,
    WEIGHTS_INFER_FP16,
    WEIGHTS_INFER_QUANT,
    get_scenario_weights,
    get_category_weights,
    CATEGORY_DEFS,
    estimate_vram_total,
    estimate_card_count,
    estimate_inference_concurrency_cards,
    estimate_training_flops,
    resolve_arch_params,
    resolve_moe_metadata,
    DimensionResult,
    ScoringResult,
    aggregate_score,
    scoring_result_to_dict,
    DIMENSION_META,
)
from chip_model.pipeline.data_agent import DataAgentOrchestrator, get_data_agent_status
from chip_model.pipeline.test_workspace import latest_test_db
from chip_model.pipeline.open_web_test import (
    list_open_web_test_runs,
    read_open_web_test_run,
)
from chip_model.pipeline.link_families import (
    read_chip_families, read_family_selection, save_family_selection,
)
from chip_model.pipeline.manual_control import (
    admin_token_valid, latest_manual_result, list_seed_urls,
    execute_open_web_manual_request, queue_manual_run, save_seed_url,
)
from chip_model.measured_inference import (
    get_comparable_measurements,
    score_comparable_measurements,
)


TRAINING_TOKENS_DEFAULT_T = {
    "cpt": 10.0,
    "sft": 0.2,
    "rl": 0.2,
}

# ── Lifespan ────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Set the persistent DB and apply additive operational migrations."""
    import os
    env_path = os.environ.get("DATA_DB_PATH")
    if env_path:
        set_db_path(env_path)
    DataAgentOrchestrator(db_path=get_db_path()).ensure_schema()
    yield


# ── App ─────────────────────────────────────────────────────────

app = FastAPI(
    title="AISHPerf Knowledge Graph",
    description="AI Chip × Model knowledge graph query API",
    version="0.4.0",
    lifespan=lifespan,
    docs_url=None,   # disabled — replaced by Scalar at /docs
    redoc_url=None,  # disabled
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Scalar API reference (modern interactive docs) ──

from scalar_fastapi import get_scalar_api_reference, Theme

@app.get("/docs", include_in_schema=False)
async def scalar_docs():
    return get_scalar_api_reference(
        openapi_url=app.openapi_url,
        title="AISHPerf API 测试 — 算力选型知识图谱",
        theme=Theme.DEEP_SPACE,
        show_sidebar=True,
        hide_download_button=False,
        # 中文界面 + 预填可用的示例值（default_open_all_tags 便于快速定位端点）
        overrides={
            "localization": {"locale": "zh-CN"},
            "defaultOpenAllTags": True,
        },
    )


# ═══════════════════════════════════════════════════════════════
# Batch request models
# ═══════════════════════════════════════════════════════════════

class BatchRequest(BaseModel):
    identifiers: list[str]


class ChatRequest(BaseModel):
    messages: list[dict]
    model: str | None = None


# ═══════════════════════════════════════════════════════════════
# Chat Agent (DeepSeek streaming)
# ═══════════════════════════════════════════════════════════════

@app.post("/api/v1/chat")
async def api_chat(
    body: Annotated[ChatRequest, Body(examples=[{"messages": [
        {"role": "user", "content": "我想训练 Qwen2.5-7B，3 天完成，国产优先，给出方案"},
    ]}])],
):
    """Streaming chat with DeepSeek + AISHPerf tool calling."""
    from chip_model.chat_agent import chat_stream, DEEPSEEK_MODEL

    async def event_generator():
        async for chunk in chat_stream(body.messages, body.model or DEEPSEEK_MODEL):
            yield chunk
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )


# ═══════════════════════════════════════════════════════════════
# Chips
# ═══════════════════════════════════════════════════════════════

@app.get("/api/v1/chips")
def api_chip_search(
    search: Optional[str] = Query(None, description="Fuzzy search (vendor / model / series / architecture)"),
    keyword: Optional[str] = Query(None, description="Alias for 'search' parameter"),
    vendor: Optional[str] = Query(None),
    region: Optional[str] = Query(None, description="domestic | foreign"),
    usage: Optional[str] = Query(None, description="train | inference | both"),
    vram_min: Optional[float] = Query(None, description="Min VRAM (GB)"),
    vram_max: Optional[float] = Query(None, description="Max VRAM (GB)"),
    tdp_max: Optional[float] = Query(None, description="Max TDP (W)"),
    price_max: Optional[float] = Query(None, description="Max unit price (万元/片)"),
    interconnect_min: Optional[float] = Query(None, description="Min interconnect BW (GB/s)"),
    tier: Optional[str] = Query(None, description="datacenter | consumer | all"),
    for_model: Optional[str] = Query(None, description="Auto-estimate VRAM for this model"),
    scenario: Optional[str] = Query(None, description="train | inference (with for_model)"),
    limit: int = Query(50, le=2000),
    offset: int = Query(0),
    include_provenance: bool = Query(False, description="Include per-chip field provenance summary"),
):
    """Search chips with multi-condition filtering."""
    # Accept 'keyword' as alias for 'search'
    effective_search = search or keyword
    filters = ChipFilters(
        search=effective_search, vendor=vendor, region=region, usage=usage,
        vram_min=vram_min, vram_max=vram_max, tdp_max=tdp_max,
        price_max=price_max, interconnect_min=interconnect_min,
        tier=tier,
        for_model=for_model, scenario=scenario,
    )
    return search_chips(filters, limit=limit, offset=offset,
                        include_provenance=include_provenance)


def _max_per_card_tps(bench_records: list[dict] | None) -> float | None:
    """从 benchmark 记录里取最大单卡实测吞吐 (throughput_tok_s / chip_count)。

    用于推理理想档的「吞吐上限」锚定。返回 None 表示无实测数据。
    """
    best: float | None = None
    for r in bench_records or []:
        try:
            tps = float(r.get("throughput_tok_s") or 0)
            cnt = int(r.get("chip_count") or 1)
        except (ValueError, TypeError):
            continue
        if tps > 0 and cnt > 0:
            per_card = tps / cnt
            if best is None or per_card > best:
                best = per_card
    return best


@app.get("/api/v1/chips/recommend")
def api_chip_recommend(
    model: str = Query(..., description="Model name (fuzzy match)",
                       examples=["Qwen2.5-7B", "Llama-3.1-8B", "DeepSeek-V3"]),
    scenario: str = Query("train", description="train | quantize | inference"),
    stage: Optional[str] = Query("sft", description="[train] cpt | sft | rl"),
    method: Optional[str] = Query("full_param", description="[train] full_param | lora  [quantize] gptq | awq | bitsandbytes | gguf"),
    quant: Optional[str] = Query("fp16", description="[inference] fp16 | int8 | int4_gptq | int4_awq | gguf_q4 | gguf_q8"),
    quantize_bits: Optional[str] = Query("int4", description="[quantize] int8 | int4 | fp8"),
    training_days: Optional[float] = Query(None, description="Target training days"),
    training_tokens: Optional[float] = Query(
        None,
        gt=0,
        description="Training data volume (T tokens); defaults: CPT=10T, SFT/RL=0.2T",
    ),
    sla_tps: Optional[float] = Query(None, description="Target inference throughput (tok/s)"),
    tier: Optional[str] = Query("datacenter", description="datacenter | all"),
    max_cards: Optional[int] = Query(None, description="Hard exclude: max cards"),
    min_cards: Optional[int] = Query(None, description="Hard floor: min cards (round up to pow2)"),
    max_price: Optional[float] = Query(None, description="Hard exclude: max unit price (万元)"),
    prefer_domestic: bool = Query(False, description="Prefer domestic chips"),
    prefer_vendor: Optional[str] = Query(None, description="Prefer vendor"),
    tco_weight: float = Query(0.0, ge=0.0, le=0.3, description="TCO dimension weight 0.0-0.3"),
    max_context: int = Query(4096, ge=1, description="推理: input_len 未提供时的兼容回退值"),
    concurrency: int = Query(1, ge=1, description="推理: 目标并发请求数"),
    input_len: Optional[int] = Query(None, ge=1, description="推理: 单请求输入长度 (tokens)"),
    output_len: Optional[int] = Query(None, ge=1, description="推理: 单请求最大输出长度 (tokens)"),
    batch_size: int = Query(1, ge=1, description="训练: batch size"),
    seq_len: int = Query(2048, ge=1, description="训练: 样本长度 (tokens)"),
    limit: int = Query(5, le=20),
):
    """Recommend chips for a model × scenario × constraints.  v3.1 quantize scenario.

    Training: choose stage (CPT/SFT/RL) + method (full_param/LoRA).
    Quantize: choose method (GPTQ/AWQ/bitsandbytes/GGUF) + bits (INT8/INT4/FP8).
    Inference: choose quantization (FP16/INT8/INT4-GPTQ/AWQ/GGUF).
    """
    stage_val = (stage or "sft").lower()
    method_val = method or "full_param"
    quant_val = quant or "fp16"
    quantize_bits_val = quantize_bits or "int4"

    # 1. Find model
    model_result = search_models(ModelFilters(search=model), limit=1)
    if model_result["count"] == 0:
        raise HTTPException(404, f"未找到模型: {model}，请检查模型名称是否正确（支持模糊匹配，如 Qwen2.5-7B、Llama-3.1-8B）")
    model_data = model_result["models"][0]

    total_params = float(model_data.get("total_params_b", 0) or 0)
    model_id = str(model_data.get("model_id", "") or "")
    arch_family = str(model_data.get("architecture_family", "") or "")

    # 2a. Resolve MoE metadata. Active parameters describe per-token compute;
    # resident weight memory is always based on total parameters in this mode.
    config_raw = get_model_config_json(model_id) or model_data.get("config_json", "") or ""
    moe_meta = resolve_moe_metadata(model_id, arch_family, total_params, config_raw)
    moe_activated = moe_meta.get("active_params_b")

    # 2b. Check quantized model in model data
    import json as _json
    is_quantized = False
    quant_bits = None
    try:
        cfg = _json.loads(config_raw) if config_raw else {}
        is_quantized = cfg.get("is_quantized", False)
        quant_bits = cfg.get("quant_bits")
    except Exception:
        pass

    if scenario == "train" and is_quantized:
        raise HTTPException(400, f"量化模型 {model_id} 只能用于推理场景，不支持训练")

    # 3. Calculate VRAM (minimum / complete single-request tiers) + FLOPs.
    # For inference, one request's peak KV cache always covers input + output.
    inference_input_len = input_len if input_len and input_len > 0 else max_context
    inference_output_len = output_len if output_len and output_len > 0 else 512
    arch_params = resolve_arch_params(config_raw, total_params)
    vram_est = estimate_vram_total(
        total_params, scenario=scenario,
        stage=stage_val, method=method_val, quant=quant_val,
        quantize_bits=quantize_bits_val,
        moe_activated_B=moe_activated,
        max_context=max_context, concurrency=concurrency,
        input_len=inference_input_len, output_len=inference_output_len,
        batch_size=batch_size, seq_len=seq_len,
        arch=arch_params,
    )
    min_vram_total = vram_est.get("min_vram_raw", vram_est["min_vram"])
    full_vram_total = vram_est.get("full_vram_raw", vram_est["full_vram"])
    min_vram_formula = vram_est["min_formula"]
    full_vram_formula = vram_est["full_formula"]
    kv_cache_gb = vram_est.get("kv_cache_gb_raw", vram_est.get("kv_cache_gb", 0.0))
    target_vram_total = vram_est.get("target_vram_raw", full_vram_total)
    vram_formula = full_vram_formula

    if scenario == "train":
        training_tokens_val = (
            training_tokens
            if training_tokens is not None
            else TRAINING_TOKENS_DEFAULT_T.get(stage_val, TRAINING_TOKENS_DEFAULT_T["sft"])
        )
        # Dense models execute all parameters per token. MoE models keep all
        # expert weights resident in VRAM, but only the routed/active parameter
        # subset contributes to per-token training compute.
        training_compute_params = float(moe_meta.get("compute_params_b") or total_params)
        training_compute_basis = str(moe_meta.get("compute_parameter_basis") or "total")
        total_flops = estimate_training_flops(training_compute_params, training_tokens_val)
    else:
        training_tokens_val = 0.0
        training_compute_params = 0.0
        training_compute_basis = "total"
        total_flops = 0.0

    model_summary = f"{model_id} | {arch_family} | {total_params}B params"
    if moe_meta.get("is_moe") and moe_activated:
        model_summary += f" | {moe_activated}B active/token"

    # 4. Get candidates (v3.0: no min_maturity filtering)
    _, candidates = get_chip_recommend_candidates(
        model, scenario=scenario, tier=tier or "datacenter",
        prefer_domestic=prefer_domestic,
        measured_model_id=(model_id if scenario == "inference" and
                           model_id.casefold().endswith("/deepseek-v4-flash") else None),
    )

    if not candidates:
        if scenario == "train" and moe_activated and moe_activated < total_params:
            raise HTTPException(
                404,
                f"MoE模型 {model_id} 的标准常驻权重部署需加载全部 {total_params:.0f}B 参数 "
                f"(每 token 激活 {moe_activated:.0f}B 只影响计算量)，"
                f"VRAM 估算 ≥{min_vram_total:.0f}GB。当前数据库无单卡满足需求的芯片。\n"
                f"建议：\n"
                f"1. 推理使用 INT8/INT4 权重量化降低常驻权重显存\n"
                f"2. 使用张量并行/专家并行将全部专家权重分片到多卡\n"
                f"3. 只有显式启用 CPU/NVMe 专家卸载时，才可采用另一套显存模型"
            )
        raise HTTPException(404, f"没有芯片满足 {model} 的VRAM需求 (≥{min_vram_total:.0f}GB)，请尝试其他模型或放宽约束")

    # 5. Get scenario-specific category weights (v4.4: 4-category system)
    cat_weights, scenario_label = get_category_weights(
        scenario, stage=stage_val, method=method_val, quant=quant_val,
        quantize_bits=quantize_bits_val,
    )

    # The workbook's Flash measurements are a model-specific inference
    # reference.  Do not rank by same-scale/other-model fallback benchmarks.
    is_flash_inference = (
        scenario == "inference"
        and model_id.casefold().endswith("/deepseek-v4-flash")
    )
    measured_scores = {}
    if is_flash_inference:
        measured_scores = score_comparable_measurements(
            get_comparable_measurements(
                model_id, [str(c.get("chip_model") or "") for c in candidates],
            )
        )
        if measured_scores:
            cat_weights = CategoryWeights(
                ecosystem_maturity=0.20,
                benchmark_evidence=0.60,
                compute_power=0.15,
                cost_effectiveness=0.05,
            )

    # 6. Scoring loop
    scored: list[dict] = []

    for chip in candidates:
        chip_dict = dict(chip)
        vram = float(chip_dict.get("vram_gb", 0) or 0)
        price_wan = float(chip_dict.get("price_cny_wan", 0) or 0)
        chip_model_name = str(chip_dict.get("chip_model", "") or "")

        # benchmark 记录提前取，理想档「吞吐上限」锚定要用
        bench_records = get_chip_benchmarks_for_model(
            chip_model_name, model_id, total_params,
        )
        if is_flash_inference:
            # The fixed reference workload is for ranking, not a guarantee
            # of TPS at arbitrary user-selected input/output/concurrency.
            # In particular, never size Flash SLA cards from a fallback model.
            bench_records = []

        # ── Card estimation (inference: 最小=权重+单请求峰值KV / 目标并发) ──
        # Exact ceiling first, then power-of-two topology rounding.  Do not add
        # an unconditional extra card and do not silently cap large clusters.
        min_card_calc = estimate_card_count(min_vram_total, vram)
        full_card_calc = estimate_card_count(full_vram_total, vram)
        vram_cards = min_card_calc["rounded_cards"]
        full_cards = full_card_calc["rounded_cards"]
        if full_cards < vram_cards:
            full_cards = vram_cards
        deployment_floor_cards = full_cards if scenario == "inference" else vram_cards
        ideal_cards = deployment_floor_cards
        estimated_days = None
        single_machine_conc = None  # compatibility field; shared-pool sizing no longer uses replica capacity
        ideal_calc = {
            "basis": "full",
            "raw_cards": (full_card_calc if scenario == "inference" else min_card_calc)["raw_cards"],
            "rounded_cards": deployment_floor_cards,
            "formula": (
                "目标并发无需增加额外实例，目标并发卡数 = 最小部署卡数"
                if scenario == "inference"
                else "未设置额外性能目标，理想部署卡数 = 最小部署卡数"
            ),
        }

        fp16_val = parse_fp16(chip_dict.get("precision_perf", ""))

        if scenario == "train" and fp16_val > 0:
            # 训练理想部署：按训练时长 (FLOPs + MFU) 反推卡数
            bench_mfu = get_chip_benchmark_mfu(chip_model_name)
            mfu_target = (bench_mfu / 100.0) if bench_mfu else 0.30
            effective_per_card_day = fp16_val * 1e12 * mfu_target * 86400
            if effective_per_card_day > 0 and training_days:
                raw_compute = max(1, math.ceil(total_flops / (effective_per_card_day * training_days)))
                compute_cards = round_up_pow2(raw_compute)
                ideal_cards = max(vram_cards, compute_cards)
                estimated_days = round(
                    total_flops / (effective_per_card_day * ideal_cards), 1
                )
                ideal_calc = {
                    "basis": "training_performance",
                    "raw_cards": raw_compute,
                    "rounded_cards": ideal_cards,
                    "compute_rounded_cards": compute_cards,
                    "compute_params_b": training_compute_params,
                    "compute_parameter_basis": training_compute_basis,
                    "training_tokens_t": training_tokens_val,
                    "flop_multiplier": 6,
                    "mfu": round(mfu_target, 4),
                    "mfu_source": "benchmark" if bench_mfu else "default",
                    "fp16_tflops": round(fp16_val, 2),
                    "total_flops": total_flops,
                    "target_days": training_days,
                    "formula": (
                        f"总训练计算量 = 6 × {training_compute_params:.1f}B"
                        f"（{'每token激活参数' if training_compute_basis == 'active' else '总参数'}）"
                        f" × {training_tokens_val:g}T tokens = {total_flops:.3e} FLOPs；"
                        f"ceil({total_flops:.3e} ÷ ({fp16_val:.2f} TFLOPS/卡 × 10¹² × "
                        f"{mfu_target:.2f} MFU × 86400秒 × {training_days:g}天)) = {raw_compute} 卡"
                        f" → 取2的幂 = {compute_cards} 卡 → 与最小部署 {vram_cards} 卡取较大值 = {ideal_cards} 卡"
                    ),
                }
        elif scenario == "inference":
            # 目标并发按统一显存池计算：常驻权重只加载一份，每个在途
            # 请求增加一份峰值 KV。仅当用户显式填写单请求目标吞吐且有
            # 同模型实测吞吐时，才叠加吞吐卡数约束；不使用隐藏的60秒假设。
            ideal_calc = estimate_inference_concurrency_cards(
                weight_vram_gb=vram_est.get("weight_vram_raw", vram_est.get("weight_vram", 0.0)),
                per_request_kv_gb=vram_est.get("ideal_kv_gb_raw", vram_est.get("ideal_kv_gb", 0.0)),
                concurrency=concurrency,
                per_card_vram_gb=vram,
                per_card_tps=_max_per_card_tps(bench_records),
                per_request_tps=sla_tps,
            )
            ideal_cards = max(full_cards, ideal_calc["rounded_cards"])
            ideal_calc["rounded_cards"] = ideal_cards

        # 单调约束：推理目标并发 ≥ 最小部署；训练/量化理想部署 ≥ 最小部署。
        deployment_floor_cards = full_cards if scenario == "inference" else vram_cards
        if ideal_cards < deployment_floor_cards:
            ideal_cards = deployment_floor_cards
        if full_cards < vram_cards:
            full_cards = vram_cards

        recommended_cards = ideal_cards

        if min_cards:
            min_cards_pow2 = round_up_pow2(min_cards)
            if recommended_cards < min_cards_pow2:
                recommended_cards = min_cards_pow2
                ideal_calc["minimum_card_constraint"] = min_cards
                ideal_calc["rounded_cards"] = recommended_cards
                ideal_calc["formula"] += f"；用户最小卡数约束 {min_cards} → {min_cards_pow2} 卡"

        # Hard exclude (v3.0: no min_maturity)
        if max_cards and recommended_cards > max_cards:
            continue
        if max_price and price_wan and price_wan > max_price:
            continue

        meets_sla = True
        if training_days and estimated_days is not None and estimated_days > training_days:
            meets_sla = False

        # ── Bandwidth need estimate ──
        # Model bandwidth need: inference 1 card must at least have enough BW
        # to keep up with compute. For training it's higher (gradient comm).
        # Simple formula: compute-bound BW = FP16 TFLOPS × 1000 / 30 GFLOPS/byte
        # (typical efficiency ratio for LLM inference)
        # For multi-card training: inter-card comm ≈ model_params × 2 × cards
        model_bandwidth_gb_s = 0.0
        vram_bw = float(chip_dict.get("vram_bw_gb_s", 0) or 0)
        if fp16_val > 0 and vram_bw > 0:
            if scenario == "train":
                # Training: gradient all-reduce bandwidth per step
                # Roughly: model params × 2 (fp16) × 3 (forward+backward+grad) / step_sec
                # Simplified: use compute BW proxy
                model_bandwidth_gb_s = fp16_val * 50  # ~50 GB/s per TFLOPS for training comm
            elif scenario == "inference":
                # Inference: memory-bound, model params × 2 bytes / token × tokens/s
                # Simplified: vram_bw is the bottleneck, model needs roughly vram_bw of 1 card
                model_bandwidth_gb_s = vram_bw * 0.8  # 80% of single-card BW as baseline need
            else:
                # Quantize: similar to inference
                model_bandwidth_gb_s = vram_bw * 0.6

        # ── Benchmark data ──
        bench_mfu_val = get_chip_benchmark_mfu(chip_model_name)
        bench_tps_val = get_chip_benchmark_tps(chip_model_name)
        if is_flash_inference:
            # An unrelated benchmark or a bare model mention is not a Flash
            # inference observation; every unmeasured chip gets the same 50.
            bench_mfu_val = None
            bench_tps_val = None
        compat_verified = get_chip_model_compat_count(chip_model_name)
        official_ratio = get_chip_source_credibility(chip_model_name)

        # ── v4.2 Scoring ──
        ctx = RecommendContext(
            chip=chip_dict,
            model_params_B=total_params,
            scenario=scenario,
            stage=stage_val,
            method=method_val,
            quant=quant_val,
            quantize_bits=quantize_bits_val,
            min_vram_total=min_vram_total,
            vram_formula=vram_formula,
            vram_cards=vram_cards,
            recommended_cards=recommended_cards,
            fp16_tflops=fp16_val,
            model_bandwidth_gb_s=model_bandwidth_gb_s,
            training_tokens_T=training_tokens_val,
            target_training_days=training_days,
            target_tps=sla_tps,
            estimated_training_days=estimated_days,
            benchmark_count=len(bench_records),
            max_benchmark_mfu=bench_mfu_val,
            max_benchmark_tps=bench_tps_val,
            measured_inference=measured_scores.get(chip_model_name),
            compat_verified_count=compat_verified,
            official_ratio=official_ratio,
        )

        scoring_result = aggregate_score(
            ctx, cat_weights,
        )

        scored.append({
            "chip": chip_dict,
            "vram_cards": vram_cards,
            "full_cards": full_cards,
            "recommended_cards": recommended_cards,
            "estimated_training_days": estimated_days,
            "meets_sla": meets_sla,
            "price_wan": price_wan,
            "single_machine_concurrency": single_machine_conc,
            "card_calculation": {
                "rounding_rule": "先向上取整，再向上取最接近的2的幂；不额外加卡，不设置静默上限",
                "per_card_vram_gb": round(vram, 1),
                "display_tiers": ["minimum", "ideal"],
                "minimum": {
                    **(full_card_calc if scenario == "inference" else min_card_calc),
                    "label": "最小部署",
                },
                "full": {
                    **full_card_calc,
                    "label": "最小部署" if scenario == "inference" else "最小部署（兼容字段）",
                    "visible": scenario == "inference",
                    "deprecated": scenario != "inference",
                },
                "ideal": {
                    **ideal_calc,
                    "label": "目标并发部署" if scenario == "inference" else "理想部署",
                },
            },
            "score": scoring_result.total_score,
            "scoring": scoring_result_to_dict(scoring_result),
            "measured_inference": measured_scores.get(chip_model_name),
        })

    if not scored:
        raise HTTPException(404, "所有候选芯片均被硬约束排除，请放宽最大卡数或最高单价限制")

    scored.sort(
        key=lambda x: (bool(x["measured_inference"]), x["score"])
        if measured_scores else x["score"],
        reverse=True,
    )
    top = scored[:limit]

    return {
        "model": model_summary,
        "requirements": {
            "scenario": scenario,
            "stage": stage_val if scenario == "train" else None,
            "method": method_val if scenario in ("train", "quantize") else None,
            "quant": quant_val if scenario == "inference" else None,
            "quantize_bits": quantize_bits_val if scenario == "quantize" else None,
            "scenario_label": scenario_label,
            "vram_formula": vram_formula,
            "min_vram_gb": round(min_vram_total, 1),
            "full_vram_gb": round(full_vram_total, 1),
            "target_vram_gb": round(target_vram_total, 1) if scenario == "inference" else None,
            "kv_cache_gb": round(kv_cache_gb, 3) if scenario == "inference" else None,
            "target_kv_cache_gb": vram_est.get("target_kv_gb") if scenario == "inference" else None,
            "kv_cache_dtype": "BF16/FP16 (2 bytes/element)" if scenario == "inference" else None,
            "min_vram_formula": min_vram_formula,
            "full_vram_formula": full_vram_formula,
            "training_tokens_T": round(training_tokens_val, 1) if scenario == "train" else None,
            "target_training_days": training_days if scenario == "train" else None,
            "target_tokens_per_sec": sla_tps,
            "max_context": max_context if scenario == "inference" else None,
            "concurrency": concurrency if scenario == "inference" else None,
            "input_len": inference_input_len if scenario == "inference" else None,
            "output_len": inference_output_len if scenario == "inference" else None,
            "total_context": vram_est.get("total_context") if scenario == "inference" else None,
            "context_basis": "input_plus_output" if scenario == "inference" else None,
            "ideal_kv_gb": vram_est.get("ideal_kv_gb") if scenario == "inference" else None,
            "batch_size": batch_size if scenario == "train" else None,
            "seq_len": seq_len if scenario == "train" else None,
            "model_calculation": moe_meta,
            "measured_ranking": {
                "enabled": bool(measured_scores),
                "model_specific": is_flash_inference,
                "measured_chip_count": len(measured_scores),
                "note": "同模型同参考输入/输出/并发实测优先；未实测芯片的实测项统一为中性50分。"
                if is_flash_inference else None,
            },
            "vram_calculation": vram_est.get("calculation"),
            "max_cards": max_cards,
            "min_cards": min_cards,
            "max_price_wan": max_price,
        },
        "scoring_meta": {
            "version": "4.4.0",
            "scenario_label": scenario_label,
            "category_weights": cat_weights.to_dict(),
            "dimensions": DIMENSION_META,
            "categories": CATEGORY_DEFS,
        },
        "candidates": [
            chip_recommend_candidate_v2(
                chip=s["chip"],
                vram_cards=s["vram_cards"],
                recommended_cards=s["recommended_cards"],
                full_cards=s["full_cards"],
                estimated_training_days=s["estimated_training_days"],
                meets_sla=s["meets_sla"],
                total_cost_wan=(
                    round(s["price_wan"] * s["recommended_cards"], 1)
                    if s["price_wan"] else None
                ),
                single_machine_concurrency=s["single_machine_concurrency"],
                card_calculation=s["card_calculation"],
                measured_inference=s["measured_inference"],
                score=s["score"],
                scoring=s["scoring"],
                deployment_guide=get_deployment_guide(
                    s["chip"].get("chip_model", ""),
                    model_id,
                    backend=classify_backend(
                        s["chip"].get("vendor", ""),
                        s["chip"].get("chip_type", ""),
                    ),
                ),
            )
            for s in top
        ],
        "rejected": len(scored) - len(top),
    }


# ── v2.0 chip_recommend_candidate (with scoring breakdown) ──

def chip_recommend_candidate_v2(
    chip: dict,
    vram_cards: int,
    recommended_cards: int,
    estimated_training_days: float | None,
    meets_sla: bool,
    total_cost_wan: float | None,
    score: float,
    scoring: dict,
    deployment_guide: dict | None = None,
    full_cards: int | None = None,
    single_machine_concurrency: float | None = None,
    card_calculation: dict | None = None,
    measured_inference: dict | None = None,
) -> dict:
    """Format one scored chip for v2 recommend output (includes dimension breakdown)."""
    # Reuse base candidate shape but add scoring
    base = chip_recommend_candidate(
        chip=chip,
        vram_cards=vram_cards,
        recommended_cards=recommended_cards,
        estimated_training_days=estimated_training_days,
        meets_sla=meets_sla,
        total_cost_wan=total_cost_wan,
        score=score,
        full_cards=full_cards,
        single_machine_concurrency=single_machine_concurrency,
    )
    base["scoring"] = scoring
    base["deployment_guide"] = deployment_guide
    base["recommend"]["card_calculation"] = card_calculation or {}
    base["recommend"]["measured_inference"] = measured_inference
    return base


# ── Methodology endpoint ──

@app.get("/api/v1/methodology")
def api_methodology():
    """Return scoring methodology documentation for the UI (v4.4: 4-category system, 8 dimensions)."""
    return {
        "version": "4.4.0",
        "description": "AISHPerf 芯片推荐引擎 — 4大类·8子维度 分层评分方法 (v4.4)",
        "card_estimation": {
            "training_tokens_defaults": "训练数据量默认值：CPT=10T tokens；SFT=0.2T tokens（200M）；RL=0.2T tokens（200M）。用户输入时以输入值为准。",
            "vram_train": "最小: P×12×1.25(权重+优化器) + 激活值(batch×seq×hidden×layers×40B)",
            "vram_train_lora": "LoRA: P×2.5×1.25(冻结基座) + 激活值(batch×seq×hidden×layers×40B)",
            "vram_train_tiers": "训练仅两档：最小部署=显存下限；理想部署=max(最小部署, 按FLOPs/MFU/目标天数反推的卡数)",
            "vram_quantize": "GPTQ:3.5×P×1.25 / AWQ:3.0×P×1.25 / bitsandbytes:2.5×P×1.25 / GGUF:2.5×P×1.25",
            "vram_inference": "最小: 总参数量P×精度字节×1.25 + 单请求峰值KV Cache(2×layers×kv_heads×head_dim×2 bytes/element×(输入+输出))",
            "vram_inference_tiers": "推理仅两档：最小部署=权重+单请求峰值KV；目标并发部署=权重+单请求峰值KV×目标并发",
            "ideal_inference": "目标并发: 权重显存 + 单请求峰值KV×目标并发 → 除以单卡显存 → 取2幂次方；默认统一显存池不重复加载权重；仅在显式设置单请求吞吐且有实测时叠加吞吐约束",
            "vram_inference_quant": "INT8:1.0×P / INT4:0.5×P (权重)；KV Cache 默认仍为BF16/FP16，即2 bytes/element",
            "compute_train": "总训练FLOPs=6×P_compute×tokens；Dense 的 P_compute=总参数量，MoE 的 P_compute=每token激活参数量；理想卡数=max(最小部署, ceil(总FLOPs÷(TFLOPS×10¹²×MFU×86400×天数))→取2幂次方)",
            "mfu_default": 0.30,
            "mfu_prefer_benchmark": "优先使用 chip_model_benchmarks 表的实测 MFU",
            "inference_throughput_formula": "min(compute_bound, memory_bound) × 0.30 效率因子",
        },
        "vram_factors": {
            "title": "显存计算 — 各因子含义",
            "formula_general": "min_vram_total = 总参数量(B) × bytes_per_param × safety_factor",
            "formula_cards": "raw_cards = max(1, ceil(required_vram / chip_vram_gb))；cards = 向上取最接近的2幂次方（不额外+1）",
            "note_moe": "MoE：激活参数量决定每 token 计算量；默认常驻权重部署仍按总参数量计算显存。专家并行负责跨卡分片，只有显式专家卸载才使用另一套模型。",
            "factors": [
                {"factor": "bytes_per_param", "label": "每参数字节数", "desc": "每个参数占用的显存字节数（含参数本身+梯度+优化器状态+激活值开销）", "values": [
                    {"scenario": "训练·SFT全参", "value": "12 bytes/param", "breakdown": "FP16参数(2) + FP16梯度(2) + Adam m/v(8)；激活值按 batch×seq×hidden×layers 另算"},
                    {"scenario": "训练·CPT", "value": "12 bytes/param", "breakdown": "同SFT全参；激活值单独计算并逐项展示"},
                    {"scenario": "训练·RL(PPO/GRPO)", "value": "25 bytes/param", "breakdown": "Actor(2) + Critic(2) + Ref模型(2) + 优化器(8-12) + 激活(6-8) ≈ 22-30，取25"},
                    {"scenario": "训练·LoRA", "value": "2.5 bytes/param", "breakdown": "冻结基座(2) + LoRA适配器 + 无优化器状态，大幅降低显存需求"},
                    {"scenario": "量化·GPTQ", "value": "3.5 bytes/param", "breakdown": "FP16模型(2) + Hessian矩阵缓冲区(1.5)"},
                    {"scenario": "量化·AWQ", "value": "3.0 bytes/param", "breakdown": "FP16模型(2) + 激活统计缓冲区(1.0)"},
                    {"scenario": "量化·bitsandbytes/GGUF", "value": "2.5 bytes/param", "breakdown": "FP16模型(2) + 量化缓冲区(0.5)"},
                    {"scenario": "推理·FP16", "value": "2.0 bytes/param", "breakdown": "仅模型权重加载，无反向传播"},
                    {"scenario": "推理·INT8", "value": "1.0 bytes/param", "breakdown": "INT8 量化权重 = 1 byte/param"},
                    {"scenario": "推理·INT4/GPTQ/AWQ/GGUF", "value": "0.5 bytes/param", "breakdown": "INT4 量化权重 = 0.5 byte/param"},
                ]},
                {"factor": "safety_factor", "label": "安全系数", "desc": "对模型状态/权重预留碎片化与运行时缓冲；激活值和 KV Cache 另行计算", "values": [
                    {"scenario": "训练场景", "value": "×1.25", "breakdown": "模型状态预留 25%；训练激活值另加"},
                    {"scenario": "推理场景", "value": "×1.25", "breakdown": "常驻权重预留 25%；KV Cache 另加"},
                    {"scenario": "量化场景", "value": "×1.25", "breakdown": "预留 25% 用于校准数据 + 中间缓冲区"},
                ]},
                {"factor": "kv_cache_dtype", "label": "KV Cache 元素精度", "desc": "KV Cache 精度与权重量化是两个独立配置；平台采用多数后端的保守默认值", "values": [
                    {"scenario": "默认", "value": "BF16/FP16 = 2 bytes/element", "breakdown": "每个K或V元素占2字节；INT8/INT4权重量化不会自动把KV也量化"},
                    {"scenario": "可选优化", "value": "FP8 = 1 byte/element", "breakdown": "只有芯片、推理后端与模型显式支持FP8 KV Cache时才能采用，当前推荐表单暂不默认启用"},
                ]},
                {"factor": "card_rounding", "label": "卡数取整规则", "desc": "最终推荐卡数取 2 的幂次方（1/2/4/8/16/32…），确保 NCCL 等集合通信效率最优", "values": [
                    {"scenario": "通用", "value": "ceil → pow2", "breakdown": "先向上取整，再取最近的 ≥ 当前值 的 2 幂次方"},
                ]},
                {"factor": "mfu", "label": "MFU (Model FLOPs Utilization)", "desc": "实际算力利用率，理论峰值算力无法 100% 达到", "values": [
                    {"scenario": "默认值", "value": "0.30 (30%)", "breakdown": "无实测数据时的保守估计，覆盖多数芯片"},
                    {"scenario": "有实测数据", "value": "优先使用 chip_model_benchmarks", "breakdown": "取该芯片在 benchmark 表中的实测 MFU 均值"},
                ]},
            ],
        },
        "total_score_formula": "总分 = 生态成熟度×0.40 + 实测验证度×0.30 + 算力性能×0.20 + 性价比×0.10 (大类权重统一 4:3:2:1)",
        "model_specific_measured_ranking": {
            "model": "deepseek-ai/DeepSeek-V4-Flash",
            "scenario": "inference",
            "reference": "1024输入 + 1024输出 + 1并发",
            "condition": "至少一款候选芯片有同模型、同参考场景、有效卡数及吞吐实测",
            "category_weights_when_available": {
                "benchmark_evidence": 0.60,
                "ecosystem_maturity": 0.20,
                "compute_power": 0.15,
                "cost_effectiveness": 0.05,
            },
            "measured_formula": "单卡吞吐60% + TTFT20% + TPOT20%，各指标在同组实测芯片间归一化",
            "missing_policy": "无同场景实测时实测项统一为中性50分；有实测芯片先于未实测芯片排序",
            "source": "用户提供的推理测试明细数据_20260713（金山文档）；原始行逐条保留溯源",
        },
        "scenario_weights": {
            "训练·SFT全参":   {"ecosystem_maturity": 0.40, "benchmark_evidence": 0.30, "compute_power": 0.20, "cost_effectiveness": 0.10},
            "训练·SFT·LoRA": {"ecosystem_maturity": 0.40, "benchmark_evidence": 0.30, "compute_power": 0.20, "cost_effectiveness": 0.10},
            "训练·CPT":       {"ecosystem_maturity": 0.40, "benchmark_evidence": 0.30, "compute_power": 0.20, "cost_effectiveness": 0.10},
            "训练·RL":        {"ecosystem_maturity": 0.40, "benchmark_evidence": 0.30, "compute_power": 0.20, "cost_effectiveness": 0.10},
            "量化":            {"ecosystem_maturity": 0.40, "benchmark_evidence": 0.30, "compute_power": 0.20, "cost_effectiveness": 0.10},
            "推理·FP16":      {"ecosystem_maturity": 0.40, "benchmark_evidence": 0.30, "compute_power": 0.20, "cost_effectiveness": 0.10},
            "推理·量化":       {"ecosystem_maturity": 0.40, "benchmark_evidence": 0.30, "compute_power": 0.20, "cost_effectiveness": 0.10},
        },
        "scoring_dimensions": DIMENSION_META,
        "hard_constraints": [
            "max_cards: 推荐卡数超过此值 → 排除",
            "min_cards: 推荐卡数低于此值 → 拉高到 min_cards (2幂次方)",
            "max_price: 单价超过此值 → 排除",
        ],
    }




@app.get("/api/v1/chips/{identifier}")
def api_chip_profile(
    identifier: str = Path(..., description="Chip ID (pure digits) or name (fuzzy)",
                           examples=["A100 SXM4 80GB", "H200 SXM 141GB", "昇腾910C"]),
):
    """Full chip profile.  identifier = ID (pure digits) or name (fuzzy)."""
    result = get_chip_profile(identifier)
    if result is None:
        raise HTTPException(404, f"Chip not found: {identifier}")
    return result


@app.post("/api/v1/chips/batch")
def api_chip_batch(
    body: Annotated[BatchRequest, Body(examples=[{"identifiers": ["5", "A100 SXM4 80GB", "Ascend 910B B1 64GB"]}])],
):
    """Batch chip profiles."""
    results = get_chip_profiles_batch(body.identifiers)
    missing = sum(1 for r in results if r is None)
    return {
        "count": len(results),
        "found": len(results) - missing,
        "missing": missing,
        "profiles": [r for r in results if r is not None],
    }


# ═══════════════════════════════════════════════════════════════
# Models
# ═══════════════════════════════════════════════════════════════

@app.get("/api/v1/models")
def api_model_search(
    search: Optional[str] = Query(None, description="Fuzzy search (model_id / author)"),
    architecture: Optional[str] = Query(None, description="dense | moe"),
    params_min: Optional[float] = Query(None, description="Min params (B)"),
    params_max: Optional[float] = Query(None, description="Max params (B)"),
    for_chip: Optional[str] = Query(None, description="Find models compatible with this chip"),
    limit: int = Query(50, le=2000),
    offset: int = Query(0),
    include_provenance: bool = Query(False, description="Include per-model field provenance summary"),
):
    """Search models."""
    filters = ModelFilters(
        search=search, architecture=architecture,
        params_min=params_min, params_max=params_max, for_chip=for_chip,
    )
    return search_models(filters, limit=limit, offset=offset,
                         include_provenance=include_provenance)


@app.get("/api/v1/models/{identifier}")
def api_model_profile(
    identifier: str = Path(..., description="Model ID (pure digits) or name (fuzzy)",
                           examples=["Qwen2.5-7B", "DeepSeek-V3", "1"]),
):
    """Full model profile.  identifier = ID (pure digits) or name (fuzzy)."""
    result = get_model_profile(identifier)
    if result is None:
        raise HTTPException(404, f"Model not found: {identifier}")
    return result


@app.post("/api/v1/models/batch")
def api_model_batch(
    body: Annotated[BatchRequest, Body(examples=[{"identifiers": ["Qwen2.5-7B", "DeepSeek-V3", "Llama-3.1-8B"]}])],
):
    """Batch model profiles."""
    results = get_model_profiles_batch(body.identifiers)
    missing = sum(1 for r in results if r is None)
    return {
        "count": len(results),
        "found": len(results) - missing,
        "missing": missing,
        "profiles": [r for r in results if r is not None],
    }


# ═══════════════════════════════════════════════════════════════
# Benchmarks
# ═══════════════════════════════════════════════════════════════

@app.get("/api/v1/benchmarks")
def api_benchmark_search(
    chip: Optional[str] = Query(None, description="Filter by chip model name"),
    chip_model: Optional[str] = Query(None, description="Alias for 'chip' parameter"),
    model: Optional[str] = Query(None),
    workload: Optional[str] = Query(None, description="inference | training"),
    suite: Optional[str] = Query(None, description="MLPerf | vendor_doc | community"),
    limit: int = Query(50, le=2000),
    offset: int = Query(0),
    include_provenance: bool = Query(False, description="Include per-benchmark field provenance summary"),
):
    """Search benchmark records."""
    effective_chip = chip or chip_model
    filters = BenchmarkFilters(chip=effective_chip, model=model, workload=workload, suite=suite)
    return search_benchmarks(filters, limit=limit, offset=offset,
                             include_provenance=include_provenance)


# ═══════════════════════════════════════════════════════════════
# Compatibility
# ═══════════════════════════════════════════════════════════════

@app.get("/api/v1/compat")
def api_compat_search(
    chip: Optional[str] = Query(None, description="Filter by chip model name"),
    chip_model: Optional[str] = Query(None, description="Alias for 'chip' parameter"),
    model: Optional[str] = Query(None),
    status: Optional[str] = Query(None, description="verified | vendor_claimed | community | unsupported"),
    has_benchmark: bool = Query(False, description="Only return records that have benchmark evidence"),
    limit: int = Query(50, le=2000),
    offset: int = Query(0),
    include_provenance: bool = Query(False, description="Include per-compat field provenance summary"),
):
    """Search compatibility records."""
    effective_chip = chip or chip_model
    filters = CompatFilters(chip=effective_chip, model=model, status=status,
                            has_benchmark=has_benchmark)
    return search_compat(filters, limit=limit, offset=offset,
                         include_provenance=include_provenance)


# ═══════════════════════════════════════════════════════════════
# Provenance
# ═══════════════════════════════════════════════════════════════

@app.get("/api/v1/provenance")
def api_provenance_search(
    table: Optional[str] = Query(None, description="chips | models | chip_model_benchmarks | chip_model_compatibility"),
    row_id: Optional[str] = Query(None),
    field: Optional[str] = Query(None, description="Field name (fuzzy)"),
    source_type: Optional[str] = Query(None),
    confidence: Optional[str] = Query(None, description="high | medium | low"),
    is_official: Optional[str] = Query(None, description="0 | 1"),
    limit: int = Query(50, le=2000),
    offset: int = Query(0),
):
    """Search field-level provenance records."""
    filters = ProvenanceFilters(
        table_name=table, row_id=row_id, field_name=field,
        source_type=source_type, confidence=confidence, is_official=is_official,
    )
    return search_provenance(filters, limit=limit, offset=offset)


@app.get("/api/v1/provenance/stats")
def api_provenance_stats(
    table: Optional[str] = Query(None, description="Filter by table name"),
):
    """Provenance aggregation stats."""
    return get_provenance_stats(table=table)


# ═══════════════════════════════════════════════════════════════
# DB status
# ═══════════════════════════════════════════════════════════════

@app.get("/api/v1/db/status")
def api_db_status():
    """Database info."""
    return {"database": str(get_db_path()), "tables": get_db_stats()}


def _decode_candidate_fields(value: str | None) -> list[dict]:
    try:
        decoded = json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return decoded if isinstance(decoded, list) else []


def _decode_run_counts(value: str | None) -> dict[str, int]:
    try:
        decoded = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    if not isinstance(decoded, dict):
        return {}
    counts: dict[str, int] = {}
    for key, count in decoded.items():
        try:
            counts[str(key)] = int(count)
        except (TypeError, ValueError):
            continue
    return counts


def _source_update_run_row(row) -> dict | None:
    if not row:
        return None
    item = dict(row)
    item["counts"] = _decode_run_counts(item.pop("counts_json", None))
    if item.get("duration_ms") is not None:
        item["duration_ms"] = max(0, int(round(item["duration_ms"])))
    item["business_tables_modified"] = False
    item["sources"] = []
    return item


def _source_check_row(row) -> dict:
    item = dict(row)
    item["candidate_fields"] = _decode_candidate_fields(
        item.pop("candidate_fields_json", None)
    )
    return item


def _source_update_row(row) -> dict:
    item = dict(row)
    item["candidate_fields"] = _decode_candidate_fields(
        item.pop("candidate_fields_json", None)
    )
    item["truncated"] = bool(item.get("truncated"))
    return item


def _source_auto_apply_row(row) -> dict:
    item = dict(row)
    for source_key, target_key, fallback in (
        ("fields_json", "fields", {}),
        ("old_values_json", "old_values", {}),
        ("evidence_json", "evidence", {}),
        ("validation_json", "validation", {}),
    ):
        raw = item.pop(source_key, None)
        try:
            decoded = json.loads(raw or "{}")
        except (TypeError, json.JSONDecodeError):
            decoded = fallback
        item[target_key] = decoded if isinstance(decoded, dict) else fallback
    return item


def _source_run_event_row(row) -> dict:
    item = dict(row)
    raw = item.pop("details_json", None)
    try:
        details = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        details = {}
    item["details"] = details if isinstance(details, dict) else {}
    return item


@app.get("/api/v1/source-updates")
def api_source_updates(
    limit: int = Query(30, ge=1, le=200),
    offset: int = Query(0, ge=0),
    mode: str = Query("formal", pattern="^(formal|test)$"),
):
    """List source-page changes for the system status and review UI."""
    try:
        selected_db = get_db_path() if mode == "formal" else latest_test_db()
        if selected_db is None:
            return {"available": False, "count": 0, "latest_at": None,
                    "latest_run": None, "updates": [], "mode": "test"}
        with get_db(selected_db, readonly=True) as db:
            table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_diffs'"
            ).fetchone()
            if not table:
                return {
                    "available": False,
                    "count": 0,
                    "latest_at": None,
                    "latest_run": None,
                    "updates": [],
                }
            latest_run_row = db.execute(
                "SELECT id, run_type, mode, status, started_at, finished_at, counts_json, "
                "error_summary, "
                "(julianday(finished_at)-julianday(started_at))*86400000 AS duration_ms "
                "FROM update_runs WHERE run_type='source_refresh' "
                + ("AND mode='test' " if mode == "test" else "") +
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
            latest_run = _source_update_run_row(latest_run_row)
            auto_apply_table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_auto_applies'"
            ).fetchone()
            if latest_run:
                latest_run["events"] = []
                event_table = db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='update_run_events'"
                ).fetchone()
                if event_table:
                    event_rows = db.execute(
                        "SELECT id, run_id, link_id, actor, stage, status, message, "
                        "details_json, created_at FROM update_run_events "
                        "WHERE run_id=? ORDER BY id",
                        (latest_run["id"],),
                    ).fetchall()
                    latest_run["events"] = [
                        _source_run_event_row(row) for row in event_rows
                    ]
                source_rows = db.execute(
                    "SELECT c.id AS check_id, c.link_id, c.requested_url, c.final_url, "
                    "c.outcome, c.http_status, c.error_code, c.error_message, c.checked_at, "
                    "c.duration_ms, c.attempt_count, c.response_bytes, c.content_type, "
                    "l.description, l.vendor, l.category, "
                    "d.id AS update_id, d.candidate_fields_json "
                    "FROM source_checks c "
                    "LEFT JOIN link_library l ON l.id=c.link_id "
                    "LEFT JOIN source_diffs d ON d.source_check_id=c.id "
                    "WHERE c.run_id=? ORDER BY c.id",
                    (latest_run["id"],),
                ).fetchall()
                latest_run["sources"] = [_source_check_row(row) for row in source_rows]
                latest_run["auto_applies"] = []
                if auto_apply_table:
                    apply_rows = db.execute(
                        "SELECT a.id, a.source_diff_id, a.chip_id, a.chip_model, a.status, "
                        "a.source_url, a.fields_json, a.old_values_json, a.evidence_json, "
                        "a.validation_json, a.backup_path, a.started_at, a.finished_at, "
                        "a.error_summary FROM source_auto_applies a "
                        "JOIN source_diffs d ON d.id=a.source_diff_id "
                        "WHERE d.run_id=? ORDER BY a.id",
                        (latest_run["id"],),
                    ).fetchall()
                    latest_run["auto_applies"] = [
                        _source_auto_apply_row(row) for row in apply_rows
                    ]
                    latest_run["business_tables_modified"] = any(
                        item.get("status") == "success"
                        for item in latest_run["auto_applies"]
                    )
            total_row = db.execute(
                "SELECT COUNT(*) AS count, MAX(d.created_at) AS latest_at "
                "FROM source_diffs d JOIN update_runs r ON r.id=d.run_id "
                + ("WHERE r.mode='test'" if mode == "test" else "")
            ).fetchone()
            rows = db.execute(
                "SELECT d.id, d.run_id, d.link_id, d.previous_content_hash, "
                "d.content_hash, d.added_lines, d.removed_lines, d.truncated, "
                "d.diff_summary, d.diff_path, d.candidate_fields_json, d.created_at, "
                "c.requested_url, c.final_url, c.http_status, "
                "l.description, l.vendor, l.category "
                "FROM source_diffs d "
                "JOIN update_runs r ON r.id=d.run_id "
                "JOIN source_checks c ON c.id=d.source_check_id "
                "LEFT JOIN link_library l ON l.id=d.link_id "
                + ("WHERE r.mode='test' " if mode == "test" else "") +
                "ORDER BY d.created_at DESC, d.id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            updates = [_source_update_row(row) for row in rows]
            apply_by_diff: dict[int, dict] = {}
            if auto_apply_table and updates:
                placeholders = ",".join("?" for _ in updates)
                apply_rows = db.execute(
                    "SELECT id, source_diff_id, chip_id, chip_model, status, source_url, "
                    "fields_json, old_values_json, evidence_json, validation_json, "
                    "backup_path, started_at, finished_at, error_summary "
                    f"FROM source_auto_applies WHERE source_diff_id IN ({placeholders})",
                    [item["id"] for item in updates],
                ).fetchall()
                apply_by_diff = {
                    int(row["source_diff_id"]): _source_auto_apply_row(row)
                    for row in apply_rows
                }
            for item in updates:
                item["auto_apply"] = apply_by_diff.get(int(item["id"]))
        return {
            "available": True,
            "count": int(total_row["count"] or 0),
            "latest_at": total_row["latest_at"],
            "latest_run": latest_run,
            "updates": updates,
        }
    except sqlite3.OperationalError as exc:
        raise HTTPException(status_code=503, detail=f"数据更新记录暂不可用：{exc}") from exc


@app.get("/api/v1/data-agent/status")
def api_data_agent_status(
    run_id: int | None = Query(None, ge=1),
    limit: int = Query(100, ge=1, le=500),
    mode: str = Query("formal", pattern="^(formal|test)$"),
):
    """Read the latest orchestrator cycle, worker queue and candidate facts."""
    try:
        selected_db = get_db_path() if mode == "formal" else latest_test_db()
        if selected_db is None:
            return {"available": True, "latest_cycle": None, "jobs": [],
                    "discoveries": [], "candidates": [], "inbox": [],
                    "publications": [], "totals": {}, "mode": "test"}
        result = get_data_agent_status(db_path=selected_db, run_id=run_id, limit=limit)
        result["mode"] = mode
        return result
    except sqlite3.OperationalError as exc:
        raise HTTPException(status_code=503, detail=f"数据抓取智能体状态暂不可用：{exc}") from exc


@app.get('/api/v1/data-agent/runs')
def api_data_agent_runs(mode: str = Query('test', pattern='^(formal|test)$'),
                        limit: int = Query(10, ge=1, le=50), offset: int = Query(0, ge=0)):
    from chip_model.pipeline.run_history import list_runs
    return list_runs(get_db_path(), mode=mode, limit=limit, offset=offset)


@app.get('/api/v1/data-agent/runs/{session}/{run_id}')
def api_data_agent_run_detail(session: str, run_id: int = Path(..., ge=1)):
    from chip_model.pipeline.run_history import run_detail
    try:
        return run_detail(get_db_path(), session, run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (OSError, sqlite3.Error) as exc:
        raise HTTPException(status_code=503, detail='运行记录暂不可用') from exc


@app.get('/api/v1/data-agent/open-web-runs')
def api_open_web_test_runs(final_only: bool = False):
    """List isolated open-web test runs; never reads candidate data from the formal DB."""
    return {
        "runs": list_open_web_test_runs(get_db_path(), final_only=final_only)
    }


@app.get('/api/v1/data-agent/open-web-runs/{session_id}')
def api_open_web_test_run_detail(session_id: str):
    try:
        return read_open_web_test_run(get_db_path(), session_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (OSError, sqlite3.Error) as exc:
        raise HTTPException(status_code=503, detail='开放互联网测试记录暂不可用') from exc


class ManualRunRequest(BaseModel):
    mode: str
    limit: int = 5
    pipeline: str = "known_sources"
    skill: str = "chip-specs"
    chips: list[str] = Field(default_factory=list)
    queries: list[str] = Field(default_factory=list)
    target_fields: list[str] = Field(default_factory=list)


class SeedUrlRequest(BaseModel):
    mode: str
    url: str
    description: str
    category: str
    vendor: str = ""
    enabled: bool = True


class FamilySelectionRequest(BaseModel):
    parent_url: str
    child_urls: list[str]


def _require_secure_admin_transport(request: Request) -> None:
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip().lower()
    host = (request.url.hostname or "").lower()
    if request.url.scheme == "https" or forwarded_proto == "https":
        return
    if host in {"localhost", "127.0.0.1", "::1", "testserver"}:
        return
    raise HTTPException(
        status_code=426,
        detail="数据更新管理接口只允许通过 HTTPS 或本机/SSH 隧道访问。",
    )


def _require_data_agent_admin(token: str | None, request: Request) -> None:
    _require_secure_admin_transport(request)
    if not admin_token_valid(token):
        raise HTTPException(status_code=403, detail="需要数据更新管理员口令。")


@app.get("/api/v1/data-agent/seeds")
def api_data_agent_seeds(
    http_request: Request,
    mode: str = Query("formal", pattern="^(formal|test)$"),
    admin_token: str | None = Header(None, alias="X-Data-Agent-Admin"),
):
    _require_data_agent_admin(admin_token, http_request)
    return {"mode": mode, "seeds": list_seed_urls(mode)}


@app.get("/api/v1/data-agent/families")
def api_data_agent_families(
    http_request: Request,
    admin_token: str | None = Header(None, alias="X-Data-Agent-Admin"),
):
    _require_data_agent_admin(admin_token, http_request)
    return {"families": read_chip_families(),
            "selection": read_family_selection()}


@app.post("/api/v1/data-agent/family-selection")
def api_data_agent_family_selection(
    request: FamilySelectionRequest,
    http_request: Request,
    admin_token: str | None = Header(None, alias="X-Data-Agent-Admin"),
):
    _require_data_agent_admin(admin_token, http_request)
    try:
        return save_family_selection(request.parent_url, request.child_urls)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/v1/data-agent/seeds")
def api_data_agent_save_seed(
    request: SeedUrlRequest,
    http_request: Request,
    admin_token: str | None = Header(None, alias="X-Data-Agent-Admin"),
):
    _require_data_agent_admin(admin_token, http_request)
    try:
        return save_seed_url(
            request.mode, request.url, request.description, request.category,
            request.vendor, enabled=request.enabled,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/v1/data-agent/manual-run")
async def api_data_agent_manual_run(
    http_request: Request,
    admin_token: str | None = Header(None, alias="X-Data-Agent-Admin"),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
):
    # Authentication precedes normalization and any persistent start. The raw
    # object avoids legacy defaults leaking into V3 requests.
    _require_secure_admin_transport(http_request)
    authenticated = admin_token_valid(admin_token)
    if not authenticated:
        # Keep the separate known_sources legacy endpoint semantics.
        raw = await http_request.body()
        try:
            hint = json.loads(raw)
        except ValueError:
            hint = {}
        legacy = isinstance(hint, dict) and hint.get('pipeline', 'known_sources') == 'known_sources' and not any(
            k in hint for k in ('domain', 'chip', 'request_id', 'resume_run_id', 'skill', 'chips', 'queries', 'target_fields')) and idempotency_key is None
        raise HTTPException(status_code=403 if legacy else 401, detail="需要数据更新管理员口令。")
    try:
        payload = await http_request.json()
        if not isinstance(payload, dict):
            raise ValueError('启动请求必须为对象')
        is_v3 = payload.get('pipeline') != 'known_sources' if 'pipeline' in payload else False
        is_v3 = is_v3 or idempotency_key is not None or any(
            k in payload for k in ('domain', 'chip', 'request_id', 'resume_run_id', 'skill', 'chips', 'queries', 'target_fields'))
        if is_v3:
            from chip_model.pipeline.manual_control import queue_domain_run, DomainStartConflict
            try:
                receipt = queue_domain_run(payload, idempotency_key=idempotency_key)
            except DomainStartConflict as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            return Response(content=json.dumps(receipt, ensure_ascii=False), media_type='application/json', status_code=202)
        # Existing known-source queue is independent; no V3 input reaches it.
        request = ManualRunRequest.model_validate(payload)
        return queue_manual_run(request.mode, request.limit, pipeline=request.pipeline,
            skill=request.skill, chips=request.chips, queries=request.queries, target_fields=request.target_fields)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (OSError, sqlite3.Error) as exc:
        raise HTTPException(status_code=503, detail=f'持久运行队列不可用：{exc}') from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/v1/data-agent/manual-run")
def api_data_agent_manual_run_result(
    mode: str = Query("formal", pattern="^(formal|test)$"),
):
    return {"mode": mode, "latest_request": latest_manual_result(mode)}


@app.get("/api/v1/source-updates/{update_id}")
def api_source_update_detail(
    update_id: int = Path(..., ge=1),
    mode: str = Query("formal", pattern="^(formal|test)$"),
):
    """Return one bounded diff; only files under the project snapshot root are read."""
    try:
        selected_db = get_db_path() if mode == "formal" else latest_test_db()
        if selected_db is None:
            raise HTTPException(status_code=404, detail="尚未运行测试巡检。")
        with get_db(selected_db, readonly=True) as db:
            row = db.execute(
                "SELECT d.*, c.requested_url, c.final_url, c.http_status, "
                "l.description, l.vendor, l.category "
                "FROM source_diffs d "
                "JOIN update_runs r ON r.id=d.run_id "
                "JOIN source_checks c ON c.id=d.source_check_id "
                "LEFT JOIN link_library l ON l.id=d.link_id WHERE d.id=? "
                + ("AND r.mode='test'" if mode == "test" else ""),
                (update_id,),
            ).fetchone()
    except sqlite3.OperationalError as exc:
        raise HTTPException(status_code=503, detail=f"数据更新记录暂不可用：{exc}") from exc
    if not row:
        raise HTTPException(status_code=404, detail="数据更新记录不存在")

    item = _source_update_row(row)
    item["diff_text"] = None
    diff_value = item.get("diff_path")
    if diff_value:
        diff_path = FilePath(str(diff_value)).resolve()
        allowed_root = ((get_project_root() / "data") if mode == "formal" else selected_db.parent) / "source_snapshots"
        allowed_root = allowed_root.resolve()
        try:
            diff_path.relative_to(allowed_root)
        except ValueError:
            item["diff_read_error"] = "差异文件不在允许读取的快照目录中"
        else:
            try:
                item["diff_text"] = diff_path.read_text(encoding="utf-8")[:20_500]
            except OSError as exc:
                item["diff_read_error"] = f"无法读取差异文件：{exc}"
    return item


# ═══════════════════════════════════════════════════════════════
# Health
# ═══════════════════════════════════════════════════════════════

@app.api_route("/api/v1/health", methods=["GET", "HEAD"])
def api_health():
    return {"status": "ok", "version": "0.4.0"}


# ═══════════════════════════════════════════════════════════════
# Static files — must be last so API routes take priority
# ═══════════════════════════════════════════════════════════════

import os as _os
_static_dir = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "static")


@app.get("/chips", include_in_schema=False)
@app.get("/models", include_in_schema=False)
@app.get("/recommend", include_in_schema=False)
@app.get("/methodology", include_in_schema=False)
@app.get("/vram-logic", include_in_schema=False)
@app.get("/compat", include_in_schema=False)
@app.get("/status", include_in_schema=False)
async def spa_tab_entry():
    """Serve the SPA shell for clean, refresh-safe tab URLs."""
    return FileResponse(_os.path.join(_static_dir, "index.html"))


if _os.path.isdir(_static_dir):
    app.mount("/", StaticFiles(directory=_static_dir, html=True), name="static")


# ═══════════════════════════════════════════════════════════════
# Entrypoint
# ═══════════════════════════════════════════════════════════════

def run_server(db_path: str | None = None, host: str = "0.0.0.0", port: int = 8000):
    """Launch uvicorn.  Called from cli.py --server or directly."""
    import uvicorn
    if db_path:
        set_db_path(db_path)
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="AISHPerf FastAPI Server")
    ap.add_argument("--db-path", default=None, help="SQLite database path")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    run_server(db_path=args.db_path, host=args.host, port=args.port)

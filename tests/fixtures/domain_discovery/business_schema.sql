-- Schema-only fixture extracted read-only from data/data.db on 2026-10-08.
-- Actual snapshot DDL; contains no business rows. Base schema.sql lacks the
-- benchmark citation, evidence_level, methodology, reference_url columns.

CREATE TABLE chips (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,

    -- 标识
    vendor          TEXT,
    vendor_display  TEXT,
    vendor_region   TEXT,
    chip_series     TEXT,
    chip_model      TEXT,
    chip_type       TEXT,
    usage           TEXT,
    tier            TEXT,

    -- 架构
    architecture    TEXT,
    arch_codename   TEXT,
    generation      TEXT,
    process_node_nm TEXT,
    foundry         TEXT,
    die_size_mm2    TEXT,
    transistors_b   TEXT,
    package_type    TEXT,
    is_chiplet      TEXT,

    -- 显存
    vram_gb        TEXT,
    vram_type      TEXT,
    vram_bus_bit   TEXT,
    vram_bw_gb_s   TEXT,
    vram_clock_mhz TEXT,

    -- 计算单元
    compute_units TEXT,
    tensor_cores  TEXT,
    rt_cores      TEXT,
    shading_units TEXT,
    sm_count      TEXT,

    -- 缓存
    l1_cache_kb     TEXT,
    l2_cache_mb     TEXT,
    on_chip_sram_mb TEXT,

    -- 精度（枚举，如 "FP32, FP16, BF16, FP8, INT8, INT4"）
    precision_support TEXT,   -- 硬件支持的精度列表
    precision_perf   TEXT,   -- 各精度对应算力，如 "FP16=312TF, BF16=624TF, FP8=1248TF, INT8=624TOPS"

    -- 频率
    base_clock_mhz  TEXT,
    boost_clock_mhz TEXT,

    -- 功耗与物理
    tdp_w           TEXT,
    max_power_w     TEXT,
    psu_w           TEXT,
    power_connector TEXT,
    board_length_mm TEXT,
    board_width_mm  TEXT,
    slot_width      TEXT,
    form_factor     TEXT,
    bus_interface   TEXT,

    -- 互联
    interconnect_bw_gb_s TEXT,
    interconnect_tech    TEXT,
    network_interface    TEXT,

    -- 软件生态
    software_stack        TEXT,
    compatible_frameworks TEXT,

    -- 生命周期 & 发布状态
    release_date      TEXT,
    production_status TEXT,   -- 已量产 / 已发布 / EOL ...（已发布芯片）
    eol_date          TEXT,
    target_market     TEXT,
    is_released       TEXT,   -- 0=待发布/传闻中，1=已发布/量产（合并了 upcoming_chips）
    expected_release_date TEXT,  -- 预计发布时间（待发布芯片用）
    known_specs       TEXT,   -- 已确认的规格（待发布芯片用）
    unconfirmed_items TEXT,   -- 尚未确认的信息（待发布芯片用）

    -- 价格（合并了 chip_price）
    price_usd     TEXT,
    price_cny_wan TEXT,
    price_period  TEXT,   -- 价格对应的时间/时期
    price_notes   TEXT,   -- 价格说明

    -- 芯片介绍 & 生态评估
    description           TEXT,   -- 芯片概述（一段话）
    highlights            TEXT,   -- 核心亮点
    limitations           TEXT,   -- 已知局限/短板
    target_workloads      TEXT,   -- 适合场景：训练 / 推理 / 边缘 / HPC
    typical_deployment    TEXT,   -- 典型部署形态：单卡 / 8卡 / 集群 / 云端
    competitor_comparison TEXT,   -- 与竞品的对比说明

    -- 生态评估（原 chip_ecosystem 合并进来）
    ecosystem_notes  TEXT,   -- 生态成熟度详细说明
    maturity_level   TEXT,   -- 生态成熟度评分（0-5）
    framework_compat TEXT,   -- 兼容框架列表
    sw_stack         TEXT,   -- 推荐软件栈
    cuda_compat      TEXT,   -- CUDA 兼容程度
    cloud_available  TEXT,   -- 0/1，是否云上可用
    cluster_scale    TEXT,   -- 已知集群规模
    key_strength     TEXT,   -- 生态核心优势
    key_weakness     TEXT,   -- 生态核心短板

    -- 时间戳
    created_at TEXT,
    updated_at TEXT
, deployment_links TEXT);

CREATE TABLE chip_model_compatibility (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    chip_model    TEXT,
    model_id      TEXT,

    compat_status TEXT,   -- verified / vendor_claimed / community / unknown / unsupported
    framework     TEXT,
    precision     TEXT,
    verified_at   TEXT,
    notes         TEXT,
    created_at    TEXT
);

CREATE TABLE chip_model_benchmarks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    chip_model TEXT,
    model_id   TEXT,

    -- 测试套件
    suite_name TEXT,

    -- 测试条件
    workload_type    TEXT,
    scenario         TEXT,
    task             TEXT,
    hardware_config  TEXT,
    chip_count       TEXT,
    framework        TEXT,
    precision        TEXT,
    batch_size       TEXT,
    input_seq_length  TEXT,
    output_seq_length TEXT,
    concurrency      TEXT,

    -- 推理指标（Prefill / Decode 拆分）
    prefill_throughput     TEXT,
    decode_throughput      TEXT,
    time_to_first_token_ms TEXT,
    inter_token_latency_ms TEXT,
    memory_peak_mb         TEXT,
    throughput_tok_s       TEXT,
    throughput_samples_s   TEXT,
    tpot_ms               TEXT,

    -- 训练指标
    mfu_pct              TEXT,
    gpu_hours            TEXT,
    training_tokens_T    TEXT,
    training_gpu_count   TEXT,
    training_workload_type TEXT,   -- pretrain / SFT / LoRA / full_finetune ...

    -- 其他
    test_date  TEXT,
    notes      TEXT,
    created_at TEXT
, source_type TEXT, source_url TEXT, citation TEXT, evidence_level TEXT, methodology TEXT, reference_url TEXT, confidence TEXT);

CREATE TABLE deployment_guides (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            chip_model    TEXT,
            model_id      TEXT,
            url           TEXT NOT NULL,
            title         TEXT,
            source_type   TEXT,
            notes         TEXT,
            created_at    TEXT,
            updated_at    TEXT
        , backend TEXT);

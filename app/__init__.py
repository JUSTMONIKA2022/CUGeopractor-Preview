# -*- coding: utf-8 -*-
"""行至大地·CUGeopractor 应用包入口。

模块分层约定（低技术债务、模块化）：
    config   配置加载（.env / 环境变量 / 默认值）
    secrets  密钥本机加密存储（API Key 不落明文）
    llm      LLM 适配层（OpenAI 兼容协议，多厂商）
    rag      知识库（导入 / 向量检索 / 引用）
    agent    对话主循环 / 工具注册表 / 会话记忆
    web      本地 Web UI（默认 127.0.0.1）
    cli      命令行入口（Web/CLI/Docker 三形态共用本包）
"""

__version__ = "0.1.0"

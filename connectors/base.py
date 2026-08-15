# -*- coding: utf-8 -*-
"""连接器（Connector）协议占位。

说明：
    - 连接器是"校园系统能力接入层"（课表/场馆/教务/一卡通等），按已确认决策，
      MVP 阶段【不开发】任何具体连接器；
    - 本文件只定义统一协议（接口规范），保证第二阶段接入时不破坏核心架构；
    - 设计红线（对应可行性报告 §5.3）：
        1) 连接器只允许使用"用户本人合法登录后的会话/凭据"；
        2) 不得内置绕过认证、批量抓取他人数据的逻辑；
        3) 连接器默认关闭，需用户显式启用。
"""

from __future__ import annotations

from abc import ABC, abstractmethod


def tool_error(source: str, reason: str) -> str:
    """统一错误输出格式：`[错误] 来源: 原因`。

    所有连接器失败时统一使用本函数返回，便于 LLM 理解来源、用户排查。
    """
    return f"[错误] {source}: {reason}"


def tool_info(source: str, message: str) -> str:
    """统一信息输出格式：`[信息] 来源: 说明`。

    所有连接器"无结果/降级说明"类提示统一使用本函数返回。
    """
    return f"[信息] {source}: {message}"


class BaseConnector(ABC):
    """连接器抽象基类（第二阶段实现的具体连接器必须继承本类）。

    子类实现约定：
        - name 为唯一注册名；
        - enabled 默认 False，必须由用户在配置中显式开启；
        - invoke 中不得内置任何绕过认证/爬取他人数据的逻辑。
    """

    # 连接器唯一名称（如 "course_table"、"library"）
    name: str = ""

    # 连接器说明（展示给用户的用途描述）
    description: str = ""

    def __init__(self, enabled: bool = False) -> None:
        # 默认关闭：对应"校园连接器默认关闭"的安全决策
        self.enabled = enabled

    @abstractmethod
    def invoke(self, query: str) -> str:
        """执行一次连接器查询，返回文本结果。

        参数：
            query: 用户输入（与具体连接器解析规则相关）
        返回：
            查询结果文本；失败时返回以 [错误] 开头的提示。
        """
        raise NotImplementedError

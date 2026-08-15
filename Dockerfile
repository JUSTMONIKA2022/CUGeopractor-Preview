# 行至大地·CUGeopractor —— 镜像构建
# 说明：多阶段设计未启用（MVP 精简），使用 slim 基础镜像控制体积；
#      运行用户为非 root，降低容器内被利用后的影响面（对应安全基线沙箱诉求）。

FROM python:3.13-slim

# 设置工作目录与 Python 环境（不产生 .pyc，避免污染卷挂载）
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# 先复制依赖清单以利用 Docker 层缓存，再复制源码
COPY pyproject.toml README.md ./
COPY app ./app
COPY connectors ./connectors

# 安装项目依赖（含运行时依赖；chromadb 相关较重，构建时间较长属正常）
RUN pip install --no-cache-dir .

# 创建非 root 运行用户（安全基线：最小权限）
RUN useradd --create-home --uid 1000 cugeopractor
# 数据目录由容器用户拥有（挂载卷时权限可控）
RUN mkdir -p /app/data && chown -R cugeopractor:cugeopractor /app

USER cugeopractor

# 默认仅暴露 Web UI 端口（对外绑定由 docker-compose 控制在 127.0.0.1）
EXPOSE 8080

# 默认命令：启动本地 Web UI（等价于 cugeopractor serve）
CMD ["cugeopractor", "serve"]

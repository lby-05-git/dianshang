# 后端 API 镜像（构建上下文为项目根目录）。
# 评测中心首次一键运行会读取 eval_refund Golden 数据集，必须一并打包。
FROM python:3.12-slim

WORKDIR /app

# rapidocr(onnxruntime)/opencv 运行库（libxcb/libGL 为 opencv 依赖，slim 镜像缺失）
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    libgl1 \
    libglib2.0-0 \
    libxcb1 \
    libxkbcommon0 \
    fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt

COPY backend/ ./
COPY eval_refund/ ./eval_refund/

COPY backend/docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh
ENTRYPOINT ["docker-entrypoint.sh"]

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

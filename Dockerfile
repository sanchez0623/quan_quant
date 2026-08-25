# ---- 前端构建 ----
FROM node:20-alpine AS frontend-build
WORKDIR /build
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ---- 运行镜像（FastAPI + 前端静态文件单容器）----
FROM python:3.11-slim
WORKDIR /app

# 依赖层（代码改动不重装）
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

# 应用代码与前端产物
COPY backend/ /app/backend/
COPY --from=frontend-build /build/dist /app/frontend/dist
COPY config.example/ /app/config.example/

EXPOSE 8000
WORKDIR /app/backend
CMD ["python", "run.py"]

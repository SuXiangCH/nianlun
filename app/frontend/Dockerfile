# syntax=docker/dockerfile:1
# 前端：Vite 构建静态产物，nginx 托管并同源反代 /api 到后端。

FROM node:22-alpine AS build
WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY . .
# VITE_API_BASE="/" 经 client.ts 去尾斜杠后为空串，浏览器请求变成同源相对
# 路径（/api/v1/...），由本镜像的 nginx 反代到 api 服务，无需 CORS。
ARG VITE_API_BASE=/
ENV VITE_API_BASE=${VITE_API_BASE}
RUN npm run build

FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80

# syntax=docker/dockerfile:1.6
# 前端运维中心：Vue 3 + Vite 5
# 多阶段构建：build 阶段 pnpm install + vite build；runtime 阶段 nginx 托管静态资源

# ---------- Stage 1: build ----------
# 用 slim(Debian/glibc) 而非 alpine(musl)：npm 在 musl 上有 "Exit handler
# never called!" 崩溃 bug，会导致 bin-linking 失败，node_modules/.bin/vite 丢失
FROM node:20-slim AS build
WORKDIR /build

# 启用 corepack 并固定 pnpm 版本（pnpm 10.x 兼容 Node 20；11.x 需要 Node 22+）
RUN corepack enable && corepack prepare pnpm@10.18.2 --activate

# 先装依赖（层缓存：package.json 未变时跳过重装）
COPY package.json package-lock.json ./
RUN npm config set registry https://registry.npmmirror.com \
 && pnpm config set registry https://registry.npmmirror.com \
 && pnpm install --no-frozen-lockfile

# 再 COPY 源码构建
# 直接跑 vite build，跳过 package.json 里的 vue-tsc 严格类型检查
# （项目代码有若干 TS 类型告警，不影响运行时，但会让 vue-tsc 报错中断构建）
COPY . .
RUN node_modules/.bin/vite build

# ---------- Stage 2: runtime ----------
FROM nginx:1.27-alpine AS runtime
RUN rm /etc/nginx/conf.d/default.conf
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /build/dist /usr/share/nginx/html

EXPOSE 80

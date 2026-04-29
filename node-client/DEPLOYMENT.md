# 客户端分发指南

本文档介绍如何将 claude-remote-agent 客户端分发给其他用户使用。

---

## 方案 1：发布到 npm（推荐给开发者）

你的 `package.json` 已经配置好了发布基础。

### 发布步骤：

```bash
# 1. 登录 npm 账号
npm login

# 2. 发布公开包
npm publish --access public
```

### 用户安装使用：

```bash
# 全局安装
npm install -g claude-remote-agent

# 运行
claude-remote-agent
```

> ⚠️ **注意**：包名 `claude-remote-agent` 可能已被占用，发布前请先检查 npm 上是否存在同名包，如已存在需要修改为唯一名称。

---

## 方案 2：打包成单文件可执行文件（推荐给普通用户）⭐

使用 `pkg` 将 Node.js 程序打包成独立的可执行文件，用户不需要安装 Node.js 环境即可运行。

### 打包步骤：

```bash
# 1. 安装 pkg 工具
npm install -g pkg

# 2. 在项目根目录执行（先确保已 npm run build）
pkg . --output claude-remote-agent.exe

# 3. 跨平台打包（同时生成 Windows/Linux/macOS 版本）
pkg . --targets node18-win-x64,node18-linux-x64,node18-macos-x64
```

### 用户使用：

直接将生成的 `.exe` 文件发送给用户，用户双击即可运行。

> 💡 提示：如果希望缩小体积，可以使用 `upx` 进一步压缩可执行文件。

---

## 方案 3：压缩包分发（最简单）

适合有一定技术基础的用户使用。

### 打包步骤：

```bash
# 1. 构建项目
npm run build

# 2. 创建发布目录
mkdir -p release
cp -r dist/ release/
cp package.json release/
cp .env.example release/

# 3. 安装生产依赖
cd release
npm install --production
cd ..

# 4. 打包成 zip
# Windows: 右键 release 文件夹 -> 发送到 -> 压缩(zipped)文件夹
# Linux/macOS: zip -r claude-remote-agent.zip release/
```

### 用户使用：

```bash
# 1. 解压 zip 文件
cd claude-remote-agent

# 2. 复制配置文件（如果需要自定义）
cp .env.example .env
# 编辑 .env 配置服务器地址等参数

# 3. 运行
node dist/main.js
```

---

## 方案 4：Docker 镜像（容器化部署）

适合服务器环境部署。

### 步骤 1：创建 `Dockerfile`

在 `node-client/` 目录下创建 `Dockerfile`：

```dockerfile
FROM node:18-alpine

WORKDIR /app

# 安装依赖
COPY package*.json ./
RUN npm install --production

# 复制构建产物
COPY dist/ ./dist/
COPY .env.example ./

# 暴露端口（如果需要）
# EXPOSE 8080

# 启动命令
CMD ["node", "dist/main.js"]
```

### 步骤 2：构建并推送镜像

```bash
# 构建镜像
docker build -t yourname/claude-remote-agent .

# 推送到 Docker Hub（需要先登录）
docker login
docker push yourname/claude-remote-agent
```

### 用户使用：

```bash
# 拉取镜像
docker pull yourname/claude-remote-agent

# 运行（通过环境变量配置）
docker run -e SERVER_URL=ws://your-server:8000/ws/client \
           -e AGENT_TOKEN=your-token \
           -e CLIENT_ID=default \
           yourname/claude-remote-agent

# 或者使用 .env 文件
docker run --env-file .env yourname/claude-remote-agent
```

---

## 方案对比

| 方案 | 适用场景 | 优点 | 缺点 |
|------|---------|------|------|
| **npm 发布** | 技术人员/开发者 | 安装简单、版本管理方便 | 需要 Node.js 环境 |
| **单文件 exe** | 非技术 Windows 用户 | 零依赖、双击运行 | 体积较大 (~50MB) |
| **压缩包** | 有基础的技术用户 | 灵活、可定制 | 需要 Node.js 环境 |
| **Docker** | 服务器部署 | 环境一致、易于扩展 | 需要 Docker 环境 |

---

## 📌 推荐方案

- **给非技术 Windows 用户** → 方案 2（单 exe 文件）
- **给开发者/技术人员** → 方案 1（npm）或方案 3（压缩包）
- **服务器部署** → 方案 4（Docker）

---

## 发布前检查清单

- [ ] 代码已通过 `npm run build` 成功编译
- [ ] `.env.example` 包含所有必需的配置项说明
- [ ] `package.json` 中的 `version` 已更新
- [ ] 测试过在干净环境下的运行情况
- [ ] README.md 或快速使用指南已准备好

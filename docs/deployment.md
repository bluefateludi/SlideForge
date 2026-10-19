# Ubuntu 单机生产部署

适用于腾讯云 Ubuntu 2 核 4GB 等单机环境。使用 Docker Engine、Compose 与 Buildx 插件，服务器不需要额外安装 Python 或 Node。Docker 安装参考 [官方 Ubuntu 文档](https://docs.docker.com/engine/install/ubuntu/)。先确认 `sudo docker compose version`、`sudo docker buildx version` 可用，并执行 `sudo systemctl enable --now docker`。

下文在仓库根目录执行，示例路径为 `/workspace/ai-ppt-generator`。代码需先提交到服务器可以获取的仓库。不要把本地 `.venv`、`node_modules` 或密钥打包到镜像。

## 配置

首次部署时复制 `backend/.env.example` 为 `backend/.env`，设置：

- `APP_ENV=production`。
- `LLM_API_KEY` 及实际可用的模型名称、接口地址。
- `JWT_SECRET`：用 `openssl rand -hex 32` 生成的随机值。

在仓库根目录 `.env` 中设置独立的 `POSTGRES_PASSWORD`（建议 `openssl rand -hex 24`，避免 URL 保留字符），并将两个环境文件权限设为 `600`。已有文件不要重复覆盖；已有数据库不能仅修改这个变量来轮换数据库密码。Compose 会注入内部数据库、Redis 和文件存储地址。

每次登录后的快捷函数：

```bash
cd /workspace/ai-ppt-generator
dc() { sudo docker compose -f docker-compose.prod.yml "$@"; }
```

此前若使用 `docker-compose.cloud.yml`，可继续在函数中加入 `-f docker-compose.cloud.yml`；本次所需的重启、日志、并发和共享存储设置已经纳入生产配置。检查旧文件没有覆盖新镜像或恢复 `uv run` 启动命令。保持原 Compose 项目名（默认由目录名确定），否则会连接到另一组命名卷。

```bash
dc config --quiet
```

## 前端打包

首次部署或前端更新时执行。保留同一份 Linux `node_modules` 可以利用 npm 下载缓存，但不要直接复用 Windows 的依赖目录。

```bash
sudo docker run --rm --memory=2g \
  -e NODE_OPTIONS=--max-old-space-size=1536 \
  -v "$PWD:/workspace" -w /workspace/frontend \
  node:24-bookworm-slim sh -c 'npm ci && npm run build'
test -f frontend/dist/index.html
```

前端仍采用挂载 `frontend/dist` 的方式交付，本次没有改成多阶段前端镜像，也没有新增 npm 下载缓存配置。

## 后端构建与首次启动

先执行 `git rev-parse --short HEAD` 获取提交标识，然后用 `sudo nano .env` 在根目录配置中加入或更新 `SLIDEFORGE_IMAGE=slideforge-backend:<提交标识>`。保留已有的 `POSTGRES_PASSWORD`，不要覆盖整个环境文件。所有发布命令沿用上面的 `dc` 函数，Compose 会读取根目录 `.env`；不要依赖会被 sudo 丢弃的 shell 环境变量。

```bash
# Worker 不再单独 build；这里只构建一次。
dc build api
dc up -d --wait postgres redis
dc run --rm --no-deps api alembic upgrade head
dc up -d --no-build
dc ps
curl -fsS http://127.0.0.1:39880/api/v1/health
```

build、迁移和 up 必须使用同一标签。以后登录继续使用根目录 `.env` 中的镜像标签，不要意外退回默认 `local` 标签；每次发布更新这个标签。

健康结果应为 `status`、`database`、`redis` 全部 `ok`。在腾讯云防火墙中仅放通需要的来源到 TCP 39880；Postgres / Redis 没有公网端口映射。浏览器访问 `http://服务器IP:39880`，验证注册、文档上传、生成大纲、生成页面及 PPTX 导出。正式提供账号和文档服务前配置域名与 HTTPS。

## 从旧镜像升级

先备份数据库及上传文件，确认当前服务使用的卷和存储路径。原版镜像未持久化的容器内文件，需要在替换容器前单独导出；新的数据卷不会自动找到这些旧文件。

新镜像使用 UID / GID `10001:10001`。如果现有共享卷由 root 写入，先构建新镜像，再暂停写入、迁移权限和数据库：

```bash
dc stop api worker
dc run --rm --no-deps --user 0 --entrypoint chown api \
  -R 10001:10001 /data/storage
dc run --rm --no-deps api alembic upgrade head
dc up -d --no-build
```

权限命令仅作用于该项目的 `/data/storage` 挂载卷。不要递归修改整个仓库、宿主机根目录或数据库卷的所有权。新建的存储卷会继承镜像中目录的用户权限，一般无需这一步。

## 构建缓存与字体

- 不改文件再次 `dc build api`，依赖和字体层应显示 `CACHED`。
- 只改业务源码，第三方依赖及字体层仍可复用，项目安装步骤会重跑。
- 修改 `pyproject.toml` 后要提交更新的 `uv.lock`；不一致时构建失败。
- 依赖层失效时，BuildKit 挂载的 uv 缓存可减少重复下载，但换服务器、清理缓存或换版本仍需下载。
- 固定 Python / uv 版本和 digest；定期主动升级并验证，不依赖 `latest`。
- 字体来自 `backend/scripts/fonts.json` 的固定提交，SHA-256 验证、30 秒超时及最多 3 次尝试；默认缺字体就构建失败。字体下载受网络状况影响。
- 明确接受缺字体估算时，才使用 `dc build --build-arg ALLOW_MISSING_FONTS=true api`。这是降级镜像，构建日志会标明，不能作为正常字体验收通过。

首次构建约 6 分钟并不反常。用 `dc --progress plain build api` 收集逐层耗时，分别比较首次构建、无修改重建、只改源码重建。没有实测前不承诺具体耗时；前端构建单独计时。

## 自动重启与维护

所有长期服务配置 `restart: unless-stopped`。Docker 开机启动后，服务器重启或容器进程退出可自动恢复；手动停止的容器保持停止。该策略不会因为健康检查失败就重启仍存活的进程，也不会自动发布代码更新。

启动命令直接使用镜像内 `uvicorn` / `arq`，迁移直接调用 `alembic`，不执行依赖同步。每个构建同步步骤使用 `--no-dev`，镜像还设置 `UV_NO_DEV=1`；运行依赖及其传递依赖保留，pytest / Ruff 等开发组不安装。

```bash
dc logs -f --tail=100 api worker nginx
sudo docker system df
```

更新代码后按“构建 → 必要的权限迁移 → 数据库迁移 → up”顺序发布，Compose 会替换镜像已变化的容器，无需逐个手动重启进程。前端或 Nginx 配置单独变化时执行 `dc restart nginx`，确保新文件挂载及配置生效。不要使用 `down -v` 清理缓存，它会删除业务数据卷。镜像回滚不等于数据库回滚，应另外评估迁移兼容性。

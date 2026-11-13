#!/usr/bin/env bash
# 部署 20261113：场景指令 + 在线医生本地直连（不走 AI_WebApi）
# 在服务器 hc202@192.168.8.160 上执行，需 sudo/docker 权限。
set -euo pipefail

TAG="meta_agent:20261113-scene-doctor-local"
BASE=/home/program/ai_metaAgent
SRC_TGZ="$BASE/metaAgent-codex-src-20261113-flat.tar.gz"
BUILD="$BASE/build-20261113"
RELEASE="$BASE/release-5080-codex"

[ -f "$SRC_TGZ" ] || { echo "缺少源码包 $SRC_TGZ，先 scp 上传"; exit 1; }

# 1) 解压构建目录（flat 包，解到空目录即源码树）
rm -rf "$BUILD"
mkdir -p "$BUILD"
tar -xzf "$SRC_TGZ" -C "$BUILD"

# 2) 构建镜像（Dockerfile 默认走阿里云 PyPI）
docker build -t "$TAG" "$BUILD"

# 3) 备份生产 compose 并切换镜像行
cd "$RELEASE"
cp -a compose.yaml "compose.yaml.bak.$(date +%m%d-%H%M)"
sed -i "s|image: *meta_agent:[a-zA-Z0-9._-]*|image: $TAG|" compose.yaml
echo "---- compose image 行 ----"
grep -n "image:" compose.yaml

# 4) 检查医生本地直连所需账号（webyun 登录，在线医生功能依赖）
echo "---- agent.env 医生/场景配置检查 ----"
grep -E "META_AGENT__WEBYUN_LOGIN_(PHONE|PASSWORD)" agent.env \
  || echo "WARN: agent.env 缺 META_AGENT__WEBYUN_LOGIN_PHONE/PASSWORD，在线医生直连会 401"
grep -E "META_AGENT__SCENE_LOCAL_ENABLED|META_AGENT__DOCTORS_LOCAL_ENABLED" agent.env \
  || echo "INFO: 未设置本地化开关，默认均为 true（本地直连生效）"

# 5) 重建容器（预期输出 Recreate meta_agent_codex）
docker compose -p agent-codex --env-file agent.env up -d --force-recreate

# 6) 验证
sleep 10
echo "---- 容器状态 ----"
docker ps --filter name=meta_agent_codex --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'
echo "---- 健康检查 ----"
curl -sS http://127.0.0.1:5080/health/ready && echo
echo "---- 场景直连冒烟（拉点位库版本）----"
docker exec meta_agent_codex python -c "
import asyncio, httpx
from meta_agent.domains.scene_local import DEFAULT_VERSION_URL, DEFAULT_API_KEY, DEFAULT_API_TOKEN
async def main():
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(DEFAULT_VERSION_URL, json={}, headers={'apikey': DEFAULT_API_KEY, 'apitoken': DEFAULT_API_TOKEN})
        r.raise_for_status()
        data = r.json()
        print('scene upstream OK, payload head:', str(data)[:200])
asyncio.run(main())
"
echo "---- 尾部日志 ----"
docker logs --tail 15 meta_agent_codex

echo
echo "部署完成。回滚方式："
echo "  1) 代码级回滚（保留镜像，仅关功能）: agent.env 加 META_AGENT__SCENE_LOCAL_ENABLED=false / META_AGENT__DOCTORS_LOCAL_ENABLED=false 后重新 up -d --force-recreate"
echo "  2) 镜像级回滚: sed -i 's|image: *meta_agent:[a-zA-Z0-9._-]*|image: meta_agent:20261112-postfix|' $RELEASE/compose.yaml 后重新 up"

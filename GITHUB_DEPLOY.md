# 从 GitHub 仓库部署到 /mnt/data/zht

仓库创建为 `jiuyueQ/qwen-collector-proxy` 后，可以通过当前账号的 SSH 或 GitHub CLI 身份在服务器拉取。私有仓库需要认证；代码、部署文档和测试可入库，`collector.env`、采集数据和 Conda 环境已通过 `.gitignore` 排除。

代理项目：`/mnt/data/zht/qwen-collector-proxy`。模型目录保持 `/mnt/data/models/Qwen3.8-27B`，不需要移动模型。

## HTTPS 拉取

仓库已设为 Public 时，可直接执行以下命令；如果仓库仍为 Private，需要先配置 GitHub 身份，或使用后面的 SSH/CLI 方式：

```bash
mkdir -p /mnt/data/zht
git clone https://github.com/jiuyueQ/qwen-collector-proxy.git /mnt/data/zht/qwen-collector-proxy
cd /mnt/data/zht/qwen-collector-proxy

conda create -p "$PWD/.conda-env" python=3.12 -y
conda activate "$PWD/.conda-env"
python -m pip install -r requirements.txt
cp -n collector.env.example collector.env
chmod 600 collector.env
bash start_proxy.sh
```

## SSH 拉取

前提：服务器已有可访问该私有仓库的 SSH 身份。使用已有的账号 SSH key，或为该仓库配置只读 deploy key；不要把私钥复制进项目目录。

```bash
mkdir -p /mnt/data/zht
cd /mnt/data/zht
git clone git@github.com:jiuyueQ/qwen-collector-proxy.git
cd /mnt/data/zht/qwen-collector-proxy

conda create -p "$PWD/.conda-env" python=3.12 -y
conda activate "$PWD/.conda-env"
python -m pip install -r requirements.txt

cp -n collector.env.example collector.env
chmod 600 collector.env
bash start_proxy.sh
```

## GitHub CLI 拉取

服务器已安装 GitHub CLI 时，可先完成自己的交互登录，再拉取私有仓库：

```bash
gh auth login --hostname github.com --git-protocol https --web
gh repo clone jiuyueQ/qwen-collector-proxy /mnt/data/zht/qwen-collector-proxy
cd /mnt/data/zht/qwen-collector-proxy
```

登录验证由你自己完成，认证信息不应填写在项目文件或带凭据的 clone URL 中。拉取后的环境安装与启动命令同上。

## 后续更新

当前版本的 Qwen 消息兼容、原始请求与转发请求保存规则，以及 **nohup 更新重启命令**见 [CHAT_COMPAT.md](CHAT_COMPAT.md)。已有 nohup 部署可执行：

```bash
(
  set -e
  cd /mnt/data/zht/qwen-collector-proxy
  git pull --ff-only origin main
  bash restart_proxy.sh
)
```

以下命令用于 systemd 用户服务部署。

已有本地 `collector.env` 和 `data/` 不在仓库内。停止代理后更新代码，再启动：

```bash
cd /mnt/data/zht/qwen-collector-proxy
systemctl --user stop qwen-collector
git pull --ff-only
conda activate "$PWD/.conda-env"
python -m pip install -r requirements.txt
systemctl --user restart qwen-collector
```

手动运行用户在原终端 Ctrl+C 后执行 pull，最后运行 `bash start_proxy.sh`。如果该路径已存在旧版解压文件，先在别处备份目录，不要直接向已有目录 clone 或覆盖配置。

Responses 与 `previous_response_id` 的上游设置、两轮测试和完整数据导出见 [RESPONSES.md](RESPONSES.md)。

## 已有旧目录的部署

如果之前部署在 `/mnt/data/qwen-collector-proxy`，在新目录重新拉取代码并创建新的 Conda 环境，避免直接移动带有绝对路径的旧环境。若复用了旧 `collector.env`，将其中的 `COLLECTOR_DATA_DIR` 改成 `/mnt/data/zht/qwen-collector-proxy/data`。

旧采集数据需要保留时，先停止旧代理，再复制到新 data 目录。随后重新安装新版 `qwen-collector.service` 到用户 systemd 目录，执行 `systemctl --user daemon-reload` 后启动。模型服务继续读取原模型目录。

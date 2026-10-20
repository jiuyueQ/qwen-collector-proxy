# 从 GitHub 私有仓库部署到 /mnt/data

仓库创建为 `jiuyueQ/qwen-collector-proxy` 后，可以通过当前账号的 SSH 或 GitHub CLI 身份在服务器拉取。私有仓库需要认证；代码、部署文档和测试可入库，`collector.env`、采集数据和 Conda 环境已通过 `.gitignore` 排除。

## SSH 拉取

前提：服务器已有可访问该私有仓库的 SSH 身份。使用已有的账号 SSH key，或为该仓库配置只读 deploy key；不要把私钥复制进项目目录。

```bash
cd /mnt/data
git clone git@github.com:jiuyueQ/qwen-collector-proxy.git
cd /mnt/data/qwen-collector-proxy

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
gh repo clone jiuyueQ/qwen-collector-proxy /mnt/data/qwen-collector-proxy
cd /mnt/data/qwen-collector-proxy
```

登录验证由你自己完成，认证信息不应填写在项目文件或带凭据的 clone URL 中。拉取后的环境安装与启动命令同上。

## 后续更新

已有本地 `collector.env` 和 `data/` 不在仓库内。停止代理后更新代码，再启动：

```bash
cd /mnt/data/qwen-collector-proxy
systemctl --user stop qwen-collector
git pull --ff-only
conda activate "$PWD/.conda-env"
python -m pip install -r requirements.txt
systemctl --user restart qwen-collector
```

手动运行用户在原终端 Ctrl+C 后执行 pull，最后运行 `bash start_proxy.sh`。如果该路径已存在旧版解压文件，先在别处备份目录，不要直接向已有目录 clone 或覆盖配置。

Responses 与 `previous_response_id` 的上游设置、两轮测试和完整数据导出见 [RESPONSES.md](RESPONSES.md)。

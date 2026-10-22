# homelab 分支

[dgongut/docker-controller-bot](https://github.com/dgongut/docker-controller-bot) 的个人 fork，在上游版本之上加了几个插件。**上游文件一个都不改**，所有改动都在新增文件里，合并上游新版本时不会冲突。

| 文件 | 作用 |
| :--- | :--- |
| `homelab/main.py` | 镜像入口：把上游的 `docker-controller-bot.py` 当模块加载（命令和回调就注册好了），再装插件、启动 |
| `homelab/plugins.py` | 把插件装到上游的 bot 上，并挪到上游兜底处理函数的前面；命令菜单里加 `/status` |
| `homelab/docker_updates.py` | 经 Portainer 查新、按钮确认后更新容器，保留回退容器 2 天，清理构建缓存（每天 03:30 由外部触发 `--scan`） |
| `homelab/n8n_status.py` | `/status`：转发给 n8n 的主机状态报告 |
| `homelab/self_update.py` | 机器人自己的更新（见下） |
| `homelab/paths.py` | 插件文件放在 bot 的数据卷里（5.x 是 `/app/config`） |
| `Dockerfile.homelab` | 用本地源码构建（上游 Dockerfile 是下载发布包）；`TELEMETRY=0`；可选 `APK_MIRROR` / `PIP_INDEX_URL` |
| `tests/test_homelab.py` | 插件测试，单独运行，不改上游的 `tests/run_all.py` |

容器更新的 Telegram 通知：`telegram()` 遇到断连、超时、429/5xx 会再试两次；「正在更新」「已更新」「已清理」和各项「未完成」汇总这类只告知结果的消息走 `tell()`，发不出去只记日志，不改变更新或清理的状态。2026-10-22 rsshub 的自动更新其实成功了，只是那条通知赶上连接断开，被标成 failed、报了「更新未完成」，回退容器也不会被清理；这类记录（failed 但带 `applied` / `new_id`）的回退容器现在照常在 2 天后清理。

## 更新流程

1. **CI**（`.github/workflows/homelab.yml`）：每次推送 `homelab` 跑上游的全部测试和插件测试、试构建镜像，通过后打 `homelab-v<上游版本>-h<提交数>` 标签。每天 01:17 UTC 先尝试合并上游最新的正式版本；合并冲突或测试不过就开一个 `upstream-sync` issue，不推送。
2. **机器人**（`self_update.py`）：每 6 小时看一次这些标签，有更新的就在自己所在的 Docker 主机上构建，构建好后在 Telegram 发「部署 / 忽略此版本」按钮。`upstream-sync` issue 也会提醒一次。
3. **部署**：点「部署」后经 Portainer 把 bot 的 Stack 换成新镜像；新 bot 启动后报告「已更新」。没起来的话，外部的看门狗 5 分钟后换回旧镜像，旧 bot 报告「已回滚」。

插件配置都在数据卷里，不在仓库中：`portainer-multihost.json`（Portainer 地址和密钥）、`portainer-cert.pem`、`n8n-status.json`、`self-update.json`（Stack 编号和构建镜像源）。

```bash
python3 tests/run_all.py && python3 tests/test_homelab.py
docker build -f Dockerfile.homelab --build-arg VERSION=dev -t docker-controller-bot:dev .
```

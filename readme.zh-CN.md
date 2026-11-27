[toc]

# prom textfile

[English](readme.md) | 中文

获取本机里所有exporter 监控数据写入到本地的node exporter textfile目录下，prometheus获取一次就可以拿到所有的机器的全部监控。

本脚本使用python3默认模块，无需安装python其他模块。需要 Linux 和 Python 3.8 及以上；RHEL 8、openSUSE Leap 15 默认的 `python3` 是 3.6，请安装发行版自带的 `python3.11`（RHEL 8）/ `python311`（Leap 15）包，并用 `python3.11` 运行。

可以使用supervisor或systemd来启动或管理进程。也可以定时任务

*使用 supervisor 记得开启node exporter 监控supervisor参数。*

配置文件支持多文件，文件内多配置

# run

```sh
python3 prom-textfile.py -c /etc/prom-textfile -p /var/lib/prometheus/node-exporter --debug
```

| 参数 | 默认值 | 说明 |
|---|---|---|
| `-c, --config_path` | `/etc/prom-textfile` | 单个配置文件，或配置目录（读取其中所有 `*.ini`） |
| `-p, --prometheus_node_exporter_textfile_path` | `/var/lib/prometheus/node-exporter` | node exporter textfile 目录 |
| `-s, --state_path` | `/var/lib/prom-textfile/state` | cron 模式的运行计数文件目录 |
| `-b, --buffer_path` | `/var/lib/prom-textfile/buffer` | remote write 缓存目录 |
| `--daemon` | | 常驻运行，按 `interval` 循环；不加则跑一次退出（cron 模式） |
| `--debug` | | debug 日志 |

# config

`*.ini` 中每个 section 是一个 job，输出文件名为 `<配置文件名>_<section>.prom`。

```ini
[node]
name : node
url : http://localhost:9100/metrics
interval : 30
```

| 配置项 | 说明 |
|---|---|
| `name` | job 名称，作为 label `prom_cronjob_name` |
| `url` | exporter 的 metrics 地址 |
| `interval` | 抓取间隔（秒，daemon 模式） |
| `prometheus_url` | 可选，remote write 地址，见 [remote write](#remote-write) |
| `labels` | 可选，自定义 label；与抓到的同名 label 冲突时原值保留为 `exported_<name>`，见 [自定义 label](#自定义-label) |
| `labels_overwrite` | 可选，自定义 label；与抓到的同名 label 冲突时直接覆盖，见 [自定义 label](#自定义-label) |

`[DEFAULT]` 中的配置对同一文件里的所有 job 生效，`[DEFAULT]` 本身不是 job。

## 自定义 label

给 job 的所有指标加 label，包括 `prom_textfile_job_up`、`prom_textfile_job_count`，本地 textfile 和 remote write 都生效。`prom_textfile_remote_write_*` 状态指标和 `prom-textfile.prom` 不加。

有两个配置项，区别只在抓到的指标已有同名 label 时：

| 配置项 | 抓到 `env="dev"`，配置 `env=prod` |
|---|---|
| `labels` | `env="prod",exported_env="dev"`：合并，保留原值，与 prometheus `honor_labels: false` 相同 |
| `labels_overwrite` | `env="prod"`：强制覆盖，丢弃原值 |

```ini
[DEFAULT]
# 这个文件里的所有 job
labels : env=prod,team=ops
labels_overwrite : dc=sh

[node]
name : node
url : http://localhost:9100/metrics
interval : 30
# 抓到 env="dev",dc="bj"，结果 env="prod",exported_env="dev",dc="sh",team="ops"

[mysql]
name : mysql
url : http://localhost:9104/metrics
interval : 30
# 和 [DEFAULT] 按名字合并：env=staging、team="db,ops"、dc=sh（覆盖）
labels : env=staging,team="db,ops"

[redis]
name : redis
url : http://localhost:9121/metrics
interval : 30
# 空值去掉 [DEFAULT] 里的 label：只剩 env=prod,team=ops
labels_overwrite : dc=
```

- 格式：`name=value`，逗号分隔；值含逗号或 `"` 时用双引号：`team="db,ops"`、`q="say \"hi\""`。
- 两个配置项都按 label 名合并，后面的优先：`[DEFAULT] labels` < `[DEFAULT] labels_overwrite` < job `labels` < job `labels_overwrite`。空值表示这个 job 去掉该 label，不管它来自哪个配置项。
- label 名必须匹配 `[a-zA-Z_][a-zA-Z0-9_]*`，`__` 和 `prom_cronjob_` 开头的是保留名。不合法的项记录错误后忽略，job 照常运行。
- 用 `labels_overwrite` 覆盖用来区分 series 的 label（如 `cpu`、`mode`）要小心：不同 series 会变成重复的，node exporter 会拒绝这个文件。

# remote write

配置中含有 `prometheus_url` 的 job，抓到的数据不写 textfile 目录，而是通过 [Prometheus Remote Write 1.0](https://prometheus.io/docs/specs/prw/remote_write_spec/) 直接写到远端 prometheus（protobuf + snappy 用标准库实现，无需额外依赖）。也可写入 Thanos / Mimir / VictoriaMetrics 等支持 remote write 的服务。

```ini
[node]
name : node
url : http://localhost:9100/metrics
interval : 30
prometheus_url : http://prometheus.example.com:9090/api/v1/write
# 可选认证，同时配置时 bearer token 优先
prometheus_username : user
prometheus_password : pass
prometheus_bearer_token : token
```

远端 prometheus 需要开启 remote write 接收：

```sh
prometheus --web.enable-remote-write-receiver ...
```

## 缓存与补传

每次抓取的数据先写到 `<buffer_path>/<prom_file_name>/<抓取时间毫秒>.prom`，再按时间从旧到新上传，上传成功后删除：

| 远端返回 | 处理 |
|---|---|
| 2xx | 删除缓存文件 |
| 400 | 数据本身被拒绝（如超出时间范围），移到 `rejected/`，继续上传下一个 |
| 其他错误、网络不可用 | 保留缓存，本轮停止，下个周期从最旧的开始补传 |

- 每个 job 最多缓存 24 小时、500MB（`buffer` 和 `rejected/` 分别计算），超出删除最旧的数据。
- 每个 job 的缓存目录有文件锁，cron 模式下前一次补传还没结束时，后一次只写缓存不上传。
- prometheus 默认只接受最近约 1~2 小时内的样本，断网超过这个时间的数据补传会被拒绝（400，进入 `rejected/`）。需要补传更久的数据，在远端 prometheus 配置：

```yaml
storage:
  tsdb:
    out_of_order_time_window: 24h
```

## 状态监控

remote write 状态写到本地 textfile `<prom_file_name>_remote_write.prom`，远端不可用时也能通过 node exporter 告警：

| 指标 | 说明 |
|---|---|
| `prom_textfile_remote_write_up` | 最近一次上传是否成功（1/0） |
| `prom_textfile_remote_write_buffer_files` | 等待上传的缓存文件数 |
| `prom_textfile_remote_write_rejected_files` | 被远端拒绝的文件数 |

# test

单元测试，仅依赖 python3 标准库：

```sh
python3 -m unittest discover -s tests -v
```

集成测试，会下载 node_exporter（校验 sha256）并启动两个实例，验证 cron/daemon 两种模式的输出能被 textfile collector 正常读取（`node_textfile_scrape_error 0`）。需要 linux amd64、bash、curl，占用端口 19100/19101：

```sh
tests/integration.sh
# 指定 node_exporter 版本，默认 1.9.1
NODE_EXPORTER_VERSION=1.9.1 tests/integration.sh
```

remote write 集成测试，会下载 prometheus（校验 sha256），先在 prometheus 未启动时缓存数据，再启动 prometheus 验证补传和写入的数据。同时覆盖认证：无认证、basic auth（另起一个带 `--web.config.file` 的 prometheus）、bearer token（prometheus 本身不支持 bearer token，用一个小代理放在前面）。每种情况先用错误凭据（401，数据留在缓存），再用正确凭据（补传）。需要 `htpasswd`（apache2-utils / httpd-tools），占用端口 19090/19091/19092/19102：

```sh
tests/integration_remote_write.sh
# 指定 prometheus 版本，默认 3.5.0
PROMETHEUS_VERSION=3.5.0 tests/integration_remote_write.sh
```

root 场景测试（只在 CI 跑：需要免密 sudo，会写 `/var/lib/prom-textfile` 并设置 `fs.protected_regular=2`）：安装脚本后用 sudo 运行，`-c` 指向单个配置文件，`/tmp` 下留有普通用户创建的旧 `/tmp/prom-text-self.txt`：

```sh
tests/root_scenario.sh
```

# CI

GitHub Actions 配置在 `.github/workflows/`：

| workflow | 触发 | 内容 |
|---|---|---|
| `test.yml` | push 到非 main 分支 | 最新正式版 Python 语法检查 + 单元测试 |
| `full.yml` | 所有 pull request、push 到 main、手动触发（workflow_dispatch） | 按 Python 版本和 Linux 发行版分别跑单元测试（见下表），全部通过后跑 node_exporter 和 remote write 集成测试 |

`full.yml` 测试矩阵：

| 维度 | 覆盖 |
|---|---|
| Python 版本 | 3.8 到最新正式版的每个小版本（`actions/setup-python`）。版本列表在运行时从 setup-python 的 [versions manifest](https://github.com/actions/python-versions) 读取，新的正式版可用后自动加入测试；目前为 3.8–3.15 |
| Linux 发行版 | Ubuntu 22.04 / 24.04、Debian 12 / 13、RHEL UBI 8 / 9 / 10、Fedora latest、Amazon Linux 2023、openSUSE Leap 15.6 / Tumbleweed |

发行版测试在容器中运行，使用发行版自带的 `python3`（UBI 8 和 Leap 15.6 使用 `python3.11`）。

来自 fork 的 pull request，首次运行 workflow 需要仓库维护者在 PR 页面批准。

# add default job label

`{prom_cronjob_interval="interval",prom_cronjob_name="job name",prom_cronjob_url="url"}`

# 增加监控项

| 指标 | 说明 |
|---|---|
| `prom_textfile_job_up` | 最近一次抓取是否成功（1/0） |
| `prom_textfile_job_count` | job 运行次数 |

# install

## install prom-textfile

```sh
sudo mkdir -p /etc/prom-textfile
sudo curl -L 'https://raw.githubusercontent.com/yaule/prom-textfile/main/prom-textfile.py' -o /usr/bin/prom-textfile
sudo chmod +x /usr/bin/prom-textfile
```

## systemd config scripts

### install

```sh
sudo tee /usr/lib/systemd/system/prom-textfile.service<<-EOF
[Unit]
Description=prom-textfile
Documentation=man:prom-textfile
Documentation=https://github.com/yaule/prom-textfile
[Service]
ExecStart=/usr/bin/prom-textfile --daemon
[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl restart prom-textfile
sudo systemctl enable prom-textfile
```

### remove

```sh
sudo systemctl stop prom-textfile
sudo systemctl disable prom-textfile
sudo rm -rf /etc/prom-textfile /usr/bin/prom-textfile /usr/lib/systemd/system/prom-textfile.service
sudo systemctl daemon-reload
```

## supervisor
### install

```sh
sudo mkdir -p /etc/prom-textfile
sudo apt install supervisor -y
# supervisord config
sudo tee /etc/supervisor/supervisord.conf<<-EOF
[unix_http_server]
file=/var/run/supervisor.sock   ; (the path to the socket file)
chmod=0700                       ; sockef file mode (default 0700)
[inet_http_server]
port=localhost:9001
[supervisord]
logfile=/var/log/supervisor/supervisord.log ; (main log file;default $CWD/supervisord.log)
pidfile=/var/run/supervisord.pid ; (supervisord pidfile;default supervisord.pid)
childlogdir=/var/log/supervisor            ; ('AUTO' child log dir, default $TEMP)
[rpcinterface:supervisor]
supervisor.rpcinterface_factory = supervisor.rpcinterface:make_main_rpcinterface
[supervisorctl]
serverurl=unix:///var/run/supervisor.sock ; use a unix:// URL  for a unix socket
[include]
files = /etc/supervisor/conf.d/*.conf
EOF

sudo systemctl start supervisor
sudo systemctl enable supervisor

# supervisor prom-textfile config
sudo tee /etc/supervisor/conf.d/prom-textfile.conf <<-EOF
[program:prom-textfile]
command=/usr/bin/prom-textfile --daemon
process_name=%(program_name)s
numprocs=1
minfds = 10000
minprocs = 10000
umask=022
autostart=true
startsecs=10
startretries=3
exitcodes=0
stopsignal=TERM
stopwaitsecs=10
stopasgroup=false
killasgroup=false
user=root
EOF
sudo supervisorctl reload
# enable prometheus node exporter supervisor
sudo tee /etc/default/prometheus-node-exporter <<-EOF
ARGS="--collector.supervisord"
EOF
sudo systemctl restart prometheus-node-exporter
```
### remove

```sh
sudo rm -rf /etc/prom-textfile
sudo rm -f /etc/supervisor/conf.d/prom-textfile.conf
sudo supervisorctl reload
# remove supervisor
sudo apt remove supervisor -y
```

## cron job

### install

```sh
sudo tee /usr/lib/systemd/system/prom-textfile.timer <<-EOF
[Unit]
Description=Run prom-textfile every 1 minute
[Timer]
OnBootSec=0
OnUnitActiveSec=1min
[Install]
WantedBy=timers.target
EOF
sudo tee /usr/lib/systemd/system/prom-textfile.service <<-EOF
[Unit]
Description=Collect textfile metrics for prometheus-node-exporter
[Service]
Type=oneshot
ExecStart=/bin/bash -c "/usr/bin/prom-textfile"
EOF
sudo systemctl daemon-reload
sudo systemctl enable prom-textfile.timer
sudo systemctl start prom-textfile.service prom-textfile.timer
```

### remove

```sh
sudo systemctl disable prom-textfile.timer prom-textfile.service
sudo systemctl stop prom-textfile.service prom-textfile.timer
sudo rm -f /usr/lib/systemd/system/prom-textfile.timer
sudo rm -f /usr/lib/systemd/system/prom-textfile.service
sudo systemctl daemon-reload
```

# 授权协议

[GNU General Public License v3.0](LICENSE)

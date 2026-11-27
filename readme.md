[toc]

# prom textfile

English | [中文](readme.zh-CN.md)

Scrape every exporter on the local host and write the metrics into the node exporter textfile directory, so Prometheus gets all metrics of a machine from a single scrape. Jobs can also push their metrics directly to a remote Prometheus via remote write.

The script only uses the Python 3 standard library; no extra modules are needed. Requires Python 3.8+ on Linux. On RHEL 8 and openSUSE Leap 15, where the default `python3` is 3.6, install the distro's `python3.11` (RHEL 8) / `python311` (Leap 15) package and run the script with `python3.11`.

Run it as a daemon under supervisor or systemd, or periodically as a cron job / systemd timer.

*When using supervisor, remember to enable the supervisord collector in node exporter.*

Multiple config files are supported, and each file can contain multiple jobs.

# run

```sh
python3 prom-textfile.py -c /etc/prom-textfile -p /var/lib/prometheus/node-exporter --debug
```

| Option | Default | Description |
|---|---|---|
| `-c, --config_path` | `/etc/prom-textfile` | A config file, or a directory whose `*.ini` files are all loaded |
| `-p, --prometheus_node_exporter_textfile_path` | `/var/lib/prometheus/node-exporter` | node exporter textfile directory |
| `-s, --state_path` | `/var/lib/prom-textfile/state` | Run counter files for cron mode |
| `-b, --buffer_path` | `/var/lib/prom-textfile/buffer` | Remote write buffer directory |
| `--daemon` | | Keep running and scrape every `interval`; without it, run once and exit (cron mode) |
| `--debug` | | Debug logging |

# config

Each section in an `*.ini` file is one job. The output file is named `<config file name>_<section>.prom`.

```ini
[node]
name : node
url : http://localhost:9100/metrics
interval : 30
```

| Key | Description |
|---|---|
| `name` | Job name, added as label `prom_cronjob_name` |
| `url` | Exporter metrics URL |
| `interval` | Scrape interval in seconds (daemon mode) |
| `prometheus_url` | Optional, remote write URL, see [remote write](#remote-write) |
| `labels` | Optional, custom labels; on a clash the scraped label is kept as `exported_<name>`, see [custom labels](#custom-labels) |
| `labels_overwrite` | Optional, custom labels that overwrite a scraped label with the same name, see [custom labels](#custom-labels) |

Keys in a `[DEFAULT]` section apply to every job in the same file. `[DEFAULT]` itself is not a job.

## custom labels

Add labels to every metric of a job, including `prom_textfile_job_up` and `prom_textfile_job_count`, both in the local textfile and in remote write. The `prom_textfile_remote_write_*` status metrics and `prom-textfile.prom` are not changed.

There are two keys. They differ only when the scraped metric already has a label with the same name:

| Key | Scraped `env="dev"`, config `env=prod` |
|---|---|
| `labels` | `env="prod",exported_env="dev"`: merged, the scraped value is kept, same as Prometheus with `honor_labels: false` |
| `labels_overwrite` | `env="prod"`: overwritten, the scraped value is dropped |

```ini
[DEFAULT]
# every job in this file
labels : env=prod,team=ops
labels_overwrite : dc=sh

[node]
name : node
url : http://localhost:9100/metrics
interval : 30
# scraped env="dev",dc="bj" becomes env="prod",exported_env="dev",dc="sh",team="ops"

[mysql]
name : mysql
url : http://localhost:9104/metrics
interval : 30
# merged with [DEFAULT] by name: env=staging, team="db,ops", dc=sh (overwrite)
labels : env=staging,team="db,ops"

[redis]
name : redis
url : http://localhost:9121/metrics
interval : 30
# an empty value removes a [DEFAULT] label: only env=prod,team=ops
labels_overwrite : dc=
```

- Format: `name=value` separated by commas. Quote a value that contains a comma or `"`: `team="db,ops"`, `q="say \"hi\""`.
- Both keys are merged by label name, later wins: `[DEFAULT] labels` < `[DEFAULT] labels_overwrite` < job `labels` < job `labels_overwrite`. An empty value removes that label for the job, whichever key set it.
- Label names must match `[a-zA-Z_][a-zA-Z0-9_]*`. Names starting with `__` or `prom_cronjob_` are reserved. Invalid entries are logged as errors and ignored; the job keeps running.
- Be careful with `labels_overwrite` on a label that tells series apart (e.g. `cpu`, `mode`): different series then become duplicates and node exporter rejects the file.

# remote write

Jobs with `prometheus_url` do not write to the textfile directory. Their metrics are sent directly to a remote Prometheus using [Prometheus Remote Write 1.0](https://prometheus.io/docs/specs/prw/remote_write_spec/). protobuf and snappy are implemented with the standard library, so no extra dependencies are needed. Any service that accepts remote write works, such as Thanos, Mimir or VictoriaMetrics.

```ini
[node]
name : node
url : http://localhost:9100/metrics
interval : 30
prometheus_url : http://prometheus.example.com:9090/api/v1/write
# optional auth; bearer token wins when both are set
prometheus_username : user
prometheus_password : pass
prometheus_bearer_token : token
```

The remote Prometheus must enable the remote write receiver:

```sh
prometheus --web.enable-remote-write-receiver ...
```

## buffer and backfill

Each scrape is first written to `<buffer_path>/<prom_file_name>/<scrape time in ms>.prom`, then uploaded from oldest to newest and deleted after a successful upload:

| Remote response | Action |
|---|---|
| 2xx | Delete the buffered file |
| 400 | The data itself was rejected (e.g. out of bounds); move it to `rejected/` and continue with the next file |
| Other errors, network down | Keep the buffer and stop this round; the next cycle backfills starting from the oldest file |

- Each job buffers at most 24 hours and 500MB (counted separately for the buffer and `rejected/`); beyond that the oldest data is dropped.
- Each job's buffer directory is protected by a file lock. In cron mode, if the previous run is still backfilling, the next run only writes to the buffer and skips uploading.
- By default Prometheus only accepts samples from roughly the last 1–2 hours. Backfilled data from a longer outage is rejected (400, moved to `rejected/`). To backfill older data, configure the remote Prometheus:

```yaml
storage:
  tsdb:
    out_of_order_time_window: 24h
```

## status metrics

Remote write status is written to the local textfile `<prom_file_name>_remote_write.prom`, so you can still alert through node exporter when the remote is unavailable:

| Metric | Description |
|---|---|
| `prom_textfile_remote_write_up` | Whether the last upload succeeded (1/0) |
| `prom_textfile_remote_write_buffer_files` | Number of buffered files waiting for upload |
| `prom_textfile_remote_write_rejected_files` | Number of files rejected by the remote |

# test

Unit tests, Python 3 standard library only:

```sh
python3 -m unittest discover -s tests -v
```

Integration test: downloads node_exporter (sha256 verified), starts two instances, and checks that the output of both cron and daemon mode is read by the textfile collector without errors (`node_textfile_scrape_error 0`). Requires linux amd64, bash and curl, and uses ports 19100/19101:

```sh
tests/integration.sh
# node_exporter version, default 1.9.1
NODE_EXPORTER_VERSION=1.9.1 tests/integration.sh
```

Remote write integration test: downloads Prometheus (sha256 verified), buffers data while Prometheus is not running, then starts Prometheus and checks the backfill and the written data. It also covers authentication: no auth, basic auth (a second Prometheus with `--web.config.file`) and bearer token (a small proxy in front of Prometheus, since Prometheus has no built-in bearer token auth). Each case first runs with wrong credentials (401, data stays buffered), then with correct ones (backfilled). Requires `htpasswd` (apache2-utils / httpd-tools) and uses ports 19090/19091/19092/19102:

```sh
tests/integration_remote_write.sh
# prometheus version, default 3.5.0
PROMETHEUS_VERSION=3.5.0 tests/integration_remote_write.sh
```

Root scenario test (CI only: needs passwordless sudo, writes `/var/lib/prom-textfile` and sets `fs.protected_regular=2`): installs the script, runs it with sudo and `-c` pointing at a single config file, with a stale `/tmp/prom-text-self.txt` owned by a regular user:

```sh
tests/root_scenario.sh
```

# CI

GitHub Actions workflows are in `.github/workflows/`:

| Workflow | Trigger | Runs |
|---|---|---|
| `test.yml` | Push to non-main branches | Syntax check + unit tests on the latest stable Python |
| `full.yml` | Every pull request, push to main, manual (workflow_dispatch) | Unit tests on each Python version and each Linux distribution (below), then the node_exporter and remote write integration tests |

`full.yml` test matrix:

| Dimension | Coverage |
|---|---|
| Python versions | Every minor version from 3.8 to the latest stable release (`actions/setup-python`). The list is read at run time from setup-python's [versions manifest](https://github.com/actions/python-versions), so a new release is tested as soon as it is available; currently 3.8–3.15 |
| Linux distributions | Ubuntu 22.04 / 24.04, Debian 12 / 13, RHEL UBI 8 / 9 / 10, Fedora latest, Amazon Linux 2023, openSUSE Leap 15.6 / Tumbleweed |

Distribution tests run in containers with the distribution's own packaged `python3` (`python3.11` on UBI 8 and Leap 15.6).

For pull requests from forks, a maintainer has to approve the first workflow run on the PR page.

# default job labels

`{prom_cronjob_interval="interval",prom_cronjob_name="job name",prom_cronjob_url="url"}`

# extra metrics

| Metric | Description |
|---|---|
| `prom_textfile_job_up` | Whether the last scrape of the job succeeded (1/0) |
| `prom_textfile_job_count` | Number of runs of the job |

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

# license

[GNU General Public License v3.0](LICENSE)

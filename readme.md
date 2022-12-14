[toc]

# prom textfile

获取本机里所有exporter 监控数据写入到本地的node exporter textfile目录下，prometheus获取一次就可以拿到所有的机器的全部监控。

本脚本使用python3默认模块，无需安装python其他模块。

可以使用supervisor或systemd来启动或管理进程。也可以定时任务

*使用uspervisor记得开启node exporter 监控supervisor参数。*

配置文件支持多文件，文件内多配置

# run

```sh
python3 prom-textfile.py -c /etc/prometheus-node-exporter-cronjob -p /var/lib/prometheus/node-exporter --debug
```

# add default job label

`{prom_cronjob_interval="interval",prom_cronjob_name="job name",prom_cronjob_url="url"}`

# 增加监控项

prom_textfile_job_up

prom_textfile_job_count
# install

## install prom-textfile

```sh
sudo mkdir -p /etc/prometheus-node-exporter-cronjob
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
sudo rm -rf /etc/prometheus-node-exporter-cronjob /usr/bin/prom-textfile /usr/lib/systemd/system/prom-textfile.service
sudo systemctl daemon-reload
```

## supervisor
### install

```sh
sudo mkdir -p /etc/prometheus-node-exporter-cronjob
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
sudo rm -rf /etc/prometheus-node-exporter-cronjob
sudo rm -f /etc/supervisor/conf.d/prom-textfile.conf
sudo supervisorctl reload
# remove supervisor
sudo apt remove supervisor -y
```

## cron job

### install

```sh
sudo tee /usr/lib/systemd/system/prometheus-node-exporter-apt.timer <<-EOF
[Unit]
Description=Run prom-textfile every 1 minute
[Timer]
OnBootSec=0
OnUnitActiveSec=1min
[Install]
WantedBy=timers.target
EOF
sudo tee /usr/lib/systemd/system/prometheus-node-exporter-apt.service <<-EOF
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
sudo rm -f /usr/lib/systemd/system/prometheus-node-exporter-apt.timer
sudo rm -f /usr/lib/systemd/system/prometheus-node-exporter-apt.service
sudo systemctl daemon-reload
```


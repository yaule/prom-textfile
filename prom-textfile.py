#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only

import argparse
import asyncio
import base64
import configparser
import fcntl
import glob
import logging
import re
import struct
import time
import urllib.error
import urllib.request
import sys
import datetime
import os

DEFAULT_BUFFER_PATH = '/var/lib/prom-textfile/buffer'
# cron 模式的运行计数文件目录，不放 /tmp：固定文件名在 /tmp 下会被其他用户抢先创建或替换成符号链接
DEFAULT_STATE_PATH = '/var/lib/prom-textfile/state'
# 每个 job 的缓存上限，超出后删除最旧的数据
BUFFER_MAX_AGE = 24 * 3600 * 1000  # ms
BUFFER_MAX_SIZE = 500 * 1024 * 1024  # bytes

# prometheus 文本格式的一行样本：name{labels} value [timestamp]
SAMPLE_RE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)\s*(?:\{(.*)\})?\s+([^\s]+)(?:\s+(-?\d+))?\s*$')
LABEL_RE = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*"((?:[^"\\]|\\.)*)"')
LABEL_NAME_RE = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]*$')
# 配置里的 labels：name=value，用逗号分隔；值含逗号时用双引号
CUSTOM_LABEL_RE = re.compile(r'\s*([^=,\s]+)\s*=\s*("(?:[^"\\]|\\.)*"|[^,"]*?)\s*(?:,|$)')
# labels：与抓到的同名 label 冲突时原值改名为 exported_<name>；labels_overwrite：直接覆盖
CUSTOM_LABEL_KEYS = (('labels', 'exported'), ('labels_overwrite', 'overwrite'))


class ColorFormatter(logging.Formatter):
    """Logging Formatter to add colors and count warning / errors"""

    grey = "\x1b[90m"
    green = "\x1b[92m"
    yellow = "\x1b[93m"
    red = "\x1b[91m"
    reset = "\x1b[0m"

    format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s (%(filename)s:%(lineno)d)"

    FORMATS = {
        logging.DEBUG: grey + format + reset,
        logging.INFO: green + format + reset,
        logging.WARNING: yellow + format + reset,
        logging.ERROR: red + format + reset,
        logging.CRITICAL: red + format + reset
    }

    def format(self, record):
        record.levelname = 'WARN' if record.levelname == 'WARNING' else record.levelname
        record.levelname = 'ERROR' if record.levelname == 'CRITICAL' else record.levelname
        log_fmt = self.FORMATS.get(record.levelno)
        formatter = logging.Formatter(log_fmt)
        return formatter.format(record)


class config():
    def __init__(self,config_path):
        self.config_path = config_path
        self.config_data = []

    @staticmethod
    def merge_custom_labels(file, defaults, section, name):
        '''
        返回 {label 名: (原始值, 'exported' | 'overwrite')}
        按名字合并，后面的优先：[DEFAULT] labels < [DEFAULT] labels_overwrite < job labels < job labels_overwrite
        空值表示这个 job 去掉该 label
        '''
        merged = {}
        for where, options in (('DEFAULT', defaults), (name, section)):
            for key, mode in CUSTOM_LABEL_KEYS:
                labels = parse_custom_labels(options.get(key, ''), '{} [{}] {}'.format(file, where, key))
                for label, value in labels.items():
                    merged[label] = (value, mode)
        return {label: v for label, v in merged.items() if v[0] != ''}

    def config_files(self):
        config_files = glob.glob(self.config_path)
        for f in config_files:
            self.get_config_data(f)
        return self.config_data

    def get_config_data(self,file):
        # [DEFAULT] 当普通 section 读再自己合并：[DEFAULT] 不是 job，labels 按名字合并而不是整个覆盖
        # 关闭 interpolation，label 值或 url 里的 % 不会报错
        config = configparser.ConfigParser(default_section='prom-textfile:no-default', interpolation=None)
        config.read(file)
        defaults = dict(config['DEFAULT']) if config.has_section('DEFAULT') else {}
        for k in config.sections():
            if k == 'DEFAULT':
                continue
            d = dict(defaults)
            d.update(config[k])
            if d:
                d['custom_labels'] = self.merge_custom_labels(file, defaults, dict(config[k]), k)
                s = os.path.splitext(os.path.basename(file))[0]
                d['prom_file_name'] = s + '_' + k
                logging.debug(
                    'file: {} ,config name: {} ,config : {}'.format(file, k, d))
                self.config_data.append(d)


def fetch_url(url):
    """
    Blocking download of the specified url, run in a worker thread by get_url
    """
    try:
        with urllib.request.urlopen(url, timeout=5.0) as request:
            msg = request.read()
            code = request.code
    except TimeoutError:
        logging.warning('get {}  ,time out!!!!!'.format(url))
        return '', 500
    except urllib.error.HTTPError as e:
        e.close()
        logging.warning('get {}  , {}'.format(url,e))
        return '', e.code
    except urllib.error.URLError as e:
        logging.warning('get {}  , {}'.format(url,e))
        return '', 500
    except Exception as e:
        logging.warning('get {}  , {}'.format(url,e))
        return '', 500

    logging.info('get {} status code : {}'.format(url, code))
    return msg.decode(), code


async def get_url(url):
    """
    A coroutine to download the specified url without blocking the event loop
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, fetch_url, url)


def write_file_atomic(file_path, lines):
    '''
    写临时文件再 rename，避免 node exporter 读到写了一半的 .prom
    '''
    tmp_path = file_path + '.tmp'
    with open(tmp_path, 'w') as f:
        f.writelines(lines)
    os.replace(tmp_path, file_path)


def escape_label_value(value):
    return value.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')


def parse_custom_labels(text, where):
    '''
    解析配置中的 labels：env=prod,region=us-east；值含逗号时用双引号：team="a,b"
    返回 {name: value}，value 为原始值（未转义）；空值保留，用来去掉 [DEFAULT] 里的同名 label
    名字不合法或是保留名（__ 开头、prom_cronjob_ 开头）时记录错误并忽略该项
    '''
    labels = {}
    text = text.strip()
    pos = 0
    while pos < len(text):
        m = CUSTOM_LABEL_RE.match(text, pos)
        if not m or m.end() == pos:
            logging.error('{} : invalid labels "{}" at position {}, ignore the rest'.format(where, text, pos))
            break
        pos = m.end()
        name, value = m.group(1), m.group(2)
        if value.startswith('"'):
            value = unescape_label_value(value[1:-1])
        if not LABEL_NAME_RE.match(name) or name.startswith('__') or name.startswith('prom_cronjob_'):
            logging.error('{} : invalid or reserved label name "{}", ignore'.format(where, name))
            continue
        labels[name] = value
    return labels


def unescape_label_value(value):
    return re.sub(r'\\(.)', lambda m: '\n' if m.group(1) == 'n' else m.group(1), value)


def parse_samples(text):
    '''
    解析 prom 文本，返回 [(labels, value)]，labels 含 __name__，label 值已反转义
    同一个 series 出现多次时只保留最后一个，避免 remote write 报 duplicate sample
    '''
    samples = {}
    for line in text.split('\n'):
        line = line.strip()
        if line == '' or line.startswith('#'):
            continue
        r = SAMPLE_RE.findall(line)
        if not r:
            logging.warning('remote write skip unparsable line : {}'.format(line))
            continue
        name, label, value, _ = r[0]
        try:
            value = float(value)
        except ValueError:
            logging.warning('remote write skip invalid value : {}'.format(line))
            continue
        labels = {k: unescape_label_value(v) for k, v in LABEL_RE.findall(label)}
        labels['__name__'] = name
        samples[tuple(sorted(labels.items()))] = (labels, value)
    return list(samples.values())


def _varint(n):
    n &= 0xFFFFFFFFFFFFFFFF
    out = bytearray()
    while True:
        b = n & 0x7f
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _pb_bytes(field, data):
    return _varint(field << 3 | 2) + _varint(len(data)) + data


def encode_write_request(samples, timestamp_ms):
    '''
    prometheus remote write 1.0 的 WriteRequest protobuf 编码
      WriteRequest { repeated TimeSeries timeseries = 1; }
      TimeSeries   { repeated Label labels = 1; repeated Sample samples = 2; }
      Label        { string name = 1; string value = 2; }
      Sample       { double value = 1; int64 timestamp = 2; }
    '''
    out = bytearray()
    for labels, value in samples:
        series = bytearray()
        for k in sorted(labels):
            series += _pb_bytes(1, _pb_bytes(1, k.encode()) + _pb_bytes(2, labels[k].encode()))
        sample = b'\x09' + struct.pack('<d', value) + b'\x10' + _varint(timestamp_ms)
        series += _pb_bytes(2, sample)
        out += _pb_bytes(1, bytes(series))
    return bytes(out)


def snappy_compress(data):
    '''
    snappy block 格式，只用 literal（不做压缩），标准库就能生成接收端可解码的数据
    '''
    out = bytearray(_varint(len(data)))
    for i in range(0, len(data), 65536):
        chunk = data[i:i + 65536]
        n = len(chunk) - 1
        if n < 60:
            out.append(n << 2)
        elif n < 256:
            out += bytes([60 << 2, n])
        else:
            out += bytes([61 << 2]) + struct.pack('<H', n)
        out += chunk
    return bytes(out)


class remote_write():
    '''
    抓到的数据先按抓取时间写入 <buffer_path>/<prom_file_name>/<timestamp_ms>.prom，
    再从旧到新上传；网络不可用时保留文件，恢复后补传
    '''

    def __init__(self, config: dict, buffer_path):
        self.url = config['prometheus_url']
        self.name = config.get('prom_file_name')
        self.buffer_dir = os.path.join(buffer_path, self.name)
        self.rejected_dir = os.path.join(self.buffer_dir, 'rejected')
        self.headers = {
            'Content-Encoding': 'snappy',
            'Content-Type': 'application/x-protobuf',
            'User-Agent': 'prom-textfile',
            'X-Prometheus-Remote-Write-Version': '0.1.0',
        }
        token = config.get('prometheus_bearer_token')
        username = config.get('prometheus_username')
        if token:
            self.headers['Authorization'] = 'Bearer ' + token
        elif username:
            auth = '{}:{}'.format(username, config.get('prometheus_password', ''))
            self.headers['Authorization'] = 'Basic ' + base64.b64encode(auth.encode()).decode()

    def save(self, lines, timestamp_ms):
        os.makedirs(self.rejected_dir, exist_ok=True)
        write_file_atomic(os.path.join(self.buffer_dir, '{}.prom'.format(timestamp_ms)), lines)

    def __files(self, path):
        '''
        返回 [(timestamp_ms, file, size)]，按时间从旧到新
        '''
        files = []
        for f in glob.glob(os.path.join(path, '*.prom')):
            try:
                files.append((int(os.path.basename(f)[:-5]), f, os.path.getsize(f)))
            except (ValueError, OSError):
                continue
        return sorted(files)

    def __retention(self, path, now_ms):
        files = self.__files(path)
        size = sum(s for _, _, s in files)
        dropped = 0
        for ts, f, s in files:
            if now_ms - ts <= BUFFER_MAX_AGE and size <= BUFFER_MAX_SIZE:
                break
            os.remove(f)
            size -= s
            dropped += 1
        if dropped:
            logging.warning('{} : drop {} buffered file(s) over {}h / {}MB in {}'.format(
                self.name, dropped, BUFFER_MAX_AGE // 3600000, BUFFER_MAX_SIZE // 1048576, path))

    def __send(self, body):
        request = urllib.request.Request(self.url, data=body, headers=self.headers, method='POST')
        try:
            with urllib.request.urlopen(request, timeout=10.0) as r:
                return r.code
        except urllib.error.HTTPError as e:
            with e:
                logging.warning('remote write {} : {} {}'.format(self.url, e.code, e.read()[:200]))
            return e.code
        except Exception as e:
            logging.warning('remote write {} : {}'.format(self.url, e))
            return None

    def flush(self):
        '''
        阻塞执行，返回 (up, 待上传文件数, rejected 文件数)；其他进程正在上传时返回 None
          2xx                 : 删除文件
          400                 : 数据本身有问题，移到 rejected/ 后继续下一个
          其他错误/网络不可用  : 保留文件，停止本轮，下个周期重试
        '''
        os.makedirs(self.rejected_dir, exist_ok=True)
        with open(os.path.join(self.buffer_dir, '.lock'), 'w') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                logging.info('{} : another process is uploading, skip'.format(self.name))
                return None

            now_ms = int(time.time() * 1000)
            self.__retention(self.buffer_dir, now_ms)
            self.__retention(self.rejected_dir, now_ms)

            up = 1
            sent = 0
            for ts, f, _ in self.__files(self.buffer_dir):
                with open(f) as rf:
                    samples = parse_samples(rf.read())
                code = self.__send(snappy_compress(encode_write_request(samples, ts)))
                if code is not None and 200 <= code < 300:
                    os.remove(f)
                    sent += 1
                elif code == 400:
                    logging.warning('{} : remote write rejected {}, move to {}'.format(
                        self.name, f, self.rejected_dir))
                    os.replace(f, os.path.join(self.rejected_dir, os.path.basename(f)))
                else:
                    up = 0
                    break

            remaining = len(self.__files(self.buffer_dir))
            rejected = len(self.__files(self.rejected_dir))
            logging.info('{} : remote write sent {} file(s), {} buffered, {} rejected'.format(
                self.name, sent, remaining, rejected))
            return up, remaining, rejected


class monitoring():

    def __init__(self,daemon):
        self.daemon = daemon

    def __count_with_file(self,file_path,name):
        '''
        计数文件读写失败只记录错误，不影响采集
        '''
        counter = 0
        try:
            with open(file_path,'r') as rf:
                count = rf.readlines()
            counter = int(count[-1])
        except FileNotFoundError:
            pass
        except (IndexError, ValueError):
            logging.warning('{} : invalid counter file {}, reset'.format(name, file_path))
        except OSError as e:
            logging.error('{} : read counter file {} failed: {}'.format(name, file_path, e))
        counter +=1
        logging.debug('__count_with_file :: {} {} {}'.format(name,file_path,counter))
        try:
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            write_file_atomic(file_path, str(counter))
        except OSError as e:
            logging.error('{} : write counter file {} failed: {}'.format(name, file_path, e))

        return counter

    def __count_with_men(self,name):
        try:
            self.counter +=1
        except AttributeError:
            self.counter = 1
        logging.debug('__count_with_men ::  {}  {}'.format(name,self.counter))

    def __write_monitoring_prom(self,file_path,count):
        monitor_metric_data = '''
# HELP prom_textfile_job_count job count
# TYPE prom_textfile_job_count counter
prom_textfile_job_count {}
'''.format(count)
        write_file_atomic(file_path, monitor_metric_data)


    async def monitoring_self(self,file_path,count_file):
        if self.daemon:
            while True:
                self.__count_with_men('monitoring-self')
                self.__write_monitoring_prom(file_path,self.counter)
                await asyncio.sleep(5)
        else:
            counter = self.__count_with_file(count_file,'monitoring-self')
            self.__write_monitoring_prom(file_path,counter)
            await asyncio.sleep(0.1)

    def monitoring_job(self,interval:int,count_file):
        # name = 
        # file_path = 
        if self.daemon:
            self.__count_with_men('monitoring-job')
            logging.debug('monitoring-job mem: {}'.format(self.counter))
            return self.counter
        else:
            logging.debug('monitoring-job file : {}'.format(count_file))
            count = self.__count_with_file(count_file,'monitoring-job')
            return count


class prom_metrics():

    def __init__(self, config: dict, prom_path, buffer_path=DEFAULT_BUFFER_PATH, state_path=DEFAULT_STATE_PATH):
        self.config = config
        self.prom_path = prom_path
        self.state_path = state_path
        # 配置了 prometheus_url 的 job 写缓存目录并 remote write，不再写 textfile 目录
        self.remote = remote_write(config, buffer_path) if config.get('prometheus_url') else None
        # self.metrics_data = {}
        self.default_label = {
            'prom_cronjob_name': config.get('name'),
            'prom_cronjob_url': config.get('url'),
            # daemon 按配置的 interval 循环；cron 模式由 timer 每 1 分钟跑一次
            'prom_cronjob_interval': config.get('interval') if self.config['daemon'] else 60
        }
        # 配置中的自定义 label：{名字: (转义为 prom 文本格式的值, 'exported' | 'overwrite')}
        self.custom_label = {k: (escape_label_value(v), mode)
                             for k, (v, mode) in config.get('custom_labels', {}).items()}


    def __metric_dict(self, metric: str):
        metric_dict = {
            'name': None,
            'value': None,
            'timestamp': self.get_timestamp,
            'label': {}
        }
        timestamp_count = len(self.get_timestamp)
        r = SAMPLE_RE.findall(metric)
        logging.debug(
            '__metric dict :: metric: {}  regex: {}'.format(metric, r))
        if r:
            metric_dict['name'] = r[0][0]
            if r[0][1] != '':
                metric_dict['label'] = self.__label_to_dict(r[0][1])
            metric_dict['value'] = r[0][2]
            if r[0][3] != '':
                if len(str(r[0][3])) > timestamp_count:
                    if abs(int(self.get_timestamp) - int(r[0][3][:timestamp_count])) > 600:
                        logging.debug('time 111 : : {}'.format(int(self.get_timestamp) - int(r[0][3][:timestamp_count])))
                        date_time = datetime.datetime.fromtimestamp(int(r[0][3][:timestamp_count]), datetime.timezone.utc)
                        metric_dict['metric_timestamp'] = date_time.strftime('%Y-%m-%d_%H:%M:%S+00')
                else:
                    if abs(int(self.get_timestamp) - int(r[0][3])) > 600:
                        logging.debug('timestamp : : {}'.format(int(self.get_timestamp) - int(r[0][3])))
                        date_time = datetime.datetime.fromtimestamp(int(r[0][3]))
                        metric_dict['metric_timestamp'] = str(r[0][3])
        if metric_dict['name'] == None:
            return ''
        else:
            return metric_dict

    async def __get_metrics(self):
        # # 需要处理超时/报错
        self.fetch_timestamp_ms = int(time.time() * 1000)
        self.get_timestamp = str(self.fetch_timestamp_ms // 1000)
        res, code = await get_url(self.config.get('url'))
        if code == 200:
            return res + '''
# HELP prom_textfile_job_up job counter
# TYPE prom_textfile_job_up counter
prom_textfile_job_up 1
'''
        else:
            return '''# HELP prom_textfile_job_up job counter
# TYPE prom_textfile_job_up counter
prom_textfile_job_up 0
'''

    def __recombine_line(self, line: str, custom=True):
        '''
        整理出最终的监控数据，并返回；custom=False 时不加自定义 label
        '''
        logging.debug('__recombine_line line : {}'.format(line))
        if len(re.findall(r'^#', line)) == 0 and line != '':
            metric = self.__metric_dict(line)
            logging.debug('__recombine_line metric : {}'.format(metric))
            if not metric:
                logging.warning('{} : skip unparsable line : {}'.format(
                    self.config.get('prom_file_name'), line))
                return ''
            end_metric = self.__replace_line(metric, custom)
            
            logging.debug('__recombine_line end_metric : {}'.format(end_metric))
            return end_metric
        elif line == '':
            pass
        else:
            return line+'\n'

    def __label_to_dict(self, label: str):
        '''
        label : label1="123",label2="abc"
        '''
        label_dict = {}

        # m_label = re.compile(',?(\S+?)="?([^",]+)')
        label_found = LABEL_RE.findall(label)
        logging.debug(
            '__label_to_dict :: label: {} ,  regex: {}'.format(label, label_found))
        for element in label_found:
            label, value = element
            label_dict[label] = value
        return label_dict

    def __label_to_promtext(self, label_dict: dict, custom=True):
        logging.debug(
            '__label_to_promtext ::  label_dict: {}'.format(label_dict))
        prom_label_text = '{'
        if custom:
            for name, (value, mode) in self.custom_label.items():
                # exported：和 prometheus honor_labels: false 一样，原 label 改名为 exported_<name>
                if name in label_dict and mode == 'exported':
                    exported = 'exported_' + name
                    while exported in label_dict:
                        exported = 'exported_' + exported
                    label_dict[exported] = label_dict[name]
                label_dict[name] = value
        label_dict.update(self.default_label)
        for d in sorted(label_dict.keys()):
            # label_dict[d]
            prom_label_text += '{}="{}",'.format(d, label_dict[d])
        prom_label_text = re.sub(r',$', '', prom_label_text)
        prom_label_text += '}'
        logging.debug('__label_to_promtext :: {}'.format(prom_label_text))
        return prom_label_text

    def __replace_line(self,line:dict, custom=True):
        '''
        更新label
        添加/修正 timestamp,按照timestamp排序
        'metric' : {
            'timestamp': [
                'metricname{label='123'} value'
            ]
        }
        '''

        logging.debug('replace {}'.format(line))
        if line.get('metric_timestamp'): line['label']['metric_timestamp'] = line.get('metric_timestamp')
        s = '{}{} {}\n'.format(line['name'], self.__label_to_promtext(line['label'], custom),
                                    line['value'])
        return s

    def __write_remote_status(self, up, buffer_files, rejected_files):
        '''
        remote write 状态写到本地 textfile，远端不可用时也能通过 node exporter 告警
        '''
        lines = [
            '# HELP prom_textfile_remote_write_up remote write accepted the last upload\n',
            '# TYPE prom_textfile_remote_write_up gauge\n',
            self.__recombine_line('prom_textfile_remote_write_up {}'.format(up), custom=False),
            '# HELP prom_textfile_remote_write_buffer_files buffered files waiting for upload\n',
            '# TYPE prom_textfile_remote_write_buffer_files gauge\n',
            self.__recombine_line('prom_textfile_remote_write_buffer_files {}'.format(buffer_files), custom=False),
            '# HELP prom_textfile_remote_write_rejected_files files rejected by remote write\n',
            '# TYPE prom_textfile_remote_write_rejected_files gauge\n',
            self.__recombine_line('prom_textfile_remote_write_rejected_files {}'.format(rejected_files), custom=False),
        ]
        write_file_atomic('{}/{}_remote_write.prom'.format(
            self.prom_path, self.config.get('prom_file_name')), lines)

    async def start_line(self):
        monitoring_count = monitoring(self.config['daemon'])
        while True:
            logging.info(self.config)
            # 获取监控数据
            metric_data = await self.__get_metrics()
            logging.debug('row metric data : {}'.format(metric_data))
            # 拆分数据
            metrics_data_list = list(metric_data.split('\n'))
            for n,l in enumerate(metrics_data_list):
                if l != '':
                    logging.debug('line : {} {}'.format(n,l))
                    metrics_data_list[n] = self.__recombine_line(l)

            # prom file
            prom_file = '{}/{}.prom'.format(self.prom_path,
                                            self.config.get('prom_file_name'))

            # monitoring job
            monitoring_job_count = monitoring_count.monitoring_job(int(self.config.get('interval')),os.path.join(self.state_path, '{}.txt'.format(self.config.get('prom_file_name'))))

            metrics_data_list.append('# HELP prom_textfile_job_count job count\n')
            metrics_data_list.append('# TYPE prom_textfile_job_count counter\n')
            metrics_data_list.append(self.__recombine_line('prom_textfile_job_count {}'.format(monitoring_job_count)))

            # write metric
            for n,d in enumerate(metrics_data_list):
                logging.debug('write line : {} {}'.format(n,d))
            if self.remote:
                self.remote.save(metrics_data_list, self.fetch_timestamp_ms)
                result = await asyncio.get_running_loop().run_in_executor(None, self.remote.flush)
                if result:
                    self.__write_remote_status(*result)
            else:
                write_file_atomic(prom_file, metrics_data_list)

                logging.info('{} : write prom file {} done.'.format(
                        self.config.get('prom_file_name'), prom_file))
            # run model
            if self.config['daemon']:
                await asyncio.sleep(int(self.config.get('interval')))
            else:
                break

async def run(config, prom_path, buffer_path, state_path):
    logging.debug('config : {}, prom_path: {}'.format(config,prom_path))
    p = prom_metrics(config, prom_path, buffer_path, state_path)
    await p.start_line()


def config_glob(config_path):
    '''
    -c 可以是单个配置文件，也可以是目录（读取其中所有 *.ini）
    '''
    if os.path.isfile(config_path):
        return config_path
    return os.path.join(config_path, '*.ini')


async def main(config_path, prom_path,daemon, buffer_path=DEFAULT_BUFFER_PATH, state_path=DEFAULT_STATE_PATH):
    config_data = config(config_path).config_files()
    logging.info('start config:{}'.format(config_data))
    if not config_data:
        logging.warning('no job found in config {}'.format(config_path))
    background_tasks = set()

    # add task for all config job
    for i in config_data:
        i['daemon'] = daemon
        task = asyncio.create_task(run(i, prom_path, buffer_path, state_path))
        background_tasks.add(task)

    # monitoring self
    logging.debug('prom_path : {}'.format(prom_path))
    monitoring_self = asyncio.create_task(monitoring(daemon).monitoring_self(
        os.path.join(prom_path, 'prom-textfile.prom'), os.path.join(state_path, 'prom-textfile-self.txt')))
    background_tasks.add(monitoring_self)
    # run
    await asyncio.gather(*background_tasks)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config_path', '-c', default='/etc/prom-textfile',
                        help='config file, or directory of *.ini files: default: /etc/prom-textfile')
    parser.add_argument('--prometheus_node_exporter_textfile_path', '-p', default='/var/lib/prometheus/node-exporter',
                        help='prometheus node exporter textfile path: default: /var/lib/prometheus/node-exporter')
    parser.add_argument('--state_path', '-s', default=DEFAULT_STATE_PATH,
                        help='cron mode run counter path: default: ' + DEFAULT_STATE_PATH)
    parser.add_argument('--buffer_path', '-b', default=DEFAULT_BUFFER_PATH,
                        help='remote write buffer path for jobs with prometheus_url: default: ' + DEFAULT_BUFFER_PATH)

    parser.add_argument('--daemon', action="store_true", help='daemon')
    parser.add_argument('--debug', action="store_true", help='debug')

    args = parser.parse_args()

    # set logging
    logger = logging.getLogger()
    ch = logging.StreamHandler()

    ch.setFormatter(ColorFormatter())
    logger.addHandler(ch)

    if args.debug:
        logger.setLevel(logging.DEBUG)
        ch.setLevel(logging.DEBUG)
    else:
        logger.setLevel(logging.INFO)
        ch.setLevel(logging.INFO)

    # logging config
    if args.daemon:
        logging.info('run with daemon!')
    else:
        logging.info('run with cron')

    logging.info('arg: {}'.format(args))
    # run
    try:
        asyncio.run(main(config_glob(args.config_path),
                         args.prometheus_node_exporter_textfile_path,args.daemon,
                         args.buffer_path, args.state_path))
    except KeyboardInterrupt:
        logging.info('end KeyboardInterrupt')
    except Exception:
        logging.exception('end Exception')
        logging.info('stop')
        sys.exit(1)
    logging.info('stop')
    sys.exit(0)

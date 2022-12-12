import argparse
import asyncio
import configparser
import glob
import logging
import re
import time
import urllib.request
import sys
import datetime
import os
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


def config(config_path):
    '''
    get config
    '''
    config = configparser.ConfigParser()
    # config.sections()

    config_file = glob.glob(config_path)
    config_data = []
    for f in config_file:
        config.read(f)
        for k in config.keys():
            d = dict(config[k])
            if d:
                s = f.split('/')[-1]
                d['prom_file_name'] = s[:-4] + '_' + k
                logging.debug(
                    'file: {} ,config name: {} ,config : {}'.format(f, k, d))
                config_data.append(d)
    return config_data


async def get_url(url):
    """
    A coroutine to download the specified url
    """
    try:
        request = urllib.request.urlopen(url, timeout=5.0)
    except TimeoutError:
        logging.warning('get {}  ,time out!!!!!'.format(url))
        return '', 500
    except urllib.error.URLError as e:
        logging.warning('get {}  , {}'.format(url,e))
        return '', 500
    except KeyboardInterrupt:
        logging.info('KeyboardInterrupt exit.')
        raise KeyboardInterrupt
    except:
        return '', 500

    msg = b''
    while True:
        chunk = request.read(1024)
        if not chunk:
            break

        msg = msg + chunk
    logging.info('get {} status code : {}'.format(url, request.code))
    return msg.decode(), request.code


class prom_metrics():

    def __init__(self, config: dict, prom_path):
        self.config = config
        self.prom_path = prom_path
        self.metrics_data = {}
        self.default_label = {
            'prom_cronjob_name': config.get('name'),
            'prom_cronjob_url': config.get('url'),
            'prom_cronjob_interval': 60 if self.config['daemon'] else config.get('interval')
        }

    def __metric_dict(self, metric: str):
        metric_dict = {
            'name': None,
            'value': None,
            'timestamp': self.get_timestamp,
            'label': {}
        }
        timestamp_count = len(self.get_timestamp)
        r = re.findall(
            r'^(\w+)\s?\{?(.+=.+?)?\}?\s([\d+|\-|\.|\+|e]*)\s?(\d+)?$', metric)
        logging.debug(
            '__metric dict :: metric: {}  regex: {}'.format(metric, r))
        if r:
            metric_dict['name'] = r[0][0]
            if r[0][1] != '':
                metric_dict['label'] = self.__label_to_dict(r[0][1])
            metric_dict['value'] = r[0][2]
            if r[0][3] != '':
                if len(str(r[0][3])) > timestamp_count:
                    date_time = datetime.datetime.fromtimestamp(int(r[0][3][:timestamp_count]))

                    metric_dict['metric_timestamp'] = date_time.strftime('%Y-%m-%d_%H:%M:%S+0')
                else:
                    date_time = datetime.datetime.fromtimestamp(int(r[0][3]))
                    metric_dict['metric_timestamp'] = str(r[0][3])
        else:
            metric_dict['name'] = 'prom_cronjob_up'
            metric_dict['value'] = '0'
        self.metrics_data[metric_dict['timestamp']] = []
        return metric_dict

    async def __get_metrics(self):
        # # 需要处理超时/报错
        self.get_timestamp = str(int(time.time()))
        res, code = await get_url(self.config.get('url'))
        if code == 200:
            return res
        else:
            return 'prom_cronjob_up 0'

    def __recombine(self, raw_metrics_data: str):
        self.metrics_data_recombine = []
        # 整理出是监控数据的数据，并转成dict
        for l in set(raw_metrics_data.split('\n')):
            if len(re.findall(r'^#', l)) == 0 and l != '':
                metric = self.__metric_dict(l)
                self.metrics_data_recombine.append(metric)

    def __label_to_dict(self, label: str):
        '''
        label : label1="123",label2="abc"
        '''
        label_dict = {}

        # m_label = re.compile(',?(\S+?)="?([^",]+)')
        m_label = re.compile(',?(\S+?)="(.+,?)"')
        label_found = m_label.findall(label)
        logging.debug(
            '__label_to_dict :: label: {} ,  regex: {}'.format(label, label_found))
        for element in label_found:
            label, value = element
            label_dict[label] = value
        return label_dict

    def __label_to_promtext(self, label_dict: dict):
        logging.debug(
            '__label_to_promtext ::  label_dict: {}'.format(label_dict))
        prom_label_text = '{'
        label_dict.update(self.default_label)
        for d in sorted(label_dict.keys()):
            # label_dict[d]
            prom_label_text += '{}="{}",'.format(d, label_dict[d])
        prom_label_text = re.sub(r',$', '', prom_label_text)
        prom_label_text += '}'
        logging.debug('__label_to_promtext :: {}'.format(prom_label_text))
        return prom_label_text

    def __replace(self):
        '''
        更新label
        添加/修正 timestamp,按照timestamp排序
        'metric' : {
            'timestamp': [
                'metricname{label='123'} value'
            ]
        }
        '''
        for i in self.metrics_data_recombine:
            logging.debug('replace {}'.format(i))
            if i.get('metric_timestamp'): i['label']['metric_timestamp'] = i.get('metric_timestamp')
            s = '{}{} {}\n'.format(i['name'], self.__label_to_promtext(i['label']),
                                      i['value'])
            self.metrics_data[i['timestamp']].append(s)

    async def start(self):

        while True:
            logging.info(self.config)
            # 获取监控数据
            metric_data = await self.__get_metrics()
            logging.debug('row metric data : {}'.format(metric_data))
            # 拆分数据
            self.__recombine(metric_data)
            logging.debug('recombine metric data : {}'.format(
                self.metrics_data_recombine))
            self.__replace()
            logging.debug('end metric data : {}'.format(self.metrics_data))
            prom_file = '{}/{}.prom'.format(self.prom_path,
                                            self.config.get('prom_file_name'))

            with open(prom_file, 'w') as f:
                for k in sorted(self.metrics_data.keys()):
                    f.writelines(set(self.metrics_data[k]))

            logging.info('{} : write prom file {} done.'.format(
                    self.config.get('prom_file_name'), prom_file))
            if self.config['daemon']:
                break
            else:
                await asyncio.sleep(int(self.config.get('interval')))
                


async def run(config, prom_path):
    logging.debug('config : {}, prom_path: {}'.format(config,prom_path))
    p = prom_metrics(config, prom_path)
    await p.start()


async def main(config_path, prom_path,daemon):
    config_data = config(config_path)
    logging.debug('start config:{}'.format(config_data))
    background_tasks = set()

    for i in config_data:
        i['daemon'] = daemon
        task = asyncio.create_task(run(i, prom_path))
        background_tasks.add(task)

    await asyncio.gather(*background_tasks)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config_path', '-c', default='/etc/prometheus-node-exporter-cronjob',
                        help='path: /etc/prometheus-node-exporter-cronjob')
    parser.add_argument('--prometheus_node_exporter_textfile_path', '-p', default='/var/lib/prometheus/node-exporter',
                        help='prometheus node exporter textfile path: default: /var/lib/prometheus/node-exporter')


    parser.add_argument('--daemon', action="store_false", help='daemon')
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
    logging.info('arg: {}'.format(args))
    # run
    try:
        asyncio.run(main(args.config_path+'/*.ini',
                         args.prometheus_node_exporter_textfile_path,args.daemon))
    except KeyboardInterrupt:
        logging.info('end KeyboardInterrupt')
    except Exception as e:
        logging.error('end Exception', e)
        sys.exit(1)
    finally:
        logging.info('stop')
        sys.exit(0)

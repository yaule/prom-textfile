#!/usr/bin/env python3

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


class monitoring():

    def __init__(self,daemon):
        self.daemon = daemon

    def __count_with_file(self,file_path,name):
        if os.path.isfile(file_path):
            with open(file_path,'r') as rf:
                count = rf.readlines()
                try:
                    counter = int(count[-1])
                except IndexError:
                    counter = 0
            counter +=1
        else:
            counter = 1
        logging.debug('__count_with_file :: {} {} {}'.format(name,file_path,counter))
        with open(file_path,'w') as wf:
            wf.writelines(str(counter))
        
        return counter

    def __count_with_men(self,name):
        try:
            self.counter +=1
        except AttributeError:
            self.counter = 1
        logging.debug('__count_with_men ::  {}  {}',name,self.counter)

    def __write_monitoring_prom(self,file_path,count):
        monitor_metric_data = '''
# HELP prom_textfile_job_count job count
# TYPE prom_textfile_job_count counter
prom_textfile_job_count {}
'''.format(count)
        with open(file_path,'w') as f:
            f.writelines(monitor_metric_data)


    async def monitoring_self(self,file_path):
        if self.daemon:
            while True:
                self.__count_with_men('monitoring-self')
                self.__write_monitoring_prom(file_path,self.counter)
                await asyncio.sleep(5)
        else:
            counter = self.__count_with_file('/tmp/prom-text-self.txt','monitoring-self')
            self.__write_monitoring_prom(file_path,counter)
            await asyncio.sleep(0.1)

    def monitoring_job(self,interval:int,count_file):
        # name = 
        # file_path = 
        if self.daemon:
            while True:
                self.__count_with_men('monitoring-job')
                logging.debug('monitoring-job mem: {}'.format(self.counter))
                return self.counter
        else:
            logging.debug('monitoring-job file : {}'.format(count_file))
            count = self.__count_with_file(count_file,'monitoring-job')
            return count


class prom_metrics():

    def __init__(self, config: dict, prom_path):
        self.config = config
        self.prom_path = prom_path
        # self.metrics_data = {}
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
                    if abs(int(self.get_timestamp) - int(r[0][3][:timestamp_count])) > 600:
                        logging.debug('time 111 : : {}'.format(int(self.get_timestamp) - int(r[0][3][:timestamp_count])))
                        date_time = datetime.datetime.fromtimestamp(int(r[0][3]))
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
        self.get_timestamp = str(int(time.time()))
        res, code = await get_url(self.config.get('url'))
        if code == 200:
            return res
        else:
            return '''# HELP prom_textfile_job_up job counter
# TYPE prom_textfile_job_up counter
prom_textfile_job_up 0
'''

    def __recombine_line(self, line: str):
        '''
        整理出最终的监控数据，并返回
        '''
        logging.debug('__recombine_line line : {}'.format(line))
        if len(re.findall(r'^#', line)) == 0 and line != '':
            metric = self.__metric_dict(line)
            logging.debug('__recombine_line metric : {}'.format(metric))
            end_metric = self.__replace_line(metric)
            
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

    def __replace_line(self,line:dict):
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
        s = '{}{} {}\n'.format(line['name'], self.__label_to_promtext(line['label']),
                                    line['value'])
        return s

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
            monitoring_job_count = monitoring_count.monitoring_job(int(self.config.get('interval')),'/tmp/{}.txt'.format(self.config.get('prom_file_name')))

            metrics_data_list.append('# HELP prom_textfile_job_count job count\n')
            metrics_data_list.append('# TYPE prom_textfile_job_count counter\n')
            metrics_data_list.append(self.__recombine_line('prom_textfile_job_count {}'.format(monitoring_job_count)))

            # write metric
            with open(prom_file, 'w') as f:
                for n,d in enumerate(metrics_data_list):
                    logging.debug('write line : {} {}'.format(n,d))
                    f.writelines(d)

            logging.info('{} : write prom file {} done.'.format(
                    self.config.get('prom_file_name'), prom_file))
            # run model
            if self.config['daemon']:
                await asyncio.sleep(int(self.config.get('interval')))
            else:
                break

async def run(config, prom_path):
    logging.debug('config : {}, prom_path: {}'.format(config,prom_path))
    p = prom_metrics(config, prom_path)
    await p.start_line()


async def main(config_path, prom_path,daemon):
    config_data = config(config_path)
    logging.debug('start config:{}'.format(config_data))
    background_tasks = set()

    # add task for all config job
    for i in config_data:
        i['daemon'] = daemon
        task = asyncio.create_task(run(i, prom_path))
        background_tasks.add(task)

    # monitoring self
    logging.debug('prom_path : {}'.format(prom_path))
    monitoring_self = asyncio.create_task(monitoring(daemon).monitoring_self(prom_path+'/prom-textfile.prom'))
    background_tasks.add(monitoring_self)
    # run
    await asyncio.gather(*background_tasks)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config_path', '-c', default='/etc/prometheus-node-exporter-cronjob',
                        help='path: /etc/prometheus-node-exporter-cronjob')
    parser.add_argument('--prometheus_node_exporter_textfile_path', '-p', default='/var/lib/prometheus/node-exporter',
                        help='prometheus node exporter textfile path: default: /var/lib/prometheus/node-exporter')


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

import asyncio
import aiofiles
import aiohttp
import time
import re
import configparser
import glob
import argparse
import logging


# config
# name  url  interval label'default label {prom_cronjob_metric="caddy"}' value times$(date +%s)
# caddy http://localhost:9001 10s {prom_cronjob_metric="caddy"} timestamp
# scripts


# asyncio
# read config
# timer 定时器
# get url
# replace metrics
# get file；sort ,uniq , clean and rewrite metric


def config(config_path):
    '''
    get config
    '''
    config = configparser.ConfigParser()
    # config.sections()

    config_file = glob.glob(config_path)
    config_data = []
    for f in config_file:
        f_path = config_path + '/' + f
        logging.info(f)
        config.read(f)
        config_data.append(dict(config['DEFAULT']))
    return config_data


class prom_metrics():

    def __init__(self, config: dict, prom_path):
        self.config = config
        self.prom_path = prom_path
        self.metrics_data = {}
        self.default_label = {
            'prom_cronjob_name': config.get('name'),
            'prom_cronjob_url': config.get('url'),
            'prom_cronjob_interval': config.get('interval')
        }

    def __metric_dict(self, metric: str):
        metric_dict = {
            'name': None,
            'value': None,
            'timestamp': None,
            'label': {}
        }
        timestamp_count = len(self.get_timestamp)
        r = re.findall(
            r'^(\w+)\s?\{?(.+=.+?)?\}?\s([\d+|\-|\.|\+|e]*)\s?(\d+)?$', metric)

        metric_dict['name'] = r[0][0]
        if r[0][1] != '':
            metric_dict['label'] = self.__label_to_dict(r[0][1])
        metric_dict['value'] = r[0][2]
        if r[0][3] != '':
            if len(str(r[0][3])) > timestamp_count:
                metric_dict['timestamp'] = str(r[0][3])[:timestamp_count]
        else:
            metric_dict['timestamp'] = self.get_timestamp

        self.metrics_data[metric_dict['timestamp']] = []
        return metric_dict

    async def __get_metrics(self):
        # req = urllib.request.Request(self.config.get('url'), method='GET')
        # # 需要处理超时/报错
        # response = urllib.request.urlopen(req, timeout=5.0)
        # self.get_timestamp = str(int(time.time()))
        # res = response.read().decode()
        async with aiohttp.ClientSession() as client:
            async with session.get(self.config.get('url')) as response:
                status_code = response.status
                res = await response.text()

        self.get_timestamp = str(int(time.time()))
        if status_code == 200:
            return res
        else:
            return 'prom_cronjob_up 0 {}'.format(self.get_timestamp)

    def __recombine(self, raw_metrics_data: str):
        self.metrics_data_recombine = []
        # 整理出是监控数据的数据，并转成dict
        for l in raw_metrics_data.split('\n'):
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
        for element in label_found:
            label, value = element
            label_dict[label] = value
        return label_dict

    def __label_to_promtext(self, label_dict: dict):
        prom_label_text = '{'
        label_dict.update(self.default_label)
        for d in sorted(label_dict.keys()):
            label_dict[d]
            prom_label_text += '{}="{}",'.format(d, label_dict[d])
        prom_label_text = re.sub(r',$', '', prom_label_text)
        prom_label_text += '}'
        return prom_label_text

    def __replace(self):
        '''
        更新label
        添加/修正 timestamp,按照timestamp排序
        'metric' : {
            'timestamp': [
                'metricname{label='123'} value timestamp'
            ]
        }
        '''
        for i in self.metrics_data_recombine:
            s = '{}{} {} {}\n'.format(i['name'], self.__label_to_promtext(i['label']),
                                      i['value'], i['timestamp'])
            self.metrics_data[i['timestamp']].append(s)

    async def start(self):
        print(self.config)
        print(self.config.get('url'))
        # 获取监控数据
        metrics_data = await self.__get_metrics()
        # 拆分数据
        self.__recombine(metrics_data)
        self.__replace()
        prom_file = '{}/{}.prom'.format(self.prom_path,
                                        self.config.get('name'))
        async with aiofiles.open(prom_file, 'w') as f:
            for k in sorted(self.metrics_data.keys()):
                await f.writelines(self.metrics_data[k])

async def run(config,prom_path):
    logging.debug('config : {}'.format(config))
    p = prom_metrics(config,prom_path)
    await p.start()

def main(config_path, prom_path):
    config_data = config(config_path)
    
    # for conf in config_data:
    #     logging.debug('config : {}'.format(conf))
    #     p = prom_metrics(conf,prom_path)
    #     p.start()
    loop = asyncio.get_event_loop()
    background_tasks = set()

    for i in config_data:
        task = asyncio.create_task(run(i,prom_path))

        # Add task to the set. This creates a strong reference.
        background_tasks.add(task)

        # To prevent keeping references to finished tasks forever,
        # make each task remove its own reference from the set after
        # completion:
        task.add_done_callback(background_tasks.discard)

    try:
        # Run the event loop
        loop.run_forever()
    finally:
        # We are done. Close sockets and the event loop.

        loop.close()

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config_path','-c',required=True, help='path: /etc/prometheus-node-exporter-cronjob')
    parser.add_argument('--prometheus_node_exporter_textfile_path','-p',default='/var/lib/prometheus/node-exporter/', help='prometheus node exporter textfile path: default: /var/lib/prometheus/node-exporter/')

    parser.add_argument('--debug', action="store_true", help='debug')

    args = parser.parse_args()
    LOG_FORMAT = "%(asctime)s - %(levelname)s - %(message)s"
    DATE_FORMAT = "%Y-%m-%d %H:%M:%S %p"
    if args.debug:
        logging.basicConfig(level=logging.DEBUG,
                            format=LOG_FORMAT, datefmt=DATE_FORMAT)
    else:
        logging.basicConfig(level=logging.INFO,
                            format=LOG_FORMAT, datefmt=DATE_FORMAT)

    main(args.config_path+'/*.ini', args.prometheus_node_exporter_textfile_path)


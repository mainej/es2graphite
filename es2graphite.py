#!/usr/bin/env python

import re
import sys
import json
import pickle
import struct
import logging
import logging.handlers
import time
import traceback
import socket
import urllib
import urllib2
import argparse
from datetime import datetime

NODES = {}
CLUSTER_NAME = ''
STATUS = {'red': 0, 'yellow': 1, 'green': 2}
SHARD_STATE = {'CREATED': 0, 'RECOVERING': 1, 'STARTED': 2, 'RELOCATED': 3, 'CLOSED': 4}
HOST_IDX = -1
loglevel = {    'info': logging.INFO,
                'warn': logging.WARN,
                'error': logging.ERROR,
                'debug': logging.DEBUG
            }



def timeit(method):
    def timed(*args, **kw):
        ts = time.time()
        result = method(*args, **kw)
        te = time.time()
        logging.debug( '%r %2.2f sec' % \
              (method.__name__, te-ts))
        return result
    return timed

def timeit_detailed(method):
    def timed(*args, **kw):
        ts = time.time()
        result = method(*args, **kw)
        te = time.time()
        logging.debug( '%r (%r, %r) %2.2f sec' % \
             (method.__name__, args, kw, te-ts))
        return result
    return timed

def log(what, force=False):
    logging.info(what)

def get_es_host():
    global HOST_IDX
    HOST_IDX = (HOST_IDX + 1) % len(args.es) # round-robin
    return args.es[HOST_IDX]

@timeit
def normalize(what):
    if not isinstance(what, (list, tuple)):
        return re.sub('\W+', '_',  what.strip().lower()).encode('utf-8')
    elif len(what) == 1:
        return normalize(what[0])
    else:
        return '%s.%s' % (normalize(what[0]), normalize(what[1:]))

@timeit
def add_metric(metrics, prefix, metric_path, stat, val, timestamp):
    if isinstance(val, bool):
        val = int(val)
    if isinstance(val, (str, unicode)):
        try:
            val = int(val)
        except ValueError:
            try:
                val = float(val)
            except ValueError:
                logging.debug('add_metric: Unable to convert to integer')

    if prefix[-1] == 'translog' and stat == 'id':
        return
    elif isinstance(val, (int, long, float)) and stat != 'timestamp':
        metrics.append((prefix + '.' + normalize((metric_path, stat)), (timestamp, val)))
    elif stat == 'status' and val in STATUS:
        metrics.append((prefix + '.' + normalize((metric_path, stat)), (timestamp, STATUS[val])))
    elif stat == 'state' and val in SHARD_STATE:
        metrics.append((prefix + '.' + normalize((metric_path, stat)), (timestamp, SHARD_STATE[val])))
        
@timeit
def process_cluster_health(prefix, health):
    metrics = []
    global CLUSTER_NAME
    CLUSTER_NAME = health['cluster_name']
    process_section(int(time.time()), metrics, prefix, (CLUSTER_NAME), health)
    return metrics

@timeit
def process_node_disk_allocation(prefix, allocation, cluster_name):
    metrics = []
    for node_idx in range(len(allocation)):
        node_allocation = allocation[node_idx]
        node_name = node_allocation['node']
        node_allocation = {key: node_allocation[key] for key in node_allocation if key not in ['node', 'host', 'ip']}
        process_section(int(time.time()), metrics, prefix, (CLUSTER_NAME, node_name, 'disk'), node_allocation)
    return metrics

@timeit
def process_node_memory_allocation(prefix, allocation, cluster_name):
    metrics = []
    for node_idx in range(len(allocation)):
        node_allocation = allocation[node_idx]
        node_name = node_allocation['name']
        node_allocation = {key: node_allocation[key] for key in node_allocation if key not in ['name']}
        process_section(int(time.time()), metrics, prefix, (CLUSTER_NAME, node_name, 'memory'), node_allocation)
    return metrics

@timeit
def process_node_load(prefix, load, cluster_name):
    metrics = []
    for node_idx in range(len(load)):
        node_load = load[node_idx]
        node_name = node_load['name']
        node_load = {key: node_load[key] for key in node_load if key not in ['name']}
        process_section(int(time.time()), metrics, prefix, (CLUSTER_NAME, node_name, 'os'), node_load)
    return metrics

@timeit
def process_thread_pool(prefix, load, cluster_name):
    metrics = []
    for thread_idx in range(len(load)):
        thread_pool = load[thread_idx]
        node_name = thread_pool['host']
        thread_pool = {key: thread_pool[key] for key in thread_pool if key not in ['host']}
        process_section(int(time.time()), metrics, prefix, (CLUSTER_NAME, node_name, 'thread_pool'), thread_pool)
    return metrics

@timeit
def process_indices_status(prefix, status):
    metrics = []
    process_section(int(time.time()), metrics, prefix, (CLUSTER_NAME, 'indices'), status['indices'])
    return metrics
    
@timeit
def process_indices_stats(prefix, stats):
    metrics = []
    process_section(int(time.time()), metrics, prefix, (CLUSTER_NAME, 'indices', '_all'), stats['_all'])
    if args.stats_level != 'cluster':
        process_section(int(time.time()), metrics, prefix, (CLUSTER_NAME, 'indices'), stats['indices'])
    return metrics
    
@timeit
def process_segments_status(prefix, status):
    metrics = []
    process_section(int(time.time()), metrics, prefix, (CLUSTER_NAME, 'indices'), status['indices'])
    return metrics
    
@timeit
def process_section(timestamp, metrics, prefix, metric_path, section):
    for stat in section:
        stat_val = section[stat]
        if 'timestamp' in section:
            timestamp = int(section['timestamp'] / 1000) # es has epoch in ms, graphite needs seconds

        if isinstance(stat_val, dict):
            process_section(timestamp, metrics, prefix, (metric_path, stat), stat_val)
        elif isinstance(stat_val, list):
            if prefix[-1] == 'fs' and stat == 'data':
                for disk in stat_val:
                    mount = disk['mount']
                    process_section(timestamp, metrics, prefix, (metric_path, stat, mount), disk)
            elif prefix[-1] == 'os' and stat == 'load_average':
                add_metric(metrics, prefix, metric_path, (stat, '1min_avg'), stat_val[0], timestamp)
                add_metric(metrics, prefix, metric_path, (stat, '5min_avg'), stat_val[1], timestamp)
                add_metric(metrics, prefix, metric_path, (stat, '15min_avg'), stat_val[2], timestamp)
            elif prefix[-1] == 'shards' and re.match('\d+', stat) is not None:
                for shard in stat_val:
                    shard_node = NODES[shard['routing']['node']]
                    process_section(timestamp, metrics, prefix, (metric_path, stat, shard_node), shard)
            else:
                for stat_idx, sub_stat_val in enumerate(stat_val):
                    if isinstance(sub_stat_val, dict):
                        process_section(timestamp, metrics, prefix, (metric_path, stat, str(stat_idx)), sub_stat_val)
                    else:
                        add_metric(metrics, prefix, metric_path, (stat, str(stat_idx)), sub_stat_val, timestamp)
        else:
            add_metric(metrics, prefix, metric_path, stat, stat_val, timestamp)

@timeit
def submit_to_graphite(metrics):
    if not args.dry_run:
        graphite_socket = {'socket': socket.socket( socket.AF_INET, socket.SOCK_STREAM ), 
                           'host': args.graphite_host, 
                           'port': int(args.graphite_port)}
        graphite_socket['socket'].connect( ( graphite_socket['host'], graphite_socket['port'] ) )


    if args.protocol == 'pickle':
        if args.dry_run:
            for m, mval  in metrics:
                if not args.silent:
                    logging.info('%s %s = %s' % (mval[0], m, mval[1]))
        else:
            try:
                payload = pickle.dumps(metrics)
                header = struct.pack('!L', len(payload))
                graphite_socket['socket'].sendall( "%s%s" % (header, payload) )
            except socket.error, serr:
                logging.error('Communication to Graphite server failed: ' + str(serr))
                logging.debug(urllib.quote_plus(traceback.format_exc()))
    elif args.protocol == 'plaintext':
        for metric_name, metric_list in metrics:
            metric_string = "%s %s %d" % ( metric_name, metric_list[1], metric_list[0])
            if args.dry_run:
                if not args.silent:
                    logging.info('Metric String: ' + metric_string)
            else:
                try:
                    graphite_socket['socket'].send( "%s\n" % metric_string )
                except socket.error, serr:
                    logging.error('Communicartion to Graphite server failed: ' + str(serr))
                    logging.debug(urllib.quote_plus(traceback.format_exc()))
    else:
        logging.error('Unsupported Protocol.')
        sys.exit(1)
    if not args.dry_run:
        graphite_socket['socket'].close()

@timeit
def get_metrics():
    dt = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
 
    cluster_health_url = 'http://%s/_cluster/health?level=%s' % (get_es_host(), args.health_level)
    log('%s: GET %s' % (dt, cluster_health_url))
    cluster_health_data = urllib2.urlopen(cluster_health_url).read()
    cluster_health = json.loads(cluster_health_data)
    cluster_health_metrics = process_cluster_health(args.prefix, cluster_health)
    submit_to_graphite(cluster_health_metrics)

    node_disk_allocation_url = 'http://%s/_cat/allocation?format=json&bytes=b' % get_es_host()
    log('%s: GET %s' % (dt, node_disk_allocation_url))
    node_disk_allocation_data = urllib2.urlopen(node_disk_allocation_url).read()
    node_disk_allocation = json.loads(node_disk_allocation_data)
    node_disk_allocation_metrics = process_node_disk_allocation(args.prefix, node_disk_allocation, cluster_health['cluster_name'])
    submit_to_graphite(node_disk_allocation_metrics)

    node_memory_allocation_url = 'http://%s/_cat/nodes?format=json&bytes=b&h=heapPercent,heapMax,ramPercent,ramMax,name' % get_es_host()
    log('%s: GET %s' % (dt, node_memory_allocation_url))
    node_memory_allocation_data = urllib2.urlopen(node_memory_allocation_url).read()
    node_memory_allocation = json.loads(node_memory_allocation_data)
    node_memory_allocation_metrics = process_node_memory_allocation(args.prefix, node_memory_allocation, cluster_health['cluster_name'])
    submit_to_graphite(node_memory_allocation_metrics)

    node_load_url = 'http://%s/_cat/nodes?format=json&bytes=b&h=load,name' % get_es_host()
    log('%s: GET %s' % (dt, node_load_url))
    node_load_data = urllib2.urlopen(node_load_url).read()
    node_load = json.loads(node_load_data)
    node_load_metrics = process_node_load(args.prefix, node_load, cluster_health['cluster_name'])
    submit_to_graphite(node_load_metrics)

    thread_pool_url = 'http://%s/_cat/thread_pool?format=json&h=host,bulk.active,bulk.queue,bulk.rejected,index.active,index.queue,index.rejected,search.active,search.queue,search.rejected' % get_es_host()
    log('%s: GET %s' % (dt, thread_pool_url))
    thread_pool_data = urllib2.urlopen(thread_pool_url).read()
    thread_pool = json.loads(thread_pool_data)
    thread_pool_metrics = process_thread_pool(args.prefix, thread_pool, cluster_health['cluster_name'])
    submit_to_graphite(thread_pool_metrics)

    if args.stats_level != 'none':
        indices_stats_url = 'http://%s/_stats?all=true&level=%s' % (get_es_host(), args.stats_level)
        log('%s: GET %s' % (dt, indices_stats_url))
        indices_stats_data = urllib2.urlopen(indices_stats_url).read()
        indices_stats = json.loads(indices_stats_data)
        indices_stats_metrics = process_indices_stats(args.prefix, indices_stats)
        submit_to_graphite(indices_stats_metrics)
   
    if args.segments:
        segments_status_url = 'http://%s/_segments' % get_es_host()
        log('%s: GET %s' % (dt, segments_status_url))
        segments_status_data = urllib2.urlopen(segments_status_url).read()
        segments_status = json.loads(segments_status_data)
        segments_status_metrics = process_segments_status(args.prefix, segments_status)
        submit_to_graphite(segments_status_metrics)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Send elasticsearch metrics to graphite')
    parser.add_argument('-p', '--prefix', default='es', help='graphite metric prefix. Default: %(default)s')
    parser.add_argument('-g', '--graphite-host', default='localhost', help='graphite hostname. Default: %(default)s')
    parser.add_argument('-o', '--graphite-port', default=2004, type=int, help='graphite port. Default: %(default)s')
    parser.add_argument('-i', '--interval', default=60, type=int, help='interval in seconds. Default: %(default)s')
    parser.add_argument('-l', '--log-file', default='./es2graphite.log', type=str, help='full path to the log file. Default: %(default)s')
    parser.add_argument('--health-level', choices=['cluster', 'indices', 'shards'], default='indices', help='The level of health metrics. Default: %(default)s')
    parser.add_argument('--stats-level', choices=['none', 'cluster', 'indices', 'shards'], help='The level of stats metrics. Default: same as --health-level')
    parser.add_argument('--log-level', choices=['info', 'warn', 'error', 'debug'], default='warn', help='The logging level. Default: %(default)s')
    parser.add_argument('--protocol', choices=['plaintext', 'pickle'], default='pickle', help='The graphite submission protocol. Default: %(default)s')
    parser.add_argument('-s', '--silent', action='store_true', help='Silence metric printing to logs or stdout. Default: %(default)s')
    parser.add_argument('--stdout', action='store_true', help='output logging to stdout. Default: %(default)s')
    parser.add_argument('--shard-stats', action='store_true', help='Collect shard level stats metrics. Default: %(default)s')
    parser.add_argument('--segments', action='store_true', help='Collect low-level segment metrics. Default: %(default)s')
    parser.add_argument('-d', '--dry-run', action='store_true', help='Print metrics, don\'t send to graphite. Default: %(default)s')
    parser.add_argument('-v', '--verbose', action='store_true', help='Verbose output. Default: %(default)s')
    parser.add_argument('es', nargs='+', help='elasticsearch host:port', metavar='ES_HOST')
    args = parser.parse_args()
    if args.stats_level is None:
        args.stats_level = args.health_level
    if args.shard_stats:
        args.stats_level = 'shards'


    root_logger = logging.getLogger()
    logFormatter = logging.Formatter("%(asctime)s [%(threadName)-5.12s] [%(levelname)-8.8s]  %(message)s")
    if args.log_level.lower() == 'debug':
        logFormatter = logging.Formatter("%(asctime)s [%(threadName)-5.12s %(filename)-20.20s:%(funcName)-5.5s:%(lineno)-3d] [%(levelname)-8.8s]  %(message)s")
    if args.stdout:
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(logFormatter)
        root_logger.addHandler(stream_handler)
    else:
        file_handler = logging.handlers.RotatingFileHandler("{0}".format(args.log_file), 
                                                            maxBytes=100000000, 
                                                            backupCount=5)
        root_logger.addHandler(file_handler)
        file_handler.setFormatter(logFormatter)

    root_logger.setLevel(loglevel[args.log_level])

    while True:
        try:
            if args.dry_run:
                logging.warn('Metric not Submitted. Processing as a Dry Run.')
                get_metrics()
                sys.exit()
            else:
                get_metrics()
                completion_minute = datetime.now().minute
                while datetime.now().minute == completion_minute:
                    logging.debug('Waiting to run.. (' + str(completion_minute) + ')')
                    time.sleep(1)
        except Exception, e:
            logging.error(urllib.quote_plus(traceback.format_exc()))

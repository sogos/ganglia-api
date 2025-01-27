#!/usr/bin/env python
########################################
#
# ganglia-api.py - Ganglia Metric API
#
########################################

import datetime
import glob
import logging
import os
import re
import select
import socket
import sys
import time
from xml.etree import ElementTree
from xml.parsers.expat import ExpatError

try:
    from urllib import quote  # Python 2.X
except ImportError:
    from urllib.parse import quote  # Python 3+
from threading import Thread

import tornado.httpserver
import tornado.ioloop
import tornado.options
import tornado.web
from tornado.options import define, options

import settings

__version__ = '2.0.0'

ganglia_config = None
ganglia_data = None

define("port", default=8080, help="run on the given port", type=int)

logging.basicConfig(level=logging.DEBUG if settings.DEBUG else logging.INFO,
                    format="%(asctime)s %(name)s[%(process)d] %(levelname)s - %(message)s",
                    filename=settings.LOGFILE)

logger = logging.getLogger("ganglia-api")


class Elem:
    def __init__(self, elem):
        self.elem = elem

    def __getattr__(self, name):
        return self.elem.get(name.upper())


class NullElem:
    def __getattr__(self, name):
        return None


class ApiMetric:
    tag_re = re.compile("\s+")

    def id(self):
        group = self.group if self.group is not None else ""
        id_elements = [self.environment, self.grid.name, self.cluster.name, self.host.name, group, self.name]
        return str.lower(".".join(filter(None,id_elements)))

    def api_dict(self):
        type, units = ApiMetric.metric_type(self.type, self.units, self.slope)
        metric = {'environment': self.environment,
                  'grid': self.grid.name,
                  'cluster': self.cluster.name,
                  'host': self.host.name,
                  'id': self.id(),
                  'metric': self.name,
                  'instance': self.instance,
                  'group': self.group,
                  'title': self.title,
                  'tags': ApiMetric.parse_tags(self.host.tags),
                  'description': self.desc,
                  'sum': self.sum,
                  'num': self.num,
                  'value': ApiMetric.is_num(self.val),
                  'units': units,
                  'type': type,
                  'sampleTime': datetime.datetime.fromtimestamp(
                      int(self.host.reported) + int(self.host.tn) - int(self.tn)).isoformat() + ".000Z",
                  'graphUrl': self.graph_url,
                  'dataUrl': self.data_url}
        return dict((k, v) for k, v in metric.items() if v is not None)

    @staticmethod
    def parse_tags(tag_string):
        return tag_string.split(',')

    @staticmethod
    def metric_type(type, units, slope):
        if units == 'timestamp':
            return 'timestamp', 's'
        if 'int' in type or type == 'float' or type == 'double':
            return 'gauge', units
        if type == 'string':
            return 'text', units
        return 'undefined', units

    @staticmethod
    def is_num(val):
        try:
            return int(val)
        except ValueError:
            pass
        try:
            return float(val)
        except ValueError:
            return val

    def __str__(self):
        return "%s %s %s %s %s %s" % (
            self.environment, self.grid.name, self.cluster.name, self.host.name, self.group, self.name)


class Metric(Elem, ApiMetric):
    def __init__(self, elem, host, cluster, grid, environment):
        self.host = host
        self.cluster = cluster
        self.grid = grid
        self.environment = environment
        Elem.__init__(self, elem)
        self.metadata = dict()
        for extra_data in elem.findall("EXTRA_DATA"):
            for extra_elem in extra_data.findall("EXTRA_ELEMENT"):
                name = extra_elem.get("NAME")
                if name:
                    self.metadata[name] = extra_elem.get('VAL')

        original_metric_name = self.name

        try:
            self.metadata['NAME'], self.metadata['INSTANCE'] = self.name.split('-', 1)
        except ValueError:
            self.metadata['INSTANCE'] = ''

        if self.name in ['fs_util', 'inode_util']:
            if self.instance == 'rootfs':
                self.metadata['INSTANCE'] = '/'
            else:
                self.metadata['INSTANCE'] = '/' + '/'.join(self.instance.split('-'))

        params = {"environment": self.environment,
                  "grid": self.grid.name,
                  "cluster": self.cluster.name,
                  "host": self.host.name,
                  "metric": original_metric_name}
        url = '%s%s/metrics?' % (settings.API_SERVER, settings.BASE_URL)
        for (k, v) in params.items():
            if v is not None: url += "&%s=%s" % (k, quote(v))
        self.data_url = url

        params = {"c": self.cluster.name,
                  "h": self.host.name,
                  "v": "0",
                  "m": original_metric_name,
                  "r": "1day",
                  "z": "default",
                  "vl": self.units.replace('%', 'percent'),
                  "ti": self.title}
        url = '%sgraph.php?' % self.grid.authority
        for (k, v) in params.items():
            if v is not None: url += "&%s=%s" % (k, quote(v))
        self.graph_url = url

    def __getattr__(self, name):
        try:
            if self.metadata.has_key(name.upper()):
                return self.metadata[name.upper()]
            else:
                return Elem.__getattr__(self, name)
        except AttributeError:
            return None

    def html_dir(self):
        return 'ganglia-' + self.environment + '-' + self.grid.name


# Artificial metric generated from the Host
class HeartbeatMetric(ApiMetric):
    def __init__(self, host, cluster, grid, environment):
        self.host = host
        self.cluster = cluster
        self.grid = grid
        self.environment = environment
        self.val = int(host.tn)
        self.tn = 0
        self.tags = host.tags
        self.name = "heartbeat"
        self.group = "ganglia"
        self.title = "Ganglia Agent Heartbeat"
        self.desc = "Ganglia agent heartbeat in seconds"
        self.type = 'uint16'
        self.units = 'seconds'
        self.slope = 'both'

    def __getattr__(self, name):
        return None


class GangliaGmetad:
    hostname = "localhost"

    def __init__(self, environment, xml_port, interactive_port):
        self.environment = environment
        self.xml_port = xml_port
        self.interactive_port = interactive_port

    def read_data_from_port(self, host, port, send=None):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.connect((host, port))
            r, w, x = select.select([sock], [], [], 2)
            if not r:
                sock.close()
                return
        except socket.error as e:
            logger.warning('Could not open socket to %s:%d - %s', host, port, e)
            return

        try:
            if send is not None: sock.send(send)
        except socket.error as e:
            logger.warning('Could not send to %s:%d - %s', host, port, e)
            return

        buffer = ""
        while True:
            try:
                data = sock.recv(8192)
            except socket.error as e:
                logger.warning('Could not receive data from %s:%d - %s', host, port, e)
                return

            if not data: break
            buffer += data.decode("ISO-8859-1")

        sock.close()
        return buffer

    def read_xml_data(self):
        return self.read_data_from_port(self.hostname, self.xml_port)

    def read_xml_metrics(self):
        result = list()

        xml_data = self.read_xml_data()
        if xml_data:
            try:
                ganglia = ElementTree.XML(xml_data)
            except UnicodeEncodeError:
                logging.error('Could not parse XML data')
                return result
        else:
            return result

        for grid_elem in ganglia.findall("GRID"):
            grid = Elem(grid_elem)
            for cluster_elem in grid_elem.findall("CLUSTER"):
                cluster = Elem(cluster_elem)
                for host_elem in cluster_elem.findall("HOST"):
                    host = Elem(host_elem)
                    result.append(HeartbeatMetric(host, cluster, grid, self.environment))
                    for metric_elem in host_elem.findall("METRIC"):
                        result.append(Metric(metric_elem, host, cluster, grid, self.environment))

        return result

    def read_interactive_data(self):
        return self.read_data_from_port(self.hostname, self.interactive_port, '/?filter=summary\r\n')

    def read_interactive_metrics(self):
        result = list()

        interactive_data = self.read_interactive_data()
        if interactive_data:
            try:
                ganglia = ElementTree.XML(interactive_data)
            except ExpatError as e:
                logging.error('Could not parse XML data: %s', e)
                return result
        else:
            return result

        for grid_elem in ganglia.findall("GRID"):
            grid = Elem(grid_elem)
            for cluster_elem in grid_elem.findall("CLUSTER"):
                cluster = Elem(cluster_elem)
                for metric_elem in cluster_elem.findall("METRICS"):
                    result.append(Metric(metric_elem, NullElem(), cluster, grid, self.environment))

        return result

    def read_metrics(self):
        xml_metrics = self.read_xml_metrics()
        interactive_metrics = self.read_interactive_metrics()
        xml_metrics.extend(interactive_metrics)
        return xml_metrics


class GangliaConfig:

    def __init__(self):
        self.environments = self.parse_ganglia_config()

    def parse_ganglia_config(self):
        logger.info("Parsing ganglia configurations")
        result = dict()
        for file in glob.glob(os.path.join(settings.GANGLIA_PATH, 'gmetad*.conf')):
            m = re.search('gmetad-(\S+).conf', file)
            if m:
                environment = m.group(1)
            else:
                environment = 'all'

            xml_port = 0
            interactive_port = 0
            f = open(file).readlines()
            for line in f:
                m = re.search('xml_port\s+(\d+)', line)
                if m:
                    xml_port = int(m.group(1))
                m = re.search('interactive_port\s+(\d+)', line)
                if m:
                    interactive_port = int(m.group(1))

            ports = GangliaGmetad(environment, xml_port, interactive_port)

            result[environment] = ports
            logger.info('Found %s (env=%s) with ports %d and %d', file, environment, ports.xml_port,
                        ports.interactive_port)

        return result

    def get_gmetad_config(self):
        return self.environments.values()

    def get_gmetad_for(self, environment):
        def is_match(gmetad):
            return (not environment) or (gmetad.environment in environment)

        return filter(is_match, self.environments.values())


class GmetadData():
    def __init__(self):
        self.data = dict()

    def update(self, gmetad):
        logger.info("  getting metrics for %s", gmetad.environment)
        gmetad_metrics = gmetad.read_metrics()
        logger.info("  updated %d metrics for %s", len(gmetad_metrics), gmetad.environment)
        self.data[gmetad.environment] = gmetad_metrics
        return len(gmetad_metrics)

    def metrics_for(self, environment):
        try:
            return self.data[environment]
        except KeyError:
            return list()

    def metrics(self, gmetad):
        return self.metrics_for(gmetad.environment)


class GangliaPollThread(Thread):
    def run(self):
        while True:
            self.update_ganglia_data()

    def update_ganglia_data(self):
        gmetad_list = ganglia_config.get_gmetad_config()
        logger.info("Updating data from gmetad...")
        total_metrics = 0
        for counter, gmetad in enumerate(gmetad_list):
            metrics_for_gmetad = ganglia_data.update(gmetad)
            total_metrics += metrics_for_gmetad
            logger.debug("  (%d/%d) updated %d metrics for %s", counter + 1, len(gmetad_list), metrics_for_gmetad,
                         gmetad.environment)
            time.sleep(0.2)

        logger.info("Done (found %d metrics)", total_metrics)


class ApiHandler(tornado.web.RequestHandler):
    def get(self):
        start = time.time()
        environment = self.get_arguments("environment")
        grid = self.get_arguments("grid")
        metric_list = self.get_arguments("metric")
        group_list = self.get_arguments("group")
        host_list = self.get_arguments("host")
        cluster_list = self.get_arguments("cluster")

        def emptyOrContains(list, value):
            return len(list) == 0 or value in list

        def is_match(metric):
            return (emptyOrContains(metric_list, metric.name)
                    and emptyOrContains(group_list, metric.group)
                    and emptyOrContains(host_list, metric.host.name)
                    and emptyOrContains(cluster_list, metric.cluster.name)
                    and emptyOrContains(grid, metric.grid.name))

        gmetad_list = ganglia_config.get_gmetad_for(environment)
        metric_dicts = list()
        for gmetad in gmetad_list:
            metrics = ganglia_data.metrics(gmetad)
            for metric in filter(is_match, metrics):
                metric_dicts.append(metric.api_dict())

        response = {
            "metrics": metric_dicts,
            "status": "ok",
            "total": len(metric_dicts),
            "time": "%.3f" % (time.time() - start)
        }
        self.write(response)


def main():
    logger.info("Starting up Ganglia metric API v%s", __version__)

    # Write pid file if not already running
    if os.path.isfile(settings.PIDFILE):
        pid = open(settings.PIDFILE).read()
        try:
            os.kill(int(pid), 0)
            logging.error('Process with pid %s already exists, exiting', pid)
            sys.exit(1)
        except OSError:
            pass

    open(settings.PIDFILE, 'w').write(str(os.getpid()))

    global ganglia_config
    ganglia_config = GangliaConfig()
    global ganglia_data
    ganglia_data = GmetadData()
    poll_thread = GangliaPollThread()
    poll_thread.daemon = True
    poll_thread.start()

    tornado.options.parse_command_line()
    application = tornado.web.Application([
        (r"%s/metrics" % settings.BASE_URL, ApiHandler),
    ])
    http_server = tornado.httpserver.HTTPServer(application)
    http_server.listen(options.port)
    tornado.ioloop.IOLoop.instance().start()


if __name__ == "__main__":
    main()

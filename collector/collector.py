#!/usr/bin/python
"""
Retrieves and collects data from the the NetApp E-series web server
and sends the data to an influxdb server
"""
import struct
import time
import logging
import socket
import argparse
import concurrent.futures
import requests
import json
from datetime import datetime

from influxdb import InfluxDBClient

try:
    import cPickle as pickle
except ImportError:
    import pickle

DEFAULT_USERNAME = 'admin'
DEFAULT_PASSWORD = 'admin'

DEFAULT_SYSTEM_NAME = 'unnamed'

INFLUXDB_HOSTNAME = 'influxdb'
INFLUXDB_PORT = 8086
INFLUXDB_DATABASE = 'eseries'

__version__ = '1.0'

#######################
# LIST OF METRICS######
#######################

DRIVE_PARAMS = [
    'averageReadOpSize',
    'averageWriteOpSize',
    'combinedIOps',
    'combinedResponseTime',
    'combinedThroughput',
    'otherIOps',
    'readIOps',
    'readOps',
    'readPhysicalIOps',
    'readResponseTime',
    'readThroughput',
    'writeIOps',
    'writeOps',
    'writePhysicalIOps',
    'writeResponseTime',
    'writeThroughput'
]

SYSTEM_PARAMS = [
    "maxCpuUtilization",
    "cpuAvgUtilization"
]

VOLUME_PARAMS = [
    'averageReadOpSize',
    'averageWriteOpSize',
    'combinedIOps',
    'combinedResponseTime',
    'combinedThroughput',
    'flashCacheHitPct',
    'flashCacheReadHitBytes',
    'flashCacheReadHitOps',
    'flashCacheReadResponseTime',
    'flashCacheReadThroughput',
    'otherIOps',
    'queueDepthMax',
    'queueDepthTotal',
    'readCacheUtilization',
    'readHitBytes',
    'readHitOps',
    'readIOps',
    'readOps',
    'readPhysicalIOps',
    'readResponseTime',
    'readThroughput',
    'writeCacheUtilization',
    'writeHitBytes',
    'writeHitOps',
    'writeIOps',
    'writeOps',
    'writePhysicalIOps',
    'writeResponseTime',
    'writeThroughput'
]

MEL_PARAMS = [
    'id',
    'description',
    'location'
]


#######################
# PARAMETERS###########
#######################

NUMBER_OF_THREADS = 10

# LOGGING
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
requests.packages.urllib3.disable_warnings()
LOG = logging.getLogger("collector")

# Disables reset connection warning message if the connection time is too long
logging.getLogger('requests.packages.urllib3.connectionpool').setLevel(logging.WARNING)


#######################
# ARGUMENT PARSER######
#######################

PARSER = argparse.ArgumentParser()

PARSER.add_argument('-u', '--username', default='',
                    help='Provide the username used to connect to the Web Services Proxy. '
                         'If not specified, will check for the \'/collector/config.json\' file. '
                         'Otherwise, it will default to \'' + DEFAULT_USERNAME + '\'')
PARSER.add_argument('-p', '--password', default='',
                    help='Provide the password for this user to connect to the Web Services Proxy. '
                         'If not specified, will check for the \'/collector/config.json\' file. '
                         'Otherwise, it will default to \'' + DEFAULT_PASSWORD + '\'')
PARSER.add_argument('-t', '--intervalTime', type=int, default=5,
                    help='Provide the time (seconds) in which the script polls and sends data '
                         'from the SANtricity webserver to the influxdb backend. '
                         'If not specified, will use the default time of 60 seconds. <time>')
PARSER.add_argument('--proxySocketAddress', default='webservices',
                    help='Provide both the IP address and the port for the SANtricity webserver. '
                         'If not specified, will default to localhost. <IPv4 Address:port>')
PARSER.add_argument('-s', '--showStorageNames', action='store_true',
                    help='Outputs the storage array names found from the SANtricity webserver')
PARSER.add_argument('-v', '--showVolumeNames', action='store_true', default=0,
                    help='Outputs the volume names found from the SANtricity webserver')
PARSER.add_argument('-a', '--showVolumeMetrics', action='store_true', default=0,
                    help='Outputs the volume payload metrics before it is sent')
PARSER.add_argument('-d', '--showDriveNames', action='store_true', default=0,
                    help='Outputs the drive names found from the SANtricity webserver')
PARSER.add_argument('-b', '--showDriveMetrics', action='store_true', default=0,
                    help='Outputs the drive payload metrics before it is sent')
PARSER.add_argument('-c', '--showSystemMetrics', action='store_true', default=0,
                    help='Outputs the system payload metrics before it is sent')
PARSER.add_argument('-m', '--showMELMetrics', action='store_true', default=0,
                    help='Outputs the MEL payload metrics before it is sent')
PARSER.add_argument('-e', '--showStateMetrics', action='store_true', default=0,
                    help='Outputs the state payload metrics before it is sent')
PARSER.add_argument('-i', '--showIteration', action='store_true', default=0,
                    help='Outputs the current loop iteration')
PARSER.add_argument('-n', '--doNotPost', action='store_true', default=0,
                    help='Pull information, but do not post to influxdb')
CMD = PARSER.parse_args()
PROXY_BASE_URL = 'http://{}/devmgr/v2/storage-systems'.format(CMD.proxySocketAddress)

#######################
# HELPER FUNCTIONS#####
#######################

def get_configuration():
    try:
        with open("config.json") as config_file:
            config_data = json.load(config_file)
            if config_data:
                return config_data
    except:
        return dict()


def get_session():
    """
    Returns a session with the appropriate content type and login information.
    :return: Returns a request session for the SANtricity RestAPI Webserver
    """
    request_session = requests.Session()

    # Try to use what was passed in for username/password...
    username = CMD.username
    password = CMD.password
    
    # ...if there was nothing passed in then try to read it from config file
    if ((username is None or username == "") and (password is None or password == "")):
        # Try to read username and password from config file, if it exists
        # Otherwise default to DEFAULT_USERNAME/DEFAULT_PASSWORD
        try:
            with open("config.json") as config_file:
                config_data = json.load(config_file)
                if (config_data):
                    username = config_data["username"]
                    password = config_data["password"]
        except:
            LOG.exception("Unable to open \"/collector/config.json\" file")
            username = DEFAULT_USERNAME
            password = DEFAULT_PASSWORD

    request_session.auth = (username, password)
    request_session.headers = {"Accept": "application/json",
                               "Content-Type": "application/json",
                               "netapp-client-type": "grafana-" + __version__}
    # Ignore the self-signed certificate issues for https
    request_session.verify = False
    return request_session


def get_drive_location(storage_id, session):
    """
    :param storage_id: Storage system ID on the Webserver
    :param session: the session of the thread that calls this definition
    ::return: returns a dictionary containing the disk id matched up against
    the tray id it is located in:
    """
    hardware_list = session.get("{}/{}/hardware-inventory".format(
        PROXY_BASE_URL, storage_id)).json()
    tray_list = hardware_list["trays"]
    drive_list = hardware_list["drives"]
    tray_ids = {}
    drive_location = {}

    for tray in tray_list:
        tray_ids[tray["trayRef"]] = tray["trayId"]

    for drive in drive_list:
        drive_tray = drive["physicalLocation"]["trayRef"]
        tray_id = tray_ids.get(drive_tray)
        if tray_id != "none":
            drive_location[drive["driveRef"]] = [tray_id, drive["physicalLocation"]["slot"]]
        else:
            LOG.error("Error matching drive to a tray in the storage system")
    return drive_location

def collect_storage_metrics(sys):
    """
    Collects all defined storage metrics and posts them to influxdb
    :param sys: The JSON object of a storage_system
    """
    try:
        session = get_session()
        client = InfluxDBClient(host=INFLUXDB_HOSTNAME, port=INFLUXDB_PORT, database=INFLUXDB_DATABASE)

        sys_id = sys["id"]
        sys_name = sys.get("name", sys_id)
        # If this storage device lacks a name, use the id
        if not sys_name or len(sys_name) <= 0:
            sys_name = sys_id
        # If this storage device still lacks a name, use a default
        if not sys_name or len(sys_name) <= 0:
            sys_name = DEFAULT_SYSTEM_NAME

        json_body = list()

        # Get Drive statistics
        drive_stats_list = session.get(("{}/{}/analysed-drive-statistics").format(
            PROXY_BASE_URL, sys_id)).json()
        drive_locations = get_drive_location(sys_id, session)
        if CMD.showDriveNames:
            for stats in drive_stats_list:
                location_send = drive_locations.get(stats["diskId"])
                LOG.info(("Tray{:02.0f}, Slot{:03.0f}").format(location_send[0], location_send[1]))
        # Add Drive statistics to json body
        for stats in drive_stats_list:
            disk_location_info = drive_locations.get(stats["diskId"])
            disk_item = dict(
                measurement = "disks",
                tags = dict(
                    sys_id = sys_id,
                    sys_name = sys_name,
                    sys_tray = ("{:02.0f}").format(disk_location_info[0]),
                    sys_tray_slot = ("{:03.0f}").format(disk_location_info[1])
                ),
                fields = dict(
                    (metric, stats.get(metric)) for metric in DRIVE_PARAMS
                )
            )
            if CMD.showDriveMetrics:
                LOG.info("Drive payload: %s", disk_item)
            json_body.append(disk_item)

        # Get System statistics
        system_stats_list = session.get(("{}/{}/analysed-system-statistics").format(
            PROXY_BASE_URL, sys_id)).json()
        # Add System statistics to json body
        sys_item = dict(
            measurement = "systems",
            tags = dict(
                sys_id = sys_id,
                sys_name = sys_name
            ),
            fields = dict(
                (metric, system_stats_list.get(metric)) for metric in SYSTEM_PARAMS
            )
        )
        if CMD.showSystemMetrics:
            LOG.info("System payload: %s", sys_item)
        json_body.append(sys_item)
        
        # Get Volume statistics
        volume_stats_list = session.get(("{}/{}/analysed-volume-statistics").format(
            PROXY_BASE_URL, sys_id)).json()
        if CMD.showVolumeNames:
            for stats in volume_stats_list:
                LOG.info(stats["volumeName"]);
        # Add Volume statistics to json body
        for stats in volume_stats_list:
            vol_item = dict(
                measurement = "volumes",
                tags = dict(
                    sys_id = sys_id,
                    sys_name = sys_name,
                    vol_name = stats["volumeName"]
                ),
                fields = dict(
                    (metric, stats.get(metric)) for metric in VOLUME_PARAMS
                )
            )
            if CMD.showVolumeMetrics:
                LOG.info("Volume payload: %s", vol_item)
            json_body.append(vol_item)

        if not CMD.doNotPost:
            client.write_points(json_body, database=INFLUXDB_DATABASE, time_precision="s")

    except RuntimeError:
        LOG.error(("Error when attempting to post statistics for {}/{}").format(sys["name"], sys["id"]))


def collect_major_event_log(sys):
    """
    Collects all defined MEL metrics and posts them to influxdb
    :param sys: The JSON object of a storage_system
    """
    try:
        session = get_session()
        client = InfluxDBClient(host=INFLUXDB_HOSTNAME, port=INFLUXDB_PORT, database=INFLUXDB_DATABASE)
        
        sys_id = sys["id"]
        sys_name = sys.get("name", sys_id)
        # If this storage device lacks a name, use the id
        if not sys_name or len(sys_name) <= 0:
            sys_name = sys_id
        # If this storage device still lacks a name, use a default
        if not sys_name or len(sys_name) <= 0:
            sys_name = DEFAULT_SYSTEM_NAME
        
        json_body = list()
        start_from = -1
        mel_grab_count = 8192
        query = client.query("SELECT id FROM major_event_log WHERE sys_id='%s' ORDER BY time DESC LIMIT 1" % sys_id)

        if query:
            start_from = int(next(query.get_points())["id"]) + 1
            
        mel_response = session.get(("{}/{}/mel-events").format(PROXY_BASE_URL, sys_id),
                                   params = {"count": mel_grab_count, "startSequenceNumber": start_from}).json();
        if CMD.showMELMetrics:
            LOG.info("Starting from %s", str(start_from))
            LOG.info("Grabbing %s MELs", str(len(mel_response)))
        for mel in mel_response:
            item = dict(
                measurement = "major_event_log",
                tags = dict(
                    sys_id = sys_id,
                    sys_name = sys_name,
                    event_type = mel["eventType"],
                    time_stamp = mel["timeStamp"],
                    category = mel["category"],
                    priority = mel["priority"],
                    critical = mel["critical"],
                    ascq = mel["ascq"],
                    asc = mel["asc"]
                ),
                fields = dict(
                    (metric, mel.get(metric)) for metric in MEL_PARAMS
                ),
                time = datetime.utcfromtimestamp(int(mel["timeStamp"])).isoformat()
            )
            if CMD.showMELMetrics:
                LOG.info("MEL payload: %s", item)
            json_body.append(item)
        
        client.write_points(json_body, database=INFLUXDB_DATABASE, time_precision="s")
    except RuntimeError:
        LOG.error(("Error when attempting to post MEL for {}/{}").format(sys["name"], sys["id"]))


def collect_system_state(sys):
    """
    Collects state information from the storage system and posts it to influxdb
    :param sys: The JSON object of a storage_system
    """
    try:
        session = get_session()
        client = InfluxDBClient(host=INFLUXDB_HOSTNAME, port=INFLUXDB_PORT, database=INFLUXDB_DATABASE)
        
        sys_id = sys["id"]
        sys_name = sys.get("name", sys_id)
        # If this storage device lacks a name, use the id
        if not sys_name or len(sys_name) <= 0:
            sys_name = sys_id
        # If this storage device still lacks a name, use a default
        if not sys_name or len(sys_name) <= 0:
            sys_name = DEFAULT_SYSTEM_NAME
        
        json_body = list()
        query = client.query("SELECT * FROM failures WHERE sys_id='%s'" % sys_id)
                    
        failure_response = session.get(("{}/{}/failures").format(PROXY_BASE_URL, sys_id)).json();
        for failure in failure_response:
            found = False
            fail_type = failure["failureType"]
            obj_ref = failure["objectRef"]
            obj_type = failure["objectType"]

            # check to see if we've seen this failure before
            if query:
                failure_points = (query.get_points(measurement='failures'))
                for point in failure_points:
                    if fail_type == point.failure_type and obj_ref == point.object_ref and obj_type == point.object_type:
                        found = True
                        break
            # if this is a new failure, we want to post it to influxdb
            if not found:
                item = dict(
                    measurement = "failures",
                    tags = dict(
                        sys_id = sys_id,
                        sys_name = sys_name,
                        failure_type = fail_type,
                        object_ref = obj_ref,
                        object_type = obj_type
                    ),
                    fields = dict(
                        value = True
                    ),
                    time = datetime.utcnow().isoformat()
                )
                if CMD.showStateMetrics:
                    LOG.info("Failure payload: %s", item)
                json_body.append(item)
        
        num = len(json_body)
        if num > 0:
            LOG.info("Found %s new failures", str(num))
        client.write_points(json_body, database=INFLUXDB_DATABASE, time_precision="s")
    except RuntimeError:
        LOG.error(("Error when attempting to post state information for {}/{}").format(sys["name"], sys["id"]))


#######################
# MAIN FUNCTIONS#######
#######################

if __name__ == "__main__":
    executor = concurrent.futures.ProcessPoolExecutor(NUMBER_OF_THREADS)
    SESSION = get_session()
    loopIteration = 1

    client = InfluxDBClient(host=INFLUXDB_HOSTNAME, port=INFLUXDB_PORT, database=INFLUXDB_DATABASE)

    client.create_database(INFLUXDB_DATABASE)

    try:
        # Ensure we can connect. Wait for 2 minutes for WSP to startup.
        SESSION.get(PROXY_BASE_URL, timeout=120)
        configuration = get_configuration()
        for system in configuration.get("storage_systems", list()):
            LOG.info("system: %s", str(system))
            body = dict(controllerAddresses=system.get("addresses"),
                        password=system.get("password") or configuration.get("array_password"),
                        acceptCertificate=True)
            response = SESSION.post(PROXY_BASE_URL, json=body)
            response.raise_for_status()
    except requests.exceptions.HTTPError or requests.exceptions.ConnectionError:
        LOG.exception("Failed to add configured systems!")
    except json.decoder.JSONDecodeError:
        LOG.exception("Failed to open configuration file due to invalid JSON!")

    while True:
        time_start = time.time()
        try:
            response = SESSION.get(PROXY_BASE_URL)
            if response.status_code != 200:
                LOG.warning("We were unable to retrieve the storage-system list! Status-code={}".format(response.status_code))
        except requests.exceptions.HTTPError or requests.exceptions.ConnectionError as e:
            LOG.warning("Unable to connect to the Web Services instance to get storage-system list!", e)
        except Exception as e:
            LOG.warning("Unexpected exception!", e)
        else:
            storageList = response.json()
            LOG.info("Names: %s", len(storageList))
            if CMD.showStorageNames:
                for storage in storageList:
                    storage_name = storage["name"]
                    if not storage_name or len(storage_name) <= 0:
                        storage_name = storage["id"]
                    if not storage_name or len(storage_name) <= 0:
                        storage_name = DEFAULT_STORAGE_NAME
                    LOG.info(storage_name)

            # Iterate through all storage systems and collect metrics
            collector = [executor.submit(collect_storage_metrics, sys) for sys in storageList]
            concurrent.futures.wait(collector)

            # Iterate through all storage system and collect state information
            collector = [executor.submit(collect_system_state, sys) for sys in storageList]
            concurrent.futures.wait(collector)

            # Iterate through all storage system and collect MEL entries
            collector = [executor.submit(collect_major_event_log, sys) for sys in storageList]
            concurrent.futures.wait(collector)

        time_difference = time.time() - time_start
        if CMD.showIteration:
            LOG.info("Time interval: {:07.4f} Time to collect and send:"
                     " {:07.4f} Iteration: {:00.0f}"
                     .format(CMD.intervalTime, time_difference, loopIteration))
            loopIteration += 1

        # Dynamic wait time to get the proper interval
        wait_time = CMD.intervalTime - time_difference
        if CMD.intervalTime < time_difference:
            LOG.error("The interval specified is not long enough. Time used: {:07.4f} "
                      "Time interval specified: {:07.4f}"
                      .format(time_difference, CMD.intervalTime))
            wait_time = time_difference
        time.sleep(wait_time)

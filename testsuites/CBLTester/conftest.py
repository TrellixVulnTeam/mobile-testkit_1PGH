import pytest
import time

from keywords.utils import log_info
from utilities.cluster_config_utils import persist_cluster_config_environment_prop
from keywords.ClusterKeywords import ClusterKeywords
from keywords.couchbaseserver import CouchbaseServer
from keywords.constants import CLUSTER_CONFIGS_DIR
from keywords.SyncGateway import sync_gateway_config_path_for_mode
from keywords.exceptions import ProvisioningError
from keywords.tklogging import Logging
from CBLClient.Replication import Replication
from CBLClient.Replicator_new import Replicator
from CBLClient.ReplicatorConfiguration import ReplicatorConfiguration
from CBLClient.Database import Database
from keywords.utils import host_for_url


def pytest_addoption(parser):
    parser.addoption("--mode",
                     action="store",
                     help="Sync Gateway mode to run the test in, 'cc' for channel cache or 'di' for distributed index")

    parser.addoption("--skip-provisioning",
                     action="store_true",
                     help="Skip cluster provisioning at setup",
                     default=False)

    parser.addoption("--server-version",
                     action="store",
                     help="server-version: Couchbase Server version to install (ex. 4.5.0 or 4.5.0-2601)")

    parser.addoption("--sync-gateway-version",
                     action="store",
                     help="sync-gateway-version: Sync Gateway version to install (ex. 1.3.1-16 or 590c1c31c7e83503eff304d8c0789bdd268d6291)")

    parser.addoption("--liteserv-platform",
                     action="store",
                     help="liteserv-platform: the platform to assign to the liteserv")

    parser.addoption("--liteserv-version",
                     action="store",
                     help="liteserv-version: the version to download / install for the liteserv")

    parser.addoption("--liteserv-host",
                     action="store",
                     help="liteserv-host: the host to start liteserv on")

    parser.addoption("--liteserv-port",
                     action="store",
                     help="liteserv-port: the port to assign to liteserv")

    parser.addoption("--enable-sample-bucket",
                     action="store",
                     help="enable-sample-bucket: Enable a sample server bucket")

    parser.addoption("--xattrs",
                     action="store_true",
                     help="xattrs: Enable xattrs for sync gateway")


# This will get called once before the first test that
# runs with this as input parameters in this file
# This setup will be called once for all tests in the
# testsuites/listener/shared/client_sg/ directory
@pytest.fixture(scope="session")
def params_from_base_suite_setup(request):
    liteserv_platform = request.config.getoption("--liteserv-platform")
    liteserv_version = request.config.getoption("--liteserv-version")
    liteserv_host = request.config.getoption("--liteserv-host")
    liteserv_port = request.config.getoption("--liteserv-port")

    skip_provisioning = request.config.getoption("--skip-provisioning")
    sync_gateway_version = request.config.getoption("--sync-gateway-version")
    mode = request.config.getoption("--mode")

    server_version = request.config.getoption("--server-version")
    enable_sample_bucket = request.config.getoption("--enable-sample-bucket")
    xattrs_enabled = request.config.getoption("--xattrs")
    base_url = "http://{}:{}".format(liteserv_host, liteserv_port)
    cluster_config = "{}/base_{}".format(CLUSTER_CONFIGS_DIR, mode)

    try:
        server_version
    except NameError:
        log_info("Server version is not provided")
        persist_cluster_config_environment_prop(cluster_config, 'server_version', "")
    else:
        log_info("Running test with server version {}".format(server_version))
        persist_cluster_config_environment_prop(cluster_config, 'server_version', server_version)

    try:
        sync_gateway_version
    except NameError:
        log_info("Sync gateway version is not provided")
        persist_cluster_config_environment_prop(cluster_config, 'sync_gateway_version', "")
    else:
        log_info("Running test with sync_gateway version {}".format(sync_gateway_version))
        persist_cluster_config_environment_prop(cluster_config, 'sync_gateway_version', sync_gateway_version)

    if xattrs_enabled:
        log_info("Running test with xattrs for sync meta storage")
        persist_cluster_config_environment_prop(cluster_config, 'xattrs_enabled', True)
    else:
        log_info("Using document storage for sync meta data")
        persist_cluster_config_environment_prop(cluster_config, 'xattrs_enabled', False)

    sg_config = sync_gateway_config_path_for_mode("sync_gateway_travel_sample", mode)
    cluster_utils = ClusterKeywords()
    cluster_topology = cluster_utils.get_cluster_topology(cluster_config)
    
    if not skip_provisioning:
        log_info("Installing Sync Gateway + Couchbase Server + Accels ('di' only)")

        try:
            cluster_utils.provision_cluster(
                cluster_config=cluster_config,
                server_version=server_version,
                sync_gateway_version=sync_gateway_version,
                sync_gateway_config=sg_config
            )
        except ProvisioningError:
            logging_helper = Logging()
            logging_helper.fetch_and_analyze_logs(cluster_config=cluster_config, test_name=request.node.name)
            raise

    if enable_sample_bucket:
        server_url = cluster_topology["couchbase_servers"][0]
        server = CouchbaseServer(server_url)

        buckets = server.get_bucket_names()
        if enable_sample_bucket in buckets:
            server.delete_buckets()
            time.sleep(5)
            server.load_sample_bucket(enable_sample_bucket)
            server._create_internal_rbac_bucket_user(enable_sample_bucket)
            # Sleep for a while for SG to import all docs
            time.sleep(120)

    # Create CBL database
    cbl_db = "test_db"
    db = Database(base_url)
    sg_db = "db"
    sg_url = cluster_topology["sync_gateways"][0]["public"]
    sg_ip = host_for_url(sg_url)
    target_url = "blip://{}:4985/{}".format(sg_ip, sg_db)

    log_info("Creating a Database {}".format(cbl_db))
    source_db = db.create(cbl_db)
    log_info("Getting the database name")
    db_name = db.getName(source_db)
    assert db_name == "test_db"

    # Start continuous replication
#     replicator = Replication(base_url)
#     log_info("Configuring replication")
#     repl = replicator.configure(source_db, target_url)
#     log_info("Starting replication")
#     replicator.start(repl)
    replicator_obj = Replicator(base_url)
    config_obj = ReplicatorConfiguration(base_url)
    repl = config_obj.create(source_db, targetURI=target_url)
    replicator = replicator_obj.create(repl)
    #print replicator_obj.getStatus(replicator)
    # Wait for replication to complete
    time.sleep(120)

    yield {
        "cluster_config": cluster_config,
        "mode": mode,
        "xattrs_enabled": xattrs_enabled,
        "liteserv_platform": liteserv_platform,
        "cluster_topology": cluster_topology,
        "liteserv_version": liteserv_version,
        "liteserv_host": liteserv_host,
        "liteserv_port": liteserv_port,
        "target_url": target_url,
        "sg_ip": sg_ip,
        "sg_db": sg_db,
        "source_db": source_db,
        "cbl_db": cbl_db
    }

#     log_info("Stopping replication")
#     replicator.stop_replication(repl)


@pytest.fixture(scope="function")
def params_from_base_test_setup(request, params_from_base_suite_setup):
    cluster_config = params_from_base_suite_setup["cluster_config"]
    xattrs_enabled = params_from_base_suite_setup["xattrs_enabled"]
    liteserv_host = params_from_base_suite_setup["liteserv_host"]
    liteserv_port = params_from_base_suite_setup["liteserv_port"]
    test_name = request.node.name
    cluster_topology = params_from_base_suite_setup["cluster_topology"]
    mode = params_from_base_suite_setup["mode"]
    target_url = params_from_base_suite_setup["target_url"]
    sg_ip = params_from_base_suite_setup["sg_ip"]
    sg_db = params_from_base_suite_setup["sg_db"]
    source_db = params_from_base_suite_setup["source_db"]
    cbl_db = params_from_base_suite_setup["cbl_db"]
    cluster_helper = ClusterKeywords()
    cluster_hosts = cluster_helper.get_cluster_topology(cluster_config=cluster_config)
    sg_url = cluster_hosts["sync_gateways"][0]["public"]
    sg_admin_url = cluster_hosts["sync_gateways"][0]["admin"]

    log_info("Running test '{}'".format(test_name))
    log_info("cluster_config: {}".format(cluster_config))
    log_info("cluster_topology: {}".format(cluster_topology))
    log_info("mode: {}".format(mode))
    log_info("xattrs_enabled: {}".format(xattrs_enabled))

    # This dictionary is passed to each test
    yield {
        "cluster_config": cluster_config,
        "cluster_topology": cluster_topology,
        "mode": mode,
        "sg_url": sg_url,
        "sg_admin_url": sg_admin_url,
        "xattrs_enabled": xattrs_enabled,
        "liteserv_host": liteserv_host,
        "liteserv_port": liteserv_port,
        "target_url": target_url,
        "sg_ip": sg_ip,
        "sg_db": sg_db,
        "source_db": source_db,
        "cbl_db": cbl_db
    }
